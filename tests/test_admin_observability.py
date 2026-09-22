"""管理端增量任务的验证：Planning Sessions + Provider Health（文档 §19）。

守两件事：
  1. **Session 能回答"用户在正式规划之前卡在哪"**（分阶段 Discovery 状态、事件时间线、→ Run）；
  2. **Provider Health 用真实调用数据聚合**，且"没有调用历史"必须显示 UNKNOWN 而不是失败。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import sessions
from app.store import TravelPlanStore
from tests.fakes import make_store

TOKEN = "admin-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def store(tmp_path) -> TravelPlanStore:
    return make_store(tmp_path / "t.db")


@pytest.fixture()
def client(store: TravelPlanStore, monkeypatch) -> TestClient:
    from app import api as api_module

    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(api_module, "get_store", lambda: store)
    monkeypatch.setattr(api_module._RUN_EXECUTOR, "submit", lambda fn, *a: None)
    return TestClient(api_module.api)


def _seed_session(store: TravelPlanStore, *, destination: str = "成都", with_run: bool = False) -> str:
    session = sessions.create_session(
        store,
        {"origin": "北京", "destination": destination, "start_date": "2026-10-01", "days": 5, "travelers": 2},
    )
    session_id = session["session_id"]
    session.update(
        {
            "status": sessions.SESSION_READY,
            "discovery_status": sessions.DISCOVERY_PARTIAL,
            "discovery": {
                "transport": {"status": "OK", "result_count": 6, "duration_ms": 900, "degraded": False, "error": None},
                "hotels": {"status": "OK", "result_count": 12, "duration_ms": 1200, "degraded": False, "error": None},
                "social": {"status": "EMPTY", "result_count": 0, "duration_ms": 300, "degraded": True, "error": None},
                "places": {"status": "OK", "result_count": 15, "duration_ms": 4000, "degraded": False, "error": None},
            },
            "place_candidates": [
                {"place_id": "p1", "name": "熊猫基地", "category": "family", "category_label": "亲子"},
                {"place_id": "p2", "name": "宽窄巷子", "category": "attraction", "category_label": "热门景点"},
            ],
            "poi_selections": {"p1": "MUST", "p2": "WANT"},
            "prefetch": {"outbound": [{"train_no": "G89"}], "inbound": [], "hotels": [{"hotel_id": "h1"}],
                         "evidences": [], "places": [{"place_id": "p1"}], "provider_calls": [{"provider": "tuniu"}]},
            "events": [
                {"at": "2026-09-20T10:00:00+00:00", "event": "transport_prefetch_finished", "detail": "status=OK"},
                {"at": "2026-09-20T10:00:10+00:00", "event": "discovery_finished", "detail": "地点 15 个"},
            ],
        }
    )
    if with_run:
        run_id = "tp-guided-1"
        store.create_run(run_id, original_query="北京→成都", source="guided", source_session_id=session_id)
        store.finish_run(run_id, "completed")
        session["run_id"] = run_id
        session["status"] = sessions.SESSION_STARTING
    store.save_planning_session(session)
    return session_id


class TestAdminPlanningSessions:
    def test_list_requires_auth(self, client):
        assert client.get("/api/v1/admin/planning-sessions").status_code == 401
        assert client.get("/api/v1/admin/planning-sessions", headers=AUTH).status_code == 200

    def test_list_fields_match_the_console_contract(self, client, store):
        _seed_session(store)
        body = client.get("/api/v1/admin/planning-sessions", headers=AUTH).json()
        assert body["total"] == 1
        row = body["items"][0]
        for key in (
            "session_id", "status", "discovery_status", "origin", "destination", "start_date", "days",
            "travelers", "must_count", "want_count", "reject_count", "place_count", "run_id",
            "created_at", "updated_at", "expires_at",
        ):
            assert key in row, key
        assert row["destination"] == "成都"
        assert row["must_count"] == 1 and row["want_count"] == 1

    @pytest.mark.parametrize(
        ("query", "expected"),
        [("?has_run=false", 1), ("?has_run=true", 0), ("?destination=成都", 1), ("?destination=重庆", 0),
         ("?status=READY", 1), ("?status=CANCELLED", 0)],
    )
    def test_list_filters(self, client, store, query, expected):
        _seed_session(store)
        body = client.get(f"/api/v1/admin/planning-sessions{query}", headers=AUTH).json()
        assert body["total"] == expected

    def test_detail_returns_the_documented_envelope(self, client, store):
        session_id = _seed_session(store)
        body = client.get(f"/api/v1/admin/planning-sessions/{session_id}", headers=AUTH).json()
        for key in ("session", "basic_info", "preferences", "discovery", "prefetch_summary",
                    "poi_selections", "events", "run_link"):
            assert key in body, key
        # 4.3 分阶段 Discovery 状态：每阶段都要有 status/耗时/结果数/是否降级
        assert set(body["discovery"]["stages"]) == {"transport", "hotels", "social", "places"}
        social = body["discovery"]["stages"]["social"]
        assert social["status"] == "EMPTY" and social["degraded"] is True
        assert body["discovery"]["overall"] == "PARTIAL"
        # 4.4 摘要
        assert body["prefetch_summary"]["transport_candidates"] == 1
        assert body["prefetch_summary"]["hotel_candidates"] == 1
        # 4.5 用户选择（数量 + 名称）
        assert body["poi_selections"]["counts"]["must"] == 1
        assert [item["name"] for item in body["poi_selections"]["items"]] == ["熊猫基地", "宽窄巷子"]
        # 5 事件时间线
        assert any(event["event"] == "discovery_finished" for event in body["events"])

    def test_detail_404(self, client):
        assert client.get("/api/v1/admin/planning-sessions/ps-nope", headers=AUTH).status_code == 404

    def test_session_to_run_link(self, client, store):
        session_id = _seed_session(store, with_run=True)
        body = client.get(f"/api/v1/admin/planning-sessions/{session_id}", headers=AUTH).json()
        assert body["run_link"]["has_run"] is True
        assert body["run_link"]["run_id"] == "tp-guided-1"
        assert body["run_link"]["run_status"] == "SUCCESS"
        # Run 侧也能反向追溯回 Session
        detail = client.get("/api/v1/admin/runs/tp-guided-1", headers=AUTH).json()
        assert detail["run"]["source"] == "guided"
        assert detail["run"]["source_session_id"] == session_id


class TestRunTimelineLlmPreview:
    """Run Trace 的 ASSISTANT 事件要能回答"模型收到了什么 / 回了什么 / 花了多少 token"。

    两条路径都必须诚实：老 span 没有预览字段 → 说"这条 span 没写预览"，而不是笼统宣称
    "从来没持久化过"；新 span 有就照实给出，且 token 取不到时留 None（不是 0）。
    """

    @staticmethod
    def _run_with_llm_span(store: TravelPlanStore, run_id: str, attributes: dict) -> None:
        store.create_run(run_id, original_query="北京→成都")
        store.finish_run(run_id, "completed")
        store.save_trace_span(
            run_id,
            f"{run_id}:llm:final_answer",
            component="llm",
            name="final_answer",
            status="SUCCESS",
            started_at="2026-09-20T10:00:00+00:00",
            finished_at="2026-09-20T10:00:03+00:00",
            attributes=attributes,
        )

    def test_legacy_span_says_the_preview_was_not_recorded(self, client, store):
        self._run_with_llm_span(
            store,
            "tp-legacy",
            {"tag": "final_answer", "model": "fake", "status": "OK", "duration_ms": 3000, "chars": 12},
        )
        body = client.get("/api/v1/admin/runs/tp-legacy/timeline", headers=AUTH).json()
        event = next(item for item in body["events"] if item["event_type"] == "ASSISTANT")

        assert event["input_preview"] is None and event["output_preview"] is None
        assert event["tokens_in"] is None and event["tokens_out"] is None
        # 降级必须在 payload 里看得见，而不是静默留空。
        assert any("没有 Prompt 预览" in note for note in event["notes"])
        assert any("没有 token 用量" in note for note in event["notes"])
        assert set(event["metadata"]) >= {
            "system_preview", "user_preview", "context_preview", "assistant_preview",
            "prompt_version", "prompt_hash",
            "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
        }
        assert event["metadata"]["total_tokens"] is None
        # 顶层说明不能再宣称"正文从未被持久化"。
        assert any("早期版本记录的 run 的 span 里没有预览字段" in note for note in body["notes"])

    def test_span_with_previews_and_usage_is_surfaced(self, client, store):
        self._run_with_llm_span(
            store,
            "tp-preview",
            {
                "tag": "extract_places",
                "model": "kimi",
                "status": "OK",
                "duration_ms": 1200,
                "chars": 9,
                "system_preview": "SYS",
                "user_preview": "USER",
                "context_preview": "CTX",
                "assistant_preview": "ASSISTANT 正文",
                "prompt_version": "deadbee",
                "prompt_hash": "abc123abc123",
                "input_tokens": 12,
                "output_tokens": 34,
                "cached_tokens": 0,
                "total_tokens": 46,
            },
        )
        body = client.get("/api/v1/admin/runs/tp-preview/timeline", headers=AUTH).json()
        event = next(item for item in body["events"] if item["event_type"] == "ASSISTANT")

        assert event["input_preview"] == "[system]\nSYS\n\n[user]\nUSER\n\n[context]\nCTX"
        assert event["output_preview"] == "ASSISTANT 正文"
        assert (event["tokens_in"], event["tokens_out"]) == (12, 34)
        assert event["metadata"]["total_tokens"] == 46
        # 0 是模型真的回了 cached=0，与"没回这个字段"（None）不是一回事。
        assert event["metadata"]["cached_tokens"] == 0
        assert event["metadata"]["prompt_version"] == "deadbee"
        assert event["metadata"]["prompt_hash"] == "abc123abc123"
        assert event["notes"] == []
        assert "46 tokens" in event["summary"]

    def test_previews_are_scrubbed_on_the_way_out(self, client, store, monkeypatch):
        """span 可能是别的进程/旧版本写进来的：出口再过一次脱敏，明文 Key 不许出现。"""
        monkeypatch.setenv("AMAP_API_KEY", "amap-live-123456")
        self._run_with_llm_span(
            store,
            "tp-raw",
            {
                "tag": "critic",
                "status": "OK",
                "system_preview": "key=amap-live-123456",
                "user_preview": "api_key: amap-live-123456",
                "assistant_preview": "ok",
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
            },
        )
        response = client.get("/api/v1/admin/runs/tp-raw/timeline", headers=AUTH)
        assert response.status_code == 200
        assert "amap-live-123456" not in response.text


class TestProviderHealth:
    def test_no_history_is_unknown_not_failure(self, client, store):
        body = client.get("/api/v1/admin/providers", headers=AUTH).json()
        assert body["items"], "配置了但没调用过的 Provider 也要出现"
        assert all(row["status"] == "UNKNOWN" for row in body["items"])
        assert body["summary"]["unknown"] == len(body["items"])
        assert body["summary"]["needs_attention"] == []

    def test_aggregation_from_real_calls(self, client, store):
        store.save_provider_calls(
            [
                {"call_id": "c1", "provider": "tikhub", "tool": "xiaohongshu", "status": "OK",
                 "source_type": "discovery", "session_id": "ps-1", "fetched_at": "2026-09-20T10:00:00+00:00",
                 "duration_ms": 900, "returned": 5},
                {"call_id": "c2", "provider": "tikhub", "tool": "xiaohongshu", "status": "OK",
                 "source_type": "run", "run_id": "tp-1", "fetched_at": "2026-09-20T10:01:00+00:00",
                 "duration_ms": 1100, "returned": 3},
                {"call_id": "c3", "provider": "tikhub", "tool": "douyin", "status": "TIMEOUT",
                 "source_type": "discovery", "session_id": "ps-1", "fetched_at": "2026-09-20T10:02:00+00:00",
                 "duration_ms": 1500, "error": "timeout"},
                {"call_id": "c4", "provider": "tikhub", "tool": "douyin", "status": "TIMEOUT",
                 "source_type": "discovery", "session_id": "ps-2", "fetched_at": "2026-09-20T10:03:00+00:00",
                 "duration_ms": 1500, "error": "timeout"},
            ]
        )
        row = next(
            item for item in client.get("/api/v1/admin/providers", headers=AUTH).json()["items"]
            if item["provider"] == "tikhub"
        )
        assert row["calls"] == 4
        assert row["successes"] == 2 and row["failures"] == 2
        assert row["timeouts"] == 2
        assert row["success_rate"] == 0.5
        assert row["avg_latency_ms"] == 1250
        assert row["last_failure_at"] == "2026-09-20T10:03:00+00:00"
        # 健康状态：失败率 50% → DEGRADED（阈值 20% / 60%）
        assert row["status"] == "DEGRADED"
        # 来源区分（文档 §11）：Discovery 与正式 Run 都要能看出来
        assert set(row["sources"]) == {"discovery", "run"}

    def test_fallback_count_is_reported(self, client, store):
        store.save_provider_calls(
            [
                {"call_id": "f1", "provider": "amap", "tool": "route", "status": "UNAVAILABLE",
                 "source_type": "run", "run_id": "tp-1", "fetched_at": "2026-09-20T10:00:00+00:00",
                 "duration_ms": 50, "fallback": True},
                {"call_id": "f2", "provider": "amap", "tool": "route", "status": "OK",
                 "source_type": "run", "run_id": "tp-1", "fetched_at": "2026-09-20T10:01:00+00:00",
                 "duration_ms": 60},
            ]
        )
        row = next(
            item for item in client.get("/api/v1/admin/providers", headers=AUTH).json()["items"]
            if item["provider"] == "amap"
        )
        assert row["fallback_count"] == 1

    def test_detail_lists_recent_calls_without_secrets(self, client, store):
        store.save_provider_calls(
            [
                {"call_id": "d1", "provider": "tuniu", "tool": "hotel", "status": "OK",
                 "source_type": "discovery", "session_id": "ps-1", "fetched_at": "2026-09-20T10:00:00+00:00",
                 "duration_ms": 800, "returned": 12,
                 "query": {"city": "成都", "api_key": "should-not-appear"}},
            ]
        )
        body = client.get("/api/v1/admin/providers/tuniu", headers=AUTH).json()
        assert body["provider"] == "tuniu"
        assert body["calls"] and body["calls"][0]["tool"] == "hotel"
        assert "should-not-appear" not in client.get("/api/v1/admin/providers/tuniu", headers=AUTH).text
        assert client.get("/api/v1/admin/providers/tuniu").status_code == 401
