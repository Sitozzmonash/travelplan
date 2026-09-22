"""Discovery 的硬过滤、依赖调度及增量交接回归；只使用内存和假 Provider/LLM。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import threading

import pytest

from app import discovery, places
from app.models import Place, PreferenceProfile, TripIntent
from tests.fakes import FakeHub, FakeLLM


@pytest.fixture(autouse=True, scope="session")
def no_orphan_sweep_on_startup():
    """覆盖 conftest 的同名 fixture：纯 Discovery 测试不导入会初始化 DB 的 app.api。"""
    yield


@pytest.fixture
def intent():
    return TripIntent(
        destination=["成都"], origin="北京", start_date="2026-10-01",
        days=3, travelers=2, source="guided",
    )


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test gate was not released")


class ControlledHub(FakeHub):
    """仅替换 Provider 边界，保留真实 discovery / resolver / ledger / 聚合代码。"""

    def __init__(self, *, gates=None, raises=(), empty=()):
        super().__init__(store=None, empty=set(empty))
        self.gates = gates or {}
        self.raises = set(raises)
        self.attempts = []
        self.attempt_lock = threading.Lock()

    def before(self, name):
        with self.attempt_lock:
            self.attempts.append(name)
        if name in self.gates:
            self.gates[name].wait()
        if name in self.raises:
            raise RuntimeError(f"fake {name} failure")

    def search_trains(self, *args, **kwargs):
        self.before("transport")
        return super().search_trains(*args, **kwargs)

    def search_flights(self, *args, **kwargs):
        self.before("transport")
        return super().search_flights(*args, **kwargs)

    def search_hotels(self, *args, **kwargs):
        self.before("hotels")
        return super().search_hotels(*args, **kwargs)

    def search_xiaohongshu(self, *args, **kwargs):
        self.before("social")
        return super().search_xiaohongshu(*args, **kwargs)

    def search_douyin(self, *args, **kwargs):
        self.before("social")
        return super().search_douyin(*args, **kwargs)

    def web_search(self, *args, **kwargs):
        self.before("social")
        return super().web_search(*args, **kwargs)

    def search_poi(self, *args, **kwargs):
        self.before("places")
        return super().search_poi(*args, **kwargs)


class ControlledLLM(FakeLLM):
    def __init__(self, *, profile_gate=None, fail_profile=False):
        super().__init__()
        self.profile_gate = profile_gate
        self.fail_profile = fail_profile
        self.profile_attempts = 0

    def invoke_json(self, system, user, *, tag=""):
        if tag == "preference_profile":
            self.profile_attempts += 1
            if self.profile_gate is not None:
                self.profile_gate.wait()
            if self.fail_profile:
                raise RuntimeError("fake profile failure")
        return super().invoke_json(system, user, tag=tag)


def start_prefetch(hub, llm, intent, **kwargs):
    box = {}

    def run():
        try:
            box["result"] = discovery.prefetch(hub, llm, intent, **kwargs)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def finish_prefetch(thread, box):
    thread.join(8)
    assert not thread.is_alive(), "prefetch did not finish (possible pool dependency deadlock)"
    assert "error" not in box, box.get("error")
    return box["result"]


def rejected_places(reasons):
    examples = {
        "out_of_city": Place(place_id="outside", name="乐山大佛", city="乐山市", type="风景名胜"),
        "not_destination": Place(
            place_id="address", name="窄巷子", city="成都", type="地名地址信息;普通地名",
        ),
        "orphan_facility": Place(
            place_id="parking", name="熊猫基地停车场", city="成都", type="交通设施服务;停车场",
        ),
    }
    return [examples[reason] for reason in reasons]


@pytest.mark.parametrize("reasons", [
    ("out_of_city",), ("not_destination",), ("orphan_facility",),
    ("out_of_city", "not_destination", "orphan_facility"),
])
def test_all_hard_rejections_stay_empty(intent, reasons):
    class RejectedHub(FakeHub):
        def search_poi(self, *args, **kwargs):
            result = super().search_poi(*args, **kwargs)
            result.items = rejected_places(reasons)
            return result

    hub = RejectedHub(store=None)
    result = discovery.extract_place_candidates(hub, FakeLLM(), intent, [])
    assert not result.places
    assert {item["reason"] for item in result.resolver["dropped"]} == set(reasons)
    assert any("本次返回空候选" in note for note in result.degradations)
    assert "poi_detail" not in hub.calls
    if "out_of_city" in reasons:
        assert any("地理围栏" in note for note in result.notes)


def test_extra_places_cannot_revive_hard_rejections():
    result = discovery.resolve_pois(
        resolver=places.PlaceResolver(None, city="成都"), results={}, terms=[],
        extra_places=rejected_places(("out_of_city", "not_destination", "orphan_facility")),
    )
    assert result.places == []
    assert len(result.outcome.dropped) == 3


def test_clean_fallback_keeps_only_accepted_candidates():
    class EmptyVisibleResolver(places.PlaceResolver):
        def _visible_places(self):
            # 模拟收敛输出缺失；围栏、非目的地及设施过滤仍使用真实 ingest。
            return []

    clean = Place(place_id="park", name="人民公园", city="成都", type="风景名胜")
    result = discovery.resolve_pois(
        resolver=EmptyVisibleResolver(None, city="成都"), results={}, terms=[],
        extra_places=[*rejected_places(("out_of_city", "not_destination", "orphan_facility")), clean],
    )
    assert [place.place_id for place in result.places] == ["park"]
    assert len(result.outcome.dropped) == 3
    assert any("已通过硬过滤" in note for note in result.degradations)


@pytest.mark.parametrize("social_failure", [False, True])
def test_empty_or_failed_social_still_searches_keywords(intent, social_failure):
    hub = ControlledHub(
        empty={"search_xiaohongshu", "search_douyin", "web_search"},
        raises={"social"} if social_failure else set(),
    )
    llm = ControlledLLM()
    partials = []
    result = discovery.prefetch(hub, llm, intent, on_partial=partials.append)
    assert result["social"].evidences == []
    assert result["stages"]["social"]["status"] == ("FAILED" if social_failure else "EMPTY")
    assert ("social" in result["errors"]) == social_failure
    assert result["places"].places
    assert result["stages"]["places"]["status"] == "OK"
    assert any("只能来自关键词搜索" in note for note in result["places"].notes)
    assert any(s["stages"]["places"]["status"] == "OK" and s["places"].places for s in partials)
    queries = [entry["arguments"]["keywords"] for entry in hub.audit_entries() if entry["tool"] == "search_poi"]
    assert queries and len(queries) == len(set(queries))
    assert len(queries) == result["places"].keyword_hits
    assert Counter(llm.tags) == {"query_expansion": 1, "preference_profile": 1}


def test_places_partial_is_ready_while_transport_hotels_and_profile_block(intent):
    transport, hotels, profile = Gate(), Gate(), Gate()
    hub = ControlledHub(gates={"transport": transport, "hotels": hotels})
    llm = ControlledLLM(profile_gate=profile)
    places_ready = threading.Event()
    partials = []

    def on_partial(snapshot):
        partials.append(snapshot)
        if snapshot["stages"]["places"]["status"] == "OK":
            places_ready.set()

    thread, box = start_prefetch(hub, llm, intent, workers=3, on_partial=on_partial)
    try:
        assert transport.entered.wait(3)
        assert hotels.entered.wait(3)
        assert profile.entered.wait(3), "intent-only profile must start before providers finish"
        assert places_ready.wait(3), "places must not wait for transport/hotels/profile"
        partial = next(s for s in partials if s["stages"]["places"]["status"] == "OK")
        assert partial["stages"]["transport"]["status"] == "RUNNING"
        assert partial["stages"]["hotels"]["status"] == "RUNNING"
        assert partial["transport"] is None and partial["hotels"] is None
        assert partial["profile"] == PreferenceProfile.default_profile().model_dump(mode="json")
        assert partial["hotel_areas"] and any(partial["poi_pools"].values())
        assert partial["stages"]["places"]["resolver"] == partial["places"].resolver
        # 已完成地点可按既有 bundle contract 直接过户，不依赖 sessions/api 导入。
        bundle = discovery.PrefetchBundle.load(discovery.PrefetchBundle(
            places=partial["places"].places, evidences=partial["social"].evidences,
            discovery=partial["stages"], ledger=partial["ledger"],
            profile=partial["profile"], hotel_areas=partial["hotel_areas"], poi_pools=partial["poi_pools"],
        ).dump())
        assert bundle.places and bundle.discovery["places"]["status"] == "OK"
    finally:
        for gate in (transport, hotels, profile):
            gate.release.set()
        result = finish_prefetch(thread, box)
    assert result["transport"].all_options and result["hotels"].items
    assert llm.profile_attempts == 1
    assert len(partials) == 4


@pytest.mark.parametrize("social_failure", [False, True])
def test_one_worker_finishes_without_duplicate_work(intent, social_failure):
    hub = ControlledHub(raises={"social"} if social_failure else set())
    llm = ControlledLLM()
    thread, box = start_prefetch(hub, llm, intent, workers=1)
    result = finish_prefetch(thread, box)
    assert result["places"].places
    assert all(stage["status"] != "RUNNING" for stage in result["stages"].values())
    assert hub.attempts.count("transport") == 4
    assert hub.attempts.count("hotels") == 1
    assert hub.attempts.count("places") == result["places"].keyword_hits
    assert llm.tags.count("query_expansion") == 1
    assert llm.profile_attempts == 1
    assert not any(tag == "extract_places_retry" for tag in llm.tags)
    assert "duplicate_query" not in result["ledger"]


def test_prefetch_does_not_add_provider_or_llm_work(intent):
    from app.observability import CallLedger
    from app.profile import generate_preference_profile

    baseline_hub, baseline_llm = FakeHub(store=None), FakeLLM()
    ledger = CallLedger(scope="discovery")
    discovery.fetch_transport_candidates(baseline_hub, intent)
    discovery.fetch_hotel_candidates(baseline_hub, intent, pages=2)
    social = discovery.discover_social_evidence(baseline_hub, baseline_llm, intent, ledger=ledger)
    baseline_places = discovery.extract_place_candidates(
        baseline_hub, baseline_llm, intent, social.evidences, queries=social.queries, ledger=ledger,
    )
    generate_preference_profile(intent, baseline_llm)

    hub, llm = FakeHub(store=None), FakeLLM()
    partials = []
    result = discovery.prefetch(hub, llm, intent, hotel_pages=2, on_partial=partials.append)
    assert Counter(hub.calls) == Counter(baseline_hub.calls)
    assert Counter(llm.tags) == Counter(baseline_llm.tags)
    assert result["ledger"] == ledger.summary()
    assert [p.name for p in result["places"].places] == [p.name for p in baseline_places.places]
    assert len(partials) == 4


def test_failures_are_independent_and_profile_has_default(intent):
    hub = ControlledHub(raises={"transport", "hotels", "social"})
    llm = ControlledLLM(fail_profile=True)
    result = discovery.prefetch(hub, llm, intent)
    assert result["stages"]["transport"]["status"] == "EMPTY"
    assert result["transport"].degradations  # 交通内部的并发查询已独立捕获失败。
    assert result["stages"]["hotels"]["status"] == "FAILED"
    assert result["stages"]["social"]["status"] == "FAILED"
    assert set(result["errors"]) == {"hotels", "social"}
    assert result["stages"]["places"]["status"] == "OK"
    assert result["places"].places and result["poi_pools"]["attraction"]
    assert result["profile"] == PreferenceProfile.default_profile().model_dump(mode="json")
    assert llm.profile_attempts == 1


def test_place_failure_preserves_other_results_and_explains_empty_candidates(intent):
    class BrokenResolverStore:
        def __getattr__(self, name):
            raise RuntimeError("fake entity store failure")

    result = discovery.prefetch(FakeHub(store=None), FakeLLM(), intent, store=BrokenResolverStore())
    assert result["stages"]["places"]["status"] == "FAILED"
    assert "fake entity store failure" in result["errors"]["places"]
    assert not result["places"].places
    assert any("地点候选生成失败" in note for note in result["places"].degradations)
    for name in ("transport", "hotels", "social"):
        assert result["stages"][name]["status"] == "OK"


@pytest.mark.parametrize("with_failures", [False, True])
def test_partials_are_serial_cumulative_and_detached(intent, with_failures, monkeypatch):
    transport, hotels, social = Gate(), Gate(), Gate()
    hub = ControlledHub(
        gates={"transport": transport, "hotels": hotels, "social": social},
        raises={"hotels", "social"} if with_failures else set(),
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    premature_snapshot = threading.Event()
    callback_lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "calls": 0}
    delivered = []
    frozen = []
    aggregate = discovery.aggregate_candidate_metadata

    def observed_aggregate(*args, **kwargs):
        # 观察真实聚合入口：不能在等待回调锁时预先算一份将要过期的快照。
        if first_entered.is_set() and not release_first.is_set():
            premature_snapshot.set()
        return aggregate(*args, **kwargs)

    monkeypatch.setattr(discovery, "aggregate_candidate_metadata", observed_aggregate)

    def on_partial(snapshot):
        with callback_lock:
            state["active"] += 1
            state["calls"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            first = state["calls"] == 1
        try:
            if first:
                first_entered.set()
                assert release_first.wait(5)
            else:
                second_entered.set()
            delivered.append(snapshot)
            frozen.append(deepcopy(snapshot))
        finally:
            with callback_lock:
                state["active"] -= 1

    thread, box = start_prefetch(hub, FakeLLM(), intent, workers=3, on_partial=on_partial)
    try:
        assert all(g.entered.wait(3) for g in (transport, hotels, social))
        # 酒店先完成，但故意拖慢它的回调；其间让交通/社交完成并争用发布锁。
        hotels.release.set()
        assert first_entered.wait(3)
        transport.release.set()
        social.release.set()
        assert not premature_snapshot.wait(0.25), "snapshot was taken before acquiring callback lock"
        assert not second_entered.is_set(), "partial callbacks overlapped"
    finally:
        for gate in (transport, hotels, social):
            gate.release.set()
        release_first.set()
        result = finish_prefetch(thread, box)
    assert state["maximum"] == 1
    assert len(delivered) == 4
    previous_done = set()
    previous_errors = set()
    previous_ledger = {}
    for snapshot in [*delivered, result]:
        done = {name for name, stage in snapshot["stages"].items() if stage["status"] != "RUNNING"}
        assert previous_done <= done
        assert previous_errors <= set(snapshot["errors"])
        for marker, count in previous_ledger.items():
            assert snapshot["ledger"].get(marker, 0) >= count
        for name in done:
            assert snapshot["stages"][name]["finished_at"]
            assert snapshot["stages"][name]["duration_ms"] >= 0
        previous_done, previous_errors, previous_ledger = done, set(snapshot["errors"]), snapshot["ledger"]
    assert delivered == frozen, "later evidence extraction mutated an earlier snapshot"
    delivered[-1]["places"].places.clear()
    delivered[-1]["stages"]["places"]["status"] = "RUNNING"
    assert result["places"].places and result["stages"]["places"]["status"] == "OK"


def test_callback_failure_does_not_stop_dependent_places(intent):
    calls = []

    def broken_callback(snapshot):
        calls.append(snapshot)
        raise RuntimeError("fake persistence failure")

    result = discovery.prefetch(FakeHub(store=None), FakeLLM(), intent, on_partial=broken_callback)
    assert len(calls) == 4
    assert result["places"].places
    assert result["errors"] == {}


def test_aggregate_metadata_is_deterministic_and_uses_explicit_default(intent):
    candidates = [Place(place_id="food", name="老火锅", type="餐饮", business_area="春熙路")]
    result = discovery.aggregate_candidate_metadata(intent, [], candidates)
    assert result == discovery.aggregate_candidate_metadata(intent, [], candidates)
    assert set(result) == {"profile", "hotel_areas", "poi_pools"}
    assert result["profile"] == PreferenceProfile.default_profile().model_dump(mode="json")
    assert result["hotel_areas"] == discovery.extract_hotel_areas(intent, [], candidates)
    assert result["poi_pools"] == discovery.split_poi_pools(candidates)
    profile = PreferenceProfile.from_llm({"travel_style": "food", "hotel_area": {"food_density": 1.0}})
    personalized = discovery.aggregate_candidate_metadata(intent, [], candidates, profile=profile)
    assert personalized["profile"] == profile.model_dump(mode="json")
    assert personalized["hotel_areas"] == discovery.extract_hotel_areas(intent, [], candidates, profile=profile)


def test_aggregate_failure_does_not_erase_other_metadata(intent, monkeypatch):
    def broken_areas(*args, **kwargs):
        raise RuntimeError("fake aggregation failure")

    monkeypatch.setattr(discovery, "extract_hotel_areas", broken_areas)
    candidates = [Place(place_id="park", name="人民公园", type="风景名胜")]
    result = discovery.aggregate_candidate_metadata(intent, [], candidates)
    assert result["hotel_areas"] == []
    assert result["poi_pools"]["attraction"] == ["park"]
    assert result["profile"] == PreferenceProfile.default_profile().model_dump(mode="json")
