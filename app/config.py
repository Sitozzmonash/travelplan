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

#: Postgres（Neon）连接串的环境变量名。
#: 刻意**不**放进 ``TravelPlanConfig``：该 dataclass 会被 ``public_dict()`` 整份回显到
#: 管理端，而连接串里含用户名/密码。这里只暴露一个读取函数，凭据永远不进配置快照。
DATABASE_URL_ENV = "DATABASE_URL"


def database_url() -> str | None:
    """读取 Postgres 连接串；未配置时返回 None（调用方据此走本地 SQLite）。

    只认环境变量：连接串属于"部署环境事实"，不允许被运行时覆盖（``EDITABLE_KEYS``）改写，
    否则一个手滑的覆盖值就能把线上写到别处去。空串按"未配置"处理。
    """

    raw = os.environ.get(DATABASE_URL_ENV, "").strip()
    return raw or None


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

    # ==================================================================
    # 性能（Part A / D / E）：受控并发、取数上限、Provider 单独 timeout
    # ==================================================================
    # 为什么这些值必须在 config 而不是散落在节点里：它们决定"一次 run 打多少个 Provider
    # 请求、并发几路、等多久放弃"。上线后要调的是这些数字，不是流程代码，放这里才能
    # 通过环境变量 / 管理端在不重启服务的前提下改。

    #: 互不依赖的 Provider 查询并发上限（交通 4 条线、POI 关键词搜索等）。
    #: 它同时是"绝不无限并发打 Provider"这道闸门的唯一开关。
    provider_max_concurrency: int = 4
    #: POI 详情（营业时间/地址）并发上限
    poi_verify_max_concurrency: int = 4
    #: 路线查询并发上限（路线是 verify 阶段最贵的一项，14 段串行能吃掉一分钟）
    route_max_concurrency: int = 4
    #: 互不依赖的模型调用并发上限。刻意给得很小：并发模型调用换不来多少墙钟时间，
    #: 却会让 token 峰值与限流风险一起上升。
    llm_max_concurrency: int = 2

    #: 一次节点内最多拿多少个高德关键词去搜 POI（每个关键词一次 search_poi）
    poi_query_limit: int = 8
    #: 单次 search_poi 要几页
    poi_page_size: int = 10
    #: 最多给几个地点补高德 POI 详情（营业时间/地址）
    poi_detail_limit: int = 6
    #: 最多查几段市内路线（**不做** POI × POI 全连接）
    max_route_lookups: int = 14
    #: 最多查几个景点的门票价格（只有入选候选才查）
    ticket_lookup_limit: int = 3
    #: 攻略里最多提取多少个原始地名送去高德核实（原始候选，不是用户可见候选）
    discovery_max_places: int = 20
    #: Discovery 阶段最多对多少个 POI 做详情核实
    discovery_poi_verify_limit: int = 20
    #: 用户可见的 POI 候选上限（用户旅程 §8：12~20 个，不要一次丢 50 个）
    user_visible_poi_limit: int = 20
    #: 进入 Planner 的深度候选上限（规格：8~15）。超过这个数的候选不参与深度验证
    #: （不查 POI 详情、不参与路线计算），但仍然保留在候选集合里由 Trust/AdRisk 打分。
    planner_poi_limit: int = 15

    # --- 每个 Provider 的单独 timeout（Part E）---
    # 依据是一次真实 run 的 Profiling：12306 单次 59.3s（MCP 冷启动另有 19.6s），
    # 途牛各接口 5s 级但偶发劣化，高德 2.5~3.3s（插件内部 20s × 3 次重试），
    # Tavily 3.5s，TikHub / MediaCrawler 是几十秒级。**不能**所有 Provider 用同一个值：
    # 一个值要么把 12306 的正常慢查询误判成故障，要么让高德的病态重试拖住整个 run。
    #: 途牛（机票/火车/酒店/门票）：偏大，允许它比高德慢一个量级
    tuniu_timeout_seconds: float = 90.0
    #: 12306 MCP：单次实测 59.3s，留足余量；超时后走途牛火车 fallback
    railway_12306_timeout_seconds: float = 90.0
    #: 高德：实测 2.5~3.3s，偏小 —— 超时就按"未核实"处理，绝不用估算值冒充
    amap_timeout_seconds: float = 25.0
    #: TikHub：几十秒级，超时后走 MediaCrawler fallback
    tikhub_timeout_seconds: float = 45.0
    #: MediaCrawler：拉子进程 + 可能卡在扫码登录，偏大（插件默认 180）
    mediacrawler_timeout_seconds: float = 180.0
    #: 联网搜索（Tavily）：实测 3.5s，偏小
    web_search_timeout_seconds: float = 30.0

    # --- 引导式旅程（Planning Session / Discovery）---
    discovery_enabled: bool = True
    planning_session_ttl_minutes: int = 45
    #: 用户可见 POI 候选上限的**历史别名**（管理端旧配置项 / 环境变量 DISCOVERY_PLACE_LIMIT）。
    #: 实际生效的是 ``user_visible_poi_limit``；这里保留字段只为了让旧覆盖值仍能被读到。
    discovery_place_limit: int = 20
    #: 每个类别先展示多少个
    discovery_place_per_category: int = 5
    #: 酒店候选翻几页（途牛首页只给一页，候选池太窄会显得"只会选贵的"）
    discovery_hotel_pages: int = 3
    #: Discovery 并行取数的线程数（交通/酒店/攻略/抽取四条线）
    discovery_workers: int = 4
    #: 用户点「开始规划」时，若 Discovery 还在跑，最多再等这么久（秒）。
    #: 为什么给一个短等待：用户点下去时可能只差一两秒就查完了，等一下能多复用一批候选；
    #: 但**绝不能久等** —— 等到就带上已完成的部分开始，没完成的部分由正式流程自己补查。
    discovery_grace_seconds: float = 3.0

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

        # 用户可见候选上限优先读 USER_VISIBLE_POI_LIMIT，读不到再退回历史别名
        # DISCOVERY_PLACE_LIMIT —— 旧部署的覆盖值必须继续生效，否则一次升级就会
        # 悄悄把"用户看到 20 个候选"变成"看到默认值"。
        visible_places = integer("USER_VISIBLE_POI_LIMIT", None) or integer(
            "DISCOVERY_PLACE_LIMIT", defaults.user_visible_poi_limit
        )

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
            # --- 性能（Part A/D/E）---
            # 并发上限一律钳到 >=1：0 或负数会让节点"什么都不查"，
            # 那是比慢更严重的问题（会静默产出没有验证过的行程）。
            provider_max_concurrency=max(
                1, integer("PROVIDER_MAX_CONCURRENCY", defaults.provider_max_concurrency) or 1
            ),
            poi_verify_max_concurrency=max(
                1, integer("POI_VERIFY_MAX_CONCURRENCY", defaults.poi_verify_max_concurrency) or 1
            ),
            route_max_concurrency=max(
                1, integer("ROUTE_MAX_CONCURRENCY", defaults.route_max_concurrency) or 1
            ),
            llm_max_concurrency=max(
                1, integer("LLM_MAX_CONCURRENCY", defaults.llm_max_concurrency) or 1
            ),
            poi_query_limit=max(1, integer("POI_QUERY_LIMIT", defaults.poi_query_limit) or 1),
            poi_page_size=max(1, integer("POI_PAGE_SIZE", defaults.poi_page_size) or 1),
            poi_detail_limit=integer("POI_DETAIL_LIMIT", defaults.poi_detail_limit)
            if integer("POI_DETAIL_LIMIT", defaults.poi_detail_limit) is not None
            else defaults.poi_detail_limit,
            max_route_lookups=max(1, integer("MAX_ROUTE_LOOKUPS", defaults.max_route_lookups) or 1),
            ticket_lookup_limit=integer("TICKET_LOOKUP_LIMIT", defaults.ticket_lookup_limit)
            if integer("TICKET_LOOKUP_LIMIT", defaults.ticket_lookup_limit) is not None
            else defaults.ticket_lookup_limit,
            discovery_max_places=max(1, integer("DISCOVERY_MAX_PLACES", defaults.discovery_max_places) or 1),
            discovery_poi_verify_limit=integer(
                "DISCOVERY_POI_VERIFY_LIMIT", defaults.discovery_poi_verify_limit
            )
            if integer("DISCOVERY_POI_VERIFY_LIMIT", defaults.discovery_poi_verify_limit) is not None
            else defaults.discovery_poi_verify_limit,
            user_visible_poi_limit=visible_places or defaults.user_visible_poi_limit,
            planner_poi_limit=max(1, integer("PLANNER_POI_LIMIT", defaults.planner_poi_limit) or 1),
            tuniu_timeout_seconds=decimal("TUNIU_TIMEOUT_SECONDS", defaults.tuniu_timeout_seconds)
            or defaults.tuniu_timeout_seconds,
            railway_12306_timeout_seconds=decimal(
                "RAILWAY_12306_TIMEOUT_SECONDS", defaults.railway_12306_timeout_seconds
            )
            or defaults.railway_12306_timeout_seconds,
            amap_timeout_seconds=decimal("AMAP_TIMEOUT_SECONDS", defaults.amap_timeout_seconds)
            or defaults.amap_timeout_seconds,
            tikhub_timeout_seconds=decimal("TIKHUB_TIMEOUT_SECONDS", defaults.tikhub_timeout_seconds)
            or defaults.tikhub_timeout_seconds,
            mediacrawler_timeout_seconds=decimal(
                "MEDIACRAWLER_TIMEOUT_SECONDS", defaults.mediacrawler_timeout_seconds
            )
            or defaults.mediacrawler_timeout_seconds,
            web_search_timeout_seconds=decimal(
                "WEB_SEARCH_TIMEOUT_SECONDS", defaults.web_search_timeout_seconds
            )
            or defaults.web_search_timeout_seconds,
            discovery_enabled=flag("DISCOVERY_ENABLED", defaults.discovery_enabled),
            planning_session_ttl_minutes=integer(
                "PLANNING_SESSION_TTL_MINUTES", defaults.planning_session_ttl_minutes
            )
            or defaults.planning_session_ttl_minutes,
            discovery_place_limit=visible_places or defaults.discovery_place_limit,
            discovery_place_per_category=integer(
                "DISCOVERY_PLACE_PER_CATEGORY", defaults.discovery_place_per_category
            )
            or defaults.discovery_place_per_category,
            discovery_hotel_pages=integer("DISCOVERY_HOTEL_PAGES", defaults.discovery_hotel_pages)
            or defaults.discovery_hotel_pages,
            discovery_workers=integer("DISCOVERY_WORKERS", defaults.discovery_workers)
            or defaults.discovery_workers,
            discovery_grace_seconds=decimal(
                "DISCOVERY_GRACE_SECONDS", defaults.discovery_grace_seconds
            )
            if decimal("DISCOVERY_GRACE_SECONDS", defaults.discovery_grace_seconds) is not None
            else defaults.discovery_grace_seconds,
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
    # 性能：受控并发（Part A / L）。上限给到 16 是有意的护栏 ——
    # 再往上就不是"调参"而是拿 Provider 的限流去赌一次 run 能不能跑完。
    "PROVIDER_MAX_CONCURRENCY": ("int", 1, 16),
    "POI_VERIFY_MAX_CONCURRENCY": ("int", 1, 16),
    "ROUTE_MAX_CONCURRENCY": ("int", 1, 16),
    "LLM_MAX_CONCURRENCY": ("int", 1, 8),
    # 性能：取数上限（Part D）
    "POI_QUERY_LIMIT": ("int", 1, 30),
    "POI_PAGE_SIZE": ("int", 1, 50),
    "POI_DETAIL_LIMIT": ("int", 0, 60),
    "MAX_ROUTE_LOOKUPS": ("int", 0, 60),
    "TICKET_LOOKUP_LIMIT": ("int", 0, 30),
    "DISCOVERY_MAX_PLACES": ("int", 1, 80),
    "DISCOVERY_POI_VERIFY_LIMIT": ("int", 0, 60),
    "USER_VISIBLE_POI_LIMIT": ("int", 4, 60),
    "PLANNER_POI_LIMIT": ("int", 1, 60),
    # 性能：Provider 单独 timeout（秒，Part E）
    "TUNIU_TIMEOUT_SECONDS": ("float", 5.0, 300.0),
    "RAILWAY_12306_TIMEOUT_SECONDS": ("float", 5.0, 300.0),
    "AMAP_TIMEOUT_SECONDS": ("float", 2.0, 120.0),
    "TIKHUB_TIMEOUT_SECONDS": ("float", 5.0, 300.0),
    "MEDIACRAWLER_TIMEOUT_SECONDS": ("float", 5.0, 600.0),
    "WEB_SEARCH_TIMEOUT_SECONDS": ("float", 2.0, 120.0),
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


