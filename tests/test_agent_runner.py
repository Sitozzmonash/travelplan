"""`app/agent_runner.py` 的离线单测：Agent Loop 出计划这条主路径。

全部离线：不联网、不真 key、不碰 `data/travelplan.db`（store 落在 tmp_path）。
模型与 Hub 都是替身，但**跑的是真的 Agent Loop**（`create_harness_agent` + LangChain 的
工具调用循环），因为要被验证的正是这段接线：工具可见性、交卷机制、时间兜底、落库成文。

每个用例断言的四件事是固定的：
1. 返回的是 `RunResult`（形状与固定流程一致）；
2. plan 只能来自 `submit_final_plan`（或最后一条回复里的 JSON 兜底）；
3. 拿不到行程时 `status=FAILED` 且**没有任何假 plan**；
4. `save_plan` / `finish_run` 真的被调用（用 RecordingStore 记账，不是看返回值猜）。
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agent_runner import SUBMIT_TOOL_NAME, execute_agent_run
from app.models import TripIntent
from app.prompts import TRAVEL_PLANNER_PROMPT_VERSION
from app.store import TravelPlanStore

RUN_ID = "tp-agent-test"
#: 规划流程的 user prompt 里有这一句（见 agent_runner._user_prompt）。模型用它区分
#: "这是主规划的调用"与"这是别的模型调用"（例如长期记忆 consolidation）。
PLANNER_MARK = "请开始：先补齐缺的关键事实"
QUERY = "10月1日从北京去成都玩3天，两个人，预算6000，喜欢美食和拍照"

#: 工具名清单。web_search 不在手动传入的列表里（它与 SuperHarness 自动发现的同名内置
#: 工具冲突），但它必须仍然对模型可见 —— 见 test_all_travel_tools_are_visible_to_the_model。
TRAVEL_TOOL_NAMES = {
    "search_trains",
    "search_flights",
    "search_hotels",
    "search_scenic_tickets",
    "geocode",
    "search_poi",
    "poi_detail",
    "route",
    "search_xiaohongshu",
    "search_douyin",
    "web_search",
}


@pytest.fixture(autouse=True)
def isolated_harness_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块的 Agent 装配不写长期记忆、不打 Console Trace。

    为什么必须做：`MemoryMiddleware` 会把每次 run 的 rollout 写进 `settings.memory_db_url`
    （默认 `data/memory.db`，也就是开发库），测试不该改别人的数据；更麻烦的是它攒够
    `consolidate_rollouts` 条之后会**再调一次模型**做 consolidation —— 那一次会打乱脚本
    模型的动作序列，让用例"有时过有时不过"。

    这里替换的是 `superharness.agent` 模块里的 `settings` 名字（`create_harness_agent`
    装配时读的就是它），不动 `superharness.config`，其它模块不受影响。
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
    # 终端 Console / Trace 渲染读的是 `ObservabilityMiddleware.__init__` 的**默认参数**
    # （def 时就绑定好了），所以改模块属性没用，得改 `__defaults__`。
    # 关掉的只是终端打印，不影响任何业务事实，也不影响本模块自己的步骤上报。
    monkeypatch.setattr(ObservabilityMiddleware.__init__, "__defaults__", (patched,))


# ======================================================================
# 替身：store / hub / 模型
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
    """只实现会被调用的取数方法，返回真实形状的 `ProviderResult`（离线、无网络）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows: list[dict[str, Any]] = []

    # --- 工具内部真正会调的方法 ---

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

    # --- audit 账本（audit_report.json / Bad Case 都读它）---

    def audit_entries(self) -> list[dict[str, Any]]:
        self.rows = [
            {
                "provider": "fake",
                "tool": name,
                "arguments": {},
                "status": "OK",
                "item_count": 1,
                "duration_ms": 3,
                "fetched_at": "2026-09-22T00:00:00+00:00",
                "source_id": f"src-{index}",
            }
            for index, name in enumerate(self.calls)
        ]
        return self.rows


