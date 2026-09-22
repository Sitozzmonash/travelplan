"""A 线：Canonical Place 实体层与 Entity Resolver 的离线契约测试。

覆盖改造方案 §22 的 6 条验收 Case，外加线上真实出现过的两种脏数据（成都那 20 条
候选里的子设施扎堆与跨市越界）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import places
from app.models import Place, utcnow
from app.store import TravelPlanStore

CITY = "成都"


def poi(
    poi_id: str,
    name: str,
    *,
    amap_type: str = "",
    lat: float | None = 30.65,
    lng: float | None = 104.06,
    address: str | None = None,
    city: str | None = CITY,
    district: str | None = None,
    business_area: str | None = None,
    order: int = 0,
) -> places.PoiRecord:
    return places.PoiRecord(
        provider="amap",
        provider_place_id=poi_id,
        name=name,
        amap_type=amap_type,
        lat=lat,
        lng=lng,
        address=address,
        city=city,
        district=district,
        business_area=business_area,
        order=order,
    )


def names(outcome: places.IngestOutcome) -> list[str]:
    return [place.name for place in outcome.places]


# ======================================================================
# 文本 / 设施判定
# ======================================================================


class TestFacilityDetection:
    @pytest.mark.parametrize(
        "name,amap_type,expected",
        [
            ("杜甫草堂(地铁站)", "交通设施服务;地铁站;地铁站", "metro"),
            ("成都杜甫草堂博物馆正门售票处", "", "ticket_office"),
            ("成都大熊猫繁育研究基地迎迎停车场", "交通设施服务;停车场;公共停车场", "parking"),
            ("成都大熊猫繁育研究基地游客中心B区", "生活服务;信息咨询中心;服务中心", "service_center"),
            ("成都宽窄巷子桔子水晶酒店", "住宿服务;宾馆酒店", "hotel"),
            ("宽窄巷子(B口)", "", "entrance"),
            ("熊猫基地西门游客中心", "", "service_center"),
        ],
    )
    def test_known_facilities_are_detected(self, name, amap_type, expected):
        assert places.facility_kind(name, amap_type) == expected

    @pytest.mark.parametrize(
        "name,amap_type",
        [
            ("成都大熊猫繁育研究基地", "风景名胜;旅游景点;旅游景点"),
            ("成都杜甫草堂博物馆", "科教文化服务;博物馆;博物馆"),
            ("熊猫塔", "风景名胜;旅游景点;旅游景点"),
            ("宽窄巷子步行街", "购物服务;商业街;步行街"),
            ("蜀大侠火锅春熙路店", "餐饮服务;中餐厅;火锅店"),
        ],
    )
    def test_real_places_are_not_facilities(self, name, amap_type):
        assert places.facility_kind(name, amap_type) is None

    @pytest.mark.parametrize(
        "amap_type",
        ["地名地址信息;地名地址信息;普通地名", "商务住宅;住宅区;住宅小区"],
    )
    def test_non_destination_types_are_not_candidates(self, amap_type):
        """地址桩（"窄巷子"）与住宅小区（"锦里苑"）都从真实候选里捞出来过。"""

        assert places.non_destination_reason(amap_type)
        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "宽窄巷子景区", amap_type="购物服务;特色商业街"),
                poi("P2", "窄巷子", amap_type=amap_type, order=1),
            ]
        )
        assert names(outcome) == ["宽窄巷子景区"]
        assert {item["reason"] for item in outcome.dropped} == {"not_destination"}

    def test_type_is_trusted_over_ambiguous_name(self):
        """"草"" 这种单字不再把地铁站"救"成景点 —— 高德 type 是结构化的，优先信它。"""

        assert places.facility_kind("杜甫草堂(地铁站)", "交通设施服务;地铁站") == "metro"


