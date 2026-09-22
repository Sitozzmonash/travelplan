"""Benchmark 套件自己的验证：**它必须能判对，也必须能判错**。

三条硬要求（少了任何一条，这套基准就会退化成"永远绿的装饰"）：

1. 12 条内置用例能离线跑通，逐例判分与预期一致；
2. 判卷器对**坏行为**必须判 fail（REJECT 侵入 / 编造价格 / 未解决时间冲突）——
   这就是"不要造永远绿的测试"的落点；
3. 管理端的 benchmark 接口真的接上了这套基准（不是 try/except 降级成 503）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from app.store import TravelPlanStore

STORE: TravelPlanStore | None = None


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """跑**一次**完整套件（12 例，约 12 秒），所有断言复用这一份结果。

    每个用例都单独跑一遍会让这个文件变成分钟级；共用一份结果不影响断言强度 ——
    断言的是每个 case 自己记录的判分与事实。
    """

    from benchmark.runner import run_benchmark

    base: Path = tmp_path_factory.mktemp("benchmark")
    store = TravelPlanStore(db_path=base / "benchmark.db")
    store.init_schema()
    run = run_benchmark(store=store, results_dir=base / "results", write_baseline=False)
    return {
        "run": run,
        "store": store,
        "results_dir": base / "results",
        "cases": {row["case_id"]: row for row in store.get_benchmark_case_results(run["benchmark_run_id"])},
    }


def _case(bench: dict[str, Any], case_id: str) -> dict[str, Any]:
    row = bench["cases"].get(case_id)
    assert row is not None, f"没有跑到用例 {case_id}（实际：{sorted(bench['cases'])}）"
    return row


# ======================================================================
# 1. 套件本身：离线、跑通、用例齐全
# ======================================================================


def test_all_suites_run_offline_and_pass(bench):
    run = bench["run"]
    assert run["status"] == "SUCCESS"
    assert run["case_count"] == 12
    assert run["passed"] == 12, [
        (row["case_id"], row["status"]) for row in bench["cases"].values()
    ]
    assert run["failed"] == 0
    assert run["model"] == "scripted-offline"
    # 本路径没有 Jev：这个字段必须如实为 False，不能沿用旧套件的"Jev ON"。
    assert run["jev_enabled"] is False
    assert run["live"] is False


def test_every_case_file_loads_and_declares_expectations():
    from benchmark.runner import CASES_DIR, DETERMINISTIC_SUITES, load_cases

    total = 0
    for suite in DETERMINISTIC_SUITES:
        cases = load_cases(suite)
        assert cases, f"{suite} 一个用例都没有"
        for case in cases:
            assert case["suite"] == suite
            assert case["expect"], f"{case['case_id']} 没有 expect（无从判定）"
            assert (CASES_DIR / f"{suite}.jsonl").is_file()
        total += len(cases)
    assert total == 12


def test_grouped_metrics_cover_the_five_dimensions(bench):
    from benchmark.metrics import METRIC_GROUPS

    groups = bench["run"]["metrics"]["groups"]
    assert set(groups) == set(METRIC_GROUPS), f"多/少了维度：{sorted(groups)}"
    # 本路径没有 Jev，因此**不该**有 jev 维度；缺失的维度由管理端显式标出。
    assert "jev" not in groups
    for name, rows in groups.items():
        assert rows, f"{name} 维度是空的"
        for value in rows.values():
            assert value is None or isinstance(value, (int, float))


def test_results_are_written_for_offline_replay(bench):
    import json

    target = bench["results_dir"] / bench["run"]["benchmark_run_id"]
    summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
    assert summary["case_count"] == 12
    assert summary["passed"] == 12
    lines = [line for line in (target / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 12


def test_baseline_file_is_a_real_baseline():
    """仓库里那份基线必须是本路径的、且指标齐全（否则回归对比无从比较）。"""

    from benchmark.metrics import ALL_METRICS
    from benchmark.runner import read_baseline

    baseline = read_baseline()
    assert baseline is not None, "benchmark/baselines/agent_loop.json 不存在"
    assert baseline["scope"] == "agent_loop"
    assert baseline["case_count"] == 12
    assert "jev" not in (baseline.get("suites") or [])
    for name in ALL_METRICS:
        assert name in baseline["metrics"], name


#: case 允许出现的字段。**故意写成白名单**：把 `expect_time_conflict_cod` 拼错成别的名字，
#: 判卷器不会报错、只会不再检查 —— 那条用例会静默变弱，而"静默变弱"正是评测套件最危险的坏法。
CASE_KEYS = {
    "case_id",
    "suite",
    "title",
    "query",
    "fixtures",
    "intent",
    "tool_calls",
    "submit",
    "tail",
    "script",
    "repeat_last_step",
    "expect",
}
FIXTURE_KEYS = {"failing", "empty", "env", "usage", "delay_seconds"}
EXPECT_KEYS = {
    "expect_status",
    "expect_no_plan",
    "min_days",
    "min_items",
    "require_grounded_numbers",
    "no_reject_intrusion",
    "max_unresolved_time_conflicts",
    "expect_time_conflict_code",
    "expect_budget_within",
    "expect_transport_absent",
    "expect_hotel_absent",
    "expect_inbound_transport_no",
    "expect_place_ids",
    "expect_missing_artifacts",
    "expect_degradation_contains",
    "forbid_degradations",
    "expect_injected",
    "expect_failed_calls",
    "min_provider_calls",
}


def test_case_ids_are_unique_and_declared_keys_are_reviewed():
    from benchmark.runner import DETERMINISTIC_SUITES, load_cases

    seen: set[str] = set()
    for suite in DETERMINISTIC_SUITES:
        for case in load_cases(suite):
            assert case["case_id"] not in seen, f"case_id 重复：{case['case_id']}"
            seen.add(case["case_id"])
            assert set(case) <= CASE_KEYS, f"{case['case_id']} 有未登记的字段：{set(case) - CASE_KEYS}"
            assert set(case.get("fixtures") or {}) <= FIXTURE_KEYS
            assert set(case["expect"]) <= EXPECT_KEYS, (
                f"{case['case_id']} 有未登记的 expect 键：{set(case['expect']) - EXPECT_KEYS}"
                "（拼错的键不会报错，只会让这条用例静默变弱）"
            )
    assert len(seen) == 12


def test_unknown_suite_is_rejected(tmp_path: Path):
    """套件名写错时必须报错，而不是"跑了 0 例、通过率 100%"。"""

    from benchmark.runner import run_benchmark

    store = TravelPlanStore(db_path=tmp_path / "unknown.db")
    store.init_schema()
    with pytest.raises(ValueError):
        run_benchmark(store=store, suites=["basic", "nope"])
    with pytest.raises(ValueError):
        # 只勾退役套件：本路径没有 Jev、也不联网，必须显式失败
        run_benchmark(store=store, suites=["jev_decision", "live_smoke"])


# ======================================================================
# 2. 逐例判分口径
# ======================================================================


def test_normal_case_numbers_are_grounded_and_no_degradation(bench):
    row = _case(bench, "basic-bj-cd-3d")
    assert row["status"] == "pass"
    detail = row["detail"]
    assert detail["run_status"] == "completed"
    assert detail["degradations"] == []
    assert detail["grounding"]["unsupported"] == []
    assert detail["grounding"]["checked"] >= 8
    assert detail["grounding"]["rate"] == 1.0
    assert detail["plan_days"] == 3


def test_budget_near_limit_stays_within(bench):
    row = _case(bench, "basic-budget-near-limit")
    assert row["status"] == "pass"
    assert row["metrics"]["budget_overflow_rate"] == 0.0
    assert row["detail"]["run_status"] == "completed"


def test_reject_list_is_honoured(bench):
    row = _case(bench, "regression-rejected-poi-not-in-plan")
    assert row["status"] == "pass"
    detail = row["detail"]
    assert set(detail["reject_ids"]) == {"B0FF2", "B0FF5"}
    assert detail["reject_intruders"] == []
    assert row["metrics"]["reject_intrusion_rate"] == 0.0
    # MUST 的 B0FF1 必须真的进了行程（否则"REJECT 没进"可能只是"什么都没排"）。
    assert "B0FF1" in detail["plan_place_ids"]


def test_time_conflict_is_detected_and_revised(bench):
    row = _case(bench, "hard-time-conflict-revised")
    assert row["status"] == "pass"
    detail = row["detail"]
    assert detail["conflicts_detected"] >= 1, "这个用例的排法就是排不下的，必须被发现"
    assert detail["conflicts_resolved"] == detail["conflicts_detected"]
    assert detail["unresolved_conflicts"] == 0
    assert row["metrics"]["unresolved_time_conflict_rate"] == 0.0
    assert row["metrics"]["time_conflict_resolved_rate"] == 1.0


def test_truncation_keeps_the_plan_and_says_so(bench):
    row = _case(bench, "hard-truncated-keeps-plan")
    assert row["status"] == "pass"
    detail = row["detail"]
    assert detail["run_status"] == "completed"
    assert detail["plan_days"] >= 1, "已经交卷的行程不能因为循环被截断而丢掉"
    assert any("最大步数" in note for note in detail["degradations"])
    # 降级要进 run_progress，管理端才看得到 DEGRADED
    assert detail["progress_status"] == "DEGRADED"


def test_empty_tools_mean_failed_without_fabrication(bench):
    row = _case(bench, "hard-empty-tools-no-fabrication")
    assert row["status"] == "pass", row.get("detail")
    detail = row["detail"]
    assert detail["run_status"] == "failed"
    assert detail["plan_days"] == 0
    # 没有 plan 就不许有 plan 制品（伪造成品比失败更糟）
    assert "plan.json" not in detail["artifacts"]
    assert "plan.md" not in detail["artifacts"]
    # 注入真的生效了（否则这条用例是在空转）
    assert set(detail["injected"]) == {"search_hotels", "search_poi"}


def test_empty_results_are_visible_in_the_call_ledger(bench):
    """"查了但返回空"必须在账本里留下痕迹，不能看起来和"查到了"一样。"""

    row = _case(bench, "provider-empty-hotels")
    assert row["status"] == "pass"
    statuses = {call["tool"]: call["status"] for call in row["detail"]["provider_calls"]}
    assert statuses["search_hotels"] == "EMPTY"
    assert row["metrics"]["provider_failure_calls"] >= 1


# ======================================================================
# 3. 判卷器自检：坏行为必须判 fail（"不要永远绿"）
# ======================================================================


def _hostile_case(**overrides: Any) -> dict[str, Any]:
    """一份"故意做错"的剧本，三个错各自独立、都可被代码证实：

    1. 把用户 REJECT 的 B0FF2 排进行程；
    2. 给一个假世界里从来没有过的价格（9999）；
    3. 带着一条**未解决**的 error 级时间问题交卷（行程里没有它的说明）。

    为什么第 3 条用"自己报的 error warning"而不是构造一个排不下的时间：`_apply_time_pass`
    会把 TRANSFER_TIME_SHORTFALL / RETURN_DEPARTURE_CONFLICT 都修掉（平移或移除），
    构造不出"改完还剩"的冲突 —— 那正说明那条兜底是有效的。而"Agent 自己承认排不下"
    是真实存在的一种交卷形态，它必须被判 fail。
    """

    from benchmark.fixtures.world import minimal_plan

    plan = minimal_plan(days=2)
    plan["days"][0]["items"] = [
        {
            "name": "武侯祠",
            "type": "attraction",
            "place_id": "B0FF2",  # 用户 REJECT 的点
            "start_time": "14:30",
            "end_time": "16:00",
            "price": 9999,  # 假世界里从来没有这个价格
            "price_type": "realtime",
            "reason": "编的",
        }
    ]
    plan["days"][1]["items"] = [
        {
            "name": "宽窄巷子",
            "type": "attraction",
            "place_id": "B0FF1",
            "start_time": "10:00",
            "end_time": "12:00",
            "price": 0,
            "price_type": "realtime",
            "reason": "正常排法",
        }
    ]
    plan["warnings"] = [
        {
            "day_index": 0,
            "severity": "error",
            "code": "SCHEDULE_INFEASIBLE",
            "reason": "用户想去的点与返程时间冲突，本次没能排下",
            "resolved": False,
        }
    ]
    case: dict[str, Any] = {
        "case_id": "self-test-hostile",
        "suite": "self_test",
        "title": "判卷器自检：这份行程必须判 fail",
        "query": "10月1日从北京去成都玩2天",
        "intent": {
            "destination": ["成都"],
            "origin": "北京",
            "start_date": "2026-10-01",
            "days": 2,
            "budget_total": 6000,
            "place_selections": {"B0FF2": "REJECT"},
        },
        "tool_calls": [
            ["search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}],
            ["search_trains", {"origin": "成都东", "destination": "北京西", "depart_date": "2026-10-02"}],
            ["search_hotels", {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-03"}],
            ["search_poi", {"keywords": "宽窄巷子 武侯祠", "region": "成都"}],
        ],
        "submit": plan,
        "expect": {
            "expect_status": "completed",
            "no_reject_intrusion": True,
            "require_grounded_numbers": True,
            "max_unresolved_time_conflicts": 0,
        },
    }
    case.update(overrides)
    return case


def test_judge_fails_a_hostile_plan(tmp_path: Path):
    from benchmark.runner import run_case

    store = TravelPlanStore(db_path=tmp_path / "hostile.db")
    store.init_schema()
    result = run_case(_hostile_case(), store=store, output_dir=tmp_path / "out")

    assert result["status"] == "fail", "判卷器把一份明显有问题的行程判成通过了"
    failures = " | ".join(result["failures"])
    assert "no_reject_intrusion" in failures
    assert "grounded_numbers" in failures and "9999" in failures
    assert "unresolved_time_conflicts" in failures
    # 三个问题各自都要有具体说明，不能只给一个笼统的 fail
    assert len(result["failures"]) >= 3


def test_judge_flags_numbers_that_were_never_returned(tmp_path: Path):
    """只改一处：把一个真实价格换成编造的价格 —— 判分必须立刻变红。"""

    from benchmark.fixtures.world import minimal_plan
    from benchmark.runner import run_case

    store = TravelPlanStore(db_path=tmp_path / "invented.db")
    store.init_schema()
    plan = minimal_plan(days=1)
    plan["days"][0]["items"][0]["price"] = 8888
    poisoned = plan["days"][0]["items"][0]["price"]
    assert poisoned == 8888
    case = _hostile_case(submit=plan, intent={}, expect={"expect_status": "completed",
                                                        "require_grounded_numbers": True})
    result = run_case(case, store=store, output_dir=tmp_path / "out2")
    assert result["status"] == "fail"
    assert any("8888" in failure for failure in result["failures"]), result["failures"]


def test_metrics_never_invent_a_zero_for_unmeasurable_cases(tmp_path: Path):
    """拿不到 plan 的用例：指标写 None（"没有可测对象"），不写 0 冒充"确实是零"。"""

    from benchmark.runner import run_case

    store = TravelPlanStore(db_path=tmp_path / "none.db")
    store.init_schema()
    case = {
        "case_id": "self-test-no-plan",
        "suite": "self_test",
        "title": "没有 plan 时不许把指标写成 0",
        "query": "随便问问",
        "script": [{"text": "我还没有排好行程。"}],
        "expect": {"expect_status": "failed", "expect_no_plan": True},
    }
    result = run_case(case, store=store, output_dir=tmp_path / "out3")
    assert result["status"] == "pass"  # 如实失败 = 符合预期
    assert result["metrics"]["grounded_number_rate"] is None
    assert result["metrics"]["avg_items_per_day"] is None
    assert result["metrics"]["days_nonempty_rate"] is None


# ======================================================================
# 4. Evolution 接口
# ======================================================================


def test_make_measure_returns_flat_numbers():
    from benchmark.runner import make_measure

    measure = make_measure(suites=("provider_failure",), limit=1)
    metrics = measure({})
    assert metrics, "measure 没有返回任何指标"
    assert all(isinstance(value, float) for value in metrics.values()), metrics
    # 指标名必须与 Evolution 的方向表有交集，否则对照实验无从判定
    from app.evolution import METRIC_DIRECTIONS

    assert set(metrics) & set(METRIC_DIRECTIONS), sorted(metrics)


# ======================================================================
# 5. 管理端接口真的接上了这套基准
# ======================================================================


def test_benchmark_runner_is_really_importable():
    """接口层不再是"延迟导入失败 → 503"：`benchmark.runner` 真的能导进来。"""

    from app import api as api_module

    runner = api_module._benchmark_runner()
    assert callable(runner)
    assert runner.__module__ == "benchmark.runner"

    from benchmark.runner import BASELINES_DIR

    assert BASELINES_DIR.is_dir()


def test_admin_can_trigger_a_real_benchmark_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """POST → 后台真的跑完 → 列表/详情能读到结果（不是一条永远 RUNNING 的记录）。"""

    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", "benchmark-test-token")
    store = TravelPlanStore(db_path=tmp_path / "admin.db")
    store.init_schema()
    monkeypatch.setattr(api_module, "get_store", lambda: store)
    # 结果别写进仓库（评测产物不该污染工作树）
    monkeypatch.setattr("benchmark.runner.RESULTS_DIR", tmp_path / "results")
    client = TestClient(api_module.api)
    auth = {"Authorization": "Bearer benchmark-test-token"}

    response = client.post(
        "/api/v1/admin/benchmark/runs", json={"suites": ["basic"], "limit": 1}, headers=auth
    )
    assert response.status_code == 202, response.text
    benchmark_run_id = response.json()["benchmark_run_id"]

    item: dict[str, Any] = {}
    for _ in range(200):
        items = client.get("/api/v1/admin/benchmark/runs", headers=auth).json()["items"]
        item = next(entry for entry in items if entry["benchmark_run_id"] == benchmark_run_id)
        if item["status"] != "RUNNING":
            break
        time.sleep(0.1)
    assert item["status"] == "SUCCESS", item
    assert item["case_count"] == 1
    assert item["passed"] == 1
    assert item["pass_rate"] == 1.0

    detail = client.get(f"/api/v1/admin/benchmark/runs/{benchmark_run_id}", headers=auth).json()
    assert set(detail) == {"run", "metrics", "case_results", "baseline_compare"}
    # 管理端要按维度画图：五个维度都得在（jev 维度本路径不存在，缺失由页面显式标出）
    assert set(detail["metrics"]) == {
        "hard_constraints",
        "plan_quality",
        "evidence",
        "provider",
        "performance",
    }
    assert detail["case_results"] and detail["case_results"][0]["suite"] == "basic"


def test_admin_rejects_a_run_with_only_retired_suites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """只勾退役套件（jev_decision / live_smoke）时：如实 FAILED，不产出"0 例 100% 通过"。"""

    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", "benchmark-test-token")
    store = TravelPlanStore(db_path=tmp_path / "admin2.db")
    store.init_schema()
    monkeypatch.setattr(api_module, "get_store", lambda: store)
    client = TestClient(api_module.api)
    auth = {"Authorization": "Bearer benchmark-test-token"}

    response = client.post(
        "/api/v1/admin/benchmark/runs", json={"suites": ["jev_decision"]}, headers=auth
    )
    assert response.status_code == 202
    benchmark_run_id = response.json()["benchmark_run_id"]
    item: dict[str, Any] = {}
    for _ in range(100):
        items = client.get("/api/v1/admin/benchmark/runs", headers=auth).json()["items"]
        item = next(entry for entry in items if entry["benchmark_run_id"] == benchmark_run_id)
        if item["status"] != "RUNNING":
            break
        time.sleep(0.05)
    assert item["status"] == "FAILED"
    assert "退役" in (item["notes"] or "")
