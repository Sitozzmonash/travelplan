"""跨会话城市知识库：攻略**正文**的复用（存储层）。

为什么单独一组：旧实现只把 POI 与"攻略提及"落进城市缓存，正文没落 —— 于是第二个去
同一座城市的会话仍然要把社媒检索 + 模型抽取全价重付一遍（实测社媒链 45~180s、
批抽取一批可到 54.7s）。这一组把**正文的存储语义**钉住：

  1. 正文与检索词能原样 roundtrip，且按内容键去重（同一篇文章不会反复累积新行）；
  2. 超过 TTL 的行在下次写入时被清掉（否则一座城市会永远 stale）。

（原先还有三条"正式 run 命中缓存后不再打 Provider"的端到端断言，随固定 12 步流程
一起退役：那是在断言旧流程的取数预算，Agent 路径的取数由 Agent 自己在循环里决定。）
"""

from __future__ import annotations

from datetime import timedelta

from app import city_cache, discovery, sessions
from app.models import Evidence, Place, TripIntent, utcnow
from tests.fakes import FakeHub, FakeLLM, make_store
from tests.test_city_cache import explicit_sqlite_only


def _place() -> Place:
    return Place(
        place_id="amap-1", name="宽窄巷子", normalized_name="宽窄巷子", city="成都",
        type="sight", lat=30.66, lng=104.05, amap_verified=True,
    )


def _intent(*, origin="广州", days=5) -> TripIntent:
    return TripIntent(
        destination=["成都"],
        origin=origin,
        start_date="2026-10-01",
        days=days,
        travelers=2,
        source="guided",
    )


def _discover(store, intent: TripIntent, *, session_id: str):
    """跑一次完整 Discovery（生产保真：Discovery 不落库，所以 store=None）。"""

    hub = FakeHub(store=None, run_id=session_id, poi_spread=0.25)
    result = discovery.prefetch(hub, FakeLLM(), intent, hotel_pages=2)
    return result, hub


def _cache_from(store, intent: TripIntent, result, hub, *, session_id: str, updated_at: str | None = None):
    """按生产的写法把一次 Discovery 的可复用部分落进城市缓存。"""

    bundle = sessions._bundle_from(session_id, intent, result, hub)
    city_cache.write_candidates(
        "成都",
        bundle.places,
        bundle.evidences,
        store=store,
        updated_at=updated_at,
        social_queries=bundle.social_queries,
        social_served_queries=bundle.social_served_queries,
    )
    return bundle