class TestBranchSignature:
    def test_branches_of_same_brand_have_different_signatures(self):
        left = places.branch_signature("蜀大侠火锅春熙路店")
        right = places.branch_signature("蜀大侠火锅太古里店")
        assert left and right and left != right
        assert places.branch_conflict("蜀大侠火锅春熙路店", "蜀大侠火锅太古里店") is True

    def test_same_branch_is_not_a_conflict(self):
        assert places.branch_conflict("星巴克(成都春熙路店)", "星巴克春熙路店") is False

    def test_business_words_ending_with_dian_are_not_branches(self):
        assert places.branch_signature("桔子水晶酒店") == ""
        assert places.branch_signature("明婷饭店") == ""


# ======================================================================
# §22 Case 1 / 2：同一实体的多种写法
# ======================================================================


class TestCase1Aliases:
    def test_three_writings_become_one_entity_with_aliases(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        resolver = places.PlaceResolver(store, city=CITY)
        outcome = resolver.ingest(
            [
                poi("B1", "成都杜甫草堂博物馆", amap_type="科教文化服务;博物馆", address="青华路37号"),
                poi("B2", "杜甫草堂景区", amap_type="风景名胜;旅游景点", address="青华路37号", order=1),
                poi("B3", "成都杜甫草堂", amap_type="风景名胜;旅游景点", address="青华路37号", order=2),
            ]
        )
        assert len(outcome.places) == 1
        place = outcome.places[0]
        assert place.name == "成都杜甫草堂博物馆"
        assert len(place.merged_from) == 2
        assert "杜甫草堂景区" in place.aliases

        resolver.flush()
        rows = store.get_canonical_places(CITY)
        assert len(rows) == 1
        refs = store.get_place_provider_refs(CITY)
        assert {row["provider_place_id"] for row in refs} == {"B1", "B2", "B3"}
        # 别名索引里应能查到三种写法（归一化后）。
        alias_keys = {row["normalized_alias"] for row in store.get_place_aliases(CITY)}
        assert "杜甫草堂" in alias_keys


# ======================================================================
# §22 Case 2：熊猫基地 —— 多信号一致才合并，且不依赖高德 ID 相同
# ======================================================================


class TestCase2Merge:
    def test_similar_names_merge_on_multi_signal(self):
        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "成都大熊猫繁育研究基地", amap_type="风景名胜;旅游景点", lat=30.7333, lng=104.1467),
                poi(
                    "P2", "熊猫基地", amap_type="风景名胜;旅游景点",
                    lat=30.7337, lng=104.1470, address="熊猫大道1375号", order=1,
                ),
            ]
        )
        assert len(outcome.places) == 1

    def test_same_name_far_apart_keeps_two_entities(self):
        """§9.3 冲突信号：名字像但坐标相距 8km —— 宁可留两个实体。"""

        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "星火火锅", lat=30.65, lng=104.06),
                poi("P2", "星火锅", lat=30.72, lng=104.06, order=1),
            ]
        )
        assert len(outcome.places) == 2


# ======================================================================
# §22 Case 3：母体 / 子设施
# ======================================================================


class TestCase3ParentChild:
    def test_gate_and_center_fold_into_parent(self):
        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "成都大熊猫繁育研究基地", amap_type="风景名胜;旅游景点", lat=30.7333, lng=104.1467),
                poi(
                    "P2", "熊猫基地南门", amap_type="交通设施服务;出入口",
                    lat=30.7340, lng=104.1470, order=1,
                ),
                poi(
                    "P3", "熊猫基地游客中心", amap_type="生活服务;信息咨询中心;服务中心",
                    lat=30.7338, lng=104.1469, order=2,
                ),
            ]
        )
        assert names(outcome) == ["成都大熊猫繁育研究基地"]
        folded = {item["name"] for item in outcome.folded}
        assert folded == {"熊猫基地南门", "熊猫基地游客中心"}

    def test_facility_never_becomes_a_standalone_candidate(self):
        """停车场 / 地铁站 / 酒店一个都不进候选，并且各自出现在该出现的地方。"""

        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "成都大熊猫繁育研究基地", amap_type="风景名胜;旅游景点"),
                poi("P2", "成都大熊猫繁育研究基地迎迎停车场", amap_type="交通设施服务;停车场", order=1),
                poi("P3", "杜甫草堂(地铁站)", amap_type="交通设施服务;地铁站", order=2),
                poi("P4", "成都宽窄巷子桔子水晶酒店", amap_type="住宿服务;宾馆酒店", order=3),
            ]
        )
        assert names(outcome) == ["成都大熊猫繁育研究基地"]
        # 停车场认得出母体 → 挂成子设施；地铁站 / 酒店找不到母体 → 记进 dropped。
        assert {item["name"] for item in outcome.folded} == {"成都大熊猫繁育研究基地迎迎停车场"}
        dropped_ids = {item["place_id"] for item in outcome.dropped}
        assert {"P3", "P4"} <= dropped_ids
        # 子设施的信息没有丢：名字挂在母体的别名上。
        assert "成都大熊猫繁育研究基地迎迎停车场" in outcome.places[0].aliases


