"""A 线：跨会话城市知识缓存的离线契约测试。"""

from __future__ import annotations

from datetime import timedelta

from app import city_cache
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
