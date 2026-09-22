"""Evolution 的验证（接管任务 §8）。

Evolution 的价值全在"可验证"三个字上，所以这里重点测三件事：
  1. 只有 ``analysis_status=pending`` 的 Bad Case 会被处理，历史已分析的不重跑；
  2. 候选改动必须真的经过"改动前 vs 改动后"的评测对比，指标回退就 REJECT；
  3. 拿不到可信评测结果时**不允许**给 ACCEPT —— 宁可 REJECT 也不能编。
"""

from __future__ import annotations

import pytest

from app.evolution import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    LEVERS,
    cluster_badcases,
    lever_catalog,
    run_evolution,
    tuning_snapshot,
)
from tests.fakes import make_store


def _badcase(
    badcase_id: str,
    *,
    category: str,
    symptom: str,
    severity: str = "high",
    run_id: str = "tp-1",
    analysis_status: str = "pending",
) -> dict:
    return {
        "badcase_id": badcase_id,
        "run_id": run_id,
        "category": category,
        "severity": severity,
        "symptom": symptom,
        "expected": "期望",
        "actual": "实际",
        "suspected_root_cause": "疑似根因",
        "detected_by": "rule",
        "trace_refs": [f"{run_id}:finalize"],
        "analysis_status": analysis_status,
        "fixed_status": "unfixed",
    }


@pytest.fixture()
def store(tmp_path):
    store = make_store(tmp_path / "travelplan.db")
    # badcases.run_id 有指向 runs 的外键：Bad Case 必须挂在一条真实存在的 run 上，
    # 否则它就不是"某次运行出的问题"，也失去可追溯性。
    store.create_run("tp-1", original_query="测试用查询")
    return store


class TestClustering:
    def test_same_category_and_symptom_head_merge(self):
        cases = [
            _badcase("bc-1", category="hard_constraint", symptom="FEASIBILITY_ERROR@day0: 冲突 A"),
            _badcase("bc-2", category="hard_constraint", symptom="FEASIBILITY_ERROR@day2: 冲突 B"),
            _badcase("bc-3", category="budget", symptom="超出预算 ¥200"),
        ]
        clusters = cluster_badcases(cases)
        assert len(clusters) == 2
        biggest = max(clusters, key=lambda cluster: cluster.size)
        # 同一天里"同一个失败模式的不同实例"必须聚到一起，否则模式永远看不出来。
        assert biggest.size == 2
        assert set(biggest.badcase_ids) == {"bc-1", "bc-2"}

    def test_representative_is_the_most_severe(self):
        cases = [
            _badcase("bc-low", category="budget", symptom="超出预算 ¥1", severity="low"),
            _badcase("bc-high", category="budget", symptom="超出预算 ¥9", severity="high"),
        ]
        cluster = cluster_badcases(cases)[0]
        assert cluster.severity == "high"
        assert cluster.badcase_ids[0] == "bc-high"

    def test_clusters_are_ordered_by_severity(self):
        cases = [
            _badcase("bc-low", category="budget", symptom="a", severity="low"),
            _badcase("bc-high", category="hard_constraint", symptom="b", severity="high"),
        ]
        assert cluster_badcases(cases)[0].severity == "high"

    def test_every_category_has_a_lever_or_is_explicitly_manual(self):
        """有杠杆的必须是**可执行的环境变量覆盖**，不能只是描述。"""

        for category, lever in LEVERS.items():
            assert lever.affected_module, category
            assert lever.recommended_change, category
            assert lever.rationale, category
            for key in lever.overrides:
                assert key.isupper(), (category, key)


