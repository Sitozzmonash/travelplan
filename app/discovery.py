"""Discovery：把"取真实候选"从 Workflow 节点里抽出来，供两处复用。

为什么必须有这一层（接管任务 §13）：引导式旅程要在用户还在选偏好时就把交通/酒店/攻略/
地点查出来（Prefetch），而正式 Workflow 的 ②③④⑤ 步也要查同样的东西。
如果各写一份，就一定会出现"Web 查到的是这个、Workflow 用的是那个"，而且改一处漏一处。

所以这里放**唯一一份**取数实现：

    fetch_transport_candidates   交通候选（去程 + 回程）
    fetch_hotel_candidates       酒店候选（可翻多页扩大候选池）
    discover_social_evidence     攻略证据（小红书 → 抖音 → 网页）
    extract_place_candidates     地点候选（攻略抽取 + 关键词 POI + 去重）

调用方分工：
  * Workflow 节点：拿结果 → 打分/比选/写 state/写 stage（决策与文案留在流程层）；
  * Planning Session Prefetch：拿结果 → 存进会话草稿，供用户点选，并在正式 run 里复用。

`pages` 这类"扩大候选池"的开关只在 Discovery 侧打开：正式 run 复用 Discovery 的结果，
所以它不需要自己再打一遍 Provider（也是用户抱怨"酒店候选太窄"的真正修法）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app import planner
from app.config import current_config
from app.llm import LLM, degraded_note
from app.models import (
    Decision,
    DecisionStatus,
    Evidence,
    HotelOption,
    Place,
    TripIntent,
    coerce_str,
)
from app.observability import (
    MARK_PREFETCH_REUSED,
    MARK_PROVIDER_CALLED,
    CallLedger,
    parallel_map,
    query_key,
)
from app.prompts import EXTRACT_PLACES_PROMPT, RESEARCH_QUERY_EXPANSION_PROMPT
from app.providers import ProviderHub

# ======================================================================
# 上限与关键词表（原来散在 workflow.py 里，随取数逻辑一起搬过来；workflow 再 re-export）
# ======================================================================

SOCIAL_QUERY_LIMIT = 3
WEB_QUERY_LIMIT = 2
MAX_EVIDENCE = 40
EXTRACT_EVIDENCE_LIMIT = 4
EVIDENCE_TEXT_CHARS = 1200
POI_QUERY_LIMIT = 8
POI_PAGE_SIZE = 10
#: Discovery 阶段最多验证多少个 POI 是真实存在的（不两两算路线，那太贵）
DISCOVERY_POI_VERIFY_LIMIT = 20

PREFERENCE_POI_QUERY = {"拍照": "观景台", "放松": "茶馆", "亲子": "乐园", "夜生活": "酒吧街"}

#: 检索词里出现这些词说明它不是在找"地点"，别拿它去搜 POI。
NON_PLACE_QUERY_WORDS = (
    "住宿", "酒店", "民宿", "交通", "机票", "高铁", "火车", "预算", "花费", "人均",
    "避坑", "注意事项", "攻略", "路线", "穿搭", "天气",
)

#: 类别 → 搜索关键词（Discovery 用关键词兜底保证"必去景点不会因为攻略没写而缺席"）
DEFAULT_POI_KEYWORDS = ("景点", "博物馆", "公园", "步行街", "古镇")


# ======================================================================
# 返回结构
# ======================================================================


class SimpleResult:
    """与 ``ProviderResult`` 同形的空结果（只用 ``status`` / ``items`` / ``provider``）。

    并发取数时某条线炸了，下游仍然要能拿到"这一次没结果"这个**数据**，
    而不是一个异常或一个编出来的候选。
    """

    __slots__ = ("status", "items", "provider", "error")

    def __init__(self, *, provider: str, status: str = "UNAVAILABLE", error: str | None = None) -> None:
        self.status = status
        self.items: list[Any] = []
        self.provider = provider
        self.error = error


@dataclass(slots=True)
class TransportCandidates:
    outbound: list[Any] = field(default_factory=list)
    inbound: list[Any] = field(default_factory=list)
    provider_status: dict[str, str] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)
    queried: bool = False
    skip_reason: str = ""

    @property
    def all_options(self) -> list[Any]:
        return [*self.outbound, *self.inbound]


@dataclass(slots=True)
class HotelCandidates:
    items: list[HotelOption] = field(default_factory=list)
    provider_status: str = "SKIPPED"
    pages_fetched: int = 0
    degradations: list[str] = field(default_factory=list)
    skip_reason: str = ""


@dataclass(slots=True)
class SocialEvidence:
    evidences: list[Evidence] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    query_source: str = ""
    platforms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlaceCandidates:
    places: list[Place] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    amap_status: str = "OK"
    extracted_from_evidence: int = 0
    keyword_hits: int = 0


# ======================================================================
# 交通
# ======================================================================


def fetch_transport_candidates(hub: ProviderHub, intent: TripIntent, *, reason_out: dict[str, Any] | None = None) -> TransportCandidates:
    """查去程/回程的大交通候选（不做比选，比选留给 `app/selection.py` 的策略层）。

    四条查询（去程火车 / 去程航班 / 回程火车 / 回程航班）互不依赖，一起发出去（Part A）：
    串行做这四件事实测吃掉 210s，而它们唯一的"依赖"是以前是顺序写的。
    结果按**固定顺序**合并（去程火车、去程航班、回程火车、回程航班），
    所以并发不会让候选顺序抖动。
    """

    destination = _destination(intent)
    origin = coerce_str(intent.origin) or None
    result = TransportCandidates()
    if not (origin and destination and intent.start_date):
        result.skip_reason = "缺少出发地 / 目的地 / 出发日期，本次没有查询大交通（没有用估算值代替）"
        result.provider_status = {"train": "SKIPPED", "flight": "SKIPPED"}
        return result

    start = intent.start_date
    back = _last_date(intent)
    travelers = max(1, intent.travelers)
    result.queried = True
    cfg = current_config()
    has_back = back is not None and back >= start

    # 四个任务按固定顺序定义，`parallel_map` 保证结果同序 —— 去程火车、去程航班、
    # 回程火车、回程航班。谁先返回不参与任何决策。
    tasks: list[Any] = [
        lambda: hub.search_trains(origin, destination, start),
        lambda: hub.search_flights(origin, destination, start, travelers=travelers),
    ]
    if has_back:
        tasks.append(lambda: hub.search_trains(destination, origin, back))
        tasks.append(lambda: hub.search_flights(destination, origin, back, travelers=travelers))
    outcomes = parallel_map(tasks, max_workers=cfg.provider_max_concurrency, thread_prefix="tp-disc-transport")

    def _value(index: int, label: str, provider: str) -> Any:
        """取第 index 个结果；失败时返回空候选 + 一条降级说明（绝不用估算值代替）。"""

        outcome = outcomes[index]
        if outcome.ok and outcome.value is not None:
            return outcome.value
        result.degradations.append(f"{label} 查询失败（{outcome.error or '未知错误'}），本次按无候选处理")
        return SimpleResult(provider=provider)

    out_trains = _value(0, "去程火车", "12306")
    out_flights = _value(1, "去程航班", "tuniu")
    result.provider_status["去程火车"] = out_trains.status
    result.provider_status["去程航班"] = out_flights.status
    result.outbound = [*out_trains.items, *out_flights.items]

    if has_back:
        in_trains = _value(2, "回程火车", "12306")
        in_flights = _value(3, "回程航班", "tuniu")
        result.provider_status["回程火车"] = in_trains.status
        result.provider_status["回程航班"] = in_flights.status
        result.inbound = [*in_trains.items, *in_flights.items]
    else:
        result.provider_status["回程火车"] = "SKIPPED"
        result.provider_status["回程航班"] = "SKIPPED"
    if reason_out is not None:
        reason_out["back"] = back
    return result


# ======================================================================
# 酒店
# ======================================================================


def fetch_hotel_candidates(
    hub: ProviderHub,
    intent: TripIntent,
    *,
    pages: int = 1,
) -> HotelCandidates:
    """查住宿候选。

    `pages > 1` 时翻多页扩大候选池。为什么要这个：途牛首页返回的候选可能**整页都是
    高档酒店**，用户会以为"系统只会挑贵的"；翻页才有可能拿到经济型选择。
    Discovery 默认翻页，正式 run 复用 Discovery 的结果，所以不会重复打 Provider。
    """

    destination = _destination(intent)
    result = HotelCandidates()
    if not (destination and intent.start_date):
        result.skip_reason = "缺少目的地或入住日期，本次没有查询酒店（没有用估算价格代替）"
        return result

    check_in = intent.start_date
    check_out = _last_date(intent) or check_in
    if check_out <= check_in:
        result.skip_reason = f"行程只有 {intent.days} 天（{check_in.isoformat()} 当天往返），不需要住宿"
        return result

    seen: dict[str, HotelOption] = {}
    status = "SKIPPED"
    for page in range(1, max(1, pages) + 1):
        page_result = hub.search_hotels(
            destination, check_in, check_out, travelers=max(1, intent.travelers), page_num=page
        )
        status = page_result.status
        result.pages_fetched += 1
        fresh = 0
        for item in page_result.items:
            key = str(item.hotel_id or item.name)
            if key in seen:
                continue
            seen[key] = item
            fresh += 1
        # 一页没有新东西就停：真实 Provider 的翻页到底了，假件也会在这里立刻退出，
        # 避免"翻页把同一个结果查 N 遍"。
        if fresh == 0:
            break
    result.items = list(seen.values())
    result.provider_status = status
    return result


# ======================================================================
# 攻略证据
# ======================================================================


def discover_social_evidence(
    hub: ProviderHub,
    llm: LLM,
    intent: TripIntent,
    *,
    ledger: CallLedger | None = None,
) -> SocialEvidence:
    """搜攻略：小红书 → 抖音（小红书空时）→ 网页，全部失败也照常返回空集。

    同一条线内部的多次检索（3 个小红书关键词、2 个网页关键词）互不依赖，受控并发；
    但**合并顺序按检索词顺序**，`MAX_EVIDENCE` 的截断点因此与串行版本完全一致 ——
    并发只影响墙钟，不影响结果。抖音回退依赖小红书的结果，仍然是串行的。
    """

    result = SocialEvidence()
    destination = _destination(intent) or ""
    prefs = "、".join(intent.preferences[:4])
    cfg = current_config()
    books = ledger or CallLedger(scope="discovery")

    expansion = llm.invoke_json(
        RESEARCH_QUERY_EXPANSION_PROMPT,
        f"目的地：{destination}\n天数：{intent.days}\n偏好：{prefs or '无特别偏好'}\n"
        f"原始需求：{destination} 旅游攻略",
        tag="query_expansion",
    )
    if expansion.ok and isinstance(expansion.value, list) and expansion.value:
        result.queries = [coerce_str(item) for item in expansion.value if coerce_str(item)]
        result.query_source = f"模型扩写（{len(result.queries)} 条）"
    else:
        result.queries = _fallback_queries(intent)
        result.query_source = "规则兜底检索词"
        result.degradations.append(degraded_note(expansion, "检索词退化为组合"))

    evidences: list[Evidence] = []
    social_keywords = list(result.queries[:SOCIAL_QUERY_LIMIT])
    outcomes = parallel_map(
        [
            (
                lambda kw=keyword: books.fetch(
                    query_key("tikhub", "search_xiaohongshu", query=kw),
                    lambda kw=kw: hub.search_xiaohongshu(kw),
                    detail=f"小红书「{kw}」",
                )[0]
            )
            for keyword in social_keywords
        ],
        max_workers=cfg.provider_max_concurrency,
        thread_prefix="tp-disc-social",
    )
    for keyword, outcome in zip(social_keywords, outcomes):
        if not outcome.ok or outcome.value is None:
            result.notes.append(f"小红书「{keyword}」：调用失败（已按无结果处理）")
            continue
        social = outcome.value
        result.platforms["小红书"] = result.platforms.get("小红书", 0) + len(social.items)
        evidences.extend(social.items)
        if not social.items:
            result.notes.append(f"小红书「{keyword}」：{social.status}")
        if len(evidences) >= MAX_EVIDENCE:
            break

    if not any(ev.provider == "xhs" for ev in evidences) and destination:
        douyin = books.fetch(
            query_key("tikhub", "search_douyin", query=f"{destination} 旅游"),
            lambda: hub.search_douyin(f"{destination} 旅游"),
            detail="抖音回退",
        )[0]
        result.platforms["抖音"] = result.platforms.get("抖音", 0) + len(douyin.items)
        evidences.extend(douyin.items)
        result.notes.append(f"小红书没有返回可用攻略，回退抖音查询：{douyin.status}")

    web_keywords = list(result.queries[:WEB_QUERY_LIMIT])
    web_outcomes = parallel_map(
        [
            (
                lambda kw=keyword: books.fetch(
                    query_key("tavily", "web_search", query=kw),
                    lambda kw=kw: hub.web_search(kw, max_results=5),
                    detail=f"网页搜索「{kw}」",
                )[0]
            )
            for keyword in web_keywords
        ],
        max_workers=cfg.provider_max_concurrency,
        thread_prefix="tp-disc-web",
    )
    for keyword, outcome in zip(web_keywords, web_outcomes):
        if not outcome.ok or outcome.value is None:
            result.notes.append(f"网页搜索「{keyword}」：调用失败（已按无结果处理）")
            continue
        web = outcome.value
        if web.items:
            result.platforms["网页"] = result.platforms.get("网页", 0) + len(web.items)
        else:
            result.notes.append(f"网页搜索「{keyword}」：{web.status}")
        call = web.calls[0] if web.calls else None
        for index, item in enumerate(web.items):
            evidences.append(
                Evidence.from_content(
                    {
                        "title": item.get("title"),
                        "text": item.get("snippet") or item.get("title"),
                        "url": item.get("url"),
                    },
                    evidence_id=f"{call.source_id}-w{index}" if call else f"web-{index}",
                    source_type="web",
                    provider="tavily",
                    source_id=call.source_id if call else None,
                    source_url=coerce_str(item.get("url")) or None,
                    fetched_at=call.fetched_at if call else None,
                )
            )

    result.evidences = evidences[:MAX_EVIDENCE]
    if not result.evidences:
        result.degradations.append(
            "社交与网页攻略都没有返回可用内容，Trust Score 的「多来源证据」分项会偏低；"
            "这不是数据缺失的掩盖，而是如实反映"
        )
    return result


def _fallback_queries(intent: TripIntent) -> list[str]:
    """模型不给检索词时的规则兜底：目的地 + 偏好词组合。"""

    destination = _destination(intent) or ""
    queries = [f"{destination} 攻略", f"{destination} 必去"]
    if intent.preferences:
        queries.append(f"{destination} {'、'.join(intent.preferences[:2])}")
    for preference in intent.preferences[:2]:
        word = PREFERENCE_POI_QUERY.get(preference, preference)
        queries.append(f"{destination} {word}")
    return [query.strip() for query in queries if query.strip()][:SOCIAL_QUERY_LIMIT + WEB_QUERY_LIMIT]


# ======================================================================
# 地点
# ======================================================================


def extract_place_candidates(
    hub: ProviderHub,
    llm: LLM,
    intent: TripIntent,
    evidences: Sequence[Evidence],
    *,
    queries: Sequence[str] | None = None,
    verify_limit: int | None = None,
    poi_limit: int | None = None,
    ledger: CallLedger | None = None,
) -> PlaceCandidates:
    """从攻略提到的地方 + 关键词，去高德查成真实 POI 候选。

    关键设计：**攻略里的地名只当搜索关键词用**。候选地点必须能被高德查到，
    所以坐标、行政区、营业时间全部有出处；查不到就不进候选，绝不用模型编一个地点。

    Discovery 阶段只做到这里：不两两算路线、不逐个查门票 —— 那要等用户选完之后做。
    ``ledger`` 让同一次 Discovery 内的重复关键词只查一次高德（Part C）。
    """

    result = PlaceCandidates()
    destination = _destination(intent) or ""

    extracted: list[dict[str, Any]] = []
    provider_mentions: list[str] = []
    for evidence in evidences:
        provider_mentions.extend(evidence.place_mentions)

    llm_targets = [
        evidence for evidence in evidences if not evidence.place_mentions and len(evidence.text) >= 40
    ][:EXTRACT_EVIDENCE_LIMIT]
    for evidence in llm_targets:
        response = llm.invoke_json(
            EXTRACT_PLACES_PROMPT,
            f"标题：{evidence.title}\n来源：{evidence.provider}/{evidence.source_type}\n"
            f"证据 id：{evidence.id}\n正文：\n{evidence.text[:EVIDENCE_TEXT_CHARS]}",
            tag=f"extract_places:{evidence.id}",
        )
        if response.ok and isinstance(response.value, dict):
            for item in response.value.get("places", []) or []:
                if isinstance(item, dict) and coerce_str(item.get("name")):
                    extracted.append({**item, "evidence_id": evidence.id})
        else:
            result.degradations.append(degraded_note(response, f"证据 {evidence.id} 的地点抽取被跳过"))
    result.extracted_from_evidence = len(extracted)

    if not evidences:
        result.notes.append("没有可用的攻略证据，地点候选只能来自关键词搜索")

    keywords = _keyword_candidates(intent, list(queries or ()))
    cfg = current_config()
    books = ledger or CallLedger(scope="discovery")
    limit = cfg.poi_query_limit if poi_limit is None else max(1, poi_limit)

    # 关键词表：攻略自带地名 / 模型抽出的地名 / 兜底关键词。先合并去重再截断 ——
    # 同一家店出现在两条攻略里时不该查两遍高德（Part C 的"同一个键只查一次"），
    # 也让 DISCOVERY_MAX_PLACES（原始候选上限）真的是"去重后的原始候选数"。
    raw_names: list[str] = []
    for name in [
        *provider_mentions[:limit],
        *[coerce_str(item.get("name")) for item in extracted[:limit]],
    ]:
        cleaned = coerce_str(name).strip()
        if cleaned and cleaned not in raw_names:
            raw_names.append(cleaned)
    search_terms = [*raw_names[: cfg.discovery_max_places], *keywords]

    # 关键词 POI 搜索互不依赖 → 受控并发；合并仍按关键词顺序（first-wins 的字段因此稳定）。
    outcomes = parallel_map(
        [
            (
                lambda term=term: books.fetch(
                    query_key("amap", "search_poi", destination=destination, query=term),
                    lambda term=term: hub.search_poi(
                        term, destination, page_size=cfg.poi_page_size, type_hint="attraction"
                    ),
                    detail=f"高德 POI「{term}」",
                )[0]
            )
            for term in search_terms
        ],
        max_workers=cfg.provider_max_concurrency,
        thread_prefix="tp-disc-poi",
    )

    poi_status = "OK"
    places: list[Place] = []
    seen_ids: set[str] = set()
    searched = 0
    for term, outcome in zip(search_terms, outcomes):
        searched += 1
        if not outcome.ok or outcome.value is None:
            result.degradations.append(f"高德 POI 查询「{term}」失败：{outcome.error}")
            continue
        poi = outcome.value
        poi_status = poi_status if poi_status != "OK" else poi.status
        for place in poi.items:
            if place.place_id in seen_ids:
                continue
            seen_ids.add(place.place_id)
            places.append(place)

    result.keyword_hits = searched
    if not places and destination:
        result.degradations.append(
            f"高德 POI 查询没有返回任何地点（{poi_status}），本次没有可选地点；"
            "没有用模型生成的地点填补"
        )
    result.amap_status = poi_status

    deduped, dedupe_decisions = planner.dedupe_places(places)
    result.decisions.extend(dedupe_decisions)

    # 用户可见候选上限（用户旅程 §8：12~20 个，不要一次丢 50 个）。为什么在这里截：
    # 用户是在这一步做 MUST/WANT/REJECT 的，清单太长等于让他做无意义的筛选；
    # 截断顺序按现有顺序（攻略提到过的在前），不按分数——此时还没有分数。
    visible_limit = max(1, cfg.user_visible_poi_limit)
    if len(deduped) > visible_limit:
        result.notes.append(
            f"候选 {len(deduped)} 个超过用户可见上限 {visible_limit}，已按攻略提及顺序截断"
        )
        deduped = deduped[:visible_limit]

    # Discovery 只对前 N 个做 POI 详情核实（营业时间/地址），控制成本；
    # 正式 run 的 ⑥ 步会对"真正可能排进行程"的点做深度验证（Part D）。
    verify = max(0, int(verify_limit if verify_limit is not None else cfg.discovery_poi_verify_limit))
    detail_targets = deduped[:verify]
    detail_outcomes = parallel_map(
        [
            (
                lambda place=place: books.fetch(
                    query_key("amap", "get_poi_detail", query=place.place_id),
                    lambda place=place: hub.poi_detail(place.place_id),
                    detail=f"POI 详情 {place.name}",
                )[0]
            )
            for place in detail_targets
        ],
        max_workers=cfg.poi_verify_max_concurrency,
        thread_prefix="tp-disc-detail",
    )
    for place, outcome in zip(detail_targets, detail_outcomes):
        if not outcome.ok or outcome.value is None or not outcome.value.ok:
            continue
        detail = outcome.value
        _apply_poi_detail(place, detail.items[0] if detail.items else {})
    result.places = deduped
    return result


def _keyword_candidates(intent: TripIntent, queries: list[str]) -> list[str]:
    """关键词兜底：保证"必去景点"不会因为攻略没写就完全缺席（两者都只是候选）。

    上限来自 config（``POI_QUERY_LIMIT``）：每个关键词都是一次真实的高德调用。
    """

    destination = _destination(intent) or ""
    cap = max(1, current_config().poi_query_limit)
    names = list(DEFAULT_POI_KEYWORDS)
    for preference in intent.preferences[:4]:
        word = PREFERENCE_POI_QUERY.get(preference, preference)
        if word and word not in names:
            names.append(word)
    for query in queries[:4]:
        cleaned = re.sub(rf"^{re.escape(destination)}\s*", "", query).strip()
        cleaned = re.sub(r"(必去|推荐|避坑|不值得|攻略|美食|住宿)$", "", cleaned).strip()
        if any(word in cleaned for word in NON_PLACE_QUERY_WORDS):
            continue
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names[:cap]


def _apply_poi_detail(place: Place, detail: dict[str, Any]) -> None:
    if not detail:
        return
    place.amap_verified = True
    if detail.get("opening_hours") and not place.opening_hours:
        place.opening_hours = coerce_str(detail.get("opening_hours")) or place.opening_hours
    if detail.get("address") and not place.address:
        place.address = coerce_str(detail.get("address")) or place.address
    if detail.get("business_area") and not place.business_area:
        place.business_area = coerce_str(detail.get("business_area")) or place.business_area
    if detail.get("district") and not place.district:
        place.district = coerce_str(detail.get("district")) or place.district


# ======================================================================
# 组合入口
# ======================================================================


def _run_stage(run: Any, name: str, func: Any, args: tuple, report: Any) -> None:
    """跑一条 Discovery 线，无论成功失败都在结束时上报一次。

    为什么要单独包一层：并行线是"谁先完成谁先可用"，只有每条线结束时立刻上报，
    用户在等待期间点「开始规划」才能拿到已经完成的那部分（否则要等全部跑完）。
    """

    try:
        run(name, func, args)
    finally:
        report(name)


def _destination(intent: TripIntent) -> str | None:
    for item in intent.destination or []:
        text = coerce_str(item).strip()
        if text:
            return text
    return None


def _last_date(intent: TripIntent):
    if intent.end_date is not None:
        return intent.end_date
    if intent.start_date is None:
        return None
    from datetime import timedelta

    return intent.start_date + timedelta(days=max(1, intent.days) - 1)


def prefetch(
    hub: ProviderHub,
    llm: LLM,
    intent: TripIntent,
    *,
    hotel_pages: int = 1,
    workers: int = 4,
    on_partial: Any | None = None,
    ledger: CallLedger | None = None,
) -> dict[str, Any]:
    """四条线并行取数，返回给 Planning Session 存草稿的原始结果。

    为什么并行：用户在选交通/酒店/节奏的这几十秒里，后台应该已经在查了。
    串行会让"Prefetch 提前开始"这件事失去意义。

    单条线失败不影响其它线（`TransportCandidates` 等各自带 degradations），
    这也满足验收标准里"social / hotel / transport 失败仍可继续"。

    ``ledger`` 是这一次 Discovery 的查询键账本（Part C）：四条线共用一个，
    所以"交通查过的键""social 查过的关键词"彼此之间也不会重复请求。
    """

    from concurrent.futures import ThreadPoolExecutor

    books = ledger or CallLedger(scope="discovery")
    destinations = {
        "transport": (fetch_transport_candidates, (hub, intent)),
        "hotels": (fetch_hotel_candidates, (hub, intent)),
        "social": (discover_social_evidence, (hub, llm, intent)),
    }
    outcomes: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def run(name: str, func: Any, args: tuple) -> None:
        try:
            if name == "hotels":
                outcomes[name] = func(*args, pages=hotel_pages)
            elif name == "social":
                outcomes[name] = func(*args, ledger=books)
            else:
                outcomes[name] = func(*args)
        except Exception as exc:  # noqa: BLE001 —— 一条线崩了不能带走整个 Discovery
            errors[name] = f"{type(exc).__name__}: {exc}"

    import threading
    import time as _time

    # 每条线单独计时：管理端要回答"Discovery 卡在哪一步"，只有总耗时是答不出来的。
    # 未完成的线在这里用 status=RUNNING 表示 —— 用户中途点「开始规划」时，
    # 正式 run 要能区分"这条线查过了但没数据"和"这条线还没查完"。
    timings: dict[str, dict[str, Any]] = {
        name: {
            "started_at": _iso(),
            "finished_at": None,
            "duration_ms": None,
            "status": "RUNNING",
            "result_count": 0,
            "degraded": False,
            "error": None,
        }
        for name in (*destinations, "places")
    }
    started_at = {name: _time.perf_counter() for name in destinations}
    lock = threading.Lock()

    def finish(name: str, *, status: str, count: int, degraded: bool, error: str | None = None) -> None:
        """收尾一条线并通知调用方（增量落盘用）。"""

        with lock:
            timings[name].update(
                {
                    "finished_at": _iso(),
                    "duration_ms": round((_time.perf_counter() - started_at.get(name, _time.perf_counter())) * 1000)
                    if name in started_at
                    else None,
                    "status": status,
                    "result_count": count,
                    "degraded": degraded,
                    "error": error,
                }
            )
        if on_partial is not None:
            try:
                on_partial(snapshot())
            except Exception:  # noqa: BLE001 —— 落盘失败不该毁掉 Discovery
                pass

    def snapshot() -> dict[str, Any]:
        with lock:
            return {
                "transport": outcomes.get("transport"),
                "hotels": outcomes.get("hotels"),
                "social": outcomes.get("social"),
                "places": outcomes.get("places") or PlaceCandidates(),
                "errors": dict(errors),
                "stages": {name: dict(info) for name, info in timings.items()},
            }

    def report(name: str) -> None:
        """一条线跑完 → 定状态并通知调用方（顺序无关，谁先完成谁先上报）。"""

        if name == "transport":
            transport = outcomes.get("transport")
            finish(
                name,
                status="FAILED" if name in errors else ("EMPTY" if not (transport and transport.all_options) else "OK"),
                count=len(transport.all_options) if transport else 0,
                degraded=bool(transport and transport.degradations),
                error=errors.get(name),
            )
        elif name == "hotels":
            hotels = outcomes.get("hotels")
            finish(
                name,
                status="FAILED" if name in errors else ("EMPTY" if not (hotels and hotels.items) else "OK"),
                count=len(hotels.items) if hotels else 0,
                degraded=bool(hotels and hotels.degradations),
                error=errors.get(name),
            )
        elif name == "social":
            social = outcomes.get("social") or SocialEvidence()
            finish(
                name,
                status="FAILED" if name in errors else ("EMPTY" if not social.evidences else "OK"),
                count=len(social.evidences),
                degraded=bool(social.degradations),
                error=errors.get(name),
            )
        elif name == "places":
            places_stage = outcomes.get("places") or PlaceCandidates()
            finish(
                name,
                status="FAILED" if name in errors else ("EMPTY" if not places_stage.places else "OK"),
                count=len(places_stage.places),
                degraded=bool(places_stage.degradations),
                error=errors.get(name),
            )

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="tp-discovery") as pool:
        futures = {
            name: pool.submit(_run_stage, run, name, func, args, report)
            for name, (func, args) in destinations.items()
        }
        for future in futures.values():
            future.result()

    social: SocialEvidence = outcomes.get("social") or SocialEvidence()
    places = PlaceCandidates()
    places_started = _time.perf_counter()
    if social.evidences:
        try:
            places = extract_place_candidates(
                hub, llm, intent, social.evidences, queries=social.queries, ledger=books
            )
        except Exception as exc:  # noqa: BLE001
            errors["places"] = f"{type(exc).__name__}: {exc}"
    outcomes["places"] = places
    started_at["places"] = places_started
    report("places")

    transport: TransportCandidates | None = outcomes.get("transport")
    hotels: HotelCandidates | None = outcomes.get("hotels")
    stages = {name: dict(info) for name, info in timings.items()}
    return {
        "transport": transport,
        "hotels": hotels,
        "social": social,
        "places": places,
        "errors": errors,
        "stages": stages,
        # 本次 Discovery 的缓存/复用/降级计数（Part C）：写进会话，正式 run 与
        # 管理端因此能回答"Prefetch 阶段省了多少重复查询"。
        "ledger": books.summary(),
    }


def _iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# Prefetch 结果的搬运（会话 → 正式 run）
# ======================================================================


@dataclass(slots=True)
class PrefetchBundle:
    """Discovery 查到的原始候选，交给正式 run 复用。

    为什么要有类型化的搬运：会话把它存成 JSON，正式 run 再还原成同一批模型对象。
    如果 run 复用的是一个"半成品字典"，Provider 归一化出来的字段就会悄悄丢一半，
    最后表现为"引导式跑的行程比一句话跑的差"，而且很难查。
    """

    session_id: str | None = None
    #: 用户填/选过的结构化基础信息；有它就不再让模型重新猜一遍
    basic_intent: dict[str, Any] = field(default_factory=dict)
    outbound: list[Any] = field(default_factory=list)
    inbound: list[Any] = field(default_factory=list)
    hotels: list[HotelOption] = field(default_factory=list)
    evidences: list[Evidence] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    #: Discovery 期间的 Provider 调用账本（要"过户"到正式 run 的 sources 表，保住来源链）
    provider_calls: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    discovery_status: str = "READY"
    #: 分阶段 Discovery 状态（管理端与 Bad Case 都要用它回答"卡在哪一步"）
    discovery: dict[str, Any] = field(default_factory=dict)
    #: Discovery 阶段的缓存/复用/降级计数（Part C）：正式 run 与 perf 摘要在同一个
    #: 界面上回答"Prefetch 省了多少次重复查询"。
    ledger: dict[str, int] = field(default_factory=dict)
    #: 开始规划时为等 Discovery 而实际等待的毫秒数（0 = 没有在跑，直接开始）
    grace_waited_ms: int = 0

    @property
    def reused(self) -> bool:
        return bool(self.outbound or self.inbound or self.hotels or self.evidences or self.places)

    def dump(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "basic_intent": self.basic_intent,
            "outbound": [_dump_model(item) for item in self.outbound],
            "inbound": [_dump_model(item) for item in self.inbound],
            "hotels": [_dump_model(item) for item in self.hotels],
            "evidences": [_dump_model(item) for item in self.evidences],
            "places": [_dump_model(item) for item in self.places],
            "provider_calls": list(self.provider_calls),
            "degradations": list(self.degradations),
            "discovery_status": self.discovery_status,
            "discovery": dict(self.discovery),
            "ledger": dict(self.ledger),
            "grace_waited_ms": self.grace_waited_ms,
        }

    @classmethod
    def load(cls, payload: Mapping[str, Any] | None) -> "PrefetchBundle":
        """从会话 JSON 还原；任何一块坏了都只丢那一块，不让整次规划失败。"""

        data = dict(payload or {})
        return cls(
            session_id=coerce_str(data.get("session_id")) or None,
            basic_intent=dict(data.get("basic_intent") or {}),
            outbound=_load_models(data.get("outbound")),
            inbound=_load_models(data.get("inbound")),
            hotels=_load_models(data.get("hotels")),
            evidences=_load_models(data.get("evidences")),
            places=_load_models(data.get("places")),
            provider_calls=list(data.get("provider_calls") or []),
            degradations=[coerce_str(item) for item in (data.get("degradations") or []) if coerce_str(item)],
            discovery_status=coerce_str(data.get("discovery_status")) or "READY",
            discovery=dict(data.get("discovery") or {}),
            ledger={
                str(key): int(value)
                for key, value in (data.get("ledger") or {}).items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
            grace_waited_ms=int(data.get("grace_waited_ms") or 0),
        )


def _dump_model(item: Any) -> Any:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return item


def _load_models(items: Any) -> list[Any]:
    """按 name 字段判断该还原成哪个模型。

    用"检查 model 类名"而不是"记住列表顺序"：会话 JSON 是跨版本存在的，
    顺序变了不该让还原静默错位。
    """

    result: list[Any] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        model = _MODEL_BY_NAME.get(_model_name_of(item))
        if model is None:
            continue
        try:
            result.append(model.model_validate(dict(item)))
        except Exception:  # noqa: BLE001 —— 单条坏数据不该毁掉整次复用
            continue
    return result


def _model_name_of(item: Mapping[str, Any]) -> str:
    """从字段形状反推模型类型（dump 里不带类名）。"""

    if "flight_no" in item:
        return "FlightOption"
    if "train_no" in item:
        return "TrainOption"
    if "hotel_id" in item or "price_per_night" in item:
        return "HotelOption"
    if "place_id" in item and "normalized_name" in item:
        return "Place"
    if "evidence_id" in item or "source_type" in item and "text" in item:
        return "Evidence"
    return ""


def _model_registry() -> dict[str, Any]:
    from app.models import Evidence as _Evidence
    from app.models import FlightOption as _Flight
    from app.models import HotelOption as _Hotel
    from app.models import Place as _Place
    from app.models import TrainOption as _Train

    return {
        "FlightOption": _Flight,
        "TrainOption": _Train,
        "HotelOption": _Hotel,
        "Place": _Place,
        "Evidence": _Evidence,
    }


_MODEL_BY_NAME = _model_registry()


__all__ = [
    "TransportCandidates",
    "HotelCandidates",
    "SocialEvidence",
    "PlaceCandidates",
    "PrefetchBundle",
    "fetch_transport_candidates",
    "fetch_hotel_candidates",
    "discover_social_evidence",
    "extract_place_candidates",
    "prefetch",
    "SOCIAL_QUERY_LIMIT",
    "WEB_QUERY_LIMIT",
    "MAX_EVIDENCE",
    "EXTRACT_EVIDENCE_LIMIT",
    "EVIDENCE_TEXT_CHARS",
    "POI_QUERY_LIMIT",
    "POI_PAGE_SIZE",
    "PREFERENCE_POI_QUERY",
    "NON_PLACE_QUERY_WORDS",
]
