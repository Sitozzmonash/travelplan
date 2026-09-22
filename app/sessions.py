"""Planning Session（引导式旅程的"规划前会话"）。

解决三个问题（用户旅程落地任务 §3）：
  1. 用户还在点偏好时不该产生正式 Run（关掉页面 ≠ 一次 FAILED）；
  2. Discovery 可以提前跑，用户点选时后台已经把真实候选查好了；
  3. 只有用户点「开始规划」才创建 run_id，并带上他表达过的偏好与 POI 选择。

状态机（**不要**和 Run 的 RUNNING/SUCCESS/... 混用）：

    COLLECTING ──► DISCOVERING ──► READY ──► STARTING ──► （正式 run）
         │              │            │
         └──────────────┴────────────┴──► CANCELLED / EXPIRED

Session 过期（默认 45 分钟）后允许重建；旧 Discovery 结果不再复用（避免用陈旧价格排行程）。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from app import city_cache, discovery
from app.config import current_config
from app.workflow import DEFAULT_OUTPUT_DIR
from app.models import TripIntent, coerce_float, coerce_str, coerce_str_list
from app.selection import (
    HOTEL_PRIORITIES,
    PACE_LABELS,
    PLACE_CATEGORY_LABELS,
    TRANSPORT_MODES,
    TRANSPORT_PRIORITIES,
    normalize_hotel_priority,
    normalize_pace,
    normalize_place_selections,
    normalize_transport_constraints,
    normalize_transport_mode,
    normalize_transport_priority,
    place_category,
    place_reason,
)
from app.store import TravelPlanStore

#: 会话状态（与 Run 状态刻意分开命名）。
SESSION_COLLECTING = "COLLECTING"
SESSION_DISCOVERING = "DISCOVERING"
SESSION_READY = "READY"
SESSION_STARTING = "STARTING"
SESSION_CANCELLED = "CANCELLED"
SESSION_EXPIRED = "EXPIRED"
SESSION_STATUSES = (
    SESSION_COLLECTING,
    SESSION_DISCOVERING,
    SESSION_READY,
    SESSION_STARTING,
    SESSION_CANCELLED,
    SESSION_EXPIRED,
)

#: Discovery 状态。
DISCOVERY_PENDING = "PENDING"
DISCOVERY_RUNNING = "RUNNING"
DISCOVERY_READY = "READY"
DISCOVERY_PARTIAL = "PARTIAL"
DISCOVERY_FAILED = "FAILED"

#: 用户可以在 PATCH 里改的字段（白名单，避免把任意键写进会话）。
PATCHABLE_FIELDS = (
    "transport_mode",
    "transport_priority",
    "transport_constraints",
    "hotel_priority",
    "hotel_max_price_per_night",
    "hotel_min_rating",
    "hotel_min_star",
    "hotel_room_type",
    "hotel_allow_change",
    "pace",
    "budget_total",
)


class RecommendationInputError(ValueError):
    """确认事务中的推荐/选择校验失败；抛出后事务回滚，不产生 Run。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    return f"ps-{stamp}-{uuid.uuid4().hex[:6]}"


# ======================================================================
# 创建 / 读取 / 修改
# ======================================================================


def create_session(
    store: TravelPlanStore,
    payload: Mapping[str, Any],
    *,
    submit: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """建会话并（可选）立刻在后台开始 Prefetch。

    `submit` 由 API 层注入（进程内线程池）；不传就是只建会话不取数（测试用）。
    """

    config = current_config()
    now = _now()
    ttl = max(5, config.planning_session_ttl_minutes)
    basic = {
        "origin": coerce_str(payload.get("origin")) or None,
        "destination": coerce_str(payload.get("destination")) or None,
        "start_date": coerce_str(payload.get("start_date")) or None,
        "end_date": coerce_str(payload.get("end_date")) or None,
        "days": int(coerce_float(payload.get("days")) or 0) or None,
        "travelers": int(coerce_float(payload.get("travelers")) or 0) or 1,
        "budget_total": coerce_float(payload.get("budget_total")),
    }
    session = {
        "session_id": new_session_id(),
        "status": SESSION_COLLECTING,
        "discovery_status": DISCOVERY_PENDING,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl)).isoformat(),
        "basic_intent": basic,
        "preferences": {
            "transport_mode": "auto",
            "transport_priority": "auto",
            "transport_constraints": [],
            "hotel_priority": "auto",
            "hotel_max_price_per_night": None,
            "hotel_min_rating": None,
            "hotel_min_star": None,
            "hotel_room_type": None,
            "hotel_allow_change": "auto",
            "pace": "auto",
        },
        "poi_selections": {},
        "transport_candidates": [],
        "hotel_candidates": [],
        "place_candidates": [],
        "evidence_summary": {},
        "degradations": [],
        "events": [_event("session_created", f"{basic.get('origin') or '?'} → {basic.get('destination') or '?'}")],
        "run_id": None,
        "prefetch": {"recommendation": {
            "status": "PENDING", "version": 1, "place_ids": [], "source": None,
        }},
    }
    queued = config.discovery_enabled and submit is not None and bool(basic.get("destination"))
    if queued:
        session["status"] = SESSION_DISCOVERING
        session["discovery_status"] = DISCOVERY_RUNNING
        session["events"].append(_event("discovery_queued", "已提交攻略推荐；交通与酒店在正式规划时查询"))
    elif submit is not None:
        session["status"] = SESSION_READY
        session["discovery_status"] = DISCOVERY_FAILED
        session["prefetch"]["recommendation"]["status"] = DISCOVERY_FAILED
        session["degradations"].append("攻略推荐未启用或缺少目的地，不能直接开始规划")
    store.save_planning_session(session)
    if queued:
        submit(run_discovery, store, session["session_id"])
    return session


