"""引导式旅程的验证（用户旅程落地任务 §20：Session API / Prefetch / Preference / Planner）。

这些用例守的是"按钮真的有用"这件事：用户点过的偏好必须变成结构化字段、进入正式 run，
并且 MUST/WANT/REJECT 必须真的改变候选集合 —— 而不是只写进一句话。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import discovery, recommendations, sessions
from app.models import Place, TripIntent
from app.selection import (
    MUST,
    REJECT,
    WANT,
    apply_user_place_preferences,
    preference_bonus,
    resolve_pace,
)
from app.store import TravelPlanStore
from tests.fakes import FakeHub, FakeLLM, make_store


def _place(place_id: str, name: str) -> Place:
    return Place(place_id=place_id, name=name, normalized_name=name, city="成都")


def _ready_recommendation(session_id: str) -> discovery.PrefetchBundle:
    return discovery.PrefetchBundle(
        session_id=session_id,
        discovery_status="READY",
        places=[_place("p1", "熊猫基地"), _place("p2", "宽窄巷子")],
        extras={"recommendation": {
            "status": "READY", "version": 1, "source": "llm", "place_ids": ["p1", "p2"],
        }},
    )


def _seed_ready_recommendation(store: TravelPlanStore, session_id: str) -> None:
    def publish(current):
        current["status"] = sessions.SESSION_READY
        current["discovery_status"] = sessions.DISCOVERY_READY
        current["prefetch"] = _ready_recommendation(session_id).dump()
    store.update_planning_session(session_id, publish)


class TestUserPlaceSelection:
    def test_reject_is_hard_excluded(self):
        places = [_place("p1", "熊猫基地"), _place("p2", "网红店")]
        outcome = apply_user_place_preferences(places, {"p2": REJECT})
        assert [place.place_id for place in outcome.kept] == ["p1"]
        assert [place.place_id for place in outcome.excluded] == ["p2"]
        assert outcome.decisions[0].reason_codes[0] == "USER_REJECT"

    def test_must_and_want_are_kept_and_bonused(self):
        places = [_place("p1", "熊猫基地"), _place("p2", "宽窄巷子"), _place("p3", "锦里")]
        selections = {"p1": MUST, "p2": WANT}
        outcome = apply_user_place_preferences(places, selections)
        assert len(outcome.kept) == 3
        assert [place.place_id for place in outcome.must_places] == ["p1"]
        assert [place.place_id for place in outcome.want_places] == ["p2"]
        must_bonus, _ = preference_bonus(places[0], selections)
        want_bonus, _ = preference_bonus(places[1], selections)
        neutral_bonus, reason = preference_bonus(places[2], selections)
        assert must_bonus > want_bonus > 0
        assert neutral_bonus == 0 and reason is None

    def test_selection_can_be_keyed_by_name_too(self):
        places = [_place("p1", "熊猫基地")]
        outcome = apply_user_place_preferences(places, {"熊猫基地": REJECT})
        assert outcome.excluded and not outcome.kept


class TestPaceAuto:
    def test_auto_is_deterministic(self):
        intent = TripIntent(destination=["成都"], days=5, pace="auto")
        assert resolve_pace(intent, candidate_count=8, playable_days=5) == "relaxed"
        assert resolve_pace(intent, candidate_count=30, playable_days=5) == "packed"
        # 同样的输入永远同样的结果（"帮我选"不是随机）
        assert resolve_pace(intent, candidate_count=18, playable_days=5) == resolve_pace(
            intent, candidate_count=18, playable_days=5
        )

    def test_explicit_pace_wins(self):
        intent = TripIntent(destination=["成都"], days=5, pace="relaxed")
        assert resolve_pace(intent, candidate_count=40, playable_days=5) == "relaxed"


class TestDiscovery:
    def test_prefetch_returns_candidates_without_persisting(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        store.create_run("ps-test")
        hub = FakeHub(store=store, run_id="ps-test")
        intent = TripIntent(
            destination=["成都"], origin="北京", start_date="2026-10-01", days=5, travelers=2
        )
        result = discovery.prefetch(hub, FakeLLM(), intent, hotel_pages=2)
        assert result["transport"].outbound and result["transport"].inbound
        assert result["hotels"].items
        assert result["social"].evidences
        assert result["places"].places
        assert not result["errors"]
        # 候选必须来自真实 Provider（带 place_id），不是模型编的
        assert all(place.place_id for place in result["places"].places)

    def test_bundle_roundtrip_keeps_types(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        store.create_run("ps-test")
        hub = FakeHub(store=store, run_id="ps-test")
        intent = TripIntent(destination=["成都"], origin="北京", start_date="2026-10-01", days=5)
        result = discovery.prefetch(hub, FakeLLM(), intent)
        bundle = discovery.PrefetchBundle(
            session_id="ps-test",
            outbound=result["transport"].outbound,
            hotels=result["hotels"].items,
            evidences=result["social"].evidences,
            places=result["places"].places,
            provider_calls=hub.audit_entries(),
        )
        restored = discovery.PrefetchBundle.load(bundle.dump())
        assert restored.reused
        assert type(restored.outbound[0]).__name__ in {"TrainOption", "FlightOption"}
        assert type(restored.hotels[0]).__name__ == "HotelOption"
        assert type(restored.places[0]).__name__ == "Place"


class TestSessionService:
    @pytest.fixture()
    def store(self, tmp_path) -> TravelPlanStore:
        return make_store(tmp_path / "t.db")

    def test_create_patch_and_view(self, store: TravelPlanStore):
        session = sessions.create_session(
            store,
            {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 5, "travelers": 2},
        )
        assert session["status"] == sessions.SESSION_COLLECTING
        # 没有 submit 就不跑 Discovery；新会话仍必须明确标记推荐尚未就绪。
        assert session["discovery_status"] == sessions.DISCOVERY_PENDING
        assert session["prefetch"]["recommendation"]["status"] == "PENDING"
        assert session["prefetch"]["recommendation"]["place_ids"] == []
        assert session["place_candidates"] == []

        patched = sessions.patch_session(
            store,
            session["session_id"],
            {"transport_priority": "cheapest", "pace": "auto", "poi_selections": {"p1": "MUST"}},
        )
        assert patched is not None
        assert patched["preferences"]["transport_priority"] == "cheapest"
        assert patched["poi_selections"] == {"p1": "MUST"}

        view = sessions.session_view(patched)
        assert view["preference_labels"]["transport_priority"] == "价格最低"
        assert view["preference_labels"]["pace"] == "帮我安排"
        assert view["capabilities"]["hotel_star_filter"] is False
        # 「接受换酒店」目前不影响排程（行程全程只订一家），必须如实声明为不可用，
        # 否则前端会留一个"点了没用"的按钮 —— 这正是 PRD 里反复要消灭的假按钮。
        assert view["capabilities"]["hotel_allow_change"] is False
        assert view["capabilities"]["hotel_allow_change_note"]

    def test_unknown_values_fall_back_to_auto(self, store: TravelPlanStore):
        session = sessions.create_session(store, {"origin": "北京", "destination": "成都"})
        patched = sessions.patch_session(
            store, session["session_id"], {"transport_mode": "潜艇", "hotel_priority": "???", "pace": "???"}
        )
        assert patched is not None
        assert patched["preferences"]["transport_mode"] == "auto"
        assert patched["preferences"]["hotel_priority"] == "auto"
        assert patched["preferences"]["pace"] == "auto"

    def test_base_intent_is_immutable_so_discovery_cannot_go_stale(self, store: TravelPlanStore):
        """基础信息不允许通过 PATCH 改 —— 这是"Discovery 不会串味"的唯一防线。

        改了目的地却复用上一轮 Discovery，就会拿着成都的攻略去排重庆的行程。
        现在的做法是**结构上不允许**（白名单里没有 origin/destination/start_date/days），
        所以这个用例锁的是"白名单别被加宽"：一旦有人加进去，这条断言会先炸。
        需要换目的地时前端重新 POST 一个 session，Discovery 自然重跑。
        """

        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 3}
        )
        patched = sessions.patch_session(
            store,
            session["session_id"],
            {
                "destination": "重庆",
                "origin": "上海",
                "days": 10,
                "start_date": "2027-01-01",
                "pace": "packed",
            },
        )
        assert patched is not None

        basic = patched["basic_intent"]
        assert basic["destination"] == "成都"
        assert basic["origin"] == "北京"
        assert basic["days"] == 3
        assert basic["start_date"] == "2026-10-01"
        # 白名单内的字段照常生效：不是"整个 PATCH 都被拒了"。
        assert patched["preferences"]["pace"] == "packed"

    def test_start_creates_exactly_one_run_and_is_idempotent(self, store: TravelPlanStore):
        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 5}
        )
        _seed_ready_recommendation(store, session["session_id"])
        jobs: list[tuple] = []

        def submit(fn: Any, *args: Any) -> None:
            jobs.append((fn, args))

        first = sessions.start_run(store, session["session_id"], submit=submit)
        second = sessions.start_run(store, session["session_id"], submit=submit)
        assert first["run_id"] == second["run_id"]
        assert len(jobs) == 1  # 重复点「开始规划」不会再排一个后台任务
        run = store.get_run(first["run_id"])
        assert run is not None and run["source"] == "guided"
        assert run["source_session_id"] == session["session_id"]
        confirmed = jobs[0][1][4]
        assert [place.place_id for place in confirmed.places] == ["p1", "p2"]
        assert confirmed.extras["selection_source"] == "recommended"

    @pytest.mark.parametrize("enabled", [False, True])
    def test_create_without_submit_keeps_recommendation_pending(self, store, monkeypatch, enabled):
        monkeypatch.setenv("DISCOVERY_ENABLED", "1" if enabled else "0")
        session = sessions.create_session(store, {"origin": "北京", "destination": "成都"})
        saved = store.get_planning_session(session["session_id"])
        assert saved["status"] == sessions.SESSION_COLLECTING
        assert saved["prefetch"]["recommendation"]["status"] == "PENDING"

    def test_disabled_discovery_with_submit_is_failed(self, store, monkeypatch):
        monkeypatch.setenv("DISCOVERY_ENABLED", "0")
        jobs = []
        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都"}, submit=lambda *args: jobs.append(args)
        )
        assert session["discovery_status"] == sessions.DISCOVERY_FAILED
        assert session["prefetch"]["recommendation"]["status"] == "FAILED"
        assert not jobs
        outcome = sessions.start_run(store, session["session_id"], submit=lambda *args: jobs.append(args))
        assert outcome == {"error": "recommendation_unavailable"}
        assert not jobs
        assert store.get_planning_session(session["session_id"])["run_id"] is None

    @pytest.mark.parametrize(("state", "error"), [
        ("legacy", "recommendation_unavailable"),
        ("PENDING", "recommendation_not_ready"),
        ("RUNNING", "recommendation_not_ready"),
        ("FAILED", "recommendation_unavailable"),
        ("empty", "recommendation_unavailable"),
    ])
    def test_unready_recommendation_cannot_start_even_with_raw_candidates(self, store, monkeypatch, state, error):
        monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0")
        session = sessions.create_session(store, {"origin": "北京", "destination": "成都"})
        payload = _ready_recommendation(session["session_id"]).dump()
        payload["raw_candidates"] = list(payload["places"])
        if state == "legacy":
            payload.pop("recommendation")
        elif state == "empty":
            payload["places"] = []
            payload["recommendation"]["place_ids"] = []
        else:
            payload["recommendation"]["status"] = state
        session["prefetch"] = payload
        store.save_planning_session(session)
        outcome = sessions.start_run(
            store, session["session_id"], submit=lambda *args: pytest.fail("must not submit")
        )
        assert outcome == {"error": error}
        assert store.get_planning_session(session["session_id"])["run_id"] is None
        with store._connect() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
            assert conn.execute("SELECT COUNT(*) AS n FROM run_progress").fetchone()["n"] == 0

    def test_closed_session_refuses_to_start(self, store: TravelPlanStore):
        session = sessions.create_session(store, {"origin": "北京", "destination": "成都"})
        sessions.cancel_session(store, session["session_id"])
        outcome = sessions.start_run(store, session["session_id"], submit=lambda *a: None)
        assert outcome["error"] == "session_closed"

    def test_intent_from_basic_never_calls_a_model(self):
        intent = sessions.intent_from_basic(
            {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 5, "travelers": 2},
            {"transport_mode": "train", "hotel_priority": "value", "pace": "relaxed"},
        )
        assert intent.destination == ["成都"]
        assert intent.transport_mode == "train"
        assert intent.hotel_priority == "value"
        assert intent.pace == "relaxed"
        assert intent.source == "guided"


class TestSessionApi:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch) -> tuple[TestClient, TravelPlanStore]:
        from app import api as api_module

        store = make_store(tmp_path / "t.db")
        monkeypatch.setattr(api_module, "get_store", lambda: store)
        monkeypatch.setenv("DISCOVERY_ENABLED", "1")

        def recommend(llm, intent, *, store, session_id, on_progress=None):
            on_progress({"stage": "recommendation", "status": "RUNNING", "result_count": 0})
            return _ready_recommendation(session_id)

        monkeypatch.setattr(recommendations, "recommend_guided", recommend)
        jobs = []

        def submit(fn, *args):
            if fn is sessions.run_discovery:
                # 推荐引擎零网络：只注入假模型，不再注入 ProviderHub。
                return fn(*args, llm_factory=FakeLLM)
            assert fn is sessions._start_job
            jobs.append(args)  # API 只验证提交；正式 Workflow 由下方 FakeHub E2E 覆盖。

        monkeypatch.setattr(api_module._RUN_EXECUTOR, "submit", submit)
        return TestClient(api_module.api), store, jobs

    def test_create_get_patch_start(self, client):
        http, _store, jobs = client
        created = http.post(
            "/api/v1/planning-sessions",
            json={"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 5, "travelers": 2},
        )
        assert created.status_code == 202
        session_id = created.json()["session_id"]

        got = http.get(f"/api/v1/planning-sessions/{session_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["basic_intent"]["destination"] == "成都"
        assert body["preferences"]["pace"] == "auto"

        patched = http.patch(
            f"/api/v1/planning-sessions/{session_id}",
            json={"hotel_priority": "value", "poi_selections": {"p1": "REJECT"}},
        )
        assert patched.status_code == 200
        assert patched.json()["poi_selections"] == {"p1": "REJECT"}

        started = http.post(f"/api/v1/planning-sessions/{session_id}/start")
        assert started.status_code == 202
        assert started.json()["run_id"]
        assert len(jobs) == 1, "只有走到 START 才提交正式规划任务"

        missing = http.get("/api/v1/planning-sessions/ps-nope")
        assert missing.status_code == 404

    def test_cancel_then_start_is_409(self, client):
        http, _store, _jobs = client
        session_id = http.post(
            "/api/v1/planning-sessions", json={"origin": "北京", "destination": "成都", "days": 3}
        ).json()["session_id"]
        assert http.delete(f"/api/v1/planning-sessions/{session_id}").status_code == 200
        assert http.post(f"/api/v1/planning-sessions/{session_id}/start").status_code == 409
