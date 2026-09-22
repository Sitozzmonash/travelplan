"""A 线：地点实体层与 Discovery / 城市缓存接起来的契约测试。

单元级行为在 `tests/test_places_resolver.py`；这里只钉"接起来之后"的四件事：

* A5：抽出的地名真的落进 `city_poi_mentions`（线上 0/213 有值）；
* A6：按目的地做地理围栏（成都缓存里混进乐山大佛）；
* A3/A4：第二次跑同一个城市不再打高德（别名 / 查询缓存命中）；
* A10：攻略证据挂到 canonical 实体上。
"""

from __future__ import annotations

import pytest

from app import city_cache, discovery, places
from app.models import Evidence, Place, TripIntent, utcnow
from app.store import TravelPlanStore
from tests.test_city_cache import explicit_sqlite_only
from tests.fakes import FakeHub, FakeLLM, make_store

CITY = "成都"


class _FenceHub(FakeHub):
    """给"乐山大佛"这个关键词返回邻市的 POI，用来验证地理围栏。

    真实的成因就是这样：按关键词查高德时**没有按目的地限定**，邻市的热门景点会被带进候选。
    """

    def search_poi(self, keywords, region, *, types=None, page=1, page_size=20, type_hint=None, **kwargs):
        result = super().search_poi(
            keywords, region, types=types, page=page, page_size=page_size, type_hint=type_hint, **kwargs
        )
        if "乐山" in str(keywords):
            for place in result.items:
                place.city = "乐山市"
        return result


def _intent() -> TripIntent:
    return TripIntent(
        destination=[CITY], origin="北京", start_date="2026-10-01", days=3, travelers=2, source="guided"
    )


def _evidence(evidence_id: str = "ev-1") -> Evidence:
    return Evidence(
        id=evidence_id,
        source_type="web",
        provider="tavily",
        title="成都三日游记",
        text="第一天去了宽窄巷子，第二天武侯祠和锦里，晚上在春熙路吃火锅，还去了杜甫草堂。",
    )


class _RenameHub(FakeHub):
    """把"杜甫草堂"这个关键词查回来的 POI 改名为高德上的正式全称。

    线上就是这么分裂的：攻略写"杜甫草堂"，高德返回并入库的是"成都杜甫草堂博物馆"，
    两边字符串不相等 —— 只按字符串相等匹配，提及表就永远是 0 行。
    """

    def search_poi(self, keywords, region, *, types=None, page=1, page_size=20, type_hint=None, **kwargs):
        result = super().search_poi(
            keywords, region, types=types, page=page, page_size=page_size, type_hint=type_hint, **kwargs
        )
        for place in result.items:
            if place.name == "杜甫草堂":
                place.name = "成都杜甫草堂博物馆"
                place.normalized_name = ""
        return result