def _result(items: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(status="OK", provider="fake", items=items, error=None)


def _is_planner_call(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage) and PLANNER_MARK in str(message.content)
        for message in messages
    )


class ScriptedPlannerModel(BaseChatModel):
    """按剧本逐轮出招的离线模型：每一步要么调工具，要么直接回复。

    剧本里"非规划调用"（例如长期记忆 consolidation）一律回一句空文本且**不推进脚本**，
    因此用例结果不受开发机 `data/memory.db` 里攒了多少 rollout 影响。
    """

    script: list[dict[str, Any]] = Field(default_factory=list)
    #: 每次模型调用时它看到的工具名（用来验证工具可见性）
    visible: list[list[str]] = Field(default_factory=list)
    turns: int = 0
    #: 每轮回报的 token 用量（input / output / cached），用来验证成本护栏
    usage: tuple[int, int, int] = (30, 10, 4)
    #: 每轮模型调用前的固定延迟（用来验证超时截断）
    delay_seconds: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "scripted-planner"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedPlannerModel:
        self.visible.append(sorted(getattr(tool, "name", "") for tool in tools))
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

        if self.delay_seconds:
            time.sleep(self.delay_seconds)

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


def _submit_args(*, days: int = 3, reject: bool = False) -> dict[str, Any]:
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
    if reject:
        items.append({"name": "用户明确排除的店", "start_time": "14:00", "end_time": "15:00"})
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
# 1. 交卷 → plan → 落库与制品
# ======================================================================


def test_submit_final_plan_is_the_plan_and_pipeline_persists(tmp_path):
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": "search_trains", "args": {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}},
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args()},
            {"text": "行程已经交卷。"},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    # plan 来自 submit_final_plan 的参数，而不是模型随便写的一段文字
    assert result.plan.run_id == RUN_ID
    assert [day.day_index for day in result.plan.days] == [0, 1, 2]
    assert result.plan.days[0].items[0].name == "宽窄巷子"
    assert result.plan.transport is not None
    assert result.plan.transport.selected.train_no == "G89"  # type: ignore[union-attr]
    assert result.plan.hotel is not None and result.plan.hotel.selected.price_per_night == 420  # type: ignore[union-attr]
    # 预算总额来自用户（intent），缺项由代码补齐；结余是算出来的。
    # breakdown 系列字段由最终 plan 重算（中文 key + real/estimated 拆分，见 _rebuild_budget）：
    # 交通 = (780+780)×2 人 = 3120，住宿 = 420/晚×2 晚×1 间 = 840，餐饮估算 = 120/人/天×2人×3天 = 720。
    assert result.plan.budget.budget_total == 6000
    assert result.plan.budget.remaining == 1320
    assert result.plan.budget.status == "within_budget"
    assert result.plan.budget.breakdown["交通"] == 3120
    assert result.plan.budget.breakdown["住宿"] == 840
    assert result.plan.budget.known_real_cost == 3960
    assert result.plan.budget.estimated_cost == 720
    assert result.plan.sources[0].source_id == "src-0"
    # agent 没交酒店 alternatives → 空（前端"其他候选"不出现假数据）
    assert result.plan.hotel.alternatives == []

    # 落库：save_plan / finish_run 真的被调用了
    assert [name for name, *_ in store.calls] == ["save_plan", "finish_run"]
    assert store.statuses() == ["completed"]
    assert store.get_plan(RUN_ID) is not None

    # 制品：与固定流程同一套（plan.json / plan.md / audit_report.json + run 级三件）
    for filename in ("plan.json", "plan.md", "audit_report.json", "trace.jsonl", "metrics.json", "badcases.json"):
        assert filename in result.outputs, filename
        assert Path(result.outputs[filename]).is_file(), filename

    # prompt 版本进 trace 与 audit（admin 要能回答"这次跑的是哪一版 prompt"）
    spans = store.get_trace_spans(RUN_ID)
    prompt_spans = [span for span in spans if span["name"] == "prompt_version"]
    assert prompt_spans, "prompt 版本没有写进 trace"
    assert prompt_spans[0]["attributes"]["prompt_version"] == TRAVEL_PLANNER_PROMPT_VERSION
    assert result.audit["agent"]["prompt_version"] == TRAVEL_PLANNER_PROMPT_VERSION
    assert result.audit["agent"]["plan_origin"] == SUBMIT_TOOL_NAME

    # 工具调用进了 trace（前端"在查火车票"那条轮询链靠它）
    tool_spans = [span for span in spans if span["component"] == "tool"]
    assert {span["name"] for span in tool_spans} == {"search_trains", SUBMIT_TOOL_NAME}
    progress = store.get_run_progress(RUN_ID)
    assert progress is not None

    # 模型调用与 token 进 run_metrics（键名与旧流程一致）
    metrics = store.get_run_metrics(RUN_ID)
    assert metrics["llm_calls"] == 3
    assert metrics["total_tokens"] == 3 * 40
    assert metrics["tool_calls"] == 1  # 只有 search_trains 真的打了 Provider
    # Run 完成即存质量摘要：dashboard 有列值就直接读，不再逐个解 plan_json
    assert metrics["quality_score"] is not None
    assert isinstance(metrics["quality_score"], float)
    assert 0.0 <= metrics["quality_score"] <= 1.0
    # audit 的 llm_calls 与 trace 上的模型调用同形可读
    # （run_metrics 只落固定列，prompt 版本在 trace 与 audit 里，不在这里）
    assert len(result.audit["llm_calls"]) == 3