# ======================================================================
# §22 Case 4：连锁分店
# ======================================================================


class TestCase4ChainBranches:
    def test_two_branches_stay_two_places_even_when_close(self):
        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("C1", "海底捞火锅春熙路店", amap_type="餐饮服务;火锅店", lat=30.657, lng=104.081),
                poi("C2", "海底捞火锅太古里店", amap_type="餐饮服务;火锅店", lat=30.6574, lng=104.0813, order=1),
            ]
        )
        assert len(outcome.places) == 2


# ======================================================================
# §22 Case 5：Query Cache / Alias 命中 → 不再打 Provider
# ======================================================================


class TestCase5Reuse:
    def test_alias_hit_skips_provider(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        first = places.PlaceResolver(store, city=CITY, run_id="run-1")
        first.ingest([poi("B1", "成都杜甫草堂博物馆", amap_type="科教文化服务;博物馆", address="青华路37号")])
        first.record_query("杜甫草堂", provider_result_ids=["B1"])
        first.flush()

        second = places.PlaceResolver(store, city=CITY, run_id="run-2")
        to_search, hits = second.plan_queries(["杜甫草堂"])
        assert to_search == []
        assert len(hits) == 1 and hits[0].action == "alias_hit"
        assert hits[0].places[0].place_id == "B1"

    def test_query_cache_hit_skips_provider_even_without_alias(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        first = places.PlaceResolver(store, city=CITY, run_id="run-1")
        first.ingest([poi("B1", "某小店", amap_type="餐饮服务;中餐厅")])
        # 故意只登记 query，不让"串串香"成为该实体的别名。
        first.record_query("串串香", provider_result_ids=["B1"])
        first.flush()

        second = places.PlaceResolver(store, city=CITY, run_id="run-2")
        to_search, hits = second.plan_queries(["串串香"])
        assert to_search == []
        assert hits[0].action == "query_cache_hit"
        assert hits[0].places[0].name == "某小店"

    def test_cache_does_not_invent_an_entity(self, tmp_path):
        """缓存里指向的实体已经不在库里时按未命中处理（方案 §14）。"""

        store = TravelPlanStore(tmp_path / "places.db")
        store.upsert_poi_query_cache(
            CITY,
            [
                {
                    "query_type": "search_poi",
                    "normalized_query": places.place_key("串串香", CITY),
                    "matched_place_ids": ["place_canonical_gone"],
                    "fetched_at": utcnow().isoformat(),
                }
            ],
        )
        resolver = places.PlaceResolver(store, city=CITY)
        to_search, hits = resolver.plan_queries(["串串香"])
        assert to_search == ["串串香"] and hits == []

    def test_expired_cache_falls_back_to_search(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        first = places.PlaceResolver(store, city=CITY, run_id="run-1")
        first.ingest([poi("B1", "某小店")])
        first.record_query("串串香", provider_result_ids=["B1"])
        first.flush()
        stale = utcnow() + timedelta(days=400)
        resolver = places.PlaceResolver(store, city=CITY, now=stale)
        to_search, hits = resolver.plan_queries(["串串香"])
        assert to_search == ["串串香"] and hits == []


# ======================================================================
# §22 Case 6 与地理围栏：预热数据被直接复用 / 邻市不串味
# ======================================================================


class TestCase6PrefetchAndFence:
    def test_second_session_reuses_prefetched_entity(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        warm = places.PlaceResolver(store, city=CITY, run_id="preheat")
        warm.ingest([poi("B1", "宽窄巷子", amap_type="购物服务;商业街", address="长顺上街")])
        warm.record_query("宽窄巷子", provider_result_ids=["B1"])
        warm.flush()

        live = places.PlaceResolver(store, city=CITY, run_id="run-1")
        to_search, hits = live.plan_queries(["宽窄巷子", "宽窄巷子景区"])
        # 两种写法归一化后都是"宽窄巷子"，别名索引一次命中，一个 Provider 都不用打。
        assert to_search == []
        assert [hit.action for hit in hits] == ["alias_hit", "alias_hit"]

    def test_out_of_city_poi_is_dropped_with_reason(self):
        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest(
            [
                poi("P1", "宽窄巷子", amap_type="购物服务;商业街"),
                poi("P2", "乐山大佛", amap_type="风景名胜;旅游景点", city="乐山市", order=1),
            ]
        )
        assert names(outcome) == ["宽窄巷子"]
        assert outcome.dropped[0]["reason"] == "out_of_city"
        assert "乐山" in outcome.dropped[0]["detail"]

    def test_missing_city_is_kept(self):
        """Provider 没报城市时不删：拿不到字段不等于结果无关。"""

        resolver = places.PlaceResolver(None, city=CITY)
        outcome = resolver.ingest([poi("P1", "宽窄巷子", city=None)])
        assert names(outcome) == ["宽窄巷子"]


# ======================================================================
# 留痕 / 明细复用 / 幂等
# ======================================================================


class TestTraceAndIdempotency:
    def test_decisions_are_traced(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        resolver = places.PlaceResolver(store, city=CITY, run_id="run-1")
        resolver.ingest(
            [
                poi("P1", "成都大熊猫繁育研究基地", amap_type="风景名胜;旅游景点", lat=30.7333, lng=104.1467),
                poi("P2", "熊猫基地南门", amap_type="交通设施服务;出入口", lat=30.734, lng=104.147, order=1),
                poi("P3", "乐山大佛", city="乐山市", order=2),
            ]
        )
        resolver.flush()
        traces = store.list_place_resolver_traces(run_id="run-1")
        actions = {row["action"] for row in traces}
        assert {"created", "folded"} <= actions

    def test_second_run_does_not_duplicate_entities(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        for run_id in ("run-1", "run-2"):
            resolver = places.PlaceResolver(store, city=CITY, run_id=run_id)
            resolver.ingest([poi("B1", "成都杜甫草堂博物馆", amap_type="科教文化服务;博物馆")])
            resolver.flush()
        assert len(store.get_canonical_places(CITY)) == 1
        assert len(store.get_place_provider_refs(CITY)) == 1

    def test_needs_detail_is_false_once_address_and_hours_are_known(self, tmp_path):
        store = TravelPlanStore(tmp_path / "places.db")
        resolver = places.PlaceResolver(store, city=CITY)
        resolver.ingest(
            [poi("B1", "成都杜甫草堂博物馆", amap_type="科教文化服务;博物馆", address="青华路37号")]
        )
        resolver.entities[
            places.canonical_id_for(CITY, "成都杜甫草堂博物馆", 30.65, 104.06)
        ].opening_hours = "09:00-18:00"
        resolver.flush()
        fresh = places.PlaceResolver(store, city=CITY)
        assert fresh.needs_detail("B1") is False
        assert fresh.needs_detail("B-unknown") is True

    def test_memory_only_mode_writes_nothing(self, tmp_path):
        resolver = places.PlaceResolver(None, city=CITY)
        resolver.ingest([poi("B1", "宽窄巷子")])
        assert resolver.flush()["persisted"] is False

    def test_purge_removes_canonical_layer_for_city(self, tmp_path):
        store = TravelPlanStore(tmp_path / "cache.db")
        resolver = places.PlaceResolver(store, city=CITY)
        resolver.ingest([poi("B1", "宽窄巷子")])
        resolver.record_query("宽窄巷子", provider_result_ids=["B1"])
        resolver.flush()
        store.upsert_city_pois(CITY, [{"place_id": "B1", "name": "宽窄巷子"}])

        removed = store.purge_city_cache(CITY)
        assert removed["city_pois"] == 1
        assert removed["canonical_places"] == 1
        assert store.get_canonical_places(CITY) == []
        assert store.get_place_provider_refs(CITY) == []
        assert store.get_poi_query_cache_rows(CITY) == []


def test_place_record_roundtrip_keeps_provider_fields():
    place = Place(
        place_id="B1", name="宽窄巷子", type="购物服务;商业街", lat=30.66, lng=104.05,
        address="长顺上街", city=CITY, district="青羊区", business_area="宽窄巷子",
    )
    record = places.PoiRecord.from_place(place, source_query="宽窄巷子")
    restored = record.to_place()
    assert restored.place_id == "B1"
    assert restored.type == "购物服务;商业街"
    assert restored.amap_verified is True


class TestCategoryPriority:
    def test_historic_street_tagged_as_shopping_is_still_an_attraction(self):
        """宽窄巷子 / 锦里的高德类型同时是 `购物服务;特色商业街` 与 `风景名胜`。

        "购物优先"会把它们归进"特色体验"池，而用户心里的分类是景点。
        """

        assert places.category_from_type("购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点") == "attraction"
        # 纯商业体仍然归购物（不含风景名胜标记）。
        assert places.category_from_type("购物服务;特色商业街;步行街") == "shopping"
        assert places.category_from_type("购物服务;商场;购物中心") == "shopping"
        # 餐饮优先于一切：高德给"餐饮相关场所"时不该被别的标记抢走。
        assert places.category_from_type("餐饮服务;中餐厅;火锅店") == "food"

    def test_pool_mapping_is_the_single_source_of_truth(self):
        from app.discovery import _POOL_BY_CATEGORY

        assert _POOL_BY_CATEGORY is places.POOL_BY_CATEGORY


class TestCountyLevelDestinationFence:
    """目的地是县 / 县级市时，高德 `cityname` 报的是地级市，不能据此判"越界"。

    线上真实事故：查"婺源"时高德回 `上饶市`、查"大理"时回 `大理白族自治州`，两座城市的
    候选被地理围栏**全部**清空，兜底又把未过滤的原始候选（含酒店 / 收费站）写进了缓存。
    """

    def test_prefecture_city_with_matching_district_is_kept(self):
        resolver = places.PlaceResolver(None, city="婺源")
        outcome = resolver.ingest(
            [
                poi("P1", "婺源篁岭景区", amap_type="风景名胜;旅游景点", city="上饶市", district="婺源县"),
                poi("P2", "婺源北收费站", amap_type="道路附属设施;收费站;收费站", city="上饶市", district="婺源县", order=1),
            ]
        )
        assert names(outcome) == ["婺源篁岭景区"]
        assert {item["reason"] for item in outcome.dropped} == {"orphan_facility"}

    def test_autonomous_prefecture_prefix_matches(self):
        assert places.city_related("大理白族自治州", "大理")
        assert places.city_related("大理市", "大理")
        assert not places.city_related("乐山市", "成都")

    def test_neighbour_city_still_fenced_out(self):
        resolver = places.PlaceResolver(None, city="成都")
        outcome = resolver.ingest(
            [
                poi("P1", "宽窄巷子", amap_type="购物服务;特色商业街", city="成都市", district="青羊区"),
                poi("P2", "乐山大佛", amap_type="风景名胜;旅游景点", city="乐山市", district="市中区", order=1),
            ]
        )
        assert names(outcome) == ["宽窄巷子"]
        assert outcome.dropped[0]["reason"] == "out_of_city"
