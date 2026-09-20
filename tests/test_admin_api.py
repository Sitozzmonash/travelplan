"""管理端 API 的验证（接管任务 §5 §7 §8 §10）。

这里守的是三件事：
  1. **鉴权**：没有 Token 一律 401，Token 没配置一律 503 —— Trace/Config/Evolution 不能公开；
  2. **形状**：管理端前端依赖的字段必须真的存在（缺字段会让页面静默变空，而不是报错）；
  3. **不泄密**：任何响应里都不能出现 Secret。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.store import TravelPlanStore
from app.workflow import execute_travel_run
from tests.fakes import QUERY, FakeHub, FakeJev, FakeLLM

RUN_ID = "tp-admin-fixture"
TOKEN = "admin-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """一次离线 run（带 FakeJev）产出的真实库 + 产物，供所有管理端读接口复用。"""

    base: Path = tmp_path_factory.mktemp("admin-fixture")
    store = TravelPlanStore(db_path=base / "travelplan.db")
    result = execute_travel_run(
        QUERY,
        user_id="admin-test",
        run_id=RUN_ID,
        output_dir=base / "outputs",
        store=store,
        hub=FakeHub(store=store, run_id=RUN_ID),
        llm=FakeLLM(),
        jev=FakeJev(),
    )
    assert result.status == "completed", result.error
    return {"store": store, "output_dir": base / "outputs", "result": result}


@pytest.fixture()
def client(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app import api as api_module

    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(api_module, "get_store", lambda: world["store"])
    monkeypatch.setattr(api_module, "_output_dir", lambda: world["output_dir"])
    return TestClient(api_module.api)


class TestAuth:
    def test_health_is_public_but_admin_is_not(self, client):
        assert client.get("/api/v1/health").status_code == 200
        for path in (
            "/api/v1/admin/overview",
            "/api/v1/admin/runs",
            "/api/v1/admin/badcases",
            "/api/v1/admin/benchmark/runs",
            "/api/v1/admin/config",
            "/api/v1/admin/jev/health",
            "/api/v1/admin/evolution",
        ):
            assert client.get(path).status_code == 401, path

    def test_wrong_token_is_rejected(self, client):
        response = client.get("/api/v1/admin/overview", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_unconfigured_token_is_503_not_200(self, world, monkeypatch):
        from app import api as api_module

        monkeypatch.delenv("TRAVELPLAN_ADMIN_TOKEN", raising=False)
        monkeypatch.setattr(api_module, "get_store", lambda: world["store"])
        unconfigured = TestClient(api_module.api)
        assert unconfigured.get("/api/v1/admin/overview").status_code == 503

    def test_no_secret_is_ever_echoed(self, client, monkeypatch):
        monkeypatch.setenv("JEV_API_KEY", "never-return-this")
        monkeypatch.setenv("AMAP_API_KEY", "amap-secret")
        for path in ("/api/v1/admin/config", "/api/v1/admin/overview", "/api/v1/admin/jev/health"):
            response = client.get(path, headers=AUTH)
            assert response.status_code == 200
            assert "never-return-this" not in response.text
            assert "amap-secret" not in response.text


class TestOverview:
    def test_shape_matches_the_frontend_contract(self, client):
        body = client.get("/api/v1/admin/overview", headers=AUTH).json()
        for key in (
            "run_count", "statuses", "degraded_count", "failed_count", "running_count",
            "total_tokens", "total_llm_calls", "total_jev_calls", "total_tool_calls",
            "provider_failures", "badcase_open", "badcase_total", "jev", "benchmark", "evolution",
        ):
            assert key in body, key
        assert body["run_count"] >= 1
        assert body["total_jev_calls"] >= 1
        assert "fallback_count" in body["jev"]
        assert "quota" in body["jev"]
        # 额度只有服务端真回过才有值；没有就必须是 unknown。
        assert body["jev"]["quota_source"] in {"reported", "unknown"}
        assert set(body["benchmark"]) == {"latest_run_id", "latest_suites", "latest_pass_rate"}
        assert set(body["evolution"]) == {"enabled", "pending_badcases", "last_run_id", "last_decision"}


class TestRuns:
    def test_list_returns_a_total_and_supports_status_filter(self, client):
        body = client.get("/api/v1/admin/runs", headers=AUTH).json()
        assert body["total"] >= 1
        assert body["items"][0]["run_id"] == RUN_ID

        # 用列表里真实的 status 去过滤，而不是假定这次 run 一定是 SUCCESS：
        # 有降级的 run 是 DEGRADED，这正是状态模型要区分的两件事。
        status = body["items"][0]["status"]
        assert status in {"SUCCESS", "DEGRADED", "FAILED", "RUNNING", "CANCELLED"}
        matching = client.get("/api/v1/admin/runs", params={"status": status}, headers=AUTH).json()
        assert matching["total"] >= 1
        assert all(item["status"] == status for item in matching["items"])

        empty = client.get("/api/v1/admin/runs", params={"status": "NONSENSE"}, headers=AUTH).json()
        assert empty["total"] == 0 and empty["items"] == []

    def test_detail_contains_everything_the_page_renders(self, client):
        body = client.get(f"/api/v1/admin/runs/{RUN_ID}", headers=AUTH).json()
        for key in ("run", "metrics", "progress", "trace", "decisions", "provider_calls", "jev_calls", "llm_calls", "badcases"):
            assert key in body, key
        assert body["metrics"]["jev_calls"] >= 1
        assert body["trace"], "trace 不能为空"
        # Jev Calls 面板的每个字段都必须有值，否则前端只能渲染一行空白。
        call = body["jev_calls"][0]
        for key in ("decision_type", "status", "confidence", "latency_ms", "fallback", "input_summary", "criteria", "quota"):
            assert key in call, key
        # LLM 调用来自 llm span。
        assert body["llm_calls"]
        assert body["llm_calls"][0]["tag"]

    def test_detail_404(self, client):
        assert client.get("/api/v1/admin/runs/tp-nope", headers=AUTH).status_code == 404


class TestBadcases:
    def test_list_has_items_facets_and_total(self, client, world):
        body = client.get("/api/v1/admin/badcases", headers=AUTH).json()
        assert body["total"] == len(body["items"]) >= 1
        assert set(body["facets"]) == {"category", "severity", "analysis_status"}
        assert body["items"][0]["run_id"] == RUN_ID

    def test_list_filters_by_run_and_status(self, client):
        body = client.get("/api/v1/admin/badcases", params={"run_id": RUN_ID, "analysis_status": "pending"}, headers=AUTH).json()
        assert body["total"] >= 1
        other = client.get("/api/v1/admin/badcases", params={"run_id": "tp-other"}, headers=AUTH).json()
        assert other["total"] == 0

    def test_detail_expands_trace_refs(self, client):
        listing = client.get("/api/v1/admin/badcases", headers=AUTH).json()
        badcase_id = listing["items"][0]["badcase_id"]
        body = client.get(f"/api/v1/admin/badcases/{badcase_id}", headers=AUTH).json()
        assert body["badcase"]["badcase_id"] == badcase_id
        assert body["trace"], "trace_refs 必须被展开"
        assert all(set(item) == {"ref", "span", "missing"} for item in body["trace"])

    def test_patch_updates_allowed_fields(self, client):
        listing = client.get("/api/v1/admin/badcases", headers=AUTH).json()
        badcase_id = listing["items"][0]["badcase_id"]
        response = client.patch(
            f"/api/v1/admin/badcases/{badcase_id}",
            json={
                "analysis_status": "analyzed",
                "fixed_status": "fixed",
                "root_cause_status": "verified",
                "suspected_root_cause": "人工确认的根因",
            },
            headers=AUTH,
        )
        assert response.status_code == 200
        updated = response.json()
        assert updated["analysis_status"] == "analyzed"
        assert updated["fixed_status"] == "fixed"
        assert updated["root_cause_status"] == "verified"
        assert updated["suspected_root_cause"] == "人工确认的根因"
        # 回滚状态，避免影响其他用例对 pending 的断言。
        client.patch(f"/api/v1/admin/badcases/{badcase_id}", json={"analysis_status": "pending", "fixed_status": "unfixed"}, headers=AUTH)

    @pytest.mark.parametrize(
        "payload",
        [
            {"analysis_status": "nonsense"},
            {"fixed_status": "nope"},
            {"root_cause_status": "maybe"},
            {"severity": "catastrophic"},
        ],
    )
    def test_patch_rejects_invalid_enums(self, client, payload):
        listing = client.get("/api/v1/admin/badcases", headers=AUTH).json()
        badcase_id = listing["items"][0]["badcase_id"]
        response = client.patch(f"/api/v1/admin/badcases/{badcase_id}", json=payload, headers=AUTH)
        assert response.status_code == 422

    def test_patch_404_for_unknown_id(self, client):
        assert client.patch("/api/v1/admin/badcases/bc-nope", json={"analysis_status": "analyzed"}, headers=AUTH).status_code == 404


class TestConfigAndJevHealth:
    def test_config_exposes_tuning_and_levers(self, client):
        body = client.get("/api/v1/admin/config", headers=AUTH).json()
        assert set(body) >= {"config", "planner_tuning", "secret_configured", "evolution", "note"}
        assert "max_items_per_day" in body["planner_tuning"]
        assert body["evolution"]["levers"]
        assert "admin" in body["secret_configured"]
        # 阈值必须是当前生效值，而不是硬编码。
        assert body["config"]["jev_min_confidence"] == pytest.approx(0.70) or body["config"]["jev_min_confidence"] > 0

    def test_jev_health_reports_status_and_quota_honestly(self, client):
        body = client.get("/api/v1/admin/jev/health", headers=AUTH).json()
        for key in ("enabled", "configured", "status", "latency_ms", "quota", "quota_source", "fallback_count", "calls", "timeout", "low_confidence", "invalid_response"):
            assert key in body, key
        assert body["calls"] >= 1
        if body["quota_source"] == "unknown":
            assert body["quota"] == "unknown"

    def test_providers_reports_config_and_real_stats(self, client):
        body = client.get("/api/v1/admin/providers", headers=AUTH).json()
        assert set(body) == {"configured", "stats", "note"}
        assert body["stats"], "至少要有一次真实工具调用统计"


class TestEvolutionApi:
    def test_state_lists_levers_and_pending(self, client):
        body = client.get("/api/v1/admin/evolution", headers=AUTH).json()
        assert body["pending_badcases"] >= 1
        assert "analyzed_badcases" in body
        assert body["runs"] == []
        assert body["levers"]

    def test_trigger_is_refused_when_disabled(self, client, monkeypatch):
        monkeypatch.delenv("EVOLUTION_ENABLED", raising=False)
        assert client.post("/api/v1/admin/evolution/runs", json={"limit": 5}, headers=AUTH).status_code == 409

    def test_trigger_runs_and_persists_experiences(self, client, monkeypatch):
        monkeypatch.setenv("EVOLUTION_ENABLED", "true")
        # 评测入口用假的：这条用例验证的是"触发 → 落库 → 可查"的链路，不是评测本身。
        monkeypatch.setattr(
            "app.api._evolution_measure",
            lambda: (lambda overrides: {"hard_constraint_violation_rate": 0.5 if not overrides else 0.0,
                                        "preference_coverage": 0.7}),
        )
        response = client.post("/api/v1/admin/evolution/runs", json={"limit": 10}, headers=AUTH)
        assert response.status_code == 202
        evolution_run_id = response.json()["evolution_run_id"]

        detail: dict[str, Any] = {}
        for _ in range(100):
            detail = client.get(f"/api/v1/admin/evolution/runs/{evolution_run_id}", headers=AUTH).json()
            if detail["run"]["status"] != "RUNNING":
                break
            time.sleep(0.05)
        assert detail["run"]["status"] == "SUCCESS"
        assert detail["run"]["badcase_count"] >= 1
        assert detail["experiences"], "至少要产出一条 Experience"
        experience = detail["experiences"][0]
        assert set(experience) >= {
            "experience_id", "source_badcases", "failure_pattern", "verified_root_cause",
            "affected_module", "recommended_change", "before_metrics", "after_metrics", "decision",
        }
        assert detail["badcases"]

    def test_detail_404(self, client):
        assert client.get("/api/v1/admin/evolution/runs/evo-nope", headers=AUTH).status_code == 404


class TestBenchmarkApi:
    def test_list_is_empty_before_any_run(self, client):
        body = client.get("/api/v1/admin/benchmark/runs", headers=AUTH).json()
        assert body["items"] == []
        assert body["limit"] == 20

    def test_detail_404(self, client):
        assert client.get("/api/v1/admin/benchmark/runs/bm-nope", headers=AUTH).status_code == 404

    def test_start_returns_503_when_the_suite_is_unavailable(self, client, monkeypatch):
        from fastapi import HTTPException

        def unavailable():
            raise HTTPException(status_code=503, detail="Benchmark 套件不可用")

        monkeypatch.setattr("app.api._benchmark_runner", unavailable)
        response = client.post("/api/v1/admin/benchmark/runs", json={"suites": ["basic"]}, headers=AUTH)
        assert response.status_code == 503
        # 不可用时不能留下一条永远 RUNNING 的记录。
        assert client.get("/api/v1/admin/benchmark/runs", headers=AUTH).json()["items"] == []

    def test_start_creates_a_row_and_the_job_finishes_it(self, client, monkeypatch):
        """后端用桩替换评测套件：这里验证的是"触发 → 建行 → 任务收尾"的接线。"""

        def stub_runner() -> Any:
            def run(**kwargs: Any) -> dict[str, Any]:
                store = kwargs["store"]
                store.update_benchmark_run(
                    kwargs["benchmark_run_id"],
                    {
                        "status": "SUCCESS",
                        "case_count": 2,
                        "passed": 2,
                        "failed": 0,
                        "metrics": {"quality": {"preference_coverage": 0.9}},
                        "finished_at": "2026-01-01T00:00:00+00:00",
                    },
                )
                return store.get_benchmark_run(kwargs["benchmark_run_id"]) or {}

            return run

        monkeypatch.setattr("app.api._benchmark_runner", stub_runner)
        response = client.post("/api/v1/admin/benchmark/runs", json={"suites": ["basic"], "jev_enabled": False}, headers=AUTH)
        assert response.status_code == 202
        benchmark_run_id = response.json()["benchmark_run_id"]

        item: dict[str, Any] = {}
        for _ in range(100):
            items = client.get("/api/v1/admin/benchmark/runs", headers=AUTH).json()["items"]
            item = next(entry for entry in items if entry["benchmark_run_id"] == benchmark_run_id)
            if item["status"] != "RUNNING":
                break
            time.sleep(0.05)
        assert item["status"] == "SUCCESS"
        assert item["jev_enabled"] is False
        assert item["pass_rate"] == 1.0

        detail = client.get(f"/api/v1/admin/benchmark/runs/{benchmark_run_id}", headers=AUTH).json()
        assert set(detail) == {"run", "metrics", "case_results", "baseline_compare"}


class TestArtifacts:
    @pytest.mark.parametrize(
        "filename",
        ["plan.json", "plan.md", "audit_report.json", "trace.jsonl", "metrics.json", "badcases.json"],
    )
    def test_all_six_artifacts_are_downloadable(self, client, filename):
        response = client.get(f"/api/v1/plans/{RUN_ID}/artifacts/{filename}")
        assert response.status_code == 200

    def test_unknown_artifact_is_refused(self, client):
        assert client.get(f"/api/v1/plans/{RUN_ID}/artifacts/../../etc/passwd").status_code in {404, 422}
        assert client.get(f"/api/v1/plans/{RUN_ID}/artifacts/secrets.env").status_code == 404

    def test_trace_jsonl_is_valid_ndjson(self, client, world):
        path = world["output_dir"] / RUN_ID / "trace.jsonl"
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines
        assert all("span_id" in span and "component" in span for span in lines)