def test_hotel_alternatives_are_mapped(tmp_path):
    """agent 交 1 个酒店 + 2 个 alternatives → plan.hotel.alternatives 有 2 条，字段原样映射。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=1)
    payload["hotel"] = {
        "name": "春熙路某酒店",
        "provider": "fake",
        "hotel_id": "H-1",
        "source_id": "src-h1",
        "price_per_night": 420,
        "alternatives": [
            {
                "name": "备选酒店A",
                "provider": "fake",
                "hotel_id": "H-2",
                "source_id": "src-h2",
                "price_per_night": 380,
                "rating": 4.5,
            },
            {"name": "备选酒店B", "provider": "fake", "hotel_id": "H-3", "price_per_night": 450},
        ],
    }
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and result.plan.hotel is not None
    assert result.plan.hotel.selected.name == "春熙路某酒店"
    assert result.plan.hotel.selected.hotel_id == "H-1"
    assert result.plan.hotel.selected.source_id == "src-h1"
    assert [alt.name for alt in result.plan.hotel.alternatives] == ["备选酒店A", "备选酒店B"]
    assert result.plan.hotel.alternatives[0].price_per_night == 380
    assert result.plan.hotel.alternatives[0].hotel_id == "H-2"
    assert result.plan.hotel.alternatives[0].source_id == "src-h2"
    assert result.plan.hotel.alternatives[0].rating == 4.5
    assert result.plan.hotel.alternatives[1].price_per_night == 450


def test_hotel_alternatives_are_capped_at_four(tmp_path):
    """超过 4 条候选只留前 4 条（与 transport_alternatives 同量级），不把多余候选灌进 plan。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=1)
    payload["hotel"] = {
        "name": "春熙路某酒店",
        "provider": "fake",
        "price_per_night": 420,
        "alternatives": [
            {"name": f"备选{i}", "provider": "fake", "price_per_night": 300 + i} for i in range(5)
        ],
    }
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and result.plan.hotel is not None
    assert len(result.plan.hotel.alternatives) == 4
    assert [alt.name for alt in result.plan.hotel.alternatives] == [f"备选{i}" for i in range(4)]


