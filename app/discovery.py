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

from app import models
from app import places
from app.config import current_config
from app.llm import LLM, degraded_note, invoke_json_in_batches
from app.models import (
    Decision,
    DecisionStatus,
    Evidence,
    HotelOption,
    Place,
    PreferenceProfile,
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
from app.prompts import EXTRACT_PLACES_BATCH_PROMPT, RESEARCH_QUERY_EXPANSION_PROMPT
from app.providers import ProviderHub
from app.selection import place_category

# ======================================================================
# 上限与关键词表（原来散在 workflow.py 里，随取数逻辑一起搬过来；workflow 再 re-export）
# ======================================================================

#: 取数上限（社交/网页/抽取/POI）**一律读 `app.config`**，这里不再留同名镜像：
#: 镜像与 config 迟早漂移，而"节点实际读的是哪一个"从代码上看不出来。

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
    #: 真正发起过 Provider 检索的 query（小红书线 + 网页线，含失败的尝试）。
    #: 与 ``queries``（计划要搜的）分开：正式 run 只有知道"哪些真的搜过了"，
    #: 才能只补差集，而不是全量重搜（白付一次模型调用）或全量复用（漏地点）。
    served_queries: list[str] = field(default_factory=list)
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
    #: 实体层这次做了什么（复用 / 新建 / 合并 / 丢弃 / 折叠）。管理端与 run 详情用它回答
    #: "这次为什么（没）重新查高德"，详见 `places.PlaceResolver.traces`。
    resolver: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlaceResolution:
    """一次"搜索结果 → canonical 候选"的产物。"""

    places: list[Place] = field(default_factory=list)
    outcome: Any = None  # places.IngestOutcome
    traces: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    searched_terms: list[str] = field(default_factory=list)
    reused_terms: list[str] = field(default_factory=list)


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
    #
    # 火车走**对冲**（与正式 run 同一套逻辑）：12306 是主源，但实测单次能打到 90s 预算
    # 上限（Discovery 里两次超时就是 180s），而它慢的时候途牛往往早就回来了。
    # 对冲让主源慢时提前采用备胎，而不是"盯到 90s 再串行等途牛" —— 那正是用户在前端
    # 选偏好时白白多等的那两分钟。
    hedge = bool(cfg.transport_hedge_enabled)
    tasks: list[Any] = [
        lambda: hub.search_trains(origin, destination, start, hedge=hedge),
        lambda: hub.search_flights(origin, destination, start, travelers=travelers),
    ]
    if has_back:
        tasks.append(lambda: hub.search_trains(destination, origin, back, hedge=hedge))
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
    social_keywords = list(result.queries[: current_config().social_query_limit])
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
        if len(evidences) >= current_config().evidence_max:
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

    web_keywords = list(result.queries[: current_config().web_query_limit])
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

    result.evidences = evidences[: current_config().evidence_max]
    # 记录"真的搜过哪些 query"：小红书线与网页线各取前 N 条发起过 Provider 调用
    # （无论成败都算 attempt）。正式 run 用它与计划 queries 求差集，只补没收到的部分。
    result.served_queries = _dedupe_queries([*social_keywords, *web_keywords])
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
    limit = current_config()
    return [query.strip() for query in queries if query.strip()][
        : limit.social_query_limit + limit.web_query_limit
    ]


def _normalize_query(query: Any) -> str:
    """检索词比较用的归一化键：去掉所有空白 + 大小写折叠。

    为什么这么宽：模型扩写与规则兜底对同一意图会产出带空格/不带空格的变体
    （"成都 攻略" vs "成都攻略"），严格相等会把已搜过的 query 误判成"还没搜"，
    于是又白搜一遍。空白与大小写不承载检索语义，比较时必须忽略。
    """

    return "".join(coerce_str(query).split()).casefold()


def _dedupe_queries(queries: Sequence[str]) -> list[str]:
    """按归一化键去重但保留首次出现的原始写法与顺序（顺序影响证据合并的截断点）。"""

    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        text = coerce_str(query).strip()
        key = _normalize_query(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# ======================================================================
# 地点
# ======================================================================


def extract_places_from_evidences(
    llm: LLM,
    evidences: Sequence[Evidence],
) -> tuple[list[dict[str, Any]], list[str]]:
    """从攻略证据里抽出 ``{...地点字段..., "evidence_id": 来源 id}`` 条目。

    返回 ``(条目, 降级说明)``。正式 run 与 Discovery 共用这一个实现 —— 否则"批大小、
    并发上限、归属规则、失败降级文案"会在两处各自漂移，而它们全是性能/正确性开关。

    两件事同时做，缺一不可：
      * **多篇合成一批**：一次模型往返处理多篇，减少往返次数；
      * **批之间受控并发**：把墙钟压到最慢那一批，并发上限来自 config
        （`LLM` 客户端还有第二道闸门）。
    批大小、正文截断长度、最多抽几条，同样全部来自 config。
    """

    cfg = current_config()
    targets = [
        evidence
        for evidence in evidences
        if not evidence.place_mentions and len(evidence.text) >= 40
    ][: cfg.extract_evidence_limit]
    if not targets:
        return [], []

    extracted: list[dict[str, Any]] = []
    degradations: list[str] = []
    batch_size = max(1, int(cfg.extract_batch_size))
    single_payload: dict[str, str] = {
        evidence.id: (
            f"### 证据 id：{evidence.id}\n"
            f"标题：{evidence.title}\n"
            f"来源：{evidence.provider}/{evidence.source_type}\n"
            f"正文：\n{evidence.text[: cfg.extract_text_chars]}"
        )
        for evidence in targets
    }
    batches: list[tuple[list[str], str]] = []
    for start in range(0, len(targets), batch_size):
        chunk = targets[start : start + batch_size]
        batches.append(([evidence.id for evidence in chunk], "\n\n".join(single_payload[e.id] for e in chunk)))

    batch_results = invoke_json_in_batches(
        llm,
        system=EXTRACT_PLACES_BATCH_PROMPT,
        batches=batches,
        tag="extract_places",
        max_workers=cfg.llm_max_concurrency,
    )

    # 批超时不该连带丢掉批里**所有**证据：批内各篇本来就是互相独立的。
    # 所以整批失败且批里不止一篇时，按单篇**重试一次**（预算更紧），把
    # "一次慢调用损失 2 篇"降级成"最坏情况下每篇各自决定成败"。
    # 只重试"整批失败"的情形：单篇批失败了重试不会有不同结果。
    retry_batches = [
        ([item_id], single_payload[item_id])
        for ids, response in batch_results
        if len(ids) > 1 and not (response.ok and isinstance(response.value, dict))
        for item_id in ids
    ]
    if retry_batches:
        degraded_notes = [
            f"证据 {'、'.join(ids)} 的批量抽取未成功（{response.status}），已按单篇重试一次"
            for ids, response in batch_results
            if len(ids) > 1 and not (response.ok and isinstance(response.value, dict))
        ]
        degradations.extend(degraded_notes)
        batch_results = list(batch_results) + [
            *invoke_json_in_batches(
                llm,
                system=EXTRACT_PLACES_BATCH_PROMPT,
                batches=retry_batches,
                tag="extract_places_retry",
                max_workers=cfg.llm_max_concurrency,
            )
        ]

    extracted: list[dict[str, Any]] = []
    known_ids = {evidence.id for evidence in targets}
    # 按**批的定义顺序**消费：并发与批大小都不会改变抽出来的地点顺序。
    for ids, response in batch_results:
        if not (response.ok and isinstance(response.value, dict)):
            for item_id in ids:
                degradations.append(
                    degraded_note(response, f"证据 {item_id} 的地点抽取被跳过（同批 {len(ids)} 条一起失败）")
                )
            continue
        payload = response.value
        entries = payload.get("results")
        if not isinstance(entries, list):
            # 模型只回了单篇结构：只有批里就一条证据时归属才是无歧义的；多篇时宁可丢掉
            # 也不猜 —— 猜出来的归属会把地点挂到错误的证据上，比少一条地点更糟。
            if len(ids) == 1:
                entries = [{"evidence_id": ids[0], "places": payload.get("places") or []}]
            else:
                degradations.append(
                    f"一批 {len(ids)} 条证据的抽取返回了单篇结构、无法判定归属，本批地点按缺失处理"
                )
                continue
        attributed: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            owner = coerce_str(entry.get("evidence_id")).strip()
            if owner not in known_ids:
                continue
            attributed.add(owner)
            for item in entry.get("places") or []:
                if isinstance(item, dict) and coerce_str(item.get("name")):
                    extracted.append({**item, "evidence_id": owner})
        missing = [item_id for item_id in ids if item_id not in attributed]
        if missing:
            degradations.append(
                f"证据 {'、'.join(missing)} 的抽取结果里没有对应的归属条目，这几条按抽不到地点处理"
            )
    return extracted, degradations


def open_resolver(
    *,
    store: Any | None,
    destination: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> "places.PlaceResolver":
    """建一个绑定到目的地城市的实体解析器（`store=None` 时退化成纯内存）。"""

    return places.PlaceResolver(
        store, city=destination, run_id=run_id, session_id=session_id
    )


def resolve_pois(
    *,
    resolver: "places.PlaceResolver",
    results: Mapping[str, Any],
    terms: Sequence[str],
    extra_places: Sequence[Place] = (),
) -> PlaceResolution:
    """把"检索词 → Provider 结果"收敛成 canonical 候选（A3 / A4 / A6 / A8）。

    这是**唯一**一处把高德返回变成候选的地方：Discovery 与正式 run 都走它，所以
    "地理围栏、子设施收敛、连锁分店保护、实体复用"只有一份实现。

    `terms` 是**真正打过 Provider 的词**（命中别名 / 查询缓存的词不该出现在这里，
    它们的候选从 `extra_places` 传进来），顺序即候选顺序。

    绝不空榜：收敛后一个都不剩而原始候选非空时，退回原始候选并如实记降级 ——
    候选里混着子设施也比让用户面对空列表好，但必须说清这份列表是没收敛过的。
    """

    record_order = 0
    records: list[places.PoiRecord] = []
    searched: list[str] = []
    raw_places: list[Place] = []
    seen_ids: set[str] = set()
    for term in terms:
        result = results.get(term)
        searched.append(term)
        provider_ids: list[str] = []
        for place in getattr(result, "items", None) or []:
            if not place.place_id or place.place_id in seen_ids:
                continue
            seen_ids.add(place.place_id)
            raw_places.append(place)
            provider_ids.append(place.place_id)
            records.append(places.PoiRecord.from_place(place, source_query=term, order=record_order))
            record_order += 1
        resolver.record_query(term, provider_result_ids=provider_ids)
    for place in extra_places:
        if not place.place_id or place.place_id in seen_ids:
            continue
        seen_ids.add(place.place_id)
        raw_places.append(place)
        records.append(places.PoiRecord.from_place(place, order=record_order))
        record_order += 1

    outcome = resolver.ingest(records)
    resolution = PlaceResolution(
        places=outcome.places,
        outcome=outcome,
        degradations=list(outcome.degradations),
        notes=list(outcome.notes),
        searched_terms=searched,
    )
    if outcome.dropped:
        out_of_city = [item for item in outcome.dropped if item["reason"] == "out_of_city"]
        skipped = [item for item in outcome.dropped if item["reason"] != "out_of_city"]
        if out_of_city:
            resolution.notes.append(
                f"地理围栏剔除 {len(out_of_city)} 条不属于目的地的 POI（"
                + "、".join(f"{item['name']}（{item.get('detail', '')}）" for item in out_of_city[:3])
                + "）"
            )
        if skipped:
            resolution.notes.append(
                f"{len(skipped)} 条不适合当候选的 POI 没有进候选（附属设施 / 地址桩 / 住宅等）："
                + "、".join(str(item["name"]) for item in skipped[:5])
            )
    if outcome.folded:
        resolution.notes.append(
            f"{len(outcome.folded)} 个子设施挂到了母体上（不再并列成候选）："
            + "、".join(f"{item['name']}→{item['parent_name']}" for item in outcome.folded[:4])
        )
    if outcome.merged:
        resolution.notes.append(f"实体层合并了 {outcome.merged} 条同实体的重复记录")
    if not outcome.places and raw_places:
        # 兜底分两级，先要"仍然干净"，再退到"可能脏但至少不空"：
        #   ① 过完围栏、且不是附属设施 / 地址桩的原始候选（保住"不给用户看停车场"这条）；
        #   ② 实在连一个都没有，才退回真正未过滤的原始候选，并如实说明它没收敛。
        dropped_ids = {str(item.get("place_id") or "") for item in outcome.dropped}
        clean = [
            place
            for place in raw_places
            if str(place.place_id) not in dropped_ids
            and places.facility_kind(place.name, place.type) is None
            and places.non_destination_reason(place.type) is None
        ]
        if clean:
            resolution.places = clean
            resolution.degradations.append(
                f"实体层没有收敛出可选地点（候选全是附属设施 / 地址桩），"
                f"已退回 {len(clean)} 条原始候选（已剔除设施，但未经实体合并）"
            )
        else:
            resolution.places = raw_places
            resolution.degradations.append(
                f"实体收敛后没有剩下可选地点，已退回 {len(raw_places)} 条**未经收敛**的原始候选；"
                "这份列表可能包含附属设施"
            )
    return resolution


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
    store: Any | None = None,
) -> PlaceCandidates:
    """从攻略提到的地方 + 关键词，去高德查成真实 POI 候选。

    关键设计：**攻略里的地名只当搜索关键词用**。候选地点必须能被高德查到，
    所以坐标、行政区、营业时间全部有出处；查不到就不进候选，绝不用模型编一个地点。

    查之前必须过一遍 `PlaceResolver`（A3）：别名或查询缓存命中就不打高德 ——
    "Agent 可以决定要查什么，但不能决定绕过缓存重新查"。

    Discovery 阶段只做到这里：不两两算路线、不逐个查门票 —— 那要等用户选完之后做。
    ``ledger`` 让同一次 Discovery 内的重复关键词只查一次高德（Part C）。
    """

    result = PlaceCandidates()
    destination = _destination(intent) or ""

    provider_mentions: list[str] = []
    for evidence in evidences:
        provider_mentions.extend(evidence.place_mentions)

    extracted, extract_degradations = extract_places_from_evidences(llm, evidences)
    result.degradations.extend(extract_degradations)
    result.extracted_from_evidence = len(extracted)
    # A5：抽出来的地名必须写回证据，否则 city_poi_mentions 永远是空表
    # （它按 evidence.place_mentions 建行，而模型抽出的地名此前从不落回这里）。
    apply_extracted_mentions(evidences, extracted)

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
    search_terms = _dedupe_queries([*raw_names[: cfg.discovery_max_places], *keywords])

    resolver = open_resolver(
        store=store if store is not None else getattr(hub, "store", None),
        destination=destination,
        run_id=getattr(hub, "run_id", None) or None,
    )
    # A3 / A4：先问实体层"这些词还值得查吗"。命中别名或查询缓存的词一次高德都不打。
    to_search, hits = resolver.plan_queries(search_terms)
    reused_places: list[Place] = []
    for hit in hits:
        reused_places.extend(hit.places)
    if hits:
        result.notes.append(
            f"{len(hits)} 个检索词命中已有实体 / 查询缓存，未重复调用高德："
            + "、".join(f"{hit.term}（{hit.reason}）" for hit in hits[:4])
            + ("……" if len(hits) > 4 else "")
        )

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
            for term in to_search
        ],
        max_workers=cfg.provider_max_concurrency,
        thread_prefix="tp-disc-poi",
    )

    results: dict[str, Any] = {}
    poi_status = "OK"
    for term, outcome in zip(to_search, outcomes):
        if not outcome.ok or outcome.value is None:
            result.degradations.append(f"高德 POI 查询「{term}」失败：{outcome.error}")
            results[term] = None
            continue
        results[term] = outcome.value
        poi_status = poi_status if poi_status != "OK" else outcome.value.status

    resolution = resolve_pois(
        resolver=resolver,
        results=results,
        terms=to_search,
        extra_places=reused_places,
    )
    result.degradations.extend(resolution.degradations)
    result.notes.extend(resolution.notes)
    result.resolver = {
        **(resolver.flush()),
        "reused_terms": [hit.term for hit in hits],
        "reused_actions": {hit.term: hit.action for hit in hits},
        "searched_terms": resolution.searched_terms,
        "folded": list(getattr(resolution.outcome, "folded", []) or []),
        "dropped": list(getattr(resolution.outcome, "dropped", []) or []),
        "traces": resolution.traces,
    }
    result.keyword_hits = len(to_search)

    deduped = resolution.places
    if not deduped and destination:
        result.degradations.append(
            f"高德 POI 查询没有返回任何地点（{poi_status}），本次没有可选地点；"
            "没有用模型生成的地点填补"
        )
    result.amap_status = poi_status

    for entry in resolution.traces:
        if str(entry.get("action")) != "folded":
            continue
        result.decisions.append(
            Decision(
                entity_id=str(entry.get("canonical_place_id") or ""),
                status=DecisionStatus.KEEP,
                agent_or_stage="place_resolver",
                reason_codes=["folded_facility", str(entry.get("relation_type") or "child")],
                reason_text=str(entry.get("reason") or "子设施并入母体"),
                scores={"confidence": entry.get("confidence")},
            )
        )

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
    # A4 的 Cache First：实体层已经有地址 + 营业时间的不再打一次高德。
    verify = max(0, int(verify_limit if verify_limit is not None else cfg.discovery_poi_verify_limit))
    detail_targets = [
        place for place in deduped[:verify] if resolver.needs_detail(place.place_id)
    ]
    skipped_details = len(deduped[:verify]) - len(detail_targets)
    if skipped_details:
        result.notes.append(
            f"{skipped_details} 个候选的详情（地址 / 营业时间）已在实体层里，未重复查高德"
        )
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


