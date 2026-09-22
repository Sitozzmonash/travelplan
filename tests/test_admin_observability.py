"""管理端增量任务的验证：Planning Sessions + Provider Health（文档 §19）。

守两件事：
  1. **Session 能回答"用户在正式规划之前卡在哪"**（分阶段 Discovery 状态、事件时间线、→ Run）；
  2. **Provider Health 用真实调用数据聚合**，且"没有调用历史"必须显示 UNKNOWN 而不是失败。
"""

from __future__ import annotations

import json

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


class TestAgentRunTimeline:
    """Agent Loop 出计划的 run：admin 要能看到每一步在做什么、每步 token、总 token。

    这一路的事实形状与固定 12 步流程不同：
      * `run_stages.stage_id` 是**中文展示名**（「在查酒店」），同名工具的第 N 次调用共享
        同一行（后写覆盖先写），逐次事实只在 `trace_spans` 上；
      * 工具 span 带 `stage_key`（`tool:search_hotels:2`）/ `seq`，但**不带** `source_id`；
      * 模型 span 带 input / output / cached / total 与 `cumulative_*`。

    所以这里守三件事：步骤挂回中文步骤名、每步 token 与整 run 汇总都能读出、
    agent 元信息（交卷方式 / prompt 版本 / truncation）按来源如实给出。
    """

    RUN = "tp-agent-1"

    @staticmethod
    def _tool(
        store: TravelPlanStore,
        run_id: str,
        *,
        tool: str,
        display: str,
        seq: int,
        started: str,
        finished: str,
        status: str = "SUCCESS",
        duration_ms: int = 1200,
        query: dict | None = None,
    ) -> None:
        """一次工具调用：run_stages 一行（中文展示名）+ trace 一条 tool span。"""

        facts = {
            "tool": tool,
            "status": status,
            "duration_ms": duration_ms,
            "started": started,
            "finished": finished,
            "seq": seq,
            "stage_key": f"tool:{tool}" if seq == 1 else f"tool:{tool}:{seq}",
            "query": json.dumps(query or {"city": "成都"}, ensure_ascii=False),
            "note": "工具返回预览",
            "error": None,
        }
        store.update_stage(
            run_id,
            display,
            status,
            message=display,
            facts=facts,
            started_at=started,
            finished_at=finished,
        )
        store.save_trace_span(
            run_id,
            f"{run_id}:tool:{tool}:{seq}",
            component="tool",
            name=tool,
            status=status,
            started_at=started,
            finished_at=finished,
            parent_span_id=f"{run_id}:agent",
            attributes={
                "tool": tool,
                "status": status,
                "duration_ms": duration_ms,
                "stage_key": facts["stage_key"],
                "seq": seq,
                "query": facts["query"],
                "note": facts["note"],
                "output_chars": 12,
            },
        )

    @staticmethod
    def _model(
        store: TravelPlanStore,
        run_id: str,
        *,
        model: str,
        seq: int,
        started: str,
        finished: str,
        tokens: tuple[int, int, int],
        cumulative_total: int,
    ) -> None:
        """一次模型调用：run_stages 一行（「在思考行程」）+ trace 一条 llm span。"""

        tokens_in, tokens_out, cached = tokens
        total = tokens_in + tokens_out
        store.update_stage(
            run_id,
            "在思考行程",
            "SUCCESS",
            message="在思考行程",
            facts={
                "model": model,
                "status": "SUCCESS",
                "duration_ms": 800,
                "started": started,
                "finished": finished,
                "seq": seq,
                "stage_key": f"llm:{model}",
                "error": None,
            },
            started_at=started,
            finished_at=finished,
        )
        store.save_trace_span(
            run_id,
            f"{run_id}:llm:{model}:{seq}",
            component="llm",
            name=model,
            status="SUCCESS",
            started_at=started,
            finished_at=finished,
            parent_span_id=f"{run_id}:agent",
            attributes={
                "model": model,
                "tag": model,
                "status": "SUCCESS",
                "duration_ms": 800,
                "seq": seq,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cached_tokens": cached,
                "total_tokens": total,
                "cumulative_total_tokens": cumulative_total,
            },
        )

    @classmethod
    def _seed(cls, store: TravelPlanStore, run_id: str = RUN) -> None:
        store.create_run(run_id, original_query="北京→成都 5 天")
        # prompt 版本 span：Agent 路径由 `agent_runner._record_prompt_version` 写。
        store.save_trace_span(
            run_id,
            f"{run_id}:workflow:agent_prompt",
            component="workflow",
            name="prompt_version",
            status="SUCCESS",
            started_at="2026-09-22T10:00:00+00:00",
            attributes={
                "prompt_version": "prompt-sha-abc",
                "system_prompt_chars": 4096,
                "tools": ["search_hotels", "search_flights", "submit_final_plan"],
                "agent": "superharness",
            },
        )
        # 先查火车（09:59），再查酒店（10:00:02），最后模型思考 —— 时间顺序与登记顺序不同。
        cls._tool(
            store,
            run_id,
            tool="search_trains",
            display="在查火车票",
            seq=1,
            started="2026-09-22T09:59:00+00:00",
            finished="2026-09-22T09:59:02+00:00",
            duration_ms=2000,
        )
        cls._tool(
            store,
            run_id,
            tool="search_hotels",
            display="在查酒店",
            seq=1,
            started="2026-09-22T10:00:02+00:00",
            finished="2026-09-22T10:00:05+00:00",
            duration_ms=3000,
            query={"city": "成都", "checkin": "2026-10-01"},
        )
        cls._model(
            store,
            run_id,
            model="gpt-test",
            seq=1,
            started="2026-09-22T10:00:06+00:00",
            finished="2026-09-22T10:00:08+00:00",
            tokens=(100, 20, 5),
            cumulative_total=120,
        )
        store.save_run_metrics(
            run_id,
            {
                "duration_ms": 9000,
                "input_tokens": 150,
                "output_tokens": 50,
                "cached_tokens": 25,
                "total_tokens": 200,
                "llm_calls": 1,
                "jev_calls": 0,
                "tool_calls": 2,
                "provider_failures": 0,
                "badcase_count": 0,
                "cost": 0.012,
            },
        )
        store.finish_run(run_id, "completed")

    def test_steps_carry_chinese_stage_status_and_duration(self, client, store):
        self._seed(store)
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()
        agent = body["agent"]

        assert agent["is_agent_run"] is True
        assert body["summary"]["agent_steps"] == 3
        # 步骤按真实发生顺序（不是登记顺序）：火车 09:59 → 酒店 10:00:02 → 模型 10:00:06
        assert [step["tool"] or step["model"] for step in agent["steps"]] == [
            "search_trains",
            "search_hotels",
            "gpt-test",
        ]
        assert [step["order"] for step in agent["steps"]] == [1, 2, 3]
        assert [step["kind"] for step in agent["steps"]] == ["tool", "tool", "model"]

        trains, hotels, model = agent["steps"]
        # 中文步骤名（run_stages 的 stage_id）与状态、耗时都要读出来
        assert (trains["stage"], trains["stage_title"]) == ("在查火车票", "在查火车票")
        assert trains["step_key"] == "tool:search_trains"
        assert trains["status"] == "SUCCESS" and trains["duration_ms"] == 2000
        assert hotels["stage_title"] == "在查酒店" and hotels["duration_ms"] == 3000
        assert hotels["tool"] == "search_hotels" and hotels["seq"] == 1
        assert "成都" in (hotels["query"] or "")
        assert model["stage_title"] == "在思考行程"
        assert model["model"] == "gpt-test"

        # 事件侧的阶段归属同样要修好（否则前端分组一律「未归属阶段」）
        tool_event = next(
            item for item in body["events"] if item["event_type"] == "TOOL" and item["tool"] == "search_hotels"
        )
        assert tool_event["stage"] == "在查酒店"
        assert tool_event["round_index"] is not None
        assistant = next(item for item in body["events"] if item["event_type"] == "ASSISTANT")
        assert assistant["stage"] == "在思考行程"
        assert (assistant["tokens_in"], assistant["tokens_out"]) == (100, 20)
        assert assistant["metadata"]["cached_tokens"] == 5
        assert assistant["metadata"]["total_tokens"] == 120

    def test_each_step_token_and_run_total(self, client, store):
        self._seed(store)
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()
        agent = body["agent"]

        # 每步 token 只在模型步上有（工具不消耗模型 token）
        assert agent["steps"][0]["tokens"] is None
        assert agent["steps"][2]["tokens"] == {"input": 100, "output": 20, "cached": 5, "total": 120}
        assert agent["steps"][2]["cumulative_total_tokens"] == 120

        tokens = agent["tokens"]
        assert tokens["source"] == "run_metrics"
        assert (tokens["input"], tokens["output"], tokens["cached"], tokens["total"]) == (150, 50, 25, 200)
        assert (tokens["steps_llm"], tokens["steps_tool"]) == (1, 2)
        assert tokens["step_total"] == 120
        assert tokens["duration_ms"] == 9000
        # 汇总 token 与 run 头部同一份口径
        assert body["run"]["total_tokens"] == 200

    def test_meta_comes_from_trace_when_audit_is_missing(self, client, store):
        self._seed(store)
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()
        agent = body["agent"]

        assert agent["prompt_version"] == "prompt-sha-abc"
        assert agent["tools"] == ["search_hotels", "search_flights", "submit_final_plan"]
        # 没有 audit 文件也没有 submit span / plan：交卷方式如实留空，不猜
        assert agent["plan_origin"] is None and agent["plan_origin_source"] is None
        assert agent["truncation"] is None
        assert any("truncation" in note for note in agent["notes"])
        assert agent["limits"]["from_audit"] is False

    def test_plan_origin_derived_from_submit_span(self, client, store):
        self._seed(store)
        self._tool(
            store,
            self.RUN,
            tool="submit_final_plan",
            display="在调用 submit_final_plan",
            seq=1,
            started="2026-09-22T10:00:09+00:00",
            finished="2026-09-22T10:00:09+00:00",
            duration_ms=0,
        )
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()

        assert body["agent"]["plan_origin"] == "submit_final_plan"
        assert body["agent"]["plan_origin_source"] == "trace_spans"
        assert "submit_final_plan" in body["agent"]["plan_origin_label"]
        assert any("反推" in note for note in body["agent"]["notes"])

    def test_audit_agent_block_is_read_when_present(self, client, store, tmp_path, monkeypatch):
        """audit_report.json 是 plan_origin / truncation / 步数上限的权威来源（库里没有这些列）。"""

        output_dir = tmp_path / "outputs"
        (output_dir / self.RUN).mkdir(parents=True)
        (output_dir / self.RUN / "audit_report.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "prompt_version": "prompt-sha-abc",
                        "plan_origin": "message_json",
                        "max_steps": 24,
                        "timeout_seconds": 600,
                        "truncation": "Agent 达到最大步数（24 步）仍未收尾",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("TRAVELPLAN_OUTPUT_DIR", str(output_dir))
        self._seed(store)
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()
        agent = body["agent"]

        assert agent["plan_origin"] == "message_json"
        assert agent["plan_origin_source"] == "audit_report.json"
        assert "message_json" in agent["plan_origin_label"]
        assert agent["truncation"] == "Agent 达到最大步数（24 步）仍未收尾"
        assert agent["limits"]["from_audit"] is True
        assert (agent["limits"]["max_steps"], agent["limits"]["timeout_seconds"]) == (24, 600)

    def test_provider_call_is_aligned_to_the_tool_step(self, client, store):
        """Agent 路径的 tool span 不带 source_id：按时间窗口把 Provider 账本行挂回去。"""

        self._seed(store)
        store.save_source(
            self.RUN,
            {
                "source_id": "src-hotel-1",
                "provider": "tuniu",
                "source_type": "run",
                "query": {"city": "成都"},
                "fetched_at": "2026-09-22T10:00:03+00:00",  # 落在 search_hotels 的窗口内
                "normalized": {"count": 7, "items": [{"name": "某酒店"}]},
                "status": "OK",
            },
        )
        store.save_source(
            self.RUN,
            {
                "source_id": "src-early-1",
                "provider": "amap",
                "source_type": "discovery",
                "query": {"keywords": "熊猫基地"},
                "fetched_at": "2026-09-22T09:00:00+00:00",  # 不在任何工具窗口内
                "normalized": {"count": 3, "items": []},
                "status": "OK",
            },
        )
        body = client.get(f"/api/v1/admin/runs/{self.RUN}/timeline", headers=AUTH).json()

        hotel_step = next(step for step in body["agent"]["steps"] if step["tool"] == "search_hotels")
        assert (hotel_step["provider"], hotel_step["returned"]) == ("tuniu", 7)
        hotel_event = next(
            item for item in body["events"] if item["event_type"] == "TOOL" and item["tool"] == "search_hotels"
        )
        assert hotel_event["provider"] == "tuniu"
        assert hotel_event["metadata"]["source_id"] == "src-hotel-1"
        assert "返回 7 条" in hotel_event["summary"]
        assert any("时间窗口" in note for note in hotel_event["notes"])

        # 对齐上的账本行不再作为孤儿 Provider 事件重复出现
        provider_titles = [item["title"] for item in body["events"] if item["event_type"] == "PROVIDER"]
        assert "tuniu · run" not in provider_titles
        # 没对齐上的照旧单独列出，并按 Agent 口径说明原因
        orphan = next(
            item for item in body["events"] if item["event_type"] == "PROVIDER" and item["title"].startswith("amap")
        )
        assert any("时间窗口" in note for note in orphan["notes"])

    def test_legacy_fixed_flow_run_keeps_its_attribution(self, client, store):
        """旧 run（固定 12 步）不许被 Agent 口径改写：父 span 关系仍然生效。"""

        run_id = "tp-legacy-flow"
        store.create_run(run_id, original_query="北京→成都")
        store.update_stage(
            run_id,
            "check_budget",
            "SUCCESS",
            message="check_budget",
            facts={"projected_total": 5000, "status": "within_budget"},
            started_at="2026-09-20T10:00:00+00:00",
            finished_at="2026-09-20T10:00:01+00:00",
        )
        store.save_trace_span(
            run_id,
            f"{run_id}:tool:search_hotels:1",
            component="tool",
            name="search_hotels",
            status="SUCCESS",
            started_at="2026-09-20T10:00:00+00:00",
            finished_at="2026-09-20T10:00:01+00:00",
            parent_span_id=f"{run_id}:check_budget",
            attributes={"provider": "tuniu", "tool": "search_hotels", "status": "OK", "returned": 12},
        )
        store.finish_run(run_id, "completed")

        body = client.get(f"/api/v1/admin/runs/{run_id}/timeline", headers=AUTH).json()
        assert body["agent"]["is_agent_run"] is False
        event = next(item for item in body["events"] if item["event_type"] == "TOOL")
        assert event["stage"] == "check_budget"
        assert event["provider"] == "tuniu" and event["metadata"]["returned"] == 12

    def test_submit_tool_name_matches_the_runner_constant(self):
        """两处常量必须一致：`admin_timeline` 刻意不 import agent_runner（重依赖）。"""

        from app.admin_timeline import SUBMIT_TOOL_NAME
        from app.agent_runner import SUBMIT_TOOL_NAME as RUNNER_SUBMIT_TOOL_NAME

        assert SUBMIT_TOOL_NAME == RUNNER_SUBMIT_TOOL_NAME

    def test_reads_back_what_the_step_reporter_writes(self, tmp_path):
        """真实写入端（`app/agent_trace.StepReporter`）→ 真实读取端（`run_timeline`）。

        手工造 span 的用例只证明"字段对了就能读"；这一条用同一份落库代码写一遍，
        守的是两边字段名不许漂移（改一处不改另一处，页面会静默空白）。
        """

        from app.admin_timeline import run_timeline
        from app.agent_trace import build_step_reporter

        store = make_store(tmp_path / "agent.db")
        run_id = "tp-agent-roundtrip"
        store.create_run(run_id, original_query="北京→成都")
        reporter = build_step_reporter(store, run_id)

        tool_step = reporter.begin_tool("search_hotels", {"city": "成都"})
        reporter.finish_tool(tool_step, output='{"items":[1,2,3]}')
        model_step = reporter.begin_model("gpt-test")
        reporter.finish_model(model_step, input_tokens=120, output_tokens=30, cached_tokens=10)
        store.save_run_metrics(
            run_id,
            {
                "duration_ms": 4200,
                "input_tokens": 120,
                "output_tokens": 30,
                "cached_tokens": 10,
                "total_tokens": 150,
                "llm_calls": 1,
                "jev_calls": 0,
                "tool_calls": 1,
                "provider_failures": 0,
                "badcase_count": 0,
                "cost": 0.004,
            },
        )
        store.finish_run(run_id, "completed")

        body = run_timeline(store, run_id)
        agent = body["agent"]
        # prompt_version span 没写（只有 agent_runner 会写）：靠 tool span 的 stage_key 认出来
        assert agent["is_agent_run"] is True
        assert [step["kind"] for step in agent["steps"]] == ["tool", "model"]

        tool_row, model_row = agent["steps"]
        assert tool_row["stage_title"] == "在查酒店"
        assert tool_row["tool"] == "search_hotels"
        assert tool_row["status"] == "SUCCESS"
        assert isinstance(tool_row["duration_ms"], int)
        assert tool_row["tokens"] is None
        assert model_row["stage_title"] == "在思考行程"
        assert model_row["tokens"] == {"input": 120, "output": 30, "cached": 10, "total": 150}
        assert model_row["cumulative_total_tokens"] == 150
        assert agent["tokens"]["total"] == 150 and agent["tokens"]["step_total"] == 150

        # 事件侧：工具事件挂在中文步骤名上，模型事件带得上 token
        tool_event = next(item for item in body["events"] if item["event_type"] == "TOOL")
        assert tool_event["stage"] == "在查酒店" and tool_event["round_index"] is not None
        assistant = next(item for item in body["events"] if item["event_type"] == "ASSISTANT")
        assert assistant["metadata"]["total_tokens"] == 150
        # 轮询用的那个字段也一致（前端实时进度与事后复盘看的是同一个步骤名）
        assert store.get_run_progress(run_id)["current_stage"] == "在思考行程"


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
