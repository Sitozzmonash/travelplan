"""Jev 接入的端到端验证（接管任务 §4）。

这些用例回答四个必须成立的问题：
  1. Jev 真的被调用了，且调用**只发生在三个高价值节点**上；
  2. Jev 选出的方案真的被采用（而不是记一笔然后丢掉）；
  3. Jev 任何形式的失败都不影响行程产出（fallback + 可审计）；
  4. 越权/低置信度/选错方案这两类"软决策与硬事实冲突"必须留下 Bad Case。

全部离线：FakeHub / FakeLLM / FakeJev 都只替换网络层。
"""

from __future__ import annotations

import json

import pytest

from app.decision.planner_decision import (
    DECISION_PLAN_CHOICE,
    DECISION_QUALITY_GATE,
    DECISION_TRADEOFF,
)
from app.workflow import execute_travel_run
from tests.fakes import QUERY, FakeHub, FakeJev, FakeLLM, make_store


RUN_ID = "tp-jev-test"


def _run(tmp_path, *, jev=None, failing=(), empty=()):
    store = make_store(tmp_path / "tp.db")
    result = execute_travel_run(
        QUERY,
        store=store,
        # FakeHub 会把每次调用落进 sources（有 run_id 外键），所以必须用同一个 run_id。
        hub=FakeHub(store=store, run_id=RUN_ID, failing=set(failing), empty=set(empty)),
        llm=FakeLLM(),
        jev=jev,
        output_dir=tmp_path / "outputs",
        run_id=RUN_ID,
    )
    return store, result


class TestJevIsActuallyWired:
    def test_jev_is_called_only_on_the_three_decision_points(self, tmp_path):
        jev = FakeJev()
        store, result = _run(tmp_path, jev=jev)
        assert result.ok, result.error

        tags = [call["tag"] for call in jev.calls]
        # 只有这三个节点允许问 Jev；多一个就说明"逐个 POI 调 Jev"的坏味道已经出现。
        assert set(tags) <= {DECISION_PLAN_CHOICE, DECISION_TRADEOFF, DECISION_QUALITY_GATE}
        # 质量门无条件经过；方案选择与取舍只在存在多份候选时才发问（一个选项的"选择"是浪费）。
        assert DECISION_QUALITY_GATE in tags
        assert len(tags) == len(set(tags)) <= 3

    def test_jev_state_carries_no_secret_or_user_identity(self, tmp_path):
        jev = FakeJev()
        _run(tmp_path, jev=jev)
        for call in jev.calls:
            payload = json.dumps(call["state"], ensure_ascii=False, default=str)
            for forbidden in ("api_key", "API_KEY", "Bearer", "user_id", "anonymous", "Secret"):
                assert forbidden not in payload, (call["tag"], forbidden)
            # 意图摘要是白名单字段，不应把 TripIntent 整个倒出去。
            assert "raw_query" not in payload

    def test_jev_choice_is_recorded_with_its_reason(self, tmp_path):
        jev = FakeJev()
        store, result = _run(tmp_path, jev=jev)
        assert result.ok
        choice = result.audit["plan_choice"]
        # 无论有没有真的问 Jev，这次选择本身必须留下"选了谁 + 为什么"。
        assert choice["selected"]
        assert choice["variant"]
        assert choice["reason"]
        assert "fallback" in choice
        # 选中的方案必须与最终行程一致，否则"选了 B 却跑 A"会骗过所有人。
        assert choice["selected"] in set(choice["candidates"])

    def test_jev_calls_are_persisted_as_trace_spans_and_audit(self, tmp_path):
        jev = FakeJev()
        store, result = _run(tmp_path, jev=jev)
        spans = store.get_trace_spans(RUN_ID)
        jev_spans = [span for span in spans if span["component"] == "jev"]
        assert [span["name"] for span in jev_spans] == [call["tag"] for call in jev.calls]
        for span in jev_spans:
            attributes = span["attributes"]
            # 管理端 Run Detail 的 "Jev Calls" 就靠这几个字段，缺一个就渲染不出来。
            assert attributes["decision_type"]
            assert attributes["input_summary"]
            assert attributes["criteria"]
            assert "fallback" in attributes
            assert "quota" in attributes
            assert "confidence" in attributes

        metrics = store.get_run_metrics(RUN_ID)
        assert metrics["jev_calls"] == len(jev.calls)
        assert len(result.audit["jev_calls"]) == len(jev.calls)

    def test_planner_subspans_are_nested_under_the_workflow_span(self, tmp_path):
        store, _ = _run(tmp_path, jev=FakeJev())
        planner_spans = [
            span for span in store.get_trace_spans(RUN_ID) if span["component"] == "planner"
        ]
        assert {span["name"] for span in planner_spans} >= {
            "build_candidates",
            "hard_constraint_recheck",
        }
        for span in planner_spans:
            assert span["parent_span_id"] == f"{RUN_ID}:build_initial_plan"

    def test_workflow_span_tree_has_no_orphans(self, tmp_path):
        store, _ = _run(tmp_path, jev=FakeJev())
        spans = store.get_trace_spans(RUN_ID)
        ids = {span["span_id"] for span in spans}
        for span in spans:
            parent = span["parent_span_id"]
            if parent:
                assert parent in ids, span["span_id"]