def get_session(store: TravelPlanStore, session_id: str, *, expire: bool = True) -> dict | None:
    """读会话。读取时顺手做一次过期判定 —— 不依赖后台定时任务。"""

    if expire:
        store.expire_planning_sessions(now=_now().isoformat())
    session = store.get_planning_session(session_id)
    if expire and session is not None and session["status"] == SESSION_EXPIRED:
        # 只补当前真正过期的行，不能按 updated_at 猜测刚才过期的是谁。
        session = store.update_planning_session(session_id, lambda current: None)
    return session


def patch_session(
    store: TravelPlanStore,
    session_id: str,
    payload: Mapping[str, Any],
    *,
    llm_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """更新偏好 / POI 选择。

    基础信息（出发地/目的地/日期/天数）**不允许**在这里改：它们决定 Discovery 的口径，
    改了必须重建会话并重跑 Discovery，否则会拿着成都的攻略去排重庆的行程
    （用户旅程落地任务 §16）。前端要改基础信息就重新 POST 一个 session。
    """

    # 先做纯归一化，事务中只合并本请求包含的键，避免并发 PATCH 互相覆盖。
    normalized: dict[str, Any] = {}
    for key in PATCHABLE_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "transport_mode":
            value = normalize_transport_mode(value)
        elif key == "transport_priority":
            value = normalize_transport_priority(value)
        elif key == "transport_constraints":
            value = normalize_transport_constraints(value)
        elif key == "hotel_priority":
            value = normalize_hotel_priority(value)
        elif key == "pace":
            value = normalize_pace(value)
        elif key in ("hotel_max_price_per_night", "hotel_min_rating", "hotel_min_star"):
            value = coerce_float(value)
        elif key == "hotel_allow_change":
            # 前端可能发布尔（true/false），后端契约是 yes/no/auto —— 两种都收，
            # 免得一个类型不一致把整个 PATCH 打成 422。
            if isinstance(value, bool):
                value = "yes" if value else "no"
            else:
                value = coerce_str(value) or None
        else:
            value = coerce_str(value) or None
        normalized[key] = value

    def apply(session: dict[str, Any]) -> None:
        if session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED, SESSION_STARTING):
            return
        preferences = session["preferences"]
        changed: list[str] = []
        for key, value in normalized.items():
            if preferences.get(key) != value:
                preferences[key] = value
                changed.append(key)
        if "budget_total" in payload:
            budget = coerce_float(payload["budget_total"])
            if session["basic_intent"].get("budget_total") != budget:
                session["basic_intent"]["budget_total"] = budget
                if "budget_total" not in changed:
                    changed.append("budget_total")
        if "poi_selections" in payload:
            selections = normalize_place_selections(payload["poi_selections"])
            if selections != session["poi_selections"]:
                session["poi_selections"] = selections
                changed.append("poi_selections")
        if changed:
            session["events"].append(_event("user_preferences_updated", "、".join(changed)))

    # 偏好只影响排序，不重跑 Discovery；STARTING 后冻结已确认的输入。
    return store.update_planning_session(session_id, apply)


def cancel_session(store: TravelPlanStore, session_id: str) -> dict[str, Any] | None:
    def cancel(session: dict[str, Any]) -> None:
        # 已创建的正式 Run 不属于取消草稿接口，不能把它伪装成已取消。
        if session["status"] in (SESSION_STARTING, SESSION_CANCELLED, SESSION_EXPIRED):
            return
        session["status"] = SESSION_CANCELLED
        session["events"].append(_event("session_cancelled", "用户取消"))

    return store.update_planning_session(session_id, cancel)


# ======================================================================
# Discovery
# ======================================================================