def test_budget_breakdown_is_rebuilt_with_chinese_keys(tmp_path):
    """预算卡必须有中文分类行：breakdown 由最终 plan 重算，real/estimated 与 item 价格对得上。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=2)
    # 门票给一个真实价（50 元/人）；大交通 G89+G90 各 780/人，酒店 420/晚 × 1 晚 × 1 间
    payload["days"][0]["items"] = [
        {
            "name": "宽窄巷子",
            "type": "attraction",
            "place_id": "B0FF1",
            "start_time": "09:00",
            "end_time": "11:00",
            "price": 50,
            "price_type": "realtime",
            "source_ids": ["src-0"],
            "reason": "高德核实的真实地点",
        }
    ]
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    breakdown = result.plan.budget.breakdown
    # 中文分类 key：只要有大交通/住宿/门票，预算卡就一定有这几行
    assert breakdown["交通"] == 3120  # (780 + 780) × 2 人
    assert breakdown["住宿"] == 420  # 420/晚 × 1 晚 × 1 间
    assert breakdown["门票"] == 100  # 50/人 × 2 人
    assert breakdown["餐饮估算"] == 480  # 120/人/天 × 2 人 × 2 天（成都 tier2）
    # real 与 estimated 分开，且与上面的 item 价格对得上
    assert result.plan.budget.known_real_cost == 3120 + 420 + 100
    assert result.plan.budget.estimated_cost == 480
    assert result.plan.budget.projected_total == 3120 + 420 + 100 + 480
    assert result.plan.budget.breakdown_price_type["交通"] == "realtime"
    assert result.plan.budget.breakdown_price_type["住宿"] == "realtime"
    assert result.plan.budget.breakdown_price_type["门票"] == "realtime"
    assert result.plan.budget.breakdown_price_type["餐饮估算"] == "estimated"


def test_unattributed_realtime_price_is_removed_before_plan_and_budget(tmp_path):
    """没有逐项来源的数字不能被 Agent 伪装成实时价格。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=1)
    item = payload["days"][0]["items"][0]
    item.update({"price": 11, "price_type": "realtime", "source_ids": []})
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    actual = result.plan.days[0].items[0]
    assert actual.price is None
    assert actual.price_type == "unknown"
    assert "可追溯的工具来源" in actual.risk_note

