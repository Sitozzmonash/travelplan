"""主规划入口：SuperHarness Agent Loop 自己调工具、自己出计划（P1/P2）。

它取代什么
----------
`app/workflow.py::execute_travel_run` 是「固定 12 步 LangGraph 出计划」。本模块的
`execute_agent_run` 是它的**替代品**：同一套签名、同一个 `RunResult`，但规划引擎换成
SuperHarness Agent Loop —— Agent 在循环里自己决定要不要查火车/机票/酒店/门票/POI/路线/
攻略，预算、时间、合理性、用户偏好全部写进 system prompt 由它自己控制。

Python 在这一路里只保留**一件事**：时间冲突验证（返程赶不上、营业时间排不下、时间重叠）。
它接在 Agent 交卷之后，改的是 days 与 warnings，不校验预算（预算是 prompt 的职责，
见 feedback/inbox/2026-09-22 的方案文档 2.1）。

复用什么（这一路的重点不是重写，而是接上）
------------------------------------------
* 取数能力：`app/travel_tools.py::build_travel_tools(hub)`（ProviderHub 的 11 个工具）；
* 计划管线：`app/workflow.py` 的 `_render_markdown` / `_write_artifacts` /
  `_write_run_artifacts` / `_build_audit` / `_detect_and_save_badcases` —— plan.json /
  plan.md / audit_report.json / trace.jsonl / metrics.json / badcases.json 与落库
  （`store.save_plan` + `store.finish_run`）与固定流程**同一套代码**，产物形状不变；
* 观测：`app/agent_trace.py::build_step_reporter` 把每一跳写进 `run_stages` /
  `trace_spans`（前端的"在查酒店"、admin 的每步 token 都靠它）；
* 模型降级：`app/agent.py::build_model_with_fallbacks`（主 → bk1 → bk2）。

三个必须知道的实现取舍
--------------------
1. **工具可见性**：SuperHarness 的 `CapabilityMiddleware` 会用 BM25 从"手动传入的 tools"
   里按 `settings.tool_top_k`（默认 3）筛出可见工具，而它打分用的 query 是会话里**最后一条
   HumanMessage** —— Agent Loop 里那条消息从头到尾不变，于是"这一次 run 只能看见 3 个工具"
   是**固定**的：10 个旅行工具里有 7 个永远调不到，`submit_final_plan` 也可能被筛掉。
   所以这里挂一个 `_AlwaysVisibleTools` middleware，在能力筛选**之后**把本业务的工具
   补回模型可见列表（按名字去重）。这是**必要的接线**，不是绕过 Router：Router 仍然在跑，
   仍然记录它选了什么（`router.selected` 事件）。
2. **web_search 重名**：`build_travel_tools(hub)` 里的 `web_search` 与 SuperHarness 自动
   发现的同名内置工具会被 `superharness/agent.py::_build_local_tools` 判为冲突并直接抛
   ValueError（"不允许静默覆盖"）。`create_harness_agent` 没有按次关闭自动发现的开关
   （只有全局 `settings.tool_auto_discovery`），所以这里**不把这个重名工具传进 tools=**：
   内置那个 `web_search` 本来就够用，而 `hub.web_search` 转发的正是同一个工具
   （`app/providers.py::ProviderHub.web_search` 直接取本地的 `web_search` 工具）。
   代价：`hub.web_search` 的缓存/审计账本不再被这一步经过 —— 所以 `_AlwaysVisibleTools`
   会在内置工具**缺席**时用 hub 版顶上（两个同名工具不会同时出现在一个请求里）。
3. **交卷机制**：Agent 调用终结工具 `submit_final_plan`（参数 schema 见
   `SubmittedPlan`），工具闭包把它记进一个可变 holder；循环结束后从这里取行程。
   没调用就退一步解析最后一条消息里的 JSON；再拿不到就如实 FAILED —— **绝不编造 plan**。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app import planner
from app.agent_trace import build_step_reporter
from app.config import current_config
from app.models import (
    BudgetStatus,
    BudgetSummary,
    Decision,
    FeasibilityIssue,
    FlightOption,
    HotelOption,
    HotelPlan,
    ItemType,
    ItineraryDay,
    ItineraryItem,
    PriceType,
    RouteOption,
    SourceRef,
    TrainOption,
    TransportPlan,
    TravelLeg,
    TripIntent,
    TripPlan,
    coerce_datetime,
    coerce_float,
    coerce_int,
    coerce_str,
)
from app.observability import SpanKind, call_with_deadline, now_iso, span_id
from app.prompts import TRAVEL_PLANNER_PROMPT_VERSION, TRAVEL_PLANNER_SYSTEM_PROMPT
from app.providers import ProviderHub, default_mcp_servers
from app.store import TravelPlanStore
from app.travel_tools import build_travel_tools
from app.workflow import (
    DEFAULT_OUTPUT_DIR,
    STATUS_COMPLETED,
    STATUS_FAILED,
    RunResult,
    _adopt_prefetch_sources,
    _build_audit,
    _detect_and_save_badcases,
    _materialize_city_evidence,
    _render_markdown,
    _write_artifacts,
    _write_run_artifacts,
    new_run_id,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SubmittedPlan",
    "execute_agent_run",
]


# ======================================================================
# SuperHarness 能力瘦身（全局 patch，见 feedback/inbox/2026-09-22 §3.3）
# ======================================================================
# `super_harness/superharness/config.py` 里 `memory_enabled=True` /
# `retrieval_enabled=True` / `log_enabled=True` / `trace_enabled=True` 是**字面量**，
# 没有环境变量开关。开着它们意味着每次 `create_harness_agent` 都会：
#   1. 建 MemoryManager（SQLite `data/memory.db`）与 RetrievalManager —— 两个对象
#      本身就是内存（Render 免费档只有 512MiB，OOM 根因之一）；
#   2. 每 run 把 rollout 写进 SQLite，攒够 `memory_consolidate_rollouts` 条还会
#      **再调一次模型**做 consolidation —— 那一次不在任何业务账本里，纯粹浪费。
#
# 旅行规划**不需要**长期记忆/检索：事实全部来自当次工具调用，跨 run 复用记忆反而是
# 正确性风险（上次的候选会污染这次的证据链）。所以这里在**模块顶层**做一次**幂等**
# patch —— 任何入口（API 规划 / CLI / HybridRunner / 预热 Agent）import 本模块后，
# `create_harness_agent` 装配时读到的 `superharness.agent.settings` 都是关掉这四个
# 能力的那一份。样板是 `benchmark/fixtures/world.py::isolated_harness`（用
# `dataclasses.replace`，同时 patch `superharness.agent.settings` 与
# `ObservabilityMiddleware.__init__.__defaults__`）；这里是同一件事的**永久**版本。
# benchmark 的 contextmanager 自己会再 replace 一次，幂等，互不干扰。
_PATCHED_HARNESS_SETTINGS = False


def _disable_harness_memory_and_logs() -> None:
    """关掉 harness 的 memory/retrieval/log/trace（全局、幂等，只执行一次）。

    这是全局 patch：影响本进程里所有走 `create_harness_agent` 的路径
    （主规划 Agent Loop、CLI 的 route=AGENT、HybridRunner、预热 Agent），
    对本项目都合适 —— 理由见上面注释。**重启才生效**是特征而不是限制：
    本模块一旦被 import，patch 就已生效，`execute_agent_run` 里对
    `create_harness_agent` 的直接调用（以及 `app/agent.py` 的 `create_harness_app`）
    装配时读到的都是关掉记忆/检索的那份 settings。
    """

    global _PATCHED_HARNESS_SETTINGS
    if _PATCHED_HARNESS_SETTINGS:
        return
    from superharness import agent as harness_agent
    from superharness.config import settings as default_settings
    from superharness.middleware import ObservabilityMiddleware

    patched = dataclasses.replace(
        default_settings,
        memory_enabled=False,
        retrieval_enabled=False,
        log_enabled=False,
        trace_enabled=False,
    )
    # `create_harness_agent` / `create_harness_app` 装配时读的是 `superharness.agent`
    # 模块命名空间里的 `settings`（`from .config import settings` 的模块级绑定），
    # 所以改这里就够了；`superharness.config.settings` 原样不动，其它使用者不受影响。
    harness_agent.settings = patched
    # 终端 Console / Trace 渲染读的是 `ObservabilityMiddleware.__init__` 的**默认参数**
    # （def 时就绑定好了），改模块属性没用，得改 `__defaults__`。
    # 关掉的只是终端打印与 Trace 渲染，不影响任何业务事实，
    # 也不影响 `app/agent_trace.py` 自己的步骤上报（那是另一条链路）。
    ObservabilityMiddleware.__init__.__defaults__ = (patched,)
    _PATCHED_HARNESS_SETTINGS = True


_disable_harness_memory_and_logs()

#: 终结工具名。写死在常量里：prompt、审计与单测都引用它，改名必须三处一起改。
SUBMIT_TOOL_NAME = "submit_final_plan"


# ======================================================================
# 上下文：从"上次对话"里找最后一条 AI 消息（解析兜底用）
# ======================================================================


def _last_ai_text(state: Any) -> str:
    """取 Agent 最后一条 AI 消息的正文（用于"没交卷"时的 JSON 解析兜底）。

    只认 AIMessage / 带 role=assistant 的字典：工具结果（ToolMessage）里也有 JSON，
    把它们当行程解析会把"工具返回的候选列表"误当成一份计划 —— 那是编造的一种。
    """

    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    for message in reversed(list(messages or [])):
        content = getattr(message, "content", None)
        role = str(getattr(message, "type", "") or "")
        if content is None and isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "")
            content = message.get("content")
        if role not in {"ai", "assistant"}:
            continue
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
            ]
            return "\n".join(part for part in parts if part)
    return ""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从一段文本里抠出**平衡的** JSON 对象。

    模型经常把 JSON 包在 ```` ```json ```` 里、前后带一句说明。这里用括号配平扫描而不是
    正则：正则遇到嵌套对象只能截到第一个 `}`，解析出来的是半个计划。
    扫描时尊重字符串与转义，避免把 JSON 串里的花括号当结构。
    """

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(payload, dict):
                        return payload
                    break
        start = text.find("{", start + 1)
    return None