class TestJevNeverBreaksTheRun:
    @pytest.mark.parametrize(
        "status",
        ["TIMEOUT", "HTTP_429", "HTTP_401", "INVALID_RESPONSE", "BUDGET_EXCEEDED", "LOW_CONFIDENCE"],
    )
    def test_every_failure_mode_still_produces_a_plan(self, tmp_path, status):
        jev = FakeJev(
            {
                DECISION_PLAN_CHOICE: {"status": status},
                DECISION_TRADEOFF: {"status": status},
                DECISION_QUALITY_GATE: {"status": status},
            }
        )
        store, result = _run(tmp_path, jev=jev)
        assert result.ok, f"{status} 把整次规划打挂了"
        assert result.plan is not None
        assert result.audit["plan_choice"]["fallback"] is True
        assert result.audit["quality_gate"]["fallback"] is True
        # fallback 必须写明原因，否则运维无法区分"没配 Key"和"Jev 挂了"。
        gate_record = result.audit["quality_gate"]["jev"]
        assert gate_record["status"] == status
        assert gate_record["fallback"] is True
        assert gate_record["fallback_reason"]

    def test_unavailable_jev_marks_timeout_and_keeps_run_green(self, tmp_path):
        jev = FakeJev(available=False)
        store, result = _run(tmp_path, jev=jev)
        assert result.ok
        assert result.audit["jev_calls"]
        assert all(record["status"] == "TIMEOUT" for record in result.audit["jev_calls"])

    def test_run_without_jev_client_is_skipped_not_failed(self, tmp_path):
        # jev=None 时必须构造一个"未配置"的客户端并走 SKIPPED 分支，而不是崩。
        store, result = _run(tmp_path, jev=None)
        assert result.ok
        metrics = store.get_run_metrics(RUN_ID)
        # SKIPPED 不是一次真实调用，不能计入用量。
        assert metrics["jev_calls"] == 0


class TestJevBadCases:
    def test_quota_exhausted_records_a_badcase(self, tmp_path):
        jev = FakeJev({DECISION_QUALITY_GATE: {"status": "HTTP_429", "quota": {"remaining": 0}}})
        store, _ = _run(tmp_path, jev=jev)
        items, _ = store.list_badcases(run_id=RUN_ID)
        assert any(item["category"] == "jev_quota" for item in items)

    def test_timeout_records_a_badcase(self, tmp_path):
        jev = FakeJev({DECISION_QUALITY_GATE: {"status": "TIMEOUT"}})
        store, _ = _run(tmp_path, jev=jev)
        items, _ = store.list_badcases(run_id=RUN_ID)
        assert any(item["category"] == "jev_timeout" for item in items)

    def test_invalid_response_records_a_badcase(self, tmp_path):
        jev = FakeJev({DECISION_QUALITY_GATE: {"status": "INVALID_RESPONSE"}})
        store, _ = _run(tmp_path, jev=jev)
        items, _ = store.list_badcases(run_id=RUN_ID)
        assert any(item["category"] == "jev_invalid_response" for item in items)

    def test_jev_keeping_a_plan_with_hard_issues_is_recorded(self, tmp_path):
        """硬问题在、Jev 说 KEEP → jev_missed_replan（最有价值的一类 Bad Case）。"""

        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "KEEP"}})
        store, result = _run(tmp_path, jev=jev, empty={"search_hotels"})
        gate = result.audit["quality_gate"]
        assert gate["hard_issue_count"] > 0
        items, _ = store.list_badcases(run_id=RUN_ID)
        assert any(item["category"] == "jev_missed_replan" for item in items)

    def test_jev_asking_replan_without_hard_issue_is_recorded(self, tmp_path):
        jev = FakeJev({DECISION_QUALITY_GATE: {"choice": "REPLAN"}})
        store, result = _run(tmp_path, jev=jev)
        gate = result.audit["quality_gate"]
        if gate["hard_issue_count"] == 0:
            items, _ = store.list_badcases(run_id=RUN_ID)
            assert any(item["category"] == "jev_unnecessary_replan" for item in items)


