"""Benchmark 套件自身的测试（接管任务 §9、§14）。

评测套件如果不被测，它自己就会悄悄坏掉：case 少了一行、指标名拼错、跑完不落库 ——
这些都不会报错，只会让"分数"变得毫无意义。所以这里守四件事：
个数、格式、指标名集合、端到端落库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.store import TravelPlanStore
from benchmark.metrics import (
    ALL_METRICS,
    METRIC_GROUPS,
    RunFacts,
    compute_metrics,
    group,
    jev_comparison,
    quality_proxy,
)
from benchmark.runner import (
    BENCHMARK_VERSION,
    DETERMINISTIC_SUITES,
    LIVE_SUITE,
    SUITES,
    load_cases,
    make_measure,
    run_benchmark,
)

#: 每个套件的用例数（接管任务 §9 规定：12 / 6 / 6 / 4 / 4 + 5 Live Smoke）。
EXPECTED_CASE_COUNTS = {
    "basic": 12,
    "hard": 6,
    "badcase_regression": 6,
    "provider_failure": 4,
    "jev_decision": 5,
    "live_smoke": 5,
}

REQUIRED_CASE_KEYS = ("case_id", "suite", "title", "query")
KNOWN_FIXTURE_KEYS = {"failing", "empty", "llm_unavailable", "poi_spread", "jev_script"}
TOP_LEVEL_CASE_KEYS = {
    "case_id",
    "suite",
    "title",
    "query",
    "fixtures",
    "expect",
    "live",
    "requires_jev",
}

KNOWN_EXPECT_KEYS = {
    "hard_constraints_ok",
    "min_items",
    "expect_badcase_categories",
    "expect_no_badcase_categories",
    "expect_jev_choice",
    "expect_jev_fallback",
    "expect_replan",
    "allow_failed",
}


class TestCaseFiles:
    @pytest.mark.parametrize("suite", SUITES)
    def test_case_count_matches_the_spec(self, suite: str) -> None:
        assert len(load_cases(suite)) == EXPECTED_CASE_COUNTS[suite]

    @pytest.mark.parametrize("suite", SUITES)
    def test_every_case_has_required_fields_and_known_keys(self, suite: str) -> None:
        for case in load_cases(suite):
            for key in REQUIRED_CASE_KEYS:
                assert case.get(key), f"{case.get('case_id')} 缺少 {key}"
            assert case["suite"] == suite, f"{case['case_id']} 的 suite 与文件不一致"
            unknown_fixtures = set(case.get("fixtures") or {}) - KNOWN_FIXTURE_KEYS
            assert not unknown_fixtures, f"{case['case_id']} 有未知 fixture：{unknown_fixtures}"
            unknown_expect = set(case.get("expect") or {}) - KNOWN_EXPECT_KEYS
            assert not unknown_expect, f"{case['case_id']} 有未知 expect：{unknown_expect}"
            unknown_top = set(case) - TOP_LEVEL_CASE_KEYS
            assert not unknown_top, f"{case['case_id']} 有未知顶层字段：{unknown_top}"

    def test_case_ids_are_unique_across_all_suites(self) -> None:
        ids = [case["case_id"] for suite in SUITES for case in load_cases(suite)]
        assert len(ids) == len(set(ids))

    def test_queries_are_china_domestic_only(self) -> None:
        """产品范围只做国内旅行：用例里不能出现境外城市。"""

        forbidden = ("东京", "大阪", "京都", "曼谷", "巴黎", "苏黎世", "新加坡", "首尔", "伦敦", "纽约")
        for suite in SUITES:
            for case in load_cases(suite):
                for city in forbidden:
                    assert city not in case["query"], f"{case['case_id']} 出现境外城市 {city}"

    def test_live_cases_are_isolated_in_the_live_suite(self) -> None:
        for suite in DETERMINISTIC_SUITES:
            for case in load_cases(suite):
                assert not case.get("live"), f"{case['case_id']} 在离线套件里标了 live"
        assert load_cases(LIVE_SUITE), "Live Smoke 套件不能为空"

    def test_jev_decision_cases_need_the_poi_spread_fixture_or_are_single_candidate(self) -> None:
        """软决策用例必须真的能触发决策：要么把点拉开产生多候选，要么明确只测兜底。"""

        for case in load_cases("jev_decision"):
            script = (case.get("fixtures") or {}).get("jev_script") or {}
            if "plan_choice" in script:
                # 方案选择只在存在多份候选时才会发给 Jev，所以这些用例必须把点拉开。
                assert (case.get("fixtures") or {}).get("poi_spread"), case["case_id"]


class TestMetrics:
    def test_metric_name_set_is_exactly_the_spec(self) -> None:
        expected = {
            "date_mismatch_rate", "hard_time_conflict_rate", "opening_hours_violation_rate",
            "missing_transport_disclosure_rate", "missing_hotel_disclosure_rate",
            "budget_math_error_rate", "hallucinated_price_rate", "hallucinated_route_rate",
            "missing_source_rate", "hard_constraint_violation_rate",
            "category_diversity", "consecutive_same_type_count", "backtracking_score",
            "daily_load_balance", "meal_time_quality", "pace_match", "preference_coverage",
            "first_day_quality", "last_day_quality", "route_efficiency",
            "evidence_coverage", "multi_source_ratio", "poi_verified_ratio",
            "unsupported_recommendation_rate",
            "provider_success_rate", "fallback_success_rate", "timeout_rate",
            "end_to_end_latency", "llm_latency", "jev_latency", "provider_latency",
            "llm_calls", "jev_calls", "tool_calls",
            "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
            "jev_success_rate", "jev_fallback_rate", "jev_timeout_rate",
            "jev_low_confidence_rate", "jev_plan_accept_rate", "jev_replan_rate",
            "quality_delta_with_jev", "latency_delta_with_jev",
        }
        assert set(ALL_METRICS) == expected
        # 六个维度必须把全部指标都覆盖到，不多不少。
        assert sorted(metric for names in METRIC_GROUPS.values() for metric in names) == sorted(ALL_METRICS)

    def test_empty_facts_do_not_crash_and_stay_none(self) -> None:
        metrics = compute_metrics(RunFacts(plan=None))
        assert set(metrics) == set(ALL_METRICS)
        # 没有 plan 时不能编造 0：那是"无法测量"，不是"零违规"。
        assert metrics["hard_constraint_violation_rate"] is None
        assert metrics["provider_success_rate"] is None

    def test_group_covers_six_dimensions(self) -> None:
        groups = group(compute_metrics(RunFacts(plan=None)))
        assert set(groups) == set(METRIC_GROUPS)

    def test_quality_proxy_averages_the_two_user_facing_metrics(self) -> None:
        assert quality_proxy({"preference_coverage": 0.5, "pace_match": 1.0}) == 0.75
        assert quality_proxy({"preference_coverage": None, "pace_match": None}) is None

    def test_jev_comparison_reports_both_deltas(self) -> None:
        off = {"preference_coverage": 0.5, "pace_match": 0.5, "end_to_end_latency": 1000}
        on = {"preference_coverage": 0.7, "pace_match": 0.7, "end_to_end_latency": 1300}
        comparison = jev_comparison(off, on)
        assert comparison["quality_delta_with_jev"] == pytest.approx(0.2)
        assert comparison["latency_delta_with_jev"] == pytest.approx(300)


class TestRunnerEndToEnd:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> TravelPlanStore:
        return TravelPlanStore(db_path=tmp_path / "bench.db")

    @pytest.fixture()
    def results_dir(self, tmp_path: Path) -> Path:
        """结果写到临时目录：测试不该往仓库的 benchmark/results/ 里丢文件。"""

        return tmp_path / "results"

    @pytest.fixture()
    def baselines_dir(self, tmp_path: Path) -> Path:
        """基线也写临时目录。

        必须这么做：`baselines/` 里是**提交进仓库的对照基线**，测试跑一次 limit=2 的
        冒烟就把它们覆盖掉，后来的人会拿着一份"2 个用例"的基线当真（真实发生过）。
        """

        return tmp_path / "baselines"

    def test_run_benchmark_persists_rows_and_finishes(
        self, store: TravelPlanStore, results_dir: Path
    ) -> None:
        run = run_benchmark(
            store=store,
            suites=["basic"],
            limit=2,
            jev_enabled=True,
            write_baseline=False,
            results_dir=results_dir,
        )
        assert run["status"] == "SUCCESS"
        assert run["case_count"] == 2
        assert run["passed"] + run["failed"] == 2
        assert run["version"] == BENCHMARK_VERSION
        assert run["jev_enabled"] is True

        results = store.get_benchmark_case_results(run["benchmark_run_id"])
        assert len(results) == 2
        for result in results:
            assert result["status"] in {"pass", "fail"}
            assert set(result["metrics"]) == set(ALL_METRICS)
            assert result["detail"]["run_id"]

        # 逐 case 的 run 也必须落在库里（可追溯：从 case 能回到 run）。
        for result in results:
            assert store.get_run(result["detail"]["run_id"]) is not None

    def test_baseline_is_written_and_comparison_appears(
        self, store: TravelPlanStore, results_dir: Path, baselines_dir: Path
    ) -> None:
        off = run_benchmark(
            store=store, suites=["hard"], limit=2, jev_enabled=False, write_baseline=True,
            results_dir=results_dir, baselines_dir=baselines_dir,
        )
        on = run_benchmark(
            store=store, suites=["hard"], limit=2, jev_enabled=True, write_baseline=True,
            results_dir=results_dir, baselines_dir=baselines_dir,
        )
        assert (baselines_dir / "jev_off.json").is_file()
        assert (baselines_dir / "jev_on.json").is_file()

        # 两份基线都存在之后，后续运行要给出 Jev OFF vs ON 的差值。
        third = run_benchmark(
            store=store, suites=["hard"], limit=1, jev_enabled=True, write_baseline=False,
            results_dir=results_dir, baselines_dir=baselines_dir,
        )
        comparison = (third.get("metrics") or {}).get("baseline_compare")
        assert comparison is not None
        assert "quality_delta_with_jev" in comparison
        assert off["benchmark_run_id"] != on["benchmark_run_id"]

    def test_unknown_suite_is_rejected(self, store: TravelPlanStore) -> None:
        with pytest.raises(ValueError):
            run_benchmark(store=store, suites=["nope"], write_baseline=False)

    def test_make_measure_returns_a_flat_metric_dict(self, store: TravelPlanStore) -> None:
        measure = make_measure(store, suites=["hard"], limit=1)
        metrics = measure({})
        assert metrics is not None
        assert set(metrics) <= set(ALL_METRICS)
        assert metrics, "至少要有一些可测量的指标"

        # 候选改动也应该能测（这里给一个无副作用的覆盖值）。
        tweaked = measure({"TP_MAX_ITEMS_PER_DAY": "2"})
        assert tweaked is not None

    def test_live_smoke_is_excluded_from_deterministic_metrics(
        self, store: TravelPlanStore, results_dir: Path
    ) -> None:
        """Live 用例不参与确定性指标 —— 它的波动来自第三方，不是代码。"""

        run = run_benchmark(
            store=store, suites=["basic"], limit=1, write_baseline=False, results_dir=results_dir
        )
        payload = run.get("metrics") or {}
        assert "live_smoke" in payload
        assert payload["live_smoke"]["case_count"] == 0
        assert run["case_count"] == 1  # 只算确定性用例
