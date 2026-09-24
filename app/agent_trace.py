"""Agent Loop 的「步骤上报」：把 Agent 每一步变成前端能轮询、admin 能复盘的事实。

为什么需要它
------------
主规划改成 SuperHarness Agent Loop 之后，「这一步在做什么」不再由固定 12 步图的节点
决定，而是 Agent 自己在循环里选工具。于是有两件事必须被看见：

* 前端要**实时**回答「在查机票」「在查酒店」（轮询现有字段，见下）；
* admin 要在事后看到每一步的耗时与 token。

SuperHarness 原生 EventBus 只把事件交给 Console / JSONL，不落业务库，所以这里做一个
薄的**观测桥**：把 Agent Loop 的模型调用与工具调用翻译进现成的三张表
（``run_stages`` / ``trace_spans`` / ``run_progress``）。**不新建表、不改表结构**。

落库字段（全部复用现有列）
--------------------------
* 工具调用 →
  ``run_stages``（stage_id = 中文展示名，``facts_json`` = ``{tool, status, duration_ms,
  started, finished, seq, stage_key, query, note, error}``）
  ＋ ``trace_spans``（component=``tool``，attributes 带 tool / status / duration_ms / query）。
* 模型调用 →
  ``trace_spans``（component=``llm``，attributes 带 input_tokens / output_tokens /
  cached_tokens / **total_tokens**，键名与 ``app/admin_timeline.py`` 读的一致；
  ``llm.finished`` 事件里没有 total_tokens，这里按 ``input + output`` 自己算）
  ＋ ``run_stages``（stage_id = 「在思考行程」，让前端在模型思考期间不停留在上一个工具名）。
* ``run_progress.current_stage`` 由 ``store.update_stage`` 一并更新 —— **它就是前端要轮询的字段**。

为什么 stage_id 用中文展示名，而不是工具名
------------------------------------------
``store.update_stage(run_id, stage_id, ...)`` 会把 ``stage_id`` 同时写进
``run_progress.current_stage``（``app/store.py:913``）。需求是「前端轮询到当前步骤的中文
展示名」，也就是 ``current_stage`` 必须是「在查酒店」这类文案，所以 stage_id 用展示名；
工具名与序号（机器可读的那份身份）放在 ``facts_json.tool`` / ``facts_json.stage_key``，
工具调用的 trace span ``name`` 就是工具名 —— admin 要按工具维度聚合时读 span。

同一个工具的第 N 次调用共享同一行 stage（主键 ``run_id + stage_id``），每次调用用最新的
facts 覆盖它（先 RUNNING 后 SUCCESS，前端因此能看到「正在查」）。**逐次调用的完整事实在
``trace_spans``**：span_id 带序号（``<run>:tool:<name>:2``），不会被顶掉。

怎么挂到 Agent 上（二选一）
--------------------------
推荐 —— 自定义 Middleware（能拿到每次工具 / 模型调用**前后**的钩子，且不依赖
SuperHarness 把 EventBus 暴露出来）::

    reporter = build_step_reporter(store, run_id)
    agent = create_harness_agent(..., tools=..., middleware=[reporter.middleware])
    # 固定流程 + Agent Loop 一起挂：create_travel_app(..., middleware=[reporter.middleware])

备选 —— EventBus 订阅（``create_harness_app`` 内部自己 new 了 ObservabilityMiddleware，
runner 只暴露 ``emit`` 方法、拿不到 ``bus``；只有自建 agent 时才用得上）::

    reporter = build_step_reporter(store, run_id)
    reporter.subscribe(observability_middleware.bus)

两条路走同一套落库逻辑，**二选一即可**（同时挂会把每一步记两遍）。

底线
----
观测失败绝不能影响主流程：所有落库调用都过 ``_guard``，异常只记 debug 日志
（写法同 ``app/agent.py::_Emitter``）。工具/模型本身抛的异常则原样向上抛 —— 上报吞掉的
只能是自己的错误，不能吞业务错误。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from superharness.observability import EventType

from .observability import SpanKind, StageStatus, now_iso, start_iso
from .observability import span_id as build_span_id

logger = logging.getLogger(__name__)

# ======================================================================
# 展示名（工具名 → 中文步骤文案）
# ======================================================================
# 唯一一份映射在 `app/travel_tools.py::TOOL_DISPLAY_NAMES`（工具定义与展示名放一起，
# 新接一个工具时不会漏改展示名）。这里只做**再导出**，不复制第二套。
# 兜底只为「travel_tools 暂时 import 不动」（比如它正在被重构、或依赖缺失）时不让
# 整个 app 起不来：此时未知工具显示「在调用 xxx」，比让规划失败好得多。
try:  # pragma: no cover - 正常路径都会命中
    from .travel_tools import TOOL_DISPLAY_NAMES as _TOOL_DISPLAY_NAMES
except Exception:  # noqa: BLE001
    logger.debug("app.travel_tools 不可用，步骤展示名退化为内置子集", exc_info=True)
    _TOOL_DISPLAY_NAMES = {
        "search_trains": "在查火车票",
        "search_flights": "在查机票",
        "search_hotels": "在查酒店",
        "search_scenic_tickets": "在查景区门票",
        "search_poi": "在搜周边地点",
        "search_xiaohongshu": "在翻小红书攻略",
        "search_douyin": "在翻抖音攻略",
        "web_search": "在搜网页资料",
    }

#: 再导出一份**副本**：调用方可能想追加自己的映射（如某个 MCP Server 的工具），
#: 改副本不会污染工具定义那一份。
TOOL_DISPLAY_NAMES: dict[str, str] = dict(_TOOL_DISPLAY_NAMES)

#: 模型调用期间的步骤文案。模型在想的时候，前端不该还停在最后一个工具名上。
MODEL_STEP_LABEL = "在思考行程"

#: 没有映射的工具的文案前缀。
UNKNOWN_TOOL_PREFIX = "在调用"

#: 工具 facts 的文本预览上限（trace 是给人看的，不是全量归档）。
PREVIEW_CHARS = 400
#: 用户明确允许管理端保留的模型交互预览上限。每段先脱敏，再最多保留 200 字符。
LLM_PREVIEW_CHARS = 200

# ======================================================================
# 兜底脱敏 / 截断
# ======================================================================
# `app/redact.py` 是全仓唯一一份「哪些字符串不能出现在输出里」；它 import 了
# providers（较重）。这里照样用 try/except 包一层：观测模块不该成为 app 起不来的原因。
try:  # pragma: no cover - 正常路径都会命中
    from .redact import clip as _redact_clip
    from .redact import scrub as _redact_scrub
except Exception:  # noqa: BLE001
    def _redact_scrub(text: str) -> str:
        return text

    def _redact_clip(text: str, *, limit: int = 2000) -> str:
        return text if len(text) <= limit else f"{text[:limit]}…（已截断，原文 {len(text)} 字符）"


def _preview(value: Any, *, limit: int = PREVIEW_CHARS) -> str:
    """把任意对象压成一段可落库的预览文本（先脱敏，再截断）。"""

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 —— 预览失败不该让上报失败
            text = str(value)
    return _redact_clip(_redact_scrub(text), limit=limit)


def _llm_preview(value: Any) -> str | None:
    """LLM 正文仅保存用户已授权的脱敏 200 字预览；空内容不伪造为空串。"""

    text = _preview(value, limit=LLM_PREVIEW_CHARS)
    return text or None


def _message_text(message: Any) -> str:
    """兼容 LangChain 的字符串与 content-block 消息形状，不读取其他对象属性。"""

    if message is None:
        return ""
    return _preview(getattr(message, "content", message), limit=LLM_PREVIEW_CHARS)


def _message_role(message: Any) -> str:
    role = getattr(message, "role", None) or getattr(message, "type", None)
    if role:
        return str(role).lower()
    name = type(message).__name__.lower()
    if "system" in name:
        return "system"
    if "human" in name or "user" in name:
        return "user"
    if "tool" in name:
        return "tool"
    return "assistant" if "ai" in name or "assistant" in name else "context"


def _request_model_previews(request: Any) -> dict[str, str | None]:
    """把模型实际收到的消息按 system / user / context 分段，之后统一脱敏截断。"""

    system_parts = [_message_text(getattr(request, "system_message", None))]
    user_parts: list[str] = []
    context_parts: list[str] = []
    for message in getattr(request, "messages", None) or []:
        text = _message_text(message)
        if not text:
            continue
        role = _message_role(message)
        if role == "system":
            system_parts.append(text)
        elif role in {"human", "user"}:
            user_parts.append(text)
        else:
            context_parts.append(text)

    def combine(parts: list[str]) -> str | None:
        unique = list(dict.fromkeys(part for part in parts if part))
        return _llm_preview("\n\n".join(unique)) if unique else None

    return {
        "system_preview": combine(system_parts),
        "user_preview": combine(user_parts),
        "context_preview": combine(context_parts),
    }


def _response_preview(response: Any) -> str | None:
    messages = getattr(response, "result", None) or []
    return _llm_preview(_message_text(messages[-1])) if messages else None


def display_name_for(tool: str) -> str:
    """工具名 → 前端展示文案。

    先精确匹配，再按后缀匹配（``tuniu_search_hotels`` / ``railway_12306_get_tickets``
    这类带 Provider 前缀的工具名 —— 与 ``app/admin_timeline.py::_stage_for_tool`` 同一套
    规则）。都没命中就显示「在调用 <工具名>」，而不是回退成一个像是有意义的文案。
    """

    name = str(tool or "").strip()
    if not name:
        return MODEL_STEP_LABEL
    exact = TOOL_DISPLAY_NAMES.get(name)
    if exact:
        return exact
    for known, label in TOOL_DISPLAY_NAMES.items():
        if name.endswith(known):
            return label
    return f"{UNKNOWN_TOOL_PREFIX} {name}"


# ======================================================================
# 步骤句柄
# ======================================================================


@dataclass(slots=True)
class ToolStep:
    """一次工具调用的进行中状态。``begin_tool`` 返回，``finish_tool`` 收口。"""

    name: str
    display_name: str
    stage_key: str
    seq: int
    span_id: str
    started_at: str
    start_perf: float
    query: str = ""


@dataclass(slots=True)
class ModelStep:
    """一次模型调用的进行中状态。``begin_model`` 返回，``finish_model`` 收口。"""

    model: str
    seq: int
    span_id: str
    started_at: str
    start_perf: float
    system_preview: str | None = None
    user_preview: str | None = None
    context_preview: str | None = None


# ======================================================================
# 上报器
# ======================================================================


class StepReporter:
    """拿到 ``store`` + ``run_id`` 就能工作：工具/模型每一步都落进现有表。

    ``run_id`` 可以是字符串，也可以是一个零参可调用对象（返回当前 run_id）——
    后者用于「一个 middleware 实例 + 线程局部的 run_id」的常驻装配。
    """

    def __init__(
        self,
        store: Any,
        run_id: str | Callable[[], str],
        *,
        parent_span_id: str | None = None,
    ) -> None:
        self.store = store
        self._run_id = run_id
        #: 这些 span 挂在哪条父 span 下。固定流程按 ``<run_id>:<stage_id>`` 记账，
        #: 传进来 admin 就能把工具 span 归到对应阶段；拿不到就留空。
        self.parent_span_id = parent_span_id
        self._lock = threading.Lock()
        self._tool_calls = 0
        self._tool_seq: dict[str, int] = {}
        self._llm_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._middleware: StepReporterMiddleware | None = None
        self._pending_tools: dict[str, ToolStep] = {}
        self._pending_models: dict[str, ModelStep] = {}

    # ------------------------------------------------------------------
    # 装配入口
    # ------------------------------------------------------------------

    @property
    def middleware(self) -> StepReporterMiddleware:
        """本上报器的 Middleware（每次返回同一个实例，避免重复挂）。"""

        if self._middleware is None:
            self._middleware = StepReporterMiddleware(self)
        return self._middleware

    def subscribe(self, bus: Any) -> StepReporter:
        """把上报器挂到 EventBus 上（备选接入方式，见模块 docstring）。"""

        bus.subscribe(
            self.handle_event,
            EventType.TOOL_STARTED,
            EventType.TOOL_FINISHED,
            EventType.TOOL_ERROR,
            EventType.LLM_STARTED,
            EventType.LLM_FINISHED,
            EventType.LLM_ERROR,
        )
        return self

    def unsubscribe(self, bus: Any) -> None:
        bus.unsubscribe(self.handle_event)

    # ------------------------------------------------------------------
    # 运行时信息
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        value: Any = self._run_id
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 —— 解析不到 run_id 时不落库，但不抛
                logger.debug("读取 run_id 失败，本次步骤不落库", exc_info=True)
                return ""
        return str(value or "")

    def token_totals(self) -> dict[str, int]:
        """到目前为止累计的 token / 调用次数（admin 汇总 run_metrics 时直接用）。"""

        with self._lock:
            return {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cached_tokens": self._cached_tokens,
                # 事件里没有 total_tokens：按 input + output 记（cached 已含在 input 内）。
                "total_tokens": self._input_tokens + self._output_tokens,
                "llm_calls": self._llm_calls,
                "tool_calls": self._tool_calls,
            }

    # ------------------------------------------------------------------
    # 工具：开始 / 结束
    # ------------------------------------------------------------------

    def begin_tool(
        self,
        name: str,
        args: Any = None,
        *,
        span_id: str | None = None,
        started_at: str | None = None,
    ) -> ToolStep:
        """工具调用开始：run_stages 记为 RUNNING，current_stage 变成中文文案。

        ``started_at`` 仅供「只收到结束事件」的订阅路径回填真实开始时刻。
        """

        tool = str(name or "").strip() or "unknown_tool"
        run_id = self.run_id

        with self._lock:
            self._tool_calls += 1
            seq = self._tool_seq.get(tool, 0) + 1
            self._tool_seq[tool] = seq

        display = display_name_for(tool)
        stage_key = f"tool:{tool}" if seq == 1 else f"tool:{tool}:{seq}"
        started = started_at or now_iso()
        sid = span_id or build_span_id(run_id or "run", SpanKind.TOOL, tool, suffix=str(seq))
        query = _preview(args)
        step = ToolStep(
            name=tool,
            display_name=display,
            stage_key=stage_key,
            seq=seq,
            span_id=sid,
            started_at=started,
            start_perf=time.perf_counter(),
            query=query,
        )

        self._write_stage(
            display,
            StageStatus.RUNNING,
            facts=self._tool_facts(step, status=StageStatus.RUNNING, duration_ms=None, finished_at=None),
            started_at=started,
        )
        self._write_span(
            sid,
            component=SpanKind.TOOL,
            name=tool,
            status=StageStatus.RUNNING,
            started_at=started,
            attributes={
                "tool": tool,
                "status": str(StageStatus.RUNNING),
                "stage_key": stage_key,
                "seq": seq,
                "query": query,
            },
        )
        return step

    def finish_tool(
        self,
        step: ToolStep | None,
        *,
        output: Any = None,
        status: str | StageStatus = StageStatus.SUCCESS,
        error: BaseException | str | None = None,
        duration_ms: float | None = None,
        finished_at: str | None = None,
    ) -> None:
        """工具调用结束：更新同一条 stage（含耗时）＋ 补齐 tool span。"""

        if step is None:
            return
        status_text = str(status)
        finished = finished_at or now_iso()
        duration = int(duration_ms) if duration_ms is not None else int(
            (time.perf_counter() - step.start_perf) * 1000
        )
        error_text = _error_text(error)
        note = _preview(output)

        self._write_stage(
            step.display_name,
            status_text,
            facts=self._tool_facts(
                step,
                status=status_text,
                duration_ms=duration,
                finished_at=finished,
                note=note,
                error=error_text,
            ),
            started_at=step.started_at,
            finished_at=finished,
        )
        self._write_span(
            step.span_id,
            component=SpanKind.TOOL,
            name=step.name,
            status=status_text,
            started_at=step.started_at,
            finished_at=finished,
            attributes={
                "tool": step.name,
                "status": status_text,
                "duration_ms": duration,
                "stage_key": step.stage_key,
                "seq": step.seq,
                "query": step.query,
                "note": note,
                "output_chars": len(output) if isinstance(output, str) else None,
            },
            error=error_text,
        )

    def fail_tool(self, step: ToolStep | None, error: BaseException | str, **kwargs: Any) -> None:
        """工具调用失败：状态 FAILED，错误原文落进 facts 与 span。"""

        self.finish_tool(step, status=StageStatus.FAILED, error=error, **kwargs)

    # ------------------------------------------------------------------
    # 模型：开始 / 结束
    # ------------------------------------------------------------------

    def begin_model(
        self,
        model: str = "",
        *,
        span_id: str | None = None,
        started_at: str | None = None,
        system_preview: Any = None,
        user_preview: Any = None,
        context_preview: Any = None,
    ) -> ModelStep:
        """模型调用开始：current_stage 记为「在思考行程」。"""

        name = str(model or "").strip() or "llm"
        run_id = self.run_id

        with self._lock:
            self._llm_calls += 1
            seq = self._llm_calls

        started = started_at or now_iso()
        sid = span_id or build_span_id(run_id or "run", SpanKind.LLM, name, suffix=str(seq))
        step = ModelStep(
            model=name,
            seq=seq,
            span_id=sid,
            started_at=started,
            start_perf=time.perf_counter(),
            system_preview=_llm_preview(system_preview),
            user_preview=_llm_preview(user_preview),
            context_preview=_llm_preview(context_preview),
        )

        self._write_stage(
            MODEL_STEP_LABEL,
            StageStatus.RUNNING,
            facts={
                "model": name,
                "status": str(StageStatus.RUNNING),
                "duration_ms": None,
                "started": started,
                "finished": None,
                "seq": seq,
                "stage_key": f"llm:{name}",
            },
            started_at=started,
        )
        self._write_span(
            sid,
            component=SpanKind.LLM,
            name=name,
            status=StageStatus.RUNNING,
            started_at=started,
            attributes={
                "model": name,
                "tag": name,
                "status": str(StageStatus.RUNNING),
                "seq": seq,
                "system_preview": step.system_preview,
                "user_preview": step.user_preview,
                "context_preview": step.context_preview,
            },
        )
        return step

    def finish_model(
        self,
        step: ModelStep | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        status: str | StageStatus = StageStatus.SUCCESS,
        error: BaseException | str | None = None,
        duration_ms: float | None = None,
        finished_at: str | None = None,
        assistant_preview: Any = None,
    ) -> None:
        """模型调用结束：写 llm span（token 累计）＋ 收口「在思考行程」这一步。"""

        if step is None:
            return
        status_text = str(status)
        finished = finished_at or now_iso()
        duration = int(duration_ms) if duration_ms is not None else int(
            (time.perf_counter() - step.start_perf) * 1000
        )
        error_text = _error_text(error)

        with self._lock:
            self._input_tokens += max(0, int(input_tokens or 0))
            self._output_tokens += max(0, int(output_tokens or 0))
            self._cached_tokens += max(0, int(cached_tokens or 0))
            totals = {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cached_tokens": self._cached_tokens,
                "total_tokens": self._input_tokens + self._output_tokens,
                "llm_calls": self._llm_calls,
            }

        # 本次调用的 token：`llm.finished` 事件只给 input/output/cached，没有 total，
        # 所以 total 自己按 input + output 算（cached 是 input 的一部分，不重复加）。
        call_total = max(0, int(input_tokens or 0)) + max(0, int(output_tokens or 0))

        self._write_stage(
            MODEL_STEP_LABEL,
            status_text,
            facts={
                "model": step.model,
                "status": status_text,
                "duration_ms": duration,
                "started": step.started_at,
                "finished": finished,
                "seq": step.seq,
                "stage_key": f"llm:{step.model}",
                "input_tokens": max(0, int(input_tokens or 0)),
                "output_tokens": max(0, int(output_tokens or 0)),
                "cached_tokens": max(0, int(cached_tokens or 0)),
                "total_tokens": call_total,
                "error": error_text,
            },
            started_at=step.started_at,
            finished_at=finished,
        )
        self._write_span(
            step.span_id,
            component=SpanKind.LLM,
            name=step.model,
            status=status_text,
            started_at=step.started_at,
            finished_at=finished,
            attributes={
                # 键名与 app/admin_timeline.py 读的一致（tokens_in/out、total、tag、model）。
                "model": step.model,
                "tag": step.model,
                "status": status_text,
                "duration_ms": duration,
                "seq": step.seq,
                "input_tokens": max(0, int(input_tokens or 0)),
                "output_tokens": max(0, int(output_tokens or 0)),
                "cached_tokens": max(0, int(cached_tokens or 0)),
                "total_tokens": call_total,
                "system_preview": step.system_preview,
                "user_preview": step.user_preview,
                "context_preview": step.context_preview,
                "assistant_preview": _llm_preview(assistant_preview),
                # 累计值：admin 看「到这一步为止花了多少」不必自己加。
                **{f"cumulative_{key}": value for key, value in totals.items()},
            },
            error=error_text,
        )

    def fail_model(self, step: ModelStep | None, error: BaseException | str, **kwargs: Any) -> None:
        """模型调用失败：状态 FAILED，token 记为 0。"""

        self.finish_model(step, status=StageStatus.FAILED, error=error, **kwargs)

    # ------------------------------------------------------------------
    # EventBus 订阅路径（备选接入方式）
    # ------------------------------------------------------------------

    def __call__(self, event: Any) -> None:
        self.handle_event(event)

    def handle_event(self, event: Any) -> None:
        """把 SuperHarness 事件翻译成同一套落库动作（见模块 docstring 的备选接入）。"""

        try:
            self._handle_event(event)
        except Exception:  # noqa: BLE001 —— 事件消费失败不能反噬 Agent 主流程
            logger.debug("步骤上报处理事件失败", exc_info=True)

    def _handle_event(self, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        data = getattr(event, "data", None) or {}
        sid = str(getattr(event, "span_id", "") or "") or None
        stamp = _event_iso(event)

        if event_type == EventType.TOOL_STARTED:
            step = self.begin_tool(data.get("name") or "", data.get("args"), span_id=sid)
            if sid:
                self._pending_tools[sid] = step
        elif event_type == EventType.TOOL_FINISHED:
            duration = data.get("duration_ms")
            step = self._pending_tools.pop(sid or "", None) or self._synthetic_tool(data, sid, duration, stamp)
            self.finish_tool(
                step,
                output=data.get("output"),
                duration_ms=duration,
                finished_at=stamp,
            )
        elif event_type == EventType.TOOL_ERROR:
            duration = data.get("duration_ms")
            step = self._pending_tools.pop(sid or "", None) or self._synthetic_tool(data, sid, duration, stamp)
            self.fail_tool(step, _event_error(data), duration_ms=duration, finished_at=stamp)
        elif event_type == EventType.LLM_STARTED:
            step = self.begin_model(
                data.get("model") or "",
                span_id=sid,
                system_preview=data.get("system_preview"),
                user_preview=data.get("user_preview"),
                context_preview=data.get("context_preview"),
            )
            if sid:
                self._pending_models[sid] = step
        elif event_type in (EventType.LLM_FINISHED, EventType.LLM_ERROR):
            duration = data.get("duration_ms")
            step = self._pending_models.pop(sid or "", None) or self._synthetic_model(data, sid, duration, stamp)
            if event_type == EventType.LLM_ERROR:
                self.fail_model(step, _event_error(data), duration_ms=duration, finished_at=stamp)
            else:
                self.finish_model(
                    step,
                    input_tokens=_int_or_zero(data.get("input_tokens")),
                    output_tokens=_int_or_zero(data.get("output_tokens")),
                    cached_tokens=_int_or_zero(data.get("cached_tokens")),
                    duration_ms=duration,
                    finished_at=stamp,
                    assistant_preview=data.get("assistant_preview") or data.get("output"),
                )

    def _synthetic_tool(self, data: Mapping[str, Any], sid: str | None, duration: Any, finished: str) -> ToolStep:
        """只有 `tool.finished` 没有 `tool.started`（订阅接晚了）时补一个句柄。

        没有开始事件就没有真实开始时刻，用 ``finished - duration_ms`` 反推（拿不到
        duration 就退回当前时刻）—— 宁可时间窗粗糙，也不要让 span 显示成 0 耗时。
        """

        return self.begin_tool(
            data.get("name") or "",
            data.get("args"),
            span_id=sid,
            started_at=start_iso(finished, duration if isinstance(duration, (int, float)) else None),
        )

    def _synthetic_model(self, data: Mapping[str, Any], sid: str | None, duration: Any, finished: str) -> ModelStep:
        """只有 `llm.finished` 没有 `llm.started`（订阅接晚了）时补一个句柄。"""

        return self.begin_model(
            data.get("model") or "",
            span_id=sid,
            started_at=start_iso(finished, duration if isinstance(duration, (int, float)) else None),
        )

    # ------------------------------------------------------------------
    # 落库（全部经 _guard，绝不抛）
    # ------------------------------------------------------------------

    def _tool_facts(
        self,
        step: ToolStep,
        *,
        status: str,
        duration_ms: int | None,
        finished_at: str | None,
        note: str = "",
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "tool": step.name,
            "status": status,
            "duration_ms": duration_ms,
            "started": step.started_at,
            "finished": finished_at,
            "seq": step.seq,
            # 机器可读的步骤身份：run_stages.stage_id 是中文展示名（见模块 docstring），
            # 工具名 + 序号放在这里，admin 要按工具聚合时用得上。
            "stage_key": step.stage_key,
            "query": step.query,
            "note": note,
            "error": error,
        }

    def _write_stage(
        self,
        stage_id: str,
        status: str | StageStatus,
        *,
        facts: dict[str, Any],
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """写 run_stages 并把 run_progress.current_stage 推到这一步。"""

        run_id = self.run_id
        if not run_id or self.store is None:
            return
        message = str(stage_id)
        self._guard(
            f"update_stage {message}",
            lambda: self.store.update_stage(
                run_id,
                message,
                str(status),
                message=message,
                facts=facts,
                started_at=started_at,
                finished_at=finished_at,
            ),
        )

    def _write_span(
        self,
        span: str,
        *,
        component: str | SpanKind,
        name: str,
        status: str | StageStatus,
        started_at: str,
        finished_at: str | None = None,
        attributes: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        run_id = self.run_id
        if not run_id or self.store is None:
            return
        status_text = str(status)
        self._guard(
            f"save_trace_span {name}",
            lambda: self.store.save_trace_span(
                run_id,
                span,
                component=str(component),
                name=name,
                status=status_text,
                started_at=started_at,
                finished_at=finished_at,
                parent_span_id=self.parent_span_id,
                attributes=attributes or {},
                error=error,
            ),
        )

    @staticmethod
    def _guard(label: str, action: Callable[[], Any]) -> Any:
        """观测是旁路：任何失败只记 debug 日志，绝不向上抛。

        写法与 ``app/agent.py::_Emitter`` 一致 —— 一次落库失败不该把一趟旅行规划打成失败。
        """

        try:
            return action()
        except Exception:  # noqa: BLE001
            logger.debug("步骤上报失败（不影响主流程）：%s", label, exc_info=True)
            return None


# ======================================================================
# Middleware（推荐接入方式）
# ======================================================================


class StepReporterMiddleware(AgentMiddleware):
    """在工具 / 模型调用的前后各上报一次（``middleware=[reporter.middleware]`` 注入）。

    只做两件事：``handler`` 前开一步、``handler`` 后收一步。上报自己的异常全部吞掉，
    业务异常原样抛出 —— 绝不能把 Agent 的失败变成"看起来成功"。
    """

    def __init__(self, reporter: StepReporter) -> None:
        self.reporter = reporter

    # ---- 工具 ----

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        step = self._begin_tool(request)
        try:
            result = handler(request)
        except Exception as exc:
            self._finish_tool(step, status=StageStatus.FAILED, error=exc)
            raise
        self._finish_tool(step, output=getattr(result, "content", result))
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        step = self._begin_tool(request)
        try:
            result = await handler(request)
        except Exception as exc:
            self._finish_tool(step, status=StageStatus.FAILED, error=exc)
            raise
        self._finish_tool(step, output=getattr(result, "content", result))
        return result

    # ---- 模型 ----

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        step = self._begin_model(request)
        try:
            response = handler(request)
        except Exception as exc:
            self._finish_model(step, status=StageStatus.FAILED, error=exc)
            raise
        self._finish_model(step, usage=_response_usage(response), assistant_preview=_response_preview(response))
        return response

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        step = self._begin_model(request)
        try:
            response = await handler(request)
        except Exception as exc:
            self._finish_model(step, status=StageStatus.FAILED, error=exc)
            raise
        self._finish_model(step, usage=_response_usage(response), assistant_preview=_response_preview(response))
        return response

    # ---- 内部：把上报动作再包一层，连属性访问异常也不许冒出去 ----

    def _begin_tool(self, request: Any) -> ToolStep | None:
        try:
            return self.reporter.begin_tool(_request_tool_name(request), _request_tool_args(request))
        except Exception:  # noqa: BLE001
            logger.debug("工具步骤上报（开始）失败", exc_info=True)
            return None

    def _finish_tool(
        self,
        step: ToolStep | None,
        *,
        output: Any = None,
        status: str | StageStatus = StageStatus.SUCCESS,
        error: BaseException | str | None = None,
    ) -> None:
        try:
            self.reporter.finish_tool(step, output=output, status=status, error=error)
        except Exception:  # noqa: BLE001
            logger.debug("工具步骤上报（结束）失败", exc_info=True)

    def _begin_model(self, request: Any) -> ModelStep | None:
        try:
            return self.reporter.begin_model(_request_model_name(request), **_request_model_previews(request))
        except Exception:  # noqa: BLE001
            logger.debug("模型步骤上报（开始）失败", exc_info=True)
            return None

    def _finish_model(
        self,
        step: ModelStep | None,
        *,
        usage: Mapping[str, Any] | None = None,
        status: str | StageStatus = StageStatus.SUCCESS,
        error: BaseException | str | None = None,
        assistant_preview: str | None = None,
    ) -> None:
        usage = usage or {}
        try:
            self.reporter.finish_model(
                step,
                input_tokens=_int_or_zero(usage.get("input_tokens")),
                output_tokens=_int_or_zero(usage.get("output_tokens")),
                cached_tokens=_int_or_zero(usage.get("cached_tokens")),
                status=status,
                error=error,
                assistant_preview=assistant_preview,
            )
        except Exception:  # noqa: BLE001
            logger.debug("模型步骤上报（结束）失败", exc_info=True)


# ======================================================================
# 构造入口
# ======================================================================


def build_step_reporter(
    store: Any,
    run_id: str | Callable[[], str],
    *,
    parent_span_id: str | None = None,
    bus: Any = None,
) -> StepReporter:
    """拿到 ``store`` + ``run_id`` 就能工作。

    返回的 ``StepReporter`` 同时是两种形态，按接入方式二选一：

    * ``reporter.middleware`` —— 推荐：``middleware=[reporter.middleware]``；
    * ``reporter`` 本身是 EventBus handler —— 传 ``bus`` 或在装配后 ``reporter.subscribe(bus)``。
    """

    reporter = StepReporter(store, run_id, parent_span_id=parent_span_id)
    if bus is not None:
        reporter.subscribe(bus)
    return reporter


# ======================================================================
# 小工具
# ======================================================================


def _error_text(error: BaseException | str | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return _preview(error)
    return _preview(f"{type(error).__name__}: {error}")


def _event_error(data: Mapping[str, Any]) -> str:
    """`tool.error` / `llm.error` 的 data 形状：{type, error, duration_ms}。"""

    kind = str(data.get("type") or "").strip()
    text = str(data.get("error") or "").strip()
    if kind and text:
        return f"{kind}: {text}"
    return kind or text or "未知错误"


def _event_iso(event: Any) -> str:
    stamp = getattr(event, "timestamp", None)
    isoformat = getattr(stamp, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except Exception:  # noqa: BLE001
            pass
    return now_iso()


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _request_tool_name(request: Any) -> str:
    call = getattr(request, "tool_call", None)
    if isinstance(call, Mapping):
        name = call.get("name")
        if name:
            return str(name)
    tool = getattr(request, "tool", None)
    return str(getattr(tool, "name", None) or "unknown_tool")


def _request_tool_args(request: Any) -> Any:
    call = getattr(request, "tool_call", None)
    if isinstance(call, Mapping):
        return call.get("args")
    return None


def _request_model_name(request: Any) -> str:
    model = getattr(request, "model", None)
    if model is None:
        return "llm"
    return str(
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )


def _response_usage(response: Any) -> dict[str, int]:
    """从模型响应里取 usage（与 ``ObservabilityMiddleware._model_finish`` 同一套字段）。

    取不到就返回 0：宁可记 0 token，也不要编一个数字。
    """

    messages = getattr(response, "result", None) or []
    message = messages[-1] if messages else None
    usage = getattr(message, "usage_metadata", None) or {}
    if not isinstance(usage, Mapping):
        return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    details = usage.get("input_token_details") or {}
    cached = details.get("cache_read", 0) if isinstance(details, Mapping) else 0
    return {
        "input_tokens": _int_or_zero(usage.get("input_tokens")),
        "output_tokens": _int_or_zero(usage.get("output_tokens")),
        "cached_tokens": _int_or_zero(cached),
    }


__all__ = [
    "MODEL_STEP_LABEL",
    "TOOL_DISPLAY_NAMES",
    "ModelStep",
    "StepReporter",
    "StepReporterMiddleware",
    "ToolStep",
    "build_step_reporter",
    "display_name_for",
]
