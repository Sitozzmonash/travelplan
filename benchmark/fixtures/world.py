"""Benchmark 的离线世界：把一次 case 变成可复现的 run 输入。

为什么复用 `tests/fakes.py` 而不是另写一套假件：**同一次失败必须只有一种解释**。
如果 Benchmark 有一套自己的假 Provider，那么"单测通过、评测失败"就会变成无法归因的
分歧 —— 而这两者本来测的是同一条业务路径。所以这里只做"把 case 的注入参数翻译成
run 的依赖"这一件事，假件本身仍由 tests/fakes.py 提供。

`live=True` 时不注入任何假件，直接用生产依赖（真实 Provider / 模型 / Jev）——
那是 Live Smoke 的语义，且它的结果**不进确定性指标**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.decision.planner_decision import (
    DECISION_PLAN_CHOICE,
    DECISION_QUALITY_GATE,
    DECISION_TRADEOFF,
)
from app.store import TravelPlanStore

#: Fixture 版本。改了假件的行为或 case 的注入语义就要改它 ——
#: 否则两份 baseline（Jev OFF / ON）会拿不同世界的数据做对比，结论无意义。
FIXTURE_VERSION = "fixtures.v1"

#: 会被注入的假 Jev 决策标签（与 app/decision/planner_decision.py 一致）。
JEV_TAGS = (DECISION_PLAN_CHOICE, DECISION_TRADEOFF, DECISION_QUALITY_GATE)


@dataclass(slots=True)
class CaseWorld:
    """一次 case 的运行时依赖。"""

    hub: Any
    llm: Any
    jev: Any
    store: TravelPlanStore
    run_id: str
    case: dict[str, Any]
    injected: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        closer = getattr(self.hub, "close", None)
        if callable(closer):
            closer()


def build_world(
    case: dict[str, Any],
    *,
    store: TravelPlanStore,
    run_id: str,
    live: bool = False,
    jev_enabled: bool = True,
) -> CaseWorld:
    """按 case 的 `fixtures` 组装依赖。

    确定性套件一律不联网；`live=True` 走真实依赖，此时 `fixtures` 会被忽略
    （给 live case 写注入参数本身就是错的，这里显式记下来而不是默默无视）。
    """

    fixtures = dict(case.get("fixtures") or {})
    if live:
        from app.agent import create_travel_app  # noqa: F401  (导入即校验生产装配可用)
        from app.decision.jev import JevClient
        from app.llm import LLM
        from app.providers import ProviderHub, default_mcp_servers

        hub = ProviderHub(run_id=run_id, store=store, mcp_servers=default_mcp_servers())
        return CaseWorld(
            hub=hub,
            llm=LLM.from_env(),
            jev=JevClient() if jev_enabled else None,
            store=store,
            run_id=run_id,
            case=case,
            injected={"live": True, "fixtures_ignored": sorted(fixtures)},
        )

    from tests.fakes import FakeHub, FakeJev, FakeLLM

    failing = set(fixtures.get("failing") or ())
    empty = set(fixtures.get("empty") or ())
    # poi_spread 决定"这些点算不算同一天的一个区域"。默认值让所有点落进同一地理簇，
    # 于是 Top-K 只排得出一种拓扑；要测 Jev 的"选方案"就必须把点拉开。
    hub = FakeHub(
        store=store,
        run_id=run_id,
        failing=failing,
        empty=empty,
        poi_spread=float(fixtures.get("poi_spread") or 0.01),
    )
    llm = FakeLLM(unavailable=bool(fixtures.get("llm_unavailable")))
    script = {tag: dict(value) for tag, value in (fixtures.get("jev_script") or {}).items()}
    unknown = set(script) - set(JEV_TAGS)
    if unknown:
        raise ValueError(f"case {case.get('case_id')} 的 jev_script 含未知标签：{sorted(unknown)}")
    # Jev OFF 时不注入假 Jev：让"关掉 Jev"走真实的"未配置/关闭"分支，
    # 而不是用一个"假装关掉了"的替身 —— 否则 OFF 基线证明不了任何事。
    jev = FakeJev(script) if jev_enabled else None

    return CaseWorld(
        hub=hub,
        llm=llm,
        jev=jev,
        store=store,
        run_id=run_id,
        case=case,
        injected={"failing": sorted(failing), "empty": sorted(empty), "jev_script": sorted(script)},
    )
