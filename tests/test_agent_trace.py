"""`app/agent_trace.py` 的离线单测：Agent 步骤上报（前端实时进度 + admin 每步耗时/token）。

只写 tmp_path 下的临时 SQLite（或纯内存替身），不联网、不碰 data/travelplan.db。
替身 store 记录「被以什么字段调用」，真 store 用例验证「落库后轮询读得到什么」——
两者都要，因为观测桥的契约正好是这两面。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware.types import (
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent_trace import (
    MODEL_STEP_LABEL,
    TOOL_DISPLAY_NAMES,
    build_step_reporter,
    display_name_for,
)
from app.store import TravelPlanStore

RUN_ID = "tp-20260922-test"


# ======================================================================
# 替身 store：只记调用参数
# ======================================================================


class RecordingStore:
    """记录每一次 update_stage / save_trace_span 的参数。

    与真 store 的两条语义保持一致，否则断言会失真：
      * `update_stage` 会把 `stage_id` 一并写进 `run_progress.current_stage`；
      * 同一个 span_id 再写一次是**替换**（真库是 INSERT OR REPLACE），所以 `current_stage`
        取最后一次调用，span 取同一个 id 的最后一行。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.stages: list[dict[str, Any]] = []
        self.spans: list[dict[str, Any]] = []
        self.fail = fail

    def update_stage(
        self,
        run_id: str,
        stage_id: str,
        status: str,
        *,
        message: str = "",
        facts: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("sqlite is down")
        self.stages.append(
            {
                "run_id": run_id,
                "stage_id": stage_id,
                "status": status,
                "message": message,
                "facts": dict(facts or {}),
                "started_at": started_at,
                "finished_at": finished_at,
            }
        )

    def save_trace_span(
        self,
        run_id: str,
        span_id: str,
        *,
        component: str,
        name: str,
        status: str,
        started_at: str,
        finished_at: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("sqlite is down")
        self.spans.append(
            {
                "run_id": run_id,
                "span_id": span_id,
                "component": component,
                "name": name,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "parent_span_id": parent_span_id,
                "attributes": dict(attributes or {}),
                "error": error,
            }
        )

    # ---- 读侧（与 store.get_run_progress / get_trace_spans 同形） ----

    @property
    def current_stage(self) -> str | None:
        return self.stages[-1]["stage_id"] if self.stages else None

    def span(self, span_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.spans if row["span_id"] == span_id]
        return rows[-1] if rows else None

    def spans_of(self, component: str) -> list[dict[str, Any]]:
        """按 component 取「每个 span_id 的最后一行」——模拟 INSERT OR REPLACE。"""

        latest: dict[str, dict[str, Any]] = {}
        for row in self.spans:
            if row["component"] == component:
                latest[row["span_id"]] = row
        return list(latest.values())


class ExplodingStore:
    """两种写都抛：用来验证「观测失败绝不影响主流程」。"""

    def update_stage(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("update_stage 炸了")

    def save_trace_span(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("save_trace_span 炸了")


# ======================================================================
# 真实的 langchain 请求 / 响应形状
# ======================================================================


def tool_request(name: str, args: dict[str, Any] | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": f"call-{name}"},
        tool=None,
        state={},
        runtime=None,
    )


def model_request(model_name: str = "gpt-test") -> ModelRequest:
    return ModelRequest(
        model=SimpleNamespace(model_name=model_name),
        messages=[],
        tools=[],
        state={},
        runtime=None,
        system_message=None,
        tool_choice=None,
        response_format=None,
        model_settings=None,
    )


def model_response(
    input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0
) -> ModelResponse:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens:
        usage["input_token_details"] = {"cache_read": cached_tokens}
    return ModelResponse(result=[AIMessage(content="好的", usage_metadata=usage)])


def tool_result(content: str = '{"status":"OK","items":[]}') -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="call-x")


# ======================================================================
# 工具调用
# ======================================================================


def test_tool_call_writes_stage_and_tool_span():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)

    result = reporter.middleware.wrap_tool_call(
        tool_request("search_hotels", {"city": "成都"}), lambda request: tool_result()
    )

    # 业务结果原样返回（上报只是个旁观者）
    assert result.content == '{"status":"OK","items":[]}'

    # run_stages：同一行先 RUNNING 后 SUCCESS，stage_id / message 都是中文展示名
    assert [row["stage_id"] for row in store.stages] == ["在查酒店", "在查酒店"]
    assert store.stages[0]["status"] == "RUNNING"
    finished = store.stages[-1]
    assert finished["status"] == "SUCCESS"
    assert finished["message"] == "在查酒店"
    assert finished["run_id"] == RUN_ID

    facts = finished["facts"]
    assert facts["tool"] == "search_hotels"
    assert facts["stage_key"] == "tool:search_hotels"
    assert facts["seq"] == 1
    assert isinstance(facts["duration_ms"], int) and facts["duration_ms"] >= 0
    assert facts["started"] and facts["finished"]
    assert facts["error"] is None
    # facts 必须能原样 JSON 序列化（store 会写进 TEXT 列）
    assert isinstance(facts["query"], str)

    # trace_spans：component=tool，同一个 span_id 的结束态覆盖开始态
    latest = store.span(store.spans_of("tool")[-1]["span_id"])
    assert latest is not None
    assert latest["component"] == "tool"
    assert latest["name"] == "search_hotels"
    assert latest["status"] == "SUCCESS"
    assert latest["started_at"] and latest["finished_at"]
    assert latest["attributes"]["tool"] == "search_hotels"
    assert latest["attributes"]["duration_ms"] >= 0
    assert "search_hotels" in latest["span_id"]

    # 前端轮询的那个字段
    assert store.current_stage == "在查酒店"


def test_repeated_tool_call_keeps_per_call_identity():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)
    middleware = reporter.middleware

    middleware.wrap_tool_call(tool_request("search_trains"), lambda request: tool_result())
    middleware.wrap_tool_call(tool_request("search_trains"), lambda request: tool_result())

    finishes = [row for row in store.stages if row["status"] == "SUCCESS"]
    assert [row["facts"]["stage_key"] for row in finishes] == [
        "tool:search_trains",
        "tool:search_trains:2",
    ]
    # 逐次调用的事实必须落在两个不同的 span 上（span_id 是主键，不带序号会互相顶掉）
    assert len({row["span_id"] for row in store.spans_of("tool")}) == 2
    assert [row["facts"]["seq"] for row in finishes] == [1, 2]


def test_tool_failure_reports_failed_and_reraises():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)

    def boom(request: ToolCallRequest) -> ToolMessage:
        raise TimeoutError("12306 没在超时前返回")

    with pytest.raises(TimeoutError):
        reporter.middleware.wrap_tool_call(tool_request("search_trains"), boom)

    facts = store.stages[-1]["facts"]
    assert store.stages[-1]["status"] == "FAILED"
    assert facts["status"] == "FAILED"
    assert "12306" in facts["error"]
    span = store.spans_of("tool")[-1]
    assert span["status"] == "FAILED"
    assert "TimeoutError" in span["error"]