def run_discovery(
    store: TravelPlanStore,
    session_id: str,
    *,
    llm_factory: Callable[[], Any] | None = None,
) -> None:
    """后台只做攻略推荐（**零网络**：只读注入的城市数据库缓存）；实时机酒属于用户确认后的正式规划。

    推荐引擎不再持有 ProviderHub，也不会查 Web / 核验高德 —— 库里的数据直接交给模型，
    模型用 function call 交回推荐清单。原始城市候选不能提前冒充推荐。最终写入只更新发现
    字段，保留锁内最新的用户选择。
    """

    session = get_session(store, session_id)
    if session is None or session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED, SESSION_STARTING):
        return
    intent = intent_from_basic(session.get("basic_intent") or {}, session.get("preferences") or {})
    started = _now()
    bundle = discovery.PrefetchBundle(session_id=session_id)

    def publish_progress(entry: Mapping[str, Any]) -> None:
        stage = entry.get("stage")
        if stage not in {"database", "recommendation", "places"}:
            return
        def update(current: dict[str, Any]) -> None:
            if current["status"] in (SESSION_CANCELLED, SESSION_EXPIRED, SESSION_STARTING):
                return
            current["status"] = SESSION_DISCOVERING
            current["discovery_status"] = DISCOVERY_RUNNING
            prefetch = current.setdefault("prefetch", {})
            prefetch["recommendation"] = {
                "status": DISCOVERY_RUNNING, "version": 1, "place_ids": [], "source": None,
            }
            prefetch.setdefault("discovery", {})[stage] = {
                "status": entry.get("status", "RUNNING"), "result_count": entry.get("result_count"),
            }
        store.update_planning_session(session_id, update)

    publish_progress({"stage": "recommendation", "status": "RUNNING"})
    try:
        from app.llm import LLM
        from app.recommendations import recommend_guided

        llm = llm_factory() if llm_factory else LLM.from_env()
        bundle = recommend_guided(llm, intent, store=store, session_id=session_id,
                                  on_progress=publish_progress)
        bundle.outbound = []
        bundle.inbound = []
        bundle.hotels = []
        recommendation = dict(bundle.extras.get("recommendation") or {})
        if not recommendation:
            raise ValueError("攻略推荐缺少结构化提交结果")
        if not bundle.places:
            recommendation["status"] = DISCOVERY_FAILED
        bundle.discovery_status = recommendation.get("status", DISCOVERY_FAILED)
        if bundle.discovery_status not in (DISCOVERY_READY, DISCOVERY_PARTIAL, DISCOVERY_FAILED):
            bundle.discovery_status = DISCOVERY_FAILED
        recommendation["status"] = bundle.discovery_status
        bundle.extras["recommendation"] = recommendation
        try:
            _record_provider_calls(store, bundle.provider_calls, session_id=session_id)
        except Exception:  # 观测不可用不能把已完成推荐变成失败。
            bundle.degradations.append("推荐调用审计写入失败")
    except Exception as exc:  # 失败必须结束加载，且不能退回查机酒或未经筛选的全城候选。
        from app.redact import scrub

        bundle.discovery_status = DISCOVERY_FAILED
        bundle.degradations.append(f"攻略推荐失败：{type(exc).__name__}: {scrub(str(exc))[:300]}")
        bundle.extras["recommendation"] = {
            "status": DISCOVERY_FAILED, "source": "unavailable", "version": 1, "place_ids": [],
        }
        bundle.places = []
        bundle.hotel_areas = []

    def publish_finished(current: dict[str, Any]) -> None:
        if current["status"] in (SESSION_CANCELLED, SESSION_EXPIRED, SESSION_STARTING):
            return
        current["status"] = SESSION_READY
        current["discovery_status"] = bundle.discovery_status
        current["transport_candidates"] = []
        current["hotel_candidates"] = []
        current["place_candidates"] = _recommendation_cards(bundle, current["poi_selections"])
        current["evidence_summary"] = _evidence_summary_of(bundle)
        stages = {**(current.get("prefetch") or {}).get("discovery", {}), **bundle.discovery}
        for state in stages.values():
            if state.get("status") in ("RUNNING", "PENDING"):
                state["status"] = "FAILED" if bundle.discovery_status == DISCOVERY_FAILED else "SKIPPED"
        stages["recommendation"] = {"status": bundle.discovery_status, "result_count": len(bundle.places)}
        bundle.discovery = stages
        current["prefetch"] = bundle.dump()
        current["degradations"] = list(bundle.degradations)
        current["events"].append(_event(
            "recommendation_finished",
            f"status={bundle.discovery_status}，推荐 {len(bundle.places)} 个地点；"
            f"耗时 {round((_now() - started).total_seconds() * 1000)}ms；未查询机票酒店",
        ))

    store.update_planning_session(session_id, publish_finished)


