"""指标计算（纯函数，可单测）。

三条口径（与旧版一致，本路径继续沿用）：

1. **算不出来就写 `None`，不写 0**。`0` 是"确实是零"，`None` 是"这次没有可测对象"。
   把两者混起来，"通过率 100%"可能只是"一条用例都没跑"。
2. **分组不给总分**。硬约束 / 行程质量 / 证据 / Provider / 性能五组各自独立 ——
   聚合分会掩盖"具体哪一维退化"。本路径没有 Jev，所以**没有 jev 组**。
3. **套件等权合成**，不按用例数加权：basic 有 3 例、provider_failure 有 2 例，
   按数量加权会让"基础用例多"直接盖过"故障场景表现"。

每个指标都带 `*_rate` / `avg_` / 计数三类后缀之一，一眼能看出量纲。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 维度 → 该维度下的指标名。**新增指标必须登记在这里**，否则会掉进 `other` 组
#: （不会丢，但管理端会看不到分组，等于静默降级）。
METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "hard_constraints": (
        "hard_constraint_violation_rate",
        "days_nonempty_rate",
        "reject_intrusion_rate",
        "unresolved_time_conflict_rate",
        "time_conflict_resolved_rate",
        "budget_overflow_rate",
    ),
    "plan_quality": (
        "avg_items_per_day",
        "category_diversity",
        "preference_coverage",
    ),
    "evidence": (
        "grounded_number_rate",
        "unsupported_number_rate",
        "source_traceable_rate",
    ),
    "provider": (
        "provider_success_rate",
        "provider_failure_calls",
        "injected_failure_hit_rate",
    ),
    "performance": (
        "duration_ms",
        "llm_calls",
        "tool_calls",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    ),
}

ALL_METRICS: tuple[str, ...] = tuple(
    name for names in METRIC_GROUPS.values() for name in names
)

#: `MetricList` 在管理端按这个顺序展示。
GROUP_ORDER: tuple[str, ...] = (*METRIC_GROUPS, "other")


def _rate(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def compute_case_metrics(
    case: Mapping[str, Any], observation: Any, *, passed: bool
) -> dict[str, float | None]:
    """一个 case 的指标。全部来自**已发生的事实**，没有一项来自模型自述。"""

    plan = observation.plan
    days = list(getattr(plan, "days", []) or ())
    items = [item for day in days for item in day.items]
    grounding = dict(observation.grounding or {})
    calls = list(observation.provider_calls or ())
    ok_calls = [row for row in calls if row.get("status") == "OK"]
    run_metrics = dict(observation.run_metrics or {})

    # --- 硬约束 ---
    reject_intruders = list(observation.reject_intruders or ())
    unresolved = int(observation.unresolved_conflicts or 0)
    budget_over = False
    if plan is not None and plan.budget.projected_total is not None and plan.budget.budget_total is not None:
        budget_over = plan.budget.projected_total > plan.budget.budget_total
    violation = bool(reject_intruders) or unresolved > 0 or budget_over
    if observation.status != "completed":
        # "如实失败"的用例不算违规（它没有产出任何行程）；但"completed 却交了空行程"算。
        violation = violation or (plan is not None and not items)

    # --- 行程质量 ---
    types = {str(item.type) for item in items}
    preferences = list(getattr(plan.intent, "preferences", []) or ()) if plan is not None else []
    preference_hits = 0
    for preference in preferences:
        text = " ".join([item.name + item.reason for item in items])
        if str(preference) and str(preference) in text:
            preference_hits += 1

    # --- 证据 ---
    sources = list(observation.sources or ())
    traceable = 0
    for source in getattr(plan, "sources", []) or []:
        if str(source.source_id) in {str(row.get("source_id")) for row in sources}:
            traceable += 1
    checked = grounding.get("checked") or 0
    unsupported = len(grounding.get("unsupported") or ())

    metrics: dict[str, float | None] = {
        "hard_constraint_violation_rate": 1.0 if violation else 0.0,
        "days_nonempty_rate": None if plan is None else (1.0 if days and items else 0.0),
        "reject_intrusion_rate": _rate(len(reject_intruders), max(1, len(observation.reject_ids)))
        if observation.reject_ids else (0.0 if plan is not None else None),
        "unresolved_time_conflict_rate": _rate(unresolved, max(1, observation.conflicts_detected))
        if observation.conflicts_detected else (0.0 if plan is not None else None),
        "time_conflict_resolved_rate": _rate(observation.conflicts_resolved, observation.conflicts_detected)
        if observation.conflicts_detected else None,
        "budget_overflow_rate": None if plan is None else (1.0 if budget_over else 0.0),
        "avg_items_per_day": round(len(items) / len(days), 4) if days else None,
        "category_diversity": _rate(len(types), len(items)),
        "preference_coverage": _rate(preference_hits, len(preferences)) if preferences else None,
        "grounded_number_rate": grounding.get("rate"),
        "unsupported_number_rate": _rate(unsupported, checked),
        "source_traceable_rate": _rate(traceable, len(getattr(plan, "sources", []) or []))
        if plan is not None and getattr(plan, "sources", None) else None,
        "provider_success_rate": _rate(len(ok_calls), len(calls)) if calls else None,
        "provider_failure_calls": float(len(calls) - len(ok_calls)) if calls else None,
        # 故障注入场景真的生效了吗？没生效的用例是"空转"（跑通了但什么都没测），
        # 这个指标就是用来把空转暴露出来的。
        "injected_failure_hit_rate": _injection_hit_rate(case, observation),
        "duration_ms": float(observation.elapsed_ms),
        "llm_calls": _number(run_metrics.get("llm_calls")),
        "tool_calls": _number(run_metrics.get("tool_calls")),
        "total_tokens": _number(run_metrics.get("total_tokens")),
        "input_tokens": _number(run_metrics.get("input_tokens")),
        "output_tokens": _number(run_metrics.get("output_tokens")),
    }
    _ = passed  # 通过与否只进汇总的 passed/failed；指标本身不掺入"判分结果"
    return metrics


def _injection_hit_rate(case: Mapping[str, Any], observation: Any) -> float | None:
    expected = [str(tool) for tool in (case.get("expect") or {}).get("expect_injected") or ()]
    if not expected:
        return None
    injected = {str(tool) for tool in observation.injected}
    return _rate(len([tool for tool in expected if tool in injected]), len(expected))


def _number(value: Any) -> float | None:
    """取不到写 None（"不知道"），不写 0 冒充。"""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_cases(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一组 case 的汇总。每个指标只对**有值**的用例求均值（None 不参与分母）。"""

    metrics: dict[str, float | None] = {}
    for name in ALL_METRICS:
        values = [
            float(result["metrics"][name])
            for result in results
            if isinstance(result.get("metrics", {}).get(name), (int, float))
        ]
        metrics[name] = round(sum(values) / len(values), 4) if values else None
    passed = sum(1 for result in results if result.get("status") == "pass")
    return {
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "metrics": metrics,
    }