# ======================================================================
# 交卷参数 schema（面向 LLM 的精简投影）
# ======================================================================
# 为什么不直接用 TripPlan 当 args_schema：TripPlan 的 JSON schema 约 20KB（含 Decision /
# FlightOption / TrainOption 的全部字段），而它每次模型调用都会随工具一起进上下文 ——
# 40 步的循环里光 schema 就要几万 token。这里只保留"必须由 Agent 决定、Python 无法从
# 别处补出来"的字段，其余在 _to_trip_plan 里补默认值。
# 改这个 schema = 改 Agent 的产出契约，必须同时改 TRAVEL_PLANNER_SYSTEM_PROMPT 的自查清单。


class PlanLegArgs(BaseModel):
    """item 之间的一段移动（对应 TravelLeg）。耗时必须来自 `route` 工具。"""

    mode: str = Field(default="transit", description="transit / walking / driving / riding / flight / train")
    duration_minutes: int | None = Field(default=None, description="这段路要多少分钟；来自 route 工具")
    distance_meters: int | None = Field(default=None, description="距离（米）；来自 route 工具")
    estimated_cost: float | None = Field(default=None, description="这段路的花费（元），没有就 null")
    verified: bool = Field(default=False, description="是否是真实路线数据（route 工具返回 verified=true 才填 true）")


class PlanItemArgs(BaseModel):
    """行程里的一条安排（对应 ItineraryItem）。时间用 "HH:MM"。"""

    name: str = Field(description="地点/活动名")
    type: ItemType = Field(default="attraction", description="attraction / food / hotel / transport / activity / free_time")
    place_id: str | None = Field(default=None, description="高德 POI ID（search_poi/poi_detail 返回的 place_id）")
    start_time: str | None = Field(default=None, description="开始时间 HH:MM")
    end_time: str | None = Field(default=None, description="结束时间 HH:MM")
    duration_minutes: int | None = Field(default=None, description="停留时长（分钟）")
    travel_from_previous: PlanLegArgs | None = Field(default=None, description="从上一站过来的路上时间")
    price: float | None = Field(default=None, description="这一项的费用（元），没有就 null")
    price_type: PriceType = Field(default="unknown", description="realtime=工具给的实时价 / estimated=估算 / unknown=未知")
    reason: str = Field(default="", description="为什么把它排在这里")
    risk_note: str = Field(default="", description="风险提示（证据薄弱、营销嫌疑、营业时间未知…）")
    evidence_ids: list[str] = Field(default_factory=list, description="支撑这一项的攻略/网页证据 id")
    transport_mode: str | None = Field(default=None, description="type=transport 时的方式：flight/train/transit")
    lat: float | None = Field(default=None, description="纬度（GCJ-02）")
    lng: float | None = Field(default=None, description="经度（GCJ-02）")


class PlanDayArgs(BaseModel):
    """一天行程（对应 ItineraryDay）。day_index 从 0 开始。"""

    day_index: int = Field(description="第几天，从 0 开始")
    date: str | None = Field(default=None, description="日期 YYYY-MM-DD，未知就 null")
    area: str | None = Field(default=None, description="当天主要活动区域")
    items: list[PlanItemArgs] = Field(default_factory=list, description="当天的安排，按时间先后排列")
    notes: list[str] = Field(default_factory=list, description="当天需要用户知道的说明")


class PlanTransportArgs(BaseModel):
    """选中的一段大交通（去程或回程）。"""

    kind: Literal["flight", "train"] = Field(description="飞机还是火车")
    no: str = Field(description="航班号或车次号，如 CA4102 / G89")
    provider: str = Field(default="unknown", description="数据来源名，如 tuniu / 12306")
    origin: str | None = Field(default=None, description="出发城市/机场/车站")
    destination: str | None = Field(default=None, description="到达城市/机场/车站")
    departure_at: str | None = Field(default=None, description="出发时间 YYYY-MM-DD HH:MM")
    arrival_at: str | None = Field(default=None, description="到达时间 YYYY-MM-DD HH:MM")
    duration_minutes: int | None = Field(default=None, description="总历时（分钟）")
    door_to_door_minutes: int | None = Field(default=None, description="门到门耗时（含往返机场/车站）")
    price: float | None = Field(default=None, description="票价（元），未知就 null")
    currency: str = Field(default="CNY")
    selection_reason: str = Field(default="", description="为什么选它")


class PlanHotelArgs(BaseModel):
    """选中的住宿（alternatives 里复用同一模型，字段都填，selection_reason 可以留空）。"""

    name: str = Field(description="酒店名")
    provider: str = Field(default="unknown", description="数据来源名")
    hotel_id: str | None = Field(default=None, description="search_hotels 返回的酒店 id")
    source_id: str | None = Field(default=None, description="search_hotels 返回条目的来源 id")
    room_type: str | None = Field(default=None, description="房型")
    price_per_night: float | None = Field(default=None, description="每晚价格（元）；途牛给的是起价")
    price_note: str | None = Field(default=None, description="价格口径说明，如 '420 起价/晚'")
    address: str | None = Field(default=None)
    rating: float | None = Field(default=None)
    selection_reason: str = Field(default="", description="为什么选它；alternatives 里的候选不需要填")
    alternatives: list[PlanHotelArgs] = Field(
        default_factory=list,
        description="其它酒店候选，最多 4 条；必须来自 search_hotels 返回的其它真实结果，不许编造",
    )


class PlanSourceArgs(BaseModel):
    """一条来源（对应 SourceRef）。source_id 必须是你真实见到过的，不许编。"""

    source_id: str = Field(description="工具返回里的 source_id / 证据 id")
    provider: str = Field(default="unknown")
    source_type: str = Field(default="")
    source_url: str | None = Field(default=None)
    title: str = Field(default="")
    fetched_at: str | None = Field(default=None, description="查询时间，如 2026-10-01T09:00:00+00:00")
    status: str = Field(default="OK")