def _prefetch_with_city_cache(
    session_id: str,
    intent: TripIntent,
    hub: Any,
    hotel_pages: int,
    cached: city_cache.CityCacheHit,
    *,
    on_partial: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], discovery.PrefetchBundle]:
    """先发布静态缓存，再并发补实时机酒；缓存命中不新增模型调用。"""

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.models import PreferenceProfile

    profile = PreferenceProfile.default_profile()
    profile.reason = "城市缓存路径采用均衡基线；正式规划按用户最终偏好生成画像"
    social = SimpleNamespace(
        evidences=list(cached.evidences), queries=list(cached.social_queries),
        served_queries=list(cached.social_served_queries), degradations=[],
    )
    places = SimpleNamespace(places=cached.places, degradations=[])
    stages = {
        name: {"status": "RUNNING", "result_count": 0, "duration_ms": None,
               "degraded": False, "error": None}
        for name in ("transport", "hotels")
    }
    for name, count in (("social", len(cached.evidences)), ("places", len(cached.places))):
        stages[name] = {
            "status": "STALE_CACHE" if cached.stale else "CACHE",
            "result_count": count, "duration_ms": 0, "degraded": cached.stale,
            "error": None, "updated_at": cached.updated_at,
        }
    stages["social"]["mention_count"] = len(cached.mentions)
    result: dict[str, Any] = {
        "transport": None, "hotels": None, "social": social, "places": places,
        "errors": {}, "stages": stages, "ledger": {"city_cache_hits": 1},
        **discovery.aggregate_candidate_metadata(intent, cached.evidences, cached.places, profile=profile),
        "city_cache": {
            "source": "city_cache", "updated_at": cached.updated_at, "stale": cached.stale,
            "evidences": len(cached.evidences), "mentions": len(cached.mentions),
            "social_queries": len(cached.social_queries),
        },
        "degradations": ["城市攻略缓存已过期；当前先展示最近可用结果"] if cached.stale else [],
    }
    def publish() -> None:
        if on_partial is not None:
            try:
                on_partial({**result, "errors": dict(result["errors"]),
                            "stages": {key: dict(value) for key, value in stages.items()}})
            except Exception:  # noqa: BLE001
                pass

    publish()  # 必须在发起任何实时查询之前，让静态候选立刻可读。

    def fetch(name: str) -> tuple[Any, dict[str, Any]]:
        started = time.perf_counter()
        began = _now().isoformat()
        error = None
        try:
            value = (
                discovery.fetch_transport_candidates(hub, intent) if name == "transport"
                else discovery.fetch_hotel_candidates(hub, intent, pages=max(1, hotel_pages))
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            value = discovery.TransportCandidates() if name == "transport" else discovery.HotelCandidates()
            value.degradations.append(error)
        info = _cached_stage(value, "all_options" if name == "transport" else "items", error,
                             round((time.perf_counter() - started) * 1000))
        info.update(started_at=began, finished_at=_now().isoformat())
        return value, info

    # 只有协调线程写 result/发布，杜绝分支回调乱序覆盖已完成的候选。
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tp-cache-live") as pool:
        futures = {pool.submit(fetch, name): name for name in ("transport", "hotels")}
        for future in as_completed(futures):
            name = futures[future]
            value, info = future.result()
            result[name] = value
            stages[name] = info
            if info["error"]:
                result["errors"][name] = info["error"]
            publish()
    return result, _bundle_from(session_id, intent, result, hub)


def _cached_stage(value: Any, count_attr: str, error: str | None, elapsed: int) -> dict[str, Any]:
    candidates = getattr(value, count_attr, []) or []
    return {
        "status": "FAILED" if error else ("OK" if candidates else "EMPTY"),
        "result_count": len(candidates), "duration_ms": elapsed, "degraded": bool(getattr(value, "degradations", [])),
        "error": error,
    }


def refresh_city_cache(store: TravelPlanStore, city: str) -> dict[str, Any]:
    """只刷新可复用的城市攻略/POI；没有出发地和日期时机酒函数会自然跳过。

    返回一份摘要，供调用方（预热的 `scripts/preheat_cities.py`）如实报告。预热是把结果
    写进**所有人共用**的库，所以"这次其实是降级的"必须能被看见 —— 典型情形是社媒全部
    限流、只剩网页搜索，这时缓存里一条小红书/抖音证据都没有。打一行"完成"却不知道这件事，
    等于让一份降级数据静静躺满一个 TTL，后续所有会话都吃它。
    """

    normalized = city_cache.normalize_city(city)
    if not normalized:
        return {
            "city": city, "places": 0, "evidences": 0, "social_evidences": 0,
            "queries": 0, "notes": [], "degradations": ["城市名为空"],
        }
    from app.llm import LLM
    from app.providers import ProviderHub, default_mcp_servers

    hub = ProviderHub(run_id=f"city-cache:{normalized}", store=None, mcp_servers=default_mcp_servers())
    try:
        intent = TripIntent(destination=[normalized], days=1, source="city_cache_refresh")
        config = current_config()
        result = discovery.prefetch(
            hub, LLM.from_env(), intent,
            hotel_pages=1, workers=max(1, config.discovery_workers),
            store=store,
        )
        bundle = _bundle_from(f"city-cache:{normalized}", intent, result, hub)
        written = city_cache.write_candidates(
            normalized,
            bundle.places,
            bundle.evidences,
            store=store,
            social_queries=bundle.social_queries,
            social_served_queries=bundle.social_served_queries,
        )
    finally:
        try:
            hub.close()
        except Exception:  # noqa: BLE001
            pass

    social = result.get("social")
    evidences = list(getattr(social, "evidences", []) or [])
    return {
        "city": normalized,
        "places": written,
        "evidences": len(evidences),
        "social_evidences": sum(1 for item in evidences if is_social_evidence(item)),
        "queries": len(list(getattr(social, "queries", []) or [])),
        # `notes` 里记着"小红书「X」：调用失败（已按无结果处理）"这类事实。它们不进
        # `degradations`，但正是解释"为什么一条社媒证据都没有"的关键，所以要带出来。
        "notes": list(getattr(social, "notes", []) or [])[:5],
        "degradations": list(bundle.degradations),
    }


def is_social_evidence(evidence: Any) -> bool:
    """是不是社媒（小红书 / 抖音）证据。

    判据与 `providers._evidences_from_social` 的写法对齐：它固定写 `source_type="social"`。
    再容忍一次 provider 名，避免以后换了来源写法这里静默判错。
    """

    kind = str(getattr(evidence, "source_type", "") or "").lower()
    provider = str(getattr(evidence, "provider", "") or "").lower()
    return kind == "social" or provider in {"tikhub", "mediacrawler"}


def _record_provider_calls(store: TravelPlanStore, calls: Sequence[Mapping[str, Any]], *, session_id: str) -> int:
    """把 Discovery 的调用账本写进 provider_calls（观测用，不放进 sources）。

    不写 Secret、不写完整 raw 响应：只留"谁、什么时候、调了什么、成不成、多快、返回几条"。
    """

    rows = [
        {
            "call_id": f"{session_id}:{index}:{call.get('source_id')}",
            "provider": call.get("provider"),
            "tool": call.get("tool"),
            "status": call.get("status"),
            "source_type": "discovery",
            "source_id": call.get("source_id"),
            "session_id": session_id,
            "fetched_at": call.get("fetched_at"),
            "duration_ms": call.get("duration_ms"),
            "returned": call.get("item_count"),
            "error": (call.get("error") or "")[:400] or None,
            "fallback": False,
            # query 只留能解释"查了什么"的键值，且不含任何凭据。
            "query": {
                key: value
                for key, value in (call.get("arguments") or {}).items()
                if key.lower() not in {"api_key", "token", "authorization", "secret"}
            },
        }
        for index, call in enumerate(calls)
    ]
    try:
        return store.save_provider_calls(rows)
    except Exception:  # noqa: BLE001 —— 观测写不进去不该让 Discovery 失败
        return 0


def _evidence_summary_of(bundle: discovery.PrefetchBundle) -> dict[str, Any]:
    """Prefetch 摘要：两条路径（增量落盘 / 最终落盘）用同一份口径，避免两个数对不上。"""

    return {
        "sources_used": len({call.get("source_id") for call in bundle.provider_calls}),
        "places_verified": sum(1 for place in bundle.places if place.amap_verified),
        "total_candidates": len(bundle.places),
        "transport_candidates": len(bundle.outbound) + len(bundle.inbound),
        "hotel_candidates": len(bundle.hotels),
        "evidence_count": len(bundle.evidences),
    }


def _bundle_from(session_id: str, intent: TripIntent, result: Mapping[str, Any], hub: Any) -> discovery.PrefetchBundle:
    transport = result.get("transport")
    hotels = result.get("hotels")
    social = result.get("social")
    places = result.get("places")
    degradations: list[str] = list(result.get("degradations") or [])
    for part in (transport, hotels, social, places):
        degradations.extend(getattr(part, "degradations", []) or [])
    return discovery.PrefetchBundle(
        discovery=dict(result.get("stages") or {}),
        session_id=session_id,
        basic_intent={
            "origin": intent.origin,
            "destination": (intent.destination or [None])[0],
            "start_date": intent.start_date.isoformat() if intent.start_date else None,
            "end_date": intent.end_date.isoformat() if intent.end_date else None,
            "days": intent.days,
            "travelers": intent.travelers,
            "budget_total": intent.budget_total,
        },
        outbound=list(getattr(transport, "outbound", []) or []),
        inbound=list(getattr(transport, "inbound", []) or []),
        hotels=list(getattr(hotels, "items", []) or []),
        evidences=list(getattr(social, "evidences", []) or []),
        places=list(getattr(places, "places", []) or []),
        # 把 Discovery 计划要搜的检索词与真正搜过的检索词一并过户给正式 run：
        # 少了前者，run 会为了重新算出同样的 queries 再付一次 query_expansion 模型调用；
        # 少了后者，run 无法只补差集（全量重搜/全量复用都不对）。
        social_queries=list(getattr(social, "queries", []) or []),
        social_served_queries=list(getattr(social, "served_queries", []) or []),
        provider_calls=list(hub.audit_entries()) if hub is not None else [],
        degradations=degradations,
        # Discovery 的复用/降级计数（Part C）：正式 run 的 perf 摘要要用它回答
        # "Prefetch 到底省了多少次重复查询"。
        ledger=dict(result.get("ledger") or {}),
        # B 的聚合产物必须随 prefetch_json 一起落盘；不能因 A 的会话搬运而丢失。
        hotel_areas=list(result.get("hotel_areas") or []),
        poi_pools=dict(result.get("poi_pools") or {}),
        profile=dict(result.get("profile") or {}),
        city_cache=dict(result.get("city_cache") or {}),
        discovery_status=DISCOVERY_READY,
    )


# ======================================================================
# 意图构造 / 展示视图
# ======================================================================


def intent_from_basic(basic: Mapping[str, Any], preferences: Mapping[str, Any] | None = None) -> TripIntent:
    """把用户填的基础信息 + 点选偏好变成 TripIntent（**不再经过模型**）。

    这正是"用户点过的按钮优先级高于自然语言解析"的落点：模型不再有猜的机会。
    """

    preferences = dict(preferences or {})
    start = _parse_date(basic.get("start_date"))
    end = _parse_date(basic.get("end_date"))
    days = int(coerce_float(basic.get("days")) or 0) or None
    if days is None and start is not None and end is not None:
        days = max(1, (end - start).days + 1)
    destination = coerce_str(basic.get("destination"))
    return TripIntent(
        origin=coerce_str(basic.get("origin")) or None,
        destination=[destination] if destination else [],
        start_date=start,
        end_date=end,
        days=days or 1,
        travelers=int(coerce_float(basic.get("travelers")) or 0) or 1,
        budget_total=coerce_float(basic.get("budget_total")),
        preferences=coerce_str_list(basic.get("preferences") or preferences.get("interests")),
        pace=coerce_str(preferences.get("pace")) or "auto",
        transport_mode=coerce_str(preferences.get("transport_mode")) or "auto",
        transport_priority=coerce_str(preferences.get("transport_priority")) or "auto",
        transport_constraints=coerce_str_list(preferences.get("transport_constraints")),
        hotel_priority=coerce_str(preferences.get("hotel_priority")) or "auto",
        hotel_max_price_per_night=coerce_float(preferences.get("hotel_max_price_per_night")),
        hotel_min_rating=coerce_float(preferences.get("hotel_min_rating")),
        hotel_min_star=coerce_float(preferences.get("hotel_min_star")),
        hotel_room_type=coerce_str(preferences.get("hotel_room_type")) or None,
        hotel_allow_change=coerce_str(preferences.get("hotel_allow_change")) or None,
        source="guided",
    )


def _parse_date(value: Any):
    from datetime import date

    text = coerce_str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _candidate_cards(places: Sequence[Any], selections: Mapping[str, str]) -> list[dict[str, Any]]:
    """兼容旧候选；POI 来源或合并条数不是攻略证据数。"""

    from collections import Counter
    from app.planner import dedupe_places
    from app.selection import selection_of

    unique, _ = dedupe_places(places)
    names = Counter(place.name for place in unique)
    cards: list[dict[str, Any]] = []
    for place in unique:
        category = place_category(place)
        cards.append(
            {
                "place_id": place.place_id,
                "name": place.name,
                "display_name": f"{place.name}（{place.district or place.business_area or place.place_id}）"
                if names[place.name] > 1 else place.name,
                "category": category,
                "category_label": PLACE_CATEGORY_LABELS.get(category, category),
                "area": place.business_area,
                "district": place.district,
                "reason": place_reason(place),
                "evidence_count": 0,
                "trust_score": None,
                "ad_risk": None,
                "amap_verified": bool(place.amap_verified),
                "merged_from": list(place.merged_from),
                "selected": selection_of(place, selections),
            }
        )
    return cards


def _recommendation_cards(
    bundle: discovery.PrefetchBundle, selections: Mapping[str, str]
) -> list[dict[str, Any]]:
    """最终卡片以核验过的地点为身份，以推荐提交为理由/分类/证据。"""

    proposed = {
        str(card.get("place_id")): card
        for card in bundle.extras.get("recommended_cards") or []
        if isinstance(card, Mapping)
    }
    cards = _candidate_cards(bundle.places, selections)
    for card in cards:
        recommendation = proposed.get(card["place_id"], {})
        for key in ("reason", "category", "evidence_count", "evidence_ids", "scope"):
            if key in recommendation:
                card[key] = recommendation[key]
        card["category_label"] = PLACE_CATEGORY_LABELS.get(card["category"], card["category"])
    return cards


def session_view(session: Mapping[str, Any]) -> dict[str, Any]:
    """GET /planning-sessions/{id} 的响应体。"""

    from app.selection import (
        HOTEL_PRIORITY_LABELS,
        PACE_LABELS as _PACE,
        TRANSPORT_CONSTRAINT_LABELS,
        TRANSPORT_MODE_LABELS,
        TRANSPORT_PRIORITY_LABELS,
    )

    preferences = dict(session.get("preferences") or {})
    cards = list(session.get("place_candidates") or [])
    categories: dict[str, int] = {}
    for card in cards:
        key = str(card.get("category") or "other")
        categories[key] = categories.get(key, 0) + 1
    selections = dict(session.get("poi_selections") or {})
    prefetch = dict(session.get("prefetch") or {})
    freshness = dict(prefetch.get("city_cache") or {})
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "discovery_status": session["discovery_status"],
        "created_at": session.get("created_at"),
        # 缓存命中时展示的是源数据时间，不用会话 PATCH 的时间冒充"数据刚更新"。
        "updated_at": freshness.get("updated_at") or session.get("updated_at"),
        "source": freshness.get("source"),
        # 城市知识库的新鲜度明细（含复用了多少条攻略正文 / 提及 / 检索词）。
        "city_cache": freshness or None,
        "expires_at": session.get("expires_at"),
        "run_id": session.get("run_id"),
        "error": session.get("error"),
        "basic_intent": session.get("basic_intent") or {},
        "preferences": preferences,
        "preference_labels": {
            "transport_mode": TRANSPORT_MODE_LABELS.get(preferences.get("transport_mode"), "帮我选"),
            "transport_priority": TRANSPORT_PRIORITY_LABELS.get(preferences.get("transport_priority"), "帮我选"),
            "transport_constraints": [
                TRANSPORT_CONSTRAINT_LABELS.get(item, item)
                for item in preferences.get("transport_constraints") or []
            ],
            "hotel_priority": HOTEL_PRIORITY_LABELS.get(preferences.get("hotel_priority"), "帮我选"),
            "pace": _PACE.get(preferences.get("pace"), "帮我安排"),
        },
        "poi_selections": selections,
        "transport_candidates": session.get("transport_candidates") or [],
        "hotel_candidates": session.get("hotel_candidates") or [],
        "place_candidates": cards,
        "place_categories": [
            {"category": key, "label": PLACE_CATEGORY_LABELS.get(key, key), "count": count}
            for key, count in sorted(categories.items(), key=lambda item: -item[1])
        ],
        "evidence_summary": session.get("evidence_summary") or {},
        # B 的候选池/用户画像作为会话 JSON 一起过户；空值也保留稳定形状给 C。
        "hotel_areas": prefetch.get("hotel_areas") or [],
        "poi_pools": prefetch.get("poi_pools") or {},
        "profile": prefetch.get("profile") or {},
        "recommendation": prefetch.get("recommendation"),
        "discovery": prefetch.get("discovery") or {},
        "degradations": session.get("degradations") or [],
        "events": session.get("events") or [],
        # 能力声明：途牛目前不返回星级字段，前端据此把「最低星级」置灰并说明原因，
        # 而不是让用户选一个永远不生效的过滤器。价格上限是真实可用的。
        "capabilities": {
            "hotel_star_filter": False,
            "hotel_star_note": "数据源当前不返回星级字段，星级筛选已记录但不参与排序；请用「每晚价格上限」或「评分下限」表达预算与品质要求。",
            "hotel_max_price_filter": True,
            "hotel_rating_filter": True,
            # 「接受换酒店」目前**不影响排程**：行程全程只订一家酒店（价格、预算、
            # 每日通勤都按这一家算）。与其让按钮承诺做不到的事，不如如实置灰 ——
            # 选择仍会被记录，但前端要能说明它暂不生效。
            "hotel_allow_change": False,
            "hotel_allow_change_note": "当前行程全程只订一家酒店，中途换酒店尚未支持；该选择会记入偏好，但不会改变排程。",
        },
    }