def apply_extracted_mentions(
    evidences: Sequence[Evidence], extracted: Sequence[Mapping[str, Any]]
) -> int:
    """把模型抽出的地名写回 `evidence.place_mentions`，返回写入条数。

    A5 的根因就在这一步缺失：`city_poi_mentions` 是按 `evidence.place_mentions` 建行的，
    而模型抽出的地名此前只进 `extracted` 列表、从不回到证据对象上 —— 于是不管抽到多少
    地点，那张表恒为 0 行（线上 0/213 有值）。

    写回而不是另存一份映射：`place_mentions` 的语义本来就是"这篇攻略提到了哪些地点"，
    数据源自带的提及与模型抽出的提及在这里没有区别；分开存只会让下游两个来源各写一遍。
    只追加不覆盖，并按原顺序去重（同一篇里同一地名出现两次不该记两行）。
    """

    by_id = {evidence.id: evidence for evidence in evidences}
    written = 0
    for item in extracted:
        owner = by_id.get(coerce_str(item.get("evidence_id")).strip())
        name = coerce_str(item.get("name")).strip()
        if owner is None or not name:
            continue
        if name in owner.place_mentions:
            continue
        owner.place_mentions.append(name)
        written += 1
    return written


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
# 住宿区域与候选池（E3 / E4：docs/14 §7 / §9）
# ======================================================================

