"""地点身份回归：只用内存和 tmp_path SQLite，不接触默认 Store / Provider。"""

from itertools import permutations

import pytest

from app import places, planner
from app.models import Place
from app.store import TravelPlanStore


@pytest.fixture
def store(tmp_path):
    db = TravelPlanStore(tmp_path / "identity.sqlite")
    assert db.backend_name == "sqlite"
    yield db
    db.close()


def poi(poi_id, name="人民公园", **overrides):
    values = dict(
        provider="amap", provider_place_id=poi_id, name=name,
        city="成都", lat=30.65, lng=104.06, amap_type="风景名胜;公园广场",
    )
    values.update(overrides)
    return places.PoiRecord(**values)


def place(place_id, name="人民公园", **overrides):
    values = dict(place_id=place_id, name=name, city="成都", lat=30.65, lng=104.06)
    values.update(overrides)
    return Place(**values)


@pytest.mark.parametrize("reload", [False, True])
@pytest.mark.parametrize("old_name,new_name", [
    ("人民公园", "成都市人民公园"),
    ("成都大熊猫繁育研究基地", "熊猫基地"),
    ("人民公园", "人民公園"),
])
def test_new_provider_id_reuses_canonical_across_batches(store, reload, old_name, new_name):
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest([poi("old", old_name)])
    canonical_id = next(iter(resolver.entities))
    resolver.flush()
    if reload:
        resolver = places.PlaceResolver(store, city="成都")
    result = resolver.ingest([poi("new", new_name, lat=30.6503, lng=104.0603)])
    assert result.created == 0
    assert result.merged == 1
    assert list(resolver.entities) == [canonical_id]
    assert result.places[0].name == old_name
    assert result.places[0].place_id == "old"
    assert result.places[0].merged_from == ["new"]
    resolver.flush()
    fresh = places.PlaceResolver(store, city="成都").load()
    assert list(fresh.entities) == [canonical_id]
    assert {ref.provider_place_id for ref in fresh.entities[canonical_id].refs} == {"old", "new"}


def test_loaded_aliases_and_provider_names_are_restored(store):
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest([poi("old", "成都大熊猫繁育研究基地"), poi("new", "熊猫基地")])
    canonical_id = next(iter(resolver.entities))
    resolver.flush()
    store.upsert_place_aliases([dict(
        city="成都", normalized_alias="熊猫乐园", alias="熊猫乐园",
        canonical_place_id=canonical_id,
    )])
    fresh = places.PlaceResolver(store, city="成都")
    result = fresh.resolve_query("熊猫乐园")
    assert result.action == "alias_hit"
    assert {"熊猫乐园", "熊猫基地"} <= set(result.places[0].aliases)
    assert {"熊猫乐园", "熊猫基地"} <= set(fresh.alias_names_of("old"))
    fresh.ingest([poi("third", "熊猫乐园", lat=30.6502)])
    assert len(fresh.entities) == 1
    fresh.flush()
    assert "熊猫乐园" in places.PlaceResolver(store, city="成都").alias_names_of("third")


@pytest.mark.parametrize("via_refs", [False, True])
def test_ambiguous_alias_is_not_an_arbitrary_hit_before_or_after_reload(store, via_refs):
    resolver = places.PlaceResolver(store, city="成都")
    left = "青羊人民公园" if via_refs else "人民公园"
    right = "简阳人民公园" if via_refs else "人民公园"
    resolver.ingest([
        poi("qingyang", left, district="青羊区", address="青羊路1号"),
        poi("jianyang", right, lat=30.39, lng=104.55, district="简阳市", address="简阳路1号"),
    ])
    if via_refs:
        resolver.ingest([
            poi("alias-q", district="青羊区", address="青羊路1号"),
            poi("alias-j", lat=30.39, lng=104.55, district="简阳市", address="简阳路1号"),
        ])
    assert len(resolver.entities) == 2
    resolver.record_query("人民公园", provider_result_ids=["qingyang"])
    resolver.flush()
    # 即使 DB 只能存一个别名归属、旧 query cache 只有一个候选，也不能认定它是唯一地点。
    for current in (resolver, places.PlaceResolver(store, city="成都")):
        resolution = current.resolve_query("人民公园")
        assert resolution.action == "miss"
        assert "歧义" in resolution.reason
        assert current.plan_queries(["人民公园"]) == (["人民公园"], [])
        assert len(current.entities) == 2


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_resolver_missing_coordinate_bridge_cannot_join_far_entities(order):
    records = [poi("near"), poi("unknown", lat=None, lng=None), poi("far", lat=30.39, lng=104.55)]
    resolver = places.PlaceResolver(None, city="成都")
    resolver.ingest([records[index] for index in order])
    assert len(resolver.entities) == 2
    for entity in resolver.entities.values():
        assert not {"near", "far"} <= {ref.provider_place_id for ref in entity.refs}


