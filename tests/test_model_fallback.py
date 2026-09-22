"""主模型 + 备用模型链（`.env` 的 `MODEL_*` / `MODEL_*_bk1` / `MODEL_*_bk2`）。

断言四件事：

1. 三套变量齐全 → 链长 3，顺序就是 主 → bk1 → bk2；
2. 只配主模型 → 链长 1，不报错、不装 middleware；
3. 某套备用只配一半 → 整套干净跳过（不塞空 key，也不把半成品放进链里）；
4. 链装到 middleware 上以后，语义是「抛错即降级」且顺序与链一致。

全部离线：只 monkeypatch 环境变量 + 假模型名/假 base_url。构造 ChatOpenAI 不发任何请求。
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_openai import ChatOpenAI

from app.agent import build_model_with_fallbacks
from app.config import model_endpoints

#: 三套模型变量名，与 `superharness.create_model` / `app.config.model_endpoints` 对齐。
_FIELDS = ("MODEL_NAME", "MODEL_BASE_URL", "MODEL_API_KEY")
_SUFFIXES = ("", "_bk1", "_bk2")


@pytest.fixture(autouse=True)
def clean_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """先把环境里所有模型变量摘干净。

    为什么必须显式摘：conftest 只摘 Secret（`MODEL_API_KEY`），而开发机根目录 `.env` 里的
    `MODEL_NAME_bk1` / `MODEL_BASE_URL_bk1` 那几项不是 Secret，会被 superharness 的
    `load_dotenv()` 带进环境。不摘的话「只配主模型」这条用例会因为 `.env` 里真配了备用而失败
    —— 测试结论随开发机 `.env` 变化，不可重复。monkeypatch 会在用例结束后原样还原。
    """

    for suffix in _SUFFIXES:
        for field in _FIELDS:
            monkeypatch.delenv(f"{field}{suffix}", raising=False)
    yield


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    model_name: str,
    *,
    with_api_key: bool = True,
) -> None:
    """给一套模型写假变量。`with_api_key=False` 用于构造"只配一半"的备用模型。"""

    monkeypatch.setenv(f"MODEL_NAME{suffix}", model_name)
    monkeypatch.setenv(f"MODEL_BASE_URL{suffix}", f"https://{model_name}.test/v1")
    if with_api_key:
        monkeypatch.setenv(f"MODEL_API_KEY{suffix}", f"sk-{model_name}")


def _chain(model, middleware) -> list[str]:
    """把 `(主模型, [middleware])` 摊平成模型名列表，用来断言降级顺序。"""

    names = [model.model_name]
    for item in middleware:
        names.extend(backup.model_name for backup in item.models)
    return names


def test_three_configured_models_build_a_three_step_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, "", "main-model")
    _configure(monkeypatch, "_bk1", "bk1-model")
    _configure(monkeypatch, "_bk2", "bk2-model")

    model, middleware = build_model_with_fallbacks()

    assert isinstance(model, ChatOpenAI)
    assert len(middleware) == 1
    assert isinstance(middleware[0], ModelFallbackMiddleware)
    assert _chain(model, middleware) == ["main-model", "bk1-model", "bk2-model"]


def test_primary_only_builds_a_chain_of_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """只配主模型是合法配置：链长 1，且不装任何 middleware（行为与改造前完全一致）。"""

    _configure(monkeypatch, "", "main-model")

    model, middleware = build_model_with_fallbacks()

    assert _chain(model, middleware) == ["main-model"]
    assert middleware == []


@pytest.mark.parametrize("missing_field", _FIELDS)
@pytest.mark.parametrize("blank", [False, True], ids=["unset", "blank"])
def test_half_configured_backup_is_skipped_cleanly(
    monkeypatch: pytest.MonkeyPatch, missing_field: str, blank: bool
) -> None:
    """备用模型任一项缺失/空白 → 整套跳过（不报错、不塞空 key）。

    为什么半套必须整套跳过：有 name + base_url 但没有 key 的模型一定在第一次调用时 401，
    留着它只会让"主模型失败"变成"整条链都失败"，降级就白做了。
    """

    _configure(monkeypatch, "", "main-model")
    _configure(monkeypatch, "_bk1", "bk1-model")
    _configure(monkeypatch, "_bk2", "bk2-model")
    if blank:
        monkeypatch.setenv(f"{missing_field}_bk1", "   ")
    else:
        monkeypatch.delenv(f"{missing_field}_bk1", raising=False)

    model, middleware = build_model_with_fallbacks()

    assert _chain(model, middleware) == ["main-model", "bk2-model"]


def test_missing_primary_promotes_the_first_complete_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主模型没配全、但备用配全了 → 备用顶上（"主不可用"包含"主没配"）。"""

    _configure(monkeypatch, "", "main-model", with_api_key=False)
    _configure(monkeypatch, "_bk1", "bk1-model")
    _configure(monkeypatch, "_bk2", "bk2-model")

    model, middleware = build_model_with_fallbacks()

    assert _chain(model, middleware) == ["bk1-model", "bk2-model"]