def test_budget_breakdown_is_rebuilt_when_agent_left_it_empty(tmp_path):
    """Agent 没填 budget.breakdown（甚至只给了预算额）时，预算卡也必须有中文分类行。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=1)
    payload["budget"] = {"budget_total": 6000}
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    breakdown = result.plan.budget.breakdown
    assert "交通" in breakdown and "住宿" in breakdown and "餐饮估算" in breakdown
    assert result.plan.budget.known_real_cost > 0
    assert result.plan.budget.budget_total == 6000
    assert result.plan.budget.status in ("within_budget", "over_budget")


def test_all_travel_tools_are_visible_to_the_model(tmp_path):
    """能力筛选（tool_top_k=3）不能把本业务的工具挡掉 —— 这是 Agent 能干活的前提。"""

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(script=[{"text": "不查了"}])

    _run(store, hub, output_dir, model)

    assert model.visible, "模型一次都没有被调用"
    for names in model.visible:
        assert TRAVEL_TOOL_NAMES <= set(names), sorted(TRAVEL_TOOL_NAMES - set(names))
        assert SUBMIT_TOOL_NAME in names


def test_web_search_is_not_duplicated(tmp_path):
    """手动 tools 里剔除 web_search 后，装配不会因重名抛 ValueError。

    `superharness.agent._merge_local_tools` 在"自动发现 vs 手动传入"重名时直接抛错，
    所以这条用例的价值就是"装配成功"本身；顺带钉住"同名工具只出现一次"。
    """

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(script=[{"text": "不查了"}])

    result = _run(store, hub, output_dir, model)

    assert result.run_id == RUN_ID  # 装配成功，走到了正常返回
    assert model.visible, "模型一次都没有被调用"
    names = model.visible[0]
    assert names.count("web_search") == 1
    assert len(names) == len(set(names))


# ======================================================================
# 2. 没交卷 → 从最后一条消息解析 JSON 兜底
# ======================================================================


def test_message_json_fallback_when_submit_tool_is_not_called(tmp_path):
    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args()
    payload.pop("intent")  # 兜底路径允许缺 intent（如实标降级）
    model = ScriptedPlannerModel(
        script=[
            {"text": "查完了。\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n以上。"}
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and len(result.plan.days) == 3
    assert result.audit["agent"]["plan_origin"] == "message_json"
    assert any(SUBMIT_TOOL_NAME in note for note in result.degradations)
    # 缺 intent 时给的是**空容器**（不编一个日期/预算出来），这一点必须能看出来
    assert result.plan.intent.budget_total is None
    assert result.plan.intent.destination == []


def test_json_fallback_ignores_tool_messages(tmp_path):
    """工具返回里的 JSON 不等于行程：只有 AI 消息里的才算。"""

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": "search_poi", "args": {"keywords": "宽窄巷子", "region": "成都"}},
            {"text": "我查到了一些地点，但还没有排行程。"},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None


# ======================================================================
# 3. 全失败：绝不编造 plan
# ======================================================================


def test_no_plan_means_failed_without_artifacts(tmp_path):
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(script=[{"text": "我查了火车，但还没有排好行程。"}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert SUBMIT_TOOL_NAME in (result.error or "")
    assert store.statuses() == ["failed"]
    # 没有 plan 就不建 plan 三件套（伪造成品比失败更糟）；trace / metrics 仍然留下
    assert "plan.json" not in result.outputs
    assert not (output_dir / RUN_ID / "plan.json").exists()
    assert (output_dir / RUN_ID / "trace.jsonl").is_file()


def test_missing_model_is_reported_honestly(tmp_path, monkeypatch):
    """一个模型都没配全时：如实失败，而不是抛一个看不懂的异常。"""

    store, hub, output_dir = _make_env(tmp_path)
    monkeypatch.setattr("app.agent_runner._resolve_model", lambda model: (None, []))

    result = _run(store, hub, output_dir, ScriptedPlannerModel(script=[{"text": "x"}]))

    assert result.status == "failed"
    assert result.plan is None
    assert "MODEL_NAME" in (result.error or "")


# ======================================================================
# 4. 超步数 / 超时 / 成本护栏
# ======================================================================


def test_recursion_limit_truncates_and_fails_without_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_STEPS", "6")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": "search_poi", "args": {"keywords": "宽窄巷子", "region": "成都"}},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert "最大步数" in (result.error or "")
    # 循环真的被截断了（不是"一直跑到模型自己停下"）
    assert model.turns <= 6


def test_submitted_plan_survives_truncation(tmp_path, monkeypatch):
    """先交卷再继续调工具 → 达到步数上限：行程保留 + 如实标降级。"""

    monkeypatch.setenv("MAX_AGENT_STEPS", "6")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args()},
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    assert any("最大步数" in note for note in result.degradations)
    assert store.statuses() == ["completed"]


def test_timeout_truncates_without_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "1")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[{"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}}],
        delay_seconds=2.0,
    )

    started = time.perf_counter()
    result = _run(store, hub, output_dir, model)
    elapsed = time.perf_counter() - started

    assert result.status == "failed"
    assert result.plan is None
    assert "超时" in (result.error or "")
    assert elapsed < 6.0


def test_token_budget_stops_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_RUN_TOKENS", "50")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[{"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}}],
        usage=(100, 40, 0),
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert "max_run_tokens" in (result.error or "")
    # 第一轮就超预算：第二轮模型调用不该再发生
    assert model.turns == 1


def test_token_budget_keeps_a_submitted_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_RUN_TOKENS", "50")
    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args()},
            {"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}},
        ],
        usage=(100, 40, 0),
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    assert any("成本护栏" in note for note in result.degradations)
    # 降级要同时体现在"对外状态"与"本次 run 的说明"上
    progress = store.get_run_progress(RUN_ID)
    assert progress["status"] == "DEGRADED"
    assert result.audit["agent"]["truncation"] is not None


# ======================================================================
# 5. 时间冲突兜底（Python 唯一保留的校验）
# ======================================================================


def test_time_pass_revises_overlapping_day(tmp_path):
    """路上要 60 分钟、却只留了 10 分钟空档：必须被发现并向后平移。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=2)
    payload["days"] = [
        {
            "day_index": 0,
            "date": "2026-10-01",
            "items": [
                {
                    "name": "宽窄巷子",
                    "type": "attraction",
                    "place_id": "B0FF1",
                    "start_time": "15:00",
                    "end_time": "16:00",
                },
                {
                    "name": "人民公园",
                    "type": "attraction",
                    "place_id": "B0FF2",
                    "start_time": "16:10",
                    "end_time": "17:30",
                    # 高德真实路线 60 分钟（verified=true → 走 routes 而不是直距估算）
                    "travel_from_previous": {
                        "mode": "transit",
                        "duration_minutes": 60,
                        "verified": True,
                    },
                },
            ],
        },
        {
            "day_index": 1,
            "date": "2026-10-02",
            "items": [
                {
                    "name": "人民公园喝茶",
                    "type": "activity",
                    "place_id": "B0FF3",
                    "start_time": "10:00",
                    "end_time": "11:30",
                }
            ],
        },
    ]
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None
    codes = {issue.code for issue in result.plan.warnings}
    assert "TRANSFER_TIME_SHORTFALL" in codes
    # 平移生效：第二个点被推到"上一站结束 + 60 分钟路上时间"之后
    shifted = result.plan.days[0].items[1]
    assert shifted.start_time != "16:10"
    assert shifted.start_time is not None and shifted.start_time >= "16:59"
    # 有真实路线数据时不该无端报"路线未核实"（那会掩盖 Agent 真的查过高德这件事）
    assert "ROUTE_UNVERIFIED" not in codes
    assert any(issue.resolved for issue in result.plan.warnings if issue.code == "TRANSFER_TIME_SHORTFALL")