def test_new_cluster_cannot_bridge_a_loaded_entity(store):
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest([poi("old")])
    canonical_id = next(iter(resolver.entities))
    resolver.flush()
    fresh = places.PlaceResolver(store, city="成都")
    fresh.ingest([poi("unknown", lat=None, lng=None), poi("far", lat=30.39, lng=104.55)])
    assert len(fresh.entities) == 2
    assert [ref.provider_place_id for ref in fresh.entities[canonical_id].refs] == ["old"]


def test_existing_nonprimary_reference_also_blocks_conflicting_merge(store):
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest([poi("old", lat=30.65), poi("old-edge", lat=30.66)])
    assert len(resolver.entities) == 1
    resolver.flush()
    fresh = places.PlaceResolver(store, city="成都")
    # 新记录离代表点约 1.1km，但离同实体中的另一个引用约 2.2km。
    fresh.ingest([poi("new", lat=30.64)])
    assert len(fresh.entities) == 2


def test_repeated_reference_is_idempotent_and_preserves_detail(store):
    resolver = places.PlaceResolver(store, city="成都")
    detailed = poi("same", address="青华路37号")
    detailed.opening_hours = "09:00-18:00"
    resolver.ingest([detailed])
    resolver.flush()
    fresh = places.PlaceResolver(store, city="成都")
    for _ in range(3):
        result = fresh.ingest([poi("same", "成都市人民公园", lat=None, lng=None)])
        assert result.reused_refs == 1
        assert result.places[0].merged_from == []
    entity = next(iter(fresh.entities.values()))
    assert len(entity.refs) == 1
    assert entity.primary_ref.coords == (30.65, 104.06)
    assert entity.primary_ref.address == "青华路37号"
    assert entity.primary_ref.opening_hours == "09:00-18:00"
    assert fresh.flush()["refs"] == 1
    assert len(store.get_place_provider_refs("成都")) == 1


def test_cross_batch_branches_and_other_cities_do_not_reuse_identity(store):
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest([poi("old", "海底捞火锅春熙路店", amap_type="餐饮服务")])
    old_id = next(iter(resolver.entities))
    resolver.flush()
    fresh = places.PlaceResolver(store, city="成都")
    result = fresh.ingest([
        poi("branch", "海底捞火锅太古里店", amap_type="餐饮服务", lat=30.6502),
        poi("other-city", "海底捞火锅春熙路店", city="乐山市"),
    ])
    assert len(fresh.entities) == 2
    assert [ref.provider_place_id for ref in fresh.entities[old_id].refs] == ["old"]
    assert [item["place_id"] for item in result.dropped] == ["other-city"]


@pytest.mark.parametrize("city", [None, "成都", "成都市"])
def test_administrative_prefix_and_controlled_traditional_variants(city):
    assert planner.normalize_place_name("成都市人民公園", city) == "人民公园"
    assert planner.normalize_place_name("成都市火锅") == "成都市火锅"
    assert planner.normalize_place_name("皇后公园") != planner.normalize_place_name("皇後公园")
    assert planner.normalize_place_name("里山公园") != planner.normalize_place_name("裏山公园")