def test_no_model_configured_returns_nothing_for_upstream_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一套都没配全 → `(None, [])`，不在这里抛错：错误文案由上游 create_model 统一给出。"""

    assert build_model_with_fallbacks() == (None, [])
    assert model_endpoints() == ()


def test_endpoint_repr_never_leaks_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点会出现在日志/异常里，repr 必须遮掉 key。"""

    _configure(monkeypatch, "", "main-model")

    assert "sk-main-model" not in repr(model_endpoints()[0])


class _FakeRequest:
    """只搭出 `ModelFallbackMiddleware` 真正用到的那几个字段。

    middleware 会做两件事：读 `model_settings / system_message / messages / tools` 去掉
    Anthropic cache 标记，然后用 `override(model=...)` 换模型。所以假 request 只要有这些
    属性 + 一个会替换 model 的 override 就够了，不需要真起 Agent。
    """

    model_settings: ClassVar[dict] = {}
    system_message: ClassVar[None] = None
    messages: ClassVar[list] = []
    tools: ClassVar[list] = []

    def __init__(self, model) -> None:
        self.model = model

    def override(self, **kwargs):
        request = _FakeRequest(self.model)
        request.__dict__.update({**self.__dict__, **kwargs})
        return request


def test_middleware_falls_over_on_error_in_chain_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """抛错即降级：主模型抛错后按 bk1 → bk2 依次顶上，不是同一个模型重试。"""

    _configure(monkeypatch, "", "main-model")
    _configure(monkeypatch, "_bk1", "bk1-model")
    _configure(monkeypatch, "_bk2", "bk2-model")

    model, middleware = build_model_with_fallbacks()
    (fallback,) = middleware

    tried: list[str] = []

    def handler(request):
        name = request.model.model_name
        tried.append(name)
        if name != "bk2-model":
            raise RuntimeError(f"{name} 不可用")
        return "OK-from-bk2"

    assert fallback.wrap_model_call(_FakeRequest(model), handler) == "OK-from-bk2"
    assert tried == ["main-model", "bk1-model", "bk2-model"]


def test_create_travel_app_hands_the_chain_to_superharness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配层接线：链是通过 SuperHarness 既有的 `middleware=` 注入口交进去的。"""

    _configure(monkeypatch, "", "main-model")
    _configure(monkeypatch, "_bk1", "bk1-model")
    _configure(monkeypatch, "_bk2", "bk2-model")

    captured: dict = {}

    def fake_create_harness_app(**kwargs):
        captured.update(kwargs)
        return "STUBBED-RUNNER"

    monkeypatch.setattr("app.agent.create_harness_app", fake_create_harness_app)

    from app.agent import create_travel_app

    assert create_travel_app(output_dir="out", checkpointer=None) == "STUBBED-RUNNER"

    assert captured["model"].model_name == "main-model"
    assert _chain(captured["model"], captured["middleware"]) == [
        "main-model",
        "bk1-model",
        "bk2-model",
    ]