class SubmittedPlan(BaseModel):
    """`submit_final_plan` 的参数（Agent 交卷的那份计划）。

    所有"查不到"的字段一律留 null / unknown —— 这里没有必填的数字字段（只有 name /
    source_id 这类标识），就是为了让"没查到"能如实表达，而不是被 schema 逼着填一个假值。
    """

    intent: TripIntent = Field(description="用户意图：目的地/日期/天数/人数/预算/偏好")
    transport: PlanTransportArgs | None = Field(default=None, description="去程大交通（选中的那一个）")
    inbound_transport: PlanTransportArgs | None = Field(default=None, description="回程大交通")
    transport_alternatives: list[PlanTransportArgs] = Field(default_factory=list, description="其它候选（可选，最多 4 条）")
    transport_reason: str = Field(default="", description="为什么这样选大交通（去程/回程一起说）")
    hotel: PlanHotelArgs | None = Field(default=None, description="选中的住宿")
    days: list[PlanDayArgs] = Field(default_factory=list, description="逐日行程")
    budget: BudgetSummary = Field(default_factory=BudgetSummary, description="预算汇总：真实报价与估算分开记")
    warnings: list[FeasibilityIssue] = Field(default_factory=list, description="你自己发现的时间/营业时间问题")
    sources: list[PlanSourceArgs] = Field(default_factory=list, description="行程里数字的来源")
    decisions: list[Decision] = Field(default_factory=list, description="取舍留痕：KEEP/REJECT/SELECT + 理由")


# ======================================================================
# 投影 → TripPlan
# ======================================================================


def _to_leg(args: PlanLegArgs | None) -> TravelLeg | None:
    if args is None:
        return None
    minutes = coerce_int(args.duration_minutes)
    return TravelLeg(
        mode=coerce_str(args.mode, "transit") or "transit",
        distance_meters=coerce_int(args.distance_meters),
        duration_seconds=minutes * 60 if minutes is not None else None,
        estimated_cost=coerce_float(args.estimated_cost),
        # verified 是**工具事实**：Agent 说 true 才 true，缺省一律按未验证。
        verified=bool(args.verified),
        estimated=not bool(args.verified),
    )


def _to_item(args: PlanItemArgs, *, fallback_id: str) -> ItineraryItem:
    return ItineraryItem(
        id=fallback_id,
        type=args.type,
        place_id=coerce_str(args.place_id) or None,
        name=coerce_str(args.name),
        lat=coerce_float(args.lat),
        lng=coerce_float(args.lng),
        start_time=coerce_str(args.start_time) or None,
        end_time=coerce_str(args.end_time) or None,
        duration_minutes=coerce_int(args.duration_minutes),
        travel_from_previous=_to_leg(args.travel_from_previous),
        price=coerce_float(args.price),
        price_type=args.price_type,
        reason=coerce_str(args.reason),
        risk_note=coerce_str(args.risk_note),
        evidence_ids=[coerce_str(x) for x in args.evidence_ids if coerce_str(x)],
        transport_mode=coerce_str(args.transport_mode) or None,
    )


def _to_day(args: PlanDayArgs) -> ItineraryDay:
    day_index = coerce_int(args.day_index, 0) or 0
    return ItineraryDay(
        day_index=day_index,
        date=coerce_datetime(args.date).date() if coerce_datetime(args.date) else None,
        area=coerce_str(args.area) or None,
        # item id 由 (天, 序号) 生成：修订/删除/前端定位都靠它，且不依赖 Agent 是否给 id。
        items=[
            _to_item(item, fallback_id=f"d{day_index}-{index}")
            for index, item in enumerate(args.items, start=1)
        ],
        notes=[coerce_str(note) for note in args.notes if coerce_str(note)],
    )


def _to_transport_option(args: PlanTransportArgs) -> FlightOption | TrainOption:
    departure_at = coerce_datetime(args.departure_at)
    arrival_at = coerce_datetime(args.arrival_at)
    price = coerce_float(args.price)
    common: dict[str, Any] = {
        "provider": coerce_str(args.provider, "unknown") or "unknown",
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "duration_minutes": coerce_int(args.duration_minutes),
        "door_to_door_minutes": coerce_int(args.door_to_door_minutes),
        "currency": coerce_str(args.currency, "CNY") or "CNY",
        "selection_reason": coerce_str(args.selection_reason) or None,
    }
    if args.kind == "train":
        return TrainOption(
            train_no=coerce_str(args.no),
            origin_station=coerce_str(args.origin) or None,
            destination_station=coerce_str(args.destination) or None,
            # 火车没有"总价"字段，价格靠 price_range 的 min 表达（与 TrainOption.price 同口径）。
            price_range={"min": price} if price is not None else {},
            **common,
        )
    return FlightOption(
        flight_no=coerce_str(args.no),
        origin=coerce_str(args.origin) or None,
        destination=coerce_str(args.destination) or None,
        price=price,
        **common,
    )


def _merge_intent(submitted: TripIntent, reference: TripIntent | None) -> TripIntent:
    """把调用方（引导式前端）已确认的结构化意图盖到 Agent 产出的 intent 上。

    为什么以 reference 为准：那是**用户点过的按钮**（日期/天数/人数/预算/节奏，以及
    MUST/WANT/REJECT 的地点选择）。Agent 只能把它们读进 prompt，读漏一个就会把用户的
    硬约束丢掉 —— 而硬约束丢了就是失败。`exclude_unset=True` 保证只覆盖"用户确实选过的
    字段"，reference 里的默认值不会把 Agent 提取到的信息顶掉。
    """

    if reference is None:
        return submitted
    patch = reference.model_dump(exclude_unset=True)
    if not patch:
        return submitted
    merged = submitted.model_dump()
    merged.update(patch)
    return TripIntent.model_validate(merged)


def _to_hotel_option(args: PlanHotelArgs) -> HotelOption:
    """把交卷参数里的一个酒店（selected 或 alternative）映射成 HotelOption。

    `hotel_id / source_id` 原样带过：前端"来源"面板与候选比较需要能指回 search_hotels
    的真实结果。其余字段与原来的 selected 映射逐字等价。
    """

    return HotelOption(
        provider=coerce_str(args.provider, "unknown") or "unknown",
        source_id=coerce_str(args.source_id) or None,
        hotel_id=coerce_str(args.hotel_id) or None,
        name=coerce_str(args.name),
        address=coerce_str(args.address) or None,
        room_type=coerce_str(args.room_type) or None,
        price_per_night=coerce_float(args.price_per_night),
        rating=coerce_float(args.rating),
        price_note=coerce_str(args.price_note) or None,
        selection_reason=coerce_str(args.selection_reason) or None,
    )


def _fill_budget(budget: BudgetSummary, intent: TripIntent) -> BudgetSummary:
    """只补**能算出来的**缺项，不改 Agent 给的数字。

    - 预算总额来自用户（intent），Agent 漏填时补上；
    - 结余 = 预算 − 预计总额（两个数都在才算，缺一个就是 None，不猜）；
    - 状态：Agent 填了就不动；没填且两个数都在，按"超没超"如实标。
    真实报价与估算的拆分（known_real_cost / estimated_cost）是 Agent 的职责，这里不代劳。
    """

    budget_total = budget.budget_total if budget.budget_total is not None else intent.budget_total
    remaining = budget.remaining
    if remaining is None and budget_total is not None:
        remaining = round(budget_total - (budget.projected_total or 0.0), 2)
    status: BudgetStatus = budget.status
    if status == "unknown" and budget_total is not None:
        status = "over_budget" if (remaining or 0.0) < 0 else "within_budget"
    return budget.model_copy(
        update={"budget_total": budget_total, "remaining": remaining, "status": status}
    )