#: 类别 → 候选池。景点/历史/自然/亲子/拍照归"游玩"，美食归"美食"，其余归"体验"。
#: contract 2 的三个 key 固定，前端按三张卡片列渲染；顺序也固定（attraction 在前）。
#: 映射本体住在 `places`（实体层判定"两条 POI 是不是同一类地方"要用它），这里只做引用。
_POOL_BY_CATEGORY: dict[str, str] = places.POOL_BY_CATEGORY

POOL_KEYS = ("attraction", "food", "experience")

#: 住宿区域候选的上限（docs/14 §9：3~4 个）。不放 config —— 这是角色 B 的产品口径，
#: 与取数预算（归 A）不是一回事。
HOTEL_AREA_LIMIT = 4

_CATEGORY_AREA_TAG: dict[str, str] = {
    "food": "美食多",
    "shopping": "商圈便利",
    "nightview": "夜生活",
    "attraction": "景点集中",
    "history": "景点集中",
    "nature": "自然静谧",
    "experience": "体验丰富",
}


def split_poi_pools(places: Sequence[Place]) -> dict[str, list[str]]:
    """把候选地点按池拆开（docs/14 §7）：attraction / food / experience。

    返回 ``{pool: [place_id, ...]}``，保留原候选顺序（攻略提过的在前）。
    只有三个 key；前端按 place_id 合并回 place_candidates 的完整卡片。
    """

    pools: dict[str, list[str]] = {key: [] for key in POOL_KEYS}
    for place in places:
        pool = _POOL_BY_CATEGORY.get(place_category(place), "experience")
        if place.place_id and place.place_id not in pools[pool]:
            pools[pool].append(place.place_id)
    return pools