def test_agent_warnings_are_kept_in_plan(tmp_path):
    """Agent 自己报的时间问题不能被代码那一次校验抹掉。"""

    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args(days=1)
    payload["warnings"] = [
        {
            "day_index": 0,
            "severity": "warning",
            "code": "OPENING_TIME_UNKNOWN",
            "reason": "营业时间本次未获取到",
        }
    ]
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.plan is not None
    codes = {issue.code for issue in result.plan.warnings}
    assert "OPENING_TIME_UNKNOWN" in codes


# ======================================================================
# 6. 用户硬约束（MUST/WANT/REJECT）进 prompt + 调用方 intent 覆盖
# ======================================================================


def test_reject_list_reaches_the_prompt_and_caller_intent_wins(tmp_path):
    store, hub, output_dir = _make_env(tmp_path)
    reference = TripIntent(
        destination=["成都"],
        origin="北京",
        start_date="2026-10-01",
        days=5,
        travelers=2,
        budget_total=8000,
        place_selections={"B0FF1": "MUST", "B0FF2": "REJECT"},
        source="guided",
    )
    seen_prompts: list[str] = []

    class PromptSpy(ScriptedPlannerModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if _is_planner_call(messages):
                seen_prompts.append("\n".join(str(getattr(m, "content", "")) for m in messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = PromptSpy(script=[{"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)}])
    result = _run(store, hub, output_dir, model, intent=reference)

    assert result.status == "completed", result.error
    prompt = seen_prompts[0]
    assert "B0FF2" in prompt and "REJECT" in prompt
    assert "B0FF1" in prompt and "MUST" in prompt
    assert "8000" in prompt  # 预算也进了上下文

    # 调用方（前端已确认）的结构化意图是权威：天数/预算/地点选择都按它
    assert result.plan is not None
    assert result.plan.intent.days == 5
    assert result.plan.intent.budget_total == 8000
    assert result.plan.intent.place_selections == {"B0FF1": "MUST", "B0FF2": "REJECT"}


def test_submit_tool_reports_invalid_args_instead_of_failing_the_run(tmp_path):
    """参数不合法时工具如实回报 INVALID（Agent 可以改），而不是把整次 run 打断。"""

    store, hub, output_dir = _make_env(tmp_path)
    model = ScriptedPlannerModel(
        script=[
            {"tool": SUBMIT_TOOL_NAME, "args": {"days": [{"day_index": 0}]}},
            {"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)},
            {"text": "已重新交卷。"},
        ]
    )

    result = _run(store, hub, output_dir, model)

    assert result.status == "completed", result.error
    assert result.plan is not None and len(result.plan.days) == 1
    # 第一次非法参数没有打断循环，Agent 改对之后才交卷成功
    assert model.turns == 3


def test_empty_days_is_not_a_plan(tmp_path):
    store, hub, output_dir = _make_env(tmp_path)
    payload = _submit_args()
    payload["days"] = []
    model = ScriptedPlannerModel(script=[{"tool": SUBMIT_TOOL_NAME, "args": payload}])

    result = _run(store, hub, output_dir, model)

    assert result.status == "failed"
    assert result.plan is None
    assert "days" in (result.error or "")


# ======================================================================
# 7. 预取（引导式）复用
# ======================================================================


def test_prefetch_is_digested_into_the_prompt_and_adopted(tmp_path):
    """Discovery 预取进 prompt 摘要，并按既有 helper 过户到本次 run 的 sources。"""

    store, hub, output_dir = _make_env(tmp_path)
    store.init_schema()
    prefetch = SimpleNamespace(
        session_id="ps-1",
        discovery_status="READY",
        basic_intent={"origin": "北京", "destination": "成都"},
        discovery={},
        outbound=[SimpleNamespace(label="G89", provider="fake", source_id="src-ps-1", departure_at=None, arrival_at=None, duration_minutes=330, price=780)],
        inbound=[],
        hotels=[SimpleNamespace(name="某酒店", business_area="春熙路", price_per_night=400, rating=4.6, source_id="src-ps-2")],
        places=[SimpleNamespace(place_id="B0FF1", name="宽窄巷子", type="景点", district="青羊区", business_area=None, amap_verified=True)],
        evidences=[],
        provider_calls=[
            {
                "source_id": "src-ps-1",
                "provider": "fake",
                "source_type": "train",
                "tool": "search_trains",
                "arguments": {},
                "status": "OK",
                "item_count": 1,
                "fetched_at": "2026-09-22T00:00:00+00:00",
            }
        ],
        degradations=[],
    )
    seen_prompts: list[str] = []

    class PromptSpy(ScriptedPlannerModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if _is_planner_call(messages):
                seen_prompts.append("\n".join(str(getattr(m, "content", "")) for m in messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = PromptSpy(script=[{"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)}])
    result = _run(store, hub, output_dir, model, prefetch=prefetch, source="guided")

    assert result.status == "completed", result.error
    assert "Discovery" in seen_prompts[0]
    assert "src-ps-2" in seen_prompts[0]
    # 过户：Discovery 的调用写进本次 run 的 sources，audit 里标出"复用"
    assert store.list_sources(RUN_ID)
    reused = [row for row in result.audit["provider_calls"] if "Discovery" in str(row.get("note"))]
    assert reused


# ======================================================================
# 8. 结果形状：调用方（api.py / sessions.py）依赖的字段
# ======================================================================


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_run_result_shape_is_stable(tmp_path, status):
    store, hub, output_dir = _make_env(tmp_path)
    script = (
        [{"tool": SUBMIT_TOOL_NAME, "args": _submit_args(days=1)}]
        if status == "completed"
        else [{"text": "排不出来"}]
    )
    result = _run(store, hub, output_dir, ScriptedPlannerModel(script=script))

    assert result.status == status
    assert isinstance(result.run_id, str) and result.run_id == RUN_ID
    assert isinstance(result.outputs, dict)
    assert isinstance(result.degradations, list)
    assert result.ok is (status == "completed")
    assert result.clarification == "" or isinstance(result.clarification, str)