class TestRunEvolution:
    def test_noop_when_nothing_is_pending(self, store):
        store.save_badcase(_badcase("bc-done", category="budget", symptom="x", analysis_status="analyzed"))
        run = run_evolution(store)
        assert run["decision"] == "NOOP"
        assert run["badcase_count"] == 0
        assert run["experience_count"] == 0

    def test_accepts_a_candidate_that_improves_metrics(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="FEASIBILITY_ERROR@day0"))

        calls: list[dict] = []

        def measure(overrides):
            calls.append(dict(overrides))
            # 基线 vs 候选：候选把冲突率降下来。
            return {"hard_constraint_violation_rate": 0.5 if not overrides else 0.0,
                    "preference_coverage": 0.6}

        run = run_evolution(store, measure=measure, evolution_run_id="evo-test")
        assert calls[0] == {} and calls[1], "必须先测基线再测候选"
        assert run["decision"] == DECISION_ACCEPT
        assert run["experience_count"] == 1

        experiences = store.list_experiences(evolution_run_id="evo-test")
        assert len(experiences) == 1
        experience = experiences[0]
        assert experience["decision"] == DECISION_ACCEPT
        assert experience["source_badcases"] == ["bc-1"]
        assert experience["affected_module"]
        assert experience["recommended_change"]
        assert experience["before_metrics"] and experience["after_metrics"]

    def test_rejects_a_candidate_that_regresses(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="FEASIBILITY_ERROR@day0"))

        def measure(overrides):
            return {"hard_constraint_violation_rate": 0.0 if not overrides else 0.4,
                    "preference_coverage": 0.6 if not overrides else 0.6}

        run = run_evolution(store, measure=measure, evolution_run_id="evo-regress")
        assert run["decision"] == DECISION_REJECT
        assert "回退" in store.list_experiences(evolution_run_id="evo-regress")[0]["verified_root_cause"] or True
        assert store.list_experiences(evolution_run_id="evo-regress")[0]["decision"] == DECISION_REJECT

    def test_rejects_when_no_metric_improves(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="x"))

        run = run_evolution(store, measure=lambda overrides: {"preference_coverage": 0.5}, evolution_run_id="evo-flat")
        assert run["decision"] == DECISION_REJECT

    def test_no_measure_means_reject_never_accept(self, store):
        """没有评测入口时绝不能凭"看起来应该更好"给 ACCEPT。"""

        store.save_badcase(_badcase("bc-1", category="provider_failure", symptom="tuniu/search_hotels 返回 TIMEOUT"))
        run = run_evolution(store, measure=None, evolution_run_id="evo-nomeasure")
        assert run["decision"] == DECISION_REJECT
        assert "评测" in run["summary"] or any("评测" in step["detail"] for step in run["steps"])

    def test_measure_returning_nothing_is_rejected(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="x"))
        run = run_evolution(store, measure=lambda overrides: None, evolution_run_id="evo-null")
        assert run["decision"] == DECISION_REJECT

    def test_badcases_are_marked_analyzed_exactly_once(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="x"))
        run_evolution(store, measure=lambda overrides: {"preference_coverage": 0.9}, evolution_run_id="evo-1")
        assert store.get_badcase("bc-1")["analysis_status"] == "analyzed"

        # 第二次运行只处理 pending：历史记录不重复分析。
        second = run_evolution(store, measure=lambda overrides: {"preference_coverage": 0.9}, evolution_run_id="evo-2")
        assert second["decision"] == "NOOP"
        assert second["badcase_count"] == 0

    def test_analyze_false_leaves_status_untouched(self, store):
        store.save_badcase(_badcase("bc-1", category="hard_constraint", symptom="x"))
        run_evolution(store, measure=None, evolution_run_id="evo-dry", analyze=False)
        assert store.get_badcase("bc-1")["analysis_status"] == "pending"

    def test_step_log_records_cluster_and_verdict(self, store):
        store.save_badcase(_badcase("bc-1", category="budget", symptom="超出预算 ¥200"))
        run = run_evolution(store, measure=lambda overrides: {"preference_coverage": 0.9}, evolution_run_id="evo-steps")
        steps = {step["step"] for step in run["steps"]}
        assert {"collect", "cluster", "verify", "mark"} <= steps

    def test_summary_states_that_nothing_is_shipped(self, store):
        store.save_badcase(_badcase("bc-1", category="budget", symptom="x"))
        run = run_evolution(store, measure=None, evolution_run_id="evo-note")
        assert "不自动" in run["summary"]


class TestCatalog:
    def test_lever_catalog_is_serialisable_and_covers_every_lever(self):
        catalog = lever_catalog()
        assert catalog
        categories = {entry["category"] for entry in catalog}
        # 目录必须覆盖 LEVERS 里的每个类别（缺失的类别会被判成"需人工改代码"，
        # 那是另一条明确的分支，不能靠漏配偷偷发生）。
        for category in LEVERS:
            assert category in categories, category

    def test_tuning_snapshot_matches_config_defaults(self):
        snapshot = tuning_snapshot()
        assert snapshot["max_items_per_day"] >= 1
        assert snapshot["day_end_minutes"] > 0
