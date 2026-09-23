"""app/llm.py 的单元测试（PRD §26 / §32）。

LLM 这一层的契约是「永不抛异常，只返回状态」：模型不可用、超时、返回的不是 JSON，
都是**状态**，不是异常。一次 run 不允许因为模型抽风就整份失败。

这里也锁住一个曾经真实发生的故障：模型侧卡住时，`invoke` 会一直等下去
（openai 客户端默认 10 分钟才超时），整条 run 吊死，前端 120s 早断了。
`DEFAULT_TIMEOUT_SECONDS` 当时只是被存下来、从未被使用。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.llm import (
    DEFAULT_TIMEOUT_SECONDS,
    STATUS_INVALID_RESPONSE,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    LLM,
    LLMResult,
)


@pytest.fixture(autouse=True, scope="session")
def no_orphan_sweep_on_startup():
    """LLM 单测不需要 app.api，也不能触发默认数据库初始化。"""
    yield


class _OkModel:
    """最简模型桩：`.invoke(messages)` 返回带 `.content` 的对象。"""

    def __init__(self, content: str = "模型的一段话") -> None:
        self.content = content
        self.seen: list[object] = []

    def invoke(self, messages):
        self.seen.append(messages)
        return type("_Response", (), {"content": self.content})()


class _HangingModel:
    """永远不返回，直到被显式放行。用来验证超时兜底。"""

    def __init__(self) -> None:
        self.release = threading.Event()

    def invoke(self, messages):
        self.release.wait(timeout=30)
        return type("_Response", (), {"content": "迟到的回答"})()


class _RaisingModel:
    def __init__(self, message: str = "Connection error") -> None:
        self.message = message

    def invoke(self, messages):
        raise RuntimeError(self.message)


def test_without_model_every_call_degrades() -> None:
    """没配模型不是异常，是一个降级状态。"""
    llm = LLM()
    assert llm.available is False

    result = llm.invoke("sys", "user", tag="t")
    assert result.status == STATUS_UNAVAILABLE
    assert not result.ok
    assert "MODEL_NAME" in result.error
    # 失败也要留痕：审计要能回答"模型到底调了几次、为什么没结果"。
    assert llm.calls == [result]


def test_ok_call_records_text_model_and_tag() -> None:
    llm = LLM(model=_OkModel("成都五天行程"))
    result = llm.invoke("sys", "user", tag="final_answer")

    assert result.status == STATUS_OK
    assert result.ok
    assert result.text == "成都五天行程"
    assert result.tag == "final_answer"
    assert result.duration_ms is not None
    assert llm.audit_entries()[0]["tag"] == "final_answer"


def test_content_blocks_are_flattened() -> None:
    """多模态/带 reasoning 的模型会把 content 返回成内容块列表，要摊平成字符串。"""
    llm = LLM(model=_OkModel([{"type": "text", "text": "前半"}, {"type": "text", "text": "后半"}]))
    assert llm.invoke("sys", "user", tag="t").text == "前半后半"


def test_hanging_model_times_out_instead_of_blocking_the_run() -> None:
    """模型卡住时必须按超时降级，而不是把整条 run 吊死。"""
    model = _HangingModel()
    llm = LLM(model=model)
    llm._timeout = 0.3  # 别让测试真的等 120 秒

    started = time.monotonic()
    result = llm.invoke("sys", "user", tag="extract_places")
    elapsed = time.monotonic() - started

    assert result.status == STATUS_TIMEOUT
    assert not result.ok
    assert "0.3" in result.error or "超过" in result.error
    assert elapsed < 5, "超时兜底没有生效，调用被阻塞了"
    # 超时那次不能同时在账本里留下一条"成功"。
    assert len(llm.calls) == 1
    assert llm.calls[0].status == STATUS_TIMEOUT

    model.release.set()


def test_invoke_json_still_returns_a_result_when_the_model_times_out() -> None:
    llm = LLM(model=_HangingModel())
    llm._timeout = 0.3
    result = llm.invoke_json("sys", "user", tag="critic")
    assert result.status == STATUS_TIMEOUT
    assert result.value is None


def test_model_exception_becomes_a_status_not_a_raise() -> None:
    llm = LLM(model=_RaisingModel("Connection error"))
    result = llm.invoke("sys", "user", tag="t")
    assert result.status == STATUS_UNAVAILABLE
    assert "Connection error" in result.error


def test_raised_timeout_is_classified_as_timeout() -> None:
    """模型/HTTP 层自己抛的超时要归到 TIMEOUT，而不是笼统的 UNAVAILABLE。"""
    llm = LLM(model=_RaisingModel("Request timed out"))
    assert llm.invoke("sys", "user", tag="t").status == STATUS_TIMEOUT


def test_invoke_json_parses_wrapped_json() -> None:
    """模型常把 JSON 包在 ```json 里或前后带解释，容错解析要能取出来。"""
    llm = LLM(model=_OkModel('这是结果：\n```json\n{"days": 5, "city": "成都"}\n```\n以上。'))
    result = llm.invoke_json("sys", "user", tag="parse_intent")
    assert result.status == STATUS_OK
    assert result.value == {"days": 5, "city": "成都"}


def test_unparsable_output_keeps_the_raw_text_for_audit() -> None:
    """解析失败也要保住原文 —— 审计需要看到"模型到底回了什么"。"""
    llm = LLM(model=_OkModel("抱歉，我无法回答。"))
    result = llm.invoke_json("sys", "user", tag="critic")

    assert result.status == STATUS_INVALID_RESPONSE
    assert result.value is None
    assert result.text == "抱歉，我无法回答。"
    assert "JSON" in result.error


def test_degraded_reason_is_human_readable_and_empty_on_success() -> None:
    llm = LLM()
    failed = llm.invoke("sys", "user", tag="parse_intent")
    assert "parse_intent" in failed.degraded_reason
    assert failed.degraded_reason

    ok = LLM(model=_OkModel()).invoke("sys", "user", tag="t")
    assert ok.degraded_reason == ""


def test_audit_entries_are_redacted_previews_not_raw_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计条目带 prompt/输出的**预览**：只放脱敏 + 截断后的文本，绝不放 Key。"""
    monkeypatch.setenv("MODEL_API_KEY", "sk-live-abcdef123456")
    llm = LLM(model=_OkModel("x" * 200))
    llm.invoke("system-prompt-with-secret sk-live-abcdef123456", "user", tag="t")

    entry = llm.audit_entries()[0]
    # started_at / finished_at 是为了让 Trace 能画出真实的先后（缺了它们，所有模型调用
    # 都会挤在 run 结尾）；token 与预览是为了回答"它收到了什么、回了什么、花了多少"。
    assert set(entry) == {
        "tag",
        "model",
        "status",
        "duration_ms",
        "error",
        "chars",
        "started_at",
        "finished_at",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
        "system_preview",
        "user_preview",
        "context_preview",
        "assistant_preview",
        "prompt_version",
        "prompt_hash",
    }
    assert entry["chars"] == 200
    # 进程里配置的 Key 值不允许出现在审计里（预览里也不行）。
    assert "sk-live-abcdef123456" not in str(entry)
    assert "***" in entry["system_preview"]
    assert entry["user_preview"] == "user"
    assert entry["assistant_preview"] == "x" * 200
    # 这次没传 context：字段留 None，而不是空串（空串会被读成"确实没有上下文"）。
    assert entry["context_preview"] is None


def test_preview_is_truncated_with_a_visible_tail_note() -> None:
    """预览有上限，且截断必须留痕 —— 否则读者会把半截输出当成完整的输出。"""
    import app.llm as llm_module

    llm = LLM(model=_OkModel("y" * (llm_module._PREVIEW_CHARS + 500)))
    entry = llm.invoke("sys", "user", tag="t").to_audit()

    assert entry["assistant_preview"].startswith("y" * 100)
    assert "已截断" in entry["assistant_preview"]
    assert str(llm_module._PREVIEW_CHARS + 500) in entry["assistant_preview"]
    assert entry["chars"] == llm_module._PREVIEW_CHARS + 500  # chars 仍是真实长度


def test_context_is_a_third_input_segment() -> None:
    """`context` 与"调用方自己拼进 user"语义等价，只是审计能把三段分开。"""
    model = _OkModel("好")
    llm = LLM(model=model)
    llm.invoke("SYS", "USER", tag="t", context="CTX")

    assert [message.content for message in model.seen[0]] == ["SYS", "USER\n\nCTX"]
    entry = llm.audit_entries()[0]
    assert entry["system_preview"] == "SYS"
    assert entry["user_preview"] == "USER"
    assert entry["context_preview"] == "CTX"

    # 不传 context 时消息列表与以前完全一致（老调用点不受影响）。
    plain = _OkModel("好")
    LLM(model=plain).invoke("SYS", "USER", tag="t")
    assert [message.content for message in plain.seen[0]] == ["SYS", "USER"]


def test_invoke_json_passes_context_through() -> None:
    """批量抽取这类调用走 invoke_json，context 必须原样透传，否则证据正文进不了审计。"""
    model = _OkModel('{"places": []}')
    llm = LLM(model=model)
    llm.invoke_json("SYS", "USER", tag="extract_places", context="CTX")

    assert [message.content for message in model.seen[0]] == ["SYS", "USER\n\nCTX"]
    assert llm.audit_entries()[0]["context_preview"] == "CTX"


def test_extract_places_batch_sends_evidence_body_as_context() -> None:
    """extract_places 的攻略正文必须走 context（而非拼进 user）——Trace 的 Context 行靠它取值。

    直接调 `discovery.extract_places_from_evidences`（生产里由 refresh_city_cache 触发）：
    审计里 user_preview 只是定位需求的一句话，context_preview 才是真的攻略正文。
    """
    from app.discovery import extract_places_from_evidences
    from app.models import Evidence

    class _RecorderModel:
        def __init__(self) -> None:
            self.seen: list[list[object]] = []

        def invoke(self, messages):
            self.seen.append(messages)
            return type("_Response", (), {"content": '{"results": [{"evidence_id": "e-1", "places": []}]}'})()

    evidence = Evidence(
        id="e-1", source_type="social", provider="tikhub",
        title="成都三日游", text="宽窄巷子值得去" * 30,
    )
    model = _RecorderModel()
    llm = LLM(model=model)
    items, degradations = extract_places_from_evidences(llm, [evidence])

    assert items == [] and degradations == []
    entry = llm.audit_entries()[0]
    assert entry["tag"].startswith("extract_places")
    # user 是固定指令，正文进 context —— 这样 Context 预览不再恒为 null。
    assert entry["context_preview"] is not None
    assert "### 证据 id：e-1" in entry["context_preview"]
    assert "宽窄巷子" in entry["context_preview"]
    assert "宽窄巷子" not in (entry["user_preview"] or "")


def test_audit_tokens_stay_empty_when_there_is_no_usage() -> None:
    """token 口径与 run_metrics 一致：没有数据就是 None，有数据就是 input+output。"""
    entry = LLM(model=_OkModel("好")).invoke("s", "u", tag="t").to_audit()
    assert (
        entry["input_tokens"],
        entry["output_tokens"],
        entry["cached_tokens"],
        entry["total_tokens"],
    ) == (None, None, None, None)

    # 空 dict 与 None 一样都是"取不到"：不能读成 0。
    assert LLMResult(usage={}).to_audit()["total_tokens"] is None

    with_cache = LLMResult(
        usage={"input_tokens": 10, "output_tokens": 5, "cached_tokens": 4}
    ).to_audit()
    assert (
        with_cache["input_tokens"],
        with_cache["output_tokens"],
        with_cache["cached_tokens"],
        with_cache["total_tokens"],
    ) == (10, 5, 4, 15)

    # Provider 没回 cached 字段时留白：写 0 会被读成"这次没有缓存命中"。
    assert LLMResult(usage={"input_tokens": 10, "output_tokens": 5}).to_audit()["cached_tokens"] is None


def test_prompt_hash_tells_whether_two_calls_got_identical_input() -> None:
    llm = LLM(model=_OkModel("好"))
    llm.invoke("SYS", "USER", tag="a")
    llm.invoke("SYS", "USER", tag="b")
    llm.invoke("SYS", "USER", tag="c", context="CTX")

    same_a, same_b, other = (call.to_audit() for call in llm.calls)
    assert same_a["prompt_hash"] == same_b["prompt_hash"]
    # 多带一段 context 就是另一份输入，指纹必须不同。
    assert same_a["prompt_hash"] != other["prompt_hash"]
    # 输入原文没记下来时（旧账本 / 直接构造的结果）算不出指纹，就给 None。
    assert LLMResult().to_audit()["prompt_hash"] is None


def test_prompt_version_is_the_code_version_or_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """prompt_version 标的是**代码**版本（prompt 跟着代码发版），取不到就留空。"""
    monkeypatch.setenv("TRAVELPLAN_COMMIT", "deadbee")
    assert LLMResult().to_audit()["prompt_version"] == "deadbee"

    import app.version as version_module

    monkeypatch.setattr(version_module, "travelplan_commit", lambda: None)
    assert LLMResult().to_audit()["prompt_version"] is None


def test_default_timeout_is_generous_but_bounded() -> None:
    """默认超时要比 Provider 的 60s 宽松（模型侧排队更常见），但必须有限。"""
    assert 30 <= DEFAULT_TIMEOUT_SECONDS <= 300
    assert LLM()._timeout == DEFAULT_TIMEOUT_SECONDS


def test_timeout_can_be_overridden_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同模型快慢差好几倍，超时得能改；改错了退回默认值，不能让 run 起不来。"""
    import app.llm as llm_module

    monkeypatch.setenv(llm_module.TIMEOUT_ENV_VAR, "300")
    assert llm_module._timeout_from_env() == 300.0

    monkeypatch.setenv(llm_module.TIMEOUT_ENV_VAR, "abc")
    assert llm_module._timeout_from_env() == DEFAULT_TIMEOUT_SECONDS

    monkeypatch.setenv(llm_module.TIMEOUT_ENV_VAR, "-5")
    assert llm_module._timeout_from_env() == DEFAULT_TIMEOUT_SECONDS

    monkeypatch.delenv(llm_module.TIMEOUT_ENV_VAR)
    assert llm_module._timeout_from_env() == DEFAULT_TIMEOUT_SECONDS


def test_from_env_applies_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`from_env` 是真正构造句柄的路径，环境变量必须在那里生效。"""
    import app.llm as llm_module

    monkeypatch.setenv(llm_module.TIMEOUT_ENV_VAR, "240")
    llm = llm_module.LLM.from_env(model=object())
    assert llm._timeout == 240.0


def test_llm_calls_are_reported_to_superharness_observability() -> None:
    """docs/operations/DEVELOPMENT.md §9：Router / LLM / Tool / token 由 SuperHarness 原生 Observability 负责。

    所以模型调用要上报成 llm.started / llm.finished，而不是自己再打一份日志。
    token 用量必须是模型回报的真实值——取不到就留白，不能填 0 冒充。
    """
    events: list[tuple[str, str, str | None, dict]] = []

    def collect(event_type, component, *, data=None, status=None):
        events.append((event_type, component, status, data or {}))

    llm = LLM(model=_OkModel("好"), emit=collect)
    llm.invoke("sys", "user", tag="parse_intent")

    assert [e[0] for e in events] == ["llm.started", "llm.finished"]
    assert all(e[1] == "llm" for e in events)
    assert events[1][2] == "OK"
    assert events[1][3]["tag"] == "parse_intent"


def test_llm_reports_token_usage_when_the_model_provides_it() -> None:
    """有真实用量就带上，没有就不带——两种都要能被上层区分。"""

    class _UsageModel:
        model_name = "usage-model"

        def invoke(self, messages):
            from types import SimpleNamespace

            return SimpleNamespace(
                content="好",
                usage_metadata={"input_tokens": 12, "output_tokens": 34},
            )

    llm = LLM(model=_UsageModel())
    result = llm.invoke("sys", "user", tag="t")

    assert result.usage == {"input_tokens": 12, "output_tokens": 34}
    assert llm.invoke("sys", "user", tag="t").usage is not None

    plain = LLM(model=_OkModel("好")).invoke("sys", "user", tag="t")
    assert plain.usage is None


def test_emit_failure_does_not_break_the_call() -> None:
    """观测是旁路：上报口抛异常时模型调用的结果必须照常拿到。"""

    def exploding(event_type, component, *, data=None, status=None):
        raise RuntimeError("event bus 挂了")

    llm = LLM(model=_OkModel("好"), emit=exploding)

    assert llm.invoke("sys", "user", tag="t").status == STATUS_OK


def test_from_env_falls_back_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺配置时 `create_model()` 会抛 ValueError；那应该是一次可降级的 run，不是崩进程。"""
    import app.llm as llm_module

    def exploding_create_model():
        raise ValueError("请在根目录 .env 配置 MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY")

    monkeypatch.setattr("superharness.create_model", exploding_create_model)
    handle = llm_module.LLM.from_env()
    assert handle.available is False
    assert handle.invoke("s", "u", tag="t").status == STATUS_UNAVAILABLE


def test_llm_result_ok_is_derived_from_status() -> None:
    assert LLMResult(status=STATUS_OK).ok is True
    for status in (STATUS_UNAVAILABLE, STATUS_TIMEOUT, STATUS_INVALID_RESPONSE):
        assert LLMResult(status=status).ok is False


class _NativeModel:
    model_name = "native-test"

    def __init__(self, response):
        self.response = response
        self.bindings = []
        self.messages = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_native_tools_preserve_ai_message_and_entire_dialogue(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    secret = "sk-test-native-secret-123456"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    response = AIMessage(content="", tool_calls=[{"name": "read_guides", "args": {"query": secret}, "id": "call-2"}],
                         usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17})
    model = _NativeModel(response)
    events = []
    llm = LLM(model=model, emit=lambda *args, **kw: events.append((args, kw)))
    previous = AIMessage(content="", tool_calls=[{"name": "read_guides", "args": {}, "id": "call-1"}])
    messages = [SystemMessage(content="sys"), HumanMessage(content="user"), previous,
                ToolMessage(content=secret, tool_call_id="call-1")]
    tools = [{"type": "function", "function": {"name": "read_guides", "parameters": {"type": "object", "properties": {}}}}]
    result = llm.invoke_tools(messages, tools, tag="native", tool_choice="read_guides")
    assert result.ok and result.value is response
    assert result.value.tool_calls[0]["id"] == "call-2"
    assert model.messages[0] == messages
    assert model.bindings == [(tools, {"tool_choice": "read_guides"})]
    assert llm.calls == [result]
    audit = result.to_audit()
    assert audit["total_tokens"] == 17
    assert audit["started_at"] and audit["finished_at"]
    assert "read_guides" in audit["assistant_preview"]
    assert "call-1" in audit["user_preview"]
    assert secret not in str(audit) and secret not in str(events)
    assert [event[0][0] for event in events] == ["llm.started", "llm.finished"]


@pytest.mark.parametrize("error,status", [(RuntimeError("bad schema"), STATUS_UNAVAILABLE),
                                         (TimeoutError("Request timed out"), STATUS_TIMEOUT)])
def test_native_tool_errors_are_statuses_and_redacted(error, status, monkeypatch):
    from langchain_core.messages import HumanMessage

    secret = "native-secret-123456"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    error.args = (str(error) + " " + secret,)
    llm = LLM(model=_NativeModel(error))
    result = llm.invoke_tools([HumanMessage(content="u")], [], tag="native")
    assert result.status == status and result.value is None
    assert secret not in str(result.to_audit()) and secret not in result.error
    assert len(llm.calls) == 1


def test_native_tools_unavailable_invalid_and_no_json_disguise():
    from langchain_core.messages import AIMessage

    assert LLM().invoke_tools([], [], tag="none").status == STATUS_UNAVAILABLE
    assert LLM(model=_NativeModel({"tool_calls": []})).invoke_tools([], [], tag="bad").status == STATUS_INVALID_RESPONSE
    malformed = AIMessage(content="", invalid_tool_calls=[{"name": "read_guides", "args": "{broken", "id": "bad", "error": "parse"}])
    result = LLM(model=_NativeModel(malformed)).invoke_tools([], [], tag="bad")
    assert result.status == STATUS_INVALID_RESPONSE and result.value is None
    text = AIMessage(content='{"tool_calls": [{"name": "read_guides"}]}')
    result = LLM(model=_NativeModel(text)).invoke_tools([], [], tag="text")
    assert result.value is text and result.value.tool_calls == []


def test_native_tools_budget_includes_binding_and_late_completion_is_not_audited():
    from langchain_core.messages import AIMessage

    release = threading.Event()
    finished = threading.Event()

    class SlowBind(_NativeModel):
        def bind_tools(self, tools, **kwargs):
            release.wait(5)
            return self

        def invoke(self, messages):
            finished.set()
            return self.response

    llm = LLM(model=SlowBind(AIMessage(content="late")), timeout=0.03)
    try:
        result = llm.invoke_tools([], [], tag="native")
        assert result.status == STATUS_TIMEOUT
    finally:
        release.set()
    assert finished.wait(2)
    assert llm.calls == [result]
    assert result.status == STATUS_TIMEOUT


def test_native_tools_and_plain_calls_share_concurrency_gate():
    from concurrent.futures import ThreadPoolExecutor
    from langchain_core.messages import AIMessage

    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()

    class Blocking(_NativeModel):
        def invoke(self, messages):
            if not entered.is_set():
                entered.set()
                release.wait(5)
            else:
                second.set()
            return self.response

    llm = LLM(model=Blocking(AIMessage(content="ok")), max_concurrency=1, timeout=3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(llm.invoke_tools, [], [], tag="native")
        assert entered.wait(2)
        other = pool.submit(llm.invoke, "sys", "user", tag="plain")
        try:
            assert not second.wait(0.05), "原生工具调用绕过了共享并发闸门"
        finally:
            release.set()
        assert first.result().ok and other.result().ok
    assert second.is_set() and len(llm.calls) == 2
