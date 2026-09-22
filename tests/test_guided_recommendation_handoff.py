"""攻略推荐与正式规划边界；仅临时 SQLite、内存模型与 Provider 替身。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import discovery, sessions
from app.models import Place
from app.places import PlaceResolver
from tests.fakes import make_store


def poi(pid, name, **kwargs):
    return Place(place_id=pid, name=name, city="成都", type="风景名胜", **kwargs)


def bundle(status="READY"):
    places = [poi("p1", "人民公园", district="青羊区", merged_from=["old-p1"]),
              poi("p2", "文殊院", district="青羊区")]
    return discovery.PrefetchBundle(
        places=places,
        extras={
            "recommendation": {"status": status, "source": "llm", "version": 1,
                               "place_ids": ["p1", "p2"]},
            "raw_candidates": [p.model_dump(mode="json") for p in [*places, poi("raw", "未经推荐的候选")]],
            "recommended_cards": [{"place_id": "p1", "category": "attraction", "reason": "攻略支持",
                                   "evidence_ids": ["e1", "e2"], "evidence_count": 2}],
        },
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0")
    return make_store(tmp_path / "handoff.db")


def seed(store, status="READY", selections=None):
    session = sessions.create_session(store, {
        "origin": "北京", "destination": "成都", "days": 5, "start_date": "2026-10-01",
    })
    def update(current):
        current["prefetch"] = bundle(status).dump()
        current["status"] = "READY"
        current["discovery_status"] = status
        current["poi_selections"] = selections or {}
    store.update_planning_session(session["session_id"], update)
    return session["session_id"]


@pytest.mark.parametrize(("selections", "ids", "source"), [
    ({}, ["p1", "p2"], "recommended"),
    ({"p1": "WANT"}, ["p1"], "user_selected"),
    ({"p2": "MUST"}, ["p2"], "user_selected"),
    ({"old-p1": "WANT"}, ["p1"], "user_selected"),
    ({"p1": "REJECT"}, ["p2"], "recommended"),
    ({"p1": "REJECT", "p2": "WANT"}, ["p2"], "user_selected"),
])
def test_start_uses_selected_or_recommended_not_raw_pool(store, selections, ids, source):
    sid = seed(store, selections=selections)
    jobs = []
    outcome = sessions.start_run(store, sid, submit=lambda *args: jobs.append(args))
    assert outcome.get("run_id")
    assert len(jobs) == 1
    intent, confirmed = jobs[0][4:6]
    assert [p.place_id for p in confirmed.places] == ids
    assert confirmed.extras["planning_place_ids"] == ids
    assert confirmed.extras["selection_source"] == source
    assert "raw" not in confirmed.extras["planning_place_ids"]
    if "old-p1" in selections:
        assert intent.place_selections == {"p1": "WANT"}
    assert store.get_planning_session(sid)["prefetch"]["planning_place_ids"] == ids
    assert len(store.get_planning_session(sid)["prefetch"]["places"]) == 2


@pytest.mark.parametrize(("status", "selections", "error"), [
    ("PENDING", {}, "recommendation_not_ready"),
    ("RUNNING", {}, "recommendation_not_ready"),
    ("FAILED", {}, "recommendation_unavailable"),
    ("READY", {"raw": "MUST"}, "invalid_place_selection"),
    ("READY", {"人民公园": "WANT"}, "invalid_place_selection"),
    ("READY", {"p1": "REJECT", "p2": "REJECT"}, "no_selected_places"),
    ("READY", {"p1": "MUST", "old-p1": "REJECT"}, "no_selected_places"),
])
def test_bad_recommendation_or_selection_rolls_back_run(store, status, selections, error):
    sid = seed(store, status, selections)
    outcome = sessions.start_run(store, sid, submit=lambda *args: pytest.fail("cannot submit"))
    assert outcome == {"error": error}
    saved = store.get_planning_session(sid)
    assert saved["run_id"] is None
    assert saved["status"] == "READY"
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM run_progress").fetchone()["n"] == 0


def test_recommendation_cards_use_actual_evidence_and_disambiguate():
    data = bundle()
    data.places.append(poi("jy", "人民公园", district="简阳市", lat=30.397, lng=104.546))
    data.places[0].lat, data.places[0].lng = 30.657, 104.057
    cards = sessions._recommendation_cards(data, {"old-p1": "MUST"})
    first = next(c for c in cards if c["place_id"] == "p1")
    assert first["evidence_count"] == 2
    assert first["reason"] == "攻略支持"
    assert first["selected"] == "MUST"
    assert "青羊区" in first["display_name"]
    assert "简阳市" in next(c["display_name"] for c in cards if c["place_id"] == "jy")
    assert next(c for c in cards if c["place_id"] == "p2")["evidence_count"] == 0


def test_query_cache_records_full_ids_after_ingestion():
    resolver = PlaceResolver(None, city="成都")
    p = poi("p1", "文殊院", lat=30.674, lng=104.073)
    discovery.resolve_pois(resolver=resolver, terms=["寺庙推荐", "文化路线"], results={
        "寺庙推荐": SimpleNamespace(items=[p]), "文化路线": SimpleNamespace(items=[p]),
    })
    for term in ("寺庙推荐", "文化路线"):
        resolved = resolver.resolve_query(term)
        assert resolved.hit
        assert len(resolved.canonical_ids) == 1
        assert [place.place_id for place in resolved.places] == ["p1"]


def test_session_discovery_never_calls_full_prefetch_or_machine_booking(store, monkeypatch):
    from app import recommendations

    sid = seed(store, "RUNNING")
    def recommend(llm, intent, *, store, session_id, on_progress=None):
        sessions.patch_session(store, session_id, {"pace": "relaxed", "poi_selections": {"p1": "MUST"}})
        return bundle()
    monkeypatch.setattr(recommendations, "recommend_guided", recommend)
    monkeypatch.setattr(discovery, "prefetch", lambda *a, **k: pytest.fail("full discovery called"))
    monkeypatch.setattr(discovery, "fetch_transport_candidates", lambda *a, **k: pytest.fail("transport called"))
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", lambda *a, **k: pytest.fail("hotel called"))
    sessions.run_discovery(store, sid, llm_factory=lambda: object())
    saved = store.get_planning_session(sid)
    assert saved["status"] == saved["discovery_status"] == "READY"
    assert saved["transport_candidates"] == saved["hotel_candidates"] == []
    assert saved["poi_selections"] == {"p1": "MUST"}
    assert saved["preferences"]["pace"] == "relaxed"
    assert len(saved["place_candidates"]) == 2
    assert saved["place_candidates"][0]["evidence_count"] == 2
    assert sessions.session_view(saved)["recommendation"]["source"] == "llm"


def test_session_discovery_never_constructs_a_provider_hub(store, monkeypatch):
    """推荐阶段零网络：ProviderHub 只要被构造就直接炸，run_discovery 仍必须成功。"""

    from app import providers, recommendations

    sid = seed(store, "RUNNING")

    def boom(*args, **kwargs):
        raise AssertionError("推荐阶段不得构造 ProviderHub / 发起任何 Provider 调用")

    def recommend(llm, intent, *, store, session_id, on_progress=None):
        return bundle()

    monkeypatch.setattr(providers, "ProviderHub", boom)
    monkeypatch.setattr(recommendations, "recommend_guided", recommend)
    sessions.run_discovery(store, sid, llm_factory=lambda: object())
    saved = store.get_planning_session(sid)
    assert saved["status"] == saved["discovery_status"] == "READY"
    assert saved["prefetch"]["provider_calls"] == []
    assert len(saved["place_candidates"]) == 2


def test_recommendation_exception_ends_loading_without_raw_candidates(store, monkeypatch):
    from app import recommendations

    sid = seed(store, "RUNNING")
    def fail(llm, intent, **kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(recommendations, "recommend_guided", fail)
    sessions.run_discovery(store, sid, llm_factory=lambda: object())
    saved = store.get_planning_session(sid)
    assert saved["status"] == "READY"
    assert saved["discovery_status"] == "FAILED"
    assert saved["place_candidates"] == []
    assert saved["prefetch"]["recommendation"]["status"] == "FAILED"
    assert saved["prefetch"]["discovery"]["recommendation"]["status"] == "FAILED"


@pytest.mark.parametrize("prefetch", [{}, {"places": [p.model_dump(mode="json") for p in bundle().places]}])
def test_old_session_without_recommendation_cannot_silently_expand_to_city_pool(store, prefetch):
    sid = seed(store)
    store.update_planning_session(sid, lambda current: current.update(prefetch=prefetch))
    assert sessions.start_run(store, sid, submit=lambda *a: pytest.fail("must not start")) == {
        "error": "recommendation_unavailable",
    }
    assert store.get_planning_session(sid)["run_id"] is None


def test_discovery_disabled_is_explicitly_failed_and_blocks_start(store, monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    created = sessions.create_session(store, {"destination": "成都"}, submit=lambda *a: pytest.fail("disabled"))
    assert created["discovery_status"] == "FAILED"
    assert sessions.start_run(store, created["session_id"], submit=lambda *a: pytest.fail("must not start")) == {
        "error": "recommendation_unavailable",
    }


def test_only_listed_recommendations_and_related_hotel_areas_reach_run(store):
    sid = seed(store, selections={"p1": "WANT", "p2": "REJECT"})
    def update(current):
        current["prefetch"]["hotel_areas"] = [
            {"name": "青羊区", "place_ids": ["p1", "p2"]},
            {"name": "另一区域", "place_ids": ["p2"]},
        ]
        current["prefetch"]["places"].append(poi("raw", "未经推荐候选").model_dump(mode="json"))
    store.update_planning_session(sid, update)
    jobs = []
    sessions.start_run(store, sid, submit=lambda *args: jobs.append(args))
    confirmed = jobs[0][5]
    assert [p.place_id for p in confirmed.places] == ["p1"]
    assert confirmed.hotel_areas == [{"name": "青羊区", "place_ids": ["p1"]}]


def test_progress_publishes_real_metadata_not_raw_candidates(store, monkeypatch):
    from app import recommendations
    sid = seed(store, "RUNNING")
    def recommend(llm, intent, *, on_progress, **kwargs):
        on_progress({"stage": "database", "status": "OK", "result_count": 8})
        on_progress({"stage": "places", "status": "RUNNING"})
        current = sessions.session_view(store.get_planning_session(sid))
        assert current["discovery"]["database"] == {"status": "OK", "result_count": 8}
        assert current["recommendation"]["status"] == "RUNNING"
        assert current["place_candidates"] == []
        assert sessions.start_run(store, sid, submit=lambda *a: pytest.fail("too early"))["error"] == "recommendation_not_ready"
        on_progress({"stage": "places", "status": "OK", "result_count": 5})
        return bundle()
    monkeypatch.setattr(recommendations, "recommend_guided", recommend)
    sessions.run_discovery(store, sid, llm_factory=lambda: object())
    current = sessions.session_view(store.get_planning_session(sid))
    assert current["recommendation"]["status"] == "READY"
    assert current["discovery"]["places"] == {"status": "OK", "result_count": 5}
    # 第二页推荐不再查 Web：进度阶段里不能出现 web。
    assert "web" not in current["discovery"]


def test_progress_ignores_web_stage_because_engine_is_offline(store, monkeypatch):
    """旧的 web 阶段回调兜底：引擎零网络，web 不再是被承认的进度阶段。"""

    from app import recommendations

    sid = seed(store, "RUNNING")

    def recommend(llm, intent, *, on_progress, **kwargs):
        on_progress({"stage": "web", "status": "OK", "result_count": 3})
        return bundle()

    monkeypatch.setattr(recommendations, "recommend_guided", recommend)
    sessions.run_discovery(store, sid, llm_factory=lambda: object())
    saved = store.get_planning_session(sid)
    assert saved["status"] == saved["discovery_status"] == "READY"
    assert "web" not in (saved["prefetch"].get("discovery") or {})