# ======================================================================
# 模型调用
# ======================================================================


def test_model_call_records_tokens_and_accumulates():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)
    middleware = reporter.middleware

    middleware.wrap_model_call(model_request("gpt-test"), lambda request: model_response(100, 40, 20))
    middleware.wrap_model_call(model_request("gpt-test"), lambda request: model_response(50, 10, 5))

    spans = store.spans_of("llm")
    assert len(spans) == 2
    assert all(span["component"] == "llm" for span in spans)
    # 键名与 app/admin_timeline.py 读的一致
    first, second = spans[0]["attributes"], spans[1]["attributes"]
    assert (first["input_tokens"], first["output_tokens"], first["cached_tokens"]) == (100, 40, 20)
    assert first["total_tokens"] == 140  # 事件里没有 total_tokens：自己按 input + output 记
    assert first["model"] == "gpt-test" and first["tag"] == "gpt-test"
    assert first["status"] == "SUCCESS" and first["duration_ms"] >= 0

    # 第二次调用带上累计值，admin 不必自己加
    assert second["total_tokens"] == 60
    assert second["cumulative_total_tokens"] == 200
    assert second["cumulative_input_tokens"] == 150
    assert second["cumulative_cached_tokens"] == 25

    totals = reporter.token_totals()
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 50
    assert totals["cached_tokens"] == 25
    assert totals["total_tokens"] == 200
    assert totals["llm_calls"] == 2
    # 模型调用的 steps 也落 run_stages，供轮询
    assert store.current_stage == MODEL_STEP_LABEL


def test_current_stage_is_model_label_while_model_thinks():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)
    seen: list[str | None] = []

    def handler(request: ModelRequest) -> ModelResponse:
        # handler 就是"模型正在想"的那一刻：此时 current_stage 已经推上去了
        seen.append(store.current_stage)
        return model_response(10, 2)

    reporter.middleware.wrap_model_call(model_request(), handler)

    assert seen == [MODEL_STEP_LABEL]
    assert store.stages[0]["status"] == "RUNNING"
    assert store.stages[0]["stage_id"] == MODEL_STEP_LABEL