def _event(name: str, detail: str = "") -> dict[str, str]:
    return {"at": _now().isoformat(), "event": name, "detail": detail}


# ======================================================================
# 从这里进入正式规划
# ======================================================================


def _confirm_recommended_places(
    bundle: discovery.PrefetchBundle, selections: Mapping[str, str]
) -> tuple[list[Any], dict[str, str], str]:
    """用户正选优先、推荐兜底、负选始终排除。只接受本次推荐的稳定 ID。"""

    from app.planner import dedupe_places
    from app.selection import MUST, WANT, REJECT, selection_of

    recommendation = bundle.extras.get("recommendation")
    if not isinstance(recommendation, Mapping):
        # 历史会话无推荐快照不能把整个城市池冒充推荐；请重建探索，已启动 Run 仍幂等返回。
        raise RecommendationInputError("recommendation_unavailable")
    if recommendation.get("status") in ("PENDING", "RUNNING"):
        raise RecommendationInputError("recommendation_not_ready")
    if recommendation.get("status") not in (DISCOVERY_READY, DISCOVERY_PARTIAL) or not bundle.places:
        raise RecommendationInputError("recommendation_unavailable")
    ids = recommendation.get("place_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(key, str) and key for key in ids):
        raise RecommendationInputError("recommendation_unavailable")
    places, _ = dedupe_places(bundle.places)
    all_ids = {identity for place in places for identity in (place.place_id, *place.merged_from)}
    if not set(ids) <= all_ids:
        raise RecommendationInputError("recommendation_unavailable")
    places = [place for place in places if set(ids) & {place.place_id, *place.merged_from}]
    known_ids = {identity for place in places for identity in (place.place_id, *place.merged_from)}
    if any(key not in known_ids for key in selections):
        raise RecommendationInputError("invalid_place_selection")
    has_positive = any(value in (MUST, WANT) for value in selections.values())
    remapped = {place.place_id: selection_of(place, selections) for place in places}
    chosen = [
        place for place in places
        if remapped[place.place_id] != REJECT
        and (not has_positive or remapped[place.place_id] in (MUST, WANT))
    ]
    if not chosen:
        raise RecommendationInputError("no_selected_places")
    return chosen, {key: value for key, value in remapped.items() if value in (MUST, WANT, REJECT)}, (
        "user_selected" if has_positive else "recommended"
    )


def start_run(
    store: TravelPlanStore,
    session_id: str,
    *,
    submit: Callable[..., Any],
    output_dir: str | None = None,
) -> dict[str, Any]:
    """把会话变成一次正式 run。

    只有走到这里才创建 run_id —— 用户在前面点来点去不会在运行记录里留下垃圾。
    """

    session = get_session(store, session_id)
    if session is None:
        return {"error": "not_found"}
    if session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED):
        return {"error": "session_closed"}
    if session.get("run_id"):
        # 幂等：重复点「开始规划」不该产生第二条 run
        return {"run_id": session["run_id"], "status": "RUNNING"}

    # Discovery 还在跑时给一个很短的 grace period：期间完成的结果继续合并进来。
    # 到点仍没完成的线不再阻塞用户 —— 正式流程会对缺失的部分自己补查。
    _, grace_waited_ms = _await_discovery(store, session_id)
    run_id = _new_run_id()
    prepared: dict[str, Any] = {}

    def confirm(current: dict[str, Any]) -> str:
        # 锁内读取最终用户输入和已发布预取，只做纯构造，不等模型/Provider。
        intent = intent_from_basic(current["basic_intent"], current["preferences"])
        intent.place_selections = normalize_place_selections(current["poi_selections"])
        bundle = discovery.PrefetchBundle.load(current.get("prefetch"))
        chosen, remapped, source = _confirm_recommended_places(bundle, intent.place_selections)
        bundle.places = chosen
        intent.place_selections = remapped
        bundle.extras["planning_place_ids"] = [place.place_id for place in chosen]
        bundle.extras["selection_source"] = source
        bundle.poi_pools = discovery.split_poi_pools(chosen)
        chosen_ids = {identity for place in chosen for identity in (place.place_id, *place.merged_from)}
        bundle.hotel_areas = [
            {**area, "place_ids": [pid for pid in area.get("place_ids", []) if pid in chosen_ids]}
            for area in bundle.hotel_areas
            if isinstance(area.get("place_ids"), list) and chosen_ids.intersection(area["place_ids"])
        ]
        # 快照留在会话中供审计；正式 run 仅拿所选子集，而非 raw_candidates。
        current["prefetch"]["planning_place_ids"] = bundle.extras["planning_place_ids"]
        current["prefetch"]["selection_source"] = source
        bundle.session_id = session_id
        bundle.basic_intent = dict(current["basic_intent"])
        bundle.grace_waited_ms = grace_waited_ms
        query = _compose_query(current)
        prepared.update(intent=intent, bundle=bundle, query=query)
        if grace_waited_ms:
            current["events"].append(_event(
                "discovery_grace_waited", f"等待 {grace_waited_ms}ms 后带着已完成的部分开始"
            ))
        current["events"].extend([
            _event("session_confirmed", f"用户确认并开始规划（{query}）"),
            _event("run_started", run_id),
        ])
        return query

    try:
        session, created = store.start_planning_session_run(session_id, run_id, confirm)
    except RecommendationInputError as exc:
        return {"error": str(exc)}
    if session is None:
        return {"error": "not_found"}
    if session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED):
        return {"error": "session_closed"}
    if not created:
        return {"run_id": session["run_id"], "status": "RUNNING"}
    try:
        submit(_start_job, store, run_id, prepared["query"], prepared["intent"],
               prepared["bundle"], output_dir, session_id)
    except Exception as exc:
        # 接纳失败至少如实结束 run，不留下一个永远等待工作线程的 RUNNING。
        store.finish_run(run_id, "failed", error=f"任务提交失败：{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, "status": "RUNNING"}


def _start_job(
    store: TravelPlanStore,
    run_id: str,
    query: str,
    intent: TripIntent,
    bundle: discovery.PrefetchBundle,
    output_dir: str | None,
    session_id: str,
) -> None:
    """正式 run 的执行体：带上用户确认过的结构化意图与 Discovery 候选。

    这里直接调 `execute_agent_run`（而不是经 HybridRunner 的 payload 形状）：
    结构化 intent 与 prefetch 是 HybridRunner 的 messages 载荷装不下的东西。
    它与 `app/agent.py::run_travel` 汇聚到**同一个**主规划入口（SuperHarness Agent Loop），
    没有第二套 Planner —— 也保证"引导式"和"一句话"两条入口跑的是同一套算法。
    """

    from app.agent_runner import execute_agent_run

    try:
        execute_agent_run(
            query,
            output_dir=output_dir or DEFAULT_OUTPUT_DIR,
            run_id=run_id,
            store=store,
            intent=intent,
            prefetch=bundle,
            source="guided",
            source_session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 后台线程不能把进程打挂
        store.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")


def _await_discovery(
    store: TravelPlanStore, session_id: str, *, grace_seconds: float | None = None
) -> tuple[dict[str, Any], int]:
    """等 Discovery 一个很短的 grace period，返回 (最新会话, 实际等待毫秒数)。

    只等"正在进行"的情况；已经 READY/PARTIAL/FAILED 或还没开始（PENDING）都立即返回。
    等待期间反复重读会话行，所以期间完成的线会被自动带进正式 run。
    """

    config = current_config()
    budget = (
        max(0.0, float(grace_seconds))
        if grace_seconds is not None
        else max(0.0, float(getattr(config, "discovery_grace_seconds", 3.0) or 0.0))
    )
    session = get_session(store, session_id, expire=False)
    if session is None:
        return {}, 0
    started = time.perf_counter()
    while (
        session.get("discovery_status") in (DISCOVERY_RUNNING, DISCOVERY_PENDING)
        and session.get("status") not in (SESSION_STARTING, SESSION_CANCELLED, SESSION_EXPIRED)
        and (time.perf_counter() - started) < budget
    ):
        time.sleep(min(0.2, max(0.02, budget / 10)))
        refreshed = get_session(store, session_id, expire=False)
        if refreshed is None:
            session = {}
            break
        session = refreshed
    waited = int((time.perf_counter() - started) * 1000)
    # 等待只读；确认事件由获得启动权的事务写入，不能再整行回写旧快照。
    return session, waited


def _compose_query(session: Mapping[str, Any]) -> str:
    """给这次 run 造一句可读的原始需求（审计/列表展示用，不参与解析）。"""

    basic = dict(session.get("basic_intent") or {})
    parts = [
        f"{basic.get('origin') or ''}→{basic.get('destination') or ''}",
        f"{basic.get('start_date') or '日期待定'} 起 {basic.get('days') or '?'} 天",
        f"{basic.get('travelers') or 1} 人",
    ]
    if basic.get("budget_total"):
        parts.append(f"预算 {basic['budget_total']:.0f}")
    parts.append("（引导式选择）")
    return "，".join(str(item) for item in parts if item)


def _new_run_id() -> str:
    from app.workflow import new_run_id as _wf_new_run_id

    return _wf_new_run_id()


__all__ = [
    "SESSION_STATUSES",
    "DISCOVERY_READY",
    "DISCOVERY_PARTIAL",
    "DISCOVERY_FAILED",
    "create_session",
    "get_session",
    "patch_session",
    "cancel_session",
    "run_discovery",
    "start_run",
    "session_view",
    "intent_from_basic",
]
