"""A 线：跨会话城市知识缓存的离线契约测试。"""

from __future__ import annotations

from datetime import timedelta
import json
import sqlite3

import pytest

from app import city_cache, planner
from app.models import Evidence, Place, utcnow
from app.store import TravelPlanStore


def _place() -> Place:
    return Place(
        place_id="amap-1", name="宽窄巷子", normalized_name="宽窄巷子", city="成都",
        type="sight", lat=30.66, lng=104.05, amap_verified=True,
    )


def test_city_cache_roundtrip_freshness_and_city_isolation(tmp_path):
    store = TravelPlanStore(tmp_path / "cache.db")
    evidence = Evidence(id="e-1", source_type="web", provider="tavily", text="宽窄巷子值得去", place_mentions=["宽窄巷子"])
    now = utcnow()
    city_cache.write_candidates("成都", [_place()], [evidence], store=store, updated_at=now.isoformat())

    hit = city_cache.read_candidates(" 成都 ", store=store, now=now)
    assert hit is not None
    assert hit.source == "city_cache" and hit.updated_at == now.isoformat()
    assert hit.places[0].name == "宽窄巷子"
    assert hit.mentions[0]["raw_name"] == "宽窄巷子"
    assert city_cache.read_candidates("重庆", store=store, now=now) is None

    expired = now + timedelta(days=16)
    assert city_cache.read_candidates("成都", store=store, now=expired) is None
    stale = city_cache.read_candidates("成都", store=store, now=expired, allow_stale=True)
    assert stale is not None and stale.stale is True


def test_newer_city_row_wins_over_delayed_writer(tmp_path):
    store = TravelPlanStore(tmp_path / "cache.db")
    store.upsert_city_pois("成都", [{"place_id": "p1", "name": "新版", "updated_at": "2026-01-02T00:00:00+00:00"}])
    store.upsert_city_pois("成都", [{"place_id": "p1", "name": "旧版", "updated_at": "2026-01-01T00:00:00+00:00"}])
    rows, _ = store.get_city_cache_rows("成都")
    assert rows[0]["name"] == "新版"


@pytest.fixture(autouse=True)
def explicit_sqlite_only(monkeypatch):
    original = TravelPlanStore.__init__

    def guarded(self, db_path=None, **kwargs):
        assert db_path is not None, "测试禁止默认 Store / 生产数据库"
        original(self, db_path=db_path, **kwargs)

    monkeypatch.setattr(TravelPlanStore, "__init__", guarded)


def test_dedupe_roundtrip_is_idempotent_and_preserves_metadata(tmp_path, monkeypatch):
    store = TravelPlanStore(tmp_path / "cache.db")
    first = _place().model_copy(update={"source_id": "poi-source", "aliases": ["宽窄老街"]})
    second = _place().model_copy(update={"place_id": "amap-2", "name": "成都宽窄巷子景区"})
    evidence = Evidence(id="e1", text="宽窄老街和成都宽窄巷子景区是同一处", place_mentions=["宽窄老街", second.name])
    timestamp = utcnow().isoformat()
    original = planner.dedupe_places
    calls = []

    def tracked(items):
        calls.append(len(items))
        return original(items)

    monkeypatch.setattr(planner, "dedupe_places", tracked)
    assert city_cache.write_candidates("成都", [first, second], [evidence], store=store, updated_at=timestamp) == 1
    rows_before = store.get_city_cache_rows("成都")
    hit = city_cache.read_candidates("成都", store=store)
    assert hit is not None and len(hit.places) == 1
    place = hit.places[0]
    assert place.source_id == "poi-source"
    assert "宽窄老街" in place.aliases
    assert {place.place_id, *place.merged_from} == {"amap-1", "amap-2"}
    assert {row["place_id"] for row in hit.mentions} == {place.place_id}
    assert rows_before[0][0]["evidence_count"] == 1  # 两个提及仍只是一篇攻略
    city_cache.write_candidates("成都", hit.places, hit.evidences, store=store, updated_at=timestamp)
    assert store.get_city_cache_rows("成都") == rows_before
    assert len(calls) == 3
    assert first.aliases == ["宽窄老街"] and not first.merged_from
    assert city_cache._poi_row(place, timestamp)["evidence_count"] == 0