def _rebuild_budget(plan: TripPlan, budget: BudgetSummary, intent: TripIntent) -> BudgetSummary:
    """用最终 plan 重算预算的 breakdown 系列字段（中文分类 key + 真实/估算拆分）。

    为什么在契约层重算：`SubmittedPlan.budget.breakdown` 是**自由 key**，Agent 可能填英文
    key 或漏填，前端预算卡就看不到「交通 / 住宿 / 门票…」这类中文行。`planner.build_budget`
    按固定分类（交通/住宿/门票/餐饮估算/市内交通/其他）产出中文 key，并把真实报价与估算
    分开（breakdown_price_type + known_real_cost / estimated_cost）。

    合并规则：
    - `breakdown / breakdown_price_type / known_real_cost / estimated_cost / projected_total`
      用重算值覆盖（这是本次要修的用户可见问题）；
    - `budget_total / remaining / status` 照 `_fill_budget` 的既有逻辑合并 —— Agent 显式填了
      就保留，否则回退 intent / 按重算后的 projected 算，不代劳 Agent 自己的判断；
    - `optimization_suggestions / price_notes` 保留 Agent 交的原文，不拿代码的备注顶替。

    门票价从哪来：优先 `build_budget` 直接读 days 里 `item.price`（price_type=realtime），
    这里再补一个 `place_id/name → price` 的兜底映射 —— Agent 若把 price_type 标成
    estimated/unknown，门票也能按 plan 的 item 价格汇总进真实部分，而不是白白记 0。
    """

    ticket_prices: dict[str, float] = {}
    for day in plan.days:
        for item in day.items:
            if item.type != "attraction" or item.price is None:
                continue
            if item.place_id:
                ticket_prices[item.place_id] = item.price
            if item.name:
                ticket_prices.setdefault(item.name, item.price)
    recomputed = planner.build_budget(
        intent,
        plan.transport,
        plan.hotel.selected if plan.hotel is not None else None,
        plan.days,
        ticket_prices,
        city=intent.destination[0] if intent.destination else None,
        day_count=len(plan.days),
        hotel_alternatives=list(plan.hotel.alternatives) if plan.hotel is not None else (),
    )
    merged = budget.model_copy(
        update={
            "breakdown": recomputed.breakdown,
            "breakdown_price_type": recomputed.breakdown_price_type,
            "known_real_cost": recomputed.known_real_cost,
            "estimated_cost": recomputed.estimated_cost,
            "projected_total": recomputed.projected_total,
        }
    )
    return _fill_budget(merged, intent)


def _to_trip_plan(
    submitted: SubmittedPlan, *, run_id: str, query: str, reference_intent: TripIntent | None
) -> TripPlan:
    intent = _merge_intent(submitted.intent, reference_intent)
    transport = None
    if submitted.transport is not None or submitted.inbound_transport is not None:
        transport = TransportPlan(
            selected=_to_transport_option(submitted.transport) if submitted.transport else None,
            inbound_selected=(
                _to_transport_option(submitted.inbound_transport)
                if submitted.inbound_transport
                else None
            ),
            # 候选只留展示用的前 4 条（与固定流程的 MAX_TRANSPORT_ALTERNATIVES 同量级），
            # 多了会让 plan.json 与前端比较面板都失去意义。
            alternatives=[_to_transport_option(item) for item in submitted.transport_alternatives[:4]],
            selection_reason=coerce_str(submitted.transport_reason),
        )
    hotel_plan = None
    if submitted.hotel is not None:
        hotel_plan = HotelPlan(
            selected=_to_hotel_option(submitted.hotel),
            # 候选只留展示用的前 4 条（与 transport_alternatives 同量级），且必须是
            # search_hotels 的真实返回 —— 见 PlanHotelArgs.alternatives 的字段说明。
            alternatives=[_to_hotel_option(item) for item in submitted.hotel.alternatives[:4]],
            selection_reason=coerce_str(submitted.hotel.selection_reason),
        )

    plan = TripPlan(
        run_id=run_id,
        query=query,
        intent=intent,
        transport=transport,
        hotel=hotel_plan,
        days=[_to_day(day) for day in sorted(submitted.days, key=lambda d: d.day_index or 0)],
        budget=_fill_budget(submitted.budget, intent),
        warnings=list(submitted.warnings),
        sources=[
            SourceRef(
                source_id=coerce_str(item.source_id),
                provider=coerce_str(item.provider, "unknown") or "unknown",
                source_type=coerce_str(item.source_type),
                source_url=coerce_str(item.source_url) or None,
                title=coerce_str(item.title),
                fetched_at=coerce_datetime(item.fetched_at),
                status=coerce_str(item.status, "OK") or "OK",
            )
            for item in submitted.sources
            if coerce_str(item.source_id)
        ],
        decisions=list(submitted.decisions),
    )
    # 预算的 breakdown 系列字段用最终 plan 重算（中文分类 key + 真实/估算拆分），
    # 总额/结余/状态仍由 _fill_budget 合并 —— 见 _rebuild_budget 的 docstring。
    plan.budget = _rebuild_budget(plan, submitted.budget, intent)
    return plan


# ======================================================================
# 时间冲突兜底（Python 唯一保留的校验）
# ======================================================================


def _merge_warnings(
    agent_issues: list[FeasibilityIssue], code_issues: list[FeasibilityIssue]
) -> list[FeasibilityIssue]:
    """代码算出来的结论优先，Agent 自己报的、代码没复核到的保留。

    同一条问题（同一天 + 同 code + 同 item）以代码版为准：只有代码会填
    `resolved` / `original_start` / `revised_start`，那是因为它真的做了修订动作。
    """

    merged: dict[tuple[int, str, str], FeasibilityIssue] = {}
    for issue in agent_issues:
        merged[(issue.day_index, issue.code, issue.item_id or "")] = issue
    for issue in code_issues:
        merged[(issue.day_index, issue.code, issue.item_id or "")] = issue
    return list(merged.values())


def _routes_from_plan(plan: TripPlan) -> dict[tuple[str, str], Any]:
    """把 Agent 写在 `item.travel_from_previous` 上的路段还原成 planner 认的 routes 映射。

    为什么必须还原：`planner._walk_day` 只从 `routes` 里取路段耗时，**不看**
    `item.travel_from_previous`。喂空映射的后果有两个，都是硬伤：
    1. 排程按直距估算（或同城兜底 30 分钟）推进，会把 Agent 用 `route` 工具算出来的真实
       耗时覆盖掉 —— 一次"12:40 到、13:05 走"的真实行程会被改写成别的时间；
    2. 每一段都会被标成 `ROUTE_UNVERIFIED`（warning），audit 里看起来像"全程没有一条
       真实路线"，而事实上 Agent 查过高德。

    键用 `(上一站 place_id, 下一站 place_id)` —— planner.lookup_route 认的第一种形状。
    缺 place_id 的路段不编造：它们本来就无法与 place 对齐，留给 planner 走它自己的估算，
    并如实标未核实。
    """

    routes: dict[tuple[str, str], Any] = {}
    for day in plan.days:
        previous: ItineraryItem | None = None
        for item in day.items:
            leg = item.travel_from_previous
            if (
                previous is not None
                and previous.place_id
                and item.place_id
                and leg is not None
                and leg.duration_seconds is not None
            ):
                routes[(previous.place_id, item.place_id)] = RouteOption(
                    origin_place_id=previous.place_id,
                    destination_place_id=item.place_id,
                    mode=leg.mode or "transit",
                    distance_meters=leg.distance_meters,
                    duration_seconds=leg.duration_seconds,
                    estimated_cost=leg.estimated_cost,
                    provider=leg.provider or "amap",
                    verified=bool(leg.verified),
                )
            previous = item
    return routes


def _apply_time_pass(plan: TripPlan) -> tuple[list[ItineraryDay], list[FeasibilityIssue]]:
    """对 Agent 交上来的 days 跑一次「时间是否排得下」的检查与修订。

    喂进去的证据只有两样：Agent 自己写在 item 上的路段耗时（见 `_routes_from_plan`）与
    transport 上的去回程时刻。**营业时间不在这里校验** —— 那要 Place 对象，而 Agent 路径
    不落 places 表；`places=None` 时 planner 会跳过营业时间相关的判断（检查不出来 ≠ 没问题，
    所以 prompt 里要求 Agent 自己保证，并把不确定的写进 risk_note/warnings）。
    """

    intent = plan.intent
    outbound = plan.transport.selected if plan.transport else None
    inbound = plan.transport.inbound_selected if plan.transport else None
    arrival_index = planner.arrival_day_index(outbound, intent.start_date, max(1, intent.days))
    first_day_start = planner.arrival_day_start_minutes(outbound)
    last_day_deadline = planner.departure_deadline_minutes(inbound)
    return planner.apply_feasibility_pass(
        list(plan.days),
        _routes_from_plan(plan),
        intent,
        places=None,
        first_day_start=first_day_start,
        first_day_index=arrival_index,
        last_day_deadline=last_day_deadline,
    )


# ======================================================================
# Middleware：工具可见性 + 成本护栏
# ======================================================================


