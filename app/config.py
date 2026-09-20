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

    # --- 模型单价（元 / 百万 token）---
    # 成本只有在用户显式配置了单价时才算得出来，否则写 null 而不是猜。
    # 这三项是"用户填的估计值"，产物与管理端都会标注 cost_source=user_price。
    model_price_input_per_million: float | None = None
    model_price_output_per_million: float | None = None
    model_price_cached_per_million: float | None = None

    # --- 引导式旅程（Planning Session / Discovery）---
    discovery_enabled: bool = True
    planning_session_ttl_minutes: int = 45
    #: Discovery 阶段给用户的 POI 候选总数上限（用户旅程 §8：12~20 个，不要一次丢 50 个）
    discovery_place_limit: int = 20
    #: 每个类别先展示多少个
    discovery_place_per_category: int = 5
    #: 酒店候选翻几页（途牛首页只给一页，候选池太窄会显得"只会选贵的"）
    discovery_hotel_pages: int = 3
    #: Discovery 并行取数的线程数（交通/酒店/攻略/抽取四条线）
    discovery_workers: int = 4

    @classmethod
    def from_env(cls) -> "TravelPlanConfig":
        """读取显式环境覆盖；无效值回退默认值而不让服务启动失败。"""

        defaults = cls()

        def flag(name: str, default: bool) -> bool:
            raw = _source(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def integer(name: str, default: int | None) -> int | None:
            raw = _source(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def decimal(name: str, default: float | None) -> float | None:
            raw = _source(name)
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
            jev_model=_source("JEV_MODEL") or defaults.jev_model,
            jev_base_url=_source("JEV_BASE_URL") or defaults.jev_base_url,
            badcase_enabled=flag("BADCASE_ENABLED", defaults.badcase_enabled),
            benchmark_enabled=flag("BENCHMARK_ENABLED", defaults.benchmark_enabled),
            evolution_enabled=flag("EVOLUTION_ENABLED", defaults.evolution_enabled),
            model_price_input_per_million=decimal(
                "MODEL_PRICE_INPUT_PER_MILLION", defaults.model_price_input_per_million
            ),
            model_price_output_per_million=decimal(
                "MODEL_PRICE_OUTPUT_PER_MILLION", defaults.model_price_output_per_million
            ),
            model_price_cached_per_million=decimal(
                "MODEL_PRICE_CACHED_PER_MILLION", defaults.model_price_cached_per_million
            ),
            discovery_enabled=flag("DISCOVERY_ENABLED", defaults.discovery_enabled),
            planning_session_ttl_minutes=integer(
                "PLANNING_SESSION_TTL_MINUTES", defaults.planning_session_ttl_minutes
            )
            or defaults.planning_session_ttl_minutes,
            discovery_place_limit=integer("DISCOVERY_PLACE_LIMIT", defaults.discovery_place_limit)
            or defaults.discovery_place_limit,
            discovery_place_per_category=integer(
                "DISCOVERY_PLACE_PER_CATEGORY", defaults.discovery_place_per_category
            )
            or defaults.discovery_place_per_category,
            discovery_hotel_pages=integer("DISCOVERY_HOTEL_PAGES", defaults.discovery_hotel_pages)
            or defaults.discovery_hotel_pages,
            discovery_workers=integer("DISCOVERY_WORKERS", defaults.discovery_workers)
            or defaults.discovery_workers,
        )

    def public_dict(self) -> dict[str, Any]:
        """返回可给管理端展示的配置；字段中不含 Secret。"""

        return asdict(self)


# ======================================================================
# 运行时覆盖（管理端可编辑的非 Secret 配置）
# ======================================================================
# 为什么要这一层：维护者要在不重启服务的前提下调阈值（Jev 超时、行程阈值、模型单价）。
# 规则只有三条，都很明确：
#   1. 只有白名单里的键可改（`EDITABLE_KEYS`），Secret（API Key / Admin Token）永不在此列；
#   2. 覆盖值优先于环境变量，但 `override_env()` 生效期间环境变量优先 ——
#      Benchmark / Evolution 的对照实验必须能跑在指定配置下，不能被线上覆盖值污染；
#   3. 覆盖由调用方（API 层）落库并加载，config 模块自己不碰数据库。

#: key → (类型, 最小值, 最大值)。类型用于写入时校验，区间用于挡住明显手滑。
EDITABLE_KEYS: dict[str, tuple[str, float | None, float | None]] = {
    # Jev
    "JEV_ENABLED": ("bool", None, None),
    "JEV_PLANNER_ENABLED": ("bool", None, None),
    "JEV_TIMEOUT_MS": ("int", 100, 60000),
    "JEV_MIN_CONFIDENCE": ("float", 0.0, 1.0),
    "JEV_MAX_CALLS_PER_RUN": ("int", 0, 50),
    "JEV_FALLBACK_ENABLED": ("bool", None, None),
    # 预算（本轮默认只统计不限制）
    "BUDGET_ENABLED": ("bool", None, None),
    "MAX_RUN_TOKENS": ("int", 1, None),
    "MAX_RUN_COST": ("float", 0.0, None),
    "MAX_LLM_CALLS": ("int", 1, None),
    "MAX_TOOL_CALLS": ("int", 1, None),
    "MAX_RUN_SECONDS": ("int", 1, None),
    # 模型单价（元/百万 token）；成本据此换算，未配置时 cost 为 null
    "MODEL_PRICE_INPUT_PER_MILLION": ("float", 0.0, None),
    "MODEL_PRICE_OUTPUT_PER_MILLION": ("float", 0.0, None),
    "MODEL_PRICE_CACHED_PER_MILLION": ("float", 0.0, None),
    # 行程阈值（PlannerTuning）
    "TP_MIN_CANDIDATE_SCORE": ("float", -100.0, 100.0),
    "TP_MAX_ITEMS_PER_DAY": ("int", 1, 12),
    "TP_DEFAULT_CLUSTER_KM": ("float", 0.1, 50.0),
    "TP_AD_RISK_HIGH": ("float", 0.0, 100.0),
    "TP_DAY_END_MINUTES": ("int", 600, 1439),
    "TP_HOTEL_PRICE_UNIT": ("float", 1.0, 10000.0),
    "TP_HOTEL_RATING_WEIGHT": ("float", 0.0, 100.0),
    "TP_HOTEL_LOCATION_WEIGHT": ("float", 0.0, 500.0),
    "TP_MUST_PLACE_BONUS": ("float", 0.0, 500.0),
    "TP_WANT_PLACE_BONUS": ("float", 0.0, 500.0),
    # 引导式旅程
    "DISCOVERY_ENABLED": ("bool", None, None),
    "DISCOVERY_PLACE_LIMIT": ("int", 4, 60),
    "DISCOVERY_PLACE_PER_CATEGORY": ("int", 1, 20),
    "DISCOVERY_HOTEL_PAGES": ("int", 1, 10),
    "PLANNING_SESSION_TTL_MINUTES": ("int", 5, 1440),
    # 功能开关
    "BADCASE_ENABLED": ("bool", None, None),
    "EVOLUTION_ENABLED": ("bool", None, None),
}

_RUNTIME_OVERRIDES: dict[str, str] = {}
#: `override_env()` 是否正在生效（Benchmark/Evolution 的对照实验期间为 True）。
_ENV_OVERRIDE_DEPTH = 0


def _source(name: str) -> str | None:
    """读一个配置值的最终来源：运行时覆盖 > 环境变量（但对照实验期间环境变量优先）。"""

    if is_overriding() or name not in _RUNTIME_OVERRIDES:
        value = os.environ.get(name)
        if value is not None:
            return value
    return _RUNTIME_OVERRIDES.get(name, os.environ.get(name))


def set_runtime_overrides(values: Mapping[str, Any] | None) -> None:
    """整体替换运行时覆盖。API 层启动时与每次写入后调用。"""

    _RUNTIME_OVERRIDES.clear()
    for key, value in (values or {}).items():
        if key not in EDITABLE_KEYS or value is None:
            continue
        _RUNTIME_OVERRIDES[str(key)] = _format_override(value)


def runtime_overrides() -> dict[str, str]:
    return dict(_RUNTIME_OVERRIDES)


def _format_override(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_override(key: str, value: Any) -> Any:
    """校验一个待写入的覆盖值，返回规范化后的值；非法就抛 ValueError。

    校验必须在写入前做：一个手滑把 JEV_MIN_CONFIDENCE 写成 8（本意 0.8）会让所有
    Jev 决策变成低置信度，而线上不会有任何报错 —— 只是"突然都不采纳 Jev 了"。
    """

    if key not in EDITABLE_KEYS:
        raise ValueError(f"{key} 不是可运行时修改的配置项")
    kind, low, high = EDITABLE_KEYS[key]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} 需要布尔值，收到 {value!r}")
    if value in (None, ""):
        return None
    try:
        number: float = int(value) if kind == "int" else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 需要{'整数' if kind == 'int' else '数字'}，收到 {value!r}") from exc
    if low is not None and number < low:
        raise ValueError(f"{key} 不能小于 {low}")
    if high is not None and number > high:
        raise ValueError(f"{key} 不能大于 {high}")
    return int(number) if kind == "int" else number


def is_overriding() -> bool:
    return _ENV_OVERRIDE_DEPTH > 0


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

    # --- 交通排序（app/selection.py:transport_weights 决定用哪一组）---
    transport_price_weight: float = 1.0
    transport_duration_weight: float = 1.0
    transport_comfort_weight: float = 1.0
    transport_preference_weight: float = 1.0
    transport_mode_match_bonus: float = 30.0
    transport_red_eye_penalty: float = 30.0
    transport_early_penalty: float = 20.0
    transport_transfer_penalty: float = 25.0

    # --- 酒店排序 ---
    #: 默认 0.01 = 与历史公式 `每晚价格 / 100` 完全等价（换算法前后行为不变）。
    hotel_price_weight: float = 0.01
    hotel_rating_weight: float = 2.0
    #: 位置项权重。默认 0 表示"不把距离算进分"（保持既有口径）；用户选「位置优先」「交通方便」
    #: 或维护者调大这个值时，才会用"离用户所选 POI 中心点的距离"参与打分。
    hotel_location_weight: float = 0.0
    hotel_preference_bonus: float = 20.0
    hotel_unknown_price_penalty: float = 30.0
    hotel_transit_bonus: float = 30.0
    #: 位置项归一化尺度：离所选地点中心 5km 内线性衰减到 0。
    hotel_location_range_km: float = 5.0

    # --- 用户 POI 选择（MUST / WANT 的加分；REJECT 是硬排除，不给分）---
    must_place_bonus: float = 60.0
    want_place_bonus: float = 25.0

    # --- 节奏 auto 的推导阈值（候选数 / 可玩天数）---
    pace_auto_relaxed_max_per_day: float = 3.0
    pace_auto_packed_min_per_day: float = 4.5
    #: 首末天可用时间少于完整一天时的折算系数。
    pace_auto_short_day_bonus: float = 0.15

    @classmethod
    def from_env(cls) -> "PlannerTuning":
        defaults = cls()

        def decimal(name: str, default: float) -> float:
            raw = _source(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        def integer(name: str, default: int) -> int:
            raw = _source(name)
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
            transport_price_weight=decimal("TP_TRANSPORT_PRICE_WEIGHT", defaults.transport_price_weight),
            transport_duration_weight=decimal("TP_TRANSPORT_DURATION_WEIGHT", defaults.transport_duration_weight),
            transport_comfort_weight=decimal("TP_TRANSPORT_COMFORT_WEIGHT", defaults.transport_comfort_weight),
            transport_preference_weight=decimal("TP_TRANSPORT_PREFERENCE_WEIGHT", defaults.transport_preference_weight),
            transport_mode_match_bonus=decimal("TP_TRANSPORT_MODE_BONUS", defaults.transport_mode_match_bonus),
            transport_red_eye_penalty=decimal("TP_TRANSPORT_RED_EYE_PENALTY", defaults.transport_red_eye_penalty),
            transport_early_penalty=decimal("TP_TRANSPORT_EARLY_PENALTY", defaults.transport_early_penalty),
            transport_transfer_penalty=decimal("TP_TRANSPORT_TRANSFER_PENALTY", defaults.transport_transfer_penalty),
            hotel_price_weight=1.0 / max(1.0, decimal("TP_HOTEL_PRICE_UNIT", 1.0 / defaults.hotel_price_weight)),
            hotel_rating_weight=decimal("TP_HOTEL_RATING_WEIGHT", defaults.hotel_rating_weight),
            hotel_location_weight=decimal("TP_HOTEL_LOCATION_WEIGHT", defaults.hotel_location_weight),
            hotel_preference_bonus=decimal("TP_HOTEL_PREFERENCE_BONUS", defaults.hotel_preference_bonus),
            hotel_unknown_price_penalty=decimal("TP_HOTEL_UNKNOWN_PRICE_PENALTY", defaults.hotel_unknown_price_penalty),
            hotel_transit_bonus=decimal("TP_HOTEL_TRANSIT_BONUS", defaults.hotel_transit_bonus),
            hotel_location_range_km=decimal("TP_HOTEL_LOCATION_RANGE_KM", defaults.hotel_location_range_km),
            must_place_bonus=decimal("TP_MUST_PLACE_BONUS", defaults.must_place_bonus),
            want_place_bonus=decimal("TP_WANT_PLACE_BONUS", defaults.want_place_bonus),
            pace_auto_relaxed_max_per_day=decimal(
                "TP_PACE_AUTO_RELAXED_MAX", defaults.pace_auto_relaxed_max_per_day
            ),
            pace_auto_packed_min_per_day=decimal(
                "TP_PACE_AUTO_PACKED_MIN", defaults.pace_auto_packed_min_per_day
            ),
            pace_auto_short_day_bonus=decimal("TP_PACE_AUTO_SHORT_DAY_BONUS", defaults.pace_auto_short_day_bonus),
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

    global _ENV_OVERRIDE_DEPTH
    missing = object()
    previous: dict[str, Any] = {key: os.environ.get(key, missing) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _ENV_OVERRIDE_DEPTH += 1
        yield
    finally:
        _ENV_OVERRIDE_DEPTH = max(0, _ENV_OVERRIDE_DEPTH - 1)
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
