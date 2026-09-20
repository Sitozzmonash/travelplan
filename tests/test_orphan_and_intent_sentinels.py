"""本次收口的两个可观测性保证：

1. **启动收敛孤儿 run**：进程被重启（Render 免费层会在 512Mi OOM 后重启）时，
   后台线程里的 run 随进程消失，状态永远停在 `RUNNING`，前端会一直转圈。
   启动时把它们收敛成 `CANCELLED`。
2. **两条回归哨兵**：`guided_intent_mismatch` / `discovery_stale` 按当前设计
   *不可能* 触发（基础信息不可 PATCH、Guided intent 不经模型重解析）。既然真实路径
   造不出来，就用合成的 journey 数据证明"真出了这种事会报出来、且不会误报"。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.api import ORPHAN_RUN_REASON, SKIP_ORPHAN_SWEEP_ENV, _converge_orphan_runs
from app.badcase import BadCaseContext, detect_badcases
from app.store import TravelPlanStore
from tests.fakes import make_store


# ----------------------------------------------------------------------
# 1) 启动收敛孤儿 run
# ----------------------------------------------------------------------


def _seed_run(store: TravelPlanStore, run_id: str, *, running: bool) -> None:
    """造一条 run。`finish_run` 收的是旧 API 的小写状态（"completed"），不是 "SUCCESS"。"""

    store.create_run(run_id, original_query="测试用", source="cli")
    store.finish_run(run_id, "running")
    if not running:
        store.finish_run(run_id, "completed")


class TestOrphanRunSweep:
    def test_running_runs_are_converged_to_cancelled(self, tmp_path) -> None:
        store = make_store(tmp_path / "orphan.db")
        _seed_run(store, "tp-alive", running=True)
        _seed_run(store, "tp-done", running=False)

        cancelled = store.cancel_orphan_runs(reason="重启")

        assert cancelled == ["tp-alive"]
        assert store.get_run_progress("tp-alive")["status"] == "CANCELLED"
        # 已结束的 run 不能被顺手改掉，否则历史会失真。
        assert store.get_run_progress("tp-done")["status"] == "SUCCESS"

    def test_cancelled_run_carries_a_readable_reason(self, tmp_path) -> None:
        """前端会把这句原话展示给用户，所以它得说清"为什么中断了"。"""

        store = make_store(tmp_path / "orphan2.db")
        _seed_run(store, "tp-alive", running=True)

        store.cancel_orphan_runs(reason=ORPHAN_RUN_REASON)

        progress = store.get_run_progress("tp-alive")
        assert progress["error"] == ORPHAN_RUN_REASON
        assert "重启" in progress["message"]

    def test_second_sweep_is_a_noop(self, tmp_path) -> None:
        store = make_store(tmp_path / "orphan3.db")
        _seed_run(store, "tp-alive", running=True)

        assert store.cancel_orphan_runs(reason="重启") == ["tp-alive"]
        assert store.cancel_orphan_runs(reason="重启") == []

    def test_startup_sweep_skips_when_disabled(self, tmp_path, monkeypatch) -> None:
        """conftest 默认关掉它，避免测试触发启动事件时写别人的库。"""

        store = make_store(tmp_path / "orphan4.db")
        _seed_run(store, "tp-alive", running=True)
        monkeypatch.setenv(SKIP_ORPHAN_SWEEP_ENV, "1")
        monkeypatch.setattr("app.api.get_store", lambda: store)

        _converge_orphan_runs()

        assert store.get_run_progress("tp-alive")["status"] == "RUNNING"

    def test_startup_sweep_converges_when_enabled(self, tmp_path, monkeypatch) -> None:
        store = make_store(tmp_path / "orphan5.db")
        _seed_run(store, "tp-alive", running=True)
        monkeypatch.delenv(SKIP_ORPHAN_SWEEP_ENV, raising=False)
        monkeypatch.setattr("app.api.get_store", lambda: store)

        _converge_orphan_runs()

        assert store.get_run_progress("tp-alive")["status"] == "CANCELLED"


# ----------------------------------------------------------------------
# 2) 两条回归哨兵
# ----------------------------------------------------------------------


def _journey(**overrides):
    """一份"一切正常"的 guided journey，用来单点制造异常。"""

    base = {
        "source": "guided",
        "source_session_id": "ps-1",
        "confirmed_basic_intent": {
            "destination": "成都",
            "start_date": "2026-10-01",
            "days": 5,
            "travelers": 2,
        },
        "planned_intent": {
            "destination": "成都",
            "start_date": "2026-10-01",
            "days": 5,
            "travelers": 2,
        },
        "place_selections": {"must": 0, "want": 0, "reject": 0},
        "discovery": {},
        "prefetch_available": [],
        "prefetch_reused": {},
        "prefetch_reused_any": False,
        "rejected_in_plan": [],
        "must_missing": [],
    }
    base.update(overrides)
    return base


def _journey_categories(journey) -> set[str]:
    """只取这两条哨兵，别的规则（比如 plan 缺失时的 hard_constraint）不在这条用例的射程内。"""

    cases = detect_badcases(BadCaseContext(run_id="tp-s", journey=journey))
    return {case["category"] for case in cases} & {"guided_intent_mismatch", "discovery_stale"}


class TestIntentSentinels:
    def test_consistent_journey_reports_nothing(self) -> None:
        """哨兵绝不能误报：口径一致时一条都不该出。"""

        assert _journey_categories(_journey()) == set()

    def test_swapped_destination_is_reported(self) -> None:
        """拿着成都的攻略去排重庆 —— 正是要拦住的那种事。"""

        journey = _journey(
            planned_intent={
                "destination": "重庆",
                "start_date": "2026-10-01",
                "days": 5,
                "travelers": 2,
            }
        )
        assert "guided_intent_mismatch" in _journey_categories(journey)

    def test_reusing_another_trips_prefetch_is_high_severity(self) -> None:
        journey = _journey(
            planned_intent={
                "destination": "重庆",
                "start_date": "2026-10-01",
                "days": 5,
                "travelers": 2,
            },
            prefetch_reused_any=True,
            prefetch_reused={"transport": True, "hotels": True, "social": True, "places": True},
        )
        cases = {case["category"]: case for case in detect_badcases(BadCaseContext(run_id="tp-s", journey=journey))}
        assert "discovery_stale" in cases
        assert cases["discovery_stale"]["severity"] == "high"

    def test_prefetch_used_for_the_same_trip_is_not_stale(self) -> None:
        """复用了、但复用对了，不该报。"""

        journey = _journey(prefetch_reused_any=True, prefetch_reused={"transport": True})
        assert "discovery_stale" not in _journey_categories(journey)

    def test_date_and_number_shapes_do_not_false_positive(self) -> None:
        """两侧口径不同（date vs 字符串、2 vs "2"）不能算不一致。"""

        journey = _journey(
            confirmed_basic_intent={
                "destination": "成都",
                "start_date": dt.date(2026, 10, 1),
                "days": "5",
                "travelers": 2,
            }
        )
        assert "guided_intent_mismatch" not in _journey_categories(journey)

    def test_missing_basic_intent_is_not_treated_as_mismatch(self) -> None:
        """quick 模式或旧数据没有这块信息时，不能凭空报一条。"""

        journey = _journey(confirmed_basic_intent={}, planned_intent={})
        assert _journey_categories(journey) == set()

    def test_quick_run_is_never_judged(self) -> None:
        """一句话规划没有前置选择，拿它报"意图不一致"是误报。"""

        journey = _journey(source="quick", planned_intent={"destination": "重庆"})
        assert _journey_categories(journey) == set()