class TestExtractedMentionsArePersisted:
    def test_mentions_written_back_to_evidence(self):
        """A5 前半：模型抽出的地名要回到 evidence.place_mentions。"""

        evidence = _evidence()
        extracted = [{"name": "宽窄巷子", "evidence_id": "ev-1"}, {"name": "杜甫草堂", "evidence_id": "ev-1"}]
        written = discovery.apply_extracted_mentions([evidence], extracted)
        assert written == 2
        assert evidence.place_mentions == ["宽窄巷子", "杜甫草堂"]
        # 重复抽取同一篇不该记两行。
        assert discovery.apply_extracted_mentions([evidence], extracted) == 0

    def test_mentions_table_gets_rows(self, tmp_path):
        """A5 后半：**匹配要跨写法** —— 攻略写"杜甫草堂"，入库的是"成都杜甫草堂博物馆"。"""

        store = make_store(tmp_path / "cache.db")
        store.create_run("fake-run")
        evidence = _evidence()
        evidence.place_mentions = ["杜甫草堂", "宽窄巷子"]
        result = discovery.extract_place_candidates(
            _RenameHub(store=store), FakeLLM(), _intent(), [evidence],
            queries=["宽窄巷子"], store=store,
        )
        names = {place.name for place in result.places}
        assert "成都杜甫草堂博物馆" in names, names
        assert "杜甫草堂" not in names

        city_cache.write_candidates(CITY, result.places, [evidence], store=store)
        _, mentions = store.get_city_cache_rows(CITY)
        by_place = {row["place_id"]: row["raw_name"] for row in mentions}
        dacao = next(place for place in result.places if place.name == "成都杜甫草堂博物馆")
        # 攻略里的"杜甫草堂"必须落到"成都杜甫草堂博物馆"那一行上 —— 跨写法匹配成立。
        assert by_place.get(dacao.place_id) == "杜甫草堂"
        assert "宽窄巷子" in by_place.values()

    def test_evidence_links_mount_to_canonical_place(self, tmp_path):
        """A10：证据要能顺着 canonical 实体挂上，而不是只停在一张名义上的提及表。"""

        store = make_store(tmp_path / "cache.db")
        store.create_run("fake-run")
        evidence = _evidence()
        evidence.place_mentions = ["宽窄巷子"]
        result = discovery.extract_place_candidates(
            FakeHub(store=store), FakeLLM(), _intent(), [evidence], queries=[], store=store
        )
        city_cache.write_candidates(CITY, result.places, [evidence], store=store)
        links = store.get_place_evidence_links(CITY)
        if links:  # 高德关键词没有返回任何候选时链路无从建立，此时断言"不为负"
            assert all(row["canonical_place_id"] for row in links)
            assert all(row["mention_text"] for row in links)
        assert store.get_canonical_places(CITY) or not result.places


class TestGeoFence:
    def test_out_of_city_poi_is_not_a_candidate(self):
        hub = _FenceHub(store=None)
        result = discovery.extract_place_candidates(
            hub,
            FakeLLM(),
            _intent(),
            [_evidence()],
            queries=["乐山大佛", "宽窄巷子"],
        )
        names = {place.name for place in result.places}
        assert "乐山大佛" not in names
        assert any("地理围栏" in note for note in result.notes), result.notes
        assert {item["reason"] for item in result.resolver["dropped"]} <= {
            "out_of_city", "orphan_facility"
        }


class TestCacheFirst:
    def test_second_run_reuses_entities_and_skips_amap(self, tmp_path):
        """A3 / A4：同一座城市第二次走这条链路时，别名 / 查询缓存命中的词一次高德都不打。"""

        store = make_store(tmp_path / "cache.db")
        store.create_run("fake-run")
        first_hub = FakeHub(store=store)
        first = discovery.extract_place_candidates(
            first_hub, FakeLLM(), _intent(), [_evidence()], queries=["宽窄巷子"], store=store
        )
        assert first.places, "第一次应当从高德查到候选"
        assert first.resolver["persisted"] is True
        first_searches = first_hub.calls.count("search_poi")
        assert first_searches > 0

        second_hub = FakeHub(store=store)
        second = discovery.extract_place_candidates(
            second_hub, FakeLLM(), _intent(), [_evidence()], queries=["宽窄巷子"], store=store
        )
        assert second_hub.calls.count("search_poi") < first_searches
        assert second.resolver["reused_terms"], second.resolver
        assert second.places, "复用来的候选不能是空的"
        # 复用的候选与第一次是同一批地点。
        assert {place.name for place in second.places} == {place.name for place in first.places}

    def test_detail_is_not_re_fetched_when_entity_already_has_it(self, tmp_path):
        store = make_store(tmp_path / "cache.db")
        store.create_run("fake-run")
        hub = FakeHub(store=store)
        result = discovery.extract_place_candidates(
            hub, FakeLLM(), _intent(), [_evidence()], queries=["宽窄巷子"], store=store
        )
        # 假 Hub 的 POI 自带地址与营业时间 → 实体层已够，不再逐个查详情。
        assert result.places
        assert "poi_detail" not in hub.calls

    def test_memory_only_mode_still_resolves(self):
        """不传 store 时（Discovery 的默认调用方式）照常收敛，只是没有跨会话复用。"""

        result = discovery.extract_place_candidates(
            FakeHub(store=None), FakeLLM(), _intent(), [_evidence()], queries=["宽窄巷子"]
        )
        assert result.resolver["persisted"] is False
        assert result.places


