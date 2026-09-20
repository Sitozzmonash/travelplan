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
from typing import Any, Callable, Mapping, Sequence

from app import discovery
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
    }
    store.save_planning_session(session)
    if config.discovery_enabled and submit is not None and basic.get("destination"):
        session["status"] = SESSION_DISCOVERING
        session["discovery_status"] = DISCOVERY_RUNNING
        session["events"].append(_event("discovery_queued", "已提交后台 Discovery"))
        store.save_planning_session(session)
        submit(run_discovery, store, session["session_id"])
    return session


def get_session(store: TravelPlanStore, session_id: str, *, expire: bool = True) -> dict | None:
    """读会话。读取时顺手做一次过期判定 —— 不依赖后台定时任务。"""

    if expire:
        expired = store.expire_planning_sessions(now=_now().isoformat())
        if expired:
            # 补一条 session_expired 事件：状态变了但时间线上没有痕迹，运维会以为是别的原因。
            for session in store.list_planning_sessions(limit=expired)[0]:
                session["events"] = [
                    *(session.get("events") or []),
                    _event("session_expired", f"超过 TTL 未开始规划（{session.get('expires_at')}）"),
                ]
                store.save_planning_session(session)
    return store.get_planning_session(session_id)


def patch_session(
    store: TravelPlanStore,
    session_id: str,
    payload: Mapping[str, Any],
    *,
    hub_factory: Callable[[str], Any] | None = None,
    llm_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """更新偏好 / POI 选择。

    基础信息（出发地/目的地/日期/天数）**不允许**在这里改：它们决定 Discovery 的口径，
    改了必须重建会话并重跑 Discovery，否则会拿着成都的攻略去排重庆的行程
    （用户旅程落地任务 §16）。前端要改基础信息就重新 POST 一个 session。
    """

    session = get_session(store, session_id)
    if session is None:
        return None
    if session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED):
        return session

    preferences = dict(session.get("preferences") or {})
    changed: list[str] = []
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
        if preferences.get(key) != value:
            preferences[key] = value
            changed.append(key)
    if "budget_total" in payload:
        budget = coerce_float(payload["budget_total"])
        basic = dict(session.get("basic_intent") or {})
        if basic.get("budget_total") != budget:
            basic["budget_total"] = budget
            session["basic_intent"] = basic
            changed.append("budget_total")

    if "poi_selections" in payload:
        selections = normalize_place_selections(payload["poi_selections"])
        if selections != (session.get("poi_selections") or {}):
            session["poi_selections"] = selections
            changed.append("poi_selections")

    if changed:
        session["preferences"] = preferences
        session["updated_at"] = _now().isoformat()
        session["events"] = [
            *(session.get("events") or []),
            _event("user_preferences_updated", "、".join(changed)),
        ]
        # 偏好只影响排序，不影响原始数据 —— 所以不重跑 Discovery（§15）。
        store.save_planning_session(session)
    return session


def cancel_session(store: TravelPlanStore, session_id: str) -> dict[str, Any] | None:
    session = get_session(store, session_id)
    if session is None:
        return None
    session["status"] = SESSION_CANCELLED
    session["updated_at"] = _now().isoformat()
    session["events"] = [*(session.get("events") or []), _event("session_cancelled", "用户取消")]
    store.save_planning_session(session)
    return session


# ======================================================================
# Discovery
# ======================================================================


