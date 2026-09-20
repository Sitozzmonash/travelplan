"""app/store.py 的单元测试（PRD §17、§28）。

验证 SQLite 归档链路：建表 → run → source → evidence → place → decision → plan，
以及 JSON 列能原样读回（审计要能复盘"搜了什么、返回了什么、为什么这么选"）。

关键约束：测试只写 `tmp_path` 下的临时库，**绝不碰 data/travelplan.db**。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.models import (
    BudgetSummary,
    Decision,
    DecisionStatus,
    Evidence,
    HotelOption,
    HotelPlan,
    ItineraryDay,
    ItineraryItem,
    Place,
    TripIntent,
    TripPlan,
    utcnow,
)
from app.store import DEFAULT_DB_PATH, TravelPlanStore

EXPECTED_TABLES = {
    "runs",
    "trip_requests",
    "sources",
    "evidence",
    "places",
    "place_evidence",
    "decisions",
    "plans",
    "plan_items",
}


@pytest.fixture()
def store(tmp_path) -> TravelPlanStore:
    """每个用例一个独立的库文件：用例之间不允许共享状态。"""
    return TravelPlanStore(tmp_path / "nested" / "travelplan.db")


def _evidence(evidence_id: str = "e1", source_id: str | None = "src1") -> Evidence:
    return Evidence(
        id=evidence_id,
        provider="mediacrawler",
        source_type="note",
        source_id=source_id,
        source_url="https://example.com/1",
        title="成都三天怎么玩",
        author="小明",
        published_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
        fetched_at=utcnow(),
        text="点了龙抄手，人均40元",
        place_mentions=["宽窄巷子"],
        specific_dishes=["龙抄手"],
        raw_metrics={"likes": 1200},
    )


def _plan(run_id: str = "run1") -> TripPlan:
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
        source_ids=["src1"],
    )
    return TripPlan(
        run_id=run_id,
        query="成都三日",
        intent=TripIntent(destination=["成都"], days=1, travelers=2),
        days=[ItineraryDay(day_index=0, date=date(2026, 10, 1), area="青羊区", items=[item])],
        budget=BudgetSummary(
            budget_total=6000.0,
            known_real_cost=1000.0,
            estimated_cost=200.0,
            projected_total=1200.0,
            remaining=4800.0,
            status="within_budget",
            breakdown={"门票": 100.0, "餐饮估算": 200.0},
            breakdown_price_type={"门票": "realtime", "餐饮估算": "estimated"},
        ),
        generated_at=utcnow(),
    )


class TestSchema:
    def test_all_tables_exist(self, store):
        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

        assert EXPECTED_TABLES <= {row[0] for row in rows}

    def test_init_schema_is_idempotent(self, store):
        store.init_schema()
        store.init_schema()

        with sqlite3.connect(store.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        assert count == 0

    def test_creates_parent_directory(self, tmp_path):
        target = tmp_path / "a" / "b" / "c.db"

        TravelPlanStore(target)

        assert target.exists()

    def test_default_path_is_not_used_when_explicit_path_given(self, store):
        """防止测试污染真实库。"""
        assert store.db_path != DEFAULT_DB_PATH
        assert store.db_path.name == "travelplan.db"

    def test_foreign_keys_are_enforced(self, store):
        """外键没打开的话，写脏数据不会报错，审计时才发现对不上账。"""
        with store._connect() as conn:
            enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]

        assert enabled == 1

    def test_evidence_with_unknown_source_id_is_rejected(self, store):
        store.create_run("run1")

        with pytest.raises(sqlite3.IntegrityError):
            store.save_evidence("run1", _evidence(source_id="不存在的来源"))


class TestRunLifecycle:
    def test_create_and_get_run(self, store):
        store.create_run("run1", user_id="u1", original_query="成都三日")

        run = store.get_run("run1")

        assert run is not None
        assert run["run_id"] == "run1"
        assert run["user_id"] == "u1"
        assert run["status"] == "running"
        assert run["original_query"] == "成都三日"

    def test_finish_run_updates_status(self, store):
        store.create_run("run1")

        store.finish_run("run1", status="completed")

        assert store.get_run("run1")["status"] == "completed"

    def test_finish_run_keeps_status_on_rerun(self, store):
        store.create_run("run1")
        store.finish_run("run1", status="failed")
        store.finish_run("run1", status="completed")

        assert store.get_run("run1")["status"] == "completed"

    def test_missing_run_is_none(self, store):
        assert store.get_run("nope") is None

    def test_save_trip_request_stores_intent_json(self, store):
        store.create_run("run1")
        intent = TripIntent(destination=["成都"], days=3, travelers=2, budget_total=6000.0)

        store.save_trip_request("run1", "成都三日", intent)

        with sqlite3.connect(store.db_path) as conn:
            raw_query, intent_json = conn.execute(
                "SELECT raw_query, intent_json FROM trip_requests WHERE run_id='run1'"
            ).fetchone()

        assert raw_query == "成都三日"
        assert json.loads(intent_json)["destination"] == ["成都"]
        assert json.loads(intent_json)["days"] == 3


class TestSourcesAndEvidence:
    def test_save_source_returns_id_and_round_trips_query(self, store):
        store.create_run("run1")

        source_id = store.save_source(
            "run1",
            {
                "source_id": "src1",
                "provider": "mediacrawler",
                "source_type": "note",
                "source_url": "https://example.com/1",
                "query": {"关键词": "成都宽窄巷子", "limit": 20},
                "fetched_at": "2026-10-01T08:00:00+00:00",
                "raw": {"count": 3},
                "normalized": {"kept": 3},
                "status": "OK",
            },
        )

        assert source_id == "src1"
        sources = store.list_sources("run1")
        assert len(sources) == 1
        assert sources[0]["query"]["关键词"] == "成都宽窄巷子"  # JSON 列原样读回
        assert sources[0]["raw"] == {"count": 3}
        assert sources[0]["normalized"] == {"kept": 3}
        assert sources[0]["status"] == "OK"

    def test_save_source_generates_id_when_missing(self, store):
        store.create_run("run1")

        source_id = store.save_source("run1", {"provider": "tavily"})

        assert source_id
        assert store.list_sources("run1")[0]["provider"] == "tavily"

    def test_failed_source_is_recorded_truthfully(self, store):
        """工具失败也要留痕：status + error 必须能读出来（PRD §28 / START §10）。"""
        store.create_run("run1")

        store.save_source(
            "run1",
            {"source_id": "src-fail", "provider": "amap", "status": "RATE_LIMIT", "raw": {"error": "CUQPS"}},
        )

        source = store.list_sources("run1")[0]
        assert source["status"] == "RATE_LIMIT"
        assert source["raw"]["error"] == "CUQPS"

    def test_save_evidence_round_trips_lists_and_metrics(self, store):
        store.create_run("run1")
        store.save_source("run1", {"source_id": "src1", "provider": "mediacrawler"})

        store.save_evidence("run1", _evidence())

        evidence = store.get_evidence("run1")[0]
        assert evidence["evidence_id"] == "e1"
        assert evidence["place_mentions"] == ["宽窄巷子"]
        assert evidence["specific_dishes"] == ["龙抄手"]
        assert evidence["raw_metrics"] == {"likes": 1200}
        assert evidence["published_at"].startswith("2026-03-01")

    def test_evidence_without_source_is_allowed(self, store):
        """社交文本可能来自直传而不经过 sources 表，source_id 允许为 NULL。"""
        store.create_run("run1")

        store.save_evidence("run1", _evidence(source_id=None))

        assert store.get_evidence("run1")[0]["source_id"] is None


class TestPlaces:
    def test_save_place_round_trips_aliases(self, store):
        store.create_run("run1")
        place = Place(
            place_id="P1",
            name="宽窄巷子",
            normalized_name="宽窄巷子",
            aliases=["成都宽窄巷子景区"],
            type="风景名胜",
            lat=30.6691,
            lng=104.0555,
            city="成都",
            district="青羊区",
            opening_hours="09:00-22:00",
            amap_verified=True,
        )

        store.save_place("run1", place)

        saved = store.get_places("run1")[0]
        assert saved["name"] == "宽窄巷子"
        assert saved["aliases"] == ["成都宽窄巷子景区"]
        assert saved["amap_verified"] is True
        assert saved["district"] == "青羊区"

    def test_link_place_evidence(self, store):
        store.create_run("run1")
        store.save_source("run1", {"source_id": "src1"})
        store.save_evidence("run1", _evidence())
        store.save_place("run1", Place(place_id="P1", name="宽窄巷子"))

        store.link_place_evidence("run1", "P1", "e1")

        assert store.get_place_evidence_ids("run1", "P1") == ["e1"]

    def test_linking_unknown_place_is_rejected(self, store):
        store.create_run("run1")
        store.save_source("run1", {"source_id": "src1"})
        store.save_evidence("run1", _evidence())

        with pytest.raises(sqlite3.IntegrityError):
            store.link_place_evidence("run1", "不存在的地点", "e1")

    def test_同一地点在两个_run_里各自建档且证据不串(self, store):
        for run_id in ("run1", "run2"):
            store.create_run(run_id)
            store.save_source(run_id, {"source_id": f"{run_id}-src"})
            store.save_evidence(
                run_id,
                _evidence(evidence_id=f"{run_id}-ev", source_id=f"{run_id}-src"),
            )
            store.save_place(run_id, Place(place_id="POI-SHARED", name="宽窄巷子"))
            store.link_place_evidence(run_id, "POI-SHARED", f"{run_id}-ev")

        assert [place["place_id"] for place in store.get_places("run1")] == ["POI-SHARED"]
        assert [place["place_id"] for place in store.get_places("run2")] == ["POI-SHARED"]
        assert store.get_place_evidence_ids("run1", "POI-SHARED") == ["run1-ev"]
        assert store.get_place_evidence_ids("run2", "POI-SHARED") == ["run2-ev"]


class TestDecisions:
    def test_save_and_get_decision(self, store):
        store.create_run("run1")
        decision = Decision(
            entity_id="P1",
            status=DecisionStatus.REJECT,
            agent_or_stage="dedupe_places",
            reason_codes=["duplicate_of:P1", "geo_name"],
            reason_text="与「宽窄巷子」判定为同一地点",
            scores={"distance_meters": 29.3, "name_similarity": 0.9},
        )

        store.save_decision("run1", decision)

        saved = store.get_decisions("run1")[0]
        assert saved["entity_id"] == "P1"
        assert saved["decision"] == "REJECT"
        assert saved["reason_codes"] == ["duplicate_of:P1", "geo_name"]
        assert saved["scores"]["distance_meters"] == 29.3

    def test_save_decisions_bulk_keeps_order(self, store):
        store.create_run("run1")
        decisions = [
            Decision(entity_id=f"P{i}", status=DecisionStatus.KEEP, reason_codes=["representative"])
            for i in range(5)
        ]

        store.save_decisions_bulk("run1", decisions)

        assert [row["entity_id"] for row in store.get_decisions("run1")] == [f"P{i}" for i in range(5)]

    def test_decisions_are_scoped_by_run(self, store):
        store.create_run("run1")
        store.create_run("run2")
        store.save_decision("run1", Decision(entity_id="P1", status=DecisionStatus.KEEP))
        store.save_decision("run2", Decision(entity_id="P1", status=DecisionStatus.REJECT))

        assert store.get_decisions("run1")[0]["decision"] == "KEEP"
        assert store.get_decisions("run2")[0]["decision"] == "REJECT"


class TestPlans:
    def test_save_plan_writes_json_md_and_items(self, store):
        store.create_run("run1")
        plan = _plan()

        store.save_plan(plan, plan_md="# 成都三日")

        saved = store.get_plan("run1")
        assert saved is not None
        assert saved["plan"]["run_id"] == "run1"
        assert saved["plan"]["budget"]["status"] == "within_budget"
        assert saved["plan_md"] == "# 成都三日"
        assert len(saved["items"]) == 1
        assert saved["items"][0]["day_index"] == 0
        assert saved["items"][0]["item"]["name"] == "宽窄巷子"

    def test_plan_json_is_reconstructable(self, store):
        """plan.json 是最终交付物：必须能反序列化回 TripPlan。"""
        store.create_run("run1")
        store.save_plan(_plan())

        restored = TripPlan.model_validate(store.get_plan("run1")["plan"])

        assert restored.days[0].date == date(2026, 10, 1)
        assert restored.days[0].items[0].start_minutes == 540
        assert restored.budget.breakdown_price_type["餐饮估算"] == "estimated"
        assert restored.days[0].items[0].evidence_ids == ["e1"]

    def test_save_plan_is_replaceable(self, store):
        """同一次 run 重跑要覆盖，而不是留两份自相矛盾的 plan。"""
        store.create_run("run1")
        store.save_plan(_plan())
        store.save_plan(_plan())

        saved = store.get_plan("run1")
        assert len(saved["items"]) == 1

        with sqlite3.connect(store.db_path) as conn:
            plans = conn.execute("SELECT COUNT(*) FROM plans WHERE run_id='run1'").fetchone()[0]
            items = conn.execute("SELECT COUNT(*) FROM plan_items WHERE run_id='run1'").fetchone()[0]

        assert plans == 1
        assert items == 1

    def test_get_plan_for_unknown_run_is_none(self, store):
        assert store.get_plan("nope") is None


class TestHotelAndTransportRowsAreNotStored:
    def test_hotel_is_kept_inside_plan_json(self, store):
        """酒店/航班不单独建表，只存在 plan.json 里：读回来必须完整（含 provenance）。"""
        store.create_run("run1")
        plan = _plan()
        plan.hotel = HotelPlan(
            selected=HotelOption(
                hotel_id="H1",
                name="成都某酒店",
                provider="tuniu",
                fetched_at=utcnow(),
                price_per_night=420.0,
                total_price=840.0,
                price_note="420 起价/晚",
            ),
            alternatives=[],
            selection_reason="价格与位置折中",
        )

        store.save_plan(plan)

        saved = store.get_plan("run1")["plan"]
        assert saved["hotel"]["selected"]["name"] == "成都某酒店"
        assert saved["hotel"]["selected"]["price_note"] == "420 起价/晚"
        assert saved["hotel"]["selected"]["provider"] == "tuniu"