@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("conflict", ["distance", "city", "branch"])
def test_planner_cluster_conflicts_cannot_be_bridged(order, conflict):
    if conflict == "distance":
        records = [place("near"), place("unknown", lat=None, lng=None), place("far", lat=30.39, lng=104.55)]
    elif conflict == "city":
        records = [place("near"), place("unknown", city=None), place("far", city="乐山")]
    else:
        records = [
            place("near", "海底捞火锅春熙路店", aliases=["海底捞"]),
            place("unknown", "海底捞"),
            place("far", "海底捞火锅太古里店", aliases=["海底捞"]),
        ]
    merged, _ = planner.dedupe_places([records[index] for index in order])
    assert len(merged) == 2
    for item in merged:
        assert not {"near", "far"} <= {item.place_id, *item.merged_from}


@pytest.mark.parametrize("signal", ["same_address", "direct_alias", "shared_alias", "normalized_name"])
@pytest.mark.parametrize("conflict", ["distance", "city", "branch"])
def test_all_weak_signals_obey_shared_conflict_guards(signal, conflict):
    if signal == "same_address":
        left = place("left", "明婷饭店", address="人民路1号", lat=None, lng=None)
        right = place("right", "明婷小馆", address="人民路1号", lat=None, lng=None)
    elif signal == "direct_alias":
        left = place("left", "甲天下", aliases=["乙家"], lat=None, lng=None)
        right = place("right", "乙家", lat=None, lng=None)
    elif signal == "shared_alias":
        left = place("left", "甲天下", aliases=["春熙路老店"], lat=None, lng=None)
        right = place("right", "乙家", aliases=["春熙路老店"], lat=None, lng=None)
    else:
        left, right = place("left", lat=None, lng=None), place("right", lat=None, lng=None)
    assert planner._duplicate_signal(left, right)[0]
    if conflict == "distance":
        left.lat, left.lng = 30.65, 104.06
        right.lat, right.lng = 30.39, 104.55
    elif conflict == "city":
        right.city = "乐山市"
    else:
        left.name, right.name = "海底捞火锅春熙路店", "海底捞火锅太古里店"
        left.aliases, right.aliases = [right.name, "海底捞"], ["海底捞"]
        left.address = right.address = "同一个商场"
    assert planner._duplicate_signal(left, right)[0] is None
    merged, decisions = planner.dedupe_places([left, right])
    assert {item.place_id for item in merged} == {"left", "right"}
    assert not decisions


def test_repeated_dedupe_preserves_both_provenance_chains_without_mutating_input():
    first = place("A", merged_from=["A-old", "shared"], amap_verified=True)
    second = place("B", merged_from=["B-old", "shared", "A"], aliases=["人民公園"])
    original = [first.model_dump(), second.model_dump()]
    merged, _ = planner.dedupe_places([first, second])
    assert merged[0].place_id == "A"
    assert merged[0].merged_from == ["A-old", "B", "B-old", "shared"]
    again, decisions = planner.dedupe_places(merged)
    assert [item.model_dump() for item in again] == [item.model_dump() for item in merged]
    assert not decisions
    assert [first.model_dump(), second.model_dump()] == original


def test_same_provider_id_still_wins_but_cannot_bridge_an_unrelated_id():
    merged, _ = planner.dedupe_places([
        place("same"), place("same", lat=30.39, lng=104.55),
        place("other", lat=None, lng=None, city="乐山市"),
    ])
    assert {item.place_id for item in merged} == {"same", "other"}


@pytest.mark.parametrize("reload", [False, True])
def test_district_conflict_without_coordinates_is_not_a_name_match(store, reload):
    records = [poi("qingyang", district="青羊区", lat=None, lng=None),
               poi("jianyang", district="简阳市", lat=None, lng=None)]
    resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest(records[:1])
    if reload:
        resolver.flush()
        resolver = places.PlaceResolver(store, city="成都")
    resolver.ingest(records[1:])
    assert len(resolver.entities) == 2
    merged, _ = planner.dedupe_places([
        place("qingyang", district="青羊区", lat=None, lng=None),
        place("jianyang", district="简阳市", lat=None, lng=None),
    ])
    assert len(merged) == 2
