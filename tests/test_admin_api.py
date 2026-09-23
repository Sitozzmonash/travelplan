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

from app.config import current_config
from app.store import TravelPlanStore
from tests.agent_fixture import run_agent
from tests.fakes import FakeHub, make_store

RUN_ID = "tp-admin-fixture"
TOKEN = "admin-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """一次离线 Agent run 产出的真实库 + 产物，供所有管理端读接口复用。

    走主路径 `execute_agent_run`（脚本模型 + 假 Hub，离线）；管理端读的就是这条路径
    真的写出来的 trace / metrics / 产物。
    """

    base: Path = tmp_path_factory.mktemp("admin-fixture")
    store = TravelPlanStore(db_path=base / "travelplan.db")
    result = run_agent(
        store=store,
        run_id=RUN_ID,
        output_dir=base / "outputs",
        hub=FakeHub(store=store, run_id=RUN_ID, poi_spread=0.25),
        user_id="admin-test",
        tool_calls=(
            ("search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}),
            ("search_hotels", {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-04"}),
        ),
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
        monkeypatch.setenv("AMAP_API_KEY", "amap-secret")
        for path in ("/api/v1/admin/config", "/api/v1/admin/overview"):
            response = client.get(path, headers=AUTH)
            assert response.status_code == 200
            assert "amap-secret" not in response.text


class TestOverview:
    def test_shape_matches_the_frontend_contract(self, client):
        body = client.get("/api/v1/admin/overview", headers=AUTH).json()
        for key in (
            "run_count", "statuses", "degraded_count", "failed_count", "running_count",
            "total_tokens", "total_llm_calls", "total_tool_calls",
            "provider_failures", "badcase_open", "badcase_total", "benchmark", "evolution",
        ):
            assert key in body, key
        assert body["run_count"] >= 1
        # Jev 决策层已删：overview 不再有对应的健康度维度。
        assert "jev" not in body and "total_jev_calls" not in body
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
        for key in ("run", "metrics", "progress", "trace", "decisions", "provider_calls", "llm_calls", "badcases"):
            assert key in body, key
        # Jev 决策层已删：Run Detail 不再返回 Jev 调用明细。
        assert "jev_calls" not in body
        assert body["trace"], "trace 不能为空"
        assert body["provider_calls"], "跑过真实工具的 run 必须有 provider 调用账本"
        # LLM 调用来自 llm span。
        assert body["llm_calls"]
        assert body["llm_calls"][0]["tag"]

    def test_detail_404(self, client):
        assert client.get("/api/v1/admin/runs/tp-nope", headers=AUTH).status_code == 404


@pytest.fixture()
def llm_detail_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """一个只有 llm span 的最小库：验证 Run Detail 里"模型收到了什么"的形状。"""

    from app import api as api_module

    store = make_store(tmp_path / "llm-detail.db")
    store.create_run("tp-llm", original_query="北京→成都")
    store.finish_run("tp-llm", "completed")
    store.save_run_metrics(
        "tp-llm", {"duration_ms": 4000, "input_tokens": 12, "output_tokens": 34, "total_tokens": 46}
    )
    store.update_stage(
        "tp-llm", "finalize", "SUCCESS", started_at="2026-09-20T10:00:00+00:00",
        finished_at="2026-09-20T10:00:04+00:00",
    )
    store.save_trace_span(
        "tp-llm",
        "tp-llm:llm:final_answer",
        component="llm",
        name="final_answer",
        status="SUCCESS",
        started_at="2026-09-20T10:00:00+00:00",
        finished_at="2026-09-20T10:00:01+00:00",
        attributes={"tag": "final_answer", "model": "fake", "status": "OK", "duration_ms": 1000, "chars": 5},
    )
    store.save_trace_span(
        "tp-llm",
        "tp-llm:llm:critic",
        component="llm",
        name="critic",
        status="SUCCESS",
        started_at="2026-09-20T10:00:02+00:00",
        finished_at="2026-09-20T10:00:04+00:00",
        attributes={
            "tag": "critic",
            "model": "kimi",
            "status": "OK",
            "duration_ms": 2000,
            "chars": 9,
            "system_preview": "SYS",
            "user_preview": "USER",
            "assistant_preview": "模型输出",
            "prompt_version": "deadbee",
            "prompt_hash": "h1234567890",
            "input_tokens": 12,
            "output_tokens": 34,
            "cached_tokens": 7,
            "total_tokens": 46,
        },
    )
    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(api_module, "get_store", lambda: store)
    monkeypatch.setattr(api_module, "_output_dir", lambda: tmp_path / "outputs")
    return TestClient(api_module.api)


class TestRunDetailLlmCalls:
    """Run Detail 的 LLM 调用列表要能同时喂 Workflow View 与 Conversation View。"""

    def test_calls_expose_previews_tokens_and_prompt_identity(self, llm_detail_client):
        body = llm_detail_client.get("/api/v1/admin/runs/tp-llm", headers=AUTH).json()
        calls = {call["tag"]: call for call in body["llm_calls"]}

        legacy = calls["final_answer"]
        # 老 run 的 span 没有这些字段：回 None，不回 0 / 空串（否则页面会把"没记"画成"没有"）。
        assert legacy["input_tokens"] is None and legacy["total_tokens"] is None
        assert legacy["system_preview"] is None and legacy["prompt_version"] is None
        assert legacy["finished_at"] == "2026-09-20T10:00:01+00:00"

        rich = calls["critic"]
        assert (rich["system_preview"], rich["user_preview"], rich["assistant_preview"]) == (
            "SYS", "USER", "模型输出",
        )
        assert rich["context_preview"] is None
        assert (rich["input_tokens"], rich["output_tokens"], rich["cached_tokens"], rich["total_tokens"]) == (
            12, 34, 7, 46,
        )
        assert rich["prompt_version"] == "deadbee"
        assert rich["prompt_hash"] == "h1234567890"
        assert rich["finished_at"] == "2026-09-20T10:00:04+00:00"

    def test_stage_token_note_no_longer_claims_usage_is_run_level_only(self, llm_detail_client):
        body = llm_detail_client.get("/api/v1/admin/runs/tp-llm", headers=AUTH).json()
        stage = next(row for row in body["stages"] if row["stage_id"] == "finalize")

        # run 级合计仍然来自 run_metrics（口径没变）……
        assert stage["tokens"]["total_tokens"] == 46
        # ……但说明必须改成事实：逐次用量在 llm_calls 里是有的。
        assert "整次 run 的合计" in stage["tokens"]["note"]
        assert "Provider 不按阶段回报用量" not in stage["tokens"]["note"]

    def test_overview_keeps_the_cumulative_token_counter(self, llm_detail_client):
        body = llm_detail_client.get("/api/v1/admin/overview", headers=AUTH).json()
        assert body["total_tokens"] == 46
        assert body["total_tokens_runs"] == 1
        # 显式关掉时不再计算：这个字段是 opt-in 的（默认开，保证既有契约不破）。
        assert "total_tokens" not in llm_detail_client.get(
            "/api/v1/admin/overview", params={"include_tokens": "false"}, headers=AUTH
        ).json()


@pytest.fixture()
def dashboard_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """计数版的库替身：验证 Dashboard 的第二次请求真的没再打一遍聚合。"""

    from app import admin_analytics
    from app import api as api_module

    store = make_store(tmp_path / "dashboard.db")
    for index, tokens in enumerate((100, 300, None)):
        run_id = f"tp-dash-{index}"
        store.create_run(run_id, original_query="北京→成都")
        store.finish_run(run_id, "completed")
        metrics = {"duration_ms": 1000 + index}
        if tokens is not None:
            metrics["total_tokens"] = tokens
        store.save_run_metrics(run_id, metrics)

    counts = {"list_runs": 0}

    class _CountingStore:
        """只统计 list_runs：Dashboard 的往返大头就是它（翻页扫窗口）。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            value = getattr(self._inner, name)
            if name != "list_runs":
                return value

            def counted(*args, **kwargs):
                counts["list_runs"] += 1
                return value(*args, **kwargs)

            return counted

    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(api_module, "get_store", lambda: _CountingStore(store))
    # 每个用例一份全新的缓存，避免与其它用例共享状态。
    monkeypatch.setattr(admin_analytics, "_DASHBOARDS", admin_analytics._DashboardCache(ttl_seconds=45.0))
    return TestClient(api_module.api), counts


