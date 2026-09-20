"""固定工作流（PRD §25）：把一句自然语言变成一份可执行、可审计的行程。

为什么是 LangGraph StateGraph，而不是一个大函数
--------------------------------------------
PRD §25 把流程钉成 12 步，每一步的**输入输出边界**本身就是产品的一部分：
审计要能回答"第 5 步抽出了哪些地点、第 6 步哪些路线没核实、第 11 步 Critic 说了什么"。
拆成 12 个 node、每个 node 只往 state 里写自己负责的字段，这 12 个问题的答案就是
state 的 12 个键，不需要再从日志里反推。副作用是每步都能单独跑单测。

为什么没有把 Agent 塞进流程里
--------------------------
"Evidence First, LLM Second" 的工程含义是：**决定"做什么"的是代码，模型只负责
"把自然语言变成结构化输入"和"把算好的结果讲成人话"**。所以流程骨架是固定边，
模型只在 4 处被调用（见 `app/llm.py`），且任何一处失败都只是降级，不会改道。

State 里为什么能放 ProviderHub / Store 这种对象
--------------------------------------------
这张图**不带 checkpointer**（固定流程天生可重放，不需要断点续跑），所以 state 从不
被序列化，放句柄是安全的。真要加 checkpointer 时，这几个键必须换成一个可重建的
轻量标识，这是唯一的约束。
"""

from __future__ import annotations

import json
import operator
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Callable, Mapping, Sequence, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app import planner, selection
from app.badcase import BadCaseContext, detect_badcases, summarize as summarize_badcases
from app.config import (
    current_config,
    current_tuning,
    performance_config_snapshot,
    provider_timeouts,
)
from app.decision.jev import JevClient
from app.decision.planner_decision import (
    DECISION_PLAN_CHOICE,
    DECISION_QUALITY_GATE,
    DECISION_TRADEOFF,
    QualityGate,
    choose_plan,
    quality_gate,
    resolve_tradeoff,
)
from app.llm import LLM, degraded_note
from app.models import (
    BudgetSummary,
    Decision,
    DecisionStatus,
    Evidence,
    FeasibilityIssue,
    FlightOption,
    HotelOption,
    HotelPlan,
    ItineraryDay,
    Place,
    RouteOption,
    SourceRef,
    TrainOption,
    TransportPlan,
    TripIntent,
    TripPlan,
    coerce_float,
    coerce_int,
    coerce_str,
    coerce_str_list,
    utcnow,
)
from app.prompts import (
    CRITIC_PROMPT,
    EXTRACT_PLACES_PROMPT,
    FINAL_ANSWER_PROMPT,
    INTENT_PARSE_PROMPT,
    RESEARCH_QUERY_EXPANSION_PROMPT,
)
from app.observability import (
    MARK_CACHE_HIT,
    MARK_DUPLICATE_QUERY,
    MARK_FALLBACK_QUERY,
    MARK_PREFETCH_REUSED,
    MARK_PROVIDER_CALLED,
    MARK_PROVIDER_TIMEOUT,
    MARK_ROUTE_CACHE_HIT,
    MARK_ROUTE_REQUESTED,
    MARK_ROUTE_SKIPPED,
    CallLedger,
    SpanKind,
    call_with_deadline,
    finish_iso,
    now_iso,
    query_key,
    span_id,
    start_iso,
)
from app.providers import ProviderHub, default_mcp_servers, lnglat
from app.store import TravelPlanStore
from app.version import travelplan_commit

# ==================================================
# 常量
# ==================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"

WORKFLOW_NAME = "travelplan_workflow"
#: 这段描述会被 Workflow Router 拿去和用户 Query 做 BM25，所以要写用户会说的话。
WORKFLOW_DESCRIPTION = (
    "规划一次中国境内旅行：查大交通（火车票/机票）、查酒店、查景点与攻略、"
    "排每天行程、算预算、检查时间是否来得及，最后产出可执行的行程方案。"
)

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"

#: 一次 run 的抓取预算。**这些数字现在是 app/config.py 的默认值的历史镜像**：
#: 实际生效的值一律从 `current_config()` 读（Part D：散落的写死数字要收到 config 里，
#: 才能通过环境变量 / 管理端在不重启服务的前提下调整）。这里保留同名常量只是为了
#: 让"默认是多少"在节点代码旁边一眼可见，节点里不再直接使用它们。
SOCIAL_QUERY_LIMIT = 3
WEB_QUERY_LIMIT = 2
MAX_EVIDENCE = 40
EXTRACT_EVIDENCE_LIMIT = 4
EVIDENCE_TEXT_CHARS = 1200
POI_QUERY_LIMIT = 8
POI_PAGE_SIZE = 10
POI_DETAIL_LIMIT = 6
TICKET_LOOKUP_LIMIT = 3
MAX_ROUTE_LOOKUPS = 14
MAX_TRANSPORT_ALTERNATIVES = 4

#: 路线查询的"坐标初筛"阈值（Part D §7）：两点直线距离超过这个值就判定为非相邻路段，
#: 不做市内路线查询。60km 是"市内交通"与"跨城移动"的分界 —— 一天的行程本来就不该
#: 出现跨城往返，真出现了也是排程问题，不该拿市内路线预算去掩盖它。
#: 被跳过的段进 Trace（route_skipped），行程里仍按"未核实"处理，绝不用估算冒充实测。
ROUTE_MAX_LEG_METERS = 60_000.0

#: 偏好词不能直接当高德检索词："拍照" 在成都搜出来的是摄影工作室、"夜景" 搜出来的是
#: 灯具店。只保留能搜到**地点**的词，需要换词的走这张映射。
PREFERENCE_POI_QUERY = {"拍照": "观景台", "放松": "茶馆", "亲子": "乐园", "夜生活": "酒吧街"}
#: 攻略型检索词（"成都 避坑 不值得"）里出现这些词，就说明它在回答"怎么玩/怎么省"，
#: 不是地点名，拿去做 POI 搜索只会引入噪声。
NON_PLACE_QUERY_WORDS = (
    "避坑", "不值得", "推荐", "攻略", "注意", "省钱", "免费", "交通", "天气",
    "住宿", "预算", "门票", "几天", "路线", "行程",
)

# ==================================================
# PRD §24 交通比选
# ==================================================
# 全部是"越低越好"的扣分项。刻意不用"总价最低"这种单一口径 —— PRD §24 明确要求
# 比较价格、总时长、出发到达时刻、机场/车站额外接驳、对首末天的影响、用户偏好。
TRANSPORT_PRICE_UNIT = 100.0
TRANSPORT_PRICE_WEIGHT = 1.0
TRANSPORT_MINUTE_UNIT = 10.0
TRANSPORT_MINUTE_WEIGHT = 1.0
#: 去程晚于 20:00 才能出门 → 首日基本报废。
TRANSPORT_LATE_ARRIVAL_MINUTES = 20 * 60
TRANSPORT_LATE_ARRIVAL_PENALTY = 25.0
#: 回程早于 12:00 就得往车站/机场走 → 末日基本报废。
TRANSPORT_EARLY_DEPARTURE_MINUTES = 12 * 60
TRANSPORT_EARLY_DEPARTURE_PENALTY = 25.0
TRANSPORT_PREFERENCE_BONUS = 40.0
TRANSPORT_UNKNOWN_PRICE_PENALTY = 25.0
TRANSPORT_UNKNOWN_DURATION_PENALTY = 30.0
#: 发车日与行程基准日每差一天。刻意给得比上面所有分项加起来还重：价格贵一点、
#: 早出发几小时都是"偏好问题"，日期错一天是"这趟车根本不在那天"，不该被价格翻盘。
TRANSPORT_OFF_DATE_PENALTY = 200.0

# ==================================================
# 住宿比选
# ==================================================
HOTEL_PRICE_UNIT = 100.0
HOTEL_RATING_WEIGHT = 2.0
HOTEL_PREFERENCE_BONUS = 20.0
HOTEL_UNKNOWN_PRICE_PENALTY = 30.0

# ==================================================
# 规则兜底用的正则（只在模型不可用时启用）
# ==================================================

