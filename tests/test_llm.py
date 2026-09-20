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


def test_audit_entries_never_contain_prompts_or_secrets() -> None:
    """审计条目只记 tag/model/status/耗时/字数，不含 prompt 全文与任何 Key。"""
    llm = LLM(model=_OkModel("x" * 200))
    llm.invoke("system-prompt-with-secret-skb-123", "user", tag="t")

    entry = llm.audit_entries()[0]
    assert set(entry) == {"tag", "model", "status", "duration_ms", "error", "chars"}
    assert entry["chars"] == 200
    assert "skb-123" not in str(entry)


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
    """START.md §9：Router / LLM / Tool / token 由 SuperHarness 原生 Observability 负责。

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