class TestDashboardTokens:
    def test_avg_tokens_per_run_only_counts_runs_with_a_snapshot(self, dashboard_client):
        client, _ = dashboard_client
        body = client.get("/api/v1/admin/dashboard", headers=AUTH).json()

        kpi = next(item for item in body["kpis"] if item["key"] == "avg_tokens_per_run")
        assert kpi["label"] == "平均每个规划的 token"
        assert kpi["unit"] == "count"
        # (100 + 300) / 2：没有快照的那条既不进分子也不进分母，更不当成 0。
        assert kpi["value"] == 200
        assert kpi["higher_is_better"] is False
        assert kpi["hint"] == "2 个运行有 token 快照"
        # 两个窗口指标挨在一起（都是"每个 run"口径）。
        keys = [item["key"] for item in body["kpis"]]
        assert keys.index("avg_tokens_per_run") == keys.index("avg_cost") + 1

        assert body["tokens"] == {
            "avg_per_run": 200,
            "previous_avg_per_run": None,
            "known_runs": 2,
            "window_total": 400,
        }

    def test_without_snapshots_the_kpi_is_none_not_zero(self, tmp_path, monkeypatch):
        from app import admin_analytics
        from app import api as api_module

        store = make_store(tmp_path / "empty.db")
        store.create_run("tp-empty", original_query="北京→成都")
        store.finish_run("tp-empty", "completed")
        store.save_run_metrics("tp-empty", {"duration_ms": 10})

        monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", TOKEN)
        monkeypatch.setattr(api_module, "get_store", lambda: store)
        monkeypatch.setattr(admin_analytics, "_DASHBOARDS", admin_analytics._DashboardCache(ttl_seconds=45.0))

        body = TestClient(api_module.api).get("/api/v1/admin/dashboard", headers=AUTH).json()
        kpi = next(item for item in body["kpis"] if item["key"] == "avg_tokens_per_run")
        assert kpi["value"] is None
        assert "没有带 token 快照" in kpi["hint"]
        assert body["tokens"]["window_total"] is None
        assert body["tokens"]["known_runs"] == 0