class _AlwaysVisibleTools(AgentMiddleware):
    """把本业务的工具补回模型可见列表（理由见模块 docstring 第 1 条）。

    必须是**按名字去重**的追加：SuperHarness 的能力筛选仍然生效，选中的工具原样保留，
    只是"没被选中"的那些不再等于"这个 run 用不了"。同名冲突时以请求里已有的那个为准。
    """

    def __init__(self, tools: list[Any]) -> None:
        self._tools = list(tools)

    def _merged(self, request: Any) -> Any:
        existing = {getattr(item, "name", None) for item in getattr(request, "tools", []) or []}
        extra = [item for item in self._tools if getattr(item, "name", None) not in existing]
        if not extra:
            return request
        return request.override(tools=[*(getattr(request, "tools", []) or []), *extra])

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._merged(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._merged(request))


class _TokenBudgetExceeded(RuntimeError):
    """累计 token 超过 `max_run_tokens` 时由 `_TokenBudgetGuard` 抛出。"""


class _TokenBudgetGuard(AgentMiddleware):
    """成本护栏：模型调用前检查累计 token，超了就让循环停下来。

    为什么在**模型调用前**检查：Agent 的 token 消耗几乎全在模型调用上，工具调用只是取数。
    在每一跳之前看一眼累计值，就能在"下一个大 prompt 发出去之前"停住 —— 事后再统计只能
    知道超了多少，拦不住。

    抛出的异常会被 execute_agent_run 识别成"成本截断"，如实标降级；已经交卷的 plan 仍然
    保留（行程本身是完整的，只是循环被提前掐断）。它不会被 ModelRetryMiddleware 吞掉：
    那个 middleware 的 `on_failure="error"` 只重试模型调用，而这里是在模型调用**之外**
    抛出的。
    """

    def __init__(self, limit: int | None, totals: Any) -> None:
        self._limit = limit
        self._totals = totals

    def _check(self) -> None:
        if self._limit is None:
            return
        used = int(self._totals().get("total_tokens") or 0)
        if used > self._limit:
            raise _TokenBudgetExceeded(
                f"本次 run 累计 token 已达 {used}，超过 max_run_tokens={self._limit}，循环已停止"
            )

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        self._check()
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        self._check()
        return await handler(request)


# ======================================================================
# 终结工具
# ======================================================================


def _build_submit_tool(holder: dict[str, Any]) -> Any:
    """构造终结工具：Agent 调它 = 交卷。

    `args_schema=SubmittedPlan` 让 schema 是**扁平**的（否则 langchain 会把整个模型塞进
    一个 `plan` 参数里，多一层嵌套就多一处模型出错的地方）。函数收到的是已校验的 kwargs，
    再自己 validate 一次拿类型化对象 —— 校验失败会让这次工具调用如实失败，Agent 能看到
    错在哪并重试，而不是把一份半成品悄悄收下。
    """

    @tool(SUBMIT_TOOL_NAME, args_schema=SubmittedPlan)
    def submit_final_plan(**kwargs: Any) -> str:
        """交卷：把最终行程（逐日安排、大交通、住宿、预算、来源）交给系统。

        什么时候调用：行程已经排好、自查通过（时间排得下、预算内、MUST 都在、REJECT 一个都没有）。
        一次 run 只调用一次，调用后不要再调用任何工具。
        参数说明：所有"没查到"的字段填 null / unknown，不要编造价格或时刻。
        返回：确认信息（它表示行程已被接收，不代表行程一定合法）。
        """

        try:
            plan = SubmittedPlan.model_validate(kwargs)
        except Exception as exc:  # noqa: BLE001 —— 校验失败要变成模型能读懂的反馈
            return json.dumps(
                {
                    "status": "INVALID",
                    "error": f"{type(exc).__name__}: {exc}"[:600],
                    "hint": "参数结构不合法，请修正后重新调用本工具（不要放弃交卷）。",
                },
                ensure_ascii=False,
            )
        holder["submitted"] = plan
        return json.dumps(
            {
                "status": "RECEIVED",
                "days": len(plan.days),
                "items": sum(len(day.items) for day in plan.days),
                "message": "行程已收到。请停止调用工具，用一句话说明本次规划的结论与未获取到的信息。",
            },
            ensure_ascii=False,
        )

    return submit_final_plan


# ======================================================================
# 运行上下文（system prompt 之外的这一趟的输入）
# ======================================================================


