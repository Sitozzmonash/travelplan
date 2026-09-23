"""会话并发和缓存首批可见性的回归；仅假 Provider + 临时 SQLite。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Event

import pytest

from app import discovery, recommendations, sessions
from app.models import Place, utcnow
from app.store import TravelPlanStore
from tests.fakes import FakeLLM, make_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0")
    return make_store(tmp_path / "sessions.db")


def create(store, *, seed=True):
    """建会话；默认附上一份已就绪的推荐，因为 START 现在必须有可用推荐。"""

    session = sessions.create_session(store, {
        "origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 3,
    })
    if seed:
        publish_recommendation(store, session["session_id"])
    return session


def place(place_id="p1", name="宽窄巷子", **overrides):
    values = {"place_id": place_id, "name": name, "normalized_name": name, "city": "成都",
              "type": "风景名胜", "lat": 30.66, "lng": 104.05, "amap_verified": True, "district": "青羊区"}
    values.update(overrides)
    return Place(**values)


def recommendation_bundle(session_id, places=None):
    places = list(places or [place(), place("p2", "文殊院")])
    return discovery.PrefetchBundle(
        session_id=session_id, discovery_status="READY", places=places,
        extras={"recommendation": {"status": "READY", "version": 1, "source": "llm",
                                   "place_ids": [item.place_id for item in places]}},
    )


def publish_recommendation(store, session_id, places=None):
    bundle = recommendation_bundle(session_id, places)
    def publish(current):
        current["status"] = sessions.SESSION_READY
        current["discovery_status"] = sessions.DISCOVERY_READY
        current["prefetch"] = bundle.dump()
    store.update_planning_session(session_id, publish)
    return bundle


@pytest.mark.parametrize("action", ["patch", "cancel", "expire"])
@pytest.mark.parametrize("fail", [False, True])
def test_late_recommendation_cannot_overwrite_user_or_closed_session(store, monkeypatch, action, fail):
    session_id = create(store, seed=False)["session_id"]
    observed = {}

    def fake_recommend(llm, intent, *, store=None, session_id=None, on_progress=None, **kwargs):
        on_progress({"stage": "database", "status": "OK", "result_count": 1})
        sessions.patch_session(store, session_id, {"pace": "relaxed", "poi_selections": {"p1": "MUST"}})
        if action == "cancel":
            sessions.cancel_session(store, session_id)
        elif action == "expire":
            with store._connect() as conn:
                conn.execute("UPDATE planning_sessions SET expires_at=? WHERE session_id=?",
                             ((utcnow() - timedelta(minutes=1)).isoformat(), session_id))
            sessions.get_session(store, session_id)
        observed.update(deepcopy(store.get_planning_session(session_id)))
        on_progress({"stage": "places", "status": "OK", "result_count": 1})  # 晚到的阶段回调
        if fail:
            raise RuntimeError("fake finalization failure")
        return recommendation_bundle(session_id)

    monkeypatch.setattr(recommendations, "recommend_guided", fake_recommend)
    monkeypatch.setattr(discovery, "fetch_transport_candidates", lambda *a, **k: pytest.fail("推荐阶段不能查交通"))
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", lambda *a, **k: pytest.fail("推荐阶段不能查酒店"))
    sessions.run_discovery(store, session_id,
                           llm_factory=FakeLLM)
    saved = store.get_planning_session(session_id)
    assert saved["preferences"]["pace"] == "relaxed"
    assert saved["poi_selections"] == {"p1": "MUST"}
    assert saved["run_id"] == observed["run_id"]
    for event in observed["events"]:
        assert event in saved["events"]
    if action != "patch":
        assert saved == observed, "终态会话不能被任何迟到的推荐回写改变"
    else:
        assert saved["status"] == "READY"
        assert saved["discovery_status"] == ("FAILED" if fail else "READY")
        if fail:
            # 推荐失败不能拿原始候选充数：宁可空，也不给未筛选的全城清单。
            assert saved["place_candidates"] == []
        else:
            assert saved["place_candidates"][0]["selected"] == "MUST"


@pytest.mark.parametrize("fail", [False, True])
def test_recommendation_in_flight_cannot_overwrite_an_already_started_session(store, monkeypatch, fail):
    session_id = create(store)["session_id"]
    jobs = []
    outcome = sessions.start_run(store, session_id, submit=lambda *a: jobs.append(a))
    assert outcome["run_id"]
    frozen = deepcopy(store.get_planning_session(session_id))

    def fake_recommend(llm, intent, *, store=None, session_id=None, on_progress=None, **kwargs):
        on_progress({"stage": "places", "status": "OK", "result_count": 1})
        if fail:
            raise RuntimeError("late failure")
        return recommendation_bundle(session_id)

    monkeypatch.setattr(recommendations, "recommend_guided", fake_recommend)
    sessions.run_discovery(store, session_id,
                           llm_factory=FakeLLM)
    assert store.get_planning_session(session_id) == frozen, "START 之后的推荐进度与终稿都不能再改会话"
    assert len(jobs) == 1


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


def test_start_is_rejected_until_recommendation_is_published(store, monkeypatch):
    """推荐进行中只发布阶段元数据；START 必须等到推荐真正就绪，且不能顺手查机酒。"""

    session_id = create(store, seed=False)["session_id"]
    entered, release = Event(), Event()
    ready = recommendation_bundle(session_id, [place()])

    def fake_recommend(llm, intent, *, store=None, session_id=None, on_progress=None, **kwargs):
        on_progress({"stage": "database", "status": "OK", "result_count": 8})
        entered.set()
        assert release.wait(5)
        return ready

    monkeypatch.setattr(recommendations, "recommend_guided", fake_recommend)
    monkeypatch.setattr(discovery, "fetch_transport_candidates", lambda *a, **k: pytest.fail("推荐阶段不能查交通"))
    monkeypatch.setattr(discovery, "fetch_hotel_candidates", lambda *a, **k: pytest.fail("推荐阶段不能查酒店"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sessions.run_discovery, store, session_id,
                             llm_factory=FakeLLM)
        try:
            assert entered.wait(5)
            current = store.get_planning_session(session_id)
            assert current["place_candidates"] == [], "阶段元数据不能冒充推荐清单"
            assert current["transport_candidates"] == current["hotel_candidates"] == []
            assert current["prefetch"]["discovery"]["database"]["result_count"] == 8
            assert sessions.start_run(
                store, session_id, submit=lambda *a: pytest.fail("推荐未就绪不能开始")
            ) == {"error": "recommendation_not_ready"}
        finally:
            release.set()
        future.result(timeout=10)

    saved = store.get_planning_session(session_id)
    assert saved["status"] == saved["discovery_status"] == "READY"
    assert saved["transport_candidates"] == saved["hotel_candidates"] == []
    jobs = []
    assert sessions.start_run(store, session_id, submit=lambda *a: jobs.append(a))["run_id"]
    assert [item.place_id for item in jobs[0][5].places] == ["p1"]


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
