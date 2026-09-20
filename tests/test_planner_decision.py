"""Jev 三个决策节点的单元验证（接管任务 §4）。

用**手工构造**的候选方案来测，而不是靠 fixture 碰巧排出几个候选：
决策层的契约必须被精确锁住 —— 选谁、拒绝谁、什么时候回退，都不能是"看起来对"。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.decision.planner_decision import (
    DECISION_PLAN_CHOICE,
    DECISION_QUALITY_GATE,
    DECISION_TRADEOFF,
    choose_plan,
    quality_gate,
    resolve_tradeoff,
)
from app.models import ItineraryDay, ItineraryItem, TripIntent
from app.planner import PlanCandidate, PlanQuality, plan_quality
from tests.fakes import FakeJev

INTENT = TripIntent(
    origin="北京",
    destination=["成都"],
    days=3,
    start_date=date(2026, 10, 1),
    preferences=["美食", "拍照"],
)


def _day(index: int, area: str, names: tuple[str, ...], *, item_type: str = "attraction") -> ItineraryDay:
    return ItineraryDay(
        day_index=index,
        date=date(2026, 10, 1) + timedelta(days=index),
        area=area,
        items=[
            ItineraryItem(
                id=f"d{index}-{name}",
                type=item_type,
                place_id=f"p-{name}",
                name=name,
                duration_minutes=120,
                start_time="09:00",
                end_time="11:00",
            )
            for name in names
        ],
    )


def _candidate(label: str, variant: str, areas: tuple[str, ...], *, food: bool = False) -> PlanCandidate:
    """按 day 构造一个候选；areas 的形态决定它与别的候选是否"拓扑不同"。"""

    days: list[ItineraryDay] = []
    for index, area in enumerate(areas):
        names = (f"{area}景点1", f"{area}景点2")
        days.append(_day(index, area, names))
    if food:
        days[0].items.append(
            ItineraryItem(
                id="d0-lunch",
                type="food",
                place_id="p-lunch",
                name="午餐",
                duration_minutes=60,
                start_time="12:00",
                end_time="13:00",
            )
        )
    return PlanCandidate(
        label=label, variant=variant, days=days, quality=plan_quality(days, INTENT)
    )


CANDIDATE_A = _candidate("A", "nearest", ("青羊区", "武侯区", "锦江区"))
CANDIDATE_B = _candidate("B", "score_first", ("武侯区", "青羊区", "成华区"), food=True)
CANDIDATE_C = _candidate("C", "light_first", ("锦江区", "成华区", "青羊区"))


class TestChoosePlan:
    def test_jev_choice_is_applied(self):
        jev = FakeJev({DECISION_PLAN_CHOICE: {"choice": "B"}})
        choice = choose_plan([CANDIDATE_A, CANDIDATE_B, CANDIDATE_C], INTENT, jev=jev)
        assert choice.candidate.label == "B"
        assert choice.fallback is False
        assert choice.jev_record["decision_type"] == DECISION_PLAN_CHOICE
        assert choice.jev_record["confidence"] == 0.9

    def test_only_one_candidate_never_calls_jev(self):
        jev = FakeJev()
        choice = choose_plan([CANDIDATE_A], INTENT, jev=jev)
        assert jev.calls == []
        assert choice.fallback is True
        assert choice.candidate.label == "A"

    def test_hard_constraint_violation_reverts_to_default(self):
        jev = FakeJev({DECISION_PLAN_CHOICE: {"choice": "C"}})
        choice = choose_plan(
            [CANDIDATE_A, CANDIDATE_B, CANDIDATE_C],
            INTENT,
            jev=jev,
            hard_violations=lambda candidate: ["时间倒置"] if candidate.label == "C" else [],
        )
        assert choice.candidate.label == "A"  # 退回默认方案
        assert choice.fallback is True
        assert choice.rejected == "时间倒置"

    def test_jev_failure_falls_back(self):
        jev = FakeJev({DECISION_PLAN_CHOICE: {"status": "TIMEOUT"}})
        choice = choose_plan([CANDIDATE_A, CANDIDATE_B], INTENT, jev=jev)
        assert choice.candidate.label == "A"
        assert choice.fallback is True
        assert "TIMEOUT" in choice.reason

    def test_jev_choosing_an_unknown_option_is_rejected(self):
        jev = FakeJev({DECISION_PLAN_CHOICE: {"choice": "Z"}})
        choice = choose_plan([CANDIDATE_A, CANDIDATE_B], INTENT, jev=jev)
        assert choice.fallback is True
        assert choice.candidate.label == "A"

    def test_no_jev_client_is_a_safe_fallback(self):
        choice = choose_plan([CANDIDATE_A, CANDIDATE_B], INTENT, jev=None)
        assert choice.fallback is True
        assert choice.jev_record is None

    def test_empty_candidates_is_a_programming_error(self):
        with pytest.raises(ValueError):
            choose_plan([], INTENT, jev=FakeJev())


class TestTradeoff:
    def test_keep_current(self):
        jev = FakeJev({DECISION_TRADEOFF: {"choice": "KEEP_A"}})
        result = resolve_tradeoff(
            current=CANDIDATE_A, alternatives=[CANDIDATE_B], intent=INTENT, jev=jev
        )
        assert result.choice == "KEEP_A"
        assert result.applied_label == "A"
        assert result.fallback is False

    def test_switching_to_another_candidate(self):
        jev = FakeJev({DECISION_TRADEOFF: {"choice": "KEEP_B"}})
        result = resolve_tradeoff(
            current=CANDIDATE_A, alternatives=[CANDIDATE_B], intent=INTENT, jev=jev
        )
        assert result.applied_label == "B"

    def test_replan_choice(self):
        jev = FakeJev({DECISION_TRADEOFF: {"choice": "REPLAN"}})
        result = resolve_tradeoff(
            current=CANDIDATE_A, alternatives=[CANDIDATE_B], intent=INTENT, jev=jev
        )
        assert result.choice == "REPLAN"
        assert result.applied_label is None

    def test_no_alternatives_skips_jev(self):
        jev = FakeJev()
        result = resolve_tradeoff(current=CANDIDATE_A, alternatives=[], intent=INTENT, jev=jev)
        assert jev.calls == []
        assert result.choice == "KEEP_A"


class TestQualityGate:
    def test_keep_when_jev_says_keep(self):
        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "KEEP"}})
        gate = quality_gate(
            quality=CANDIDATE_A.quality, hard_issues=[], intent=INTENT, jev=jev
        )
        assert gate.decision == "KEEP"
        assert gate.unnecessary_replan is False
        assert gate.missed_replan is False

    def test_hard_issues_are_sent_to_jev(self):
        """Jev 必须在**知道**硬问题的前提下做判断，否则 missed_replan 无从谈起。"""
        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "REPLAN"}})
        gate = quality_gate(
            quality=CANDIDATE_A.quality,
            hard_issues=["FEASIBILITY_ERROR@day1: 冲突"],
            intent=INTENT,
            jev=jev,
        )
        assert gate.decision == "REPLAN"
        assert jev.calls[0]["state"]["hard_issues"] == ["FEASIBILITY_ERROR@day1: 冲突"]
        assert gate.hard_issue_count == 1

    def test_unnecessary_replan_is_flagged(self):
        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "REPLAN"}})
        gate = quality_gate(quality=CANDIDATE_A.quality, hard_issues=[], intent=INTENT, jev=jev)
        assert gate.unnecessary_replan is True
        assert gate.missed_replan is False

    def test_missed_replan_is_flagged(self):
        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "KEEP"}})
        gate = quality_gate(
            quality=CANDIDATE_A.quality, hard_issues=["缺少住宿披露"], intent=INTENT, jev=jev
        )
        assert gate.missed_replan is True
        assert gate.unnecessary_replan is False

    def test_fallback_uses_hard_issue_count(self):
        jev = FakeJev({DECISION_QUALITY_GATE: {"status": "TIMEOUT"}})
        assert quality_gate(quality=CANDIDATE_A.quality, hard_issues=[], intent=INTENT, jev=jev).decision == "KEEP"
        assert (
            quality_gate(
                quality=CANDIDATE_A.quality, hard_issues=["x"], intent=INTENT, jev=jev
            ).decision
            == "REPLAN"
        )

    def test_signals_are_code_computed(self):
        gate = quality_gate(
            quality=CANDIDATE_A.quality, hard_issues=[], intent=INTENT, jev=FakeJev()
        )
        assert set(gate.signals) == {"pace_score", "preference_match", "diversity_score", "confidence"}
        assert all(0.0 <= value <= 1.0 for value in gate.signals.values())


class TestQualitySignals:
    def test_hard_quality_floor_caps_confidence(self):
        """软分再高，只要硬分掉下来，置信度就必须跟着掉（任务 §9 禁止软指标掩盖硬错误）。"""

        from app.planner import quality_signals

        good = quality_signals(PlanQuality(
            category_diversity=1.0, consecutive_same_type_count=0, backtracking_score=1.0,
            daily_load_balance=1.0, meal_time_quality=1.0, pace_match=1.0,
            preference_coverage=1.0, first_day_quality=1.0, last_day_quality=1.0,
            route_efficiency=1.0, item_count=9, stay_item_count=7, active_minutes=600, playable_days=3,
        ))
        bad = quality_signals(PlanQuality(
            category_diversity=1.0, consecutive_same_type_count=0, backtracking_score=0.0,
            daily_load_balance=1.0, meal_time_quality=1.0, pace_match=1.0,
            preference_coverage=1.0, first_day_quality=1.0, last_day_quality=1.0,
            route_efficiency=1.0, item_count=9, stay_item_count=7, active_minutes=600, playable_days=3,
        ))
        assert good["confidence"] > bad["confidence"]
        assert good["pace_score"] == bad["pace_score"] == 1.0
