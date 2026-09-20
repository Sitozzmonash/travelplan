"""TravelPlan 的集中式运行配置。

Secret 仍只来自环境变量；本模块只保存可审计的行为开关与阈值。把这些值集中在
这里，维护者才能分清「模型行为」与「旅行领域规则」：Prompt 在 ``prompts.py``，
固定流程在 ``workflow.py``，领域算法在 ``planner.py``。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TravelPlanConfig:
    """一次 run 的非敏感行为配置。

    环境变量只能覆盖同名字段，便于部署时调整；API 永远不会回显 API key 或其他
    Secret。预算限制默认只统计、不阻断，这是本轮产品约定。
    """

    budget_enabled: bool = False
    max_run_tokens: int | None = None
    max_run_cost: float | None = None
    max_llm_calls: int | None = None
    max_tool_calls: int | None = None
    max_run_seconds: int | None = None

    jev_enabled: bool = True
    jev_planner_enabled: bool = True
    jev_timeout_ms: int = 1500
    jev_min_confidence: float = 0.70
    jev_max_calls_per_run: int = 5
    jev_fallback_enabled: bool = True
    jev_model: str = "jev-latest"
    jev_base_url: str = "https://api.typesafe.ai/v1/systemone"

    badcase_enabled: bool = True
    benchmark_enabled: bool = True
    evolution_enabled: bool = False

    @classmethod
    def from_env(cls) -> "TravelPlanConfig":
        """读取显式环境覆盖；无效值回退默认值而不让服务启动失败。"""

        defaults = cls()

        def flag(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def integer(name: str, default: int | None) -> int | None:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def decimal(name: str, default: float | None) -> float | None:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        return cls(
            budget_enabled=flag("BUDGET_ENABLED", defaults.budget_enabled),
            max_run_tokens=integer("MAX_RUN_TOKENS", defaults.max_run_tokens),
            max_run_cost=decimal("MAX_RUN_COST", defaults.max_run_cost),
            max_llm_calls=integer("MAX_LLM_CALLS", defaults.max_llm_calls),
            max_tool_calls=integer("MAX_TOOL_CALLS", defaults.max_tool_calls),
            max_run_seconds=integer("MAX_RUN_SECONDS", defaults.max_run_seconds),
            jev_enabled=flag("JEV_ENABLED", defaults.jev_enabled),
            jev_planner_enabled=flag("JEV_PLANNER_ENABLED", defaults.jev_planner_enabled),
            jev_timeout_ms=integer("JEV_TIMEOUT_MS", defaults.jev_timeout_ms) or defaults.jev_timeout_ms,
            jev_min_confidence=decimal("JEV_MIN_CONFIDENCE", defaults.jev_min_confidence)
            or defaults.jev_min_confidence,
            jev_max_calls_per_run=integer("JEV_MAX_CALLS_PER_RUN", defaults.jev_max_calls_per_run)
            or defaults.jev_max_calls_per_run,
            jev_fallback_enabled=flag("JEV_FALLBACK_ENABLED", defaults.jev_fallback_enabled),
            jev_model=os.environ.get("JEV_MODEL", defaults.jev_model),
            jev_base_url=os.environ.get("JEV_BASE_URL", defaults.jev_base_url),
            badcase_enabled=flag("BADCASE_ENABLED", defaults.badcase_enabled),
            benchmark_enabled=flag("BENCHMARK_ENABLED", defaults.benchmark_enabled),
            evolution_enabled=flag("EVOLUTION_ENABLED", defaults.evolution_enabled),
        )

    def public_dict(self) -> dict[str, Any]:
        """返回可给管理端展示的配置；字段中不含 Secret。"""

        return asdict(self)


def current_config() -> TravelPlanConfig:
    """每次读取当前环境，避免长驻 API 进程把非敏感开关永久缓存。"""

    return TravelPlanConfig.from_env()


@dataclass(frozen=True, slots=True)
class PlannerTuning:
    """Planner 的少量可调阈值。

    这些值原本散落在 ``planner.py`` 的模块常量里。把它们挪到这里，是为了让
    Evolution / Benchmark 能**真的**提出一个候选改动并测出差异 —— 否则"候选改动"
    只是一句描述，无法验证。默认值与原常量逐一对齐，因此不设环境变量时行为不变。

    ``TP_`` 前缀避免与部署环境里其它变量撞名。
    """

    min_candidate_score: float = 0.0
    max_items_per_day: int = 5
    default_cluster_km: float = 1.5
    ad_risk_high: float = 70.0
    day_end_minutes: int = 21 * 60 + 30

    @classmethod
    def from_env(cls) -> "PlannerTuning":
        defaults = cls()

        def decimal(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def integer(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        return cls(
            min_candidate_score=decimal("TP_MIN_CANDIDATE_SCORE", defaults.min_candidate_score),
            max_items_per_day=max(1, integer("TP_MAX_ITEMS_PER_DAY", defaults.max_items_per_day)),
            default_cluster_km=decimal("TP_DEFAULT_CLUSTER_KM", defaults.default_cluster_km),
            ad_risk_high=decimal("TP_AD_RISK_HIGH", defaults.ad_risk_high),
            day_end_minutes=integer("TP_DAY_END_MINUTES", defaults.day_end_minutes),
        )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def current_tuning() -> PlannerTuning:
    """Planner 阈值。每次读取环境，让 Evolution 的临时覆盖能生效。"""

    return PlannerTuning.from_env()


@contextlib.contextmanager
def override_env(values: Mapping[str, str | None]) -> Iterator[None]:
    """临时覆盖环境变量，退出时精确还原。

    Evolution 与 Benchmark 需要"用另一套配置跑一遍"，但又不能污染调用者的进程环境
    （长驻 API 进程里同时可能有真实请求在跑）。这里只改内存中的 ``os.environ``，
    并用 sentinel 区分"原本没有这个变量"和"原本是空串"。
    """

    missing = object()
    previous: dict[str, Any] = {key: os.environ.get(key, missing) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
