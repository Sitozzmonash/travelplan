"""固定 Workflow 的运行状态与轻量 Trace 契约。

SuperHarness 仍是 Runtime 级 EventBus/Trace 的唯一所有者；这里仅定义 TravelPlan
业务层需要持久化、供轮询 API 与管理端消费的 Run/Stage 事实，绝不复制工具执行器。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"
    CANCELLED = "CANCELLED"


class StageStatus(StrEnum):
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"


class SpanKind(StrEnum):
    """Trace span 的组件分类（管理端按它分组展示与聚合）。

    §5 要求 Trace 至少覆盖这些类别。分类的意义是：看到一次 run 变慢或变差时，
    维护者能先定位到"是 Provider 慢"还是"Jev 拒绝"还是"Planner 反复重排"，
    而不是在一堆同名 span 里翻。
    """

    WORKFLOW = "workflow"
    LLM = "llm"
    JEV = "jev"
    PROVIDER = "provider"
    TOOL = "tool"
    MCP = "mcp"
    PLANNER = "planner"
    BUDGET = "budget"
    FEASIBILITY = "feasibility"
    CRITIC = "critic"
    STORE = "store"


def span_id(run_id: str, component: str | SpanKind, name: str, *, suffix: str | None = None) -> str:
    """span_id 必须稳定且唯一：同一 run 下 (component, name) 可能重复出现。

    ``suffix`` 供调用方在确实会重复的场景（例如多个 Jev 问题）上区分实例；
    不传时退化为 ``run:component:name``，与既有 workflow span 的命名保持一致。
    """

    tail = f":{suffix}" if suffix else ""
    return f"{run_id}:{component}:{name}{tail}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_run_status(legacy_status: str, *, degraded: bool = False) -> RunStatus:
    """把旧 Workflow 返回值映射到对外稳定的状态模型。"""

    if legacy_status == "completed":
        return RunStatus.DEGRADED if degraded else RunStatus.SUCCESS
    if legacy_status == "cancelled":
        return RunStatus.CANCELLED
    if legacy_status == "running":
        return RunStatus.RUNNING
    return RunStatus.FAILED


@dataclass(slots=True)
class StageProgress:
    stage_id: str
    status: StageStatus
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