def _prefetch_digest(
    prefetch: Any, selections: dict[str, str], *, limit: int = 12
) -> dict[str, Any]:
    """把 Discovery 预取摘要成一段"已经有这些候选，别重复查"的 JSON。

    只给摘要（前 N 条 + 关键字段 + 用户对该地点选过什么），不给原始 payload：预取里可能有
    几十个 POI 与几 KB 攻略正文，全量灌进 prompt 会让 Agent 的第一次模型调用就吃满上下文。
    候选的 place_id / source_id 原样保留 —— Agent 之后可以按 id 指回真实证据。
    """

    if prefetch is None:
        return {}

    def _place_rows(places: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "place_id": getattr(place, "place_id", None),
                "name": getattr(place, "name", ""),
                "type": getattr(place, "type", None),
                "area": getattr(place, "business_area", None) or getattr(place, "district", None),
                "amap_verified": getattr(place, "amap_verified", None),
                "selection": selections.get(str(getattr(place, "place_id", ""))),
            }
            for place in list(getattr(prefetch, "places", None) or [])[:limit]
        ]

    def _option_rows(options: list[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for option in list(options or [])[:limit]:
            rows.append(
                {
                    "label": getattr(option, "label", None),
                    "provider": getattr(option, "provider", None),
                    "departure_at": str(getattr(option, "departure_at", "") or ""),
                    "arrival_at": str(getattr(option, "arrival_at", "") or ""),
                    "duration_minutes": getattr(option, "duration_minutes", None),
                    "price": getattr(option, "price", None),
                    "source_id": getattr(option, "source_id", None),
                }
            )
        return rows

    return {
        "outbound": _option_rows(list(getattr(prefetch, "outbound", None) or [])),
        "inbound": _option_rows(list(getattr(prefetch, "inbound", None) or [])),
        "hotels": [
            {
                "name": getattr(hotel, "name", ""),
                "business_area": getattr(hotel, "business_area", None),
                "price_per_night": getattr(hotel, "price_per_night", None),
                "rating": getattr(hotel, "rating", None),
                "source_id": getattr(hotel, "source_id", None),
            }
            for hotel in list(getattr(prefetch, "hotels", None) or [])[:limit]
        ],
        "places": _place_rows(list(getattr(prefetch, "places", None) or [])),
        "evidence": [
            {
                "id": getattr(evidence, "id", None),
                "title": getattr(evidence, "title", ""),
                "source_id": getattr(evidence, "source_id", None),
                "text": (getattr(evidence, "text", "") or "")[:160],
            }
            for evidence in list(getattr(prefetch, "evidences", None) or [])[:limit]
        ],
        "provider_calls": len(list(getattr(prefetch, "provider_calls", None) or [])),
        "degradations": list(getattr(prefetch, "degradations", None) or []),
    }


def _user_prompt(
    query: str,
    *,
    intent: TripIntent | None,
    prefetch: Any,
    source: str,
) -> str:
    """给 Agent 的这一趟输入：用户原话 + 已确认的结构化事实 + 预取摘要。

    system prompt 是"怎么规划"（静态、带版本），这里是"这一趟规划什么"（每 run 不同），
    两者分开正是为了让 prompt 版本号有意义。
    """

    lines = [f"用户需求：{query}", ""]
    if source and source != "quick":
        lines.append(f"（本次规划来自：{source}）")
    selections: dict[str, str] = {}
    if intent is not None:
        selections = {str(key): str(value) for key, value in (intent.place_selections or {}).items()}
        lines += [
            "",
            "【用户已在前端确认的结构化信息（以此为准，不要重新猜）】",
            json.dumps(intent.model_dump(mode="json", exclude_unset=True), ensure_ascii=False),
        ]
        must = [key for key, value in selections.items() if value.upper() == "MUST"]
        reject = [key for key, value in selections.items() if value.upper() == "REJECT"]
        want = [key for key, value in selections.items() if value.upper() == "WANT"]
        if must:
            lines.append(f"必须安排（MUST）：{'、'.join(must)}")
        if want:
            lines.append(f"尽量安排（WANT）：{'、'.join(want)}")
        if reject:
            lines.append(f"**绝对不允许出现（REJECT）**：{'、'.join(reject)}")
    digest = _prefetch_digest(prefetch, selections)
    if any(digest.get(key) for key in ("outbound", "inbound", "hotels", "places", "evidence")):
        lines += [
            "",
            "【Discovery 已经查过的候选（可直接采用，不必重复查询；要更多再用工具查）】",
            json.dumps(digest, ensure_ascii=False),
        ]
    lines += [
        "",
        (
            "请开始：先补齐缺的关键事实（大交通、住宿、必去地点的实际时间与坐标），"
            "再排逐日行程，自查后调用 submit_final_plan 交卷。"
        ),
    ]
    return "\n".join(lines)


# ======================================================================
# 模型 / 度量
# ======================================================================


def _resolve_model(model: Any | None) -> tuple[Any | None, list[Any]]:
    """主模型 + 降级中间件。

    延迟导入 `app.agent`：那个模块是装配层，它需要 `execute_agent_run` 来接管规划路径，
    模块级互相 import 会成环。
    """

    if model is not None:
        return model, []
    from app.agent import build_model_with_fallbacks

    return build_model_with_fallbacks()


def _llm_result_to_audit(result: Any) -> dict[str, Any]:
    """`LLMResult.to_audit()` 的同一份输出；拿不到那个类时返回一份同形空壳。

    Agent 路径的模型调用账本里没有 prompt 预览（输入在 Agent 的对话状态里，不在这个账本），
    所以预览字段一律为 null —— "这里没有"和"模型没收到输入"必须能分开。
    """

    to_audit = getattr(result, "to_audit", None)
    if callable(to_audit):
        return to_audit()
    return {
        "tag": getattr(result, "tag", None),
        "model": getattr(result, "model", None),
        "status": getattr(result, "status", None),
        "duration_ms": getattr(result, "duration_ms", None),
        "started_at": getattr(result, "started_at", None),
        "finished_at": getattr(result, "finished_at", None),
        "error": getattr(result, "error", None),
        "chars": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "total_tokens": None,
        "system_preview": None,
        "user_preview": None,
    }


class _AgentModelLedger:
    """把 Agent Loop 的模型调用账本适配成 `_build_audit` 认识的 `LLM` 形状。

    Agent 的模型调用不经过 `app.llm.LLM`（调用方是 LangChain），账本在 `trace_spans` 上
    （component=llm，由 StepReporter 逐次写）。这里把它们还原成 `LLMResult`：
    audit_report.json 的 `llm_calls` 因此与固定流程同形，管理端不需要为 Agent 路径
    另写一套解析。取不到 span 时构造出来是空账本 —— 空即是空，不补零。
    """

    def __init__(self, spans: list[dict[str, Any]], *, model_name: str = "") -> None:
        self.calls: list[Any] = []
        for span in spans:
            attributes = span.get("attributes") or {}
            result = _LLMResultLike(
                status="OK" if str(span.get("status")) == "SUCCESS" else "FAILED",
                model=str(attributes.get("model") or model_name or ""),
                tag=str(attributes.get("tag") or attributes.get("model") or "agent"),
                duration_ms=attributes.get("duration_ms"),
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                error=span.get("error"),
                input_tokens=attributes.get("input_tokens"),
                output_tokens=attributes.get("output_tokens"),
                cached_tokens=attributes.get("cached_tokens"),
            )
            self.calls.append(result)

    def audit_entries(self) -> list[dict[str, Any]]:
        return [_llm_result_to_audit(call) for call in self.calls]


class _LLMResultLike:
    """Agent 路径的"一次模型调用"。

    刻意不再造一个 `LLMResult` 的孪生类：把 token 与耗时按 `app.llm.LLMResult` 的字段名
    平铺（`to_audit` 由 `_llm_result_to_audit` 负责调用真实现），字段名与 audit 的键一一对应，
    少一层映射就少一处漂移。
    """

    __slots__ = (
        "cached_tokens",
        "duration_ms",
        "error",
        "finished_at",
        "input_tokens",
        "model",
        "output_tokens",
        "started_at",
        "status",
        "tag",
    )

    def __init__(self, **kwargs: Any) -> None:
        for key in self.__slots__:
            setattr(self, key, kwargs.get(key))

    @property
    def ok(self) -> bool:
        return str(self.status) == "OK"

    @property
    def usage(self) -> dict[str, Any] | None:
        values = (self.input_tokens, self.output_tokens, self.cached_tokens)
        if all(value is None for value in values):
            return None
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
        }

    def to_audit(self) -> dict[str, Any]:
        input_tokens, output_tokens, cached_tokens = (
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
        )
        total = (
            None
            if input_tokens is None and output_tokens is None
            else int(input_tokens or 0) + int(output_tokens or 0)
        )
        return {
            "tag": self.tag,
            "model": self.model,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "chars": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total,
            "system_preview": None,
            "user_preview": None,
        }


def _metrics(
    *,
    started: float,
    totals: dict[str, int],
    hub: Any,
    badcase_count: int,
    degradations: list[str],
) -> dict[str, Any]:
    """run 级汇总指标。取不到的 token 写 None（"不知道"），绝不写 0 冒充。"""

    llm_calls = int(totals.get("llm_calls") or 0)
    has_usage = llm_calls > 0
    provider_calls = _hub_audit_entries(hub)
    config = current_config()
    price_in = config.model_price_input_per_million
    price_out = config.model_price_output_per_million
    price_cached = config.model_price_cached_per_million
    input_tokens = int(totals.get("input_tokens") or 0)
    output_tokens = int(totals.get("output_tokens") or 0)
    cached_tokens = int(totals.get("cached_tokens") or 0)
    cost: float | None = None
    if has_usage and price_in is not None and price_out is not None:
        cached_price = price_cached if price_cached is not None else price_in
        cost = round(
            input_tokens / 1_000_000 * price_in
            + output_tokens / 1_000_000 * price_out
            + cached_tokens / 1_000_000 * cached_price,
            6,
        )
    return {
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "input_tokens": input_tokens if has_usage else None,
        "output_tokens": output_tokens if has_usage else None,
        "cached_tokens": cached_tokens if has_usage else None,
        "total_tokens": (input_tokens + output_tokens) if has_usage else None,
        "llm_calls": llm_calls,
        # Agent 路径没有 Jev（方案选择由 prompt 承担），如实写 0 而不是沿用旧字段的语义。
        "jev_calls": 0,
        "tool_calls": len(provider_calls),
        "provider_failures": sum(1 for call in provider_calls if call.get("status") != "OK"),
        "badcase_count": badcase_count,
        "cost": cost,
        "cost_source": "user_price" if cost is not None else None,
        "agent": {
            "prompt_version": TRAVEL_PLANNER_PROMPT_VERSION,
            "degraded": bool(degradations),
        },
    }


def _hub_audit_entries(hub: Any) -> list[dict[str, Any]]:
    try:
        return list(hub.audit_entries() or []) if hub is not None else []
    except Exception:  # noqa: BLE001 —— 审计取不到只影响展示，不影响 run 的结论
        return []


def _truncation_kind(error: BaseException | None) -> str | None:
    """判断"循环为什么停下来"：步数用尽 / 成本护栏 / 都不是。

    要看**异常链**而不是只看最外层：LangGraph 与 LangChain 的 retry middleware 都可能把
    内层异常重新包一层抛出来，只看最外层会把"超步数"误报成普通错误。
    """

    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _TokenBudgetExceeded):
            return "tokens"
        if type(current).__name__ == "GraphRecursionError":
            return "steps"
        current = current.__cause__ or current.__context__
    return None


def _run_agent_loop(factory: Any, *, timeout: float) -> tuple[str, Any]:
    """跑 Agent Loop 的协程并加**硬**墙钟上限，返回 `(outcome, payload)`。

    outcome ∈ `{"ok", "timeout", "error"}`；error 时 payload 是异常对象。

    为什么要两层超时：`asyncio.wait_for` 只保证"协程被取消"，而取消后事件循环收尾要
    `shutdown_default_executor()` —— 同步工具（高德 / 12306 / 途牛全是阻塞 HTTP）跑在
    默认执行器的线程里，一个卡死的调用会把收尾拖住，于是"600 秒超时"实际上不成立。
    所以内层用 `wait_for` 发起取消，外层再套一个**守护线程 + join 预算**
    （`app.observability.call_with_deadline`，与固定流程的 Provider 超时同一套原语）：
    到点就放弃等待，不管那条线程还在做什么。被放弃的线程里不会再有结果回到本次 run。
    """

    budget = max(1.0, float(timeout))

    async def main() -> Any:
        return await asyncio.wait_for(factory(), budget)

    def worker() -> tuple[str, Any]:
        try:
            return ("ok", asyncio.run(main()))
        except (asyncio.TimeoutError, TimeoutError):
            return ("timeout", None)
        except BaseException as exc:  # noqa: BLE001 —— 原样带回给调用方判定
            return ("error", exc)

    value, timed_out, error = call_with_deadline(worker, timeout=budget + 5.0, label="agent-loop")
    if timed_out:
        return "timeout", None
    if error is not None:
        return "error", RuntimeError(error)
    return value


