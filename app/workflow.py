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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app import planner, selection
from app.badcase import BadCaseContext, detect_badcases, summarize as summarize_badcases
from app.config import current_config
from app.decision.jev import JevClient
from app.decision.planner_decision import (
    DECISION_PLAN_CHOICE,
    DECISION_QUALITY_GATE,
    DECISION_TRADEOFF,
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
from app.observability import SpanKind, now_iso, span_id
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

#: 一次 run 的抓取预算。全部写成具名常量，是为了让"为什么这次只查了 3 个攻略"
#: 能在审计里直接回答，而不是散落在代码里的魔数。
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
    #: Discovery 已查好的候选（本轮只记录是否携带，复用见 docs/10 的待办）
    prefetch: Any
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


#: 让 Jev 比较的候选方案数上限。
#: 给多了摘要变长、判断质量下降（还会挤占它 1.5s 的超时预算），给少了没有比较价值。
MAX_PLAN_CANDIDATES = 5


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
) -> None:
    """写一条业务级子 span（component 分类见 observability.SpanKind）。

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
            finished_at=now_iso(),
            attributes=attributes or {},
            parent_span_id=parent_span_id,
            error=error,
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
    """比选得分（越低越好）。分项全部保留，审计能复述"为什么是它"。"""
    parts: dict[str, float] = {}

    price = coerce_float(getattr(option, "price", None))
    parts["价格"] = (
        TRANSPORT_UNKNOWN_PRICE_PENALTY
        if price is None
        else price / TRANSPORT_PRICE_UNIT * TRANSPORT_PRICE_WEIGHT
    )

    door_to_door = _door_to_door_minutes(option)
    parts["门到门时长"] = (
        TRANSPORT_UNKNOWN_DURATION_PENALTY
        if door_to_door is None
        else door_to_door / TRANSPORT_MINUTE_UNIT * TRANSPORT_MINUTE_WEIGHT
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

    parts["偏好"] = -_transport_preference_bonus(option, intent)

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


def _hotel_score(hotel: HotelOption, intent: TripIntent) -> tuple[float, dict]:
    parts: dict[str, float] = {}
    nightly = coerce_float(hotel.nightly)
    parts["每晚价格"] = (
        HOTEL_UNKNOWN_PRICE_PENALTY if nightly is None else nightly / HOTEL_PRICE_UNIT
    )
    rating = coerce_float(hotel.rating)
    parts["评分"] = -(rating or 0.0) * HOTEL_RATING_WEIGHT
    prefs = coerce_str_list(intent.hotel_preferences)
    haystack = " ".join(
        filter(None, [hotel.name, hotel.room_type, hotel.business_area, hotel.address, hotel.price_note])
    )
    hits = [pref for pref in prefs if pref and pref in haystack]
    parts["住宿偏好"] = -HOTEL_PREFERENCE_BONUS * len(hits)
    return round(sum(parts.values()), 2), {**{k: round(v, 2) for k, v in parts.items()}, "命中偏好": len(hits)}


def _select_hotel(options: list[HotelOption], intent: TripIntent) -> tuple[HotelOption | None, list[HotelOption], str]:
    if not options:
        return None, [], ""
    scored: list[tuple[float, dict, HotelOption]] = []
    for hotel in options:
        score, parts = _hotel_score(hotel, intent)
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
    reason = "；".join(bits)
    selected.selection_reason = reason
    for hotel in alternatives:
        hotel.selection_reason = f"未选中：综合分 {_hotel_score(hotel, intent)[0]:g} 高于「{selected.name}」"
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
            "status": STATUS_CONTINUE,
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

    out_trains = hub.search_trains(origin, destination, start)
    out_flights = hub.search_flights(origin, destination, start, travelers=travelers)
    provider_status["去程火车"] = out_trains.status
    provider_status["去程航班"] = out_flights.status

    outbound, out_alts, out_reason, out_scores = _select_transport(
        [*out_trains.items, *out_flights.items], direction="outbound", intent=intent
    )

    inbound = in_alts = None
    in_reason = ""
    if back is not None and back >= start:
        in_trains = hub.search_trains(destination, origin, back)
        in_flights = hub.search_flights(destination, origin, back, travelers=travelers)
        provider_status["回程火车"] = in_trains.status
        provider_status["回程航班"] = in_flights.status
        if back == start:
            in_reason = "当天往返，回程与去程同一天查询"
        inbound, in_alts, in_reason, _ = _select_transport(
            [*in_trains.items, *in_flights.items], direction="inbound", intent=intent
        )
    else:
        provider_status["回程火车"] = "SKIPPED"
        provider_status["回程航班"] = "SKIPPED"
        in_reason = "没有确定的返程日期，未查询回程"

    degradations: list[str] = []
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

    result = hub.search_hotels(destination, check_in, check_out, travelers=max(1, intent.travelers))
    selected, alternatives, reason = _select_hotel(list(result.items), intent)

    degradations: list[str] = []
    if selected is None:
        reason = (
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

    for keyword in queries[:SOCIAL_QUERY_LIMIT]:
        social = hub.search_xiaohongshu(keyword)
        platforms["小红书"] = platforms.get("小红书", 0) + len(social.items)
        evidences.extend(social.items)
        if not social.items:
            notes.append(f"小红书「{keyword}」：{social.status}")
        if len(evidences) >= MAX_EVIDENCE:
            break

    if not any(ev.provider == "xhs" for ev in evidences) and destination:
        douyin = hub.search_douyin(f"{destination} 旅游")
        platforms["抖音"] = platforms.get("抖音", 0) + len(douyin.items)
        evidences.extend(douyin.items)
        notes.append(f"小红书没有返回可用攻略，回退抖音查询：{douyin.status}")

    for keyword in queries[:WEB_QUERY_LIMIT]:
        web = hub.web_search(keyword, max_results=5)
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


def _keyword_candidates(intent: TripIntent, queries: list[str]) -> list[str]:
    """除攻略抽取之外，直接用高德按关键词搜 POI。

    两个来源互补：攻略能捞到"本地人常去"的小店，关键词能保证"必去景点"不会因为
    攻略没写而完全缺席。两者都只是**候选**，是否入选由 Trust / Ad Risk 决定。
    """
    destination = _destination(intent) or ""
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
    return names[:POI_QUERY_LIMIT]


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

    llm_targets = [
        evidence
        for evidence in state.get("evidences", [])
        if not evidence.place_mentions and len(evidence.text) >= 40
    ][:EXTRACT_EVIDENCE_LIMIT]
    for evidence in llm_targets:
        result = llm.invoke_json(
            EXTRACT_PLACES_PROMPT,
            f"标题：{evidence.title}\n来源：{evidence.provider}/{evidence.source_type}\n"
            f"证据 id：{evidence.id}\n正文：\n{evidence.text[:EVIDENCE_TEXT_CHARS]}",
            tag=f"extract_places:{evidence.id}",
        )
        if result.ok and isinstance(result.value, dict):
            for item in result.value.get("places", []) or []:
                if isinstance(item, dict) and coerce_str(item.get("name")):
                    extracted.append({**item, "evidence_id": evidence.id})
        else:
            degradations.append(degraded_note(result, f"证据 {evidence.id} 的地点抽取被跳过"))

    if llm_targets:
        extraction_note = (
            f"有 {len(llm_targets)} 条攻略走了模型抽取地点，"
            f"{len(state.get('evidences', [])) - len(llm_targets)} 条直接用数据源自带的 place_mentions"
        )
    else:
        extraction_note = "攻略地点全部来自数据源自带的 place_mentions，本次没有为抽取地点调用模型"

    # --- 2) 高德 POI 搜索 ---
    keywords = _keyword_candidates(intent, state.get("queries") or _fallback_queries(intent))
    places: list[Place] = []
    seen_ids: set[str] = set()
    poi_status = "OK"
    searched = 0

    def add_poi(keyword: str) -> None:
        nonlocal poi_status
        result = hub.search_poi(keyword, destination, page_size=POI_PAGE_SIZE, type_hint="attraction")
        poi_status = result.status if poi_status == "OK" else poi_status
        for place in result.items:
            if place.place_id in seen_ids:
                continue
            seen_ids.add(place.place_id)
            places.append(place)

    for name in provider_mentions[:POI_QUERY_LIMIT]:
        add_poi(name)
        searched += 1
    for item in extracted[:POI_QUERY_LIMIT]:
        add_poi(coerce_str(item.get("name")))
        searched += 1
    for keyword in keywords:
        add_poi(keyword)
        searched += 1

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


def node_verify_poi_and_routes(state: TravelState) -> dict:
    hub = state["hub"]
    intent = state["intent"]
    places = list(state.get("places", []))
    destination = _destination(intent) or ""
    degradations: list[str] = []

    # --- 1) 补营业时间（可行性判定要用，PRD §22）---
    detail_calls = 0
    detail_failures = 0
    for place in places:
        if place.opening_hours or not place.amap_verified:
            continue
        if detail_calls >= POI_DETAIL_LIMIT:
            break
        detail_calls += 1
        result = hub.poi_detail(place.place_id)
        if result.ok and result.items:
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

    ordered = planner.order_by_proximity(places, hotel_coords)
    routes: dict[Any, RouteOption] = {}
    route_ok = 0
    route_fail = 0
    for origin, target in zip(ordered, ordered[1:]):
        if len(routes) >= MAX_ROUTE_LOOKUPS:
            break
        start, end = lnglat(origin), lnglat(target)
        if start is None or end is None:
            continue
        result = hub.route(start, end, "transit", city=destination or None)
        if result.ok and result.items:
            option = result.items[0]
            option.origin_place_id = origin.place_id
            option.destination_place_id = target.place_id
            routes[(origin.place_id, target.place_id)] = option
            routes[(target.place_id, origin.place_id)] = option
            route_ok += 1
        else:
            route_fail += 1

    # --- 3) 机场/车站 → 酒店的真实接驳（PRD §24 的"额外接驳"）---
    outbound_transfer: int | None = None
    inbound_transfer: int | None = None
    transport_plan = state.get("transport_plan")
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
            geo = hub.geocode(coerce_str(station), city=destination or None)
            if not (geo.ok and geo.items):
                continue
            coords = (
                coerce_float(geo.items[0].get("longitude") or geo.items[0].get("lng")),
                coerce_float(geo.items[0].get("latitude") or geo.items[0].get("lat")),
            )
            if coords[0] is None or coords[1] is None:
                continue
            route = hub.route(coords, (hotel.lng, hotel.lat), "transit", city=destination or None)
            if route.ok and route.items and route.items[0].duration_minutes is not None:
                minutes = route.items[0].duration_minutes + planner.HOTEL_CHECKIN_MINUTES
                if setter == "out":
                    outbound_transfer = minutes
                else:
                    inbound_transfer = minutes

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
        f"高德 POI 详情查询 {detail_calls} 次（补营业时间/地址）",
        f"路线查询：成功 {route_ok} 段、失败 {route_fail} 段（上限 {MAX_ROUTE_LOOKUPS} 段）",
        "路线按「酒店附近 → 由近及远」顺序计算，与后续排程的地理聚类口径一致",
        f"高德整体状态：{amap_status}",
    ]
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
    decisions: list[Decision] = []
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
    ticket_prices: dict[str, float] = {}
    ticket_attempts = 0
    for place in sorted(places, key=lambda item: -trust_scores.get(item.place_id, 0.0)):
        if ticket_attempts >= TICKET_LOOKUP_LIMIT:
            break
        ticket_attempts += 1
        result = hub.search_scenic_tickets(place.name)
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
    choice = choose_plan(
        candidates,
        intent,
        jev=jev,
        recorder=_span_recorder(state),
        parent_span_id=parent,
        hard_violations=lambda candidate: planner.hard_constraint_violations(
            candidate.days, intent, outbound=outbound
        ),
    )
    chosen = choice.candidate

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

    # --- B. 关键 Trade-off ---
    alternatives = [
        candidate for candidate in candidates if current is None or candidate.label != current.label
    ][:3]
    tradeoff = resolve_tradeoff(
        current=current or _candidate_of(plan, state),
        alternatives=alternatives,
        intent=intent,
        jev=jev,
        recorder=recorder,
        parent_span_id=parent,
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

    critic_result = llm.invoke_json(
        CRITIC_PROMPT,
        json.dumps(_plan_digest(plan), ensure_ascii=False, default=str)[:24000],
        tag="critic",
    )
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

    # --- Jev：关键 Trade-off + 质量门（接管任务 §4B §4C） ---
    review = _jev_review(state, plan, llm_decisions=llm_decisions)
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
    """Provider 调用账本 → audit / Bad Case 共用的行结构。"""

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
                "note": "；".join(entry.get("notes") or []) or entry.get("error") or None,
            }
        )
    return rows


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

    层级：workflow 节点 → provider（或 mcp）→ tool。tool span 的 id 与 Bad Case 的
    ``trace_refs`` 一致（``{run}:provider:{provider}:{tool}``），所以从 Bad Case 能直接
    跳到那条 span。
    """

    run_id = state["run_id"]

    def parent_for(stage: str | None) -> str | None:
        return f"{run_id}:{stage}" if stage else None

    emitted_groups: set[str] = set()
    for entry in _provider_call_rows(state.get("hub")):
        provider = str(entry.get("provider") or "unknown")
        tool = str(entry.get("tool") or "unknown")
        stage = PROVIDER_STAGE_MAP.get(tool)
        server = MCP_BACKED_PROVIDERS.get(provider)
        group_component = SpanKind.MCP if server else SpanKind.PROVIDER
        group_name = server or provider
        group_id = span_id(run_id, group_component, group_name)
        if group_id not in emitted_groups:
            emitted_groups.add(group_id)
            _record_subspan(
                state,
                component=group_component,
                name=group_name,
                status="SUCCESS",
                started_at=str(entry.get("fetched_at") or now_iso()),
                attributes={"provider": provider, "transport": "mcp stdio" if server else "http"},
                parent_span_id=parent_for(stage),
            )
        _record_subspan(
            state,
            component=SpanKind.TOOL,
            name=tool,
            # 单个 tool 的成败就是它自己的状态；成组状态由管理端按子 span 聚合。
            status="SUCCESS" if entry.get("status") == "OK" else "WARNING",
            started_at=str(entry.get("fetched_at") or now_iso()),
            attributes={
                "provider": provider,
                "tool": tool,
                "status": entry.get("status"),
                "returned": entry.get("returned"),
                "duration_ms": entry.get("duration_ms"),
                "query": entry.get("query"),
                "source_id": entry.get("source_id"),
                "note": entry.get("note"),
            },
            parent_span_id=group_id,
            error=None if entry.get("status") == "OK" else str(entry.get("note") or entry.get("status")),
        )

    for index, entry in enumerate(llm.audit_entries() if llm is not None else []):
        tag = str(entry.get("tag") or "unnamed")
        _record_subspan(
            state,
            component=SpanKind.LLM,
            name=tag,
            status="SUCCESS" if entry.get("status") == "OK" else "WARNING",
            started_at=now_iso(),
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
        json.dumps(_plan_digest(plan), ensure_ascii=False, default=str)[:24000],
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
    provider_calls = _provider_call_rows(hub)
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
        "timeline": timeline,
        "artifacts": artifacts,
        "degradations": list(dict.fromkeys(degradations)),
        "evidence_summary": _evidence_summary(state, plan),
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
    ) -> None:
        """业务级子 span（planner / jev / llm / provider / budget / feasibility / critic / store）。

        span_id 由 (component, name) 决定，因此 Bad Case 能引用一个**稳定可查**的 id，
        而不是一段描述性文字。
        """

        try:
            resolved_store.save_trace_span(
                resolved_run_id,
                span_id(resolved_run_id, component, name),
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
        metrics: dict[str, Any] = {
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "input_tokens": input_tokens if has_usage else None,
            "output_tokens": output_tokens if has_usage else None,
            "cached_tokens": cached_tokens if has_usage else None,
            "total_tokens": (input_tokens + output_tokens) if has_usage else None,
            "llm_calls": len(getattr(resolved_llm, "calls", [])),
            # 只统计**真实发出**的 Jev 调用：SKIPPED / 未配置 不是调用，记 0 才不会高估用量。
            "jev_calls": len(jev_records),
            "tool_calls": len(provider_calls),
            "provider_failures": sum(1 for call in provider_calls if call.get("status") != "OK"),
            "badcase_count": len(badcases),
            # 没有 Provider 可核实的 pricing 回执时，cost 必须为 null。
            "cost": None,
        }
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