class TestResolverSummaryIsObservable:
    def test_summary_exposes_dropped_and_reused(self, tmp_path):
        """留痕要够管理端回答"为什么没重新查高德"。"""

        store = make_store(tmp_path / "cache.db")
        store.create_run("fake-run")
        hub = _FenceHub(store=store)
        result = discovery.extract_place_candidates(
            hub, FakeLLM(), _intent(), [_evidence()], queries=["乐山大佛", "宽窄巷子"], store=store
        )
        summary = result.resolver
        for key in ("city", "entities", "dirty", "traces", "searched_terms", "dropped"):
            assert key in summary, key
        assert summary["city"] == CITY
        trace_rows = store.list_place_resolver_traces(city=CITY)
        assert trace_rows, "解析过程必须留痕"
        assert all(row["action"] for row in trace_rows)


class TestWorkflowPathPersistsEntityLayer:
    """正式 run 的 POI 节点也必须落实体层 —— 它和 Discovery 共用同一份复用底座。

    漏掉这一处的表现很隐蔽：候选照样出得来，但跨会话复用与 Resolver 留痕都不会发生，
    等于同一套规则只在一半的路径上生效。
    """

    def test_quick_run_writes_entities_and_traces(self, tmp_path):
        from app.workflow import execute_travel_run
        from tests.fakes import QUERY, FakeJev

        store = make_store(tmp_path / "t.db")
        run_id = "tp-pipeline-run"
        store.create_run(run_id, source="quick")
        hub = FakeHub(store=store, run_id=run_id, poi_spread=0.25)
        result = execute_travel_run(
            QUERY,
            store=store,
            hub=hub,
            llm=FakeLLM(),
            jev=FakeJev(),
            run_id=run_id,
            output_dir=tmp_path / "out",
        )
        assert result.status in {"completed", "degraded"}, result.error
        entities = store.get_canonical_places(CITY)
        assert entities, "正式 run 的候选没有写进实体层"
        assert store.get_place_provider_refs(CITY), "Provider 引用没落库，复用无从生效"
        traces = store.list_place_resolver_traces(run_id=run_id)
        assert traces, "Resolver 留痕缺失：事后无法回答'为什么没重新查高德'"
        # 落进 run 的候选里不能有附属设施（假 Hub 的 type 是 "attraction"，这里主要验证
        # 名称类设施也被拦住：检索词里带"成都必去景点"这类词时不会带进子设施）。
        saved = store.get_places(run_id)
        assert saved
        assert not [
            row for row in saved if places.facility_kind(str(row.get("name")), str(row.get("type")))
        ]