# ======================================================================
# 主入口
# ======================================================================


def execute_agent_run(
    query: str,
    *,
    user_id: str = "anonymous",
    thread_id: str | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    debug: bool = False,
    store: TravelPlanStore | None = None,
    model: Any | None = None,
    hub: ProviderHub | None = None,
    run_id: str | None = None,
    emit: Any | None = None,
    intent: TripIntent | None = None,
    prefetch: Any | None = None,
    source: str = "quick",
    source_session_id: str | None = None,
) -> RunResult:
    """跑一次 Agent Loop 规划：Agent 调工具 → 交卷 → 时间兜底 → 落库成文审计。

    签名与 `execute_travel_run` 一致（除了固定流程专属的 `llm` / `jev`：Agent 路径的模型
    调用走 LangChain，没有 `app.llm.LLM` 账本；Jev 已砍掉）。`store` / `model` / `hub`
    可以注入，是为了让单测与 Benchmark 能在不联网、不写真实库的前提下跑完整条路。

    失败与降级的原则只有一条：**拿不到行程就说拿不到**。绝不因为"必须返回点什么"而
    编一份 plan；`status=FAILED` + 明确的 error 比一份看起来合理的假行程有价值得多。
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
    config = current_config()
    #: 降级说明：进 RunResult.degradations（前端提示）与 run_metrics（管理端）。
    degradations: list[str] = []
    #: 交卷 holder：Agent 调 submit_final_plan 时写入。用可变容器是因为工具闭包拿不到
    #: 外层函数的赋值（闭包只能读，重新绑定写不回去）。
    holder: dict[str, Any] = {}
    reporter = build_step_reporter(
        resolved_store, resolved_run_id, parent_span_id=f"{resolved_run_id}:agent"
    )
    owns_hub = hub is None
    resolved_hub = hub or ProviderHub(
        run_id=resolved_run_id,
        store=resolved_store,
        mcp_servers=default_mcp_servers(),
        emit=emit,
    )
    hub_closed = False

    def close_hub() -> None:
        """释放自建的 Hub（MCP 子进程 / 事件循环）。幂等：成功与失败两条路都会走到。"""

        nonlocal hub_closed
        if owns_hub and not hub_closed:
            hub_closed = True
            resolved_hub.close()

    # Discovery 过户：复用来的调用与证据要在落 plan 之前写进本次 run 的 sources，
    # 否则 audit 里的"这条数据是复用的"无从体现（两个 helper 都自带逐条兜底）。
    adopted_calls: list[dict[str, Any]] = []
    cached_evidence: list[dict[str, Any]] = []
    if prefetch is not None:
        adopted_calls = _adopt_prefetch_sources(resolved_store, resolved_run_id, prefetch)
        cached_evidence = _materialize_city_evidence(resolved_store, resolved_run_id, prefetch)

    #: 喂 `_build_audit` / `_detect_and_save_badcases` 的最小 state。
    #: 这两个 helper 都只 `state.get(...)`（只有 run_id / store 是硬取），所以一个 dict
    #: 就够 —— 不为了"形状像 TravelState"而编造 places / evidences 这些本次真的没有的东西。
    state: dict[str, Any] = {
        "run_id": resolved_run_id,
        "query": query,
        "user_id": user_id,
        "status": STATUS_COMPLETED,
        "output_dir": str(output_dir),
        "store": resolved_store,
        "hub": resolved_hub,
        "prefetch": prefetch,
        "adopted_calls": adopted_calls,
        "cached_evidence": cached_evidence,
        # Agent 路径不落 places / evidence 表（取数由 Agent 在循环里完成，Python 只看到
        # 它交回来的 plan），所以这三项如实为空。
        "places": [],
        "evidences": [],
        "candidate_scores": {},
        "jev_calls": [],
        "stages": [],
        "timeline": [],
        "llm": None,
    }

    def finish_failed(error: str) -> RunResult:
        """如实落一个 failed run，并把已经发生的事实（trace/metrics）留下来。"""

        resolved_store.finish_run(resolved_run_id, STATUS_FAILED, error=error)
        metrics = _metrics(
            started=started,
            totals=reporter.token_totals(),
            hub=resolved_hub,
            badcase_count=0,
            degradations=degradations,
        )
        _save_metrics(resolved_store, resolved_run_id, metrics)
        outputs = _write_failure_artifacts(resolved_run_id, resolved_store, Path(output_dir), metrics)
        close_hub()
        return RunResult(
            run_id=resolved_run_id,
            status=STATUS_FAILED,
            error=error,
            outputs=outputs,
            degradations=list(degradations),
        )

    agent_model, fallback_middleware = _resolve_model(model)
    if agent_model is None:
        return finish_failed(
            "未配置可用的模型：请在 .env 配 MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY"
            "（可另配 _bk1 / _bk2 作为降级），Agent 规划无法开始。"
        )

    travel_tools = build_travel_tools(resolved_hub)
    submit_tool = _build_submit_tool(holder)
    # web_search 必须从手动 tools 里剔除：它与 SuperHarness 自动发现的同名内置工具
    # 会触发 ValueError（见模块 docstring 第 2 条）。剔除后它仍在可见列表里 ——
    # 用 hub 版顶上，由 _AlwaysVisibleTools 补回来。
    manual_tools = [item for item in travel_tools if getattr(item, "name", None) != "web_search"]
    visible_tools = [*travel_tools, submit_tool]
    middleware = [
        reporter.middleware,
        *fallback_middleware,
        _AlwaysVisibleTools(visible_tools),
        _TokenBudgetGuard(config.max_run_tokens, reporter.token_totals),
    ]

    from superharness import HarnessContext
    from superharness.agent import create_harness_agent

    agent = create_harness_agent(
        system_prompt=TRAVEL_PLANNER_SYSTEM_PROMPT,
        tools=[*manual_tools, submit_tool],
        model=agent_model,
        middleware=middleware,
        name="travelplan-planner",
    )
    _record_prompt_version(resolved_store, resolved_run_id, tools=visible_tools)

    payload = {
        "messages": [
            {
                "role": "user",
                "content": _user_prompt(query, intent=intent, prefetch=prefetch, source=source),
            }
        ]
    }
    context = HarnessContext(user_id=user_id)
    loop_config: dict[str, Any] = {"recursion_limit": max(4, int(config.max_agent_steps))}
    if thread_id:
        # 与固定流程同形：thread_id 放 configurable（记忆/断点续跑的口径），
        # 不放 HarnessContext（见 superharness.agent.thread_config 的注释）。
        loop_config["configurable"] = {"thread_id": thread_id}

    final_state: Any = None
    truncation: str | None = None
    outcome, error_payload = _run_agent_loop(
        lambda: agent.ainvoke(payload, config=loop_config, context=context),
        timeout=float(config.agent_run_timeout_seconds),
    )
    if outcome == "timeout":
        truncation = f"Agent 循环超时（超过 {config.agent_run_timeout_seconds:g} 秒）被中止"
    elif outcome == "error":
        kind = _truncation_kind(error_payload)
        if kind == "steps":
            truncation = f"Agent 达到最大步数（{config.max_agent_steps} 步）仍未收尾"
        elif kind == "tokens":
            truncation = f"成本护栏触发：{error_payload}"
        else:
            return finish_failed(f"{type(error_payload).__name__}: {error_payload}")
    else:
        final_state = error_payload

    totals = reporter.token_totals()
    submitted: SubmittedPlan | None = holder.get("submitted")
    origin = SUBMIT_TOOL_NAME
    if submitted is None and final_state is not None:
        # 兜底：Agent 没调终结工具，但可能在最后一条消息里吐了 JSON。
        submitted = _parse_submitted(final_state)
        origin = "message_json" if submitted is not None else origin
    if submitted is None:
        detail = truncation or "Agent 没有调用 submit_final_plan，也没有在回复里给出可解析的行程 JSON"
        return finish_failed(f"{detail}；本次没有产出任何行程（不编造 plan）。")

    if truncation:
        # 已经拿到行程：循环被截断不影响这份行程的完整性，但必须如实标降级。
        degradations.append(f"{truncation}；已交卷的行程仍然保留。")

    plan = _to_trip_plan(
        submitted, run_id=resolved_run_id, query=query, reference_intent=intent
    )
    if not plan.days:
        # "没有行程"不是行程：空 days 当成成功返回，等于给前端一个空白页当成品。
        return finish_failed(
            "Agent 交回来的行程没有任何一天（days 为空），本次不产出 plan（不编造行程）。"
        )
    if origin != SUBMIT_TOOL_NAME:
        degradations.append(
            "Agent 未调用 submit_final_plan，行程由最后一条回复里的 JSON 解析而来（结构可能不完整）。"
        )
    # --- 唯一保留的 Python 兜底：时间冲突 ---
    revised_days, code_issues = _apply_time_pass(plan)
    plan.days = revised_days
    plan.warnings = _merge_warnings(list(plan.warnings), code_issues)
    unresolved = sum(1 for issue in code_issues if issue.severity == "error" and not issue.resolved)
    if unresolved:
        degradations.append(f"时间校验发现 {unresolved} 个未解决的时间冲突（见 plan.warnings）")

    decisions = list(plan.decisions)
    plan_md = _render_markdown(plan, degradations=degradations, critic={})
    resolved_store.save_decisions_bulk(resolved_run_id, decisions)
    resolved_store.save_plan(plan, plan_md)
    resolved_store.finish_run(resolved_run_id, STATUS_COMPLETED)

    # audit 的 llm_calls 从 trace 上还原：**必须等循环结束之后**再建，否则账本是空的
    # （模型调用是在这次 run 里才发生的）。
    state["llm"] = _AgentModelLedger(
        _spans_of(resolved_store, resolved_run_id, "llm"),
        model_name=str(getattr(agent_model, "model_name", "") or ""),
    )

    state["stages"] = [
        {
            "stage_id": "agent_loop",
            "title": "Agent 规划",
            "summary": f"Agent 完成 {totals.get('tool_calls', 0)} 次工具调用、"
            f"{totals.get('llm_calls', 0)} 次模型调用后交卷",
            "steps": [
                f"出计划方式：{origin}",
                f"步数上限：{config.max_agent_steps}，墙钟上限：{config.agent_run_timeout_seconds:g}s",
                f"token：输入 {totals.get('input_tokens', 0)} / 输出 {totals.get('output_tokens', 0)}",
                f"prompt 版本：{TRAVEL_PLANNER_PROMPT_VERSION}",
            ],
            "stats": {"tool_calls": totals.get("tool_calls", 0), "llm_calls": totals.get("llm_calls", 0)},
        }
    ]
    state["timeline"] = [{"stage": "agent_loop", "text": "Agent 交卷", "at": now_iso()}]
    badcases = _detect_and_save_badcases(state, plan=plan, degradations=degradations)
    audit = _build_audit(state, plan=plan, decisions=decisions, degradations=degradations, prose_note="")
    audit["agent"] = {
        "prompt_version": TRAVEL_PLANNER_PROMPT_VERSION,
        "plan_origin": origin,
        "max_steps": config.max_agent_steps,
        "timeout_seconds": config.agent_run_timeout_seconds,
        "truncation": truncation,
    }
    outputs = _write_artifacts(
        plan=plan, plan_md=plan_md, audit=audit, output_dir=Path(output_dir)
    )
    metrics = _metrics(
        started=started,
        totals=totals,
        hub=resolved_hub,
        badcase_count=len(badcases),
        degradations=degradations,
    )
    metrics["agent"]["plan_origin"] = origin
    _save_metrics(resolved_store, resolved_run_id, metrics)
    if degradations:
        # 保留 runs.status="completed" 兼容旧 API，只把管理端状态提升为 DEGRADED。
        try:
            resolved_store.set_progress_status(
                resolved_run_id, "DEGRADED", "规划完成，但存在降级"
            )
        except Exception:
            logger.debug("提升 run_progress 状态为 DEGRADED 失败", exc_info=True)
    outputs = {
        **outputs,
        **_write_run_artifacts(
            run_id=resolved_run_id,
            store=resolved_store,
            output_dir=Path(output_dir),
            metrics=metrics,
            badcases=badcases,
        ),
    }
    close_hub()
    return RunResult(
        run_id=resolved_run_id,
        status=STATUS_COMPLETED,
        plan=plan,
        plan_md=plan_md,
        audit=audit,
        outputs=outputs,
        degradations=degradations,
    )


# ======================================================================
# 兜底解析 / 观测小工具
# ======================================================================


def _parse_submitted(state: Any) -> SubmittedPlan | None:
    """从 Agent 最后一条回复里解析行程 JSON（没调用终结工具时的退路）。

    解析不出来就返回 None —— 由调用方落 FAILED，绝不半猜半补。
    """

    payload = _extract_json_object(_last_ai_text(state))
    if payload is None:
        return None
    # 允许模型把行程包在 {"plan": {...}} / {"trip_plan": {...}} 里（两种都很常见）。
    for key in ("plan", "trip_plan", "itinerary"):
        nested = payload.get(key)
        if isinstance(nested, dict) and ("days" in nested or "intent" in nested):
            payload = nested
            break
    if "intent" not in payload:
        # 缺 intent 时**不**编一个 TripIntent：给一个空容器，并在 degradations 里如实说明
        # 结构不完整（调用方已确认的 intent 还是会经 _merge_intent 盖上去）。
        payload = {**payload, "intent": {}}
    try:
        return SubmittedPlan.model_validate(payload)
    except Exception:  # noqa: BLE001 —— 结构不对就是"拿不到行程"，不修修补补当成功
        return None


def _spans_of(store: Any, run_id: str, component: str) -> list[dict[str, Any]]:
    try:
        spans = store.get_trace_spans(run_id)
    except Exception:  # noqa: BLE001 —— 观测读不到不该拦住规划
        return []
    return [span for span in spans if str(span.get("component")) == component]


def _record_prompt_version(store: Any, run_id: str, *, tools: list[Any]) -> None:
    """把 prompt 版本写进 trace（admin 要能回答"这次跑的是哪一版 prompt"）。"""

    try:
        store.save_trace_span(
            run_id,
            span_id(run_id, SpanKind.WORKFLOW, "agent_prompt"),
            component=str(SpanKind.WORKFLOW),
            name="prompt_version",
            status="SUCCESS",
            started_at=now_iso(),
            attributes={
                "prompt_version": TRAVEL_PLANNER_PROMPT_VERSION,
                "system_prompt_chars": len(TRAVEL_PLANNER_SYSTEM_PROMPT),
                "tools": [getattr(item, "name", "") for item in tools],
                "agent": "superharness",
            },
        )
    except Exception:
        logger.debug("prompt 版本未写入 trace", exc_info=True)


def _save_metrics(store: Any, run_id: str, metrics: dict[str, Any]) -> None:
    try:
        store.save_run_metrics(run_id, metrics)
    except Exception:
        logger.debug("run_metrics 写入失败", exc_info=True)


def _write_failure_artifacts(
    run_id: str, store: TravelPlanStore, output_dir: Path, metrics: dict[str, Any]
) -> dict[str, str]:
    """失败 run 也把 trace/metrics 落盘。

    为什么失败也要写：失败的 run 最需要复盘 —— 它到底调了哪些工具、token 花在哪一步。
    但没有 plan 就**不建** plan 三件套（那会变成伪造成品）；`_write_run_artifacts` 只写
    trace.jsonl / metrics.json / badcases.json，语义正好。
    """

    try:
        return _write_run_artifacts(
            run_id=run_id, store=store, output_dir=output_dir, metrics=metrics, badcases=[]
        )
    except Exception:  # noqa: BLE001 —— 写盘失败不能改变 run 的结论
        return {}