class TestDashboardCache:
    def test_second_request_is_served_from_cache_and_says_so(self, dashboard_client):
        client, counts = dashboard_client

        first = client.get("/api/v1/admin/dashboard", headers=AUTH).json()
        assert first["cache"]["hit"] is False
        assert first["cache"]["computed_at"] == first["generated_at"]
        calls_after_first = counts["list_runs"]
        assert calls_after_first >= 1

        second = client.get("/api/v1/admin/dashboard", headers=AUTH).json()
        assert second["cache"]["hit"] is True
        assert counts["list_runs"] == calls_after_first, "缓存没有生效，又打了一遍库"
        # 命中缓存必须自证：哪一刻算的、最多滞后多久。
        assert second["cache"]["computed_at"] == first["cache"]["computed_at"]
        assert second["cache"]["ttl_seconds"] == 45.0
        assert any("缓存结果" in note for note in second["notes"])
        # 业务数字仍然是同一份，不是重新算出一个不同的值。
        assert second["kpis"] == first["kpis"]

    def test_expired_cache_recomputes(self, dashboard_client, monkeypatch):
        from app import admin_analytics

        client, counts = dashboard_client
        # 负 TTL = 永远过期：走的是与自然过期完全相同的那条分支，又不依赖时钟精度
        # （Windows 上 time.monotonic 的粒度约 15ms，TTL=0 可能"还没过期"）。
        monkeypatch.setattr(admin_analytics, "_DASHBOARDS", admin_analytics._DashboardCache(ttl_seconds=-1.0))

        first = client.get("/api/v1/admin/dashboard", headers=AUTH).json()
        calls_after_first = counts["list_runs"]
        second = client.get("/api/v1/admin/dashboard", headers=AUTH).json()

        assert first["cache"]["hit"] is False and second["cache"]["hit"] is False
        assert counts["list_runs"] > calls_after_first


