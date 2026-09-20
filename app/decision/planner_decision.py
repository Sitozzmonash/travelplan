"""Jev 在 Planner 里的三个高价值节点（接管任务 §4）。

为什么是这三个而不是"到处调 Jev"：
  * **Top-K 整体方案选择** —— Python 排出若干份硬约束都成立的行程，差别只在偏好匹配
    与节奏上。这类"整体更好"的判断没有唯一正确答案，正是软决策该管的。
  * **关键 Trade-off** —— 当前方案与其它候选在"满足偏好 / 顺路 / 不累"之间各有取舍。
    结构化返回 ``KEEP_A/KEEP_B/KEEP_C/KEEP_D/REPLAN``，Python 按返回值执行。
  * **Planner Quality Gate** —— 结构化返回 ``KEEP/REPLAN``，并给出软分与置信度。

三条硬规矩（贯穿本模块）：
  1. Jev 只做**可枚举选项之间**的选择，永远不生成价格、路线、行程事实。
  2. 任何失败（超时 / 429 / 额度 / 鉴权 / 结构不合法 / 置信度不足）都必须
     **记录 Trace → 标记 fallback → 确定性路径继续**，绝不抛给上层。
  3. 发送给 Jev 的 state 只含"意图摘要 + 候选行程摘要 + 质量软分"，
     不含 API Key、来源 URL、原始 Provider 响应、用户身份、完整 Trace。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.decision.jev import JevClient, JevResult
from app.models import TripIntent
from app.observability import now_iso
from app.planner import PlanCandidate, PlanQuality, quality_signals

#: 允许发送给 Jev 的意图字段白名单。用白名单而不是"排除若干字段"：
#: 以后 TripIntent 新增了敏感字段，默认不会外发。
INTENT_ALLOWLIST = (
    "origin",
    "destination",
    "days",
    "start_date",
    "end_date",
    "pace",
    "preferences",
    "theme",
    "budget_total",
    "travelers",
)

#: 选项数少于这个值时不给 Jev 发问 —— 只有一个选项的"选择"是浪费一次调用。
MIN_OPTIONS_FOR_JEV = 2

DECISION_PLAN_CHOICE = "plan_choice"
DECISION_TRADEOFF = "tradeoff"
DECISION_QUALITY_GATE = "quality_gate"


class SpanRecorder(Protocol):
    """由 Workflow 注入的 span 记录口。

    这里不直接依赖 store：`app/decision` 只负责决策语义，写库是可观测性层的事。
    记录失败绝不影响决策结果（实现方必须自己 try/except）。
    """

    def __call__(
        self,
        *,
        component: str,
        name: str,
        status: str,
        started_at: str,
        finished_at: str,
        attributes: dict[str, Any],
        parent_span_id: str | None = None,
        error: str | None = None,
    ) -> None: ...


def intent_summary(intent: TripIntent) -> dict[str, Any]:
    """意图摘要（白名单字段，且日期转字符串以保持 JSON 可序列化与稳定）。"""

    payload = intent.model_dump(mode="json")
    return {key: payload[key] for key in INTENT_ALLOWLIST if key in payload}


def jev_call_record(
    result: JevResult,
    *,
    decision_type: str,
    input_summary: str,
    criteria: dict[str, str],
) -> dict[str, Any]:
    """把一次 Jev 调用整理成管理端能直接渲染的一条记录。

    ``fallback`` 是这句的关键：它告诉维护者"这次决策最终是 Python 定的，不是 Jev 定的"。
    ``quota`` 拿不到就写 ``unknown`` —— 不伪造剩余额度（任务 §4）。
    """

    return {
        "tag": result.tag,
        "decision_type": decision_type,
        "status": result.status,
        "choice": result.choice,
        "confidence": result.confidence,
        "latency_ms": result.duration_ms,
        "model": result.model,
        "fallback": not result.ok,
        "fallback_reason": None if result.ok else (result.error or result.status),
        "quota": result.quota if result.quota is not None else "unknown",
        "input_summary": input_summary,
        "criteria": criteria,
        "error": result.error,
        "attempted": result.attempted,
    }


def _record(
    recorder: SpanRecorder | None,
    result: JevResult,
    *,
    name: str,
    started_at: str,
    finished_at: str,
    attributes: dict[str, Any],
    parent_span_id: str | None,
) -> None:
    if recorder is None:
        return
    status = "SUCCESS" if result.ok else "WARNING"
    try:
        recorder(
            component="jev",
            name=name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            attributes=attributes,
            parent_span_id=parent_span_id,
            error=None if result.ok else (result.error or result.status),
        )
    except Exception:  # noqa: BLE001 —— 观测不能反过来弄坏一次规划
        return


@dataclass(slots=True)
class PlanChoice:
    """Top-K 方案选择的结果。"""

    candidate: PlanCandidate
    jev_result: JevResult | None
    jev_record: dict[str, Any] | None
    fallback: bool
    reason: str
    rejected: str | None = None
    #: 参与比较的全部候选标签。审计要能回答"当时是在哪几份方案里选的"。
    candidate_labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.candidate.label,
            "variant": self.candidate.variant,
            "candidates": list(self.candidate_labels),
            "fallback": self.fallback,
            "reason": self.reason,
            "rejected": self.rejected,
            "jev": self.jev_record,
        }


def choose_plan(
    candidates: list[PlanCandidate],
    intent: TripIntent,
    *,
    jev: JevClient | None,
    recorder: SpanRecorder | None = None,
    parent_span_id: str | None = None,
    hard_violations: Any = None,
) -> PlanChoice:
    """在 Top-K 候选方案里选一份。

    ``hard_violations`` 是"Python 再次做硬约束复核"的回调：``callable(candidate) -> list[str]``。
    Jev 选中的方案必须通过它；不通过就退回默认方案并如实记录 —— 软决策不得越过硬约束。
    """

    if not candidates:
        raise ValueError("choose_plan 需要至少一个候选方案")
    default = candidates[0]
    if len(candidates) < MIN_OPTIONS_FOR_JEV or jev is None:
        return PlanChoice(
            candidate=default,
            jev_result=None,
            jev_record=None,
            fallback=True,
            reason="候选不足或 Jev 不可用，直接采用默认方案",
            candidate_labels=tuple(candidate.label for candidate in candidates),
        )

    by_label = {candidate.label: candidate for candidate in candidates}
    criteria = {
        candidate.label: (
            f"{candidate.variant} 拓扑：偏好匹配 {candidate.quality.preference_coverage:.0%}、"
            f"节奏匹配 {candidate.quality.pace_match:.0%}、类型多样 {candidate.quality.category_diversity:.0%}"
        )
        for candidate in candidates
    }
    state = {
        "intent": intent_summary(intent),
        "candidates": [candidate.summary() for candidate in candidates],
    }
    instructions = (
        "从候选行程中选出整体体验更好的一个。判断维度：用户偏好匹配、整体节奏、"
        "路线体验、多样性、疲劳程度、总体旅行体验。只返回选项键，不要解释。"
    )
    started = _now()
    result = jev.choose(
        tag=DECISION_PLAN_CHOICE,
        state=state,
        instructions=instructions,
        criteria=criteria,
    )
    finished = _now()
    record = jev_call_record(
        result,
        decision_type=DECISION_PLAN_CHOICE,
        input_summary=f"{len(candidates)} 个候选方案（{', '.join(criteria)}）",
        criteria=criteria,
    )
    summary = record["input_summary"]
    _record(
        recorder,
        result,
        name=DECISION_PLAN_CHOICE,
        started_at=started,
        finished_at=finished,
        attributes=record,
        parent_span_id=parent_span_id,
    )
    if not result.ok or result.choice not in by_label:
        return PlanChoice(
            candidate=default,
            jev_result=result,
            jev_record=record,
            fallback=True,
            reason=f"Jev 未给出可用选择（{result.status}），采用默认方案：{summary}",
            candidate_labels=tuple(candidate.label for candidate in candidates),
        )

    chosen = by_label[str(result.choice)]
    violations = list(hard_violations(chosen)) if callable(hard_violations) else []
    if violations:
        return PlanChoice(
            candidate=default,
            jev_result=result,
            jev_record=record,
            fallback=True,
            reason=f"Jev 选中的方案 {chosen.label} 未通过硬约束复核，已退回默认方案",
            rejected="；".join(violations[:3]),
            candidate_labels=tuple(candidate.label for candidate in candidates),
        )
    return PlanChoice(
        candidate=chosen,
        jev_result=result,
        jev_record=record,
        fallback=False,
        reason=f"Jev 选择 {chosen.label}（confidence={result.confidence}）",
        candidate_labels=tuple(candidate.label for candidate in candidates),
    )


@dataclass(slots=True)
class Tradeoff:
    """关键 Trade-off 的结果。"""

    choice: str
    applied_label: str | None
    jev_result: JevResult | None
    jev_record: dict[str, Any] | None
    fallback: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "choice": self.choice,
            "applied_label": self.applied_label,
            "fallback": self.fallback,
            "reason": self.reason,
            "jev": self.jev_record,
        }


def resolve_tradeoff(
    *,
    current: PlanCandidate,
    alternatives: list[PlanCandidate],
    intent: TripIntent,
    jev: JevClient | None,
    recorder: SpanRecorder | None = None,
    parent_span_id: str | None = None,
) -> Tradeoff:
    """当前方案 vs 其它候选方案的取舍，返回 ``KEEP_x`` 或 ``REPLAN``。

    注意：这里给出的选项**都来自真实存在的候选方案**，不是凭空描述的方案。
    因此 Jev 说 KEEP_B 时 Python 能真的换成 B 并继续跑完整流程。
    """

    if jev is None or not alternatives:
        return Tradeoff(
            choice=f"KEEP_{current.label}",
            applied_label=None,
            jev_result=None,
            jev_record=None,
            fallback=True,
            reason="没有可选替代方案，保持当前方案",
        )

    options = [current, *alternatives]
    criteria = {
        f"KEEP_{candidate.label}": (
            f"采用 {candidate.label}（{candidate.variant}）：偏好匹配 "
            f"{candidate.quality.preference_coverage:.0%}、节奏 {candidate.quality.pace_match:.0%}、"
            f"折返容忍 {candidate.quality.backtracking_score:.0%}"
        )
        for candidate in options
    }
    criteria["REPLAN"] = "现有方案都不合适，要求 Python 重新排一版"

    state = {
        "intent": intent_summary(intent),
        "current": current.summary(),
        "alternatives": [candidate.summary() for candidate in alternatives],
    }
    instructions = (
        "当前行程与其它候选各有取舍。选一个整体体验更好的：既能匹配用户偏好，"
        "又不会让某一天过累或来回折返。若都不合适选 REPLAN。"
    )
    started = _now()
    result = jev.choose(
        tag=DECISION_TRADEOFF,
        state=state,
        instructions=instructions,
        criteria=criteria,
    )
    finished = _now()
    record = jev_call_record(
        result,
        decision_type=DECISION_TRADEOFF,
        input_summary=f"当前 {current.label} + {len(alternatives)} 个替代方案",
        criteria=criteria,
    )
    _record(
        recorder,
        result,
        name=DECISION_TRADEOFF,
        started_at=started,
        finished_at=finished,
        attributes=record,
        parent_span_id=parent_span_id,
    )

    if not result.ok or result.choice not in criteria:
        return Tradeoff(
            choice=f"KEEP_{current.label}",
            applied_label=None,
            jev_result=result,
            jev_record=record,
            fallback=True,
            reason=f"Jev 未给出可用取舍（{result.status}），保持当前方案",
        )

    choice = str(result.choice)
    if choice == "REPLAN":
        return Tradeoff(
            choice=choice,
            applied_label=None,
            jev_result=result,
            jev_record=record,
            fallback=False,
            reason="Jev 认为现有方案都不合适，要求重新排程",
        )
    label = choice.removeprefix("KEEP_")
    applied = next((candidate for candidate in options if candidate.label == label), None)
    return Tradeoff(
        choice=choice,
        applied_label=applied.label if applied is not None else None,
        jev_result=result,
        jev_record=record,
        fallback=False,
        reason=f"Jev 选择 {choice}（confidence={result.confidence}）",
    )


@dataclass(slots=True)
class QualityGate:
    """Planner Quality Gate 的结果。"""

    decision: str
    signals: dict[str, float]
    hard_issue_count: int
    jev_result: JevResult | None
    jev_record: dict[str, Any] | None
    fallback: bool
    reason: str
    unnecessary_replan: bool = False
    missed_replan: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "signals": self.signals,
            "hard_issue_count": self.hard_issue_count,
            "fallback": self.fallback,
            "reason": self.reason,
            "unnecessary_replan": self.unnecessary_replan,
            "missed_replan": self.missed_replan,
            "jev": self.jev_record,
        }


def quality_gate(
    *,
    quality: PlanQuality,
    hard_issues: list[str],
    intent: TripIntent,
    jev: JevClient | None,
    recorder: SpanRecorder | None = None,
    parent_span_id: str | None = None,
) -> QualityGate:
    """KEEP / REPLAN 质量门。

    ``hard_issues`` 是**由代码判定**的硬问题清单（未解决冲突、缺交通/住宿披露等）。
    它必须完整地作为输入传给 Jev：否则 Jev 会在不知道硬问题的情况下说 KEEP，
    这正是 ``missed_replan`` 这一类 Bad Case 的成因。
    """

    signals = quality_signals(quality)
    options = {"KEEP": "保留当前行程", "REPLAN": "要求 Python 重新排一版"}
    if jev is None:
        decision = "REPLAN" if hard_issues else "KEEP"
        return QualityGate(
            decision=decision,
            signals=signals,
            hard_issue_count=len(hard_issues),
            jev_result=None,
            jev_record=None,
            fallback=True,
            reason="Jev 不可用，按硬问题数量走确定性判定",
        )

    state = {
        "intent": intent_summary(intent),
        "quality": quality.to_dict(),
        "signals": signals,
        "hard_issues": hard_issues,
    }
    instructions = (
        "判断这份行程是否需要重新排程。若存在硬问题（时间冲突、缺少交通或住宿披露）"
        "就必须 REPLAN；若只是软指标偏低，仍然选 KEEP。"
    )
    started = _now()
    result = jev.choose(
        tag=DECISION_QUALITY_GATE,
        state=state,
        instructions=instructions,
        criteria=options,
    )
    finished = _now()
    record = jev_call_record(
        result,
        decision_type=DECISION_QUALITY_GATE,
        input_summary=f"硬问题 {len(hard_issues)} 项；软分 {signals}",
        criteria=options,
    )
    _record(
        recorder,
        result,
        name=DECISION_QUALITY_GATE,
        started_at=started,
        finished_at=finished,
        attributes=record,
        parent_span_id=parent_span_id,
    )

    if not result.ok or result.choice not in options:
        return QualityGate(
            decision="REPLAN" if hard_issues else "KEEP",
            signals=signals,
            hard_issue_count=len(hard_issues),
            jev_result=result,
            jev_record=record,
            fallback=True,
            reason=f"Jev 未给出可用结论（{result.status}），按硬问题数量兜底",
        )

    decision = str(result.choice)
    # 两种"Jev 判断与硬事实不一致"都要如实标记，它们是最有价值的 Bad Case 来源。
    unnecessary = decision == "REPLAN" and not hard_issues
    missed = decision == "KEEP" and bool(hard_issues)
    return QualityGate(
        decision=decision,
        signals=signals,
        hard_issue_count=len(hard_issues),
        jev_result=result,
        jev_record=record,
        fallback=False,
        reason=f"Jev 判定 {decision}（confidence={result.confidence}）",
        unnecessary_replan=unnecessary,
        missed_replan=missed,
    )


def _now() -> str:
    return now_iso()