def extract_hotel_areas(
    intent: TripIntent,
    evidences: Sequence[Evidence],
    places: Sequence[Place],
    *,
    profile: PreferenceProfile | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """推荐住宿区域（docs/14 §9）：从候选地点的商圈/行政区聚类出区域候选。

    为什么用聚类而不是再调一次模型抽区域：区域本身就是高德给的地点的
    ``business_area / district``，是真实数据的聚合；攻略推荐度用"提到该区域名的攻略条数"
    近似，美食/商圈/夜生活密度用区域内候选点的类别占比近似 —— 全部确定性、可审计，
    不会因为"模型抽区域失败"而空榜（真实优先、降级披露）。

    排序是**动态权重 Top-K**（§9 的确定性一档）：先算密度信号，再用 profile.hotel_area
    强调用户更在乎的维度。没有个性化 profile 时退化为带攻略推荐的密度排序。
    """

    area_map: dict[str, dict[str, Any]] = {}
    for place in places:
        name = coerce_str(place.business_area or place.district)
        if not name:
            continue
        entry = area_map.setdefault(
            name,
            {"places": 0, "categories": {}, "evidence_mentions": 0, "has_coords": False},
        )
        entry["places"] += 1
        if place.lat is not None and place.lng is not None:
            entry["has_coords"] = True
        category = place_category(place)
        entry["categories"][category] = entry["categories"].get(category, 0) + 1

    # 攻略推荐度：哪几篇攻略正文里出现过区域名（含攻略自带的地点提及）。
    for evidence in evidences:
        text = " ".join(
            filter(None, [evidence.title, " ".join(evidence.place_mentions or []), evidence.text])
        )
        for name in list(area_map):
            if name and name in text:
                area_map[name]["evidence_mentions"] += 1

    if not area_map:
        return []

    max_mentions = max((entry["evidence_mentions"] for entry in area_map.values()), default=1) or 1

    def _signals(entry: dict[str, Any]) -> dict[str, float]:
        categories = entry["categories"]
        place_n = max(1, int(entry["places"]))
        return {
            "food_density": round(min(1.0, categories.get("food", 0) / place_n), 3),
            "commercial_area": round(min(1.0, categories.get("shopping", 0) / place_n), 3),
            "nightlife": round(min(1.0, categories.get("nightview", 0) / place_n), 3),
            "poi_centrality": round(min(1.0, place_n / 5.0), 3),
            "evidence": round(min(1.0, entry["evidence_mentions"] / max(2, max_mentions)), 3),
        }

    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for name, entry in area_map.items():
        signals = _signals(entry)
        fit = _area_fit_score(signals, profile)
        tags = _area_tags(entry["categories"])
        ranked.append((fit, name, {"signals": signals, "categories": entry["categories"], "tags": tags}))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    areas: list[dict[str, Any]] = []
    cap = max(1, int(limit if limit is not None else HOTEL_AREA_LIMIT))
    for fit, name, meta in ranked[:cap]:
        categories = meta["categories"]
        place_n = sum(categories.values())
        areas.append(
            {
                "key": name,
                "name": name,
                "reason": (
                    f"{name}：{place_n} 个候选地点"
                    + (f"，{meta['signals']['evidence']:.0%} 的攻略在此区域有提及" if max_mentions else "")
                    + f"，以{'/'.join(meta['tags'][:2]) or '综合'}为主"
                ),
                "tags": meta["tags"],
                "fit_score": fit,
            }
        )
    return areas


def _area_fit_score(signals: dict[str, float], profile: PreferenceProfile | None) -> float:
    """区域综合契合度 = 0.5×攻略推荐 + 0.5×profile 加权的密度信号。

    没有个性化 profile 时用均衡基线的 hotel_area 权重；个性化时用 LLM 给的权重，
    "主要想吃"的用户会看到美食密度权重被抬高。
    """

    weights = (
        profile.hotel_area
        if profile is not None and profile.personalized
        else models.DEFAULT_HOTEL_AREA_WEIGHTS
    )
    available = ("food_density", "commercial_area", "nightlife", "poi_centrality")
    wsum = sum(float(weights.get(key, 0.0)) for key in available) or 1.0
    core = sum(signals[key] * float(weights.get(key, 0.0)) for key in available) / wsum
    return round(0.5 * core + 0.5 * signals["evidence"], 4)


def _area_tags(categories: Mapping[str, int]) -> list[str]:
    ordered = sorted(categories.items(), key=lambda item: (-item[1], item[0]))
    tags: list[str] = []
    for category, _count in ordered:
        label = _CATEGORY_AREA_TAG.get(category)
        if label and label not in tags:
            tags.append(label)
    return tags[:3]


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
    store: Any | None = None,
) -> dict[str, Any]:
    """四条线并行取数，返回给 Planning Session 存草稿的原始结果。

    为什么并行：用户在选交通/酒店/节奏的这几十秒里，后台应该已经在查了。
    串行会让"Prefetch 提前开始"这件事失去意义。

    单条线失败不影响其它线（`TransportCandidates` 等各自带 degradations），
    这也满足验收标准里"social / hotel / transport 失败仍可继续"。

    ``ledger`` 是这一次 Discovery 的查询键账本（Part C）：四条线共用一个，
    所以"交通查过的键""social 查过的关键词"彼此之间也不会重复请求。

    ``store`` 让地点实体层（`app/places.py`）能把这次的结果沉淀成跨会话可复用的
    canonical 实体；不传就只能在本进程内收敛，下次去同一座城市仍要从零判断。
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

    # --- E1/E3/E4：动态偏好画像 + 住宿区域 + 候选池（角色 B 产物，contract 2 字段来源）---
    # 画像只依赖 intent，和"抽取地点"这条最慢的线**并行**跑，因此不为 Discovery 增加墙钟。
    # 区域与池是从已查好的地点/攻略**聚合**，确定性计算，不再打 Provider。
    # 失败一律退回均衡基线/空集，绝不影响 Discovery 本身。A 透传（prefetch_json 拆分）时
    # 必须原样带走 `profile` / `hotel_areas` / `poi_pools` 这三个键。
    from app.decision.profile import generate_preference_profile

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tp-disc-profile") as profile_pool:
        profile_future = profile_pool.submit(generate_preference_profile, intent, llm)

        places = PlaceCandidates()
        places_started = _time.perf_counter()
        if social.evidences:
            try:
                places = extract_place_candidates(
                    hub, llm, intent, social.evidences, queries=social.queries,
                    ledger=books, store=store,
                )
            except Exception as exc:  # noqa: BLE001
                errors["places"] = f"{type(exc).__name__}: {exc}"
                # 如实记进**候选自身的**降级说明，而不只是 `errors`：`errors` 只进 stages
                # 快照，用户与正式 run 看到的是 `PlaceCandidates.degradations`。两者不一致时
                # 就会出现"一个地点都没有，但没人说得清为什么"（线上真实发生过一次：
                # 实体层建表少了一列，全城预热 0 候选，而所有降级字段都是空的）。
                places.degradations.append(
                    f"地点候选生成失败（{type(exc).__name__}: {exc}），本次没有可选地点"
                )
        outcomes["places"] = places
        started_at["places"] = places_started
        report("places")

        try:
            profile_obj: PreferenceProfile = profile_future.result()
        except Exception:  # noqa: BLE001 —— 画像失败不该影响 Discovery
            profile_obj = PreferenceProfile.default_profile()

    hotel_areas: list[dict[str, Any]] = []
    poi_pools: dict[str, list[str]] = {key: [] for key in POOL_KEYS}
    try:
        hotel_areas = extract_hotel_areas(intent, social.evidences, places.places, profile=profile_obj)
        poi_pools = split_poi_pools(places.places)
    except Exception:  # noqa: BLE001 —— 区域/池聚合失败不该让 Discovery 挂掉
        hotel_areas = []
        poi_pools = {key: [] for key in POOL_KEYS}

    transport: TransportCandidates | None = outcomes.get("transport")
    hotels: HotelCandidates | None = outcomes.get("hotels")
    stages = {name: dict(info) for name, info in timings.items()}
    # 地点实体层这次做了什么（复用 / 新建 / 合并 / 丢弃 / 折叠）跟着 places 阶段一起走：
    # 它进 `PrefetchBundle.discovery` → 会话的 discovery_json → 管理端，
    # 于是"这次为什么（没有）重新查高德"在事后能被回答（方案 §21）。
    stages.setdefault("places", {})["resolver"] = places.resolver
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
        # 角色 B 的决策产物（contract 2）：画像 / 住宿区域 / 候选池。
        "profile": profile_obj.model_dump(mode="json"),
        "hotel_areas": hotel_areas,
        "poi_pools": poi_pools,
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
    #: Discovery 计划要搜的社交/网页检索词（模型扩写或规则兜底的结果）。
    #: 带回来是为了让正式 run 不再为"重新算出同样的检索词"再付一次 query_expansion
    #: 模型调用（实测约 13.5s），也不必凭空猜检索词。
    social_queries: list[str] = field(default_factory=list)
    #: Discovery 真正发起过 Provider 检索的检索词（含失败的尝试）。缺失的 query 才需要
    #: 正式 run 定向补搜 —— 全量重搜会白付模型调用与一串 Provider 请求，全量复用又会漏地点。
    social_served_queries: list[str] = field(default_factory=list)
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
    # B 的跨步骤产物。Prefetch 是跨版本 JSON，字段必须显式保留，不能在 load/dump 期间丢失。
    hotel_areas: list[dict[str, Any]] = field(default_factory=list)
    poi_pools: dict[str, list[Any]] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    #: A 的城市缓存新鲜度（不等同于会话的 updated_at）。
    city_cache: dict[str, Any] = field(default_factory=dict)
    #: 前向兼容：未来角色新增的 Prefetch JSON 字段原样过户给正式 run。
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def reused(self) -> bool:
        return bool(self.outbound or self.inbound or self.hotels or self.evidences or self.places)

    def missing_social_queries(self, needed: Sequence[str]) -> list[str]:
        """返回 ``needed`` 里 Discovery **没有真正搜过**的检索词（差集），供正式 run 定向补搜。

        WHY：Discovery 可能只跑完了一部分 queries（还在 RUNNING 或某条线失败），全量重搜
        会白付一次模型调用与一串 Provider 请求，全量复用又会漏地点 —— 所以要按 query 粒度
        算差集。比较忽略大小写与所有空白（模型扩写与规则兜底对同一意图会产出
        "成都 攻略" / "成都攻略" 这类变体，严格相等会误判成"还没搜"），
        结果保留 ``needed`` 的原始写法与顺序，并按归一化键去重。
        """

        served = {_normalize_query(query) for query in self.social_served_queries}
        return [query for query in _dedupe_queries(needed) if _normalize_query(query) not in served]

    def dump(self) -> dict[str, Any]:
        payload = dict(self.extras)
        payload.update({
            "session_id": self.session_id,
            "basic_intent": self.basic_intent,
            "outbound": [_dump_model(item) for item in self.outbound],
            "inbound": [_dump_model(item) for item in self.inbound],
            "hotels": [_dump_model(item) for item in self.hotels],
            "evidences": [_dump_model(item) for item in self.evidences],
            "places": [_dump_model(item) for item in self.places],
            "social_queries": list(self.social_queries),
            "social_served_queries": list(self.social_served_queries),
            "provider_calls": list(self.provider_calls),
            "degradations": list(self.degradations),
            "discovery_status": self.discovery_status,
            "discovery": dict(self.discovery),
            "ledger": dict(self.ledger),
            "grace_waited_ms": self.grace_waited_ms,
            "hotel_areas": list(self.hotel_areas),
            "poi_pools": dict(self.poi_pools),
            "profile": dict(self.profile),
            "city_cache": dict(self.city_cache),
        })
        return payload

    @classmethod
    def load(cls, payload: Mapping[str, Any] | None) -> "PrefetchBundle":
        """从会话 JSON 还原；任何一块坏了都只丢那一块，不让整次规划失败。"""

        data = dict(payload or {})
        known = {
            "session_id", "basic_intent", "outbound", "inbound", "hotels", "evidences", "places",
            "social_queries", "social_served_queries", "provider_calls", "degradations",
            "discovery_status", "discovery", "ledger", "grace_waited_ms", "hotel_areas",
            "poi_pools", "profile", "city_cache",
        }
        return cls(
            session_id=coerce_str(data.get("session_id")) or None,
            basic_intent=dict(data.get("basic_intent") or {}),
            outbound=_load_models(data.get("outbound")),
            inbound=_load_models(data.get("inbound")),
            hotels=_load_models(data.get("hotels")),
            evidences=_load_models(data.get("evidences")),
            places=_load_models(data.get("places")),
            # 纯字符串列表，不走 _load_models/_model_registry 的模型机制；
            # 旧 payload（本次改动前存的）没有这两个键 → 空列表，绝不因此抛异常。
            social_queries=[coerce_str(item) for item in (data.get("social_queries") or []) if coerce_str(item)],
            social_served_queries=[
                coerce_str(item) for item in (data.get("social_served_queries") or []) if coerce_str(item)
            ],
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
            hotel_areas=[item for item in (data.get("hotel_areas") or []) if isinstance(item, Mapping)],
            poi_pools={
                str(key): list(value) for key, value in (data.get("poi_pools") or {}).items()
                if isinstance(value, list)
            },
            profile=dict(data.get("profile") or {}),
            city_cache=dict(data.get("city_cache") or {}),
            extras={key: value for key, value in data.items() if key not in known},
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
    "extract_hotel_areas",
    "split_poi_pools",
    "prefetch",
    "extract_places_from_evidences",
    "PREFERENCE_POI_QUERY",
    "NON_PLACE_QUERY_WORDS",
]
