"""Benchmark 指标计算（接管任务 §9）。

设计原则：**每个指标都必须能从一次真实 run 的既有事实里算出来**（plan / trace span /
run_metrics / decisions / badcases），不引入任何模型打分，也不为"好看的分数"编造输入。

算不出来的指标返回 ``None`` 而不是 0：0 是"确实是零"，None 是"这次没有可测的对象"。
把两者混在一起，Jev OFF vs ON 的对比就会得出错误结论。

指标命名与 §9 的清单逐字对齐，因为管理端按名字归组展示（`app/api.py:_group_metrics`）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from app.models import ItineraryDay, TripPlan

#: 这些类型的地点才谈得上"需要证据支撑"，与 app/badcase.py 的口径保持一致。
STAY_TYPES = ("attraction", "food", "activity")

#: 质量评分直接用 Planner 唯一的实现（`plan_quality`），避免"评测一套口径、产品另一套"。
#: 这里的 `plan_quality_metrics` 只做搬运与改名，不重算。

#: 六个维度 → 指标名。管理端按这张表分组，评测报告也按它排版。
METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "hard_constraints": (
        "date_mismatch_rate",
        "hard_time_conflict_rate",
        "opening_hours_violation_rate",
        "missing_transport_disclosure_rate",
        "missing_hotel_disclosure_rate",
        "budget_math_error_rate",
        "hallucinated_price_rate",
        "hallucinated_route_rate",
        "missing_source_rate",
        "hard_constraint_violation_rate",
    ),
    "plan_quality": (
        "category_diversity",
        "consecutive_same_type_count",
        "backtracking_score",
        "daily_load_balance",
        "meal_time_quality",
        "pace_match",
        "preference_coverage",
        "first_day_quality",
        "last_day_quality",
        "route_efficiency",
    ),
    "evidence": (
        "evidence_coverage",
        "multi_source_ratio",
        "poi_verified_ratio",
        "unsupported_recommendation_rate",
    ),
    "provider": (
        "provider_success_rate",
        "fallback_success_rate",
        "timeout_rate",
    ),
    "performance": (
        "end_to_end_latency",
        "llm_latency",
        "jev_latency",
        "provider_latency",
        "llm_calls",
        "jev_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "total_tokens",
    ),
    "jev": (
        "jev_success_rate",
        "jev_fallback_rate",
        "jev_timeout_rate",
        "jev_low_confidence_rate",
        "jev_plan_accept_rate",
        "jev_replan_rate",
        "quality_delta_with_jev",
        "latency_delta_with_jev",
    ),
}

#: 全部指标名（扁平列表）。
ALL_METRICS: tuple[str, ...] = tuple(
    metric for metrics in METRIC_GROUPS.values() for metric in metrics
)

#: 越大越好的指标。用于"候选改动是否真的更好"的判定（`app/evolution.py`）。
HIGHER_IS_BETTER: dict[str, bool] = {
    metric: not metric.endswith("_rate") or metric in {"provider_success_rate", "fallback_success_rate"}
    for metric in ALL_METRICS
}
HIGHER_IS_BETTER.update(
    {
        "jev_success_rate": True,
        "jev_plan_accept_rate": True,
        "quality_delta_with_jev": True,
        "latency_delta_with_jev": False,
        "daily_load_balance": True,
        "backtracking_score": True,
        "meal_time_quality": True,
        "pace_match": True,
        "preference_coverage": True,
        "first_day_quality": True,
        "last_day_quality": True,
        "route_efficiency": True,
        "category_diversity": True,
        "evidence_coverage": True,
        "multi_source_ratio": True,
        "poi_verified_ratio": True,
        "consecutive_same_type_count": False,
        "end_to_end_latency": False,
        "llm_latency": False,
        "jev_latency": False,
        "provider_latency": False,
        "llm_calls": False,
        "jev_calls": False,
        "tool_calls": False,
        "input_tokens": False,
        "output_tokens": False,
        "cached_tokens": False,
        "total_tokens": False,
        "hard_constraint_violation_rate": False,
    }
)


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else round(numerator / denominator, 4)


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else round(sum(values) / len(values), 4)


def _stay_items(day: ItineraryDay) -> list[Any]:
    """停留项：交通与纯机动时间不算"安排"（与 app/planner.py 的口径一致）。"""

    return [item for item in day.items if item.type not in ("transport", "free_time")]


def _attributes(span: Mapping[str, Any]) -> dict[str, Any]:
    value = span.get("attributes")
    return value if isinstance(value, dict) else {}


def _spans(spans: Iterable[Mapping[str, Any]], component: str) -> list[dict[str, Any]]:
    return [dict(span) for span in spans if span.get("component") == component]


class RunFacts:
    """一次 run 的全部可测事实。

    Runner 负责把库里的东西取出来装进这个对象，指标函数只做纯计算 —— 这样指标可以
    单测（构造 RunFacts 即可），不需要跑一次真实的规划。
    """

    def __init__(
        self,
        *,
        plan: TripPlan | None,
        spans: Sequence[Mapping[str, Any]] = (),
        metrics: Mapping[str, Any] | None = None,
        evidence: Sequence[Mapping[str, Any]] = (),
        places: Sequence[Mapping[str, Any]] = (),
        badcases: Sequence[Mapping[str, Any]] = (),
        status: str = "SUCCESS",
    ) -> None:
        self.plan = plan
        self.spans = [dict(span) for span in spans]
        self.run_metrics = dict(metrics or {})
        self.evidence = [dict(item) for item in evidence]
        self.places = [dict(item) for item in places]
        self.badcases = [dict(item) for item in badcases]
        self.status = status

    # --- 派生视图 ---

    @property
    def items(self) -> list[Any]:
        if self.plan is None:
            return []
        return [item for day in self.plan.days for item in day.items]

    @property
    def stay_items(self) -> list[Any]:
        if self.plan is None:
            return []
        return [item for day in self.plan.days for item in _stay_items(day)]

    @property
    def tool_spans(self) -> list[dict[str, Any]]:
        return _spans(self.spans, "tool")

    @property
    def jev_spans(self) -> list[dict[str, Any]]:
        return _spans(self.spans, "jev")

    @property
    def llm_spans(self) -> list[dict[str, Any]]:
        return _spans(self.spans, "llm")


# ======================================================================
# 硬约束
# ======================================================================


def hard_constraint_metrics(facts: RunFacts) -> dict[str, float | None]:
    plan = facts.plan
    if plan is None:
        return {name: None for name in METRIC_GROUPS["hard_constraints"]}

    days = plan.days
    total_days = max(1, len(days))

    # 日期错位：day.date 与 intent.start_date 推出来的日期不一致。
    date_mismatch = 0
    if plan.intent.start_date is not None:
        for day in days:
            expected = plan.intent.start_date.fromordinal(
                plan.intent.start_date.toordinal() + day.day_index
            )
            if day.date is not None and day.date != expected:
                date_mismatch += 1

    # 时间冲突：未解决的 error 级可行性问题。
    unresolved = [issue for issue in plan.warnings if issue.severity == "error" and not issue.resolved]
    time_conflict = sum(1 for issue in unresolved if issue.item_id is None or "TIME" in issue.code or "CONFLICT" in issue.code)

    # 营业时间：opening_hours 不在数据里时无法判定 —— 只统计确有证据的违规（0 表示没发现，
    # 而不是"检查过全部通过"）。这一条在指标里有明确说明，避免误读。
    opening_violations = sum(1 for issue in unresolved if "OPEN" in issue.code or "HOURS" in issue.code)

    # 披露缺失。
    transport_missing = 1 if (plan.transport is None or plan.transport.selected is None) else 0
    hotel_missing = 1 if (plan.hotel is None or plan.hotel.selected is None) else 0

    # 预算算术：分项之和 vs 总额。
    budget = plan.budget
    math_error = 0
    if budget is not None and budget.breakdown:
        total = sum(float(value or 0) for value in budget.breakdown.values())
        if abs(total - float(budget.projected_total or 0)) >= 1.0:
            math_error = 1

    # 编造价格：标了实时价却没有来源引用。
    priced = 0
    unsourced_price = 0
    unsourced_route = 0
    legs = 0
    for item in facts.items:
        if item.price is not None and item.price_type == "realtime":
            priced += 1
            if not item.source_ids:
                unsourced_price += 1
        leg = item.travel_from_previous
        if leg is not None:
            legs += 1
            # "编造路线"= 声称已核实但没有 provider 出处。
            if leg.verified and not leg.provider:
                unsourced_route += 1

    # 缺来源：需要证据的停留项却没有任何 evidence 关联。
    need_evidence = [item for item in facts.stay_items if item.type in STAY_TYPES]
    missing_source = sum(1 for item in need_evidence if not item.evidence_ids)

    return {
        "date_mismatch_rate": _ratio(date_mismatch, total_days),
        "hard_time_conflict_rate": _ratio(time_conflict, total_days),
        "opening_hours_violation_rate": _ratio(opening_violations, total_days),
        "missing_transport_disclosure_rate": float(transport_missing),
        "missing_hotel_disclosure_rate": float(hotel_missing),
        "budget_math_error_rate": float(math_error),
        "hallucinated_price_rate": _ratio(unsourced_price, priced) if priced else None,
        "hallucinated_route_rate": _ratio(unsourced_route, legs) if legs else None,
        "missing_source_rate": _ratio(missing_source, len(need_evidence)) if need_evidence else None,
        "hard_constraint_violation_rate": _ratio(
            date_mismatch + len(unresolved) + transport_missing + hotel_missing + math_error + unsourced_price,
            total_days + 5,
        ),
    }


# ======================================================================
# 计划质量
# ======================================================================


def plan_quality_metrics(facts: RunFacts) -> dict[str, float | None]:
    plan = facts.plan
    if plan is None:
        return {name: None for name in METRIC_GROUPS["plan_quality"]}

    from app.planner import plan_quality

    payload = plan_quality(plan.days, plan.intent).to_dict()
    return {name: payload.get(name) for name in METRIC_GROUPS["plan_quality"]}


# ======================================================================
# 证据 / Provider / 性能 / Jev
# ======================================================================


def evidence_metrics(facts: RunFacts) -> dict[str, float | None]:
    if facts.plan is None:
        return {name: None for name in METRIC_GROUPS["evidence"]}

    scheduled = [item for item in facts.stay_items if item.type in STAY_TYPES]
    with_evidence = [item for item in scheduled if item.evidence_ids]
    providers_per_item: list[int] = []
    evidence_by_id = {str(row.get("evidence_id")): row for row in facts.evidence}
    for item in with_evidence:
        providers = {
            str((evidence_by_id.get(str(evidence_id)) or {}).get("provider") or "")
            for evidence_id in item.evidence_ids
        }
        providers_per_item.append(len({provider for provider in providers if provider}))

    verified = [item for item in facts.stay_items if getattr(item, "place_id", None)]
    amap_verified = [item for item in verified if item.source_ids]

    return {
        "evidence_coverage": _ratio(len(with_evidence), len(scheduled)) if scheduled else None,
        "multi_source_ratio": _ratio(
            sum(1 for count in providers_per_item if count >= 2), len(providers_per_item)
        )
        if providers_per_item
        else None,
        # "POI 已核实"的代码事实是：该地点经过高德 POI 校验（写进 Decision 链）。这里用
        # 行程项带 source 的比例作为可测代理，并在 README 里写明这是代理指标。
        "poi_verified_ratio": _ratio(len(amap_verified), len(verified)) if verified else None,
        # 无证据支撑却被推荐的项（attraction/food/activity 且没有 evidence）。
        "unsupported_recommendation_rate": _ratio(
            len(scheduled) - len(with_evidence), len(scheduled)
        )
        if scheduled
        else None,
    }


def provider_metrics(facts: RunFacts) -> dict[str, float | None]:
    tools = facts.tool_spans
    if not tools:
        return {name: None for name in METRIC_GROUPS["provider"]}

    statuses = Counter(str(_attributes(span).get("status") or "UNKNOWN") for span in tools)
    ok = statuses.get("OK", 0)
    total = len(tools)
    timeout = statuses.get("TIMEOUT", 0)
    # 备用链路成功：同一个 provider 先失败后成功（按 provider 分组看是否存在失败与成功共存）。
    by_provider: dict[str, set[str]] = {}
    for span in tools:
        provider = str(_attributes(span).get("provider") or "unknown")
        by_provider.setdefault(provider, set()).add(str(_attributes(span).get("status")))
    fallback_success = sum(
        1 for status_set in by_provider.values() if "OK" in status_set and len(status_set) > 1
    )

    return {
        "provider_success_rate": _ratio(ok, total),
        "fallback_success_rate": _ratio(fallback_success, len(by_provider)) if by_provider else None,
        "timeout_rate": _ratio(timeout, total),
    }


def performance_metrics(facts: RunFacts) -> dict[str, float | None]:
    metrics = facts.run_metrics
    llm_spans = facts.llm_spans
    jev_spans = facts.jev_spans
    tool_spans = facts.tool_spans

    def _avg_duration(spans: Sequence[Mapping[str, Any]]) -> float | None:
        values = [
            float(value)
            for value in (_attributes(span).get("duration_ms") for span in spans)
            if isinstance(value, (int, float))
        ]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "end_to_end_latency": metrics.get("duration_ms"),
        "llm_latency": _avg_duration(llm_spans),
        "jev_latency": _avg_duration(jev_spans),
        "provider_latency": _avg_duration(tool_spans),
        "llm_calls": metrics.get("llm_calls"),
        "jev_calls": metrics.get("jev_calls"),
        "tool_calls": metrics.get("tool_calls"),
        "input_tokens": metrics.get("input_tokens"),
        "output_tokens": metrics.get("output_tokens"),
        "cached_tokens": metrics.get("cached_tokens"),
        "total_tokens": metrics.get("total_tokens"),
    }


def jev_metrics(facts: RunFacts) -> dict[str, float | None]:
    spans = facts.jev_spans
    metrics: dict[str, float | None] = {name: None for name in METRIC_GROUPS["jev"]}
    if not spans:
        return metrics

    statuses = Counter(str(_attributes(span).get("status") or "UNKNOWN") for span in spans)
    total = len(spans)
    fallback = sum(1 for span in spans if _attributes(span).get("fallback"))

    plan_choice = [
        span for span in spans if _attributes(span).get("decision_type") == "plan_choice"
    ]
    accepted_choice = [
        span
        for span in plan_choice
        if not _attributes(span).get("fallback") and _attributes(span).get("choice")
    ]
    replan = [
        span
        for span in spans
        if _attributes(span).get("choice") == "REPLAN"
        or _attributes(span).get("decision") == "REPLAN"
    ]

    metrics.update(
        {
            "jev_success_rate": _ratio(statuses.get("OK", 0), total),
            "jev_fallback_rate": _ratio(fallback, total),
            "jev_timeout_rate": _ratio(statuses.get("TIMEOUT", 0), total),
            "jev_low_confidence_rate": _ratio(statuses.get("LOW_CONFIDENCE", 0), total),
            "jev_plan_accept_rate": _ratio(len(accepted_choice), len(plan_choice))
            if plan_choice
            else None,
            "jev_replan_rate": _ratio(len(replan), total),
        }
    )
    # quality_delta_with_jev / latency_delta_with_jev 是**套件级**对照量（同一批用例
    # 跑 Jev OFF 与 ON 才有意义），由 `jev_comparison()` 填；单条 run 上只能是 None。
    return metrics


#: 用哪个指标代表"计划质量"。取偏好覆盖与节奏匹配的均值：这两个是用户能直接感受到的，
#: 而且都由代码算出、可复现。硬约束类指标不参与 —— 它们一旦变差就单独看，
#: 不该被平均进一个"质量分"里掩盖掉（§9 明确禁止软指标掩盖硬错误）。
QUALITY_PROXY_METRICS = ("preference_coverage", "pace_match")


def quality_proxy(metrics: Mapping[str, Any]) -> float | None:
    values = [
        float(metrics[name])
        for name in QUALITY_PROXY_METRICS
        if isinstance(metrics.get(name), (int, float))
    ]
    return _mean(values)


def jev_comparison(
    off_metrics: Mapping[str, Any], on_metrics: Mapping[str, Any]
) -> dict[str, float | None]:
    """Jev OFF vs ON 的差值。回答"Jev 到底有没有让行程更好、代价是多少延迟"。"""

    return {
        "quality_delta_with_jev": _delta(
            quality_proxy(on_metrics), quality_proxy(off_metrics)
        ),
        "latency_delta_with_jev": _delta(
            on_metrics.get("end_to_end_latency"), off_metrics.get("end_to_end_latency")
        ),
    }


def _delta(current: Any, reference: Any) -> float | None:
    if not isinstance(current, (int, float)) or not isinstance(reference, (int, float)):
        return None
    return round(float(current) - float(reference), 4)


def compute_metrics(facts: RunFacts) -> dict[str, float | None]:
    """全部指标（扁平）。缺测对象时为 None，而不是 0。"""

    metrics: dict[str, float | None] = {}
    metrics.update(hard_constraint_metrics(facts))
    metrics.update(plan_quality_metrics(facts))
    metrics.update(evidence_metrics(facts))
    metrics.update(provider_metrics(facts))
    metrics.update(performance_metrics(facts))
    metrics.update(jev_metrics(facts))
    # 保证键集合与 §9 清单完全一致（缺一个管理端就少一组）。
    return {name: metrics.get(name) for name in ALL_METRICS}


def group(metrics: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """按六个维度分组（管理端与报告都用它排版）。"""

    return {
        dimension: {name: metrics.get(name) for name in names}
        for dimension, names in METRIC_GROUPS.items()
    }


def summarize_cases(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把一个套件的逐 case 结果聚合成套件级指标。

    做法：先按 case 求平均（每个 case 权重相同），再做套件汇总 —— 否则一个跑了很多
    Provider 用例的 case 会靠"调用次数多"盖过其他 case。
    """

    metrics: dict[str, float | None] = {}
    for name in ALL_METRICS:
        values = [
            float(result["metrics"][name])
            for result in results
            if isinstance(result.get("metrics"), Mapping)
            and isinstance(result["metrics"].get(name), (int, float))
        ]
        metrics[name] = _mean(values)
    passed = sum(1 for result in results if result.get("status") == "pass")
    return {
        "metrics": metrics,
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
    }