#: 中国城市名最长 4 字（呼和浩特 / 乌鲁木齐 / 香格里拉）。用 {2,4} 而不是 {2,8}：
#: 放宽到 8 会让「从北京去成都吃火锅」把「成都吃火锅」整段当成城市名。
_CN_CITY = r"[\u4e00-\u9fa5]{2,4}?"
#: 城市名后面可能出现的一切实词：活动（吃/喝/玩/逛/看/买）与目的（旅游/出差），
#: 再加上标点与行尾。少了「吃」，"去成都吃火锅" 就解析不出目的地。
_DEST_TAIL = (
    r"(?:旅游|旅行|自由行|度蜜月|出差|玩|吃|喝|住|逛|看|买|泡|爬|[，,。、；;：:！!？?\s]|$)"
)
_ORIGIN_DEST_RE = re.compile(
    rf"从\s*({_CN_CITY})\s*(?:出发|走|飞|坐车|坐高铁|坐火车)?\s*"
    rf"(?:去|到|飞往|前往|抵达)\s*({_CN_CITY})\s*{_DEST_TAIL}"
)
#: 不带"从"的说法（"长春去成都，5天"）也要认，否则整句什么都没解析出来。
#: 必须**锚在句首**：不锚的话"我想去成都"会把"想去"当成出发地。
_BARE_ORIGIN_DEST_RE = re.compile(
    rf"^\s*({_CN_CITY})\s*(?:去|到|飞往|前往|抵达)\s*({_CN_CITY})\s*{_DEST_TAIL}"
)
#: 只有目的地没有出发地（"去成都玩3天"）—— 出发地缺失仍可规划市内行程，只是不查跨城交通。
_DEST_ONLY_RE = re.compile(rf"(?:去|到|飞往|前往|抵达)\s*({_CN_CITY})\s*{_DEST_TAIL}")
#: 句首候选里出现这些字，说明它在表达意愿（"我想去""打算去"）而不是地名，直接丢掉 ——
#: 宁可这一句解析不出出发地，也不要编一个叫"我想"的城市。
_ORIGIN_VERB_CHARS = ("想", "要", "去", "到", "来", "回", "在")
_ISO_DATE_RE = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})")
_CN_DATE_RE = re.compile(r"(?:^|[^\d])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
#: 中文数字要一起收：coerce_int 认"五""两"，正则不认就会整个丢掉这个字段。
_CN_NUM = r"[\d一二两三四五六七八九十]+"
#: 只认"天/晚"，**不认"日"** —— "10月1日"里的"1日"会冒充行程天数。
_DAYS_RE = re.compile(rf"({_CN_NUM})\s*(?:天|晚)(?!\d)")
_DAYS_TOUR_RE = re.compile(rf"({_CN_NUM})\s*日游")
_TRAVELERS_RE = re.compile(rf"({_CN_NUM})\s*(?:个?人|位|大人|口人)")
#: 「预算6000」不带单位，和「6000元」都要认；「1.5万」要按万元折算，不能只取到 1.5。
_BUDGET_BARE_RE = re.compile(rf"(?:预算|总预算|花费|开支)\s*[¥￥]?\s*([\d,，]+(?:\.\d+)?)\s*([万千百])?")
_BUDGET_UNIT_RE = re.compile(
    rf"([\d,，]+(?:\.\d+)?)\s*([万千百])?\s*(?:元|块钱|块|人民币|RMB)", re.I
)
_BUDGET_RES = (_BUDGET_BARE_RE, _BUDGET_UNIT_RE)
_BUDGET_SCALES = {"万": 10_000, "千": 1_000, "百": 100}


def _budget_from(match: re.Match[str]) -> float | None:
    """「1.5万」→ 15000.0，「3000元」→ 3000.0；取不到数字返回 None。"""
    amount = coerce_float(match.group(1))
    if amount is None:
        return None
    return amount * _BUDGET_SCALES.get(match.group(2) or "", 1)


# ==================================================
# State
# ==================================================


class TravelState(TypedDict, total=False):
    """图的 State。

    带 reducer（`Annotated[..., operator.add]`）的字段是"累加"语义：每个 node 只返回
    自己新增的那几条，LangGraph 负责拼接。其余字段是默认的"覆盖"语义 —— node 返回
    什么就是什么，不返回就不动。
    """

    # --- 输入 ---
    run_id: str
    query: str
    user_id: str
    debug: bool
    output_dir: str
    # 固定流程的可观测钩子：只上报真实节点边界，不参与任何旅行决策。
    progress_hook: Any

    # --- 运行时句柄（不序列化，见模块 docstring） ---
    store: TravelPlanStore
    hub: ProviderHub
    llm: LLM
    jev: JevClient
    #: 子 span 记录口（component 由调用方给）。观测失败不影响规划。
    record_span: Any
    #: 引导式：用户确认过的结构化意图（有它就不再让模型猜一遍）
    intent_override: TripIntent | None
    #: Discovery 已查好的候选（正式 run 复用它，不重复打 Provider）
    prefetch: Any
    #: 从 Discovery 过户到本 run 的调用（审计用；已标 reused）
    adopted_calls: list[dict[str, Any]]
    #: 查询键复用账本（Part C）。**它是一个句柄，不是普通数据**：LangGraph 的 channel
    #: 只在节点返回同名键时才更新，就地改 `state["ledger"]` 不会传到下一个节点
    #: （实测：下一个节点看到的还是 None）。所以必须在初始 state 里传入**同一个对象**，
    #: 各节点只改它内部计数，不替换它 —— 这样"整趟 run 只查一次"与全局计数才成立。
    ledger: Any
    source: str
    source_session_id: str | None

    # --- 解析结果 ---
    intent: TripIntent
    clarify: str
    status: str

    # --- 数据抓取 ---
    queries: list[str]
    transport_plan: TransportPlan
    outbound: FlightOption | TrainOption | None
    inbound: FlightOption | TrainOption | None
    outbound_transfer_minutes: int | None
    inbound_transfer_minutes: int | None
    hotel_plan: HotelPlan
    evidences: list[Evidence]
    places: list[Place]
    routes: dict[Any, RouteOption]
    ticket_prices: dict[str, float]
    amap_status: str

    # --- 打分与排程 ---
    trust_scores: dict[str, float]
    trust_details: dict[str, dict]
    ad_risks: dict[str, float]
    ad_risk_details: dict[str, dict]
    candidate_scores: dict[str, dict]
    days: list[ItineraryDay]
    budget: BudgetSummary
    warnings: list[FeasibilityIssue]
    critic: dict

    # --- Top-K 与 Jev 软决策（接管任务 §4） ---
    plan_candidates: list
    plan_choice: dict
    quality: Any
    tradeoff: dict
    gate: dict
    #: 每次 Jev 调用的可审计记录（管理端 Run Detail 直接渲染）。
    jev_calls: Annotated[list[dict], operator.add]
    #: 本次 run 检测到的 Bad Case（finalize 阶段产生）。
    badcases: list[dict]

    # --- 产物 ---
    plan: TripPlan
    plan_md: str
    outputs: dict[str, str]
    audit: dict

    # --- 审计（累加语义） ---
    decisions: Annotated[list[Decision], operator.add]
    timeline: Annotated[list[dict], operator.add]
    stages: Annotated[list[dict], operator.add]
    degradations: Annotated[list[str], operator.add]


# ==================================================
# 小工具
# ==================================================


def new_run_id() -> str:
    """`tp-` 前缀与前端 mock 数据的 run_id 形状保持一致（`tp-...`）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"tp-{stamp}-{uuid.uuid4().hex[:6]}"


def _stamp(stage: str, text: str) -> dict[str, Any]:
    return {"at": utcnow().isoformat(), "stage": stage, "text": text}


# ==================================================
# 受控并发（Part A）
# ==================================================
# 为什么不是 asyncio：整条链路（ProviderHub / planner / store）都是**同步**的，
# Provider 调用本身是阻塞 IO。为了并发把它们全改成 async 会波及每一个调用点，
# 而收益只是"少几个线程"。ThreadPoolExecutor 在这里是投入产出比最高、也最不容易
# 出错的做法（Discovery 的 prefetch 已经这么做了）。
#
# 三条硬规矩：
#   1. 并发上限**只能**来自 app/config.py，节点里不许自己写死数字；
#   2. 结果按**任务定义顺序**收集（键是显式 label，不是完成顺序），所以同一份输入
#      永远得到同一份输出 —— 并发绝不能让行程变得不可复现；
#   3. 上限=1 时退化成纯串行，测试与"保守部署"都能拿它当开关。


def _run_parallel(
    tasks: Sequence[tuple[str, Callable[[], Any]]],
    *,
    limit: int,
    thread_prefix: str = "tp-parallel",
) -> dict[str, Any]:
    """并发执行互不依赖的取数任务，返回 ``{label: 结果}``（**顺序确定**）。

    单个任务抛异常时不吞掉：等所有任务都收完，再抛**任务定义顺序里第一个**异常 ——
    这等同于串行版本会在哪一步炸，不会因为并发就换一个异常冒出来。异常之前的兄弟任务
    已经跑完并被丢弃，但它们的调用仍然留在 Provider 账本里（可追溯）。
    """

    if not tasks:
        return {}
    workers = max(1, int(limit or 1))
    if workers == 1 or len(tasks) == 1:
        return {label: fn() for label, fn in tasks}

    results: dict[str, Any] = {}
    failure: BaseException | None = None
    with ThreadPoolExecutor(
        max_workers=min(workers, len(tasks)), thread_name_prefix=thread_prefix
    ) as pool:
        futures = [(label, pool.submit(fn)) for label, fn in tasks]
        for label, future in futures:
            try:
                results[label] = future.result()
            except BaseException as exc:  # noqa: BLE001 —— 收完再按顺序抛第一个
                failure = failure or exc
                results[label] = None
    if failure is not None:
        raise failure
    return results


def _first_bad_status(results: Mapping[str, Any], key: str = "status") -> str:
    """按任务顺序取第一个非 OK 状态；全 OK（或没有结果）时返回 "OK"。

    为什么不是"最后一个状态"：串行版本记的就是"第一个出问题的那次调用"，
    并发下必须保持同一个口径，否则降级文案会随线程调度变化。
    """

    for item in results.values():
        status = getattr(item, key, None)
        if status and status != "OK":
            return str(status)
    return "OK"


def _skip_span(
    state: TravelState,
    *,
    name: str,
    parent_stage: str,
    attributes: dict[str, Any],
) -> None:
    """记录一条"**因为不需要而跳过**"的 span（Part D：route_skipped / poi_skipped）。

    跳过与失败是两件事：跳过是"这一步本来就不该做"（用户已排除 / 超过 Planner 上限），
    失败是"做了但没拿到"。混在一起会让"验证覆盖率下降"看起来像 Provider 故障。
    """

    _record_subspan(
        state,
        component=SpanKind.PLANNER,
        name=name,
        status="SKIPPED",
        started_at=now_iso(),
        attributes=attributes,
        parent_span_id=f"{state['run_id']}:{parent_stage}",
    )


#: 让 Jev 比较的候选方案数上限。
#: 给多了摘要变长、判断质量下降（还会挤占它 1.5s 的超时预算），给少了没有比较价值。
MAX_PLAN_CANDIDATES = 5


def _hotel_anchor(state: TravelState) -> Sequence[float] | None:
    """"位置优先/交通方便"要用的中心点：用户所选地点（没选就用全部候选）的经纬度均值。"""

    places = [
        place
        for place in (state.get("places") or [])
        if place.lat is not None and place.lng is not None
    ]
    if not places:
        return None
    return (
        sum(float(place.lat) for place in places) / len(places),
        sum(float(place.lng) for place in places) / len(places),
    )


def _reuse_result(options: list[Any], model: type, provider: str) -> Any:
    """把 Discovery 的候选包装成"与 ProviderResult 同形"的对象。

    为什么包装而不是改下游：下游对 `ProviderResult` 的用法是 `.status/.items/.provider`
    以及 `len(result.items)`。保留这三个字段，比选、文案、stage、决策链全都不用改 ——
    上一版补丁正是因为删掉了这些局部变量，导致 8 处引用连带报错。
    """

    items = [option for option in options if isinstance(option, model)]
    return SimpleNamespace(
        status="REUSED" if items else "EMPTY",
        items=items,
        provider=provider,
        degraded=False,
    )


# ==================================================
# 查询键复用账本与 Provider 兜底超时（Part C / E）
# ==================================================


def _ledger(state: TravelState) -> CallLedger:
    """取本次 run 的查询键账本（没有就现建一个，节点不依赖调用方一定传了）。"""

    ledger = state.get("ledger")
    if ledger is None:
        ledger = CallLedger(scope=str(state.get("run_id") or "run"))
        state["ledger"] = ledger
    return ledger


def _trains_query_key(origin: str, destination: str, day: Any) -> str:
    """12306 火车查询的键（与 `ProviderHub.search_trains` 的实际参数一一对应）。"""

    return query_key(
        "12306", "railway_12306_get-tickets",
        origin=str(origin), destination=str(destination), date=str(day),
    )


def _flights_query_key(origin: str, destination: str, day: Any) -> str:
    """途牛航班查询的键。"""

    return query_key(
        "tuniu", "tuniu_search_flights",
        origin=str(origin), destination=str(destination), date=str(day),
    )


def _hotels_query_key(city: str, check_in: Any, page: int) -> str:
    """途牛酒店查询的键（翻页也是不同的键，第 1 页与第 3 页不是同一个请求）。"""

    return query_key("tuniu", "tuniu_search_hotels", destination=str(city), date=str(check_in), page=page)


def _poi_query_key(keyword: str, region: str, page: int = 1) -> str:
    return query_key("amap", "search_poi", destination=str(region), query=str(keyword), page=page)


def _route_query_key(origin: Sequence[float], destination: Sequence[float], city: str | None) -> str:
    """路线查询的键：坐标 + 城市 + 模式。"""

    return query_key(
        "amap", "route",
        origin=f"{round(float(origin[0]), 4)},{round(float(origin[1]), 4)}",
        destination=f"{round(float(destination[0]), 4)},{round(float(destination[1]), 4)}",
        extra={"city": city or ""},
    )


def _route_with_ledger(
    hub: Any,
    ledger: CallLedger,
    key: str,
    label: str,
    start: Sequence[float],
    end: Sequence[float],
    city: str | None,
) -> Any:
    """查一段市内路线，同一个键在一次 run 内只真的打一次高德（Part C）。

    命中复用记 ``route_cache_hit``，真实发起记 ``route_requested``（发起前已记）——
    管理端因此能直接回答"这几段路线是查出来的还是复用的"。
    """

    value, reused = ledger.fetch(
        key,
        lambda: hub.route(start, end, "transit", city=city),
        detail=f"路线 {label}",
    )
    if reused:
        ledger.mark(key, MARK_ROUTE_CACHE_HIT, label)
    return value


def _ticket_query_key(scenic_name: str) -> str:
    return query_key("tuniu", "tuniu_search_scenic_tickets", query=str(scenic_name))


def _remember_reused_trains(
    state: TravelState,
    ledger: CallLedger,
    origin: str,
    destination: str,
    day: Any,
    options: Sequence[Any],
    direction: str,
) -> None:
    """登记"这一趟火车查询在 Discovery 已经查过"（Part C 的 prefetch_reused 标记）。

    登记之后，同一个键在本次 run 内再被请求时会命中 cache_hit 而不是再打一次 12306。
    """

    key = _trains_query_key(origin, destination, day)
    ledger.remember(
        key,
        _reuse_result(list(options), TrainOption, "12306"),
        marker=MARK_PREFETCH_REUSED,
        detail=f"{direction} 火车复用 Discovery",
    )


def _remember_reused_flights(
    state: TravelState,
    ledger: CallLedger,
    origin: str,
    destination: str,
    day: Any,
    options: Sequence[Any],
    direction: str,
    travelers: int,
) -> None:
    """登记"这一趟航班查询在 Discovery 已经查过"。"""

    key = _flights_query_key(origin, destination, day)
    ledger.remember(key, _reuse_result(list(options), FlightOption, "tuniu"), marker=MARK_PREFETCH_REUSED,
                    detail=f"{direction} 航班复用 Discovery")


def _provider_timeout(provider: str) -> float:
    """这个 Provider 本次的预算（秒）。Part E：每个 Provider 一份，来自 config。"""

    return float(provider_timeouts().get(provider, 60.0))


def _discovery_already_extracted(state: TravelState, bundle: Any) -> bool:
    """这批证据是不是**已经在 Discovery 阶段抽过地点**了（Part F）。

    两个条件同时成立才算：
      1. Discovery 的 place_extraction 那条线已经跑完（不是 RUNNING / PENDING）——
         还在跑的时候它可能只处理了一部分证据，此时复用会丢地点；
      2. 本次 run 手里的证据就是 Discovery 抓到的那一批（按 evidence id 判定）——
         正式 run 自己补查过攻略时，那批新正文没有被抽过，必须抽。

    只有两个条件都成立，才是"同一份内容不做第二遍"，可以直接跳过模型调用。
    """

    if bundle is None:
        return False
    stage = dict(getattr(bundle, "discovery", None) or {}).get("places") or {}
    if str(stage.get("status") or "").upper() in {"", "RUNNING", "PENDING"}:
        return False
    reused_ids = {ev.id for ev in (getattr(bundle, "evidences", None) or [])}
    current_ids = {ev.id for ev in (state.get("evidences") or [])}
    return bool(current_ids) and current_ids <= reused_ids


def _timeout_result(provider: str, tool: str, note: str, status: str = "TIMEOUT") -> Any:
    """超时/失败时的**空结果**（与 ProviderResult 同形）。

    绝不用估算值或旧数据填充：返回空集 + 一条降级说明，让下游如实降级。
    """

    return SimpleNamespace(
        status=status,
        items=[],
        provider=provider,
        error=note,
        calls=[],
        degraded=True,
        tool=tool,
    )


def _bounded_provider_call(
    state: TravelState,
    *,
    provider: str,
    tool: str,
    func: Callable[[], Any],
    timeout: float | None = None,
    status: str = "TIMEOUT",
) -> tuple[Any, str]:
    """给一次阻塞的 Provider 调用加兜底预算，返回 ``(结果, 降级说明)``。

    为什么节点层还要兜一层（ProviderHub 已经按 Provider 配置了超时）：`search_trains`
    这类**复合**调用内部可能连着打两个源，单看某一个 Provider 的预算盖不住它；
    而且一旦 Provider 那边没有按预期收敛（SDK 内部超时失效、MCP 子进程僵住），
    这一层能保证"整趟 run 不会被一个源拖死"。

    超时不会被当成异常抛出，也**不会**产生任何编造的数据：返回空结果 + 一条降级，
    由调用方走自己的 fallback（换源 / 披露 / 规则估算 + verified=False）。
    """

    budget = float(timeout if timeout is not None else _provider_timeout(provider))
    value, timed_out, error = call_with_deadline(func, timeout=budget, label=f"{provider}/{tool}")
    ledger = _ledger(state)
    if timed_out:
        ledger.mark(MARK_PROVIDER_TIMEOUT, MARK_FALLBACK_QUERY, f"{provider}/{tool} 超过 {budget:g}s")
        note = f"{provider} 的 {tool} 超过 {budget:g}s 未返回，本次按超时处理（未用估算值代替）"
        return _timeout_result(provider, tool, note, status=status), note
    if error:
        ledger.mark(MARK_FALLBACK_QUERY, MARK_FALLBACK_QUERY, f"{provider}/{tool} 失败：{error}")
        note = f"{provider} 的 {tool} 调用失败（{error}），本次按不可用处理"
        return _timeout_result(provider, tool, note, status="UNAVAILABLE"), note
    return value, ""


def _record_subspan(
    state: TravelState,
    *,
    component: SpanKind | str,
    name: str,
    status: str,
    started_at: str,
    attributes: dict[str, Any] | None = None,
    parent_span_id: str | None = None,
    error: str | None = None,
    finished_at: str | None = None,
    suffix: str | None = None,
) -> None:
    """写一条业务级子 span（component 分类见 observability.SpanKind）。

    ``suffix`` 用来区分**同名但不同实例**的 span（比如同一个 tool 的第 3 次调用）：
    span_id 是主键，不带 suffix 的重复写入会让后一条覆盖前一条。
    观测永远不能反过来弄坏规划：拿不到 recorder、或写库失败，都只是少一条诊断记录。
    """

    recorder = state.get("record_span")
    if recorder is None:
        return
    try:
        recorder(
            component=str(component),
            name=name,
            status=status,
            started_at=started_at,
            # 结束时刻**必须**由调用方给出或按 duration 推出：Provider / 模型的 span 是
            # 事后统一转录的，用 now_iso() 会让每条子 span 都"跑到 run 结束"
            # （实测出现过每个 Provider 都显示 280~400s 的假象）。
            finished_at=finished_at or now_iso(),
            attributes=attributes or {},
            parent_span_id=parent_span_id,
            error=error,
            suffix=suffix,
        )
    except Exception:
        return


def _span_recorder(state: TravelState) -> Any | None:
    """把 recorder 适配成 app.decision 需要的协议（它只调用，不关心实现）。"""

    recorder = state.get("record_span")
    if recorder is None:
        return None

    def record(
        *,
        component: str,
        name: str,
        status: str,
        started_at: str,
        finished_at: str,
        attributes: dict[str, Any],
        parent_span_id: str | None = None,
        error: str | None = None,
    ) -> None:
        try:
            recorder(
                component=component,
                name=name,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                attributes=attributes,
                parent_span_id=parent_span_id,
                error=error,
            )
        except Exception:
            return

    return record


def _stage(stage_id: str, title: str, summary: str, steps: list[str], **stats: Any) -> dict:
    return {
        "id": stage_id,
        "title": title,
        "summary": summary,
        "steps": [step for step in steps if step],
        "stats": [{"label": key, "value": str(value)} for key, value in stats.items()],
    }


def _decision(
    entity_id: str,
    status: DecisionStatus,
    stage: str,
    codes: list[str],
    reason: str,
    scores: dict[str, Any] | None = None,
) -> Decision:
    return Decision(
        entity_id=entity_id,
        status=status,
        agent_or_stage=stage,
        reason_codes=codes,
        reason_text=reason,
        scores=scores or {},
    )


def _clock(minutes: int | None) -> str:
    if minutes is None:
        return "未知"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _day_label(intent: TripIntent, index: int) -> str:
    """第 index 天对应的日期；没有出发日期就只说"第 N 天"。"""
    if intent.start_date is None:
        return f"第 {index + 1} 天"
    return (intent.start_date + timedelta(days=index)).isoformat()


def _transit_span_issues(
    intent: TripIntent,
    outbound: FlightOption | TrainOption | None,
    arrival_index: int,
) -> list[FeasibilityIssue]:
    """去程占用整天数时如实告警。

    这一类问题**不是时间冲突**，是"天数被交通吃掉"：用户说"玩 6 天"，但 46 小时的火车
    会先吃掉两天。系统不替用户改天数、也不悄悄挪日期，只把"实际可玩几天"写出来。
    """
    if outbound is None:
        # 与 `_inbound_span_issues` 的 INBOUND_UNAVAILABLE 对称：去程一个候选都没有
        # （实测「广州→重庆」12306 与途牛航班同时 TIMEOUT）时，行程会按"人已经在
        # 目的地"排 —— 首日 08:30 就开始逛，而这件事原先只写在 audit.degradations 里，
        # plan.warnings 一条不提。用户看不出"这趟行程没有去程"。
        destination = _destination(intent) or "目的地"
        return [
            FeasibilityIssue(
                day_index=0,
                severity="warning",
                code="OUTBOUND_UNAVAILABLE",
                reason=(
                    "本次没有选中任何去程方案（没有查到可用车次或航班），"
                    f"行程按「人已经在 {destination}」排：第 1 天（{_day_label(intent, 0)}）"
                    f"不受抵达时间约束，请自行安排前往 {destination} 的交通 —— "
                    "系统不替这次空缺编一趟车"
                ),
            )
        ]
    if outbound.arrival_at is None or intent.start_date is None:
        return []
    day_count = max(1, intent.days)
    if planner.arrival_beyond_trip(outbound, intent.start_date, day_count):
        return [
            FeasibilityIssue(
                day_index=day_count - 1,
                severity="error",
                code="ARRIVAL_AFTER_TRIP_END",
                reason=(
                    f"去程要到 {outbound.arrival_at.strftime('%Y-%m-%d %H:%M')} 才抵达目的地，"
                    f"已经晚于行程最后一天（{_day_label(intent, day_count - 1)}）："
                    "这趟行程没有可游玩的时间，建议改交通方式或改期"
                ),
            )
        ]
    if arrival_index <= 0:
        return []
    usable = day_count - arrival_index
    return [
        FeasibilityIssue(
            day_index=arrival_index,
            severity="warning",
            code="OUTBOUND_MULTI_DAY",
            reason=(
                f"去程跨 {arrival_index + 1} 天（{outbound.departure_at.strftime('%m-%d %H:%M') if outbound.departure_at else '?'} 出发 → "
                f"{outbound.arrival_at.strftime('%m-%d %H:%M')} 抵达）：第 1~{arrival_index} 天在途，"
                f"原定 {day_count} 天里实际可游玩 {usable} 天，第 {arrival_index + 1} 天"
                f"（{_day_label(intent, arrival_index)}）才开始排行程"
            ),
        )
    ]


def _off_date_issues(
    intent: TripIntent,
    outbound: FlightOption | TrainOption | None,
    inbound: FlightOption | TrainOption | None,
) -> list[FeasibilityIssue]:
    """交通方案的实际发车日与行程约定日期不符时，如实告警而不是替它改日期。

    实测 12306 查 2026-10-02 会返回 09-30 / 10-01 的车次。这时候有三种做法，只有一种是诚实的：

    - 把车次的日期改成 10-02 → 编造数据，最坏；
    - 默默采用、什么都不说 → 用户拿到一份"10-02 出发"但实际排的是 10-01 车次的行程；
    - **保留真实车次 + 明说日期不符** ← 这里选的。
    """
    issues: list[FeasibilityIssue] = []
    pairs = (
        ("去程", "OUTBOUND_OFF_DATE", outbound, intent.start_date, 0),
        ("回程", "INBOUND_OFF_DATE", inbound, _last_date(intent), max(0, intent.days - 1)),
    )
    for label, code, option, expected, day_index in pairs:
        offset = planner.departure_day_offset(option, expected)
        if not offset or expected is None:
            continue
        departure = option.departure_at
        issues.append(
            FeasibilityIssue(
                day_index=day_index,
                severity="warning",
                code=code,
                reason=(
                    f"{label}交通方案的发车日是 {departure.strftime('%Y-%m-%d %H:%M')}，"
                    f"与行程约定的 {expected.isoformat()} 相差 {offset:+d} 天。"
                    f"所有候选都不在约定日期，这是数据源（{option.provider}）返回的日期，"
                    "系统没有改写它；行程按该方案的真实日期排，请自行确认是否仍要这趟车"
                ),
            )
        )
    return issues


def _last_date(intent: TripIntent) -> date | None:
    """行程最后一天。优先用用户给的结束日期，否则按天数推。"""
    if intent.end_date is not None:
        return intent.end_date
    if intent.start_date is None:
        return None
    return intent.start_date + timedelta(days=max(1, intent.days) - 1)


def _inbound_span_issues(
    intent: TripIntent,
    inbound: FlightOption | TrainOption | None,
) -> list[FeasibilityIssue]:
    """返程"到不了家"或"要坐两天"时如实告警。

    两件必须说清的事（都不改数据，只写出来）：

    * **没有回程方案**：候选为空（两个数据源都不可用）时，行程里不会有回家的那一段。
      不说的话，用户看到一份 3 天行程、最后一天照常逛街，以为系统会把他送回去。
    * **回程跨天**：实测「长春→成都」只有一趟 K546，10-06 07:58 开、10-08 06:45 到，
      48 小时的火车。行程表只排到 10-06，用户在车上过的 10-07 / 10-08 两天
      在计划里没有任何交代。
    """
    last_day_index = max(0, intent.days - 1)
    if inbound is None:
        return [
            FeasibilityIssue(
                day_index=last_day_index,
                severity="warning",
                code="INBOUND_UNAVAILABLE",
                reason=(
                    "本次没有选中任何回程方案（没有查到可用车次或航班），"
                    f"行程里不会有回家的那一段：请自行安排 {_day_label(intent, last_day_index)} "
                    "之后的返程，系统不替这次空缺编一趟车"
                ),
            )
        ]
    arrival = inbound.arrival_at
    last_date = _last_date(intent)
    if arrival is None or last_date is None or arrival.date() <= last_date:
        return []
    extra_days = (arrival.date() - last_date).days
    # 跨几天按**自然日**算，与去程 `_transit_span_issues` 的口径一致：
    # 10-06 07:58 开、10-08 06:45 到是"跨 3 天"，不是"跨 2 天 + 1 天"。
    departure = inbound.departure_at
    span = (arrival.date() - departure.date()).days + 1 if departure is not None else extra_days + 1
    return [
        FeasibilityIssue(
            day_index=last_day_index,
            severity="warning",
            code="INBOUND_MULTI_DAY",
            reason=(
                f"回程跨 {span} 天"
                f"（{departure.strftime('%m-%d %H:%M') if departure else '?'} 出发 → "
                f"{arrival.strftime('%m-%d %H:%M')} 抵达），"
                f"要 {arrival.strftime('%Y-%m-%d')} 才到家，已经晚于行程最后一天"
                f"（{last_date.isoformat()}）：行程表只排到 {last_date.isoformat()}，"
                f"其后 {extra_days} 天在车上，请自行确认这个耗时可接受"
            ),
        )
    ]


def _hotel_issues(intent: TripIntent, hotel: HotelOption | None) -> list[FeasibilityIssue]:
    """没有住宿候选时如实告警。

    实测「广州→重庆」途牛酒店 UNAVAILABLE，4 天行程里一晚住宿都没有、
    `budget.住宿 = 0`，而 `projected_total` 还显示"预算内"。降级记录写在
    audit.degradations 里（前端只汇总成"有 N 个数据源为降级返回"），
    `plan.warnings` 一条不提 —— 用户看不出这趟行程没地方睡。
    """
    if hotel is not None:
        return []
    nights = max(1, intent.days) - 1
    if nights <= 0:
        # 当天往返本来就不需要住宿，不该报"缺住宿"。
        return []
    return [
        FeasibilityIssue(
            day_index=0,
            severity="warning",
            code="HOTEL_UNAVAILABLE",
            reason=(
                f"本次没有查到可用的住宿候选，{nights} 晚住宿在行程里是空缺的："
                f"住宿费用没有计入预算（也没有用估算价代替），"
                f"请自行安排 {_day_label(intent, 0)} 起 {nights} 晚的住宿"
            ),
        )
    ]


def _destination(intent: TripIntent) -> str | None:
    return intent.destination[0] if intent.destination else None


def _evidences_for(all_evidences: list[Evidence], place: Place) -> list[Evidence]:
    """按名字/别名把证据挂到地点上。

    这就是 `place_evidence` 表的来源 —— Trust 的多来源分项完全依赖它，所以匹配规则
    要保守：只有名字命中（归一化后相等，或名字出现在证据的 place_mentions 里）才建立关联，
    不做模糊相似度，否则"因为都提到成都所以这条帖子也算武侯祠的证据"会虚高 Trust。
    """
    keys = {planner.normalize_place_name(place.name, city=place.city), place.normalized_name}
    keys |= {planner.normalize_place_name(alias, city=place.city) for alias in place.aliases}
    keys.discard("")
    matched: list[Evidence] = []
    for evidence in all_evidences:
        mentions = {planner.normalize_place_name(name, city=place.city) for name in evidence.place_mentions}
        mentions.discard("")
        if keys & mentions:
            matched.append(evidence)
            continue
        haystack = planner.normalize_place_name(evidence.title, city=place.city)
        if any(key and key in haystack for key in keys):
            matched.append(evidence)
    return matched


def _link_evidence(all_evidences: list[Evidence], places: list[Place]) -> dict[str, list[Evidence]]:
    return {place.place_id: _evidences_for(all_evidences, place) for place in places}


def _place_brief(place: Place) -> str:
    bits = [place.name]
    if place.district:
        bits.append(place.district)
    elif place.business_area:
        bits.append(place.business_area)
    return " · ".join(bits)


# ==================================================
# 规则兜底：模型不可用时的意图解析
# ==================================================


def _rule_based_intent(query: str) -> TripIntent:
    """只认识"从X到Y"、明确日期、"N天"、"N人"、"N元"。

    刻意做得很窄：宁可少认几个字段，也不要猜。认不出来的一律留空，由调用方
    如实告诉用户"这次没能解析出目的地"，而不是编一个城市继续往下跑。
    """
    text = coerce_str(query)
    origin: str | None = None
    destination: str | None = None
    match = _ORIGIN_DEST_RE.search(text)
    if match:
        origin = match.group(1).strip()
        destination = match.group(2).strip()
    else:
        bare = _BARE_ORIGIN_DEST_RE.search(text)
        if bare and not bare.group(1).endswith(_ORIGIN_VERB_CHARS):
            origin = bare.group(1).strip()
            destination = bare.group(2).strip()
        else:
            # 没说出发地（"去成都玩3天"），或句首那个词是意愿不是地名（"我想去成都"）：
            # 出发地留空（本次不查跨城大交通），但目的地照收 —— 市内行程照样能排。
            only = _DEST_ONLY_RE.search(text)
            if only:
                destination = only.group(1).strip()

    start_date: date | None = None
    end_date: date | None = None
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            start_date = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            start_date = None
    if start_date is None:
        cn = _CN_DATE_RE.search(text)
        if cn:
            try:
                start_date = date(date.today().year, int(cn.group(1)), int(cn.group(2)))
            except ValueError:
                start_date = None

    days_match = _DAYS_RE.search(text) or _DAYS_TOUR_RE.search(text)
    days = coerce_int(days_match.group(1)) if days_match else None
    travelers_match = _TRAVELERS_RE.search(text)
    travelers = coerce_int(travelers_match.group(1)) if travelers_match else None
    budget = None
    for pattern in _BUDGET_RES:
        match = pattern.search(text)
        if match:
            budget = _budget_from(match)
            break

    if days is None and start_date is not None:
        # 用户给了出发日但没说玩几天；捞不到天数就只给 1 天，由 workflow 如实提示。
        days = 1

    return TripIntent(
        destination=[destination] if destination else [],
        origin=origin,
        start_date=start_date,
        end_date=end_date,
        days=max(1, days or 1),
        travelers=max(1, travelers or 1),
        budget_total=budget,
        transport_preferences=["火车"] if "高铁" in text or "火车" in text else (
            ["飞机"] if "飞机" in text or "航班" in text else []
        ),
    )


# ==================================================
# PRD §24：大交通比选
# ==================================================


def _door_to_door_minutes(option: Any) -> int | None:
    """门到门总时长 = 提前到站/机场 + 在途 + 出站 + 机场车站→市区接驳。

    "飞行 2 小时"不是用户付出的时间成本 —— PRD §24 要求比门到门，这个函数就是那个口径。
    """
    duration = coerce_int(getattr(option, "duration_minutes", None))
    if duration is None:
        return None
    exit_buffer = (
        planner.AIRPORT_BAGGAGE_EXIT_MINUTES
        if isinstance(option, FlightOption)
        else planner.TRAIN_EXIT_BUFFER_MINUTES
    )
    transfer = coerce_int(getattr(option, "airport_transfer_minutes", None))
    if transfer is None:
        transfer = planner.transfer_buffer_minutes(option)
    return duration + planner.boarding_buffer_minutes(option) + exit_buffer + transfer


def _transport_preference_bonus(option: Any, intent: TripIntent) -> float:
    prefs = " ".join(coerce_str_list(intent.transport_preferences))
    if not prefs:
        return 0.0
    is_flight = isinstance(option, FlightOption)
    if is_flight and any(word in prefs for word in ("飞机", "航班", "飞往", "飞")):
        return TRANSPORT_PREFERENCE_BONUS
    if not is_flight and any(word in prefs for word in ("火车", "高铁", "动车", "铁路")):
        return TRANSPORT_PREFERENCE_BONUS
    return 0.0


def _expected_date(direction: str, intent: TripIntent) -> date | None:
    """这个方向的候选"应该"在哪天发车：去程看第一天，回程看最后一天。"""
    return intent.start_date if direction == "outbound" else _last_date(intent)


def _transport_score(option: Any, *, direction: str, intent: TripIntent) -> tuple[float, dict]:
    """比选得分（越低越好）。分项全部保留，审计能复述"为什么是它"。

    用户在向导里选的排序策略（价格最低 / 时间最短 / 舒适优先 / 性价比）通过
    `selection.transport_weights` 变成这里的倍率与惩罚项。默认（帮我选）倍率是 1.0、
    惩罚项是 0，因此**不选偏好时结果与历史完全一致**。
    """
    tuning_now = current_tuning()
    strategy = selection.transport_weights(intent).values
    parts: dict[str, float] = {}

    price = coerce_float(getattr(option, "price", None))
    parts["价格"] = (
        TRANSPORT_UNKNOWN_PRICE_PENALTY
        if price is None
        else price / TRANSPORT_PRICE_UNIT * float(strategy.get("price", tuning_now.transport_price_weight))
    )

    door_to_door = _door_to_door_minutes(option)
    parts["门到门时长"] = (
        TRANSPORT_UNKNOWN_DURATION_PENALTY
        if door_to_door is None
        else door_to_door / TRANSPORT_MINUTE_UNIT * float(strategy.get("duration", tuning_now.transport_duration_weight))
    )

    offset = planner.departure_day_offset(option, _expected_date(direction, intent))
    parts["日期一致"] = 0.0 if not offset else TRANSPORT_OFF_DATE_PENALTY * abs(offset)

    penalty = 0.0
    if direction == "outbound":
        ready = planner.arrival_ready_minutes(option)
        if ready is not None and ready >= TRANSPORT_LATE_ARRIVAL_MINUTES:
            penalty = TRANSPORT_LATE_ARRIVAL_PENALTY
    else:
        deadline = planner.departure_deadline_minutes(option)
        if deadline is not None and deadline <= TRANSPORT_EARLY_DEPARTURE_MINUTES:
            penalty = TRANSPORT_EARLY_DEPARTURE_PENALTY
    parts["首末天影响"] = penalty

    parts["偏好"] = -_transport_preference_bonus(option, intent) * float(strategy.get("preference", 1.0))

    # --- 用户在向导里点的附加约束：变成真实惩罚（不选就是 0，行为不变）---
    mode_bonus = float(strategy.get("mode_bonus") or 0.0)
    wanted_mode = str(strategy.get("mode") or "")
    if mode_bonus and wanted_mode:
        actual_mode = "flight" if isinstance(option, FlightOption) else "train"
        parts["方式匹配"] = 0.0 if actual_mode == wanted_mode else mode_bonus

    departure = getattr(option, "departure_at", None)
    arrival = getattr(option, "arrival_at", None)
    early_penalty = float(strategy.get("early_penalty") or 0.0)
    if early_penalty and departure is not None and departure.hour < TRANSPORT_EARLY_DEPARTURE_MINUTES // 60:
        parts["出发过早"] = early_penalty

    red_eye_penalty = float(strategy.get("red_eye_penalty") or 0.0)
    if red_eye_penalty and (
        (departure is not None and (departure.hour >= 23 or departure.hour < 5))
        or (arrival is not None and (arrival.hour >= 23 or arrival.hour < 5))
    ):
        parts["红眼班次"] = red_eye_penalty

    transfer_penalty = float(strategy.get("transfer_penalty") or 0.0)
    if transfer_penalty:
        is_direct = getattr(option, "is_direct", None)
        raw = getattr(option, "raw", None) or {}
        transfers = raw.get("transfer_count") if isinstance(raw, dict) else None
        if is_direct is False or (isinstance(transfers, int) and transfers > 0):
            parts["需要换乘"] = transfer_penalty

    return round(sum(parts.values()), 2), {key: round(value, 2) for key, value in parts.items()}


def _select_transport(
    options: list[Any], *, direction: str, intent: TripIntent
) -> tuple[Any | None, list[Any], str, dict[str, dict]]:
    """从候选里选一个主方案，并给出人话理由。返回 (selected, alternatives, reason, scores)。"""
    usable = [option for option in options if getattr(option, "duration_minutes", None) is not None]
    if not usable:
        usable = list(options)
    if not usable:
        return None, [], "", {}

    scored: list[tuple[float, dict, Any]] = []
    for option in usable:
        score, parts = _transport_score(option, direction=direction, intent=intent)
        scored.append((score, parts, option))
    scored.sort(key=lambda item: (item[0], coerce_str(getattr(item[2], "label", ""))))

    _, _, selected = scored[0]
    alternatives = [option for _, _, option in scored[1 : 1 + MAX_TRANSPORT_ALTERNATIVES]]
    scores = {coerce_str(getattr(option, "label", "")) or f"option-{index}": parts for index, (_, parts, option) in enumerate(scored)}

    label = coerce_str(getattr(selected, "label", "")) or "未知方案"
    door_to_door = _door_to_door_minutes(selected)
    direction_cn = "去程" if direction == "outbound" else "回程"
    bits = [f"{direction_cn}选中 {label}"]
    if door_to_door is not None:
        bits.append(f"门到门约 {door_to_door // 60} 小时 {door_to_door % 60} 分")
    if getattr(selected, "price", None) is not None:
        bits.append(f"价格 ¥{selected.price:g}（{selected.currency}）")
    else:
        bits.append("价格未返回")
    bits.append(f"在 {len(usable)} 个候选里综合分最低")
    offset = planner.departure_day_offset(selected, _expected_date(direction, intent))
    if offset:
        departure = getattr(selected, "departure_at", None)
        expected = _expected_date(direction, intent)
        bits.append(
            f"⚠️ 该方案的发车日是"
            f"{departure.strftime('%Y-%m-%d') if departure else '未知'}，与行程约定的"
            f"{expected.isoformat() if expected else '未知'}相差 {offset:+d} 天 —— "
            f"{len(usable)} 个候选里没有一个落在约定日期，这是数据源返回的日期，"
            "我们没有替它改；行程按该方案的实际日期排"
        )
    if direction == "outbound":
        ready = planner.arrival_ready_minutes(selected)
        if ready is not None:
            bits.append(f"若按此方案，抵达当天最早 {_clock(ready)} 才能开始行程")
    else:
        deadline = planner.departure_deadline_minutes(selected)
        if deadline is not None:
            bits.append(f"最后一天最晚 {_clock(deadline)} 必须结束市内活动")

    if alternatives:
        runner_up = alternatives[0]
        runner_label = coerce_str(getattr(runner_up, "label", "")) or "次选"
        runner_door = _door_to_door_minutes(runner_up)
        reason_bits = []
        if door_to_door is not None and runner_door is not None:
            delta = runner_door - door_to_door
            reason_bits.append(
                f"门到门{'多' if delta > 0 else '少'} {abs(delta) // 60} 小时 {abs(delta) % 60} 分"
            )
        if getattr(selected, "price", None) is not None and getattr(runner_up, "price", None) is not None:
            delta_price = runner_up.price - selected.price
            if delta_price:
                reason_bits.append(f"价格{'高' if delta_price > 0 else '低'} ¥{abs(delta_price):g}")
        if reason_bits:
            bits.append(f"次选 {runner_label} 的差距：{'、'.join(reason_bits)}")

    selected.selection_reason = "；".join(bits) + "（注：门到门时长中的机场/车站接驳按具名常量估算，未取到真实路线时以常量计）"
    for option in alternatives:
        option.selection_reason = f"{direction_cn}未选中：综合分 {_transport_score(option, direction=direction, intent=intent)[0]:g} 高于 {label}"

    return selected, alternatives, selected.selection_reason, scores


def _annotate_transfer_minutes(option: Any, minutes: int | None) -> None:
    """把高德真实接驳时长写回方案（PRD §24 的"机场/车站额外接驳"）。"""
    if option is None or minutes is None:
        return
    option.airport_transfer_minutes = int(minutes)
    door_to_door = _door_to_door_minutes(option)
    if door_to_door is not None:
        option.door_to_door_minutes = door_to_door


# ==================================================
# 住宿比选
# ==================================================


def _hotel_score(
    hotel: HotelOption,
    intent: TripIntent,
    *,
    anchor: Sequence[float] | None = None,
) -> tuple[float, dict]:
    """酒店比选得分（越低越好）。策略与补充条件通过 `selection.hotel_weights` 生效。

    `anchor` 是用户所选地点（或酒店候选群）的中心点：只有选了「位置优先」「交通方便」
    或维护者调大了 `TP_HOTEL_LOCATION_WEIGHT` 时，距离才会进入打分（默认权重 0，
    因此不选偏好时与历史行为一致）。
    """

    tuning_now = current_tuning()
    strategy = selection.hotel_weights(intent).values
    parts: dict[str, float] = {}
    nightly = coerce_float(hotel.nightly)
    parts["每晚价格"] = (
        HOTEL_UNKNOWN_PRICE_PENALTY
        if nightly is None
        else nightly * float(strategy.get("price", tuning_now.hotel_price_weight))
    )
    rating = coerce_float(hotel.rating)
    parts["评分"] = -(rating or 0.0) * float(strategy.get("rating", tuning_now.hotel_rating_weight))
    prefs = coerce_str_list(intent.hotel_preferences)
    haystack = " ".join(
        filter(None, [hotel.name, hotel.room_type, hotel.business_area, hotel.address, hotel.price_note])
    )
    hits = [pref for pref in prefs if pref and pref in haystack]
    parts["住宿偏好"] = -float(strategy.get("preference", tuning_now.hotel_preference_bonus)) * len(hits)

    location_weight = float(strategy.get("location") or 0.0)
    if location_weight and anchor is not None and hotel.lat is not None and hotel.lng is not None:
        distance = planner.haversine_meters((hotel.lat, hotel.lng), anchor)
        if distance is not None:
            # 离所选地点中心越远扣分越多；超出 RANGE 记满额惩罚。
            ratio = min(1.0, distance / 1000.0 / max(0.1, tuning_now.hotel_location_range_km))
            parts["位置"] = location_weight * ratio
    return round(sum(parts.values()), 2), {**{k: round(v, 2) for k, v in parts.items()}, "命中偏好": len(hits)}


def _select_hotel(
    options: list[HotelOption],
    intent: TripIntent,
    *,
    anchor: Sequence[float] | None = None,
) -> tuple[HotelOption | None, list[HotelOption], str]:
    if not options:
        return None, [], ""
    # 先按用户补充条件硬过滤（价格上限/评分下限/房型），过滤空了就放宽并说明。
    filtered, filter_note = selection.hotel_candidates_for(intent, options)
    scored: list[tuple[float, dict, HotelOption]] = []
    for hotel in filtered:
        score, parts = _hotel_score(hotel, intent, anchor=anchor)
        scored.append((score, parts, hotel))
    scored.sort(key=lambda item: (item[0], item[2].name))

    _, _, selected = scored[0]
    alternatives = [hotel for _, _, hotel in scored[1:4]]
    nightly = coerce_float(selected.nightly)
    bits = [f"选中「{selected.name}」"]
    if nightly is not None:
        bits.append(f"每晚 ¥{nightly:g}（{selected.price_note or '起价'}）")
    else:
        bits.append("途牛未返回价格，预算里不计住宿费用")
    if selected.rating is not None:
        bits.append(f"评分 {selected.rating:g}")
    if selected.business_area:
        bits.append(f"位置在{selected.business_area}")
    bits.append(f"在 {len(options)} 个候选里综合分最低")
    prefs = coerce_str_list(intent.hotel_preferences)
    if prefs:
        bits.append(f"住宿偏好「{'/'.join(prefs)}」")
    if filter_note:
        bits.append(filter_note)
    bits.append(selection.hotel_weights(intent).label)
    reason = "；".join(bits)
    selected.selection_reason = reason
    for hotel in alternatives:
        hotel.selection_reason = (
            f"未选中：综合分 {_hotel_score(hotel, intent, anchor=anchor)[0]:g} 高于「{selected.name}」"
        )
    return selected, alternatives, reason


# ==================================================
# Node 1：parse_intent
# ==================================================


#: 模型 JSON 里同一个字段可能出现的各种写法（与 models.TripIntent.from_llm 的别名保持一致）。
_INTENT_KEYS: dict[str, tuple[str, ...]] = {
    "origin": ("origin", "origin_city", "from", "from_city", "departure_city", "出发地"),
    "destination": (
        "destination", "destination_city", "destinations", "city", "cities", "dest", "to", "目的地",
    ),
    "start_date": ("start_date", "startDate", "start", "departure_date", "出发日期"),
    "days": ("days", "day_count", "duration", "天数"),
    "travelers": ("travelers", "people", "persons", "pax"),
    "budget_total": ("budget_total", "budget", "budgetTotal", "预算"),
}


def _raw_has(raw: dict, field: str) -> bool:
    """模型这次**到底给没给**这个字段（给了空值不算给）。"""
    return any(raw.get(key) not in (None, "", [], {}) for key in _INTENT_KEYS[field])


def _backfill_intent(intent: TripIntent, raw: dict, rules: TripIntent) -> tuple[TripIntent, list[str]]:
    """用规则解析补上模型漏掉的字段，返回 (意图, 补了哪些字段)。

    为什么必须有这一步：模型给出的 JSON 可能语法正确但**丢字段** —— 实测中它把目的地
    写成了 `destination_city` 之外的拼法，解析后目的地为空，整条流程就报「没能确定
    目的地城市」并停下；可用户明明说了「从北京去成都」。把一句用户已经说清楚的话
    变成一句追问，是产品层面的错误，不是模型的小毛病。

    规则解析认得的东西是确定性的，所以判据是「模型没给 → 用规则的」，模型给了就一律
    尊重模型（它更擅长"国庆""下个月第一个周末"这类模糊时间）。
    """
    updates: dict[str, Any] = {}
    filled: list[str] = []
    for field, label, use_rule in (
        ("destination", "目的地", bool(rules.destination)),
        ("origin", "出发地", bool(rules.origin)),
        ("start_date", "出发日期", rules.start_date is not None),
        ("days", "天数", bool(rules.days)),
        ("travelers", "人数", bool(rules.travelers)),
        ("budget_total", "预算", rules.budget_total is not None),
    ):
        if use_rule and not _raw_has(raw, field):
            value = getattr(rules, field)
            updates[field] = list(value) if field == "destination" else value
            filled.append(label)

    if not updates:
        return intent, []
    return intent.model_copy(update=updates), filled


def node_parse_intent(state: TravelState) -> dict:
    run_id = state["run_id"]
    query = state["query"]
    llm = state["llm"]
    store = state["store"]

    # 引导式：用户已经逐项确认过出发地/目的地/日期/人数/偏好，直接采用。
    # 让模型再猜一遍用户刚点过的按钮，等于把确定性信息降级成概率信息
    # （用户旅程落地任务 §12①：结构化数据的优先级高于自然语言解析）。
    override = state.get("intent_override")
    if override is not None:
        intent = override
        pace, pace_reason = selection.resolve_pace_with_reason(intent)
        intent.pace = pace
        store.save_trip_request(run_id, query, intent)
        summary = (
            f"引导式：直接采用用户确认的结构化信息"
            f"（来源会话 {state.get('source_session_id') or '—'}）；{pace_reason}"
        )
        return {
            "intent": intent,
            "decisions": [
                _decision(run_id, DecisionStatus.PASS, "parse_intent",
                          ["GUIDED_INTENT", "USER_CONFIRMED"], summary,
                          {"source": "guided", "pace": pace})
            ],
            "timeline": [_stamp("parse_intent", summary)],
            "stages": [
                _stage("parse_intent", "理解需求", summary, [
                    f"目的地：{'、'.join(intent.destination) or '未指定'}",
                    f"日期：{intent.start_date.isoformat() if intent.start_date else '未指定'}，"
                    f"{intent.days} 天，{intent.travelers} 人",
                    pace_reason,
                ], 来源="引导式")
            ],
        }

    degradations: list[str] = []
    rules = _rule_based_intent(query)
    backfilled: list[str] = []
    result = llm.invoke_json(INTENT_PARSE_PROMPT, query, tag="parse_intent")
    if result.ok and isinstance(result.value, dict):
        intent = TripIntent.from_llm(result.value)
        intent, backfilled = _backfill_intent(intent, result.value, rules)
        source = f"模型解析（{result.model}）"
    else:
        intent = rules
        degradations.append(degraded_note(result, "意图解析退化为规则匹配，可能漏掉日期/人数/预算"))
        source = "规则兜底（模型输出不可用）"

    store.save_trip_request(run_id, query, intent)

    destination = _destination(intent)
    clarify = ""
    status = ""
    if not destination:
        status = STATUS_NEEDS_CLARIFICATION
        clarify = (
            "没能确定目的地城市，无法开始查询。请补充「从哪出发、去哪、什么时候去、玩几天」，"
            "例如「10 月 1 日从北京去成都，5 天，2 个人，预算 8000」。"
        )
    elif intent.start_date is None:
        degradations.append("用户没有给出出发日期，大交通与酒店的实时查询需要日期，本次未查询这两项")

    days = f"{intent.start_date.isoformat()} 起 {intent.days} 天" if intent.start_date else f"{intent.days} 天（日期未定）"
    steps = [
        f"解析来源：{source}",
        f"出发地：{intent.origin or '未提供（将跳过跨城大交通查询）'}",
        f"目的地：{'、'.join(intent.destination) or '未识别'}",
        f"时间：{days}，{intent.travelers} 人",
        f"预算：{'¥%g' % intent.budget_total if intent.budget_total else '未提供'}",
    ]
    if backfilled:
        steps.append(
            f"模型未给出{'、'.join(backfilled)}，已由规则解析（「从X去Y」/日期/天数/人数/预算）补回"
        )
    if intent.preferences:
        steps.append(f"偏好：{'、'.join(intent.preferences)}")
    if intent.transport_preferences:
        steps.append(f"交通偏好：{'、'.join(intent.transport_preferences)}")
    if intent.hotel_preferences:
        steps.append(f"住宿偏好：{'、'.join(intent.hotel_preferences)}")

    return {
        "intent": intent,
        "status": status,
        "clarify": clarify,
        "degradations": degradations,
        "timeline": [_stamp("parse_intent", f"{source} → 目的地 {destination or '未识别'}，{days}，{intent.travelers} 人")],
        "stages": [
            _stage(
                "parse_intent",
                "理解需求",
                f"{source}：{'、'.join(intent.destination) or '未识别目的地'}，{days}，{intent.travelers} 人",
                steps,
                状态=status or "已解析",
            )
        ],
    }


def _after_intent(state: TravelState) -> str:
    return "stop" if state.get("status") == STATUS_NEEDS_CLARIFICATION else "continue"


# ==================================================
# Node 2：search_intercity_transport
# ==================================================


def node_search_transport(state: TravelState) -> dict:
    hub = state["hub"]
    intent = state["intent"]
    origin = intent.origin
    destination = _destination(intent)

    provider_status: dict[str, str] = {}
    if not (origin and destination and intent.start_date):
        missing = [
            name
            for name, value in (("出发地", origin), ("目的地", destination), ("出发日期", intent.start_date))
            if not value
        ]
        reason = f"缺少{'、'.join(missing)}，本次没有查询跨城大交通（没有用估算时刻代替）"
        plan = TransportPlan(provider_status={"train": "SKIPPED", "flight": "SKIPPED"}, selection_reason=reason)
        return {
            "transport_plan": plan,
            "outbound": None,
            "inbound": None,
            "degradations": [reason],
            "timeline": [_stamp("search_intercity_transport", reason)],
            "stages": [_stage("search_intercity_transport", "查大交通", reason, [reason], 候选数=0)],
        }

    start = intent.start_date
    back = _last_date(intent)
    travelers = max(1, intent.travelers)
    # 并发上限来自 config（Part A）：绝不在节点里写死数字，也绝不无限并发打 Provider。
    cfg = current_config()
    ledger = _ledger(state)
    degradations: list[str] = []
    # 一次 `search_trains` 内部可能先打 12306、失败再打途牛，所以它的兜底预算必须
    # 覆盖两个源；其余（航班）只有途牛一个源。
    transport_budget = _provider_timeout("12306") + _provider_timeout("tuniu")

    # 引导式把 Discovery 已经查到的候选带进来了。复用时**不重复打 12306/途牛**，
    # 但仍然按用户最终选的排序策略重新比选（偏好可能是在 Prefetch 之后才定的）。
    #
    # 复用必须**按方向**判定，不能"只要有一个方向有数据就整体复用"：Discovery 很可能
    # 只跑完了去程（回程那条线还在 RUNNING，或用户本来就没定返程日期）。上一版用
    # `bundle.outbound or bundle.inbound` 一刀切，结果是"去程有数据 → 回程也当成复用"，
    # 回程候选被静默置空 —— 用户看到的是"回程没票"，真相是"回程没查"。
    # 少查一个方向是 bug，不是优化，所以这里逐方向判断、缺哪个查哪个。
    bundle = state.get("prefetch")
    has_back = back is not None and back >= start
    reuse_out = bool(bundle is not None and bundle.outbound)
    reuse_in = bool(bundle is not None and has_back and bundle.inbound)
    # 12306 是火车主源，但单次实测 55s 级；开启对冲后，主源还在跑时就并发发一次途牛火车
    # 当备胎，避免"主源慢 → 傻等 90s → 再串行等途牛"把整段交通拖到 3 分钟。
    hedge = bool(cfg.transport_hedge_enabled)

    # 降级文案按**任务定义顺序**收集：谁先超时不影响用户看到的那条说明。
    notes: dict[str, str] = {}

    def _leg(label: str, provider: str, tool: str, func: Callable[[], Any], budget: float) -> Callable[[], Any]:
        """一个"带兜底预算 + 记降级"的取数任务（供并发执行）。"""

        def run() -> Any:
            value, note = _bounded_provider_call(
                state, provider=provider, tool=tool, func=func, timeout=budget
            )
            if note:
                notes[label] = note
            return value

        return run

    # 只发**没有复用**的那几路；互不依赖，一起发出去。
    # 串行做这四件事实测吃掉 210s（Part H 的 Before），而它们之间没有任何依赖关系。
    queries: list[tuple[str, Callable[[], Any]]] = []
    fetched: dict[str, Any] = {}

    if reuse_out:
        fetched["去程火车"] = _reuse_result(bundle.outbound, TrainOption, "12306")
        fetched["去程航班"] = _reuse_result(bundle.outbound, FlightOption, "tuniu")
        # 把复用的结果按查询键登记进账本：这样"同一个查询在本次 run 里再问一次"
        # 会命中 cache_hit，而不是又打一遍 12306/途牛（Part C 要消灭的正是这件事）。
        _remember_reused_trains(state, ledger, origin, destination, start, bundle.outbound, "去程")
        _remember_reused_flights(
            state, ledger, origin, destination, start, bundle.outbound, "去程", travelers
        )
    else:
        queries.append(
            (
                "去程火车",
                _leg(
                    "去程火车", "12306", "search_trains",
                    lambda: hub.search_trains(origin, destination, start, hedge=hedge),
                    transport_budget,
                ),
            )
        )
        queries.append(
            (
                "去程航班",
                _leg(
                    "去程航班", "tuniu", "search_flights",
                    lambda: hub.search_flights(origin, destination, start, travelers=travelers),
                    _provider_timeout("tuniu"),
                ),
            )
        )

    if has_back:
        if reuse_in:
            fetched["回程火车"] = _reuse_result(bundle.inbound, TrainOption, "12306")
            fetched["回程航班"] = _reuse_result(bundle.inbound, FlightOption, "tuniu")
            _remember_reused_trains(state, ledger, destination, origin, back, bundle.inbound, "回程")
            _remember_reused_flights(
                state, ledger, destination, origin, back, bundle.inbound, "回程", travelers
            )
        else:
            queries.append(
                (
                    "回程火车",
                    _leg(
                        "回程火车", "12306", "search_trains",
                        lambda: hub.search_trains(destination, origin, back, hedge=hedge),
                        transport_budget,
                    ),
                )
            )
            queries.append(
                (
                    "回程航班",
                    _leg(
                        "回程航班", "tuniu", "search_flights",
                        lambda: hub.search_flights(destination, origin, back, travelers=travelers),
                        _provider_timeout("tuniu"),
                    ),
                )
            )

    if queries:
        fetched.update(
            _run_parallel(
                queries, limit=cfg.provider_max_concurrency, thread_prefix="tp-transport"
            )
        )
    degradations.extend(
        notes[label] for label in ("去程火车", "去程航班", "回程火车", "回程航班") if label in notes
    )

    out_trains = fetched["去程火车"]
    out_flights = fetched["去程航班"]
    in_trains = fetched.get("回程火车")
    in_flights = fetched.get("回程航班")
    # 状态照抄结果的真实状态（REUSED / EMPTY / TIMEOUT / ...），**不**统一写成 "REUSED" ——
    # "Discovery 查过但没查到"与"Discovery 查到了"必须分得清。
    provider_status["去程火车"] = out_trains.status
    provider_status["去程航班"] = out_flights.status

    outbound, out_alts, out_reason, out_scores = _select_transport(
        [*out_trains.items, *out_flights.items], direction="outbound", intent=intent
    )

    inbound = in_alts = None
    in_reason = ""
    if has_back:
        if reuse_in:
            in_reason = "复用 Discovery 已查到的回程候选（未重复查询）"
        elif back == start:
            in_reason = "当天往返，回程与去程同一天查询"
        provider_status["回程火车"] = in_trains.status  # type: ignore[union-attr]
        provider_status["回程航班"] = in_flights.status  # type: ignore[union-attr]
        inbound, in_alts, in_reason, _ = _select_transport(
            [*in_trains.items, *in_flights.items], direction="inbound", intent=intent  # type: ignore[union-attr]
        )
    else:
        provider_status["回程火车"] = "SKIPPED"
        provider_status["回程航班"] = "SKIPPED"
        in_reason = "没有确定的返程日期，未查询回程"

    # 注意：`degradations` 在函数开头就已初始化（超时/失败的降级要留在里面），
    # 这里只往后追加，**不能**重新赋空列表把它清掉。
    if outbound is None:
        degradations.append(
            f"去程没有可用的大交通候选（12306 {out_trains.status} / 途牛航班 {out_flights.status}），"
            "行程按「无跨城交通」排，首日不受抵达时间约束"
        )

    decisions: list[Decision] = []
    if outbound is not None:
        label = coerce_str(getattr(outbound, "label", ""))
        decisions.append(
            _decision(
                label,
                DecisionStatus.SELECT,
                "search_intercity_transport",
                ["TRANSPORT_SELECTED", "OUTBOUND"],
                out_reason,
                out_scores.get(label, {}),
            )
        )
        decisions.extend(
            _decision(
                coerce_str(getattr(option, "label", "")) or f"alternative-{index}",
                DecisionStatus.REJECT,
                "search_intercity_transport",
                ["TRANSPORT_ALTERNATIVE", "OUTBOUND"],
                coerce_str(getattr(option, "selection_reason", "")),
                out_scores.get(coerce_str(getattr(option, "label", "")), {}),
            )
            for index, option in enumerate(out_alts)
        )
    if inbound is not None:
        label = coerce_str(getattr(inbound, "label", ""))
        decisions.append(
            _decision(
                label,
                DecisionStatus.SELECT,
                "search_intercity_transport",
                ["TRANSPORT_SELECTED", "INBOUND"],
                in_reason,
                {},
            )
        )

    selection_reason = " ".join(
        part
        for part in (
            out_reason or "去程没有可用候选",
            f"回程：{in_reason}" if in_reason else "",
            f"数据源状态：{json.dumps(provider_status, ensure_ascii=False)}",
        )
        if part
    )

    plan = TransportPlan(
        selected=outbound,
        alternatives=out_alts,
        inbound_selected=inbound,
        inbound_alternatives=list(in_alts or []),
        selection_reason=selection_reason,
        door_to_door_minutes=_door_to_door_minutes(outbound) if outbound is not None else None,
        provider_status=provider_status,
    )

    steps = [
        f"12306 去程查询：{out_trains.status}，{len(out_trains.items)} 个车次"
        + (f"（数据源 {out_trains.provider}）" if out_trains.items else ""),
        f"途牛去程航班：{out_flights.status}，{len(out_flights.items)} 个航班",
        f"比选维度：价格、门到门总时长、出发到达时刻、机场/车站额外接驳、对首末天的影响、用户偏好（PRD §24）",
    ]
    if outbound is not None:
        steps.append(f"去程选定：{coerce_str(getattr(outbound, 'label', ''))}")
    if in_reason:
        steps.append(f"回程：{in_reason}")

    return {
        "transport_plan": plan,
        "outbound": outbound,
        "inbound": inbound,
        "decisions": decisions,
        "degradations": degradations,
        "timeline": [_stamp("search_intercity_transport", (out_reason or "无去程候选")[:300])],
        "stages": [
            _stage(
                "search_intercity_transport",
                "查大交通",
                (out_reason or "没有可用的大交通候选")[:200],
                steps,
                火车候选=len(out_trains.items),
                航班候选=len(out_flights.items),
                选定=coerce_str(getattr(outbound, "label", "")) or "无",
            )
        ],
    }


# ==================================================
# Node 3：search_hotels
# ==================================================


def node_search_hotels(state: TravelState) -> dict:
    hub = state["hub"]
    intent = state["intent"]
    destination = _destination(intent)

    if not (destination and intent.start_date):
        reason = "缺少目的地或入住日期，本次没有查询酒店（没有用估算价格代替）"
        return {
            "hotel_plan": HotelPlan(selection_reason=reason),
            "degradations": [reason],
            "timeline": [_stamp("search_hotels", reason)],
            "stages": [_stage("search_hotels", "查住宿", reason, [reason], 候选数=0)],
        }

    check_in = intent.start_date
    check_out = _last_date(intent) or check_in
    if check_out <= check_in:
        reason = f"行程只有 {intent.days} 天（{check_in.isoformat()} 当天往返），不需要住宿"
        return {
            "hotel_plan": HotelPlan(selection_reason=reason),
            "timeline": [_stamp("search_hotels", reason)],
            "stages": [_stage("search_hotels", "查住宿", reason, [reason], 候选数=0)],
        }

    # 复用 Discovery 的候选。它比正式 run 多翻了几页（见 discovery.fetch_hotel_candidates），
    # 所以候选池更宽 —— 这是"酒店怎么老是那么贵"的正解：扩大可选范围，而不是改打分口径。
    bundle = state.get("prefetch")
    ledger = _ledger(state)
    degradations: list[str] = []
    hotel_note = ""
    if bundle is not None and bundle.hotels:
        result = SimpleNamespace(items=list(bundle.hotels), status="REUSED", provider="tuniu")
        # 登记复用：同一个酒店查询在本次 run 里再被请求会命中账本，不再打途牛（Part C）。
        ledger.remember(
            _hotels_query_key(destination, check_in, 1),
            result,
            marker=MARK_PREFETCH_REUSED,
            detail="酒店候选复用 Discovery",
        )
    else:
        # 兜底预算来自 config（Part E）：途牛偶发劣化时，宁可如实降级也不能拖死整次 run。
        result, hotel_note = _bounded_provider_call(
            state,
            provider="tuniu",
            tool="search_hotels",
            func=lambda: hub.search_hotels(destination, check_in, check_out, travelers=max(1, intent.travelers)),
            timeout=_provider_timeout("tuniu"),
        )
        if hotel_note:
            degradations.append(hotel_note)

    # --- Part E 的酒店 fallback 链：途牛 EMPTY / timeout → 后备来源（联网证据）→ 明确披露 ---
    # 关键：**绝不**因为途牛没数据就编一个酒店或价格。后备来源只提供"住哪个区域"这类
    # 可核对的证据，价格仍然留空（预算里如实写明未取得报价）。
    hotel_evidence_note = ""
    steps_extra: list[str] = []
    if not result.items:
        web = hub.web_search(f"{destination} 住宿 区域 推荐 交通方便", max_results=5)
        if web.items:
            ledger.mark(destination, MARK_FALLBACK_QUERY, "途牛无候选，改用联网证据提供住宿区域")
            payload = web.items[0]
            hotel_evidence_note = (
                "途牛未返回可用住宿候选，已改用联网证据说明住宿区域"
                f"（{coerce_str(payload.get('title'), '网页结果')}）；"
                "没有用估算价格代替，住宿费用不计入预算"
            )
            steps_extra.append(hotel_evidence_note)
            _record_subspan(
                state,
                component=SpanKind.TOOL,
                name="hotel_fallback_web",
                status="SUCCESS",
                # started_at 是 `_record_subspan` 的必填参数：漏传会让"途牛返回空 →
                # 走联网兜底"这条**常见**路径直接抛 TypeError 把整次 run 打挂，
                # 而它本该只是一次降级。调用就发生在这里，用这里的时刻。
                started_at=now_iso(),
                attributes={
                    "provider": "tavily",
                    "tool": "web_search",
                    "fallback": MARK_FALLBACK_QUERY,
                    "title": coerce_str(payload.get("title"), "网页结果")[:120],
                    "url": coerce_str(payload.get("url")) or None,
                    "reason": f"途牛酒店 {result.status}",
                },
                parent_span_id=f"{state['run_id']}:search_hotels",
            )
        else:
            ledger.mark(destination, MARK_FALLBACK_QUERY, "途牛与联网证据都没有住宿信息")
            hotel_evidence_note = (
                f"途牛未返回可用住宿候选（{result.status}），联网证据也没有可用信息；"
                "本次没有住宿候选，住宿费用不计入预算，也没有用估算价格代替"
            )
            steps_extra.append(hotel_evidence_note)

    selected, alternatives, reason = _select_hotel(
        list(result.items), intent, anchor=_hotel_anchor(state)
    )
    if getattr(result, "status", "") == "REUSED":
        reason = f"复用 Discovery 候选（{len(result.items)} 个，已翻页扩大范围）—— {reason}"

    if selected is None:
        reason = hotel_evidence_note or (
            f"途牛酒店查询未返回可用候选（{result.status}），本次没有住宿候选；"
            "住宿费用不计入预算，也没有用估算价格代替"
        )
        degradations.append(reason)

    decisions: list[Decision] = []
    if selected is not None:
        decisions.append(
            _decision(
                coerce_str(selected.hotel_id) or selected.name,
                DecisionStatus.SELECT,
                "search_hotels",
                ["HOTEL_SELECTED"],
                reason,
                _hotel_score(selected, intent)[1],
            )
        )

    steps = [
        f"途牛酒店查询：{result.status}，{len(result.items)} 个候选",
        f"入住 {check_in.isoformat()} → 退房 {check_out.isoformat()}",
        "比选口径：每晚价格、评分、住宿偏好命中；价格是「起价」，预算按真实单价相乘",
        *steps_extra,
    ]
    if selected is not None:
        steps.append(f"选定：{selected.name}")

    return {
        "hotel_plan": HotelPlan(
            selected=selected,
            alternatives=alternatives,
            selection_reason=reason or "途牛未返回可用住宿候选",
        ),
        "decisions": decisions,
        "degradations": degradations,
        "timeline": [_stamp("search_hotels", (reason or "无住宿候选")[:300])],
        "stages": [
            _stage(
                "search_hotels",
                "查住宿",
                (reason or "没有可用住宿候选")[:200],
                steps,
                候选数=len(result.items),
                数据源状态=result.status,
            )
        ],
    }


# ==================================================
# Node 4：search_social_guides
# ==================================================


def _fallback_queries(intent: TripIntent) -> list[str]:
    """模型不可用时的检索词兜底：只是关键词组合，不含任何"答案"。"""
    city = _destination(intent) or ""
    queries = [f"{city} 必去景点", f"{city} 美食推荐", f"{city} 避坑 不值得", f"{city} 住宿 区域"]
    for pref in intent.preferences[:3]:
        queries.append(f"{city} {pref}")
    return queries


def node_search_social(state: TravelState) -> dict:
    hub = state["hub"]
    llm = state["llm"]
    intent = state["intent"]
    destination = _destination(intent) or ""

    degradations: list[str] = []
    bundle = state.get("prefetch")
    if bundle is not None and bundle.evidences:
        # 攻略正文不会因为用户改偏好而变，直接复用 Discovery 抓到的证据，不重复检索
        # （省下的是一次可能几十秒的社交源往返）。
        reused_evidences = sorted(bundle.evidences, key=lambda item: item.id)
        _ledger(state).mark(
            "social", MARK_PREFETCH_REUSED, f"{len(reused_evidences)} 条攻略证据复用 Discovery（未重复检索）"
        )
        summary = f"复用 Discovery 已抓到的 {len(reused_evidences)} 条攻略证据（未重复检索）"
        return {
            "evidences": reused_evidences,
            "queries": [],
            "degradations": list(bundle.degradations),
            "timeline": [_stamp("search_social_guides", summary)],
            "stages": [
                _stage(
                    "search_social_guides",
                    "查攻略",
                    summary,
                    [summary, "偏好变化只影响排序，不需要重新抓原始攻略"],
                    证据条数=len(reused_evidences),
                    来源="Discovery 复用",
                )
            ],
        }

    result = llm.invoke_json(
        RESEARCH_QUERY_EXPANSION_PROMPT,
        json.dumps(
            {
                "destination": intent.destination,
                "days": intent.days,
                "travelers": intent.travelers,
                "preferences": intent.preferences,
                "constraints": intent.constraints,
                "raw_text": state["query"],
            },
            ensure_ascii=False,
        ),
        tag="query_expansion",
    )
    if result.ok and isinstance(result.value, list) and result.value:
        queries = [coerce_str(item) for item in result.value if coerce_str(item)]
        query_source = f"模型扩写（{len(queries)} 条）"
    else:
        queries = _fallback_queries(intent)
        query_source = "规则兜底检索词"
        degradations.append(degraded_note(result, "检索词退化为规则组合"))

    evidences: list[Evidence] = []
    notes: list[str] = []
    platforms: dict[str, int] = {}
    cfg = current_config()
    ledger = _ledger(state)

    # 社交检索之间互不依赖（都是"关键词 → 攻略"），所以受控并发；但**排序按检索词顺序**
    # 合并，`MAX_EVIDENCE` 截断点因此与串行版本完全一致（可复现）。
    social_keywords = list(queries[:SOCIAL_QUERY_LIMIT])
    social_results = _run_parallel(
        [
            (
                keyword,
                (lambda kw=keyword: ledger.fetch(
                    query_key("tikhub", "search_xiaohongshu", query=kw),
                    lambda kw=kw: hub.search_xiaohongshu(kw),
                    detail=f"小红书「{kw}」",
                )[0]),
            )
            for keyword in social_keywords
        ],
        limit=cfg.provider_max_concurrency,
        thread_prefix="tp-social",
    )
    for keyword in social_keywords:
        social = social_results.get(keyword)
        if social is None:
            notes.append(f"小红书「{keyword}」：调用失败（已按无结果处理）")
            continue
        platforms["小红书"] = platforms.get("小红书", 0) + len(social.items)
        evidences.extend(social.items)
        if not social.items:
            notes.append(f"小红书「{keyword}」：{social.status}")
        if len(evidences) >= MAX_EVIDENCE:
            break

    if not any(ev.provider == "xhs" for ev in evidences) and destination:
        # 依赖关系：先看小红书结果，空了才回退抖音（不能并发）。
        douyin = ledger.fetch(
            query_key("tikhub", "search_douyin", query=f"{destination} 旅游"),
            lambda: hub.search_douyin(f"{destination} 旅游"),
            detail="抖音回退",
        )[0]
        platforms["抖音"] = platforms.get("抖音", 0) + len(douyin.items)
        evidences.extend(douyin.items)
        notes.append(f"小红书没有返回可用攻略，回退抖音查询：{douyin.status}")

    web_keywords = list(queries[:WEB_QUERY_LIMIT])
    web_results = _run_parallel(
        [
            (
                keyword,
                (lambda kw=keyword: ledger.fetch(
                    query_key("tavily", "web_search", query=kw),
                    lambda kw=kw: hub.web_search(kw, max_results=5),
                    detail=f"网页搜索「{kw}」",
                )[0]),
            )
            for keyword in web_keywords
        ],
        limit=cfg.provider_max_concurrency,
        thread_prefix="tp-web",
    )
    for keyword in web_keywords:
        web = web_results.get(keyword)
        if web is None:
            notes.append(f"网页搜索「{keyword}」：调用失败（已按无结果处理）")
            continue
        if web.items:
            platforms["网页"] = platforms.get("网页", 0) + len(web.items)
        else:
            notes.append(f"网页搜索「{keyword}」：{web.status}")
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

    evidences = evidences[:MAX_EVIDENCE]
    if not evidences:
        degradations.append(
            "社交与网页攻略都没有返回可用内容，Trust Score 的「多来源证据」分项会偏低；"
            "这不是数据缺失的掩盖，而是如实反映"
        )

    steps = [
        f"检索词来源：{query_source}",
        f"实际使用的检索词：{'、'.join(queries[:SOCIAL_QUERY_LIMIT])}",
        f"各平台返回条数：{json.dumps(platforms, ensure_ascii=False)}",
        "社交内容按「参考」使用：广告风险由 Ad Risk 六项分项评估，不直接采信",
    ]
    steps.extend(notes[:5])

    summary = f"{query_source}；共收集 {len(evidences)} 条攻略/网页证据"
    return {
        "evidences": evidences,
        "queries": queries,
        "degradations": degradations,
        "timeline": [_stamp("search_social_guides", summary + f"（{'、'.join(f'{k} {v} 条' for k, v in platforms.items()) or '无'}）")],
        "stages": [
            _stage(
                "search_social_guides",
                "查攻略",
                summary,
                steps,
                证据条数=len(evidences),
                平台数=len(platforms),
            )
        ],
    }


# ==================================================
# Node 5：extract_and_normalize_places
# ==================================================


def _keyword_candidates(intent: TripIntent, queries: list[str], *, limit: int | None = None) -> list[str]:
    """除攻略抽取之外，直接用高德按关键词搜 POI。

    两个来源互补：攻略能捞到"本地人常去"的小店，关键词能保证"必去景点"不会因为
    攻略没写而完全缺席。两者都只是**候选**，是否入选由 Trust / Ad Risk 决定。

    ``limit`` 不传时读 config（POI_QUERY_LIMIT）—— 每个关键词都是一次真实的高德调用，
    这个数直接换成钱与时间。
    """
    destination = _destination(intent) or ""
    cap = max(1, int(limit if limit is not None else current_config().poi_query_limit))
    names = ["景点", "博物馆", "公园", "步行街", "古镇"]
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


def node_extract_places(state: TravelState) -> dict:
    hub = state["hub"]
    llm = state["llm"]
    intent = state["intent"]
    store = state["store"]
    run_id = state["run_id"]
    destination = _destination(intent) or ""

    degradations: list[str] = []
    decisions: list[Decision] = []

    # --- 1) 从攻略文本里抽地点（优先用 provider 已经给出的 place_mentions，省一次模型调用）---
    extracted: list[dict[str, Any]] = []
    provider_mentions: list[str] = []
    for evidence in state.get("evidences", []):
        provider_mentions.extend(evidence.place_mentions)

    bundle = state.get("prefetch")
    # Part F：Guided 模式下这批证据**在 Discovery 阶段已经抽过一遍**了。
    # 只要那一步已经跑完（不是仍在 RUNNING），就不该拿同一批文本再调一次模型 ——
    # 实测 4 次串行抽取要 60s 以上，而它只会得出同一个结论。复用结论不是"少做一步"，
    # 而是"同一份内容不做第二遍"，因此这里即使抽不出地点也照常跳过并如实说明。
    reused_extraction = _discovery_already_extracted(state, bundle)
    llm_targets = (
        []
        if reused_extraction
        else [
            evidence
            for evidence in state.get("evidences", [])
            if not evidence.place_mentions and len(evidence.text) >= 40
        ][:EXTRACT_EVIDENCE_LIMIT]
    )
    # 多篇攻略是**互相独立**的 batch，串行等它们是一笔白花的墙钟：
    # 实测 4 条证据串行 63.6s（最快 10.7s、最慢 19.6s），并发后墙钟≈最慢的那条。
    # 上限走 config 的 LLM_MAX_CONCURRENCY（独立模型调用可以并行，但必须限并发）。
    cfg = current_config()

    def _extract_one(evidence: Any) -> Any:
        """抽一条证据的地点；异常不往外抛，交给调用方按 degraded 处理。"""

        try:
            return evidence, llm.invoke_json(
                EXTRACT_PLACES_PROMPT,
                f"标题：{evidence.title}\n来源：{evidence.provider}/{evidence.source_type}\n"
                f"证据 id：{evidence.id}\n正文：\n{evidence.text[:EVIDENCE_TEXT_CHARS]}",
                tag=f"extract_places:{evidence.id}",
            )
        except Exception as exc:  # noqa: BLE001 —— 单条失败不能带走整批
            return evidence, exc

    if llm_targets:
        outcomes = _run_parallel(
            [(f"e{index}", (lambda item=item: _extract_one(item))) for index, item in enumerate(llm_targets)],
            limit=cfg.llm_max_concurrency,
            thread_prefix="tp-extract",
        ).values()
    else:
        outcomes = []

    # 按**定义顺序**消费结果，所以并发不会改变抽取出来的地点顺序。
    for outcome in outcomes:
        if isinstance(outcome, tuple) and len(outcome) == 2:
            evidence, result = outcome
        else:  # pragma: no cover —— 形状不对时跳过，不让它变成一次假成功
            continue
        if isinstance(result, Exception):
            degradations.append(f"证据 {evidence.id} 的地点抽取被跳过：{type(result).__name__}: {result}")
            continue
        if result.ok and isinstance(result.value, dict):
            for item in result.value.get("places", []) or []:
                if isinstance(item, dict) and coerce_str(item.get("name")):
                    extracted.append({**item, "evidence_id": evidence.id})
        else:
            degradations.append(degraded_note(result, f"证据 {evidence.id} 的地点抽取被跳过"))

    if reused_extraction:
        _ledger(state).mark("extract_places", MARK_PREFETCH_REUSED, "地点抽取复用 Discovery 结论（未重复调用模型）")
        extraction_note = "这批攻略在 Discovery 阶段已经抽过地点，本次不再对同一批证据调用模型（Part F）"
    elif llm_targets:
        extraction_note = (
            f"有 {len(llm_targets)} 条攻略走了模型抽取地点，"
            f"{len(state.get('evidences', [])) - len(llm_targets)} 条直接用数据源自带的 place_mentions"
        )
    else:
        extraction_note = "攻略地点全部来自数据源自带的 place_mentions，本次没有为抽取地点调用模型"

    # --- 1.5) 引导式：复用 Discovery 已经查好的地点候选 ---
    # 用户点过的"不感兴趣"在这里就必须消失：再往后走只会让它有机会被排进行程。
    if bundle is not None and bundle.places:
        outcome = selection.apply_user_place_preferences(
            bundle.places, intent.place_selections or {}
        )
        for evidence in state.get("evidences", []):
            store.save_evidence(run_id, evidence, source_id=evidence.source_id)
        for place in outcome.kept:
            store.save_place(run_id, place)
        linked = _link_evidence(state.get("evidences", []), outcome.kept)
        with_evidence = sum(1 for place in outcome.kept if linked.get(place.place_id))
        steps = [
            f"复用 Discovery 候选 {len(bundle.places)} 个（未重复查高德）",
            f"用户选择：必去 {len(outcome.must_places)}、想去 {len(outcome.want_places)}、"
            f"不感兴趣 {len(outcome.excluded)}（已硬排除，不会进入行程）",
            f"进入打分的候选 {len(outcome.kept)} 个，其中 {with_evidence} 个有攻略证据支撑",
        ]
        steps.extend(f"用户排除：{place.name}" for place in outcome.excluded[:6])
        summary = (
            f"复用 Discovery 候选 {len(outcome.kept)} 个"
            + (f"，已按用户选择硬排除 {len(outcome.excluded)} 个" if outcome.excluded else "")
        )
        _record_subspan(
            state,
            component=SpanKind.PLANNER,
            name="user_place_selection",
            status="SUCCESS",
            started_at=now_iso(),
            attributes={
                "reused": len(bundle.places),
                "kept": len(outcome.kept),
                "must": len(outcome.must_places),
                "want": len(outcome.want_places),
                "rejected": len(outcome.excluded),
            },
            parent_span_id=f"{run_id}:extract_and_normalize_places",
        )
        return {
            "places": outcome.kept,
            "decisions": outcome.decisions,
            "degradations": [],
            "timeline": [_stamp("extract_and_normalize_places", summary)],
            "stages": [
                _stage(
                    "extract_and_normalize_places",
                    "抽取与归一化地点",
                    summary,
                    steps,
                    候选数=len(outcome.kept),
                    用户排除=len(outcome.excluded),
                    来源="Discovery 复用",
                )
            ],
        }

    # --- 2) 高德 POI 搜索（受控并发，Part A）---
    keywords = _keyword_candidates(intent, state.get("queries") or _fallback_queries(intent))
    places: list[Place] = []
    seen_ids: set[str] = set()
    poi_status = "OK"

    # 关键词表：攻略里提到的地名 / 模型抽出的地名 / 兜底关键词。
    # DISCOVERY_MAX_PLACES 管的是**原始候选**总量（Part D）：先把两个来源合并去重再截断，
    # 避免"同一家店在两条攻略里出现 → 查两遍高德"。
    raw_names: list[str] = []
    for name in [
        *provider_mentions[: cfg.poi_query_limit],
        *[coerce_str(item.get("name")) for item in extracted[: cfg.poi_query_limit]],
    ]:
        cleaned = coerce_str(name).strip()
        if cleaned and cleaned not in raw_names:
            raw_names.append(cleaned)
    raw_names = raw_names[: cfg.discovery_max_places]
    search_terms = [*raw_names, *keywords]
    searched = len(search_terms)

    def search_one(term: str) -> Any:
        return hub.search_poi(term, destination, page_size=cfg.poi_page_size, type_hint="attraction")

    results = _run_parallel(
        [(term, (lambda term=term: search_one(term))) for term in search_terms],
        limit=cfg.provider_max_concurrency,
        thread_prefix="tp-poi",
    )
    # 去重必须按**任务定义顺序**做，不能用完成顺序：同一份攻略在两路并发下
    # 先返回哪个是不确定的，按完成顺序去重会让 first-wins 的字段在不同 run 里抖动。
    for term in search_terms:
        result = results[term]
        for place in result.items:
            if place.place_id in seen_ids:
                continue
            seen_ids.add(place.place_id)
            places.append(place)
    poi_status = _first_bad_status(results)

    if not places and destination:
        # 高德确实没返回任何 POI：如实降级，不用模型编地点。
        degradations.append(
            f"高德 POI 查询没有返回任何地点（{poi_status}），本次行程没有可选地点；"
            "没有用模型生成的地点填补"
        )

    # --- 3) 去重（PRD §20）---
    deduped, dedupe_decisions = planner.dedupe_places(places)
    decisions.extend(dedupe_decisions)

    # --- 4) 持久化 + 建证据链 ---
    linked = _link_evidence(state.get("evidences", []), deduped)
    for evidence in state.get("evidences", []):
        store.save_evidence(run_id, evidence, source_id=evidence.source_id)
    for place in deduped:
        store.save_place(run_id, place)
        for evidence in linked.get(place.place_id, []):
            store.link_place_evidence(run_id, place.place_id, evidence.id)

    with_evidence = sum(1 for place in deduped if linked.get(place.place_id))
    steps = [
        f"高德 POI 查询 {searched} 次（攻略抽取 {len(extracted)} 个地名 + 关键词 {len(keywords)} 个）",
        f"POI 原始候选 {len(places)} 个 → 去重后 {len(deduped)} 个",
        f"其中 {with_evidence} 个有攻略证据支撑，{len(deduped) - with_evidence} 个只有高德 POI 信息",
        extraction_note,
    ]
    for item in extracted[:6]:
        steps.append(
            f"攻略抽取：{coerce_str(item.get('name'))}"
            f"（{coerce_str(item.get('category')) or '未分类'}，{coerce_str(item.get('tone')) or 'neutral'}，来自 {item.get('evidence_id')}）"
        )

    summary = f"{len(places)} 个 POI 候选 → 去重后 {len(deduped)} 个，其中 {with_evidence} 个有攻略证据"
    return {
        "places": deduped,
        "decisions": decisions,
        "degradations": degradations,
        "timeline": [_stamp("extract_and_normalize_places", summary)],
        "stages": [
            _stage(
                "extract_and_normalize_places",
                "抽取与归一化地点",
                summary,
                steps,
                地图候选=len(places),
                去重后=len(deduped),
                有证据=with_evidence,
            )
        ],
    }


# ==================================================
# Node 6：verify_poi_and_routes
# ==================================================


def _deep_verify_places(
    state: TravelState,
    places: Sequence[Place],
    *,
    limit: int,
    selections: Mapping[str, str],
) -> tuple[list[Place], list[dict[str, Any]]]:
    """挑出值得做深度验证（POI 详情 + 路线）的地点，并给出每个被跳过点的原因。

    规则（Part D：不要深度验证最终不会使用的点）：
      * 用户 REJECT 的点**绝不**查高德、绝不查门票 —— 它已经被硬排除了，再花钱验证
        它既是浪费，也是"用户说不要还被查"的观感问题；
      * MUST / WANT 的点**无条件**深度验证（用户明确要的点，宁可多花几次调用）；
      * 其余 NEUTRAL 用 Trust 分排序取到 ``PLANNER_POI_LIMIT`` 为止 —— 规格要求
        "高分 NEUTRAL"才做深度验证。Trust 与 ``score_candidates`` 的入选口径同源，
        所以"能被排进去的点"和"被深度验证的点"不会南辕北辙。

    返回 (要验证的点, 跳过记录)。跳过记录进 Trace，让"这个点为什么没被验证"能直接回答。
    """

    linked = _link_evidence(list(state.get("evidences") or []), list(places))
    selected: list[Place] = []
    skipped: list[dict[str, Any]] = []
    for place in places:
        if selection.selection_of(place, selections) in (selection.MUST, selection.WANT):
            selected.append(place)
    selected_ids = {place.place_id for place in selected}

    def _trust_of(place: Place) -> float:
        trust, _ = planner.trust_score(
            place, linked.get(place.place_id, []), amap_verified=place.amap_verified
        )
        return float(trust)

    # REJECT 的点**直接出局**，不进 neutral 候选池。
    # 上一版把它们放进池子再靠"超出 limit"挡下来，于是"Trust 分高的 REJECT 点"照样
    # 进了深度验证 —— 用户明确说了不要还去查高德/查门票，既是实打实的浪费，
    # 也和"硬排除"这个承诺不一致。
    rejected = [
        place for place in places if selection.selection_of(place, selections) == selection.REJECT
    ]
    for place in rejected:
        skipped.append(
            {
                "place_id": place.place_id,
                "name": place.name,
                "reason": "USER_REJECT",
                "reason_text": "用户明确表示不感兴趣，已硬排除，不再做深度验证",
            }
        )
    rejected_ids = {place.place_id for place in rejected}

    neutral = [
        place
        for place in places
        if place.place_id not in selected_ids and place.place_id not in rejected_ids
    ]
    # 排序键带 place_id 兜底：同名同分也不能靠字典顺序碰运气，否则并发下
    # "哪 15 个点被深度验证"会随输入噪声变化（同分不同名时曾真的抖过）。
    neutral.sort(key=lambda place: (-_trust_of(place), place.name, place.place_id))

    room = max(0, int(limit) - len(selected))
    for index, place in enumerate(neutral):
        if index < room:
            selected.append(place)
            continue
        skipped.append(
            {
                "place_id": place.place_id,
                "name": place.name,
                "reason": "PLANNER_LIMIT",
                "reason_text": f"Trust {_trust_of(place):.0f} 未进前 {int(limit)}，按 Planner 上限跳过深度验证",
            }
        )
    return selected, skipped


def _station_to_hotel_minutes(
    hub: Any, station: str, hotel: Any, city: str | None
) -> int | None:
    """车站/机场 → 酒店 的真实接驳分钟数（含入住缓冲）；取不到就返回 None。

    抽成独立函数是为了让"去程/回程两段"能并发：原来这段逻辑嵌在 for 循环里，
    想并发只能复制一遍（复制出来的两份迟早会不一致）。
    """

    geo = hub.geocode(coerce_str(station), city=city)
    if not (geo.ok and geo.items):
        return None
    coords = (
        coerce_float(geo.items[0].get("longitude") or geo.items[0].get("lng")),
        coerce_float(geo.items[0].get("latitude") or geo.items[0].get("lat")),
    )
    if coords[0] is None or coords[1] is None:
        return None
    route = hub.route(coords, (hotel.lng, hotel.lat), "transit", city=city)
    if route.ok and route.items and route.items[0].duration_minutes is not None:
        return route.items[0].duration_minutes + planner.HOTEL_CHECKIN_MINUTES
    return None


def node_verify_poi_and_routes(state: TravelState) -> dict:
    hub = state["hub"]
    intent = state["intent"]
    places = list(state.get("places", []))
    selections = intent.place_selections or {}
    destination = _destination(intent) or ""
    degradations: list[str] = []
    cfg = current_config()

    # --- 0) 只对"可能进最终行程"的点做深度验证（Part D）---
    deep_places, skipped = _deep_verify_places(
        state, places, limit=cfg.planner_poi_limit, selections=selections
    )
    deep_ids = {place.place_id for place in deep_places}
    route_skipped = [item for item in skipped if item["reason"] == "USER_REJECT"]
    limit_skipped = [item for item in skipped if item["reason"] == "PLANNER_LIMIT"]
    if skipped:
        _skip_span(
            state,
            name="route_skipped",
            parent_stage="verify_poi_and_routes",
            attributes={
                "reason": "USER_REJECT" if route_skipped else "PLANNER_LIMIT",
                "reason_text": (
                    "用户明确排除的地点不查高德、不查门票"
                    if route_skipped
                    else f"超过 Planner 深度候选上限 {cfg.planner_poi_limit}"
                ),
                "skipped": len(skipped),
                "user_rejected": len(route_skipped),
                "over_limit": len(limit_skipped),
                "planner_poi_limit": cfg.planner_poi_limit,
                "places": skipped[:20],
            },
        )

    # --- 1) 补营业时间（可行性判定要用，PRD §22）---
    # 受益于并发的正是这一段与下一段：一条一条串行查（实测 6 次详情 + 9 段路线 ≈ 58s）
    # 和一起发出去，拿到的是同一批数据。
    detail_targets = [
        place
        for place in places
        if place.place_id in deep_ids and not place.opening_hours and place.amap_verified
    ][: cfg.poi_detail_limit]
    detail_results = _run_parallel(
        [
            (place.place_id, (lambda place=place: hub.poi_detail(place.place_id)))
            for place in detail_targets
        ],
        limit=cfg.poi_verify_max_concurrency,
        thread_prefix="tp-poi-detail",
    )
    detail_calls = len(detail_targets)
    detail_failures = 0
    # 回填按目标顺序做：并发只影响"谁先回来"，不影响"结果写进哪一行"。
    for place in detail_targets:
        result = detail_results[place.place_id]
        if result is not None and result.ok and result.items:
            item = result.items[0]
            place.opening_hours = coerce_str(item.get("opening_hours")) or place.opening_hours
            place.address = coerce_str(item.get("address")) or place.address
            place.district = coerce_str(item.get("district")) or place.district
            place.business_area = coerce_str(item.get("business_area")) or place.business_area
        else:
            detail_failures += 1
    if detail_failures:
        degradations.append(
            f"{detail_failures} 个地点的营业时间未能取得，可行性检查会把这些点按「营业时间未知」处理"
        )

    # --- 2) 计算地点之间的路线 ---
    hotel = state.get("hotel_plan").selected if state.get("hotel_plan") else None
    hotel_coords = None
    if hotel is not None and hotel.lat is not None and hotel.lng is not None:
        hotel_coords = (hotel.lat, hotel.lng)

    ordered = planner.order_by_proximity(deep_places, hotel_coords)
    routes: dict[Any, RouteOption] = {}
    route_ok = 0
    route_fail = 0
    ledger = _ledger(state)
    # 只查"相邻一段"，**不做** POI × POI 全连接（Part D §7）。上限来自 config：
    # MAX_ROUTE_LOOKUPS 就是"最多查几段"。旧实现用 len(routes)（每段成功会写正反两条
    # 记录）当计数器，等于把配置的预算悄悄砍了一半；这里改成按调用次数计。
    #
    # 两条护栏（Part D §7 的"坐标初筛 → 只查真正可能相邻的腿"）：
    #   * 缺坐标的腿不查（查了也无从发起）；
    #   * 直线距离超过 ROUTE_MAX_LEG_METERS 的腿判定为"非相邻"，不做市内路线查询 ——
    #     预算留给真正相邻的腿，而不是被一条跨城的腿吃掉。
    # 被跳过的腿**不是**被静默丢掉：进 Trace（route_skipped）+ 有一条明确说明，
    # 行程里仍然按"未核实"处理，绝不用估算值冒充实测。
    pair_tasks: list[tuple[str, Callable[[], Any]]] = []
    pair_order: list[tuple[Place, Place]] = []
    leg_skipped: list[dict[str, Any]] = []
    attempted = 0
    for origin, target in zip(ordered, ordered[1:]):
        start, end = lnglat(origin), lnglat(target)
        label = f"{origin.name} → {target.name}"
        if start is None or end is None:
            leg_skipped.append({"leg": label, "reason": "NO_COORDS", "reason_text": "缺少坐标，无法发起路线查询"})
            ledger.mark(label, MARK_ROUTE_SKIPPED, "缺少坐标")
            continue
        distance = planner.haversine_meters((origin.lat, origin.lng), (target.lat, target.lng))
        if distance is not None and distance > ROUTE_MAX_LEG_METERS:
            leg_skipped.append(
                {
                    "leg": label,
                    "reason": "TOO_FAR",
                    "reason_text": f"直线 {distance / 1000:.0f}km，判定为非相邻路段（不做全连接查询）",
                }
            )
            ledger.mark(label, MARK_ROUTE_SKIPPED, f"直线 {distance / 1000:.0f}km 超过 {ROUTE_MAX_LEG_METERS / 1000:.0f}km 初筛阈值")
            continue
        if attempted >= cfg.max_route_lookups:
            leg_skipped.append(
                {"leg": label, "reason": "BUDGET", "reason_text": f"超过 MAX_ROUTE_LOOKUPS={cfg.max_route_lookups} 上限"}
            )
            ledger.mark(label, MARK_ROUTE_SKIPPED, "超过 MAX_ROUTE_LOOKUPS 上限")
            continue
        attempted += 1
        key = _route_query_key(start, end, destination or None)
        ledger.mark(key, MARK_ROUTE_REQUESTED, label)
        pair_order.append((origin, target))
        pair_tasks.append(
            (
                f"{origin.place_id}->{target.place_id}",
                (
                    lambda start=start, end=end, key=key, label=label: _route_with_ledger(
                        hub, ledger, key, label, start, end, destination or None
                    )
                ),
            )
        )
    route_results = _run_parallel(
        pair_tasks, limit=cfg.route_max_concurrency, thread_prefix="tp-route"
    )
    for origin, target in pair_order:
        key = f"{origin.place_id}->{target.place_id}"
        result = route_results[key]
        if result is not None and result.ok and result.items:
            option = result.items[0]
            option.origin_place_id = origin.place_id
            option.destination_place_id = target.place_id
            routes[(origin.place_id, target.place_id)] = option
            routes[(target.place_id, origin.place_id)] = option
            route_ok += 1
        else:
            route_fail += 1

    # --- 3) 机场/车站 → 酒店的真实接驳（PRD §24 的"额外接驳"）---
    # 两段（去程/回程）互不依赖，一起发出去。
    outbound_transfer: int | None = None
    inbound_transfer: int | None = None
    transport_plan = state.get("transport_plan")
    if leg_skipped:
        # 被初筛/预算挡下来的段必须显式留痕：跳过不等于查过了，也不等于没发生。
        _skip_span(
            state,
            name="route_skipped_legs",
            parent_stage="verify_poi_and_routes",
            attributes={
                "reason": MARK_ROUTE_SKIPPED,
                "skipped": len(leg_skipped),
                "no_coords": sum(1 for item in leg_skipped if item["reason"] == "NO_COORDS"),
                "over_limit": sum(1 for item in leg_skipped if item["reason"] == "BUDGET"),
                "too_far": sum(1 for item in leg_skipped if item["reason"] == "TOO_FAR"),
                "max_route_lookups": cfg.max_route_lookups,
                "legs": leg_skipped[:20],
            },
        )
        degradations.append(
            f"{len(leg_skipped)} 段相邻路线没有查询（坐标缺失 / 非相邻初筛 / 超过上限 "
            f"{cfg.max_route_lookups} 段），这些段在行程里按未核实处理"
        )
    transfer_tasks: list[tuple[str, Callable[[], Any]]] = []
    if hotel is not None and hotel_coords is not None and transport_plan is not None:
        for attr, setter in (("selected", "out"), ("inbound_selected", "in")):
            option = getattr(transport_plan, attr, None)
            if option is None:
                continue
            station = (
                getattr(option, "arrival_airport", None)
                or getattr(option, "arrival_station", None)
                or getattr(option, "destination_station", None)
            )
            if not station:
                continue
            transfer_tasks.append(
                (
                    setter,
                    (
                        lambda station=station: _station_to_hotel_minutes(
                            hub, station, hotel, destination or None
                        )
                    ),
                )
            )
    transfer_results = _run_parallel(
        transfer_tasks, limit=cfg.route_max_concurrency, thread_prefix="tp-transfer"
    )
    outbound_transfer = transfer_results.get("out")
    inbound_transfer = transfer_results.get("in")

    if outbound_transfer is not None and transport_plan is not None:
        _annotate_transfer_minutes(transport_plan.selected, outbound_transfer)
    if inbound_transfer is not None and transport_plan is not None:
        _annotate_transfer_minutes(transport_plan.inbound_selected, inbound_transfer)

    amap_status = "OK"
    if route_fail and route_ok:
        amap_status = "DEGRADED"
    elif route_fail and not route_ok:
        amap_status = "UNAVAILABLE"
    if state.get("places") and not places:
        amap_status = "UNAVAILABLE"

    if route_fail:
        degradations.append(
            f"{route_fail} 段市内路线未能从高德取得（{amap_status}），这些路段在行程里标为未核实，"
            "Critic 会相应下调置信度；没有用估算值冒充实测"
        )
    if outbound_transfer is None and transport_plan is not None and transport_plan.selected is not None:
        degradations.append("去程车站/机场到酒店的真实接驳时间未取得，门到门时长按具名常量估算")

    steps = [
        f"高德 POI 详情查询 {detail_calls} 次（补营业时间/地址，并发上限 {cfg.poi_verify_max_concurrency}）",
        f"路线查询：成功 {route_ok} 段、失败 {route_fail} 段"
        f"（上限 {cfg.max_route_lookups} 段，并发上限 {cfg.route_max_concurrency}）",
        "路线按「酒店附近 → 由近及远」顺序计算，与后续排程的地理聚类口径一致",
        f"高德整体状态：{amap_status}",
    ]
    if skipped:
        steps.append(
            f"深度验证候选 {len(deep_places)} 个（Planner 上限 {cfg.planner_poi_limit}）："
            f"跳过 {len(skipped)} 个 —— 用户已排除 {len(route_skipped)} 个"
            f"（不查高德、不查门票）、超出上限 {len(limit_skipped)} 个（不查路线）"
        )
    if route_fail:
        steps.append("失败的路线在行程里标为未核实，没有用估算值冒充实测")
    if outbound_transfer is not None:
        steps.append(f"去程 车站/机场 → 酒店 真实接驳 {outbound_transfer} 分钟（含入住）")
    if inbound_transfer is not None:
        steps.append(f"回程 酒店 → 车站/机场 真实接驳 {inbound_transfer} 分钟（含入住）")

    summary = f"高德路线 {route_ok} 段成功 / {route_fail} 段失败，整体状态 {amap_status}"
    return {
        "places": places,
        "routes": routes,
        "amap_status": amap_status,
        "outbound_transfer_minutes": outbound_transfer,
        "inbound_transfer_minutes": inbound_transfer,
        "transport_plan": transport_plan,
        "degradations": degradations,
        "timeline": [_stamp("verify_poi_and_routes", summary)],
        "stages": [
            _stage(
                "verify_poi_and_routes",
                "核实 POI 与路线",
                summary,
                steps,
                路线成功=route_ok,
                路线失败=route_fail,
                高德状态=amap_status,
                深度候选=len(deep_places),
                跳过=len(skipped),
                用户排除跳过=len(route_skipped),
            )
        ],
    }


# ==================================================
# Node 7：score_candidates
# ==================================================


def node_score_candidates(state: TravelState) -> dict:
    hub = state["hub"]
    intent = state["intent"]
    store = state["store"]
    run_id = state["run_id"]
    places = list(state.get("places", []))
    evidences = list(state.get("evidences", []))
    linked = _link_evidence(evidences, places)
    routes = state.get("routes") or {}
    hotel = state.get("hotel_plan").selected if state.get("hotel_plan") else None

    # 用户的 MUST/WANT/REJECT 对**所有**入口生效（Quick 也可能带着 place_selections 进来，
    # 例如从会话里回填）。REJECT 是硬排除：不参与打分，也就不可能被排进行程。
    preference_outcome = selection.apply_user_place_preferences(
        places, intent.place_selections or {}
    )
    places = preference_outcome.kept
    selection_decisions: list[Decision] = list(preference_outcome.decisions)

    trust_scores: dict[str, float] = {}
    trust_details: dict[str, dict] = {}
    ad_risks: dict[str, float] = {}
    ad_risk_details: dict[str, dict] = {}
    candidate_scores: dict[str, dict] = {}

    hotel_coords = (hotel.lat, hotel.lng) if hotel is not None and hotel.lat is not None and hotel.lng is not None else None
    for place in places:
        place_evidences = linked.get(place.place_id, [])
        trust, trust_detail = planner.trust_score(place, place_evidences, amap_verified=place.amap_verified)
        risk, risk_detail = planner.ad_risk(place, place_evidences)
        trust_scores[place.place_id] = trust
        trust_details[place.place_id] = trust_detail
        ad_risks[place.place_id] = risk
        ad_risk_details[place.place_id] = risk_detail

        fit = 0.0
        if hotel_coords is not None:
            distance = planner.haversine_meters(place.coords, hotel_coords)
            if distance is not None:
                fit = max(0.0, 1.0 - (distance / 1000.0) / planner.ROUTE_FIT_RANGE_KM)
        score, score_detail = planner.score_candidate(
            place, trust, risk, intent.preferences, fit, place_evidences
        )
        # 用户点过的"必去/想去"必须真的改变结果，而不是只写进一句文案。
        bonus, bonus_reason = selection.preference_bonus(place, intent.place_selections or {})
        if bonus:
            score += bonus
            score_detail = {
                **score_detail,
                "score": round(score, 2),
                "user_bonus": bonus,
                "basis": f"{score_detail.get('basis', '')}；{bonus_reason}",
            }
        candidate_scores[place.place_id] = score_detail

    def _score_of(place: Place) -> float:
        return coerce_float(candidate_scores.get(place.place_id, {}).get("score"), 0.0) or 0.0

    ordered = sorted(places, key=lambda place: (-trust_scores.get(place.place_id, 0.0), place.name))
    decisions: list[Decision] = list(selection_decisions)
    for place in ordered:
        score = _score_of(place)
        status = DecisionStatus.KEEP if score >= planner.MIN_CANDIDATE_SCORE else DecisionStatus.REJECT
        codes = ["CANDIDATE_KEEP"] if status == DecisionStatus.KEEP else ["CANDIDATE_REJECT", "LOW_SCORE"]
        reason = (
            f"Trust {trust_scores[place.place_id]:.0f} / AdRisk {ad_risks[place.place_id]:.0f} / "
            f"综合 {score:.0f}；证据 {len(linked.get(place.place_id, []))} 条"
            + ("（低于入选门槛，不排进计划）" if status == DecisionStatus.REJECT else "")
        )
        decisions.append(
            _decision(place.place_id, status, "score_candidates", codes, reason, candidate_scores.get(place.place_id, {}))
        )
    store.save_decisions_bulk(run_id, decisions)

    # --- 门票：候选排序后才知道哪些景点值得查价（PRD §23 的门票要用真实价）---
    # 用户 REJECT 的点在循环开始前就已经被 `apply_user_place_preferences` 硬排除了，
    # 所以这里**不可能**给用户说"不要"的点查门票；再叠一层断言式的过滤，
    # 是为了让这条约束在代码里看得见（而不是只靠上游一个变量名）。
    cfg = current_config()
    ticket_prices: dict[str, float] = {}
    ticket_candidates = [
        place
        for place in sorted(
            places, key=lambda item: (-trust_scores.get(item.place_id, 0.0), item.name, item.place_id)
        )
        if selection.selection_of(place, intent.place_selections or {}) != selection.REJECT
    ][: cfg.ticket_lookup_limit]
    ticket_attempts = len(ticket_candidates)
    ticket_results = _run_parallel(
        [
            (place.place_id, (lambda place=place: hub.search_scenic_tickets(place.name)))
            for place in ticket_candidates
        ],
        limit=cfg.provider_max_concurrency,
        thread_prefix="tp-ticket",
    )
    for place in ticket_candidates:
        result = ticket_results[place.place_id]
        if result is None:
            continue
        for item in result.items:
            price = coerce_float(
                item.get("price") or item.get("min_price") or (item.get("prices") or {}).get("min")
            )
            if price is not None:
                ticket_prices[place.place_id] = price
                ticket_prices[place.name] = price
                break

    kept = [place for place in ordered if _score_of(place) >= planner.MIN_CANDIDATE_SCORE]
    rejected = len(places) - len(kept)
    priced = sum(1 for place in kept if place.place_id in ticket_prices)
    top = kept[:6]

    steps = [
        "Trust 与 Ad Risk 按 PRD §18/§19 的六项分项逐项计算（每项明细都进 audit）",
        f"{len(places)} 个候选 → 入选 {len(kept)} 个、剔除 {rejected} 个",
        f"门票查询 {ticket_attempts} 次，取到 {priced} 个价格"
        f"（入选景点里 {len(kept) - priced} 个没有实时票价，预算不计入该部分）",
    ]
    for place in top:
        steps.append(
            f"{place.name}：Trust {trust_scores[place.place_id]:.0f}、AdRisk {ad_risks[place.place_id]:.0f}、"
            f"综合 {coerce_float(candidate_scores[place.place_id].get('score'), 0.0):.0f}"
        )

    summary = f"{len(places)} 个候选打分完毕：入选 {len(kept)}、剔除 {rejected}"
    if preference_outcome.excluded:
        summary += f"（另有 {len(preference_outcome.excluded)} 个按用户选择硬排除）"
    return {
        "places": places,
        "trust_scores": trust_scores,
        "trust_details": trust_details,
        "ad_risks": ad_risks,
        "ad_risk_details": ad_risk_details,
        "candidate_scores": candidate_scores,
        "ticket_prices": ticket_prices,
        "decisions": decisions,
        "timeline": [_stamp("score_candidates", summary)],
        "stages": [
            _stage(
                "score_candidates",
                "候选打分",
                summary,
                steps,
                入选=len(kept),
                剔除=rejected,
                门票价格数=len(ticket_prices),
            )
        ],
    }


# ==================================================
# Node 8：build_initial_plan
# ==================================================


def node_build_plan(state: TravelState) -> dict:
    """生成初始行程：先由 Python 排出 Top-K 候选，再让 Jev 选整体更好的那份。

    分工（任务 §3）：
      Python  → 排程、时间推算、硬约束（决定"有哪些合法方案"）
      Jev     → 只在若干**同样合法**的方案之间选（决定"哪份体验更好"）

    Jev 选中的方案必须再过一次硬约束复核；不通过就退回默认方案。任何 Jev 失败都只是
    fallback，行程照常产出。
    """
    intent = state["intent"]
    places = list(state.get("places", []))
    evidences = list(state.get("evidences", []))
    linked = _link_evidence(evidences, places)
    transport_plan = state.get("transport_plan")
    hotel_plan = state.get("hotel_plan")
    run_id = state["run_id"]
    outbound = state.get("outbound")

    build_kwargs: dict[str, Any] = {
        "trust_scores": state.get("trust_scores") or {},
        "ad_risks": state.get("ad_risks") or {},
        "trust_details": state.get("trust_details") or {},
        "evidences": linked,
        "routes": state.get("routes") or {},
        "hotel": hotel_plan.selected if hotel_plan else None,
        "outbound": outbound,
        "inbound": state.get("inbound"),
        "ticket_prices": state.get("ticket_prices") or {},
        "city": _destination(intent),
        "mode_hint": None,
    }
    parent = f"{run_id}:build_initial_plan"

    candidates_started = now_iso()
    candidates = planner.build_plan_variants(
        intent,
        places,
        limit=MAX_PLAN_CANDIDATES,
        quality_places={place.place_id: place for place in places},
        quality_evidences=linked,
        **build_kwargs,
    )
    if not candidates:
        # 所有变体都失败时，退回"只跑默认策略"：与引入 Top-K 之前的行为完全一致，
        # 异常照旧抛出（而不是被变体循环吞掉），这样失败仍然能定位到真正的排程错误。
        fallback_days = planner.build_initial_plan(intent, places, **build_kwargs)
        candidates = [
            planner.PlanCandidate(
                label=planner.VARIANT_LABELS[planner.PLAN_VARIANT_NEAREST],
                variant=planner.PLAN_VARIANT_NEAREST,
                days=fallback_days,
                quality=planner.plan_quality(
                    fallback_days,
                    intent,
                    places={place.place_id: place for place in places},
                    evidences=linked,
                ),
            )
        ]
    _record_subspan(
        state,
        component=SpanKind.PLANNER,
        name="build_candidates",
        status="SUCCESS" if candidates else "FAILED",
        started_at=candidates_started,
        attributes={"candidate_count": len(candidates), "variants": [c.variant for c in candidates]},
        parent_span_id=parent,
    )

    jev = state.get("jev")
    # Part F：只把**通过硬约束复核**的候选交给 Jev 选。两种收益：
    #   1. 不再让 Jev 去选一份 Python 随后必然否决的方案（那是白花一次调用 + 一条
    #      "Jev 选错"的假信号）；
    #   2. 硬约束可行的候选只剩一份时，`choose_plan` 直接返回该方案、**不调用 Jev** ——
    #      Python 已经唯一决定了，"选择"只是浪费一次调用与 1.5s 预算。
    # 一份可行的都没有时退回全部候选：那是排程本身有问题，应该照旧让 Jev 参与并如实记录。
    viable_candidates = [
        candidate
        for candidate in candidates
        if not planner.hard_constraint_violations(candidate.days, intent, outbound=outbound)
    ] or candidates
    choice = choose_plan(
        viable_candidates,
        intent,
        jev=jev,
        recorder=_span_recorder(state),
        parent_span_id=parent,
        hard_violations=lambda candidate: planner.hard_constraint_violations(
            candidate.days, intent, outbound=outbound
        ),
    )
    chosen = choice.candidate
    single_viable = len(viable_candidates) == 1 and len(candidates) > 1

    recheck_started = now_iso()
    violations = planner.hard_constraint_violations(chosen.days, intent, outbound=outbound)
    _record_subspan(
        state,
        component=SpanKind.PLANNER,
        name="hard_constraint_recheck",
        status="SUCCESS" if not violations else "WARNING",
        started_at=recheck_started,
        attributes={"label": chosen.label, "violations": violations[:5], "count": len(violations)},
        parent_span_id=parent,
    )

    days = _annotate_items(
        chosen.days,
        places=places,
        linked=linked,
        trust_details=state.get("trust_details") or {},
        ad_risk_details=state.get("ad_risk_details") or {},
    )

    item_count = sum(len(day.items) for day in days)
    steps = [f"共 {len(days)} 天、{item_count} 个安排"]
    if len(candidates) > 1:
        steps.append(
            f"Top-{len(candidates)} 候选："
            + "、".join(f"{c.label}({c.variant})" for c in candidates)
        )
    if single_viable:
        steps.append(
            f"通过硬约束的候选只有 {viable_candidates[0].label} 一份，Python 已唯一确定，未为此调用 Jev"
        )
    steps.append(f"方案选择：{choice.reason}")
    if choice.rejected:
        steps.append(f"硬约束复核否决了 Jev 的选择：{choice.rejected}")
    for day in days:
        first = day.items[0].start_time if day.items else "—"
        last = day.items[-1].end_time if day.items else "—"
        steps.append(
            f"第 {day.day_index + 1} 天（{day.date.isoformat() if day.date else '日期未定'}，"
            f"区域 {day.area or '未定'}）：{first} ~ {last}，{len(day.items)} 项"
        )
        steps.extend(f"　{note}" for note in (day.notes or [])[:2])

    summary = f"排出 {len(days)} 天 {item_count} 项安排（候选 {len(candidates)} 份，选用 {chosen.label}）"
    decision = _decision(
        run_id,
        DecisionStatus.SELECT if not choice.fallback else DecisionStatus.PASS,
        "jev" if choice.jev_record is not None else "planner",
        ["JEV_PLAN_CHOICE"] + (["JEV_FALLBACK"] if choice.fallback else []),
        choice.reason,
        {
            "selected": chosen.label,
            "variant": chosen.variant,
            "candidates": [candidate.label for candidate in candidates],
            "fallback": choice.fallback,
            "quality": chosen.quality.to_dict(),
        },
    )
    return {
        "days": days,
        "plan_candidates": candidates,
        "plan_choice": choice.to_dict(),
        "quality": chosen.quality,
        "decisions": [decision],
        "jev_calls": [choice.jev_record] if choice.jev_record and choice.jev_record.get("attempted") else [],
        "timeline": [
            _stamp("build_initial_plan", summary + f"（{'、'.join(day.area or '未定' for day in days)}）")
        ],
        "stages": [
            _stage(
                "build_initial_plan",
                "生成初始行程",
                summary,
                steps,
                天数=len(days),
                安排数=item_count,
                候选数=len(candidates),
                选用=chosen.label,
            )
        ],
    }


def _annotate_items(
    days: list[ItineraryDay],
    *,
    places: list[Place],
    linked: dict[str, list[Evidence]],
    trust_details: dict[str, dict],
    ad_risk_details: dict[str, dict],
) -> list[ItineraryDay]:
    """给 item 补上「为什么推荐」与「风险是什么」的拆解（前端 Evidence Drawer 直接展示）。

    这些文本全部由代码从已算好的分项里生成，不经过模型 —— 理由必须和分数同源，
    否则会出现"理由是这么说的、分数是那么算的"这种无法审计的矛盾。
    """
    by_id = {place.place_id: place for place in places}
    for day in days:
        for item in day.items:
            if not item.place_id or item.place_id not in by_id:
                continue
            points: list[str] = []
            detail = trust_details.get(item.place_id) or {}
            for key, label in (
                ("multi_source", "多来源独立证据"),
                ("scale", "评价/出现规模"),
                ("specificity", "具体体验细节"),
                ("persistence", "跨时间持续出现"),
                ("local", "本地人/重复访问线索"),
                ("official", "POI/官方信息一致"),
            ):
                block = detail.get(key)
                if isinstance(block, dict) and block.get("basis"):
                    points.append(f"{label}：{block['basis']}")
            risk = ad_risk_details.get(item.place_id) or {}
            risk_bits = [
                f"{block['basis']}"
                for block in risk.values()
                if isinstance(block, dict) and block.get("basis") and coerce_float(block.get("score"), 0.0)
            ]
            item.reason_points = points
            if item.type in ("attraction", "food", "activity") and item.ad_risk is not None and item.ad_risk >= 40:
                item.risk_note = "广告嫌疑信号：" + "；".join(risk_bits[:3])
            evidence_count = len(linked.get(item.place_id, []))
            if item.type in ("attraction", "food", "activity") and evidence_count <= 1:
                item.risk_note = (item.risk_note + "；" if item.risk_note else "") + (
                    f"只有 {evidence_count} 条攻略证据支撑，建议自行确认"
                )
    return days


# ==================================================
# Node 9：check_budget
# ==================================================


def node_check_budget(state: TravelState) -> dict:
    intent = state["intent"]
    hotel_plan = state.get("hotel_plan")
    budget = planner.build_budget(
        intent,
        state.get("transport_plan"),
        hotel_plan.selected if hotel_plan else None,
        state.get("days") or [],
        state.get("ticket_prices") or {},
        city=_destination(intent),
        day_count=len(state.get("days") or []),
        hotel_alternatives=list(hotel_plan.alternatives) if hotel_plan else (),
    )

    steps = [
        f"真实花费（Provider 报价）：¥{budget.known_real_cost:,.0f}",
        f"估算花费（餐饮/无价路段）：¥{budget.estimated_cost:,.0f}",
        f"预计总额：¥{budget.projected_total:,.0f}"
        + (f"，预算 ¥{budget.budget_total:,.0f}，结余 ¥{budget.remaining:,.0f}" if budget.budget_total else "（用户没有给预算）"),
    ]
    steps.extend(budget.price_notes)
    if budget.optimization_suggestions:
        steps.extend(f"优化建议：{item}" for item in budget.optimization_suggestions)

    summary = f"预计 ¥{budget.projected_total:,.0f}（真实 ¥{budget.known_real_cost:,.0f} + 估算 ¥{budget.estimated_cost:,.0f}），状态 {budget.status}"
    _record_subspan(
        state,
        component=SpanKind.BUDGET,
        name="check_budget",
        status="SUCCESS",
        started_at=now_iso(),
        attributes={
            "projected_total": budget.projected_total,
            "known_real_cost": budget.known_real_cost,
            "estimated_cost": budget.estimated_cost,
            "status": budget.status,
        },
        parent_span_id=f"{state['run_id']}:check_budget",
    )
    return {
        "budget": budget,
        "timeline": [_stamp("check_budget", summary)],
        "stages": [
            _stage(
                "check_budget",
                "算预算",
                summary,
                steps,
                真实花费=f"¥{budget.known_real_cost:,.0f}",
                估算花费=f"¥{budget.estimated_cost:,.0f}",
                状态=budget.status,
            )
        ],
    }


# ==================================================
# Node 10：check_feasibility
# ==================================================


def _feasibility_inputs(state: TravelState) -> tuple[int, int | None, int | None]:
    """三个与 days 无关的硬边界：抵达日、首日可起排时刻、末日截止时刻。"""

    intent = state["intent"]
    outbound = state.get("outbound")
    inbound = state.get("inbound")
    arrival_index = planner.arrival_day_index(outbound, intent.start_date, max(1, intent.days))
    first_day_start = planner.arrival_day_start_minutes(
        outbound, transfer_minutes=state.get("outbound_transfer_minutes")
    )
    last_day_deadline = planner.departure_deadline_minutes(
        inbound, transfer_minutes=state.get("inbound_transfer_minutes")
    )
    return arrival_index, first_day_start, last_day_deadline


def _apply_feasibility_pass(state: TravelState, days: list[ItineraryDay]) -> tuple[list[ItineraryDay], list[FeasibilityIssue]]:
    """对一份具体的 days 跑时间/窗口修订。

    抽出来是为了让 critic 阶段"换了候选方案就重跑一遍"能和第 10 步走**完全同一条**
    路径 —— 否则换方案后 warnings 与实际 days 会对不上。
    """

    arrival_index, first_day_start, last_day_deadline = _feasibility_inputs(state)
    return planner.apply_feasibility_pass(
        days,
        state.get("routes") or {},
        state["intent"],
        places={place.place_id: place for place in state.get("places", [])},
        first_day_start=first_day_start,
        first_day_index=arrival_index,
        last_day_deadline=last_day_deadline,
    )


def _contextual_feasibility_issues(state: TravelState) -> list[FeasibilityIssue]:
    """与 days 无关的可行性告警（天数被交通吃掉 / 发车日不符 / 回程跨度 / 无住宿）。"""

    intent = state["intent"]
    outbound = state.get("outbound")
    inbound = state.get("inbound")
    arrival_index, _, _ = _feasibility_inputs(state)
    hotel_plan = state.get("hotel_plan")
    return [
        *_transit_span_issues(intent, outbound, arrival_index),
        *_off_date_issues(intent, outbound, inbound),
        *_inbound_span_issues(intent, inbound),
        *_hotel_issues(intent, hotel_plan.selected if hotel_plan else None),
    ]


def node_check_feasibility(state: TravelState) -> dict:
    intent = state["intent"]
    days = list(state.get("days") or [])
    outbound = state.get("outbound")
    inbound = state.get("inbound")
    arrival_index, first_day_start, last_day_deadline = _feasibility_inputs(state)

    revised, issues = _apply_feasibility_pass(state, days)

    resolved = sum(1 for issue in issues if issue.resolved)
    errors = sum(1 for issue in issues if issue.severity == "error" and not issue.resolved)

    # 与 days 无关的告警（天数被交通吃掉 / 发车日不符 / 回程跨度 / 一个住宿都没有）：
    # 这些事实必须说出来，但不修改行程 —— 改日期是编造，改行程去迁就它也一样。
    contextual = _contextual_feasibility_issues(state)
    issues = [*issues, *contextual]
    resolved += sum(1 for issue in contextual if issue.resolved)
    errors += sum(1 for issue in contextual if issue.severity == "error" and not issue.resolved)

    # 抵达链的原始值（落地/到站 + 出站 + 接驳 + 入住）与**起排时刻**是两个数：
    # 红眼航班 02:20 就能到酒店，但那天从 08:30 才排行程（人要睡觉）。
    raw_ready = planner.arrival_ready_minutes(
        outbound, transfer_minutes=state.get("outbound_transfer_minutes")
    )

    detail_calls = 0
    steps = [
        (
            f"去程第 {arrival_index + 1} 天（{_day_label(intent, arrival_index)}）抵达；"
            + (
                f"抵达链最早 {_clock(raw_ready)} 可开始，行程从 {_clock(first_day_start)} 起排"
                if raw_ready is not None and raw_ready < first_day_start
                else f"抵达后最早 {_clock(first_day_start)} 才能开始行程"
            )
            if first_day_start is not None
            else "首日无抵达约束"
        ),
        f"末日最晚结束时间：{_clock(last_day_deadline)}" if last_day_deadline is not None else "末日无返程约束",
        f"共发现 {len(issues)} 个问题：自动修订 {resolved} 个、未解决 {errors} 个",
    ]
    for issue in issues:
        if detail_calls >= 8:
            break
        detail_calls += 1
        steps.append(
            f"[{'已修订' if issue.resolved else issue.severity}] 第 {issue.day_index + 1} 天 "
            f"{issue.code}：{issue.reason}"
            + (f"（{issue.original_start} → {issue.revised_start}）" if issue.original_start else "")
        )

    summary = f"发现 {len(issues)} 个时间/营业时间问题，自动修订 {resolved} 个，遗留 {errors} 个"
    _record_subspan(
        state,
        component=SpanKind.FEASIBILITY,
        name="check_feasibility",
        status="SUCCESS" if errors == 0 else "WARNING",
        started_at=now_iso(),
        attributes={
            "issues": len(issues),
            "auto_revised": resolved,
            "unresolved_errors": errors,
            "arrival_index": arrival_index,
            "last_day_deadline_minutes": last_day_deadline,
        },
        parent_span_id=f"{state['run_id']}:check_feasibility",
    )
    return {
        "days": revised,
        "warnings": issues,
        "timeline": [_stamp("check_feasibility", summary)],
        "stages": [
            _stage(
                "check_feasibility",
                "检查可行性",
                summary,
                steps,
                问题数=len(issues),
                已修订=resolved,
                未解决=errors,
            )
        ],
    }


# ==================================================
# Node 11：critic_and_revise
# ==================================================


#: 送给模型（Critic / 成文）的 plan digest 上限（字符）。超了**按结构**瘦身，不从中间切断。
DIGEST_CHAR_BUDGET = 24_000
#: 瘦身第 1 级：单条安排的理由保留多少字符。reason 是 digest 里最长的文本，先压它收益最大。
DIGEST_REASON_CHARS = 80
#: 瘦身第 2 级：每天最多保留几条安排。
DIGEST_ITEMS_PER_DAY = 4
#: 瘦身第 3 级：最多保留几天。
DIGEST_DAYS = 6


def _apply_narrowing(digest: dict[str, Any], level: int) -> dict[str, Any]:
    """对 digest 做第 ``level`` 级结构瘦身（就地修改并返回）。"""

    if level <= 0:
        return digest
    # 第 1 级：压短最长的文本字段。
    for day in digest.get("days") or []:
        for item in day.get("items") or []:
            reason = item.get("reason")
            if isinstance(reason, str) and len(reason) > DIGEST_REASON_CHARS:
                item["reason"] = reason[:DIGEST_REASON_CHARS] + "…"
    transport = digest.get("transport") or {}
    if isinstance(transport.get("selection_reason"), str) and len(transport["selection_reason"]) > 300:
        transport["selection_reason"] = transport["selection_reason"][:300] + "…"
    if level < 2:
        return digest
    # 第 2 级：每天的条目数封顶，但**如实记录**砍掉了多少，模型才知道自己没看到全部。
    for day in digest.get("days") or []:
        items = day.get("items") or []
        if len(items) > DIGEST_ITEMS_PER_DAY:
            day["items_dropped"] = len(items) - DIGEST_ITEMS_PER_DAY
            day["items"] = items[:DIGEST_ITEMS_PER_DAY]
    if level < 3:
        return digest
    # 第 3 级：天数封顶 + 预算明细只留汇总。
    days = digest.get("days") or []
    if len(days) > DIGEST_DAYS:
        digest["days_dropped"] = len(days) - DIGEST_DAYS
        digest["days"] = days[:DIGEST_DAYS]
    budget = digest.get("budget")
    if isinstance(budget, dict):
        budget.pop("breakdown", None)
        budget.pop("breakdown_price_type", None)
    return digest


def _digest_json(plan: TripPlan, *, char_budget: int = DIGEST_CHAR_BUDGET) -> str:
    """plan digest → **一定合法**的 JSON 文本，尽量不超过 ``char_budget``。

    为什么要专门做这件事：上一版是 ``json.dumps(...)[:24000]``，那是**按字符切断 JSON**。
    一旦超出，模型收到的是缺括号、缺引号的残片，解析必然失败 —— 省下的不是时间，而是
    整次调用的产出（而且失败得很隐蔽：看起来像"模型不听话"）。
    所以这里在**结构**上逐级做减法，每一级都仍然是一份合法 JSON，并把"裁剪过、砍了多少"
    如实写进 payload，让模型知道自己看到的不是全部而不是以为行程就这么多。
    """

    digest = _plan_digest(plan)
    text = json.dumps(digest, ensure_ascii=False, default=str)
    for level in range(4):
        if len(text) <= char_budget:
            return text
        # `_plan_digest` 每次都新造字典，所以可以安全地就地瘦身（不需要 deepcopy）。
        if level < 3:
            _apply_narrowing(digest, level + 1)
            text = json.dumps(digest, ensure_ascii=False, default=str)
    # 已瘦到最小形态仍超预算：照原样送出（合法 JSON 比"恰好卡在预算内"重要）。
    return text


def _plan_digest(plan: TripPlan) -> dict[str, Any]:
    """给 Critic 用的紧凑摘要。只给它已算好的事实，让它只做判断（PRD §25）。"""
    return {
        "intent": plan.intent.model_dump(mode="json"),
        "transport": {
            "selected": plan.transport.selected.label if plan.transport and plan.transport.selected else None,
            "inbound": plan.transport.inbound_selected.label
            if plan.transport and plan.transport.inbound_selected
            else None,
            "selection_reason": plan.transport.selection_reason if plan.transport else "",
        },
        "hotel": {
            "selected": plan.hotel.selected.name if plan.hotel and plan.hotel.selected else None,
            "reason": plan.hotel.selection_reason if plan.hotel else "",
        },
        "days": [
            {
                "day_index": day.day_index,
                "date": day.date.isoformat() if day.date else None,
                "area": day.area,
                "items": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "type": item.type,
                        "start": item.start_time,
                        "end": item.end_time,
                        "reason": item.reason,
                        "trust": item.trust_score,
                        "ad_risk": item.ad_risk,
                        "price": item.price,
                        "price_type": item.price_type,
                        "evidence_ids": item.evidence_ids,
                        "route_verified": (
                            item.travel_from_previous.verified if item.travel_from_previous else None
                        ),
                    }
                    for item in day.items
                ],
            }
            for day in plan.days
        ],
        "budget": {
            "projected_total": plan.budget.projected_total,
            "known_real_cost": plan.budget.known_real_cost,
            "estimated_cost": plan.budget.estimated_cost,
            "status": plan.budget.status,
            "breakdown": plan.budget.breakdown,
            "breakdown_price_type": plan.budget.breakdown_price_type,
        },
        "warnings": [issue.model_dump(mode="json") for issue in plan.warnings],
    }


def _hard_issue_texts(plan: TripPlan) -> list[str]:
    """Jev 质量门必须看到的硬问题清单（由代码判定，不是模型判断）。

    它同时是 Bad Case 规则 ``jev_missed_replan`` 的判据：Jev 在**知道**这些问题的
    前提下仍然说 KEEP，才说明是它的判断出了问题。
    """

    issues = [
        f"{issue.code}@day{issue.day_index}: {issue.reason}"
        for issue in plan.warnings
        if issue.severity == "error" and not issue.resolved
    ]
    if plan.transport is None or plan.transport.selected is None:
        issues.append("缺少去程交通披露")
    if plan.hotel is None or plan.hotel.selected is None:
        issues.append("缺少住宿披露")
    return issues


@dataclass(slots=True)
class _JevReview:
    """critic 阶段两次 Jev 软决策的汇总（供 node_critic_revise 消费）。"""

    tradeoff: dict[str, Any]
    gate: dict[str, Any]
    jev_calls: list[dict[str, Any]]
    decisions: list[Decision]
    steps: list[str]
    degradations: list[str]
    #: 质量门要求 REPLAN 且确实换成了另一份候选时才非空。
    swapped: planner.PlanCandidate | None = None
    days: list[ItineraryDay] | None = None
    warnings: list[FeasibilityIssue] | None = None


def _replan_choice(
    candidates: list[planner.PlanCandidate],
    current: planner.PlanCandidate | None,
) -> planner.PlanCandidate | None:
    """确定性重排：换一份**拓扑不同**的候选（按偏好匹配→节奏匹配择优）。

    这就是 REPLAN 在 v0.1 的真实含义 —— 用同一套 Planner 的另一套区域排序策略重排一版，
    而不是调模型凭空再生成一份（那会引入无法复现的行程）。没有别的候选时返回 None。
    """

    others = [candidate for candidate in candidates if current is None or candidate.label != current.label]
    if not others:
        return None
    return max(
        others,
        key=lambda candidate: (
            candidate.quality.preference_coverage,
            candidate.quality.pace_match,
            -candidate.quality.consecutive_same_type_count,
        ),
    )


def _jev_review(
    state: TravelState,
    plan: TripPlan,
    *,
    llm_decisions: list[Decision],
) -> _JevReview:
    """Jev 的两个软决策节点：关键 Trade-off（§4B）与 Quality Gate（§4C）。

    两者都只产出"选哪个 / 要不要重排"，并把结构化结果交给 Python 执行。
    执行前一律先过 ``hard_constraint_violations``：软决策不得越过硬约束。
    """

    run_id = state["run_id"]
    jev = state.get("jev")
    intent = state["intent"]
    outbound = state.get("outbound")
    candidates = list(state.get("plan_candidates") or [])
    selected_label = (state.get("plan_choice") or {}).get("selected")
    current = next((candidate for candidate in candidates if candidate.label == selected_label), None)
    parent = f"{run_id}:critic_and_revise"
    recorder = _span_recorder(state)
    jev_calls: list[dict[str, Any]] = []
    decisions: list[Decision] = []
    steps: list[str] = []
    degradations: list[str] = []

    # --- Part F：Jev 只在"真的需要在若干合法方案之间做取舍"时才调用 ---
    # 两个跳过条件都是**可证明无损**的，不是"为了省一次调用而拍脑袋"：
    #   * 硬约束可行的候选只有当前这一份 → Python 已经唯一决定了，问 Jev 也只有一个答案；
    #   * 没有第二份可替换的候选 → 就算 Jev 说 REPLAN，Python 也无处可换（只会产出一条
    #     "无法重排"的降级），质量门的结论由硬问题数量直接定死。
    viable = [
        candidate
        for candidate in candidates
        if not planner.hard_constraint_violations(candidate.days, intent, outbound=outbound)
    ]
    alternatives = [
        candidate
        for candidate in viable
        if current is None or candidate.label != current.label
    ][:3]
    # --- B. 关键 Trade-off ---
    tradeoff = resolve_tradeoff(
        current=current or _candidate_of(plan, state),
        alternatives=alternatives,
        intent=intent,
        jev=jev if alternatives else None,
        recorder=recorder,
        parent_span_id=parent,
    )
    if not alternatives:
        steps.append("只有一份通过硬约束的候选方案，Python 已唯一确定；本次没有为此调用 Jev")
        _skip_span(
            state,
            name="jev_tradeoff_skipped",
            parent_stage="critic_and_revise",
            attributes={"reason": "no_alternative_candidate", "candidates": len(candidates), "viable": len(viable)},
        )
    if tradeoff.jev_record is not None and tradeoff.jev_record.get("attempted"):
        jev_calls.append(tradeoff.jev_record)
    decisions.append(
        _decision(
            plan.run_id,
            DecisionStatus.KEEP if tradeoff.choice.startswith("KEEP") else DecisionStatus.REVISION,
            "jev" if tradeoff.jev_record is not None else "planner",
            ["JEV_TRADEOFF", tradeoff.choice] + (["JEV_FALLBACK"] if tradeoff.fallback else []),
            tradeoff.reason,
            {"choice": tradeoff.choice, "applied": tradeoff.applied_label},
        )
    )
    steps.append(f"关键取舍：{tradeoff.reason}")

    # --- C. Planner Quality Gate ---
    # 质量门**无条件**经过（既有契约：Jev 说 KEEP 但有硬问题时必须能记成 missed_replan）。
    # 这里不做"看起来不需要判断就跳过"的优化 —— 跳过它等于放弃最有价值的一类 Bad Case。
    hard_issues = _hard_issue_texts(plan)
    quality = state.get("quality") or planner.plan_quality(plan.days, intent)
    gate = quality_gate(
        quality=quality,
        hard_issues=hard_issues,
        intent=intent,
        jev=jev,
        recorder=recorder,
        parent_span_id=parent,
    )
    if gate.jev_record is not None and gate.jev_record.get("attempted"):
        jev_calls.append(gate.jev_record)
    steps.append(
        f"质量门：{gate.reason}（软分 {gate.signals}，硬问题 {gate.hard_issue_count} 项）"
    )
    decisions.append(
        _decision(
            plan.run_id,
            DecisionStatus.PASS if gate.decision == "KEEP" else DecisionStatus.REVISION,
            "jev" if gate.jev_record is not None else "planner",
            ["JEV_QUALITY_GATE", gate.decision] + (["JEV_FALLBACK"] if gate.fallback else []),
            gate.reason,
            {"signals": gate.signals, "hard_issues": hard_issues[:5]},
        )
    )

    review = _JevReview(
        tradeoff=tradeoff.to_dict(),
        gate=gate.to_dict(),
        jev_calls=jev_calls,
        decisions=decisions,
        steps=steps,
        degradations=degradations,
    )

    # --- 执行 REPLAN：换一份候选并重跑可行性修订 ---
    wants_replan = tradeoff.choice == "REPLAN" or gate.decision == "REPLAN"
    if not wants_replan:
        return review
    replacement = _replan_choice(candidates, current)
    if replacement is None:
        note = "质量门要求重排，但没有第二份候选方案可用；已如实记录，不凭空生成新行程"
        steps.append(note)
        degradations.append(note)
        review.steps = steps
        review.degradations = degradations
        return review
    violations = planner.hard_constraint_violations(replacement.days, intent, outbound=outbound)
    if violations:
        note = f"重排候选 {replacement.label} 未通过硬约束复核，保持原方案：{violations[0]}"
        steps.append(note)
        degradations.append(note)
        review.steps = steps
        review.degradations = degradations
        return review

    revised_days, issues = _apply_feasibility_pass(state, replacement.days)
    contextual = _contextual_feasibility_issues(state)
    new_warnings = [*issues, *contextual]
    new_hard = [
        f"{issue.code}@day{issue.day_index}: {issue.reason}"
        for issue in new_warnings
        if issue.severity == "error" and not issue.resolved
    ]
    # 重排后硬问题反而变多就没有换的必要 —— 那只是把问题换了个样子。
    if len(new_hard) > len(hard_issues):
        note = (
            f"重排候选 {replacement.label} 的硬问题从 {len(hard_issues)} 增加到 {len(new_hard)}，"
            "已放弃重排并保留原方案"
        )
        steps.append(note)
        degradations.append(note)
        review.steps = steps
        review.degradations = degradations
        return review

    steps.append(
        f"已按质量门重排：{current.label if current else '原方案'} → {replacement.label}"
        f"（硬问题 {len(hard_issues)} → {len(new_hard)}）"
    )
    decisions.append(
        _decision(
            plan.run_id,
            DecisionStatus.REVISION,
            "planner",
            ["JEV_REPLAN_APPLIED", f"TO_{replacement.label}"],
            f"质量门要求重排，已换用候选 {replacement.label} 并重跑可行性修订",
            {
                "from": current.label if current else None,
                "to": replacement.label,
                "hard_issues_before": len(hard_issues),
                "hard_issues_after": len(new_hard),
            },
        )
    )
    review.swapped = replacement
    review.days = revised_days
    review.warnings = new_warnings
    review.steps = steps
    review.degradations = degradations
    # 换方案后 LLM Critic 的意见已经对不上新行程，如实说明而不是继续沿用。
    if llm_decisions:
        review.degradations.append("重排后模型 Critic 的意见对应的是被替换的方案，仅供参考")
    return review


def _candidate_of(plan: TripPlan, state: TravelState) -> planner.PlanCandidate:
    """当 state 里没有候选（老调用路径）时，用当前 days 现造一个占位候选。

    它只用于给 Jev 描述"当前是什么"，因此质量按当前 days 现算，不编造。
    """

    return planner.PlanCandidate(
        label=str((state.get("plan_choice") or {}).get("selected") or "A"),
        variant=str((state.get("plan_choice") or {}).get("variant") or planner.PLAN_VARIANT_NEAREST),
        days=list(plan.days),
        quality=state.get("quality") or planner.plan_quality(plan.days, state["intent"]),
    )


def node_critic_revise(state: TravelState) -> dict:
    llm = state["llm"]
    evidences = list(state.get("evidences", []))
    existing = list(state.get("decisions") or [])
    plan = _assemble_plan(state, decisions=existing)

    rule_decisions = planner.critique(
        plan,
        existing,
        evidences=evidences,
        amap_status=state.get("amap_status", "OK"),
    )

    # --- 模型 Critic 与 Jev 是两个**互不依赖**的调用，一起发出去（Part F） ---
    # 为什么不串行：两者输入同为"当前这份计划"，谁都不消费对方的输出；实测 critic 一次
    # 可以吃掉 26~110s（纯模型抖动），串行等于把这几十秒白白加在墙钟上。
    # 唯一有依赖的一处（"重排后 critic 意见已对不上新行程"）在两者都收完之后再补一句，
    # 因此结论与串行版本完全一致，只是不再互相等。
    with ThreadPoolExecutor(
        max_workers=max(1, int(current_config().llm_max_concurrency)),
        thread_name_prefix="tp-critic",
    ) as pool:
        critic_future = pool.submit(
            llm.invoke_json,
            CRITIC_PROMPT,
            _digest_json(plan),
            tag="critic",
        )
        # llm_decisions 先传空：它是 critic 的输出，此刻还没算出来；收完之后再补那句提示。
        review_future = pool.submit(_jev_review, state, plan, llm_decisions=[])
        critic_result = critic_future.result()
        review = review_future.result()

    critic: dict[str, Any] = {"status": critic_result.status, "verdict": "", "issues": [], "confidence": None}
    llm_decisions: list[Decision] = []
    if critic_result.ok and isinstance(critic_result.value, dict):
        payload = critic_result.value
        critic["verdict"] = coerce_str(payload.get("verdict"))
        critic["confidence"] = coerce_int(payload.get("confidence"))
        issues = [item for item in (payload.get("issues") or []) if isinstance(item, dict)]
        critic["issues"] = issues
        for index, issue in enumerate(issues):
            severity = coerce_str(issue.get("severity"), "medium") or "medium"
            scope = coerce_str(issue.get("scope"), "global") or "global"
            status = (
                DecisionStatus.REJECT
                if severity == "high" and critic["verdict"] == "reject"
                else DecisionStatus.REVISION
            )
            llm_decisions.append(
                _decision(
                    scope,
                    status,
                    "critic",
                    ["LLM_CRITIC", f"SEVERITY_{severity.upper()}", f"ISSUE_{index + 1}"],
                    f"{coerce_str(issue.get('problem'))}；建议：{coerce_str(issue.get('suggestion'))}；依据：{coerce_str(issue.get('evidence'), '无')}",
                    {"severity": severity, "scope": scope},
                )
            )
        critic_degradations: list[str] = []
    else:
        critic_degradations = [degraded_note(critic_result, "本次只有规则 Critic 参与")]

    # 换方案后 LLM Critic 的意见已经对不上新行程，如实说明而不是继续沿用
    # （与串行版本同一句话，只是改在两者都收完之后补）。
    if review.swapped is not None and llm_decisions:
        review.degradations = [
            *review.degradations,
            "重排后模型 Critic 的意见对应的是被替换的方案，仅供参考",
        ]
    critic_degradations = [*critic_degradations, *review.degradations]

    decisions = [*rule_decisions, *llm_decisions, *review.decisions]
    confidence_decision = next(
        (item for item in rule_decisions if "PLAN_CONFIDENCE" in item.reason_codes), None
    )
    confidence = (confidence_decision.scores.get("confidence") if confidence_decision else None) or 0.5

    steps = [
        f"规则 Critic 产出 {len(rule_decisions)} 条决策（未核实路段、高广告风险、超预算、证据未采用、置信度）",
        f"模型 Critic：{critic['status']}"
        + (f"，verdict={critic['verdict'] or '未给出'}" if critic_result.ok else "（未参与，已如实记录）"),
        f"规则置信度：{confidence:.0%}（高德状态 {state.get('amap_status', 'OK')}）",
        *review.steps,
    ]
    if critic["confidence"] is not None:
        steps.append(f"模型自评置信度：{critic['confidence']}/100")
    for issue in critic["issues"][:6]:
        steps.append(
            f"[{coerce_str(issue.get('severity'))}] {coerce_str(issue.get('scope'))}："
            f"{coerce_str(issue.get('problem'))} → {coerce_str(issue.get('suggestion'))}"
        )

    summary = f"规则 {len(rule_decisions)} 条 + 模型 {len(llm_decisions)} 条决策；计划置信度 {confidence:.0%}"
    if review.swapped is not None:
        summary += f"；已按质量门重排为候选 {review.swapped.label}"
    _record_subspan(
        state,
        component=SpanKind.CRITIC,
        name="rule_critic",
        status="SUCCESS",
        started_at=now_iso(),
        attributes={
            "rule_decisions": len(rule_decisions),
            "llm_decisions": len(llm_decisions),
            "confidence": confidence,
            "llm_critic_status": critic["status"],
            "hard_issues": _hard_issue_texts(plan)[:5],
        },
        parent_span_id=f"{state['run_id']}:critic_and_revise",
    )

    result: dict[str, Any] = {
        "critic": critic,
        "decisions": decisions,
        "degradations": critic_degradations,
        "tradeoff": review.tradeoff,
        "gate": review.gate,
        "jev_calls": review.jev_calls,
        "timeline": [_stamp("critic_and_revise", summary)],
        "stages": [
            _stage(
                "critic_and_revise",
                "审查与修订",
                summary,
                steps,
                规则决策=len(rule_decisions),
                模型决策=len(llm_decisions),
                置信度=f"{confidence:.0%}",
                Jev决策=len(review.jev_calls),
            )
        ],
    }
    # 重排后必须把新的 days / warnings 写回 state，否则 finalize 会拿旧行程去算预算。
    if review.days is not None:
        result["days"] = review.days
        result["warnings"] = review.warnings or []
        result["quality"] = planner.plan_quality(review.days, state["intent"])
    return result


# ==================================================
# Node 12：finalize
# ==================================================


def _assemble_plan(state: TravelState, *, decisions: list[Decision]) -> TripPlan:
    intent = state["intent"]
    hub = state.get("hub")
    return TripPlan(
        run_id=state["run_id"],
        query=state["query"],
        intent=intent,
        transport=state.get("transport_plan"),
        hotel=state.get("hotel_plan"),
        days=list(state.get("days") or []),
        budget=state.get("budget") or BudgetSummary(),
        warnings=list(state.get("warnings") or []),
        sources=[SourceRef(**ref) for ref in (hub.source_refs() if hub is not None else [])],
        decisions=list(decisions),
    )


def _render_markdown(plan: TripPlan, *, degradations: list[str], critic: dict) -> str:
    """确定性成文：所有数字直接取自 plan 对象，不经过模型改写。

    这是 plan.md 的"骨架"，模型写的那段说明只是附在它上面的散文 —— 这样即使模型
    胡说，用户照着下面的明细走依然是对的。
    """
    intent = plan.intent
    lines: list[str] = []
    lines.append(f"# {plan.run_id} · {' → '.join(intent.destination) or '未指定目的地'}")
    lines.append("")
    lines.append(f"- 原始需求：{plan.query}")
    lines.append(f"- 出发地：{intent.origin or '未提供'}")
    lines.append(f"- 时间：{intent.date_range}")
    lines.append(f"- 人数：{intent.travelers} 人")
    lines.append(
        f"- 预算：{'¥%g' % intent.budget_total if intent.budget_total else '用户未提供'}"
    )
    if intent.preferences:
        lines.append(f"- 偏好：{'、'.join(intent.preferences)}")
    lines.append("")

    transport = plan.transport
    if transport is not None and transport.selected is not None:
        selected = transport.selected
        lines.append("## 大交通")
        lines.append("")
        lines.append(f"- 去程：{selected.label}")
        if selected.departure_at and selected.arrival_at:
            lines.append(
                f"  - {selected.departure_at:%Y-%m-%d %H:%M} → {selected.arrival_at:%Y-%m-%d %H:%M}"
                f"（{selected.duration_minutes or '未知'} 分钟）"
            )
        lines.append(f"  - 门到门约 {selected.door_to_door_minutes or '未知'} 分钟（含提前到站/机场与市区接驳）")
        lines.append(f"  - 来源：{selected.provider}，查询于 {selected.fetched_at:%Y-%m-%d %H:%M} UTC")
        if transport.inbound_selected is not None:
            lines.append(f"- 回程：{transport.inbound_selected.label}")
        lines.append("")
        lines.append(f"选择理由：{transport.selection_reason}")
        if transport.alternatives:
            lines.append("")
            lines.append("其它候选：")
            for option in transport.alternatives:
                lines.append(f"- {option.label} —— {option.selection_reason or '未选中'}")
        lines.append("")

    hotel = plan.hotel
    if hotel is not None and hotel.selected is not None:
        selected = hotel.selected
        lines.append("## 住宿")
        lines.append("")
        lines.append(f"- {selected.name}（{selected.room_type or '房型未返回'}）")
        if selected.price_per_night is not None:
            lines.append(f"  - ¥{selected.price_per_night:g} / 晚（{selected.price_note or '起价'}），查询于 {selected.fetched_at:%Y-%m-%d %H:%M} UTC")
        else:
            lines.append("  - 本次未取得实时价格，住宿费用未计入预算")
        lines.append(f"  - 来源：{selected.provider}")
        lines.append("")
        lines.append(f"选择理由：{hotel.selection_reason}")
        lines.append("")

    lines.append("## 逐日行程")
    lines.append("")
    for day in plan.days:
        title = f"### 第 {day.day_index + 1} 天"
        if day.date:
            title += f" · {day.date.isoformat()}"
        if day.area:
            title += f" · {day.area}"
        lines.append(title)
        lines.append("")
        for item in day.items:
            span = f"{item.start_time or '—'}–{item.end_time or '—'}"
            lines.append(f"- **{span}** {item.name}（{item.type}）")
            if item.travel_from_previous is not None:
                leg = item.travel_from_previous
                leg_bits = [f"{leg.mode}"]
                if leg.duration_minutes is not None:
                    leg_bits.append(f"{leg.duration_minutes} 分钟")
                if leg.distance_meters:
                    leg_bits.append(f"{leg.distance_meters / 1000:.1f} km")
                if leg.estimated_cost is not None:
                    leg_bits.append(f"¥{leg.estimated_cost:g}")
                state_cn = "已核实（高德）" if leg.verified else "本次未取得实时路线，时长仅供参考"
                lines.append(f"  - 从上一站：{'、'.join(leg_bits)}（{state_cn}）")
            if item.price is not None:
                price_label = {"realtime": "实时价", "estimated": "估算", "unknown": "价格未知"}[item.price_type]
                lines.append(f"  - 费用：¥{item.price:g}（{price_label}）")
            if item.trust_score is not None:
                lines.append(f"  - Trust {item.trust_score:.0f} / AdRisk {item.ad_risk or 0:.0f}")
            if item.reason:
                lines.append(f"  - 理由：{item.reason}")
            if item.risk_note:
                lines.append(f"  - 风险：{item.risk_note}")
            if item.evidence_ids:
                lines.append(f"  - 证据：{', '.join(item.evidence_ids)}")
        for note in day.notes or []:
            lines.append(f"- 说明：{note}")
        lines.append("")

    budget = plan.budget
    lines.append("## 预算")
    lines.append("")
    lines.append(f"- 状态：{budget.status}")
    lines.append(f"- 真实花费（Provider 报价）：¥{budget.known_real_cost:,.0f}")
    lines.append(f"- 估算花费：¥{budget.estimated_cost:,.0f}")
    lines.append(f"- 预计总额：¥{budget.projected_total:,.0f}")
    if budget.budget_total is not None:
        lines.append(f"- 预算：¥{budget.budget_total:,.0f}，结余 ¥{(budget.remaining or 0):,.0f}")
    lines.append("")
    lines.append("| 分类 | 金额 | 价格类型 |")
    lines.append("| --- | --- | --- |")
    for category, amount in budget.breakdown.items():
        lines.append(f"| {category} | ¥{amount:,.0f} | {budget.breakdown_price_type.get(category, 'unknown')} |")
    lines.append("")
    for note in budget.price_notes:
        lines.append(f"- {note}")
    for suggestion in budget.optimization_suggestions:
        lines.append(f"- 优化建议：{suggestion}")
    lines.append("")

    if plan.warnings:
        lines.append("## 时间/营业时间问题")
        lines.append("")
        for issue in plan.warnings:
            mark = "已自动修订" if issue.resolved else "未解决"
            lines.append(
                f"- [{mark}] 第 {issue.day_index + 1} 天 {issue.code}：{issue.reason}"
                + (f"（{issue.original_start} → {issue.revised_start}）" if issue.original_start else "")
            )
        lines.append("")

    if critic.get("issues"):
        lines.append("## 审查意见")
        lines.append("")
        for issue in critic["issues"]:
            lines.append(
                f"- [{coerce_str(issue.get('severity'))}] {coerce_str(issue.get('scope'))}："
                f"{coerce_str(issue.get('problem'))} → {coerce_str(issue.get('suggestion'))}"
                f"（依据：{coerce_str(issue.get('evidence'), '无')}）"
            )
        lines.append("")

    if degradations:
        lines.append("## 需要你确认 / 本次未获取到")
        lines.append("")
        for note in dict.fromkeys(degradations):
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("以上价格均为查询时点报价，价格可能变化，请以实际下单页面为准。")
    lines.append("被标为「估算」的金额不是 Provider 报价；被标为「未取得实时路线」的路段时长仅供参考。")
    return "\n".join(lines)


def _write_artifacts(
    *, plan: TripPlan, plan_md: str, audit: dict, output_dir: Path
) -> dict[str, str]:
    target = output_dir / plan.run_id
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for filename, payload in (
        ("plan.json", json.dumps(plan.to_json_dict(), ensure_ascii=False, indent=2)),
        ("plan.md", plan_md),
        ("audit_report.json", json.dumps(audit, ensure_ascii=False, indent=2, default=str)),
    ):
        path = target / filename
        path.write_text(payload, encoding="utf-8")
        written[filename] = str(path)
    return written


def _provider_call_rows(hub: Any) -> list[dict[str, Any]]:
    """Provider 调用账本 → audit / Bad Case / Trace 共用的行结构。

    ``cached`` 与 ``fallback`` 必须带出来（Part C）：它们是"这次调用是复用的、
    还是本次真实发出、还是主源失败后的备选"的唯一事实来源。少带一个，Trace 与
    provider_calls 表就只能靠文案猜。
    """

    rows: list[dict[str, Any]] = []
    for entry in hub.audit_entries() if hub is not None else []:
        rows.append(
            {
                "provider": entry["provider"],
                "tool": entry["tool"],
                "query": entry.get("arguments") or {},
                "status": entry["status"],
                "returned": entry.get("item_count"),
                "duration_ms": entry.get("duration_ms"),
                "fetched_at": entry.get("fetched_at"),
                "source_id": entry.get("source_id"),
                "cached": bool(entry.get("cached")),
                "fallback": bool(entry.get("fallback")),
                "note": "；".join(entry.get("notes") or []) or entry.get("error") or None,
            }
        )
    return rows


def _span_duration_ms(span: Mapping[str, Any]) -> int:
    """一条 span 的真实耗时（毫秒）。优先信 attributes.duration_ms（账本原始值）。"""

    value = (span.get("attributes") or {}).get("duration_ms")
    if isinstance(value, (int, float)):
        return max(0, int(value))
    started, finished = span.get("started_at"), span.get("finished_at")
    if not (started and finished):
        return 0
    try:
        return max(
            0,
            int(
                (datetime.fromisoformat(str(finished)) - datetime.fromisoformat(str(started))).total_seconds()
                * 1000
            ),
        )
    except ValueError:
        return 0


def _performance_summary(state: TravelState, *, metrics: Mapping[str, Any]) -> dict[str, Any]:
    """本次 run 的性能摘要（Part M 的数据侧）。

    为什么同时写进 ``run_metrics`` 之外的 ``metrics.json``：``run_metrics`` 的表结构是
    固定的（12 个列，另一个 agent 在改 Postgres，列不能动），而性能要看的东西远不止
    12 个数（阶段耗时、每个 Provider 的平均/最大耗时、route/poi 调用数、cache hit、
    prefetch 复用、fallback、并发上限快照）。所以把它作为一个 **额外的 JSON 段落**
    落在 ``outputs/<run_id>/metrics.json`` 的 ``performance`` 里：管理端与
    ``scripts/profile_run.py`` 都能直接读到，不需要再去猜。
    """

    run_id = str(state.get("run_id") or "")
    store = state.get("store")
    spans: list[dict[str, Any]] = []
    if store is not None and run_id:
        try:
            spans = list(store.get_trace_spans(run_id))
        except Exception:  # noqa: BLE001 —— 性能摘要是诊断，读不到不能毁掉产物
            spans = []

    stages = [
        {
            "stage_id": span.get("name"),
            "status": span.get("status"),
            "duration_ms": _span_duration_ms(span),
        }
        for span in spans
        if span.get("component") == str(SpanKind.WORKFLOW)
    ]
    stages.sort(key=lambda item: -int(item["duration_ms"]))

    # Provider：本 run 真实发出的调用 + 从 Discovery 过户来的（复用）
    run_rows = _provider_call_rows(state.get("hub"))
    adopted_rows = list(state.get("adopted_calls") or [])
    per_tool: dict[str, dict[str, Any]] = {}
    for entry, reused in [(row, False) for row in run_rows] + [(row, True) for row in adopted_rows]:
        key = f"{entry.get('provider') or '?'}/{entry.get('tool') or '?'}"
        bucket = per_tool.setdefault(
            key,
            {
                "key": key,
                "calls": 0,
                "total_ms": 0,
                "max_ms": 0,
                "reused": 0,
                "cached": 0,
                "fallback": 0,
                "failures": 0,
            },
        )
        duration = entry.get("duration_ms")
        duration_ms = int(duration) if isinstance(duration, (int, float)) else 0
        bucket["calls"] += 1
        bucket["total_ms"] += duration_ms
        bucket["max_ms"] = max(bucket["max_ms"], duration_ms)
        bucket["reused"] += 1 if reused else 0
        bucket["cached"] += 1 if entry.get("cached") else 0
        bucket["fallback"] += 1 if entry.get("fallback") else 0
        bucket["failures"] += 1 if str(entry.get("status")) not in {"OK", "REUSED"} else 0
    providers = sorted(per_tool.values(), key=lambda item: -int(item["total_ms"]))
    for bucket in providers:
        bucket["avg_ms"] = round(bucket["total_ms"] / bucket["calls"]) if bucket["calls"] else 0

    def _tool_calls(tool: str) -> int:
        """某个 tool 的调用次数（键的形状是 ``provider/tool``，按最后一段精确匹配）。"""

        return sum(
            int(bucket["calls"])
            for bucket in providers
            if str(bucket["key"]).rsplit("/", 1)[-1] == tool
        )

    ledger_summary: dict[str, int] = {}
    ledger = state.get("ledger")
    if ledger is not None:
        try:
            ledger_summary = dict(ledger.summary())
        except Exception:  # noqa: BLE001
            ledger_summary = {}

    # 管理端 Run Detail 的「性能摘要」按这几个**扁平键**读取（见 frontend
    # components/admin/run-performance.tsx 的 SUMMARY_MS_KEYS）。这里在同一份 trace 上
    # 汇总，保证"页面上的数"与"trace 里的数"同源；缺的维度不编，直接 0。
    stage_ms = {
        str(item.get("stage_id")): int(item.get("duration_ms") or 0) for item in stages
    }
    llm_ms = sum(
        _span_duration_ms(span) for span in spans if span.get("component") == str(SpanKind.LLM)
    )
    jev_ms = sum(
        _span_duration_ms(span) for span in spans if span.get("component") == str(SpanKind.JEV)
    )
    route_ms = sum(
        _span_duration_ms(span)
        for span in spans
        if span.get("component") == str(SpanKind.TOOL)
        and str((span.get("attributes") or {}).get("tool") or span.get("name")) == "route"
    )
    prefetch_reused = len(adopted_rows)
    cache_hits = sum(1 for row in run_rows if row.get("cached"))
    fallbacks = sum(1 for row in run_rows if row.get("fallback"))

    return {
        "run_id": run_id,
        "total_ms": metrics.get("duration_ms"),
        "total_duration_ms": metrics.get("duration_ms"),
        "stages": stages,
        "providers": providers,
        "provider_calls": len(run_rows),
        "prefetch_reused_calls": prefetch_reused,
        "route_calls": _tool_calls("route"),
        "poi_calls": _tool_calls("search_poi"),
        "poi_detail_calls": _tool_calls("get_poi_detail"),
        "ticket_calls": _tool_calls("search_scenic_tickets"),
        "cache_hits": cache_hits,
        "fallbacks": fallbacks,
        "tool_status": {
            status: sum(1 for row in run_rows if str(row.get("status")) == status)
            for status in sorted({str(row.get("status")) for row in run_rows})
            if status
        },
        "ledger": ledger_summary,
        "llm_calls": metrics.get("llm_calls"),
        "jev_calls": metrics.get("jev_calls"),
        # --- 管理端扁平键（与上面是同一份数字，只是换个读法）---
        "transport_ms": stage_ms.get("search_intercity_transport", 0),
        "hotel_ms": stage_ms.get("search_hotels", 0),
        # Discovery 的两条线在这里（攻略 + 地点抽取）；用户在前端等的是它们的复用效果。
        "discovery_ms": stage_ms.get("search_social_guides", 0)
        + stage_ms.get("extract_and_normalize_places", 0),
        "poi_verify_ms": stage_ms.get("verify_poi_and_routes", 0),
        "route_ms": route_ms,
        "planner_ms": stage_ms.get("score_candidates", 0) + stage_ms.get("build_initial_plan", 0),
        "llm_ms": llm_ms,
        "jev_ms": jev_ms,
        "prefetch_reused": prefetch_reused,
        "fallback_count": fallbacks,
        "verification": {
            "deep_verified_places": len(state.get("routes") or {}) // 2,
            "route_skipped": ledger_summary.get("route_skipped", 0),
            "amap_status": state.get("amap_status"),
        },
        # 并发上限 / 取数上限 / 超时的快照：管理端要能回答"这次是在什么配置下跑的"。
        "config": performance_config_snapshot(),
    }


def _jev_signals(state: TravelState) -> dict[str, str]:
    """Python 对 Jev 决策的复核结论（Bad Case 规则 jev_wrong_choice 等的判据）。"""

    signals: dict[str, str] = {}
    choice = state.get("plan_choice") or {}
    if choice.get("rejected"):
        signals["choice_rejected"] = str(choice["rejected"])
    gate = state.get("gate") or {}
    if gate.get("unnecessary_replan"):
        signals["unnecessary_replan"] = str(gate.get("reason") or "")
    if gate.get("missed_replan"):
        signals["missed_replan"] = str(gate.get("reason") or "")
    return signals


def _detect_and_save_badcases(
    state: TravelState, *, plan: TripPlan, degradations: list[str]
) -> list[dict[str, Any]]:
    """按规则检测本次 run 的 Bad Case 并落库。

    检测失败（或写库失败）绝不能让一次成功的规划变成失败 run —— Bad Case 是"事后复盘"
    的能力，不是行程的一部分。
    """

    try:
        if not current_config().badcase_enabled:
            return []
        ctx = BadCaseContext(
            run_id=state["run_id"],
            plan=plan,
            journey=_user_journey_summary(state, plan),
            provider_calls=_provider_call_rows(state.get("hub")),
            jev_calls=list(state.get("jev_calls") or []),
            degradations=list(dict.fromkeys(degradations)),
            metrics={},
            jev_signals=_jev_signals(state),
            # 让每条 Bad Case 记住"是哪一版代码造成的"，否则回归集无法判断是否还成立。
            introduced_in=travelplan_commit(),
        )
        cases = detect_badcases(ctx)
        state["store"].save_badcases(cases)
        return cases
    except Exception:
        return []


def _write_run_artifacts(
    *,
    run_id: str,
    store: TravelPlanStore,
    output_dir: Path,
    metrics: dict[str, Any],
    badcases: list[dict[str, Any]],
) -> dict[str, str]:
    """写 run 级产物：trace.jsonl / metrics.json / badcases.json。

    这三件必须等**整张图跑完**再写：finalize 节点执行时，它自己的 span 与
    run_metrics 都还没落库，此刻写出来的 trace 会缺最后一步、metrics 会缺总量。

    Trace 用 JSONL（一行一个 span，便于 grep/流式处理）；数据库负责查询与聚合。
    """

    target = output_dir / run_id
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    spans = store.get_trace_spans(run_id)
    trace_path = target / "trace.jsonl"
    trace_path.write_text(
        "".join(json.dumps(span, ensure_ascii=False, default=str) + "\n" for span in spans),
        encoding="utf-8",
    )
    written["trace.jsonl"] = str(trace_path)

    metrics_path = target / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    written["metrics.json"] = str(metrics_path)

    badcases_path = target / "badcases.json"
    badcases_path.write_text(
        json.dumps(
            {"run_id": run_id, "summary": summarize_badcases(badcases), "items": badcases},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    written["badcases.json"] = str(badcases_path)

    # audit_report.json 是"这份 run 有哪些产物"的索引，新增的三件要补进去，
    # 否则前端/人会以为产物还是三件。
    audit_path = target / "audit_report.json"
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            entries = audit.setdefault("artifacts", [])
            known = {item.get("filename") for item in entries if isinstance(item, dict)}
            for filename, label, fmt in (
                ("trace.jsonl", "运行 Trace", "jsonl"),
                ("metrics.json", "运行指标", "json"),
                ("badcases.json", "Bad Case", "json"),
            ):
                if filename not in known:
                    entries.append(
                        {"label": label, "filename": filename, "format": fmt, "available": True}
                    )
            audit_path.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except (OSError, ValueError):
            pass
    return written


#: Provider 调用 → 它属于哪个 workflow 节点。
#: 审计账本里没有阶段信息（真实 Hub 是通用调用器，不知道自己在哪个业务步骤），
#: 所以按"这个工具只会在哪个节点被调用"做静态映射；映射不到就**不挂父 span**，
#: 宁可树少一层，也不编一个假的归属。
PROVIDER_STAGE_MAP: dict[str, str] = {
    "search_trains": "search_intercity_transport",
    "search_flights": "search_intercity_transport",
    "search_hotels": "search_hotels",
    "search_xiaohongshu": "search_social_guides",
    "search_douyin": "search_social_guides",
    "web_search": "search_social_guides",
    "search_poi": "verify_poi_and_routes",
    "poi_detail": "verify_poi_and_routes",
    "geocode": "verify_poi_and_routes",
    "route": "verify_poi_and_routes",
    "search_scenic_tickets": "score_candidates",
}

#: 通过 MCP Server 提供的 Provider。12306 走 `npx 12306-mcp`，不是一个 HTTP 接口。
MCP_BACKED_PROVIDERS: dict[str, str] = {"12306": "railway_12306"}

#: LLM 调用的 tag → 所属节点。tag 带序号时按前缀匹配。
LLM_STAGE_MAP: dict[str, str] = {
    "parse_intent": "parse_intent",
    "query_expansion": "search_social_guides",
    "extract_places": "extract_and_normalize_places",
    "critic": "critic_and_revise",
    "final_answer": "finalize",
}


def _adopt_prefetch_sources(
    store: TravelPlanStore, run_id: str, bundle: Any
) -> list[dict[str, Any]]:
    """把 Discovery 的 Provider 调用写进本次 run 的 sources 表，并返回审计用的行。

    `source_id` 沿用 Discovery 的原值：被复用的 Evidence 引用的就是它，
    换 id 会让证据链断掉。会话与 run 一一对应，所以不会互相顶掉。
    """

    rows: list[dict[str, Any]] = []
    for entry in getattr(bundle, "provider_calls", None) or []:
        source_id = entry.get("source_id")
        try:
            store.save_source(
                run_id,
                {
                    "source_id": source_id,
                    "provider": entry.get("provider"),
                    "source_type": entry.get("source_type"),
                    "source_url": entry.get("source_url"),
                    "query": entry.get("arguments") or {},
                    "fetched_at": entry.get("fetched_at"),
                    "status": entry.get("status"),
                },
            )
        except Exception:  # noqa: BLE001 —— 过户失败只影响可追溯性，不该毁掉规划
            continue
        rows.append(
            {
                "provider": entry.get("provider"),
                "tool": entry.get("tool"),
                "query": entry.get("arguments") or {},
                "status": entry.get("status"),
                "returned": entry.get("item_count"),
                "duration_ms": entry.get("duration_ms"),
                "fetched_at": entry.get("fetched_at"),
                "source_id": source_id,
                "note": "Discovery 阶段已查询，本次 run 复用（未重复调用）",
                "reused": True,
            }
        )
    return rows


def _record_provider_calls(state: TravelState) -> int:
    """把本次 run 的 Provider 调用写进观测账本 `provider_calls`。

    与 `sources` 的分工：sources 是"这次 run 用了什么证据"（业务，带外键）；这张表是
    "谁在什么时候调了哪个数据源、成不成、多快"（观测）。Provider Health 只读后者，
    这样它既能统计正式 Run，也能统计还没有 run 的 Discovery。
    """

    store = state.get("store")
    if store is None:
        return 0
    run_source = str(state.get("source") or "quick")
    source_type = "benchmark" if run_source == "benchmark" else "run"
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(_provider_call_rows(state.get("hub"))):
        rows.append(
            {
                "call_id": f"{state['run_id']}:{index}:{entry.get('source_id')}",
                "provider": entry.get("provider"),
                "tool": entry.get("tool"),
                "status": entry.get("status"),
                "source_type": source_type,
                "source_id": entry.get("source_id"),
                "run_id": state.get("run_id"),
                "session_id": state.get("source_session_id"),
                "fetched_at": entry.get("fetched_at"),
                "duration_ms": entry.get("duration_ms"),
                "returned": entry.get("returned"),
                "error": (entry.get("note") or "")[:400] or None,
                # Part C/E：真实来自 Provider 账本（12306→途牛、TikHub→MediaCrawler），
                # 不再恒为 False —— 恒为 False 会让 Provider Health 的 fallback_count
                # 永远显示 0，等于把"主源经常不可用"这件事藏起来。
                "fallback": bool(entry.get("fallback")),
                "query": {
                    key: value
                    for key, value in (entry.get("query") or {}).items()
                    if str(key).lower() not in {"api_key", "token", "authorization", "secret"}
                },
            }
        )
    try:
        return store.save_provider_calls(rows)
    except Exception:  # noqa: BLE001 —— 观测写不进去不该让规划失败
        return 0


def _llm_stage_for(tag: str) -> str | None:
    if tag in LLM_STAGE_MAP:
        return LLM_STAGE_MAP[tag]
    for prefix, stage in LLM_STAGE_MAP.items():
        if tag.startswith(prefix):
            return stage
    return None


def _emit_run_spans(state: TravelState, *, llm: Any) -> None:
    """把 Provider 与模型调用补成 Trace span（任务 §5 的 provider / tool / mcp / llm）。

    为什么放在 finalize 之后统一补：ProviderHub 与 LLM 的调用账本是**它们自己的**事实，
    已经在运行过程中被完整记下。在这里逐条转录成 span，不需要给每个调用点包一层回调，
    也不会因为某个调用点忘了埋点就丢数据。

    层级：workflow 节点 → provider（或 mcp）→ tool。tool span 的 id 前缀与 Bad Case 的
    ``trace_refs``（``{run}:provider:{provider}:{tool}``）一致，所以从 Bad Case 能直接
    跳到那条 span。

    **时间必须来自账本，不能来自"落盘时刻"**：这段代码在 finalize 之后才跑，用
    ``now_iso()`` 当 finished_at 会让每一条子 span 都"跑到 run 结束"（实测把 5.5s 的
    途牛调用画成 370s）。所以一律用 ``started_at + duration_ms`` 反推。
    """

    run_id = state["run_id"]

    def parent_for(stage: str | None) -> str | None:
        return f"{run_id}:{stage}" if stage else None

    def span_window(entry: Mapping[str, Any]) -> tuple[str, str]:
        """账本条目 → (started_at, finished_at)。

        没有 `duration_ms` 的条目按**零长度**处理，不要退回"当前时刻"。
        为什么：缓存命中、Provider 不可用、工具缺失这些路径根本没发生外部调用，
        它们不记 duration。若按"从现在算起"，这些 span 会被拉成"从调用时刻一直占到最后"
        —— 实测把一条 UNAVAILABLE 记录显示成 396s，看起来像一次超长调用，
        而真正超时的那条（185s TIMEOUT）反而被埋在里面。
        """

        started = str(entry.get("fetched_at") or now_iso())
        duration = entry.get("duration_ms")
        return started, finish_iso(started, duration if isinstance(duration, (int, float)) else 0)

    entries = [*_provider_call_rows(state.get("hub")), *list(state.get("adopted_calls") or [])]
    # 先算每个 Provider / MCP 分组的真实时间窗：分组 span 的时间必须是它**所有**子调用的
    # 并集，否则管理端甘特图上"分组条"会短于它自己的子条。
    group_windows: dict[str, tuple[str, str]] = {}
    for entry in entries:
        provider = str(entry.get("provider") or "unknown")
        server = MCP_BACKED_PROVIDERS.get(provider)
        group_id = span_id(run_id, SpanKind.MCP if server else SpanKind.PROVIDER, server or provider)
        started, finished = span_window(entry)
        window = group_windows.get(group_id)
        if window is None:
            group_windows[group_id] = (started, finished)
        else:
            group_windows[group_id] = (min(window[0], started), max(window[1], finished))

    emitted_groups: set[str] = set()
    for entry in entries:
        provider = str(entry.get("provider") or "unknown")
        tool = str(entry.get("tool") or "unknown")
        stage = PROVIDER_STAGE_MAP.get(tool)
        server = MCP_BACKED_PROVIDERS.get(provider)
        group_component = SpanKind.MCP if server else SpanKind.PROVIDER
        group_name = server or provider
        group_id = span_id(run_id, group_component, group_name)
        started, finished = span_window(entry)
        reused = bool(entry.get("reused"))
        cached = bool(entry.get("cached"))
        fallback = bool(entry.get("fallback"))
        status = str(entry.get("status") or "")
        if group_id not in emitted_groups:
            emitted_groups.add(group_id)
            group_started, group_finished = group_windows.get(group_id, (started, finished))
            _record_subspan(
                state,
                component=group_component,
                name=group_name,
                status="SUCCESS",
                started_at=group_started,
                finished_at=group_finished,
                attributes={
                    "provider": provider,
                    "transport": "mcp stdio" if server else "http",
                    "calls": sum(
                        1
                        for item in entries
                        if str(item.get("provider") or "unknown") == provider
                    ),
                },
                parent_span_id=parent_for(stage),
            )
        _record_subspan(
            state,
            component=SpanKind.TOOL,
            name=tool,
            # 单个 tool 的成败就是它自己的状态；成组状态由管理端按子 span 聚合。
            status="SUCCESS" if status == "OK" else ("REUSED" if reused else "WARNING"),
            started_at=started,
            finished_at=finished,
            # Part C：管理端与 profiler 要能直接回答"哪些是复用的、哪些是本次调的、
            # 哪些走了 fallback"，所以四个标记必须落在 span 属性上（不是只写进文案）。
            attributes={
                "provider": provider,
                "tool": tool,
                "status": status,
                "cache_hit": cached,
                "prefetch_reused": reused,
                "provider_called": not (cached or reused),
                "fallback_query": fallback,
                # 保留旧字段名，管理端与既有 Bad Case 规则读的是它。
                "reused_from_discovery": reused,
                "returned": entry.get("returned"),
                "duration_ms": entry.get("duration_ms"),
                "query": entry.get("query"),
                "source_id": entry.get("source_id"),
                "note": entry.get("note"),
            },
            # 每次调用一条独立的 span：同名 tool 的多次调用如果共用一个 span_id，
            # 后写的会把先写的顶掉（INSERT OR REPLACE），于是 16 次 search_poi 在
            # Trace 里只剩 1 条 —— profiler 数出来的 route/poi 调用数全是错的。
            suffix=str(entry.get("source_id") or ""),
            parent_span_id=group_id,
            error=None if status == "OK" else str(entry.get("note") or status),
        )

    for entry in llm.audit_entries() if llm is not None else []:
        tag = str(entry.get("tag") or "unnamed")
        # LLM 账本现在带真实起止时刻（app/llm.py:invoke）；没有就按 duration 反推。
        llm_started = str(entry.get("started_at") or start_iso(entry.get("finished_at"), entry.get("duration_ms")))
        llm_finished = str(entry.get("finished_at") or finish_iso(llm_started, entry.get("duration_ms")))
        _record_subspan(
            state,
            component=SpanKind.LLM,
            name=tag,
            status="SUCCESS" if entry.get("status") == "OK" else "WARNING",
            started_at=llm_started,
            finished_at=llm_finished,
            attributes={
                "tag": tag,
                "model": entry.get("model"),
                "status": entry.get("status"),
                "duration_ms": entry.get("duration_ms"),
                "chars": entry.get("chars"),
                "error": entry.get("error"),
            },
            parent_span_id=parent_for(_llm_stage_for(tag)),
            error=entry.get("error"),
        )


def node_finalize(state: TravelState) -> dict:
    store = state["store"]
    llm = state["llm"]
    run_id = state["run_id"]
    decisions = list(state.get("decisions") or [])
    plan = _assemble_plan(state, decisions=decisions)

    # 可行性修订可能删掉/挪动了 item，所以预算必须按**最终**行程重算一遍，
    # 否则 plan.json 里会出现"预算包含了一个已经被删掉的景点门票"这种自相矛盾。
    recomputed = planner.build_budget(
        plan.intent,
        state.get("transport_plan"),
        plan.hotel.selected if plan.hotel else None,
        plan.days,
        state.get("ticket_prices") or {},
        city=_destination(plan.intent),
        day_count=len(plan.days),
        hotel_alternatives=list(plan.hotel.alternatives) if plan.hotel else (),
    )
    budget_note = ""
    if state.get("budget") is not None and abs(recomputed.projected_total - plan.budget.projected_total) >= 1:
        budget_note = (
            f"可行性修订后按最终行程重算预算：¥{plan.budget.projected_total:,.0f} → ¥{recomputed.projected_total:,.0f}"
        )
    plan.budget = recomputed

    degradations = list(state.get("degradations") or [])
    critic = state.get("critic") or {}

    prose_result = llm.invoke(
        FINAL_ANSWER_PROMPT,
        _digest_json(plan),
        tag="final_answer",
    )
    prose_ok = prose_result.ok and bool(prose_result.text.strip())
    new_degradations: list[str] = []
    if prose_ok:
        prose_note = f"模型成文（{prose_result.model}）"
    else:
        prose_note = "模型成文不可用，plan.md 全部由代码生成"
        new_degradations.append(degraded_note(prose_result, "plan.md 只包含代码生成的结构化明细"))
        degradations.extend(new_degradations)

    deterministic = _render_markdown(plan, degradations=degradations, critic=critic)
    if prose_ok:
        body = deterministic.split("\n", 1)[1].lstrip("\n") if "\n" in deterministic else deterministic
        plan_md = (
            "# 行程说明\n\n"
            + prose_result.text.strip()
            + "\n\n---\n\n## 结构化明细（以下内容由代码直接生成，数字未经模型改写）\n\n"
            + body
        )
    else:
        plan_md = deterministic

    store.save_decisions_bulk(run_id, decisions)
    store.save_plan(plan, plan_md)
    store.finish_run(run_id, STATUS_COMPLETED)

    # 把 Provider / 模型调用转录成 span（component=provider|mcp|tool|llm）。放在这里是因为
    # 两者的调用账本此时才完整；Bad Case 的 trace_refs 也在这之后才落库，指向的 span 一定存在。
    _emit_run_spans(state, llm=llm)
    _record_provider_calls(state)
    if state.get("prefetch") is not None:
        journey = _user_journey_summary(state, plan)
        _record_subspan(
            state,
            component=SpanKind.PLANNER,
            name="discovery_handoff",
            status="SUCCESS",
            started_at=now_iso(),
            attributes={
                "grace_waited_ms": journey.get("grace_waited_ms"),
                # 一眼看出每条线是复用的还是本次补查的
                **{key: (info or {}).get("handoff") for key, info in (journey.get("discovery") or {}).items()},
            },
            parent_span_id=f"{run_id}:finalize",
        )
    _record_subspan(
        state,
        component=SpanKind.STORE,
        name="persist",
        status="SUCCESS",
        started_at=now_iso(),
        attributes={
            "decisions": len(decisions),
            "days": len(plan.days),
            "plan_items": sum(len(day.items) for day in plan.days),
        },
        parent_span_id=f"{run_id}:finalize",
    )

    # Bad Case 检测放在**所有事实都已定型之后**：plan / degradations / Jev 记录 / Provider
    # 账本此时才是最终值，规则判断不会因为流程后面又改动而失效。
    badcases = _detect_and_save_badcases(state, plan=plan, degradations=degradations)
    badcase_counts = summarize_badcases(badcases)

    audit = _build_audit(state, plan=plan, decisions=decisions, degradations=degradations, prose_note=prose_note)
    outputs = _write_artifacts(
        plan=plan, plan_md=plan_md, audit=audit, output_dir=Path(state["output_dir"])
    )

    steps = [
        f"决策链 {len(decisions)} 条已落库（plan / decisions / sources / evidence / places）",
        f"成文方式：{prose_note}",
        f"产物：{', '.join(outputs)}",
        f"Bad Case：{badcase_counts['total']} 条（高 {badcase_counts['high']} / 中 {badcase_counts['medium']} / 低 {badcase_counts['low']}）",
    ]
    if budget_note:
        steps.append(budget_note)

    summary = f"产出 plan.json / plan.md / audit_report.json（{len(decisions)} 条决策、{badcase_counts['total']} 条 Bad Case）"
    return {
        "plan": plan,
        "plan_md": plan_md,
        "outputs": outputs,
        "status": STATUS_COMPLETED,
        "audit": audit,
        "badcases": badcases,
        "degradations": new_degradations,
        "timeline": [_stamp("finalize", summary)],
        "stages": [
            _stage(
                "finalize",
                "成文与归档",
                summary,
                steps,
                决策数=len(decisions),
                BadCase数=badcase_counts["total"],
            )
        ],
    }


# ==================================================
# audit_report.json
# ==================================================


def _build_audit(
    state: TravelState,
    *,
    plan: TripPlan,
    decisions: list[Decision],
    degradations: list[str],
    prose_note: str,
) -> dict[str, Any]:
    """组装 audit_report.json（前端 AuditDrawer 直接消费的结构）。

    它比工程 trace 收敛：只保留"这次查了什么、谁给的、信不信、用了没有"这条链，
    不把内部 state 和 prompt 全文倒出去。
    """
    hub = state.get("hub")
    llm = state.get("llm")
    # 本次自己调的 + Discovery 过户来的（后者标 reused，避免读审计的人以为数据是凭空来的）
    provider_calls = _provider_call_rows(hub) + [
        {key: value for key, value in row.items() if key != "reused"}
        for row in (state.get("adopted_calls") or [])
    ]
    jev_calls = list(state.get("jev_calls") or [])

    sources = plan.sources
    stages = list(state.get("stages") or [])
    artifacts = [
        {"label": "计划数据", "filename": "plan.json", "format": "json", "available": True},
        {"label": "行程说明", "filename": "plan.md", "format": "markdown", "available": True},
        {"label": "规划依据", "filename": "audit_report.json", "format": "json", "available": True},
    ]
    stages.append(
        _stage(
            "summary",
            "汇总",
            f"共调用外部数据源 {len(provider_calls)} 次、模型 {len(llm.calls) if llm else 0} 次；"
            f"产出 {len(plan.days)} 天行程、{len(decisions)} 条决策",
            [
                f"数据源调用 {len(provider_calls)} 次（成功 {sum(1 for call in provider_calls if call['status'] == 'OK')} 次）",
                f"模型调用 {len(llm.calls) if llm else 0} 次（成功 {sum(1 for call in (llm.calls if llm else []) if call.ok)} 次）",
                f"成文方式：{prose_note}",
                f"来源条目 {len(sources)} 条，证据 {len(state.get('evidences') or [])} 条",
            ],
            数据源调用=len(provider_calls),
            模型调用=len(llm.calls) if llm else 0,
        )
    )

    timeline = list(state.get("timeline") or [])
    timeline.append(_stamp("summary", f"run {plan.run_id} 完成，状态 {state.get('status') or STATUS_COMPLETED}"))

    return {
        "run_id": plan.run_id,
        "query": plan.query,
        "generated_at": utcnow().isoformat(),
        "stages": stages,
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
        "provider_calls": provider_calls,
        "llm_calls": llm.audit_entries() if llm is not None else [],
        # Jev 调用明细进 audit：管理端 Run Detail 的 "Jev Calls" 直接读它，
        # 每条都带 decision_type / confidence / latency / fallback reason / quota。
        "jev_calls": jev_calls,
        "plan_choice": state.get("plan_choice") or {},
        "tradeoff": state.get("tradeoff") or {},
        "quality_gate": state.get("gate") or {},
        "quality": (state.get("quality").to_dict() if state.get("quality") is not None else {}),
        "user_journey": _user_journey_summary(state, plan),
        "timeline": timeline,
        "artifacts": artifacts,
        "degradations": list(dict.fromkeys(degradations)),
        "evidence_summary": _evidence_summary(state, plan),
    }


def _user_journey_summary(state: TravelState, plan: TripPlan) -> dict[str, Any]:
    """这次 run 的"用户前置选择"上下文（管理端 Run Detail 与 Bad Case 共用）。

    `prefetch_reused` 是**用证据推出来的**，不是标记位：如果这次 run 复用了 Discovery，
    它就不应该再自己打一遍交通/酒店/攻略。两边都发生 = 没复用（这正是要暴露的问题）。
    """

    intent = plan.intent
    selections = dict(intent.place_selections or {})
    bundle = state.get("prefetch")
    planned_ids = [item.place_id for day in plan.days for item in day.items if item.place_id]

    bundle_had: set[str] = set()
    if bundle is not None:
        if bundle.outbound or bundle.inbound:
            bundle_had.add("transport")
        if bundle.hotels:
            bundle_had.add("hotels")
        if bundle.evidences:
            bundle_had.add("social")
        if bundle.places:
            bundle_had.add("places")
    ran_itself = {
        str(entry.get("tool"))
        for entry in _provider_call_rows(state.get("hub"))
    }
    # 这些工具一旦出现在本次 run 的账本里，就说明它自己又查了一遍。
    self_queried = {
        "transport": bool({"search_trains", "search_flights"} & ran_itself),
        "hotels": "search_hotels" in ran_itself,
        "social": bool({"search_xiaohongshu", "search_douyin", "web_search"} & ran_itself),
        "places": "search_poi" in ran_itself,
    }
    reused = {
        key: (key in bundle_had and not self_queried[key]) for key in ("transport", "hotels", "social", "places")
    }
    # 每条线的交接结果，三种之一：
    #   reused          —— 用了 Discovery 已经查好的，本次没有重复打 Provider
    #   fallback_query  —— 本次自己补查了，并且查到了东西
    #   unavailable     —— 本次自己补查了，但仍然没有可用结果（要如实说出来）
    stage_info = dict(getattr(bundle, "discovery", None) or {}) if bundle is not None else {}
    resolved = {
        "transport": bool(plan.transport is not None and plan.transport.selected is not None),
        "hotels": bool(plan.hotel is not None and plan.hotel.selected is not None),
        "social": bool(state.get("evidences")),
        "places": bool(state.get("places")),
    }
    handoff: dict[str, Any] = {}
    for key, label in (
        ("transport", "交通"),
        ("hotels", "酒店"),
        ("social", "攻略"),
        ("places", "地点"),
    ):
        if reused[key]:
            status = "reused"
        elif resolved[key]:
            status = "fallback_query"
        else:
            status = "unavailable"
        handoff[key] = {
            "label": label,
            "handoff": status,
            "prefetch_available": key in bundle_had,
            "queried_in_run": self_queried[key],
            "resolved": resolved[key],
            **{k: v for k, v in (stage_info.get(key) or {}).items() if k in ("status", "result_count", "duration_ms", "degraded")},
        }
    return {
        # 每个阶段的交接状态（管理端与 Bad Case 都看这个）
        "discovery": handoff,
        "grace_waited_ms": int(getattr(bundle, "grace_waited_ms", 0) or 0) if bundle is not None else 0,
        "source": state.get("source") or "quick",
        "source_session_id": state.get("source_session_id"),
        "transport_mode": intent.transport_mode,
        "transport_priority": intent.transport_priority,
        "hotel_priority": intent.hotel_priority,
        "pace": intent.pace,
        "place_selections": {
            "must": sum(1 for value in selections.values() if str(value).upper() == "MUST"),
            "want": sum(1 for value in selections.values() if str(value).upper() == "WANT"),
            "reject": sum(1 for value in selections.values() if str(value).upper() == "REJECT"),
        },
        "prefetch_available": sorted(bundle_had),
        "prefetch_reused": reused,
        "prefetch_reused_any": any(reused.values()),
        "prefetch_status": (bundle.discovery_status if bundle is not None else None),
        # 分阶段原始状态（status/duration/result_count…）保留在 prefetch_stages 下，
        # "discovery" 留给交接结论（reused/fallback_query/unavailable），避免同名两义。
        "prefetch_stages": dict(getattr(bundle, "discovery", None) or {}) if bundle is not None else {},
        "rejected_in_plan": selection.rejected_place_intruders(planned_ids, selections),
        "must_missing": selection.must_place_shortfall(
            planned_ids, selections, list(state.get("places") or [])
        ),
    }


def _evidence_summary(state: TravelState, plan: TripPlan) -> dict[str, int]:
    """给前端的可信度汇总（FRONTEND_DESIGN §9）：三个数，全部来自已算好的结果。"""
    places = list(state.get("places") or [])
    scheduled = {
        item.place_id for day in plan.days for item in day.items if item.place_id is not None
    }
    return {
        "sources_used": len(plan.sources),
        "places_verified": sum(1 for place in places if place.amap_verified),
        "low_trust_filtered": max(0, len(state.get("candidate_scores") or {}) - len(scheduled)),
    }


# ==================================================
# 图
# ==================================================


def _instrument_node(stage_id: str, node: Any) -> Any:
    """为固定节点补业务级进度与 span，而不引入第二个 Workflow Runtime。

    SuperHarness 仍负责跨 Agent 的 EventBus/Trace；这层只写 UI 轮询需要的事实。
    即使观测写库失败，也绝不能让旅行规划本身失败。
    """

    def wrapped(state: TravelState) -> dict:
        hook = state.get("progress_hook")
        started_at = utcnow().isoformat()
        started = time.perf_counter()
        if hook is not None:
            hook(stage_id, "RUNNING", f"正在执行 {stage_id}", {}, started_at, None)
        try:
            result = node(state)
        except Exception as exc:
            if hook is not None:
                hook(
                    stage_id,
                    "FAILED",
                    f"{stage_id} 执行失败",
                    {"duration_ms": round((time.perf_counter() - started) * 1000)},
                    started_at,
                    utcnow().isoformat(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise

        stage = next(
            (entry for entry in (result.get("stages") or []) if entry.get("id") == stage_id),
            {},
        )
        facts = {
            str(item.get("label")): item.get("value")
            for item in (stage.get("stats") or [])
            if isinstance(item, dict) and item.get("label")
        }
        facts["duration_ms"] = round((time.perf_counter() - started) * 1000)
        has_warning = bool(result.get("degradations"))
        if hook is not None:
            hook(
                stage_id,
                "WARNING" if has_warning else "SUCCESS",
                str(stage.get("summary") or f"{stage_id} 已完成"),
                facts,
                started_at,
                utcnow().isoformat(),
            )
        return result

    return wrapped


def build_travel_graph() -> CompiledStateGraph:
    """编译 12 步固定流程（PRD §25）。

    唯一一处分支：意图解析后如果连目的地都没有，直接结束并让用户补充信息 ——
    这一步不走 Agent，也不猜一个城市继续跑。
    """
    graph = StateGraph(TravelState)
    graph.add_node("parse_intent", _instrument_node("parse_intent", node_parse_intent))
    graph.add_node("search_intercity_transport", _instrument_node("search_intercity_transport", node_search_transport))
    graph.add_node("search_hotels", _instrument_node("search_hotels", node_search_hotels))
    graph.add_node("search_social_guides", _instrument_node("search_social_guides", node_search_social))
    graph.add_node("extract_and_normalize_places", _instrument_node("extract_and_normalize_places", node_extract_places))
    graph.add_node("verify_poi_and_routes", _instrument_node("verify_poi_and_routes", node_verify_poi_and_routes))
    graph.add_node("score_candidates", _instrument_node("score_candidates", node_score_candidates))
    graph.add_node("build_initial_plan", _instrument_node("build_initial_plan", node_build_plan))
    graph.add_node("check_budget", _instrument_node("check_budget", node_check_budget))
    graph.add_node("check_feasibility", _instrument_node("check_feasibility", node_check_feasibility))
    graph.add_node("critic_and_revise", _instrument_node("critic_and_revise", node_critic_revise))
    graph.add_node("finalize", _instrument_node("finalize", node_finalize))

    graph.set_entry_point("parse_intent")
    graph.add_conditional_edges(
        "parse_intent", _after_intent, {"continue": "search_intercity_transport", "stop": END}
    )
    graph.add_edge("search_intercity_transport", "search_hotels")
    graph.add_edge("search_hotels", "search_social_guides")
    graph.add_edge("search_social_guides", "extract_and_normalize_places")
    graph.add_edge("extract_and_normalize_places", "verify_poi_and_routes")
    graph.add_edge("verify_poi_and_routes", "score_candidates")
    graph.add_edge("score_candidates", "build_initial_plan")
    graph.add_edge("build_initial_plan", "check_budget")
    graph.add_edge("check_budget", "check_feasibility")
    graph.add_edge("check_feasibility", "critic_and_revise")
    graph.add_edge("critic_and_revise", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


_TRAVEL_GRAPH: CompiledStateGraph | None = None


def travel_graph() -> CompiledStateGraph:
    """进程内复用同一个编译结果：compile() 不便宜，而且图是无状态的。"""
    global _TRAVEL_GRAPH
    if _TRAVEL_GRAPH is None:
        _TRAVEL_GRAPH = build_travel_graph()
    return _TRAVEL_GRAPH


# ==================================================
# 对外入口
# ==================================================


@dataclass(slots=True)
class RunResult:
    """一次规划的返回值。`status` 只有三种，前端/CLI 按它分支即可。"""

    run_id: str
    status: str
    plan: TripPlan | None = None
    plan_md: str = ""
    audit: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def clarification(self) -> str:
        return self.error or ""


def execute_travel_run(
    query: str,
    *,
    user_id: str = "anonymous",
    thread_id: str | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    debug: bool = False,
    store: TravelPlanStore | None = None,
    model: Any | None = None,
    hub: ProviderHub | None = None,
    llm: LLM | None = None,
    jev: JevClient | None = None,
    run_id: str | None = None,
    emit: Any | None = None,
    intent: TripIntent | None = None,
    prefetch: Any | None = None,
    source: str = "quick",
    source_session_id: str | None = None,
) -> RunResult:
    """跑完一次完整规划：12 步固定流程 + 落库 + 写 `outputs/<run_id>/` 产物。

    `store` / `model` / `hub` / `llm` / `jev` 允许注入，是为了让单测与 Benchmark 能在
    不联网、不写真实库的前提下跑整条流程；生产路径不传这几个参数。

    `emit` 是 SuperHarness 原生 Observability 的上报口（START.md §9：不要重造），
    只影响日志，不参与任何业务判断。
    """
    resolved_run_id = run_id or new_run_id()
    resolved_store = store or TravelPlanStore()
    resolved_store.create_run(
        resolved_run_id,
        user_id=user_id,
        original_query=query,
        source=source,
        source_session_id=source_session_id,
    )
    started = time.perf_counter()

    def progress_hook(
        stage_id: str,
        status: str,
        message: str,
        facts: dict[str, Any],
        started_at: str | None,
        finished_at: str | None,
        *,
        error: str | None = None,
    ) -> None:
        """把已经发生的节点变化写进轮询状态；观测失败不影响规划。"""
        try:
            resolved_store.update_stage(
                resolved_run_id,
                stage_id,
                status,
                message=message,
                facts=facts,
                started_at=started_at,
                finished_at=finished_at,
            )
            resolved_store.save_trace_span(
                resolved_run_id,
                f"{resolved_run_id}:{stage_id}",
                component="workflow",
                name=stage_id,
                status=status,
                started_at=started_at or utcnow().isoformat(),
                finished_at=finished_at,
                attributes=facts,
                error=error,
            )
        except Exception:
            # 可观测性是诊断能力，不能反过来把一趟原本可用的旅行规划打成失败。
            return

    def record_span(
        *,
        component: str,
        name: str,
        status: str,
        started_at: str,
        finished_at: str,
        attributes: dict[str, Any],
        parent_span_id: str | None = None,
        error: str | None = None,
        suffix: str | None = None,
    ) -> None:
        """业务级子 span（planner / jev / llm / provider / budget / feasibility / critic / store）。

        span_id 由 (component, name[, suffix]) 决定，因此 Bad Case 能引用一个**稳定可查**的
        id，而不是一段描述性文字。``suffix`` 用于"同名多次"的 span（同一个 tool 的第 N 次调用、
        多个 Jev 问题）：span_id 是主键，不带 suffix 的重复写入会把前一条顶掉。
        """

        try:
            resolved_store.save_trace_span(
                resolved_run_id,
                span_id(resolved_run_id, component, name, suffix=suffix),
                component=str(component),
                name=name,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                parent_span_id=parent_span_id,
                attributes=attributes,
                error=error,
            )
        except Exception:
            return

    def persist_metrics(
        *, result_status: str, degradations: list[str], final: dict[str, Any]
    ) -> dict[str, Any]:
        """汇总现有 LLM / Provider / Jev 留痕；取不到的 cost/token 保持空而非伪造为 0。

        返回写进库的那份 metrics，供 run 级产物（metrics.json）复用同一份数字。
        """

        usages = [call.usage or {} for call in getattr(resolved_llm, "calls", [])]
        # "拿到了 usage 对象"不等于"里面有数字"：假件/部分 Provider 会返回空 usage。
        # 全是空的时候必须写 None（不知道），不能写 0 —— 0 会被读成"模型没消耗 token"。
        has_usage = any(bool(usage) for usage in usages)
        input_tokens = sum(int(usage.get("input_tokens") or 0) for usage in usages)
        output_tokens = sum(int(usage.get("output_tokens") or 0) for usage in usages)
        cached_tokens = sum(int(usage.get("cached_tokens") or 0) for usage in usages)
        provider_calls = resolved_hub.audit_entries()
        jev_records = [call for call in (final.get("jev_calls") or []) if call.get("attempted")]
        badcases = list(final.get("badcases") or [])
        config = current_config()
        price_in = config.model_price_input_per_million
        price_out = config.model_price_output_per_million
        price_cached = config.model_price_cached_per_million
        cost: float | None = None
        if has_usage and price_in is not None and price_out is not None:
            # 单价是**用户填的估计值**，不是 Provider 回执 —— 产物里标 cost_source=user_price。
            cached_price = price_cached if price_cached is not None else price_in
            cost = round(
                input_tokens / 1_000_000 * price_in
                + output_tokens / 1_000_000 * price_out
                + cached_tokens / 1_000_000 * cached_price,
                6,
            )
        metrics: dict[str, Any] = {
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "input_tokens": input_tokens if has_usage else None,
            "output_tokens": output_tokens if has_usage else None,
            "cached_tokens": cached_tokens if has_usage else None,
            "total_tokens": (input_tokens + output_tokens) if has_usage else None,
            "llm_calls": len(getattr(resolved_llm, "calls", [])),
            # 只统计**真实发出**的 Jev 调用：SKIPPED / 未配置 不是调用，记 0 才不会高估用量。
            "jev_calls": len(jev_records),
            "tool_calls": len(_provider_call_rows(resolved_hub)),
            # 复用的调用单列：把它算进 tool_calls 会让"这次 run 花了多少"虚高。
            "adopted_tool_calls": len(adopted_calls),
            "provider_failures": sum(1 for call in provider_calls if call.get("status") != "OK"),
            "badcase_count": len(badcases),
            # 没配单价、或拿不到 token 时 cost 必须为 null（不猜）。
            "cost": cost,
            "cost_source": "user_price" if cost is not None else None,
            "cost_breakdown": {
                "input_tokens": input_tokens if has_usage else None,
                "output_tokens": output_tokens if has_usage else None,
                "cached_tokens": cached_tokens if has_usage else None,
                "input_price_per_million": price_in,
                "output_price_per_million": price_out,
                "cached_price_per_million": price_cached,
            },
        }
        # Part H：把性能摘要落到两个**已经存在**的出口，不需要新接口：
        #   1. `metrics["performance_summary"]` → outputs/<run_id>/metrics.json（机器可读）；
        #   2. 一条 `performance_summary` span → GET /admin/runs/{id} 的 `trace`（管理端
        #      Run Detail 直接就能看到，不需要新增 endpoint）。
        # 字段名对齐管理端「性能摘要」读的键（transport_ms / provider_calls / cache_hits…），
        # 所以页面上的数字与这里、与 trace 里的数字是同一份来源，不会各说各话。
        try:
            summary = _performance_summary(state=final if final else state, metrics=metrics)
            summary["total_ms"] = metrics.get("duration_ms")
            summary["total_duration_ms"] = metrics.get("duration_ms")
            metrics["performance_summary"] = summary
            resolved_store.save_trace_span(
                resolved_run_id,
                f"{resolved_run_id}:store:performance_summary",
                component=str(SpanKind.STORE),
                name="performance_summary",
                status="SUCCESS",
                started_at=start_iso(utcnow().isoformat(), metrics.get("duration_ms")),
                finished_at=utcnow().isoformat(),
                attributes=summary,
            )
        except Exception:  # noqa: BLE001 —— 摘要是诊断，写不进去不能影响 run 的结论
            pass
        try:
            resolved_store.save_run_metrics(resolved_run_id, metrics)
            if result_status == STATUS_COMPLETED and degradations:
                # 保留 runs.status="completed" 兼容旧 API，只提升管理端状态为 DEGRADED。
                resolved_store.set_progress_status(
                    resolved_run_id, "DEGRADED", "规划完成，但部分能力已降级"
                )
        except Exception:
            pass
        return metrics

    def write_artifacts(final: dict[str, Any], metrics: dict[str, Any]) -> dict[str, str]:
        """run 级产物落盘。

        只有**产出了 plan 的 run** 才建 `outputs/<run_id>/` 目录：这个目录的语义是
        "这次跑出了一份行程"。要求澄清或失败的 run 不会伪造一个产物目录 —— 它们的
        trace/metrics 仍在数据库里，管理端照常可查。
        """

        if not final.get("plan"):
            return {}
        try:
            return _write_run_artifacts(
                run_id=resolved_run_id,
                store=resolved_store,
                output_dir=Path(output_dir),
                metrics=metrics,
                badcases=list(final.get("badcases") or []),
            )
        except Exception:
            return {}

    owns_hub = hub is None
    resolved_hub = hub or ProviderHub(
        run_id=resolved_run_id,
        store=resolved_store,
        mcp_servers=default_mcp_servers(),
        emit=emit,
    )
    resolved_llm = llm or LLM.from_env(model, emit=emit)
    # Jev 客户端总是构造：它自己会在"未配置 / 开关关闭"时返回 SKIPPED，
    # 所以调用方不需要知道 Jev 是否可用，也永远不会因为它不可用而拿不到行程。
    resolved_jev = jev or JevClient()

    # 引导式：把 Discovery 的调用"过户"到本 run，再跑图。
    # 必须在任何节点写 evidence/place 之前做，否则外键会挡住复用（生产路径必踩）。
    adopted_calls: list[dict[str, Any]] = []
    if prefetch is not None:
        adopted_calls = _adopt_prefetch_sources(resolved_store, resolved_run_id, prefetch)

    # 复用账本在这里**只建一次**并作为句柄传进图（见 TravelState.ledger 的注释）。
    # Discovery 的调用也顺手播种进去：正式流程再问同一个键时会被记成 duplicate_query
    # 而不是又打一遍 Provider。
    ledger = CallLedger(scope=resolved_run_id)
    if adopted_calls:
        ledger.seed(adopted_calls)

    state: TravelState = {
        "run_id": resolved_run_id,
        "query": query,
        "user_id": user_id,
        "debug": debug,
        "output_dir": str(output_dir),
        "store": resolved_store,
        "hub": resolved_hub,
        "llm": resolved_llm,
        "jev": resolved_jev,
        "record_span": record_span,
        "intent_override": intent,
        "prefetch": prefetch,
        #: 从 Discovery 过户过来的调用（审计里要标明"复用"，不是本次真的调了）
        "adopted_calls": adopted_calls,
        "ledger": ledger,
        "source": source,
        "source_session_id": source_session_id,
        "progress_hook": progress_hook,
        "decisions": [],
        "timeline": [],
        "stages": [],
        "degradations": [],
        "jev_calls": [],
    }

    try:
        final = travel_graph().invoke(state)
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何未预期异常都要如实记录并落一个 failed run
        resolved_store.finish_run(resolved_run_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
        metrics = persist_metrics(result_status=STATUS_FAILED, degradations=[], final={})
        outputs = write_artifacts({}, metrics)
        return RunResult(
            run_id=resolved_run_id,
            status=STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}",
            outputs=outputs,
        )
    finally:
        if owns_hub:
            resolved_hub.close()

    status = final.get("status") or STATUS_FAILED
    if status == STATUS_NEEDS_CLARIFICATION:
        resolved_store.finish_run(resolved_run_id, STATUS_NEEDS_CLARIFICATION)
        degradations = list(final.get("degradations") or [])
        metrics = persist_metrics(result_status=status, degradations=degradations, final=final)
        outputs = write_artifacts(final, metrics)
        return RunResult(
            run_id=resolved_run_id,
            status=STATUS_NEEDS_CLARIFICATION,
            error=final.get("clarify"),
            outputs={**dict(final.get("outputs") or {}), **outputs},
            degradations=degradations,
        )

    plan = final.get("plan")
    if status != STATUS_COMPLETED:
        # 图走到这里说明它既没 finalize 也没要求澄清：如实落一个 failed run，
        # 而不是把中间态原样回给调用方 —— 否则 runs 行会永远停在 running。
        resolved_store.finish_run(resolved_run_id, STATUS_FAILED, error=f"流程没有正常收尾（图返回 status={status}）")
        degradations = list(final.get("degradations") or [])
        metrics = persist_metrics(result_status=STATUS_FAILED, degradations=degradations, final=final)
        outputs = write_artifacts(final, metrics)
        return RunResult(
            run_id=resolved_run_id,
            status=STATUS_FAILED,
            error=f"流程没有正常收尾（图返回 status={status}）",
            outputs={**dict(final.get("outputs") or {}), **outputs},
            degradations=degradations,
        )

    degradations = list(final.get("degradations") or [])
    metrics = persist_metrics(result_status=status, degradations=degradations, final=final)
    outputs = write_artifacts(final, metrics)
    return RunResult(
        run_id=resolved_run_id,
        status=status,
        plan=plan,
        plan_md=final.get("plan_md") or "",
        audit=final.get("audit") or {},
        outputs={**dict(final.get("outputs") or {}), **outputs},
        degradations=degradations,
    )


def plan_payload(plan: TripPlan, store: TravelPlanStore | None = None) -> dict[str, Any]:
    """给前端的 TripPlan JSON：在 plan.json 之上补 evidence 与 evidence_summary。

    `plan.json` 本身保持 PRD §28 的形状不变（sources 已经在里面），evidence 明细
    单独通过 store 取 —— 因为它是"证据库"的内容，不是"计划"的内容。
    """
    payload = plan.to_json_dict()
    if store is not None:
        payload["evidence"] = [
            {
                "id": row["evidence_id"],
                "source_type": row["source_type"],
                "provider": row["provider"],
                "source_id": row["source_id"],
                "source_url": row["source_url"],
                "title": row["title"],
                "author": row["author"],
                "published_at": row["published_at"],
                "fetched_at": row["fetched_at"],
                "text": row["text"],
                "place_mentions": row["place_mentions"],
                "specific_dishes": row["specific_dishes"],
                "raw_metrics": row["raw_metrics"],
            }
            for row in store.get_evidence(plan.run_id)
        ]
    return payload
