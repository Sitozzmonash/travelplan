"""Bad Case：从一次真实 Run 的事实里用规则找出"确实出问题"的地方。

设计取舍（PRD 与接管任务 §7）：
  * **只做规则检测**。普通 Run 不做重型的"自动进化"，也绝不让 LLM 来判断"这次是不是
    坏了" —— 那会产生一批无法复现的 Bad Case，把回归集污染掉。
  * **只写事实**。`expected` / `actual` 都来自已经算好的字段（plan / metrics / trace），
    不引入推断性描述。
  * **id 稳定**。同一次 run 反复检测必须命中同一行，否则 Evolution 会把同一件事
    当成多个失败模式。

Jev 相关的规则单独列出（任务 §7 明确要求 7 类）。它们的输入不是猜测：`jev_*` 状态
直接来自 `JevResult`，而 `jev_wrong_choice` / `jev_unnecessary_replan` /
`jev_missed_replan` 由 Python 在硬约束复核时判定后作为 signals 传进来。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.models import TripPlan

#: 只有这些类型的地点谈得上"需要证据支撑"。
EVIDENCE_REQUIRED_TYPES = ("attraction", "food", "activity")

#: Provider 失败到"整条能力不可用"的严重度分界：交通与地图是行程骨架。
CRITICAL_PROVIDERS = ("12306", "tuniu", "amap")

CATEGORY_PROVIDER_FAILURE = "provider_failure"
CATEGORY_DEGRADATION = "degradation"
CATEGORY_HARD_CONSTRAINT = "hard_constraint"
CATEGORY_BUDGET = "budget"
CATEGORY_BUDGET_MATH = "budget_math"
CATEGORY_EVIDENCE_GAP = "evidence_gap"
CATEGORY_MISSING_DISCLOSURE = "missing_disclosure"
CATEGORY_HALLUCINATED_PRICE = "hallucinated_price"
CATEGORY_ROUTE_UNVERIFIED = "route_unverified"
CATEGORY_JEV_TIMEOUT = "jev_timeout"
CATEGORY_JEV_QUOTA = "jev_quota"
CATEGORY_JEV_INVALID_RESPONSE = "jev_invalid_response"
CATEGORY_JEV_LOW_CONFIDENCE = "jev_low_confidence"
CATEGORY_JEV_WRONG_CHOICE = "jev_wrong_choice"
CATEGORY_JEV_UNNECESSARY_REPLAN = "jev_unnecessary_replan"
CATEGORY_JEV_MISSED_REPLAN = "jev_missed_replan"

#: 供管理端按"是不是 Jev 的问题"分组。
JEV_CATEGORIES = (
    CATEGORY_JEV_TIMEOUT,
    CATEGORY_JEV_QUOTA,
    CATEGORY_JEV_INVALID_RESPONSE,
    CATEGORY_JEV_LOW_CONFIDENCE,
    CATEGORY_JEV_WRONG_CHOICE,
    CATEGORY_JEV_UNNECESSARY_REPLAN,
    CATEGORY_JEV_MISSED_REPLAN,
)

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"


@dataclass(slots=True)
class BadCaseContext:
    """检测所需的全部事实。全部是已经发生过的东西，不含推测。"""

    run_id: str
    plan: TripPlan | None = None
    provider_calls: list[dict[str, Any]] = field(default_factory=list)
    jev_calls: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Python 在硬约束复核阶段对 Jev 决策的判断，例如
    #: ``{"choice_rejected": "...", "unnecessary_replan": "...", "missed_replan": "..."}``。
    jev_signals: dict[str, str] = field(default_factory=dict)
    introduced_in: str | None = None


def badcase_id(run_id: str, category: str, symptom: str) -> str:
    """稳定 id：同一次 run 的同一个症状永远指向同一条记录。"""

    digest = hashlib.sha1(f"{category}|{symptom}".encode("utf-8")).hexdigest()[:10]
    return f"bc-{run_id}-{digest}"


def _case(
    ctx: BadCaseContext,
    *,
    category: str,
    severity: str,
    symptom: str,
    expected: str,
    actual: str,
    suspected_root_cause: str,
    trace_refs: list[str],
    detected_by: str = "rule",
) -> dict[str, Any]:
    return {
        "badcase_id": badcase_id(ctx.run_id, category, symptom),
        "run_id": ctx.run_id,
        "category": category,
        "severity": severity,
        "symptom": symptom,
        "expected": expected,
        "actual": actual,
        "suspected_root_cause": suspected_root_cause,
        # 规则只能给出"疑似"根因；verified/rejected 由人或 Evolution 的验证来改。
        "root_cause_status": "suspected",
        "detected_by": detected_by,
        "trace_refs": trace_refs,
        "analysis_status": "pending",
        "fixed_status": "unfixed",
        "introduced_in": ctx.introduced_in,
        "fixed_in": None,
    }


def _provider_failures(ctx: BadCaseContext) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for call in ctx.provider_calls:
        status = str(call.get("status") or "OK")
        if status == "OK":
            continue
        provider = str(call.get("provider") or "unknown")
        tool = str(call.get("tool") or "unknown")
        critical = provider in CRITICAL_PROVIDERS
        cases.append(
            _case(
                ctx,
                category=CATEGORY_PROVIDER_FAILURE,
                severity=SEVERITY_HIGH if critical else SEVERITY_MEDIUM,
                symptom=f"{provider}/{tool} 返回 {status}",
                expected=f"{provider}.{tool} 成功返回可用数据",
                actual=f"status={status}，返回 {call.get('returned')} 条；{call.get('note') or '无附加说明'}",
                suspected_root_cause=(
                    "该 Provider 未配置或第三方服务波动；也可能是查询参数不被接受"
                ),
                trace_refs=[f"{ctx.run_id}:provider:{provider}:{tool}"],
            )
        )
    return cases


def _degradation_case(ctx: BadCaseContext) -> list[dict[str, Any]]:
    """降级是"如实记录"而不是"必然 bug"，所以只整体记一条，避免刷屏。"""

    if not ctx.degradations:
        return []
    return [
        _case(
            ctx,
            category=CATEGORY_DEGRADATION,
            severity=SEVERITY_MEDIUM,
            symptom=f"本次 run 有 {len(ctx.degradations)} 项能力降级",
            expected="所有声明的能力都能正常参与规划",
            actual="；".join(ctx.degradations[:5]),
            suspected_root_cause="外部能力（模型 / Provider / Jev）不可用，已按确定性路径降级",
            trace_refs=[f"{ctx.run_id}:finalize"],
        )
    ]


def _plan_cases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    plan = ctx.plan
    if plan is None:
        return [
            _case(
                ctx,
                category=CATEGORY_HARD_CONSTRAINT,
                severity=SEVERITY_HIGH,
                symptom="plan 为空",
                expected="流程产出可用的 TripPlan",
                actual="status=SUCCESS 但没有 plan 对象",
                suspected_root_cause="finalize 阶段未装配 plan",
                trace_refs=[f"{ctx.run_id}:finalize"],
            )
        ]

    cases: list[dict[str, Any]] = []

    unresolved = [issue for issue in plan.warnings if issue.severity == "error" and not issue.resolved]
    for issue in unresolved:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_HARD_CONSTRAINT,
                severity=SEVERITY_HIGH,
                symptom=f"{issue.code}@day{issue.day_index}",
                expected="时间/营业时间冲突被自动修订或明确告知用户",
                actual=f"未解决：{issue.reason}",
                suspected_root_cause="排程参数或 Provider 时间数据不满足约束，且自动修订未能收敛",
                trace_refs=[f"{ctx.run_id}:check_feasibility"],
            )
        )

    budget = plan.budget
    if budget is not None:
        if budget.status == "over_budget":
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_BUDGET,
                    severity=SEVERITY_MEDIUM,
                    symptom=f"超出预算 ¥{budget.over_by:,.0f}",
                    expected=f"预计花费不超过预算 ¥{budget.budget_total:,.0f}",
                    actual=f"预计 ¥{budget.projected_total:,.0f}",
                    suspected_root_cause="候选里缺少足够便宜的替代项，或用户预算本身不可行",
                    trace_refs=[f"{ctx.run_id}:check_budget"],
                )
            )
        breakdown_total = sum(float(value or 0) for value in (budget.breakdown or {}).values())
        if budget.breakdown and abs(breakdown_total - float(budget.projected_total or 0)) >= 1.0:
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_BUDGET_MATH,
                    severity=SEVERITY_HIGH,
                    symptom="分项之和与预计总额不一致",
                    expected="breakdown 各项之和等于 projected_total",
                    actual=f"分项合计 ¥{breakdown_total:,.0f} ≠ 总额 ¥{budget.projected_total:,.0f}",
                    suspected_root_cause="预算重算后没有同步更新 breakdown",
                    trace_refs=[f"{ctx.run_id}:finalize"],
                )
            )

    if plan.transport is None or plan.transport.selected is None:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_MISSING_DISCLOSURE,
                severity=SEVERITY_HIGH,
                symptom="没有披露去程交通",
                expected="plan.transport.selected 给出真实去程方案",
                actual="transport 或 selected 为空",
                suspected_root_cause="12306 / 途牛均未返回可用车次机票，且未如实告知用户",
                trace_refs=[f"{ctx.run_id}:search_intercity_transport"],
            )
        )
    if plan.hotel is None or plan.hotel.selected is None:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_MISSING_DISCLOSURE,
                severity=SEVERITY_HIGH,
                symptom="没有披露住宿",
                expected="plan.hotel.selected 给出真实住宿方案",
                actual="hotel 或 selected 为空",
                suspected_root_cause="途牛住宿未返回可用候选",
                trace_refs=[f"{ctx.run_id}:search_hotels"],
            )
        )

    seen_symptoms: set[str] = set()
    #: 低严重度的"逐项"问题先收集再聚合成一条。
    #: 为什么聚合：一次 5 天行程有十几个安排，逐项各写一条会让单次 run 产出几十条
    #: Bad Case（实测 61 条），管理端翻不动、Evolution 也只能看到一堆只出现一次的簇。
    #: 聚合成"本 run 有 N 个点路线未核实（示例：A、B、C）"才是可行动的。
    evidence_gap_names: list[str] = []
    unverified_route_names: list[str] = []
    for day in plan.days:
        for item in day.items:
            if item.type in EVIDENCE_REQUIRED_TYPES and not item.evidence_ids:
                symptom = f"evidence_gap:{item.name}"
                if symptom not in seen_symptoms:
                    seen_symptoms.add(symptom)
                    evidence_gap_names.append(item.name)
            if item.price is not None and item.price_type == "realtime" and not item.source_ids:
                cases.append(
                    _case(
                        ctx,
                        category=CATEGORY_HALLUCINATED_PRICE,
                        severity=SEVERITY_HIGH,
                        symptom=f"realtime_price_without_source:{item.name}",
                        expected="标为实时价就必须能回溯到一次 Provider 调用",
                        actual=f"price={item.price} 但 source_ids 为空",
                        suspected_root_cause="价格在归并/排程阶段被写入但丢失了来源引用",
                        trace_refs=[f"{ctx.run_id}:build_initial_plan"],
                    )
                )
            leg = item.travel_from_previous
            if leg is not None and not leg.verified and leg.estimated:
                unverified_route_names.append(item.name)

    if evidence_gap_names:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_EVIDENCE_GAP,
                severity=SEVERITY_LOW,
                symptom=f"evidence_gap:{len(evidence_gap_names)} 个安排缺少攻略证据",
                expected="推荐项有攻略证据支撑",
                actual=(
                    f"{len(evidence_gap_names)} 个安排没有任何 evidence 关联，"
                    f"示例：{'、'.join(evidence_gap_names[:6])}"
                ),
                suspected_root_cause="社交攻略里没提到这些项，或抽取阶段漏掉了它们",
                trace_refs=[f"{ctx.run_id}:score_candidates"],
            )
        )
    if unverified_route_names:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_ROUTE_UNVERIFIED,
                severity=SEVERITY_LOW,
                symptom=f"unverified_route:{len(unverified_route_names)} 段路线未核实",
                expected="相邻两点之间的路线经高德核实",
                actual=(
                    f"{len(unverified_route_names)} 段路线为估算值，示例："
                    f"{'、'.join(unverified_route_names[:6])}"
                ),
                suspected_root_cause="高德路线接口降级，或两点之间没有可用路线",
                trace_refs=[f"{ctx.run_id}:verify_poi_and_routes"],
            )
        )
    return cases


def _jev_cases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for call in ctx.jev_calls:
        tag = str(call.get("tag") or "unknown")
        status = str(call.get("status") or "")
        ref = f"{ctx.run_id}:jev:{tag}"
        if status == "TIMEOUT":
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_JEV_TIMEOUT,
                    severity=SEVERITY_MEDIUM,
                    symptom=f"jev_timeout:{tag}",
                    expected="Jev 在超时预算内返回决策",
                    actual=f"latency_ms={call.get('latency_ms')}，状态 TIMEOUT",
                    suspected_root_cause="Jev 服务响应慢或网络波动；已在 Python 侧降级",
                    trace_refs=[ref],
                )
            )
        elif status == "BUDGET_EXCEEDED" or status in {"HTTP_429", "HTTP_402", "FREE_CREDIT_EXHAUSTED"}:
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_JEV_QUOTA,
                    severity=SEVERITY_MEDIUM,
                    symptom=f"jev_quota:{tag}",
                    expected="Jev 额度充足",
                    actual=f"状态 {status}；quota={call.get('quota') or 'unknown'}",
                    suspected_root_cause="Jev 额度用尽或达到本次 run 的调用上限",
                    trace_refs=[ref],
                )
            )
        elif status == "INVALID_RESPONSE":
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_JEV_INVALID_RESPONSE,
                    severity=SEVERITY_MEDIUM,
                    symptom=f"jev_invalid_response:{tag}",
                    expected="Jev 返回合法 choice 与 confidence",
                    actual=str(call.get("error") or "响应缺少有效 choice/confidence"),
                    suspected_root_cause="Jev 接口返回结构与适配层契约不一致",
                    trace_refs=[ref],
                )
            )
        elif status == "LOW_CONFIDENCE":
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_JEV_LOW_CONFIDENCE,
                    severity=SEVERITY_LOW,
                    symptom=f"jev_low_confidence:{tag}",
                    expected="Jev 给出高于阈值的置信度，或明确弃权",
                    actual=f"confidence={call.get('confidence')}",
                    suspected_root_cause="候选方案之间差异不明显，或 Jev 对该类问题把握不足",
                    trace_refs=[ref],
                )
            )

    signals = ctx.jev_signals
    if signals.get("choice_rejected"):
        cases.append(
            _case(
                ctx,
                category=CATEGORY_JEV_WRONG_CHOICE,
                severity=SEVERITY_HIGH,
                symptom="jev_choice_failed_hard_constraint",
                expected="Jev 选出的方案通过 Python 的硬约束复核",
                actual=str(signals["choice_rejected"]),
                suspected_root_cause="Jev 拿到的是摘要，看不到硬约束细节；或候选摘要不足以支撑判断",
                trace_refs=[f"{ctx.run_id}:jev:plan_choice"],
            )
        )
    if signals.get("unnecessary_replan"):
        cases.append(
            _case(
                ctx,
                category=CATEGORY_JEV_UNNECESSARY_REPLAN,
                severity=SEVERITY_MEDIUM,
                symptom="jev_replan_without_hard_issue",
                expected="只有存在硬问题时才要求 REPLAN",
                actual=str(signals["unnecessary_replan"]),
                suspected_root_cause="质量门阈值过松，或 Jev 把软偏好当成硬问题",
                trace_refs=[f"{ctx.run_id}:jev:quality_gate"],
            )
        )
    if signals.get("missed_replan"):
        cases.append(
            _case(
                ctx,
                category=CATEGORY_JEV_MISSED_REPLAN,
                severity=SEVERITY_HIGH,
                symptom="jev_kept_plan_with_hard_issue",
                expected="存在未解决硬问题时必须 REPLAN 或明确标注",
                actual=str(signals["missed_replan"]),
                suspected_root_cause="质量门的硬问题输入没有被完整传给 Jev",
                trace_refs=[f"{ctx.run_id}:jev:quality_gate"],
            )
        )
    return cases


def detect_badcases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    """跑全部规则，返回按 (severity, category, symptom) 排序的 Bad Case 列表。"""

    cases = [
        *_provider_failures(ctx),
        *_degradation_case(ctx),
        *_plan_cases(ctx),
        *_jev_cases(ctx),
    ]
    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    cases.sort(key=lambda case: (order.get(case["severity"], 3), case["category"], case["symptom"]))
    return cases


def summarize(cases: list[dict[str, Any]]) -> dict[str, int]:
    """给 metrics.json / 管理端用的计数。"""

    summary = {"total": len(cases), SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 0, SEVERITY_LOW: 0}
    for case in cases:
        severity = str(case.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return summary