class TestDashboardQualityScore:
    """A15：质量分优先读 run_metrics 列值，缺列值的旧 run 才退回解析 plan_json。"""

    @pytest.fixture(autouse=True)
    def fresh_caches(self, monkeypatch):
        """每个用例一份全新的缓存：避免用例之间共享 dashboard / plan 缓存状态。"""
        from app import admin_analytics

        monkeypatch.setattr(admin_analytics, "_DASHBOARDS", admin_analytics._DashboardCache(ttl_seconds=45.0))
        monkeypatch.setattr(admin_analytics, "_PLANS", admin_analytics._PlanCache())

    def test_column_value_is_used_and_plan_is_not_parsed(self, tmp_path, monkeypatch):
        """有列值时不解析 plan_json：plan_snapshot 一旦被调用就立刻失败，dashboard 仍正常出数。"""
        from app import admin_analytics

        store = make_store(tmp_path / "qs.db")
        for index, score in enumerate((0.6, 0.8, 0.9)):
            run_id = f"tp-qs-{index}"
            store.create_run(run_id, original_query="北京→成都")
            store.finish_run(run_id, "completed")
            store.save_run_metrics(run_id, {"duration_ms": 1000 + index, "quality_score": score})

        def _no_plan_parse(*args, **kwargs):
            raise AssertionError("有列值不该解析 plan_json")

        monkeypatch.setattr(admin_analytics, "plan_snapshot", _no_plan_parse)

        body = admin_analytics.dashboard(store, window=admin_analytics._window("24h"), quality_sample=10)
        kpi = next(item for item in body["kpis"] if item["key"] == "quality_score")
        # 三个 run 的列值 (0.6+0.8+0.9)/3
        assert kpi["value"] == round((0.6 + 0.8 + 0.9) / 3, 4)
        assert body["quality"]["sampled_runs"] == 3
        assert "抽样 3 个运行" in kpi["hint"]

    def test_old_run_without_column_falls_back_to_plan_parse(self, tmp_path, monkeypatch):
        """缺列值的旧 run 仍退回解析 plan_json：plan_snapshot 真的被调用，且用解析结果出数。"""
        from app import admin_analytics
        from app.models import BudgetSummary, TripIntent, TripPlan

        store = make_store(tmp_path / "qs-fallback.db")
        run_id = "tp-qs-old"
        store.create_run(run_id, original_query="北京→成都")
        store.finish_run(run_id, "completed")
        store.save_run_metrics(run_id, {"duration_ms": 1000})  # 不写 quality_score → 列值为 NULL
        # 落一份真实 plan：退回路径要靠它解析出质量分
        plan = TripPlan(
            run_id=run_id,
            query="成都两天",
            intent=TripIntent(destination=["成都"], days=2, travelers=2),
            days=[],
            budget=BudgetSummary(),
        )
        store.save_plan(plan)

        calls: dict[str, int] = {"plan_snapshot": 0}
        real_snapshot = admin_analytics.plan_snapshot

        def _counting_snapshot(store_, run_id_):
            calls["plan_snapshot"] += 1
            return real_snapshot(store_, run_id_)

        monkeypatch.setattr(admin_analytics, "plan_snapshot", _counting_snapshot)

        body = admin_analytics.dashboard(store, window=admin_analytics._window("24h"), quality_sample=10)
        kpi = next(item for item in body["kpis"] if item["key"] == "quality_score")
        assert calls["plan_snapshot"] == 1
        # 分数来自退回解析的 plan，而不是列值（该 run 无列值）
        expected = admin_analytics.planner.quality_score(
            admin_analytics.planner.plan_quality(plan.days, plan.intent).to_dict()
        )
        assert kpi["value"] == expected
        assert body["quality"]["sampled_runs"] == 1


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


class TestConfig:
    def test_config_exposes_tuning_and_levers(self, client):
        body = client.get("/api/v1/admin/config", headers=AUTH).json()
        assert set(body) >= {"config", "planner_tuning", "secret_configured", "evolution", "note"}
        assert "max_items_per_day" in body["planner_tuning"]
        assert body["evolution"]["levers"]
        assert "admin" in body["secret_configured"]
        # Jev 决策层已删：既不回显它的配置项，也不再有它的密钥探针/可编辑键。
        assert not any(key.startswith("jev") for key in body["config"])
        assert not any(key.startswith("JEV_") for key in body["editable_keys"])
        assert "jev" not in body["secret_configured"]
        # 配置必须是**当前生效值**，而不是硬编码。
        assert body["config"]["badcase_enabled"] == current_config().badcase_enabled
        assert any(key.startswith("TP_") for key in body["editable_keys"])

    def test_jev_health_endpoint_is_gone(self, client):
        """Jev 决策层已整体删除：专用健康度端点不应再存在。"""

        assert client.get("/api/v1/admin/jev/health", headers=AUTH).status_code == 404

    def test_providers_reports_config_and_real_stats(self, client):
        body = client.get("/api/v1/admin/providers", headers=AUTH).json()
        assert set(body) == {"items", "summary", "note"}
        assert body["items"], "至少要有一次真实工具调用统计"
        # 没有调用历史的 Provider 必须是 UNKNOWN，不能显示成故障
        assert all(
            row["status"] in {"HEALTHY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"}
            for row in body["items"]
        )
        assert "needs_attention" in body["summary"]
        # 不能回显任何密钥
        assert "sk-" not in client.get("/api/v1/admin/providers", headers=AUTH).text


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
        response = client.post("/api/v1/admin/benchmark/runs", json={"suites": ["basic"]}, headers=AUTH)
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
        assert item["live"] is False
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