def combine_suites(
    results: Sequence[Mapping[str, Any]], suites: Sequence[str]
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """套件内先平均，再对套件等权平均（避免用例多的套件盖过用例少的套件）。"""

    per_suite: dict[str, Any] = {}
    for suite in suites:
        subset = [result for result in results if result.get("suite") == suite]
        if not subset:
            continue
        summary = summarize_cases(subset)
        summary["failures"] = summarize_failures_safe(subset)
        per_suite[suite] = summary

    combined: dict[str, float | None] = {}
    for name in ALL_METRICS:
        values = [
            float(summary["metrics"][name])
            for summary in per_suite.values()
            if isinstance(summary["metrics"].get(name), (int, float))
        ]
        combined[name] = round(sum(values) / len(values), 4) if values else None
    return combined, per_suite


def summarize_failures_safe(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    from benchmark.judges import summarize_failures

    return summarize_failures(results)


def group(metrics: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """扁平指标 → 维度分组。未登记的指标进 `other`（可见地掉队，而不是被丢掉）。"""

    groups: dict[str, dict[str, Any]] = {}
    placed: set[str] = set()
    for name, names in METRIC_GROUPS.items():
        rows = {key: metrics[key] for key in names if key in metrics}
        if rows:
            groups[name] = rows
            placed.update(rows)
    leftovers = {key: value for key, value in metrics.items() if key not in placed}
    if leftovers:
        groups["other"] = leftovers
    return groups


def compare_metrics(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """基线 vs 本轮：逐指标给差值百分比。算不出来的写 None（不猜方向）。"""

    keys = [name for name in ALL_METRICS if name in baseline or name in current]
    delta: dict[str, float | None] = {}
    for key in keys:
        base = baseline.get(key)
        value = current.get(key)
        if isinstance(base, (int, float)) and isinstance(value, (int, float)) and base:
            delta[key] = round((value - base) / abs(base) * 100, 2)
        else:
            delta[key] = None
    return {
        "baseline": {key: baseline.get(key) for key in keys},
        "current": {key: current.get(key) for key in keys},
        "delta_pct": delta,
    }


__all__ = [
    "ALL_METRICS",
    "GROUP_ORDER",
    "METRIC_GROUPS",
    "combine_suites",
    "compare_metrics",
    "compute_case_metrics",
    "group",
    "summarize_cases",
]