def test_model_failure_reports_failed_and_reraises():
    store = RecordingStore()
    reporter = build_step_reporter(store, RUN_ID)

    def boom(request: ModelRequest) -> ModelResponse:
        raise RuntimeError("模型 429")

    with pytest.raises(RuntimeError):
        reporter.middleware.wrap_model_call(model_request(), boom)

    assert store.stages[-1]["status"] == "FAILED"
    assert "429" in store.stages[-1]["facts"]["error"]
    span = store.spans_of("llm")[-1]
    assert span["status"] == "FAILED" and "RuntimeError" in span["error"]


# ======================================================================
# 观测失败不能影响主流程
# ======================================================================


def test_store_failures_are_swallowed():
    store = ExplodingStore()
    reporter = build_step_reporter(store, RUN_ID)

    result = reporter.middleware.wrap_tool_call(tool_request("search_hotels"), lambda r: tool_result())
    assert result.content == '{"status":"OK","items":[]}'

    async_check = reporter.middleware.wrap_model_call(
        model_request(), lambda request: model_response(1, 1)
    )
    assert async_check.result  # 响应原样返回

    # 直接调用上报器也不能抛
    reporter.begin_tool("search_flights", {"origin": "北京"})
    reporter.finish_tool(None)  # step 为 None 时静默跳过
    reporter.begin_model("gpt-test")
    # 落库全挂不影响计数：token 累加是内存里的事（上面那次 model 调用记了 1 + 1）
    assert reporter.token_totals()["total_tokens"] == 2


def test_missing_store_or_run_id_is_a_no_op():
    reporter = build_step_reporter(None, RUN_ID)
    reporter.middleware.wrap_tool_call(tool_request("search_hotels"), lambda r: tool_result())
    assert reporter.token_totals()["tool_calls"] == 1  # 计数仍然推进

    store = RecordingStore()
    empty = build_step_reporter(store, "")
    empty.middleware.wrap_tool_call(tool_request("search_hotels"), lambda r: tool_result())
    assert store.stages == [] and store.spans == []


def test_run_id_can_be_resolved_lazily():
    store = RecordingStore()
    current = {"run_id": "tp-a"}
    reporter = build_step_reporter(store, lambda: current["run_id"])
    middleware = reporter.middleware

    middleware.wrap_tool_call(tool_request("search_hotels"), lambda r: tool_result())
    assert store.stages[-1]["run_id"] == "tp-a"

    current["run_id"] = "tp-b"
    middleware.wrap_tool_call(tool_request("search_hotels"), lambda r: tool_result())
    assert store.stages[-1]["run_id"] == "tp-b"


# ======================================================================
# EventBus 订阅路径（备选接入方式）
# ======================================================================


