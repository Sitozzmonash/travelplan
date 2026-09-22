"""app/workflow.py 的单元测试。

12 步固定 LangGraph（`travel_graph` / `node_*` / `execute_travel_run`）已经退役：
主规划路径是 `app/agent_runner.py::execute_agent_run`（Agent 自己在循环里调工具出 plan）。
所以本文件只剩**两条路径共用**的那部分契约：

1. 证据汇总（`_evidence_summary`）与前端计划载荷（`plan_payload`）；
2. run 契约（`RunResult` / `STATUS_*`）；
3. 检索词扩写 prompt 的**顶层 JSON 数组**契约（`RESEARCH_QUERY_EXPANSION_PROMPT`
   仍由 `app/discovery.py` 使用：模型必须按数组回答，解析也按 list 处理）。

随旧流程作废的用例（规则意图解析、交通/住宿比选、可行性 issue 构造、图结构与
12 节点、execute_travel_run 全流程、needs_clarification / failed 分支）已整段删除 ——
它们断言的那些函数在旧流程删除时一并消失。等价覆盖在：
  * `tests/test_planner.py`（比选打分 / 可行性 / 实体收敛 / 预算 / 计划质量）；
  * `tests/test_agent_runner.py`（Agent Loop 全流程、工具调用与产物落盘）。
铁律：**本文件不联网、不读 API Key、不写 data/travelplan.db**。
所有输入都是合成对象，断言的是"代码拿这些输入做了什么"。
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import LLM
from app.models import (
    Evidence,
    ItineraryDay,
    ItineraryItem,
    Place,
    SourceRef,
    TripIntent,
    TripPlan,
)
from app.prompts import RESEARCH_QUERY_EXPANSION_PROMPT
from app.store import TravelPlanStore
from app.workflow import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    _evidence_summary,
    plan_payload,
)

#: 固定参照日期：所有 fixture 都用它，测试不随系统时间腐烂。
TRIP_DATE = date(2026, 10, 1)
RUN_ID = "tp-test-run"

#: 追问状态。`app/api.py` 按这个字面量把 run 映射成 422（`needs_clarification`），
#: Agent 路径不再有对应的 `STATUS_*` 常量，所以这里按对外契约的字面量断言。
STATUS_NEEDS_CLARIFICATION = "needs_clarification"


# ==================================================
# 零、合成测试输入（不是任何 Provider 的真实返回）
# ==================================================


def _intent(**overrides: Any) -> TripIntent:
    """一份完整的合成意图：北京 → 成都，2026-10-01 起 3 天 2 人。"""
    data: dict[str, Any] = {
        "destination": ["成都"],
        "origin": "北京",
        "start_date": TRIP_DATE,
        "days": 3,
        "travelers": 2,
        "budget_total": 6000.0,
    }
    data.update(overrides)
    return TripIntent(**data)


def _plan(run_id: str = RUN_ID) -> TripPlan:
    """一份合成的 TripPlan（形状与 tests/test_store.py 一致）。"""
    item = ItineraryItem(
        id="i1",
        type="attraction",
        name="宽窄巷子",
        place_id="P1",
        lat=30.6691,
        lng=104.0555,
        start_time="09:00",
        end_time="11:00",
        duration_minutes=120,
        price=50.0,
        price_type="realtime",
        trust_score=80.0,
        ad_risk=5.0,
        evidence_ids=["e1"],
    )
    return TripPlan(
        run_id=run_id,
        query="成都三日",
        intent=TripIntent(destination=["成都"], origin="北京", days=1, travelers=2),
        days=[ItineraryDay(day_index=0, date=TRIP_DATE, area="青羊区", items=[item])],
    )


@pytest.fixture()
def store(tmp_path) -> TravelPlanStore:
    """每个用例一个独立库文件：绝不碰 data/travelplan.db。"""
    return TravelPlanStore(tmp_path / "nested" / "travelplan.db")


# ==================================================
# 一、证据汇总 / plan_payload
# ==================================================


class TestEvidenceSummary:
    def test_returns_the_three_documented_keys(self):
        plan = TripPlan(
            run_id=RUN_ID,
            intent=_intent(),
            sources=[SourceRef(source_id="s1"), SourceRef(source_id="s2")],
            days=[
                ItineraryDay(
                    day_index=0,
                    items=[ItineraryItem(id="i1", type="attraction", name="宽窄巷子", place_id="P1")],
                )
            ],
        )
        state = {
            "places": [
                Place(place_id="P1", name="宽窄巷子", amap_verified=True),
                Place(place_id="P2", name="武侯祠", amap_verified=False),
            ],
            "candidate_scores": {"P1": {}, "P2": {}, "P3": {}},
        }

        summary = _evidence_summary(state, plan)

        assert set(summary) == {"sources_used", "places_verified", "low_trust_filtered"}
        assert summary == {"sources_used": 2, "places_verified": 1, "low_trust_filtered": 2}
        assert all(value >= 0 for value in summary.values())

    def test_low_trust_filtered_never_goes_negative(self):
        plan = TripPlan(
            run_id=RUN_ID,
            intent=_intent(),
            days=[
                ItineraryDay(
                    day_index=0,
                    items=[
                        ItineraryItem(id="i1", type="attraction", name="A", place_id="P1"),
                        ItineraryItem(id="i2", type="attraction", name="B", place_id="P2"),
                    ],
                )
            ],
        )

        summary = _evidence_summary({"places": [], "candidate_scores": {}}, plan)

        assert summary["low_trust_filtered"] == 0

    def test_missing_state_keys_do_not_crash(self):
        summary = _evidence_summary({}, TripPlan(run_id=RUN_ID, intent=_intent()))

        assert summary == {"sources_used": 0, "places_verified": 0, "low_trust_filtered": 0}


class TestPlanPayload:
    def test_adds_evidence_from_the_store_without_mutating_the_plan(self, store):
        store.create_run(RUN_ID)
        store.save_source(RUN_ID, {"source_id": "src1", "provider": "mediacrawler"})
        store.save_evidence(
            RUN_ID,
            Evidence(
                id="e1",
                provider="mediacrawler",
                source_type="note",
                source_id="src1",
                text="龙抄手人均 40 元",
            ),
        )
        plan = _plan()
        before = plan.to_json_dict()

        payload = plan_payload(plan, store)

        assert [row["id"] for row in payload["evidence"]] == ["e1"]
        assert payload["evidence"][0]["text"] == "龙抄手人均 40 元"
        assert "evidence" not in before
        assert plan.to_json_dict() == before  # plan 对象没有被就地改写
        assert "evidence" not in TripPlan.model_fields

    def test_without_store_there_is_no_evidence_key(self):
        payload = plan_payload(_plan())

        assert "evidence" not in payload
        assert payload["run_id"] == RUN_ID

    def test_evidence_is_scoped_to_the_run(self, store):
        store.create_run(RUN_ID)
        store.create_run("other-run")
        store.save_evidence(RUN_ID, Evidence(id="e1", provider="xhs", text="成都"))
        store.save_evidence("other-run", Evidence(id="e2", provider="xhs", text="杭州"))

        payload = plan_payload(_plan(), store)

        assert [row["id"] for row in payload["evidence"]] == ["e1"]


# ==================================================
# 二、检索词扩写：prompt 要的是数组，解析就必须按数组处理
# ==================================================


class _JSONModel:
    """只回一段 JSON 文本的模型替身（`app.llm.LLM` 依赖的就是这两个面）。"""

    model_name = "fake-model"

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


class TestQueryExpansionContract:
    def test_prompt_asks_for_a_top_level_json_array(self):
        # `app/discovery.py` 按 list 判断扩写结果，prompt 就必须真的要求顶层数组。
        assert "JSON 数组" in RESEARCH_QUERY_EXPANSION_PROMPT

    def test_invoke_json_returns_a_list_for_that_prompt(self):
        queries = ["成都 必去景点", "成都 避坑 不值得"]
        llm = LLM(model=_JSONModel(queries))

        result = llm.invoke_json(RESEARCH_QUERY_EXPANSION_PROMPT, "{}", tag="query_expansion")

        assert result.ok is True
        assert result.value == queries  # discovery 正是按 list 判断的


# ==================================================
# 三、run 契约
# ==================================================


class TestRunResult:
    def test_ok_only_for_completed(self):
        from app.workflow import RunResult

        assert RunResult(run_id="r", status=STATUS_COMPLETED).ok is True
        assert RunResult(run_id="r", status=STATUS_FAILED).ok is False
        assert RunResult(run_id="r", status=STATUS_NEEDS_CLARIFICATION).ok is False

    def test_clarification_is_the_error_text(self):
        from app.workflow import RunResult

        assert (
            RunResult(run_id="r", status=STATUS_NEEDS_CLARIFICATION, error="请补充目的地").clarification
            == "请补充目的地"
        )
        assert RunResult(run_id="r", status=STATUS_COMPLETED).clarification == ""