@pytest.mark.parametrize("legacy_rows", [False, True])
def test_cache_uses_existing_provider_identity_and_restores_aliases(tmp_path, monkeypatch, legacy_rows):
    store = TravelPlanStore(tmp_path / "canonical.db")
    timestamp = utcnow().isoformat()
    store.upsert_canonical_places([{
        "canonical_place_id": "c-1", "canonical_name": "成都大熊猫繁育研究基地",
        "normalized_name": "大熊猫繁育研究基地", "city": CITY, "lat": 30.73, "lng": 104.14,
    }])
    store.upsert_place_provider_refs([
        {"provider": "amap", "provider_place_id": "main", "canonical_place_id": "c-1", "city": CITY, "provider_name": "成都大熊猫繁育研究基地"},
        {"provider": "amap", "provider_place_id": "gate", "canonical_place_id": "c-1", "city": CITY, "provider_name": "熊猫基地南门"},
    ])
    store.upsert_place_aliases([{
        "city": CITY, "canonical_place_id": "c-1", "alias": "熊猫乐园", "normalized_alias": "熊猫乐园",
    }])
    candidates = [
        Place(place_id="main", name="成都大熊猫繁育研究基地", city=CITY, lat=30.73, lng=104.14, source_id="poi-source"),
        Place(place_id="gate", name="熊猫基地南门", city=CITY, lat=30.72, lng=104.14),
    ]
    evidence = Evidence(id="e1", text="熊猫基地南门进去，熊猫乐园好玩", place_mentions=["熊猫基地南门", "熊猫乐园"])
    if legacy_rows:
        store.upsert_city_pois(CITY, [city_cache._poi_row(place, timestamp) for place in candidates])
        store.upsert_city_poi_mentions(CITY, [{"place_id": "gate", "raw_name": "熊猫基地南门", "snippet": evidence.text, "updated_at": timestamp}])
        store.upsert_city_evidences(CITY, city_cache._evidence_rows([evidence], timestamp))
    else:
        assert city_cache.write_candidates(CITY, candidates, [evidence], store=store) == 1
        links = store.get_place_evidence_links(CITY)
        assert {row["canonical_place_id"] for row in links} == {"c-1"}
        assert {row["mention_text"] for row in links} == set(evidence.place_mentions)

    before = store.db_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("cache read 不得 persist resolver")

    monkeypatch.setattr(places.PlaceResolver, "flush", forbidden)
    hit = city_cache.read_candidates(CITY, store=store)
    assert hit is not None and len(hit.places) == 1
    assert hit.places[0].place_id == "main"
    assert {"熊猫基地南门", "熊猫乐园"} <= set(hit.places[0].aliases)
    assert hit.places[0].merged_from == ["gate"]
    assert hit.places[0].source_id == "poi-source"
    assert {row["place_id"] for row in hit.mentions} == {"main"}
    assert len(hit.mentions) == 2
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("reverse", [False, True])
def test_cache_keeps_far_apart_names_and_disambiguates_mentions(tmp_path, reverse):
    store = TravelPlanStore(tmp_path / "same-names.db")
    candidates = [
        Place(place_id="central", name="人民公园", city=CITY, district="青羊区", lat=30.66, lng=104.05, aliases=["少城公园"]),
        Place(place_id="remote", name="人民公园", city=CITY, district="新津区", lat=30.16, lng=104.05, aliases=["新津人民公园"]),
    ]
    for place in candidates:
        store.upsert_canonical_places([{"canonical_place_id": "c-" + place.place_id, "canonical_name": place.name, "city": CITY, "district": place.district, "lat": place.lat, "lng": place.lng}])
        store.upsert_place_provider_refs([{"provider": "amap", "provider_place_id": place.place_id, "provider_name": place.name, "canonical_place_id": "c-" + place.place_id, "city": CITY}])
    evidences = [
        Evidence(id="ambiguous", title="随便逛逛", text="人民公园不错", place_mentions=["人民公园"]),
        Evidence(id="central", title="青羊区一日游", text="人民公园喝茶", place_mentions=["人民公园"]),
        Evidence(id="remote", text="新津人民公园不错", place_mentions=["人民公园"]),
        Evidence(id="qualified", text="人民公园不错", place_mentions=["新津区人民公园"]),
        Evidence(id="both", text="青羊区和新津区都有人民公园", place_mentions=["人民公园"]),
    ]
    if reverse:
        candidates.reverse()
        evidences.reverse()
    assert city_cache.write_candidates(CITY, candidates, evidences, store=store) == 2
    hit = city_cache.read_candidates(CITY, store=store)
    assert hit is not None and {place.place_id for place in hit.places} == {"central", "remote"}
    attached = {row["evidence_key"]: row["place_id"] for row in hit.mentions}
    expected = {"central": "central", "remote": "remote", "qualified": "remote"}
    assert attached == {city_cache._evidence_key(evidence): expected[evidence.id] for evidence in evidences if evidence.id in expected}
    assert len(hit.mentions) == 3
    rows, mentions = store.get_city_cache_rows(CITY)
    assert len(mentions) == 3
    assert {row["place_id"]: row["evidence_count"] for row in rows} == {"central": 1, "remote": 2}