def test_event_bus_subscription_path():
    from superharness.observability import EventBus, EventType

    store = RecordingStore()
    bus = EventBus()
    reporter = build_step_reporter(store, RUN_ID, bus=bus)

    bus.emit(
        EventType.TOOL_STARTED,
        "tool",
        span_id="s-tool-1",
        data={"name": "search_flights", "args": {"origin": "北京"}},
    )
    bus.emit(
        EventType.TOOL_FINISHED,
        "tool",
        span_id="s-tool-1",
        data={"name": "search_flights", "output": "[]", "duration_ms": 1234},
    )
    bus.emit(
        EventType.TOOL_ERROR,
        "tool",
        span_id="s-tool-2",
        data={"name": "search_trains", "type": "TimeoutError", "error": "超时", "duration_ms": 5000},
    )
    bus.emit(
        EventType.LLM_FINISHED,
        "llm",
        span_id="s-llm-1",
        data={"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2, "duration_ms": 800},
    )

    # 事件里的 span_id 原样落到 trace_spans，admin 才能和原生 trace 对上
    ok = store.span("s-tool-1")
    assert ok is not None and ok["status"] == "SUCCESS" and ok["name"] == "search_flights"
    assert ok["attributes"]["duration_ms"] == 1234

    failed = store.span("s-tool-2")
    assert failed is not None and failed["status"] == "FAILED" and "TimeoutError" in failed["error"]

    llm = store.span("s-llm-1")
    assert llm is not None and llm["component"] == "llm"
    assert llm["attributes"]["total_tokens"] == 15
    assert llm["attributes"]["duration_ms"] == 800
    assert reporter.token_totals()["cached_tokens"] == 2
    assert store.current_stage == MODEL_STEP_LABEL

    finished_tools = [row for row in store.stages if row["stage_id"] == "在查机票"]
    assert finished_tools[-1]["status"] == "SUCCESS"
    assert finished_tools[-1]["facts"]["duration_ms"] == 1234


# ======================================================================
# 展示名
# ======================================================================


def test_display_names_reuse_travel_tools_mapping():
    from app.travel_tools import TOOL_DISPLAY_NAMES as TOOLS_MAP

    # 不重复定义两套：这里必须是同一份映射
    assert TOOL_DISPLAY_NAMES == TOOLS_MAP
    assert display_name_for("search_trains") == "在查火车票"
    assert display_name_for("search_hotels") == "在查酒店"

    # 带 Provider 前缀的工具名按后缀匹配（与 admin_timeline._stage_for_tool 同一套规则）
    assert display_name_for("tuniu_search_hotels") == "在查酒店"

    # 没有映射的工具如实显示，不猜一个像是有意义的文案
    assert display_name_for("some_unknown_tool") == "在调用 some_unknown_tool"
    assert display_name_for("") == MODEL_STEP_LABEL


# ======================================================================
# 真库：落库后轮询读得到什么
# ======================================================================


def test_real_store_roundtrip(tmp_path):
    store = TravelPlanStore(tmp_path / "nested" / "travelplan.db")
    store.create_run(RUN_ID)
    reporter = build_step_reporter(store, RUN_ID)
    middleware = reporter.middleware

    middleware.wrap_tool_call(
        tool_request("search_hotels", {"city": "成都"}), lambda r: tool_result('{"items":[1]}')
    )
    middleware.wrap_model_call(model_request("gpt-test"), lambda r: model_response(120, 30, 10))

    progress = store.get_run_progress(RUN_ID)
    assert progress is not None
    # 前端轮询：current_stage 就是当前步骤的中文展示名
    assert progress["current_stage"] == MODEL_STEP_LABEL
    assert progress["message"] == MODEL_STEP_LABEL

    stages = {row["stage_id"]: row for row in progress["stages"]}
    assert set(stages) == {"在查酒店", MODEL_STEP_LABEL}
    assert stages["在查酒店"]["status"] == "SUCCESS"
    assert stages["在查酒店"]["facts"]["tool"] == "search_hotels"
    assert isinstance(stages["在查酒店"]["facts"]["duration_ms"], int)

    spans = store.get_trace_spans(RUN_ID)
    tool_span = [row for row in spans if row["component"] == "tool"][-1]
    assert tool_span["status"] == "SUCCESS"
    assert tool_span["attributes"]["tool"] == "search_hotels"

    llm_span = [row for row in spans if row["component"] == "llm"][-1]
    assert llm_span["attributes"]["input_tokens"] == 120
    assert llm_span["attributes"]["cached_tokens"] == 10
    assert llm_span["attributes"]["total_tokens"] == 150
    assert llm_span["finished_at"]


# ======================================================================
# 装配验证：middleware=[reporter.middleware] 真的挂得上，钩子真的会被调用
# ======================================================================


class FakeToolModel(BaseChatModel):
    """最小可用的离线模型：不联网、支持 bind_tools、回报固定 usage。

    用它是为了在**不接真模型**的前提下跑一次完整的 Agent Loop，证明
    `middleware=[reporter.middleware]` 这条路是通的（而不是只在假请求上通过）。
    """

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = AIMessage(
            content="已经查好了",
            usage_metadata={
                "input_tokens": 30,
                "output_tokens": 8,
                "total_tokens": 38,
                "input_token_details": {"cache_read": 4},
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_middleware_is_wired_into_the_agent_loop(tmp_path):
    from superharness import HarnessContext
    from superharness.agent import create_harness_agent

    store = TravelPlanStore(tmp_path / "nested" / "wire.db")
    store.create_run(RUN_ID)
    reporter = build_step_reporter(store, RUN_ID)

    agent = create_harness_agent(
        system_prompt="你是旅行规划助手",
        middleware=[reporter.middleware],
        model=FakeToolModel(),
        tools=[],
    )
    agent.invoke(
        {"messages": [{"role": "user", "content": "帮我看看行程"}]},
        context=HarnessContext(user_id="u-test"),
    )

    # 真实 Agent Loop 里的一次模型调用被记下来了（token 自己按 input + output 算）
    spans = store.get_trace_spans(RUN_ID)
    llm_spans = [row for row in spans if row["component"] == "llm"]
    assert llm_spans, "Agent Loop 的模型调用没有被上报"
    assert llm_spans[-1]["attributes"]["input_tokens"] == 30
    assert llm_spans[-1]["attributes"]["total_tokens"] == 38
    assert llm_spans[-1]["attributes"]["cached_tokens"] == 4

    progress = store.get_run_progress(RUN_ID)
    assert progress["current_stage"] == MODEL_STEP_LABEL
    assert reporter.token_totals()["llm_calls"] >= 1