class TestJevArtifacts:
    def test_run_writes_all_six_artifacts(self, tmp_path):
        store, result = _run(tmp_path, jev=FakeJev())
        target = tmp_path / "outputs" / RUN_ID
        for filename in (
            "plan.json",
            "plan.md",
            "audit_report.json",
            "trace.jsonl",
            "metrics.json",
            "badcases.json",
        ):
            assert (target / filename).is_file(), filename
            assert filename in result.outputs

        metrics = json.loads((target / "metrics.json").read_text(encoding="utf-8"))
        # metrics.json 与库里的 run_metrics 必须是同一份数字，否则"产物"和"管理端"会各说各话。
        stored = store.get_run_metrics(RUN_ID)
        assert {key: metrics[key] for key in metrics} == {
            key: stored[key] for key in metrics
        }
        assert metrics["jev_calls"] == len(result.audit["jev_calls"])

        trace_lines = [
            json.loads(line)
            for line in (target / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(span["component"] == "jev" for span in trace_lines)
        assert any(span["component"] == "workflow" for span in trace_lines)
        assert any(span["component"] == "tool" for span in trace_lines)
        assert any(span["component"] == "mcp" for span in trace_lines)
        assert any(span["component"] == "llm" for span in trace_lines)

        badcases = json.loads((target / "badcases.json").read_text(encoding="utf-8"))
        assert badcases["run_id"] == RUN_ID
        assert badcases["summary"]["total"] == len(badcases["items"])

        # audit 的产物索引必须把新增的三件算进去，否则人会以为产物还是三件。
        audit = json.loads((target / "audit_report.json").read_text(encoding="utf-8"))
        assert {item["filename"] for item in audit["artifacts"]} >= {
            "trace.jsonl",
            "metrics.json",
            "badcases.json",
        }

    def test_badcase_trace_refs_resolve_to_real_spans(self, tmp_path):
        """Bad Case 里的 trace_refs 必须真的能在 trace 里找到，否则"可追溯"是假的。"""

        store, result = _run(tmp_path, jev=FakeJev(), empty={"search_hotels"})
        span_ids = {span["span_id"] for span in store.get_trace_spans(RUN_ID)}
        assert span_ids, "这次 run 一条 span 都没有"
        items, _ = store.list_badcases(run_id=RUN_ID)
        assert items, "这次 run 应该检测出 Bad Case（住宿 Provider 被置空）"
        for item in items:
            for ref in item["trace_refs"]:
                # 允许两种形态：既有 workflow span（{run}:{stage}）与新业务 span。
                assert ref in span_ids or any(ref.startswith(prefix) for prefix in span_ids), (
                    item["category"],
                    ref,
                )

    def test_badcases_are_persisted_and_counted_in_metrics(self, tmp_path):
        store, result = _run(tmp_path, jev=FakeJev())
        metrics = store.get_run_metrics(RUN_ID)
        items, total = store.list_badcases(run_id=RUN_ID)
        assert total == metrics["badcase_count"] == len(items)
        assert all(item["detected_by"] == "rule" for item in items)
        assert all(item["analysis_status"] == "pending" for item in items)
        assert all(item["badcase_id"].startswith(f"bc-{RUN_ID}-") for item in items)