# ======================================================================
# 性能配置的读取入口（Part A / D / E / M）
# ======================================================================
# Provider / 节点只从这里取"并发几路、查几条、等多久"：env 名与字段名只在一处出现，
# 不会出现"providers.py 抄了一遍 env 名、workflow.py 又抄了一遍"的漂移。

#: Provider 名 → 它自己的 timeout 配置字段（Part E 要求每个 provider 单独一份）。
PROVIDER_TIMEOUT_FIELDS: dict[str, str] = {
    "tuniu": "tuniu_timeout_seconds",
    "12306": "railway_12306_timeout_seconds",
    "amap": "amap_timeout_seconds",
    "tikhub": "tikhub_timeout_seconds",
    "mediacrawler": "mediacrawler_timeout_seconds",
    "tavily": "web_search_timeout_seconds",
}


def provider_timeouts(config: TravelPlanConfig | None = None) -> dict[str, float]:
    """provider → 本次生效的超时秒数。故意**不**提供"一个全局值"的回退出口。"""

    resolved = config or current_config()
    return {
        provider: float(getattr(resolved, field))
        for provider, field in PROVIDER_TIMEOUT_FIELDS.items()
    }


#: 性能摘要要快照的配置项（Part M）：管理端与 profiler 要能回答
#: "这一次 run 是在什么并发上限 / 取数上限 / 超时下跑的"。
PERFORMANCE_CONFIG_KEYS: tuple[str, ...] = (
    "provider_max_concurrency",
    "poi_verify_max_concurrency",
    "route_max_concurrency",
    "llm_max_concurrency",
    "poi_query_limit",
    "poi_page_size",
    "poi_detail_limit",
    "max_route_lookups",
    "ticket_lookup_limit",
    "discovery_max_places",
    "discovery_poi_verify_limit",
    "user_visible_poi_limit",
    "planner_poi_limit",
    "tuniu_timeout_seconds",
    "railway_12306_timeout_seconds",
    "amap_timeout_seconds",
    "tikhub_timeout_seconds",
    "mediacrawler_timeout_seconds",
    "web_search_timeout_seconds",
)


def performance_config_snapshot(config: TravelPlanConfig | None = None) -> dict[str, Any]:
    """性能相关配置的只读快照（不含任何 Secret，只有并发/上限/timeout）。"""

    resolved = config or current_config()
    return {key: getattr(resolved, key) for key in PERFORMANCE_CONFIG_KEYS}


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
