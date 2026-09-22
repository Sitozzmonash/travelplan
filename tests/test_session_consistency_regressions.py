"""会话并发和缓存首批可见性的回归；仅假 Provider + 临时 SQLite。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Event

import pytest

from app import city_cache, discovery, sessions
from app.models import Place, TripIntent, utcnow
from app.store import TravelPlanStore
from tests.fakes import FakeHub, FakeLLM, make_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0")
    return make_store(tmp_path / "sessions.db")


def create(store):
    return sessions.create_session(store, {
        "origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 3,
    })


def place():
    return Place(place_id="p1", name="宽窄巷子", normalized_name="宽窄巷子", city="成都",
                 type="风景名胜", lat=30.66, lng=104.05, amap_verified=True, district="青羊区")


def result():
    return {
        "transport": discovery.TransportCandidates(), "hotels": discovery.HotelCandidates(),
        "social": discovery.SocialEvidence(), "places": discovery.PlaceCandidates(places=[place()]),
        "stages": {key: {"status": "OK", "result_count": 1} for key in ("transport", "hotels", "social", "places")},
        "errors": {},
    }


@pytest.mark.parametrize("action", ["patch", "cancel", "start", "expire"])
@pytest.mark.parametrize("fail", [False, True])
def test_late_discovery_cannot_overwrite_user_or_closed_session(store, monkeypatch, action, fail):
    session_id = create(store)["session_id"]
    monkeypatch.setattr(city_cache, "read_candidates", lambda *a, **kw: None)
    monkeypatch.setattr(city_cache, "write_candidates", lambda *a, **kw: None)
    observed = {}

    def fake_prefetch(hub, llm, intent, *, on_partial, **kwargs):
        on_partial(result())
        sessions.patch_session(store, session_id, {"pace": "relaxed", "poi_selections": {"p1": "MUST"}})
        if action == "cancel":
            sessions.cancel_session(store, session_id)
        elif action == "start":
            sessions.start_run(store, session_id, submit=lambda *a: None)
        elif action == "expire":
            with store._connect() as conn:
                conn.execute("UPDATE planning_sessions SET expires_at=? WHERE session_id=?",
                             ((utcnow() - timedelta(minutes=1)).isoformat(), session_id))
            sessions.get_session(store, session_id)
        observed.update(deepcopy(store.get_planning_session(session_id)))
        on_partial(result())  # 晚到的分支回调
        if fail:
            raise RuntimeError("fake finalization failure")
        return result()

    monkeypatch.setattr(discovery, "prefetch", fake_prefetch)
    sessions.run_discovery(store, session_id,
                           hub_factory=lambda sid: FakeHub(store=None, run_id=sid), llm_factory=FakeLLM)
    saved = store.get_planning_session(session_id)
    assert saved["preferences"]["pace"] == "relaxed"
    assert saved["poi_selections"] == {"p1": "MUST"}
    assert saved["run_id"] == observed["run_id"]
    for event in observed["events"]:
        assert event in saved["events"]
    if action != "patch":
        assert saved == observed, "终态会话不能被任何迟到的 Discovery 回写改变"
    else:
        assert saved["status"] == "READY"
        assert saved["place_candidates"], "失败也应保留已发布候选"
        assert saved["discovery_status"] == ("FAILED" if fail else "READY")
        if not fail:
            assert saved["place_candidates"][0]["selected"] == "MUST"


def test_concurrent_starts_create_one_run_and_submit_once(store, monkeypatch):
    session_id = create(store)["session_id"]
    gate = Barrier(2)
    jobs = []
    # 两个请求已完成前置读取、同时结束 grace，真实数据库负责仲裁。
    def grace(*args):
        gate.wait(timeout=5)
        return {}, 0
    monkeypatch.setattr(sessions, "_await_discovery", grace)
    other = TravelPlanStore(store.db_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sessions.start_run, db, session_id, submit=lambda *a: jobs.append(a))
                   for db in (store, other)]
        outcomes = [future.result(timeout=10) for future in futures]
    assert outcomes[0]["run_id"] == outcomes[1]["run_id"]
    assert len(jobs) == 1
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM runs WHERE source_session_id=?", (session_id,)).fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM run_progress").fetchone()["n"] == 1
    saved = store.get_planning_session(session_id)
    assert saved["status"] == "STARTING"
    assert sum(event["event"] == "run_started" for event in saved["events"]) == 1


@pytest.mark.parametrize("action", ["cancel", "expire", "delete"])
def test_terminal_change_during_grace_prevents_start(store, monkeypatch, action):
    session_id = create(store)["session_id"]
    jobs = []
    def grace(*args):
        if action == "cancel":
            sessions.cancel_session(store, session_id)
        elif action == "delete":
            store.delete_planning_session(session_id)
        else:
            with store._connect() as conn:
                conn.execute("UPDATE planning_sessions SET expires_at=? WHERE session_id=?",
                             ((utcnow() - timedelta(seconds=1)).isoformat(), session_id))
        return {}, 10
    monkeypatch.setattr(sessions, "_await_discovery", grace)
    outcome = sessions.start_run(store, session_id, submit=lambda *a: jobs.append(a))
    assert outcome["error"] == ("not_found" if action == "delete" else "session_closed")
    assert not jobs
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0


def test_start_uses_latest_preferences_and_then_freezes_them(store, monkeypatch):
    session_id = create(store)["session_id"]
    jobs = []
    def grace(*args):
        sessions.patch_session(store, session_id, {"pace": "relaxed", "budget_total": 6000,
                                                  "poi_selections": {"p1": "REJECT"}})
        return {}, 10
    monkeypatch.setattr(sessions, "_await_discovery", grace)
    sessions.start_run(store, session_id, submit=lambda *a: jobs.append(a))
    saved = deepcopy(store.get_planning_session(session_id))
    sessions.patch_session(store, session_id, {"pace": "packed", "poi_selections": {}})
    sessions.cancel_session(store, session_id)
    assert store.get_planning_session(session_id) == saved
    intent = jobs[0][4]
    assert intent.pace == "relaxed" and intent.budget_total == 6000
    assert intent.place_selections == {"p1": "REJECT"}


def test_start_failure_rolls_back_claim_and_run_creation(store, monkeypatch):
    session_id = create(store)["session_id"]
    original = sessions._compose_query
    monkeypatch.setattr(sessions, "_compose_query", lambda *a: (_ for _ in ()).throw(ValueError("bad query")))
    with pytest.raises(ValueError, match="bad query"):
        sessions.start_run(store, session_id, submit=lambda *a: None)
    assert store.get_planning_session(session_id)["run_id"] is None
    monkeypatch.setattr(sessions, "_compose_query", original)
    outcome = sessions.start_run(store, session_id, submit=lambda *a: None)
    assert outcome["run_id"]


def test_sql_insert_failure_rolls_back_all_start_state(store):
    session_id = create(store)["session_id"]
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON run_progress BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(Exception, match="test failure"):
        sessions.start_run(store, session_id, submit=lambda *a: pytest.fail("must not submit"))
    assert store.get_planning_session(session_id)["run_id"] is None
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0


def test_submit_failure_marks_run_failed_instead_of_orphan(store):
    session_id = create(store)["session_id"]
    def reject(*args):
        raise RuntimeError("executor closed")
    with pytest.raises(RuntimeError, match="executor closed"):
        sessions.start_run(store, session_id, submit=reject)
    run_id = store.get_planning_session(session_id)["run_id"]
    assert store.get_run_progress(run_id)["status"] == "FAILED"


def test_concurrent_disjoint_patches_preserve_both_fields(store):
    session_id = create(store)["session_id"]
    gate = Barrier(2)
    def patch(payload):
        gate.wait(timeout=5)
        return sessions.patch_session(store, session_id, payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(patch, payload) for payload in ({"pace": "relaxed"}, {"hotel_priority": "value"})]
        for future in futures:
            future.result(timeout=10)
    saved = store.get_planning_session(session_id)
    assert saved["preferences"]["pace"] == "relaxed"
    assert saved["preferences"]["hotel_priority"] == "value"
    assert sum(event["event"] == "user_preferences_updated" for event in saved["events"]) == 2


def test_expiry_event_only_belongs_to_the_expired_session(store):
    expired_id = create(store)["session_id"]
    live_id = create(store)["session_id"]
    with store._connect() as conn:
        conn.execute("UPDATE planning_sessions SET expires_at=? WHERE session_id=?",
                     ((utcnow() - timedelta(seconds=1)).isoformat(), expired_id))
    sessions.get_session(store, live_id)
    sessions.get_session(store, expired_id)
    sessions.get_session(store, expired_id)
    expired = store.get_planning_session(expired_id)
    live = store.get_planning_session(live_id)
    assert sum(event["event"] == "session_expired" for event in expired["events"]) == 1
    assert all(event["event"] != "session_expired" for event in live["events"])


@pytest.mark.parametrize("stale", [False, True])
def test_cache_is_published_before_parallel_live_queries_and_keeps_aggregates(store, monkeypatch, stale):
    session_id = create(store)["session_id"]
    hit = city_cache.CityCacheHit(city="成都", places=[place()], mentions=[], updated_at=utcnow().isoformat(), stale=stale)
    published = Event()
    transport_entered, hotels_entered, release = Event(), Event(), Event()
    snapshots = []
    def transport(*args):
        assert published.is_set(), "静态缓存必须先发布"
        transport_entered.set()
        assert release.wait(5)
        return discovery.TransportCandidates()
    def hotels(*args, **kwargs):
        assert published.is_set()
        hotels_entered.set()
        assert release.wait(5)
        return discovery.HotelCandidates()
    def partial(snapshot):
        snapshots.append(deepcopy(snapshot))
        published.set()
    monkeypatch.setattr(discovery, "fetch_transport_candidates", transport)
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", hotels)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sessions._prefetch_with_city_cache, session_id, TripIntent(destination=["成都"]),
                             FakeHub(store=None, run_id=session_id), 1, hit, on_partial=partial)
        try:
            assert transport_entered.wait(5)
            assert hotels_entered.wait(5), "酒店不能等待交通结束"
            first = snapshots[0]
            assert first["places"].places
            assert first["profile"]["source"] == "fallback"
            assert first["hotel_areas"]
            assert "p1" in first["poi_pools"]["attraction"]
            assert first["stages"]["transport"]["status"] == "RUNNING"
            initial_bundle = sessions._bundle_from(session_id, TripIntent(destination=["成都"]), first, None)
            assert initial_bundle.city_cache["updated_at"] == hit.updated_at
            assert initial_bundle.city_cache["stale"] == stale
        finally:
            release.set()
        final, bundle = future.result(timeout=10)
    assert len(snapshots) == 3
    for key in ("profile", "hotel_areas", "poi_pools"):
        assert getattr(bundle, key) == final[key]
    for key in ("transport", "hotels"):
        assert final["stages"][key]["duration_ms"] is not None
        assert final["stages"][key]["started_at"] and final["stages"][key]["finished_at"]


def test_start_adopts_published_cache_while_live_queries_are_still_running(store, monkeypatch):
    session_id = create(store)["session_id"]
    hit = city_cache.CityCacheHit(city="成都", places=[place()], mentions=[], updated_at=utcnow().isoformat())
    live_entered, release = Event(), Event()
    monkeypatch.setattr(city_cache, "read_candidates", lambda *a, **kw: hit)
    def transport(*args):
        live_entered.set()
        assert release.wait(5)
        return discovery.TransportCandidates()
    def hotels(*args, **kwargs):
        assert release.wait(5)
        return discovery.HotelCandidates()
    monkeypatch.setattr(discovery, "fetch_transport_candidates", transport)
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", hotels)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sessions.run_discovery, store, session_id,
                             hub_factory=lambda sid: FakeHub(store=None, run_id=sid),
                             llm_factory=lambda: pytest.fail("cache hit must not initialize LLM"))
        try:
            assert live_entered.wait(5)
            current = store.get_planning_session(session_id)
            assert current["place_candidates"]
            assert current["prefetch"]["city_cache"]["updated_at"] == hit.updated_at
            sessions.patch_session(store, session_id, {"poi_selections": {"p1": "MUST"}})
            jobs = []
            sessions.start_run(store, session_id, submit=lambda *a: jobs.append(a))
            bundle = jobs[0][5]
            assert bundle.places and bundle.poi_pools["attraction"] == ["p1"]
            assert bundle.discovery["transport"]["status"] == "RUNNING"
            assert bundle.city_cache["source"] == "city_cache"
            frozen = deepcopy(store.get_planning_session(session_id))
        finally:
            release.set()
        future.result(timeout=10)
    assert store.get_planning_session(session_id) == frozen


@pytest.mark.parametrize("reject_insert", [False, True])
def test_session_transaction_uses_postgres_adapter_and_commits_or_rolls_back(store, monkeypatch, reject_insert):
    """真实方言包装 + SQLite 驱动替身：验证 SQL/事务协议，不冒充 PostgreSQL 并发测试。"""
    from app.db import PostgresBackend, PostgresConnection

    session_id = create(store)["session_id"]
    raw = store._connect()
    statements = []
    commits, rollbacks, released = [], [], []

    class Driver:
        def execute(self, sql, params=None):
            statements.append(sql)
            assert "?" not in sql
            if reject_insert and sql.startswith("INSERT INTO run_progress"):
                raise RuntimeError("fake postgres insert rejected")
            return raw.execute(sql.replace("%s", "?"), params or ())

        def commit(self):
            commits.append(True)
            raw.commit()

        def rollback(self):
            rollbacks.append(True)
            raw.rollback()

    driver = Driver()

    class Pool:
        def acquire(self):
            return driver

        def release(self, connection):
            released.append(connection)

    pg_store = object.__new__(TravelPlanStore)
    pg_store._backend = PostgresBackend("postgresql://unused/test")
    monkeypatch.setattr(pg_store, "_connect", lambda: PostgresConnection(Pool()))
    try:
        if reject_insert:
            with pytest.raises(RuntimeError, match="insert rejected"):
                pg_store.start_planning_session_run(session_id, "tp-pg", lambda row: "成都三日")
            assert rollbacks == [True] and not commits
            assert store.get_planning_session(session_id)["run_id"] is None
            assert store.get_run("tp-pg") is None
        else:
            saved, created = pg_store.start_planning_session_run(session_id, "tp-pg", lambda row: "成都三日")
            assert created and saved["run_id"] == "tp-pg"
            assert commits == [True] and not rollbacks
            assert store.get_run("tp-pg")["source_session_id"] == session_id
        assert statements[0].startswith("UPDATE planning_sessions SET session_id=session_id")
        assert statements[1].startswith("SELECT * FROM planning_sessions")
        assert released == [driver]
    finally:
        raw.close()


def test_cache_live_failure_preserves_candidates_and_other_stage(store, monkeypatch):
    hit = city_cache.CityCacheHit(city="成都", places=[place()], mentions=[], updated_at=utcnow().isoformat())
    monkeypatch.setattr(discovery, "fetch_transport_candidates", lambda *a: (_ for _ in ()).throw(ValueError("transport fail")))
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", lambda *a, **kw: discovery.HotelCandidates())
    final, bundle = sessions._prefetch_with_city_cache("ps-test", TripIntent(destination=["成都"]), None, 1, hit)
    assert bundle.places and bundle.profile
    assert final["stages"]["transport"]["status"] == "FAILED"
    assert final["stages"]["hotels"]["status"] == "EMPTY"
    assert any("transport fail" in message for message in bundle.degradations)
