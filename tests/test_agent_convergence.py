"""Agent Loop 收敛控制的 P0 子集：submit 即停 / 调用次数护栏 / LLM timeout / digest 新字段。

这些用例跑的是**真的 Agent Loop**（`create_harness_agent` + LangChain 工具循环），
模型与 Hub 都是替身（离线、不联网、不写开发库），store 落在 tmp_path —— 与
`tests/test_agent_runner.py` 同一套搭建方式，只验证本轮的收敛控制：

1. `submit_final_plan` 通过校验后立即 END（交卷后不再有多余的模型/工具调用）；
2. 校验失败的提交不打断循环，Agent 还能再修一轮；
3. `max_llm_calls` / `max_tool_calls` 真正生效（默认 None = 不启用）；
4. `_prefetch_digest` 的 place 行带 lat/lng/address/opening_hours/district（有值才加）；
5. `_chat_model` / `build_model_with_fallbacks` 把 `llm_timeout_seconds` 传进 ChatOpenAI。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agent_runner import SUBMIT_TOOL_NAME, _prefetch_digest, execute_agent_run
from app.store import TravelPlanStore

RUN_ID = "tp-agent-convergence"
#: 规划流程的 user prompt 里有这一句（见 agent_runner._user_prompt）。模型用它区分
#: "这是主规划的调用"与"这是别的模型调用"（例如长期记忆 consolidation）。
PLANNER_MARK = "请开始：先补齐缺的关键事实"
QUERY = "10月1日从北京去成都玩3天，两个人，预算6000，喜欢美食和拍照"


@pytest.fixture(autouse=True)
def isolated_harness_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块的 Agent 装配不写长期记忆、不打 Console Trace。

    与 test_agent_runner 同源：`MemoryMiddleware` 会写 `settings.memory_db_url`（开发库），
    更麻烦的是攒够 rollout 后会**再调一次模型**做 consolidation —— 那一次会打乱脚本模型
    的动作序列。这里替换 `superharness.agent` 模块命名空间里的 `settings`，不动
    `superharness.config`，其它模块不受影响。
    """

    from superharness import agent as harness_agent
    from superharness.config import settings
    from superharness.middleware import ObservabilityMiddleware

    patched = dataclasses.replace(
        settings,
        memory_enabled=False,
        retrieval_enabled=False,
        log_enabled=False,
        trace_enabled=False,
    )
    monkeypatch.setattr(harness_agent, "settings", patched)
    monkeypatch.setattr(ObservabilityMiddleware.__init__, "__defaults__", (patched,))


# ======================================================================
# 替身：store / hub / 模型（与 test_agent_runner 同款，只留本模块用得到的）
# ======================================================================


class RecordingStore(TravelPlanStore):
    """真 store + 记账：`save_plan` / `finish_run` 到底有没有被调用，不能靠猜。"""

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path=db_path)
        self.calls: list[tuple[str, Any]] = []

    def save_plan(self, plan: Any, plan_md: str = "") -> None:  # type: ignore[override]
        self.calls.append(("save_plan", plan))
        super().save_plan(plan, plan_md)

    def finish_run(self, run_id: str, status: str = "completed", *, error: str | None = None) -> None:
        self.calls.append(("finish_run", status, error))
        super().finish_run(run_id, status, error=error)

    def statuses(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "finish_run"]