def run_discovery(
    store: TravelPlanStore,
    session_id: str,
    *,
    hub_factory: Callable[[str], Any] | None = None,
    llm_factory: Callable[[], Any] | None = None,
) -> None:
    """后台 Prefetch：四条线并行取数，结果写回会话。

    单条线失败只记降级（`discovery_status=PARTIAL`），绝不让用户卡在加载页 ——
    用户旅程验收标准里明确要求"社交/酒店/交通失败仍可继续"。
    """

    session = get_session(store, session_id, expire=False)
    if session is None or session["status"] in (SESSION_CANCELLED, SESSION_EXPIRED, SESSION_STARTING):
        return
    intent = intent_from_basic(session.get("basic_intent") or {}, session.get("preferences") or {})

    hub = None
    llm = None
    degradations: list[str] = []
    try:
        from app.llm import LLM
        from app.providers import ProviderHub, default_mcp_servers

        # store=None：Discovery 的调用不写 sources。它属于会话（可能从没变成正式 run），
        # 而且 `sources.run_id` 有指向 runs 的外键 —— 写进去要么撞外键、要么污染运行列表。
        # 调用账本留在内存里，随后写进 provider_calls（观测表，无外键）供 Provider Health 用。
        if hub_factory is not None:
            hub = hub_factory(session_id)
        else:
            hub = ProviderHub(run_id=session_id, store=None, mcp_servers=default_mcp_servers())
        llm = llm_factory() if llm_factory is not None else LLM.from_env()
        config = current_config()
        events = list(session.get("events") or [])
        for stage, name in (
            ("transport", "transport_prefetch"),
            ("hotels", "hotel_prefetch"),
            ("social", "social_discovery"),
            ("places", "place_extraction"),
        ):
            events.append(_event(f"{name}_started", stage))
        session["events"] = events
        started = _now()

        # 每完成一条线就落一次盘：用户在 Discovery 还没跑完时点「开始规划」，
        # 读到的必须是"已经查好的那部分"，而不是空 —— 这是之前真丢数据的根因
        # （旧实现只在四条线全部结束时才写一次会话）。
        def persist_partial(snapshot: dict[str, Any]) -> None:
            try:
                partial = _bundle_from(session_id, intent, snapshot, hub)
                payload = partial.dump()
                session["transport_candidates"] = payload["outbound"] + payload["inbound"]
                session["hotel_candidates"] = payload["hotels"]
                session["place_candidates"] = _candidate_cards(
                    partial.places, session.get("poi_selections") or {}
                )
                session["evidence_summary"] = _evidence_summary_of(partial)
                session["discovery"] = dict(snapshot.get("stages") or {})
                session["prefetch"] = payload
                session["updated_at"] = _now().isoformat()
                store.save_planning_session(session)
            except Exception:  # noqa: BLE001 —— 增量落盘失败不该让 Discovery 挂掉
                return

        result = discovery.prefetch(
            hub,
            llm,
            intent,
            hotel_pages=max(1, config.discovery_hotel_pages),
            workers=max(1, config.discovery_workers),
            on_partial=persist_partial,
        )
        bundle = _bundle_from(session_id, intent, result, hub)
        stages = result.get("stages") or {}
        # 调用账本落库：Discovery 的调用发生在会话里，没有 run_id。
        # Provider Health 要能区分"Discovery 阶段大量超时"与"正式 Run 正常"，
        # 所以这些调用必须单独留痕（sources 表有 runs 外键，这里进不去）。
        _record_provider_calls(store, bundle.provider_calls, session_id=session_id)
        session.update(
            {
                "status": SESSION_READY,
                "transport_candidates": bundle.dump()["outbound"] + bundle.dump()["inbound"],
                "hotel_candidates": bundle.dump()["hotels"],
                "place_candidates": _candidate_cards(bundle.places, session.get("poi_selections") or {}),
                "evidence_summary": _evidence_summary_of(bundle),
                "discovery": stages,
                "prefetch": bundle.dump(),
                "degradations": bundle.degradations,
            }
        )
        finished_events = list(session.get("events") or [])
        for stage, name in (
            ("transport", "transport_prefetch"),
            ("hotels", "hotel_prefetch"),
            ("social", "social_discovery"),
            ("places", "place_extraction"),
        ):
            info = stages.get(stage) or {}
            finished_events.append(
                _event(
                    f"{name}_finished",
                    f"status={info.get('status') or 'UNKNOWN'}，"
                    f"结果 {info.get('result_count') or 0} 条，"
                    f"耗时 {info.get('duration_ms') or '?'}ms"
                    + (f"，error={info['error']}" if info.get("error") else ""),
                )
            )
        finished_events.append(
            _event(
                "discovery_finished",
                f"交通 {len(bundle.outbound) + len(bundle.inbound)} 个候选，"
                f"酒店 {len(bundle.hotels)} 个，地点 {len(bundle.places)} 个，"
                f"总耗时 {round((_now() - started).total_seconds() * 1000)}ms",
            )
        )
        session["events"] = finished_events
        if bundle.degradations or result.get("errors"):
            session["discovery_status"] = DISCOVERY_PARTIAL
            if result.get("errors"):
                session["degradations"] = [
                    *session["degradations"],
                    *[f"{k}: {v}" for k, v in result["errors"].items()],
                ]
        else:
            session["discovery_status"] = DISCOVERY_READY
    except Exception as exc:  # noqa: BLE001 —— Discovery 崩了也要让用户能继续
        degradations.append(f"Discovery 失败：{type(exc).__name__}: {exc}")
        session["status"] = SESSION_READY
        session["discovery_status"] = DISCOVERY_FAILED
        session["degradations"] = [*(session.get("degradations") or []), *degradations]
        session["events"] = [*(session.get("events") or []), _event("discovery_failed", degradations[-1])]
    finally:
        if hub is not None:
            try:
                hub.close()
            except Exception:  # noqa: BLE001
                pass
    session["updated_at"] = _now().isoformat()
    store.save_planning_session(session)


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
    degradations: list[str] = []
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
        provider_calls=list(hub.audit_entries()) if hub is not None else [],
        degradations=degradations,
        # Discovery 的复用/降级计数（Part C）：正式 run 的 perf 摘要要用它回答
        # "Prefetch 到底省了多少次重复查询"。
        ledger=dict(result.get("ledger") or {}),
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
    """POI 卡片（用户旅程 §8）：分类、推荐理由、攻略条数、可信度。"""

    cards: list[dict[str, Any]] = []
    for place in places:
        category = place_category(place)
        cards.append(
            {
                "place_id": place.place_id,
                "name": place.name,
                "category": category,
                "category_label": PLACE_CATEGORY_LABELS.get(category, category),
                "area": place.business_area,
                "district": place.district,
                "reason": place_reason(place),
                "evidence_count": len(place.merged_from or []) or (1 if place.source_id else 0),
                "trust_score": None,
                "ad_risk": None,
                "amap_verified": bool(place.amap_verified),
                "selected": selections.get(place.place_id),
            }
        )
    cards.sort(key=lambda card: (card["category"], card["name"]))
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
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "discovery_status": session["discovery_status"],
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
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
    session, grace_waited_ms = _await_discovery(store, session_id)
    intent = intent_from_basic(session.get("basic_intent") or {}, session.get("preferences") or {})
    if session.get("poi_selections"):
        intent.place_selections = normalize_place_selections(session["poi_selections"])
    bundle = discovery.PrefetchBundle.load(session.get("prefetch"))
    bundle.session_id = session_id
    bundle.basic_intent = dict(session.get("basic_intent") or {})
    bundle.grace_waited_ms = grace_waited_ms

    query = _compose_query(session)
    run_id = _new_run_id()
    store.create_run(run_id, original_query=query, source="guided", source_session_id=session_id)
    session["status"] = SESSION_STARTING
    session["run_id"] = run_id
    session["updated_at"] = _now().isoformat()
    session["events"] = [
        *(session.get("events") or []),
        _event("session_confirmed", f"用户确认并开始规划（{query}）"),
        _event("run_started", run_id),
    ]
    store.save_planning_session(session)

    submit(_start_job, store, run_id, query, intent, bundle, output_dir, session_id)
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

    这里直接调 `execute_travel_run`（而不是经 HybridRunner 的 payload 形状）：
    结构化 intent 与 prefetch 是 HybridRunner 的 messages 载荷装不下的东西。
    两条入口最终仍然汇聚到**同一个** `execute_travel_run`，没有第二套 Planner。
    """

    from app.workflow import execute_travel_run

    try:
        execute_travel_run(
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
        and (time.perf_counter() - started) < budget
    ):
        time.sleep(min(0.2, max(0.02, budget / 10)))
        refreshed = get_session(store, session_id, expire=False)
        if refreshed is None:
            break
        session = refreshed
    waited = int((time.perf_counter() - started) * 1000)
    if waited:
        session["events"] = [
            *(session.get("events") or []),
            _event(
                "discovery_grace_waited",
                f"开始规划时 Discovery 仍在进行，等待 {waited}ms 后带着已完成的部分开始",
            ),
        ]
        try:
            store.save_planning_session(session)
        except Exception:  # noqa: BLE001
            pass
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
