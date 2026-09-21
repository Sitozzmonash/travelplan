"""跨会话城市知识库：攻略**正文**的复用，以及 run 级 provenance 的重建。

为什么单独一组：旧实现只把 POI 与"攻略提及"落进城市缓存，正文没落 —— 于是第二个去
同一座城市的会话仍然要把社媒检索 + 模型抽取全价重付一遍（实测社媒链 45~180s、
批抽取一批可到 54.7s）。更麻烦的是 `sources.source_id` / `evidence.evidence_id` 都是
**全局主键**：跨会话沿用 Discovery 的原 id 会把上一次 run 的归属改掉，上一轮证据链当场断掉。
这一组把"正文复用"与"id 必须换"两件事一起钉住：

  1. 正文与检索词能原样 roundtrip，且按内容键去重（同一篇文章不会反复累积新行）；
  2. 超过 TTL 的行在下次写入时被清掉（否则一座城市会永远 stale）；
  3. 第二个会话命中缓存后，正式 run **不再检索社媒、不再调用模型抽取、不再查高德 POI**；
  4. 复用的正文在 run 里拿到**新的** source_id / evidence_id，sources 行标 `CACHED`
     并保留原始抓取时间与 `origin=city_cache`，证据链可追溯；
  5. 复用事实要如实披露在 degradations 里，不能被当成"刚查的"。
"""

from __future__ import annotations

from datetime import timedelta

from app import city_cache, discovery, sessions
from app.models import Evidence, Place, TripIntent, utcnow
from app.workflow import STATUS_CACHED, execute_travel_run
from tests.fakes import QUERY, FakeHub, FakeJev, FakeLLM, make_store


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
        # 30 天前的行已超过攻略 TTL(7 天) 与 POI TTL(15 天)，下次写入时应被清掉。
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


class TestCrossSessionEvidenceReuse:
    def _warm_cache(self, store):
        """会话 1（北京→成都 3 天）冷启动，顺便把成都知识库写热。"""

        first = _intent(origin="北京", days=3)
        result, hub = _discover(store, first, session_id="ps-bj")
        _cache_from(store, first, result, hub, session_id="ps-bj")
        return first

    def test_second_session_reuses_text_and_skips_all_remote_calls(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        self._warm_cache(store)

        # 会话 2（广州→成都 5 天）：命中城市缓存。
        second = _intent(origin="广州", days=5)
        hit = city_cache.read_candidates("成都", store=store, allow_stale=True)
        assert hit is not None and hit.evidences

        prefetch_hub = FakeHub(store=None, run_id="ps-gz", poi_spread=0.25)
        _, bundle = sessions._prefetch_with_city_cache(
            "ps-gz", second, prefetch_hub, 2, hit
        )
        # 命中的是攻略与 POI；交通与酒店仍然现查（实时数据不进缓存）。
        assert bundle.evidences, "缓存里的攻略正文必须进 PrefetchBundle"
        assert bundle.places, "缓存里的 POI 必须进 PrefetchBundle"

        run_id = "tp-city-reuse"
        store.create_run(run_id, source="guided", source_session_id="ps-gz")
        hub = FakeHub(store=store, run_id=run_id, poi_spread=0.25)
        llm = FakeLLM()
        result = execute_travel_run(
            QUERY,
            store=store,
            hub=hub,
            llm=llm,
            jev=FakeJev(),
            intent=second,
            prefetch=bundle,
            source="guided",
            source_session_id="ps-gz",
            run_id=run_id,
            output_dir=store.db_path.parent / "out",
        )

        # 1) 社媒与高德 POI 一个都没打；模型也没有再扩写检索词 / 抽地点。
        assert "search_xiaohongshu" not in hub.calls
        assert "search_douyin" not in hub.calls
        assert "web_search" not in hub.calls
        assert "search_poi" not in hub.calls
        assert "query_expansion" not in llm.tags
        assert "extract_places" not in llm.tags

        # 2) 交接结论认定 social / places 是"复用"，且阶段状态标成缓存。
        handoff = result.audit["user_journey"]["discovery"]
        assert handoff["social"]["handoff"] == "reused"
        assert handoff["places"]["handoff"] == "reused"
        assert handoff["social"]["status"] in {"CACHE", "STALE_CACHE"}

        # 3) 复用的正文换成了 run 级 id，并在 sources 里标 CACHED + 保留原始抓取时间。
        evidence_rows = store.get_evidence(run_id)
        assert evidence_rows, "复用的攻略必须落进本次 run 的 evidence 表"
        assert all(row["text"] for row in evidence_rows)
        assert all(row["evidence_id"].startswith(f"{run_id}-cache-") for row in evidence_rows)
        sources = {row["source_id"]: row for row in store.list_sources(run_id)}
        cached_sources = [row for row in sources.values() if row.get("status") == STATUS_CACHED]
        assert len(cached_sources) == len(evidence_rows)
        for row in evidence_rows:
            source = sources[row["source_id"]]
            assert source["status"] == STATUS_CACHED
            assert source["provider"] == row["provider"]
            assert source["fetched_at"], "必须保留原始抓取时间，不能写成'现在'"

        # 4) 复用事实如实披露。
        joined = " ".join(result.degradations or [])
        assert "城市知识库" in joined

    def test_reused_ids_never_overwrite_the_first_run(self, tmp_path):
        """跨会话沿用原 id 会把上一轮的归属改掉 —— 这条专门钉住"必须换 id"。"""

        store = make_store(tmp_path / "cache.db")
        self._warm_cache(store)

        # 跑两次"第二个会话"，两次都必须各自持有自己的 sources/evidence。
        for index in (1, 2):
            second = _intent(origin="广州", days=5)
            hit = city_cache.read_candidates("成都", store=store, allow_stale=True)
            assert hit is not None
            _, bundle = sessions._prefetch_with_city_cache(
                f"ps-gz-{index}", second, FakeHub(store=None, run_id=f"ps-gz-{index}"), 2, hit
            )
            run_id = f"tp-city-{index}"
            store.create_run(run_id, source="guided", source_session_id=f"ps-gz-{index}")
            execute_travel_run(
                QUERY,
                store=store,
                hub=FakeHub(store=store, run_id=run_id, poi_spread=0.25),
                llm=FakeLLM(),
                jev=FakeJev(),
                intent=second,
                prefetch=bundle,
                source="guided",
                source_session_id=f"ps-gz-{index}",
                run_id=run_id,
                output_dir=store.db_path.parent / "out",
            )

        first_rows = store.get_evidence("tp-city-1")
        second_rows = store.get_evidence("tp-city-2")
        assert first_rows and second_rows
        # 各自的 evidence 都还挂在**自己**的 run 上（没被后一次 INSERT OR REPLACE 搬走）。
        assert {row["evidence_id"] for row in first_rows}.isdisjoint(
            {row["evidence_id"] for row in second_rows}
        )
        assert all(row["source_id"].startswith("tp-city-1-") for row in first_rows)
        assert all(row["source_id"].startswith("tp-city-2-") for row in second_rows)