class FakeHub:
    """只实现会被调到的取数方法，返回真实形状的 `ProviderResult`（离线、无网络）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_trains(self, origin: str, destination: str, depart_date: str, **_: Any) -> Any:
        self.calls.append("search_trains")
        return _result([{"train_no": "G89", "price_range": {"min": 780}}])

    def search_hotels(self, city: str, check_in: str, check_out: str, **_: Any) -> Any:
        self.calls.append("search_hotels")
        return _result([{"name": "春熙路某酒店", "price_per_night": 420}])

    def search_poi(self, keywords: str, region: str, **_: Any) -> Any:
        self.calls.append("search_poi")
        return _result([{"place_id": "B0FF1", "name": "宽窄巷子", "amap_verified": True}])

    def web_search(self, query: str, **_: Any) -> Any:
        self.calls.append("web_search")
        return _result([{"title": "官网", "url": "https://example.com"}])

    def audit_entries(self) -> list[dict[str, Any]]:
        return []


def _result(items: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(status="OK", provider="fake", items=items, error=None)


def _is_planner_call(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage) and PLANNER_MARK in str(message.content)
        for message in messages
    )


class ScriptedPlannerModel(BaseChatModel):
    """按剧本逐轮出招的离线模型：每一步要么调工具，要么直接回复。

    `turns` 只统计「主规划调用」的次数（非规划调用如记忆 consolidation 不推进脚本），
    因此用例能精确断言「交卷后没有再发生模型调用」。
    """

    script: list[dict[str, Any]] = Field(default_factory=list)
    turns: int = 0
    #: 每轮回报的 token 用量（input / output / cached）
    usage: tuple[int, int, int] = (30, 10, 4)

    @property
    def _llm_type(self) -> str:
        return "scripted-planner"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedPlannerModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not _is_planner_call(messages):
            return _chat(AIMessage(content="（非规划流程调用，跳过）"))

        step = self.script[min(self.turns, len(self.script) - 1)]
        self.turns += 1
        usage = {
            "input_tokens": self.usage[0],
            "output_tokens": self.usage[1],
            "total_tokens": self.usage[0] + self.usage[1],
            "input_token_details": {"cache_read": self.usage[2]},
        }
        name = step.get("tool")
        if name:
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": step.get("args") or {}, "id": f"call-{self.turns}"}
                ],
                usage_metadata=usage,
            )
        else:
            message = AIMessage(content=step.get("text") or "", usage_metadata=usage)
        return _chat(message)


def _chat(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


# ======================================================================
# 剧本片段
# ======================================================================


def _submit_args(*, days: int = 3) -> dict[str, Any]:
    """一份最小但完整的交卷参数（时刻/价格都是"工具查到"的口径）。"""

    items: list[dict[str, Any]] = [
        {
            "name": "宽窄巷子",
            "type": "attraction",
            "place_id": "B0FF1",
            "start_time": "09:00",
            "end_time": "11:00",
            "price": 0,
            "price_type": "realtime",
            "source_ids": ["src-0"],
            "reason": "高德核实的真实地点",
        }
    ]
    return {
        "intent": {
            "destination": ["成都"],
            "origin": "北京",
            "start_date": "2026-10-01",
            "days": days,
            "travelers": 2,
            "budget_total": 6000,
            "preferences": ["美食", "拍照"],
        },
        "transport": {
            "kind": "train",
            "no": "G89",
            "provider": "fake",
            "departure_at": "2026-10-01 07:00",
            "arrival_at": "2026-10-01 12:30",
            "duration_minutes": 330,
            "price": 780,
        },
        "inbound_transport": {
            "kind": "train",
            "no": "G90",
            "provider": "fake",
            "departure_at": "2026-10-03 18:00",
            "arrival_at": "2026-10-03 23:30",
            "duration_minutes": 330,
            "price": 780,
        },
        "hotel": {"name": "春熙路某酒店", "provider": "fake", "price_per_night": 420},
        "days": [
            {"day_index": index, "date": f"2026-10-0{index + 1}", "items": items}
            for index in range(days)
        ],
        "budget": {
            "known_real_cost": 2400,
            "estimated_cost": 600,
            "projected_total": 3000,
            "breakdown": {"交通": 1560, "住宿": 840},
        },
        "sources": [{"source_id": "src-0", "provider": "fake", "title": "G89 车次"}],
    }


def _make_env(tmp_path: Path) -> tuple[RecordingStore, FakeHub, Path]:
    store = RecordingStore(tmp_path / "travelplan.db")
    store.init_schema()
    return store, FakeHub(), tmp_path / "outputs"


def _run(
    store: RecordingStore,
    hub: FakeHub,
    output_dir: Path,
    model: ScriptedPlannerModel,
    **kwargs: Any,
):
    return execute_agent_run(
        QUERY,
        user_id="u-agent",
        run_id=RUN_ID,
        store=store,
        hub=hub,
        model=model,
        output_dir=output_dir,
        **kwargs,
    )


# ======================================================================
# 1. submit 即停
# ======================================================================


def test_submit_stops_the_loop_immediately(tmp_path):
    """交卷通过校验后：循环不再发起多余的模型/工具调用，plan 正常落库、状态 completed。"""

    store, hub, output_dir = _make_env(tmp_path)
    # 剧本：先交卷，后面还安排了"再调一次工具"的步骤 —— 那一步不应发生。
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args()},
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and len(result.plan.days) == 3
    # 交卷后没有再一次模型调用（search_poi 那步根本没被发起）
    assert model.turns == 1
    assert "search_poi" not in hub.calls
    # plan 正常落库、run 状态 completed —— 不是 FAILED、不是 DEGRADED
    assert store.statuses() == ["completed"]
    assert store.get_plan(RUN_ID) is not None
    assert result.degradations == []
    progress = store.get_run_progress(RUN_ID)
    assert progress is not None and progress["status"] == "SUCCESS"


# ======================================================================
# 2. 校验失败可再修
# ======================================================================


def test_invalid_submit_does_not_stop_the_loop(tmp_path):
    """提交校验不过（缺 intent 的必需字段）→ 循环不停，Agent 还能修一轮再交。

    注：任务描述里的「realtime 价格无来源」在 submit 工具这一层**不会**校验失败
    （`SubmittedPlan.model_validate` 只管 schema，不校验业务规则），那种 plan 会被收下，
    由 `_remove_unattributed_realtime_prices` 事后清掉价格 —— 所以这里用真正会
    ValidationError 的 payload（缺 intent）来覆盖「校验失败 → 不写 holder → 不打断循环」。
    """

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": {"days": [{"day_index": 0}]}},
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and len(result.plan.days) == 1
    # 第一次校验失败没有停循环：agent 还能继续（模型调用数 > 1）
    assert model.turns == 2
    assert result.audit["agent"]["plan_origin"] == SUBMIT_TOOL_NAME


# ======================================================================
# 3. max_llm_calls / max_tool_calls 生效
# ======================================================================


def test_max_llm_calls_truncates_the_loop(tmp_path, monkeypatch):
    """max_llm_calls 设成小值 → 循环被截断、错误文案含「调用次数」、没有假 plan。"""

    monkeypatch.setenv("MAX_LLM_CALLS", "1")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[{"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}}],
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert "调用次数" in (result.error or "")
    # 只允许 1 次模型调用，第二轮被拦下
    assert model.turns == 1


def test_max_llm_calls_keeps_a_submitted_plan(tmp_path, monkeypatch):
    """已交卷时交卷即停放最外层 → 干净完成，不再落到调用次数截断路径。

    交卷成功是终态（_SubmissionEndGuard 优先于预算护栏）：虽然 MAX_LLM_CALLS=1 且交卷后
    还有一步，run 仍以 completed 结束、plan 保留、无"调用次数"降级；交卷后的 search_poi
    根本没有执行。调用次数护栏只管"没交卷还继续烧"的循环（见
    test_max_llm_calls_truncates_the_loop / test_max_tool_calls_truncates_the_loop）。
    """

    monkeypatch.setenv("MAX_LLM_CALLS", "1")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)},
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and len(result.plan.days) == 1
    assert not any("调用次数" in note for note in result.degradations)
    progress = store.get_run_progress(RUN_ID)
    assert progress is not None and progress["status"] == "SUCCESS"


def test_max_tool_calls_truncates_the_loop(tmp_path, monkeypatch):
    """max_tool_calls=1 → 只发生一次工具调用，第二次被拦下并如实失败。"""

    monkeypatch.setenv("MAX_TOOL_CALLS", "1")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
            {"tool": "search_trains", "args": {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert "调用次数" in (result.error or "")
    # 第一次工具调用发生后，下一次模型调用被拦下 → 第二次工具调用没发生
    assert model.turns == 1
    assert hub.calls == ["search_poi"]


def test_call_budget_is_off_by_default(tmp_path):
    """默认 max_llm_calls / max_tool_calls = None：不启用，多轮调用照常跑完。"""

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert model.turns == 2


# ======================================================================
# 4. digest 含新字段
# ======================================================================


def test_prefetch_digest_includes_new_place_fields():
    """`_place_rows` 带 lat/lng/address/opening_hours/district（有值才加，None 不进 context）。"""

    prefetch = SimpleNamespace(
        session_id="ps-1",
        discovery_status="READY",
        basic_intent={},
        discovery={},
        outbound=[],
        inbound=[],
        hotels=[],
        places=[
            SimpleNamespace(
                place_id="B0FF1",
                name="宽窄巷子",
                type="景点",
                business_area=None,
                district="青羊区",
                amap_verified=True,
                lat=30.663,
                lng=104.064,
                address="四川省成都市青羊区同仁路",
                opening_hours="08:30-21:30",
            ),
            # 第二个点这些字段全是 None：对应的键**不**出现（有值才加）
            SimpleNamespace(
                place_id="B0FF2",
                name="某无坐标点",
                type="food",
                business_area=None,
                district=None,
                amap_verified=False,
                lat=None,
                lng=None,
                address=None,
                opening_hours=None,
            ),
        ],
        evidences=[],
        provider_calls=[],
        degradations=[],
    )

    digest = _prefetch_digest(prefetch, {})
    rows = digest["places"]

    assert rows[0]["place_id"] == "B0FF1"
    assert rows[0]["lat"] == 30.663
    assert rows[0]["lng"] == 104.064
    assert rows[0]["address"] == "四川省成都市青羊区同仁路"
    assert rows[0]["opening_hours"] == "08:30-21:30"
    assert rows[0]["district"] == "青羊区"
    # 没有值的字段不灌进 context
    for key in ("lat", "lng", "address", "opening_hours", "district"):
        assert key not in rows[1]


# ======================================================================
# 5. LLM timeout
# ======================================================================


def test_chat_model_wires_the_timeout():
    """`_chat_model(..., timeout=X)` 构造的模型带 timeout；不传保持 None（行为不变）。"""

    from app.agent import _chat_model
    from app.config import ModelEndpoint

    endpoint = ModelEndpoint(
        label="main", model_name="gpt-x", base_url="http://localhost:1/v1", api_key="k"
    )
    model = _chat_model(endpoint, timeout=42.0)
    assert model.request_timeout == 42.0

    model_default = _chat_model(endpoint)
    assert model_default.request_timeout is None


def test_build_model_with_fallbacks_wires_llm_timeout(monkeypatch):
    """`build_model_with_fallbacks` 把 `llm_timeout_seconds` 同时传给主模型与备用模型。"""

    monkeypatch.setenv("MODEL_NAME", "gpt-x")
    monkeypatch.setenv("MODEL_BASE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_NAME_bk1", "gpt-y")
    monkeypatch.setenv("MODEL_BASE_URL_bk1", "http://localhost:2/v1")
    monkeypatch.setenv("MODEL_API_KEY_bk1", "k2")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "37.5")

    from app.agent import build_model_with_fallbacks

    primary, middleware = build_model_with_fallbacks()

    assert primary is not None
    assert primary.request_timeout == 37.5
    assert middleware and len(middleware) == 1
    backup = middleware[0].models[0]
    assert backup.request_timeout == 37.5
