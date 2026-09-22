"""Agent 主路径的离线装配：给"需要一次真实 run 产物"的测试复用。

与 `test_agent_runner.py` 的分工：那边逐个用例钉 Agent Loop 本身的行为（工具可见性、
交卷、截断、成本护栏……），所以自带一套可精细操控的脚本模型；这里只提供**一个最小但
真实**的 run（脚本模型 + 假 Hub + tmp 库），让读接口 / 管理端 / 落库用例不必各自重搭。

模型与 Hub 都是替身，但跑的是**真的 Agent Loop**（`create_harness_agent` + LangChain
的工具调用循环）：读接口要断言的 trace / metrics / 产物，必须是这条路径真的写出来的。
"""

from __future__ import annotations

import dataclasses
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agent_runner import SUBMIT_TOOL_NAME, execute_agent_run
from app.models import TripIntent
from tests.fakes import QUERY, FakeHub

#: 主规划调用的 user prompt 里有这一句（见 `agent_runner._user_prompt`）。模型用它把
#: "这是主规划的调用"与"这是别的模型调用"分开（后者不推进脚本）。
PLANNER_MARK = "请开始：先补齐缺的关键事实"


@contextmanager
def isolated_harness() -> Iterator[None]:
    """一次 run 期间关掉长期记忆 / 终端 Trace。

    必须做：`MemoryMiddleware` 会把 rollout 写进开发库的 `data/memory.db`，攒够条数还会
    再调一次模型做 consolidation —— 那一次会打乱脚本模型的动作序列。这里替换的是
    `superharness.agent` 模块里的 `settings`（`create_harness_agent` 装配时读的就是它），
    以及 `ObservabilityMiddleware.__init__` 的默认参数（终端渲染读的是 def 时绑定的默认值）。
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
    original_settings = harness_agent.settings
    original_defaults = ObservabilityMiddleware.__init__.__defaults__
    harness_agent.settings = patched
    ObservabilityMiddleware.__init__.__defaults__ = (patched,)
    try:
        yield
    finally:
        harness_agent.settings = original_settings
        ObservabilityMiddleware.__init__.__defaults__ = original_defaults


def _is_planner_call(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage) and PLANNER_MARK in str(message.content)
        for message in messages
    )


def _chat(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


class ScriptedPlannerModel(BaseChatModel):
    """按剧本逐轮出招的离线模型：每一步要么调一个工具，要么直接回一段文本。"""

    script: list[dict[str, Any]] = Field(default_factory=list)
    turns: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-agent-fixture"

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
            "input_tokens": 30,
            "output_tokens": 10,
            "total_tokens": 40,
            "input_token_details": {"cache_read": 4},
        }
        name = step.get("tool")
        if name:
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": step.get("args") or {}, "id": f"call-{self.turns}"}],
                usage_metadata=usage,
            )
        else:
            message = AIMessage(content=step.get("text") or "", usage_metadata=usage)
        return _chat(message)


def minimal_plan(*, days: int = 3, **overrides: Any) -> dict[str, Any]:
    """一份最小但完整的交卷参数（时刻 / 价格都是"工具查到"的口径）。"""

    item = {
        "name": "宽窄巷子",
        "type": "attraction",
        "place_id": "B0FF1",
        "start_time": "09:00",
        "end_time": "11:00",
        "price": 0,
        "price_type": "realtime",
        "reason": "高德核实的真实地点",
    }
    payload: dict[str, Any] = {
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
            {"day_index": index, "date": f"2026-10-0{index + 1}", "items": [item]}
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
    payload.update(overrides)
    return payload


def run_agent(
    *,
    store: Any,
    run_id: str,
    output_dir: Path,
    hub: Any | None = None,
    query: str = QUERY,
    user_id: str = "fixture",
    intent: TripIntent | None = None,
    prefetch: Any = None,
    source: str = "quick",
    source_session_id: str | None = None,
    plan: dict[str, Any] | None = None,
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = (),
    days: int = 3,
) -> Any:
    """跑一次真实的 Agent Loop 并把产物落进 `store` / `output_dir`。

    `tool_calls` 是交卷前要先打的工具（(工具名, 参数)）；至少要打一次工具，
    否则 trace 里不会出现 provider 调用。
    """

    script: list[dict[str, Any]] = [
        {"tool": name, "args": args} for name, args in tool_calls
    ]
    script.append({"tool": SUBMIT_TOOL_NAME, "args": plan or minimal_plan(days=days)})
    model = ScriptedPlannerModel(script=script)

    from app.store import TravelPlanStore

    store = store or TravelPlanStore()
    hub = hub if hub is not None else FakeHub(store=store, run_id=run_id)
    with isolated_harness():
        result = execute_agent_run(
            query,
            user_id=user_id,
            run_id=run_id,
            output_dir=output_dir,
            store=store,
            hub=hub,
            model=model,
            intent=intent,
            prefetch=prefetch,
            source=source,
            source_session_id=source_session_id,
        )
    # 让"一次 run 太短，时间戳全一样"的断言不至于踩到 0ms 的边界。
    time.sleep(0)
    return result