class TestCityEvidenceStorage:
    def test_evidence_text_and_queries_roundtrip(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        intent = _intent()
        result, hub = _discover(store, intent, session_id="ps-1")
        bundle = _cache_from(store, intent, result, hub, session_id="ps-1")

        assert bundle.evidences, "FakeHub 应该抓到了攻略，否则这组测试没有意义"
        assert bundle.social_queries, "FakeLLM 应该给出了检索词"

        now = utcnow()
        hit = city_cache.read_candidates("成都", store=store, now=now)
        assert hit is not None
        assert len(hit.evidences) == len(bundle.evidences)
        # 正文必须原样回来（不是 500 字片段）。
        original = sorted(bundle.evidences, key=lambda item: item.title)
        cached = sorted(hit.evidences, key=lambda item: item.title)
        for before, after in zip(original, cached):
            assert after.text == before.text
            assert after.title == before.title
            assert after.provider == before.provider
            assert after.place_mentions == before.place_mentions
        assert hit.social_queries == list(bundle.social_queries)
        assert hit.social_served_queries == list(bundle.social_served_queries)

    def test_same_article_does_not_accumulate_rows(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        store.init_schema()
        evidence = Evidence(
            id="e-1", source_type="web", provider="tavily",
            source_url="https://example.com/chengdu", title="成都三日", text="宽窄巷子值得去",
        )
        later = Evidence(
            id="e-2", source_type="web", provider="tavily",
            source_url="https://example.com/chengdu", title="成都三日", text="宽窄巷子值得去，早上去",
        )
        now = utcnow()
        city_cache.write_candidates(
            "成都", [], [evidence], store=store, updated_at=now.isoformat()
        )
        city_cache.write_candidates(
            "成都", [], [later], store=store,
            updated_at=(now + timedelta(hours=1)).isoformat(),
        )
        rows = store.get_city_evidence_rows("成都")
        assert len(rows) == 1, "同一 source_url 应该落成同一行"
        assert rows[0]["text"] == later.text, "后抓到的版本应该胜出"

    def test_expired_rows_are_pruned_on_write(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        stale_at = (utcnow() - timedelta(days=30)).isoformat()
        city_cache.write_candidates(
            "成都",
            [],
            [Evidence(id="old", source_type="web", provider="tavily", title="旧攻略", text="很久以前")],
            store=store,
            updated_at=stale_at,
        )
        assert store.get_city_evidence_rows("成都"), "先确认旧行确实写进去了"
        # 30 天前的行已超过攻略 TTL(15 天) 与 POI TTL(15 天)，下次写入时应被清掉。
        city_cache.write_candidates(
            "成都",
            [],
            [Evidence(id="new", source_type="web", provider="tavily", title="新攻略", text="刚抓的")],
            store=store,
            updated_at=utcnow().isoformat(),
        )
        rows = store.get_city_evidence_rows("成都")
        titles = {row["title"] for row in rows}
        assert titles == {"新攻略"}

    def test_evidence_limit_caps_payload(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        evidences = [
            Evidence(id=f"e-{index}", source_type="web", provider="tavily", title=f"攻略{index}", text="正文")
            for index in range(5)
        ]
        # 没有 POI 就不算命中（防止一次空 Provider 响应被缓存），所以这里必须给一个地点。
        city_cache.write_candidates(
            "成都", [_place()], evidences, store=store, updated_at=utcnow().isoformat()
        )
        hit = city_cache.read_candidates("成都", store=store, evidence_limit=2)
        assert hit is not None and len(hit.evidences) == 2

    def test_public_payload_does_not_expose_evidence_text(self, tmp_path):
        """公开只读接口没鉴权，正文不能外发；元信息与字符数保留。"""

        store = make_store(tmp_path / "cache.db")
        city_cache.write_candidates(
            "成都",
            [_place()],
            [Evidence(id="e-1", source_type="web", provider="tavily", title="成都三日", text="很长的正文" * 50)],
            store=store,
            updated_at=utcnow().isoformat(),
        )
        hit = city_cache.read_candidates("成都", store=store)
        assert hit is not None

        public = hit.payload(include_evidence_text=False)
        assert public["evidences"], "条数仍要可见"
        for item in public["evidences"]:
            assert "text" not in item
            assert item["title"] == "成都三日"
            assert item["chars"] == len("很长的正文" * 50)

        internal = hit.payload()
        assert internal["evidences"][0]["text"] == "很长的正文" * 50


def test_no_url_full_text_keys_and_mentions_survive_repeated_writes(tmp_path):
    store = make_store(tmp_path / "no-url.db")
    timestamp = utcnow().isoformat()
    first = Evidence(id="session-1", provider="social", source_type="social", title="同一个标题", text="宽窄巷子" * 150 + "早上去", place_mentions=["宽窄巷子"])
    second = first.model_copy(update={"id": "session-2", "text": "宽窄巷子" * 150 + "晚上去"})
    assert city_cache._evidence_key(first) != city_cache._evidence_key(second)
    for _ in range(2):
        city_cache.write_candidates("成都", [_place()], [first, second], store=store, updated_at=timestamp)
    rows, mentions = store.get_city_cache_rows("成都")
    assert rows[0]["evidence_count"] == 2
    assert len(mentions) == len(store.get_city_evidence_rows("成都")) == 2
    assert {row["evidence_key"] for row in mentions} == {city_cache._evidence_key(first), city_cache._evidence_key(second)}
    assert all(row["source_url"] is None for row in mentions)
    hit = city_cache.read_candidates("成都", store=store)
    assert hit is not None and len(hit.mentions) == len(hit.evidences) == 2
    assert {item.text for item in hit.evidences} == {first.text, second.text}
    city_cache.write_candidates("成都", hit.places, hit.evidences, store=store, updated_at=timestamp)
    assert store.get_city_cache_rows("成都") == (rows, mentions)
    assert all(row["source_url"] is None for row in hit.payload()["mentions"])


def test_legacy_evidence_key_and_missing_mention_key_remain_readable(tmp_path):
    store = make_store(tmp_path / "legacy-evidence.db")
    timestamp = utcnow().isoformat()
    evidence = Evidence(id="old-run", provider="web", text="宽窄巷子值得去", place_mentions=["宽窄巷子"])
    row = city_cache._evidence_rows([evidence], timestamp)[0]
    row["evidence_key"] = "legacy-meta-hash"
    store.upsert_city_evidences("成都", [row])
    store.upsert_city_pois("成都", [city_cache._poi_row(_place(), timestamp)])
    store.upsert_city_poi_mentions("成都", [{"place_id": "amap-1", "raw_name": "宽窄巷子", "provider": "web", "snippet": evidence.text, "updated_at": timestamp}])
    before = store.db_path.read_bytes()
    hit = city_cache.read_candidates("成都", store=store, evidence_limit=0)
    assert hit is not None and hit.evidences == []
    assert len(hit.mentions) == 1
    assert hit.mentions[0]["evidence_key"] == "legacy-meta-hash"
    assert hit.mentions[0]["source_url"] is None
    assert store.db_path.read_bytes() == before
    assert store.get_city_evidence_rows("成都")[0]["evidence_key"] == "legacy-meta-hash"
    full_hit = city_cache.read_candidates("成都", store=store)
    for _ in range(2):
        city_cache.write_candidates("成都", full_hit.places, full_hit.evidences, store=store, updated_at=timestamp)
    assert len(store.get_city_evidence_rows("成都")) == 1
    assert store.get_city_evidence_rows("成都")[0]["evidence_key"] == "legacy-meta-hash"
    assert len(city_cache.read_candidates("成都", store=store).mentions) == 1