def test_old_rows_dedupe_and_mentions_remap_without_writes(tmp_path, monkeypatch):
    store = TravelPlanStore(tmp_path / "cache.db")
    timestamp = utcnow().isoformat()
    originals = [
        _place(),
        _place().model_copy(update={"place_id": "old-id", "name": "成都宽窄巷子景区"}),
    ]
    rows = [city_cache._poi_row(place, timestamp) for place in originals]
    for row in rows:
        row.pop("metadata")
    store.upsert_city_pois("成都", rows)
    store.upsert_city_poi_mentions("成都", [{
        "place_id": "old-id", "raw_name": "成都宽窄巷子景区", "source_url": "https://example.test/old",
        "snippet": "以前来过", "updated_at": timestamp,
    }])
    before = store.db_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("缓存读取不能运行迁移、persist resolver 或写库")

    from app.places import PlaceResolver
    monkeypatch.setattr(PlaceResolver, "flush", forbidden)
    monkeypatch.setattr(store, "init_schema", forbidden)
    monkeypatch.setattr(store, "upsert_city_pois", forbidden)
    hit = city_cache.read_candidates("成都", store=store)
    assert hit is not None and len(hit.places) == 1
    assert hit.mentions[0]["place_id"] == hit.places[0].place_id
    assert hit.mentions[0]["evidence_key"] is None
    assert store.db_path.read_bytes() == before


def test_metadata_additive_migration_preserves_rows_and_primary_keys(tmp_path):
    from app.store import SCHEMA

    path = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace("    metadata_json TEXT NOT NULL DEFAULT '{}',\n", "").replace("    evidence_key TEXT,\n", "")
    timestamp = utcnow().isoformat()
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy_schema)
        conn.execute("INSERT INTO city_pois(city,place_id,name,updated_at) VALUES(?,?,?,?)", ("成都", "legacy", "宽窄巷子", timestamp))
        conn.execute("INSERT INTO city_poi_mentions(city,place_id,raw_name,source_type,source_url,updated_at) VALUES(?,?,?,?,?,?)", ("成都", "legacy", "宽窄巷子", "web", "", timestamp))
        pois_before = conn.execute("SELECT * FROM city_pois").fetchall()
        mentions_before = conn.execute("SELECT * FROM city_poi_mentions").fetchall()
    before = path.read_bytes()
    legacy_hit = city_cache.read_candidates("成都", store=TravelPlanStore(path, read_only=True))
    assert legacy_hit is not None and legacy_hit.places[0].aliases == []
    assert path.read_bytes() == before
    store = TravelPlanStore(path)
    store.init_schema()
    store.init_schema()
    with sqlite3.connect(path) as conn:
        assert [row[:-1] for row in conn.execute("SELECT * FROM city_pois")] == pois_before
        assert [row[:-1] for row in conn.execute("SELECT * FROM city_poi_mentions")] == mentions_before
        assert [row[1] for row in sorted(conn.execute("PRAGMA table_info(city_poi_mentions)"), key=lambda row: row[5]) if row[5]] == ["city", "source_url", "raw_name"]
    hit = city_cache.read_candidates("成都", store=store)
    assert hit is not None and len(hit.mentions) == 1
    assert hit.places[0].aliases == hit.places[0].merged_from == []
    assert hit.places[0].source_id is None
    assert hit.mentions[0]["source_url"] is None
    assert json.loads(store.get_city_cache_rows("成都")[0][0]["metadata_json"]) == {}


@pytest.mark.parametrize("backend_name", ["sqlite", "postgres"])
def test_additive_migration_is_idempotent_for_both_dialects(backend_name):
    # PostgreSQL 分支只检查 DDL，不建连接、不调用 resolve_backend。
    class Backend:
        name = backend_name

        def table_columns(self, conn, table):
            return conn.columns[table]

    class Connection:
        columns = {"city_pois": {"city", "place_id"}, "city_poi_mentions": {"city", "source_url", "raw_name"}}

        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)
            words = sql.split()
            self.columns[words[2]].add(words[5])

    store = object.__new__(TravelPlanStore)
    store._backend = Backend()
    conn = Connection()
    store._migrate_city_cache_metadata(conn)
    store._migrate_city_cache_metadata(conn)
    assert conn.statements == [
        "ALTER TABLE city_pois ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE city_poi_mentions ADD COLUMN evidence_key TEXT",
    ]


def test_read_only_store_does_not_migrate_or_create_database(tmp_path, monkeypatch):
    path = tmp_path / "cache.db"
    store = TravelPlanStore(path)
    city_cache.write_candidates("成都", [_place()], [], store=store)
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("只读构造不得迁移")

    monkeypatch.setattr(TravelPlanStore, "init_schema", forbidden)
    reader = TravelPlanStore(path, read_only=True)
    monkeypatch.setattr(city_cache, "TravelPlanStore", lambda **kwargs: reader)
    assert city_cache.read_candidates("成都") is not None
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        reader.upsert_city_pois("成都", [{"place_id": "bad", "name": "不能写"}])
    missing = tmp_path / "missing" / "cache.db"
    with pytest.raises(FileNotFoundError):
        TravelPlanStore(missing, read_only=True)
    assert not missing.parent.exists()
