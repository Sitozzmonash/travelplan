"""纯离线原生工具对话；所有数据来自替身或 tmp_path SQLite。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app import city_cache, discovery, recommendations as rec
from app.llm import LLM
from app.models import Evidence, Place, TripIntent
from app.providers import ProviderResult


@pytest.fixture(autouse=True, scope="session")
def no_orphan_sweep_on_startup():
    """纯模块测试不导入 app.api，防止其初始化默认数据库。"""
    yield


def poi(pid="park", name="中心公园", *, district="中心区", area="中心街", lat=30.66, lng=104.07, kind="风景名胜;公园", **kwargs):
    return Place(place_id=pid, name=name, city="成都", district=district, business_area=area,
                 lat=lat, lng=lng, type=kind, amap_verified=True, source_id="poi-source", **kwargs)


def evidence(eid="db-guide", *, text="中心区的中心公园适合散步，推荐住在中心街，出门即可步行前往中心公园。", url="https://guides.test/db", mentions=None):
    return Evidence(id=eid, text=text, title="城市游攻略", provider="xhs", source_url=url,
                    place_mentions=["中心公园"] if mentions is None else mentions)


def hit(places=None, evidences=None, mentions=None, stale=False):
    return city_cache.CityCacheHit(city="成都", places=[poi()] if places is None else places,
        evidences=[evidence()] if evidences is None else evidences, mentions=mentions or [],
        updated_at="2026-09-22T00:00:00+00:00", stale=stale)


def call(name, args=None, cid=None):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid or name}])


def submit(pid="park", *, eid="db-guide", category="attraction", areas=None):
    categories = {key: [] for key in rec._CATEGORIES}
    if pid:
        categories[category].append({"place_id": pid, "category": category, "reason": "攻略推荐散步休闲", "evidence_ids": [eid]})
    return {"categories": categories, "hotel_areas": areas or [], "explanation": "依据本次数据库与网页攻略推荐；缺证据类别留空。"}


def area(name="中心街", pid="park", eid="db-guide", scope="city_center"):
    return {"name": name, "reason": "攻略建议住在此区域，可步行到关联活动点", "evidence_ids": [eid], "place_ids": [pid], "scope": scope}


class NativeModel:
    """推荐只能 bind_tools；JSON 仅给 discovery 的正文地点抽取。"""
    model_name = "offline-native"

    def __init__(self, steps, extraction=None):
        self.steps = list(steps)
        self.seen = []
        self.bindings = []
        self.extractions = []
        self.assertion_errors = []
        self.extraction = extraction

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        def invoke(messages):
            self.seen.append(list(messages))
            # 每个先前原生调用都必须有匹配的 ToolMessage，不接受拼文本伪装。
            pending = set()
            for message in messages:
                if isinstance(message, AIMessage):
                    pending.update(c["id"] for c in message.tool_calls)
                elif isinstance(message, ToolMessage):
                    assert message.tool_call_id in pending
                    pending.remove(message.tool_call_id)
                    json.loads(message.content)
            assert not pending
            if not self.steps:
                raise RuntimeError("script exhausted")
            step = self.steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(messages, tools) if callable(step) else step
        def checked(messages):
            try:
                return invoke(messages)
            except AssertionError as exc:
                self.assertion_errors.append(str(exc))
                raise
        return SimpleNamespace(invoke=checked)

    def invoke(self, messages):
        self.extractions.append(list(messages))
        if self.extraction is not None:
            return self.extraction(messages)
        return AIMessage(content='{"results": []}')


class Hub:
    def __init__(self, items=None, web_error=None, geocode=True, poi_results=None):
        self.items = [] if items is None else items
        self.web_error = web_error
        self.geo = geocode
        self.poi_results = poi_results or {}
        self.web_calls = []
        self.poi_calls = []
        self.geo_calls = []

    def web_search(self, query, *, max_results):
        self.web_calls.append((query, max_results))
        assert max_results == 5
        if self.web_error:
            raise self.web_error
        return ProviderResult(status="OK", items=self.items, provider="tavily")

    def geocode(self, address, city=None):
        self.geo_calls.append((address, city))
        return ProviderResult(status="OK" if self.geo else "UNAVAILABLE", items=[{"longitude": 104.07, "latitude": 30.66, "city": "成都"}] if self.geo else [])

    def search_poi(self, keywords, region, *, page_size):
        self.poi_calls.append((keywords, region, page_size))
        return ProviderResult(status="OK", items=self.poi_results.get(keywords, []), provider="amap")


@pytest.fixture
def cache(monkeypatch):
    state = {"hit": hit(), "calls": [], "error": None}
    def read(city, **kwargs):
        state["calls"].append((city, kwargs))
        assert kwargs["store"] is not None and kwargs["allow_stale"] is True
        if state["error"]:
            raise state["error"]
        return state["hit"]
    monkeypatch.setattr(city_cache, "read_candidates", read)
    monkeypatch.setattr(discovery, "prefetch", lambda *a, **k: pytest.fail("不能回退 discovery.prefetch 查询机酒"))
    monkeypatch.setattr(city_cache, "TravelPlanStore", lambda *a, **k: pytest.fail("不能隐式打开默认数据库"))
    return state


def run(model, hub=None, *, store=None, intent=None, progress=None):
    bundle = rec.recommend_guided(hub or Hub(), LLM(model=model), intent or TripIntent(destination=["成都"], days=4),
                                  store=object() if store is None else store, session_id="offline-session", on_progress=progress)
    assert not model.assertion_errors, "模型桩内断言失败不能被LLM降级掩盖"
    return bundle


def usual_steps(args):
    return [call("recall_city_guides"), call("search_web_guides", {"query": "成都 城市攻略"}), call("submit_recommendations", args)]


def test_real_tool_conversation_two_evidence_routes_and_recommended_only(cache):
    cache["hit"] = hit(places=[poi(), poi("unused", "城市博物馆", kind="博物馆")])
    web = {"title": "中心公园游记", "snippet": "中心公园有绿荫小路，适合城市散步。", "url": "https://guides.test/web"}
    progress = []
    model = NativeModel(usual_steps(submit(areas=[area()])))
    bundle = run(model, Hub([web]), progress=progress.append)
    assert bundle.discovery_status == "READY"
    assert [p.place_id for p in bundle.places] == ["park"]
    assert bundle.hotels == bundle.outbound == bundle.inbound == []
    assert len(bundle.extras["raw_candidates"]) == 2
    assert bundle.poi_pools == discovery.split_poi_pools(bundle.places)
    assert bundle.hotel_areas[0]["scope"] == "city_center"
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 2
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert len(bundle.extras["llm_calls"]) == 3
    assert len(cache["calls"]) == 1
    assert model.bindings[0][1] == {"tool_choice": "recall_city_guides"}
    offered = [{t["function"]["name"] for t in tools} for tools, _ in model.bindings]
    assert "submit_recommendations" not in offered[0] | offered[1]
    assert "submit_recommendations" in offered[2]
    messages = model.seen[2]
    results = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.name for m in results] == ["recall_city_guides", "search_web_guides"]
    assert json.loads(results[0].content)["evidences"][0]["text"].startswith("中心区")
    web_result = json.loads(results[1].content)
    assert web_result["items"] == [web]
    assert len(web_result["evidences"]) == 2
    assert web_result["candidates"][0]["district"] == "中心区"
    assert web_result["candidates"][0]["source_id"] == "poi-source"
    assert any(entry["tool"] == "submit_recommendations" and entry["status"] == "OK" for entry in progress)
    # 推荐定稿后立刻收口 places 阶段，正式 run 不再对同一批证据重复抽取。
    assert progress[-1]["tool"] == "finalize_places"
    assert progress[-1]["result_count"] == 1
    restored = discovery.PrefetchBundle.load(bundle.dump())
    assert restored.extras["recommended_cards"] == bundle.extras["recommended_cards"]


def test_parallel_reads_all_results_are_returned_before_submit(cache):
    both = AIMessage(content="", tool_calls=[
        {"name": "search_web_guides", "args": {"query": "成都攻略"}, "id": "w"},
        {"name": "recall_city_guides", "args": {}, "id": "r"}])
    model = NativeModel([both, call("submit_recommendations", submit())])
    bundle = run(model)
    assert bundle.discovery_status == "READY"
    assert {m.tool_call_id for m in model.seen[1] if isinstance(m, ToolMessage)} == {"r", "w"}


def test_direct_submit_cannot_skip_read_tools(cache):
    model = NativeModel([call("submit_recommendations", submit()), AIMessage(content="直接推荐")])
    hub = Hub()
    bundle = run(model, hub)
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert len(cache["calls"]) == len(hub.web_calls) == 1
    assert not any(t["tool"] == "submit_recommendations" and t["status"] == "OK" for t in bundle.extras["recommendation"]["tool_trace"])


@pytest.mark.parametrize("bad", ["unknown_id", "foreign_evidence", "closed", "facility", "wrong_shopping", "schema"])
def test_rejects_invalid_recommendations(cache, bad):
    args = submit()
    if bad == "unknown_id":
        args = submit("invented")
    elif bad == "foreign_evidence":
        args = submit(eid="not-this-run")
    elif bad == "closed":
        cache["hit"] = hit(places=[poi(opening_hours="暂停营业")])
    elif bad == "facility":
        cache["hit"] = hit(places=[poi(name="中心公园停车场", kind="交通设施服务;停车场")],
                           evidences=[evidence(mentions=["中心公园停车场"])])
    elif bad == "wrong_shopping":
        cache["hit"] = hit(places=[poi(name="茗香茶叶专卖店", kind="购物服务;专卖店;茶叶")],
                           evidences=[evidence(text="茗香茶叶专卖店出售礼品茶叶。", mentions=["茗香茶叶专卖店"])])
        args = submit(category="shopping")
    elif bad == "schema":
        args = {"places": ["park"]}
    model = NativeModel(usual_steps(args) + [RuntimeError("模型失败")])
    bundle = run(model)
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert any(t["tool"] == "submit_recommendations" and t["status"] == "ERROR" for t in bundle.extras["recommendation"]["tool_trace"])
    assert all(p.place_id != "invented" for p in bundle.places)
    if bad in {"closed", "facility", "wrong_shopping"}:
        assert bundle.places == []


def test_guide_closure_without_poi_status_is_rejected(cache):
    cache["hit"] = hit(evidences=[evidence(text="中心公园暂停开放，请不要前往。")])
    bundle = run(NativeModel(usual_steps(submit())))
    assert bundle.places == []
    assert bundle.discovery_status == "PARTIAL"


@pytest.mark.parametrize("scope", ["outskirts", "city_center"])
def test_normal_city_trip_never_defaults_to_remote_lodging(cache, scope):
    cache["hit"] = hit(places=[poi("remote", "山谷公园", district="大邑县", area="山谷镇", lat=30.6, lng=103.3)],
                       evidences=[evidence(text="山谷公园适合远郊徒步，推荐住在山谷镇，步行可达山谷公园。", mentions=["山谷公园"])])
    bad = submit("remote", areas=[area("山谷镇", "remote", scope=scope)])
    good = submit("remote")
    model = NativeModel(usual_steps(bad) + [call("submit_recommendations", good, cid="fixed")])
    bundle = run(model)
    assert bundle.hotel_areas == []
    assert [p.place_id for p in bundle.places] == ["remote"]
    assert "远郊" in bundle.extras["recommended_cards"][0]["reason"]
    assert bundle.discovery_status == "PARTIAL"
    errors = [json.loads(m.content) for m in model.seen[-1] if isinstance(m, ToolMessage) and m.name == "submit_recommendations"]
    assert errors[-1]["status"] == "ERROR"


def test_explicit_outskirts_stay_still_requires_evidence_and_activities(cache):
    cache["hit"] = hit(places=[poi("remote", "山谷公园", area="山谷镇", lat=30.6, lng=103.3)],
                       evidences=[evidence(text="推荐住在山谷镇，方便步行去山谷公园。", mentions=["山谷公园"])])
    model = NativeModel(usual_steps(submit("remote", areas=[area("山谷镇", "remote", scope="outskirts")])))
    bundle = run(model, intent=TripIntent(destination=["成都"], hotel_preferences=["山谷镇度假住宿"]))
    assert bundle.hotel_areas[0]["scope"] == "outskirts"


@pytest.mark.parametrize("mutation", ["no_stay_advice", "no_activity", "unknown_center", "density", "bad_scope", "scope_list"])
def test_lodging_needs_guide_and_geographic_activity_scope(cache, mutation):
    proposed = area()
    hub = Hub()
    if mutation == "no_stay_advice":
        cache["hit"] = hit(evidences=[evidence(text="中心街有很多POI，中心公园适合散步。")])
    elif mutation == "no_activity":
        proposed["place_ids"] = ["invented"]
    elif mutation == "unknown_center":
        hub = Hub(geocode=False)
    elif mutation == "density":
        proposed["reason"] = "POI密度高，100%的攻略在这里"
    elif mutation == "bad_scope":
        proposed["scope"] = "downtown"
    elif mutation == "scope_list":
        proposed["scope"] = ["city_center"]
    bundle = run(NativeModel(usual_steps(submit(areas=[proposed]))), hub)
    assert bundle.hotel_areas == []
    assert bundle.discovery_status == "PARTIAL"


@pytest.mark.parametrize("failure", [RuntimeError("model down"), AIMessage(content="推荐中心公园"),
    AIMessage(content='{"tool_calls":[{"name":"submit_recommendations"}]}'),
    AIMessage(content="", invalid_tool_calls=[{"id": "broken", "name": "recall_city_guides", "args": "{", "error": "invalid"}])])
def test_model_failure_is_honest_evidence_fallback(cache, failure):
    cache["hit"] = hit(places=[poi(), poi("far", "遥远公园", lat=31.5)], evidences=[evidence(mentions=["中心公园", "遥远公园"])])
    bundle = run(NativeModel([failure]))
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert [p.place_id for p in bundle.places] == ["park"]
    assert "非模型推荐" in bundle.extras["recommended_cards"][0]["reason"]
    assert bundle.hotel_areas == []


def test_web_failure_is_returned_to_model_and_not_ready(cache):
    model = NativeModel(usual_steps(submit()))
    bundle = run(model, Hub(web_error=RuntimeError("offline")))
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "llm"
    message = [m for m in model.seen[2] if isinstance(m, ToolMessage) and m.name == "search_web_guides"][0]
    assert json.loads(message.content)["status"] == "ERROR"


def test_both_tools_fail_and_no_evidence_is_empty_failed(cache):
    cache["error"] = RuntimeError("db offline")
    bundle = run(NativeModel([RuntimeError("model offline")]), Hub(web_error=RuntimeError("web offline")))
    assert bundle.discovery_status == "FAILED"
    assert bundle.places == bundle.hotel_areas == bundle.evidences == []
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"


def test_successful_reads_are_idempotently_replayed(cache):
    steps = [call("recall_city_guides", cid="r1"), call("recall_city_guides", cid="r2"),
             call("search_web_guides", {"query": "成都 攻略"}, cid="w1"),
             call("search_web_guides", {"query": "成都攻略"}, cid="w2"), call("submit_recommendations", submit())]
    model = NativeModel(steps)
    hub = Hub()
    bundle = run(model, hub)
    assert len(cache["calls"]) == len(hub.web_calls) == 1
    assert bundle.discovery_status == "READY"
    assert sum(t["status"] == "REPLAY" for t in bundle.extras["recommendation"]["tool_trace"]) == 2
    results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert results[0].content == results[1].content
    assert results[2].content == results[3].content


def test_tool_round_query_and_call_limits(cache):
    steps = [call("recall_city_guides")]
    steps.extend(call("search_web_guides", {"query": "成都攻略" + str(i)}, cid=f"w{i}") for i in range(10))
    hub = Hub()
    model = NativeModel(steps)
    bundle = run(model, hub)
    assert len(model.seen) == rec._MAX_ROUNDS
    assert len(hub.web_calls) == rec._MAX_WEB_QUERIES
    assert bundle.discovery_status == "PARTIAL"
    assert any(t["status"] == "ERROR" for t in bundle.extras["recommendation"]["tool_trace"])


def test_oversized_query_never_reaches_provider(cache):
    model = NativeModel([call("recall_city_guides"), call("search_web_guides", {"query": "x" * 121}), RuntimeError("stop")])
    hub = Hub()
    run(model, hub)
    assert all(len(query) <= 120 for query, _ in hub.web_calls)
    assert len(hub.web_calls) == 1  # 合法的确定性 fallback，而不是超长模型检索词


def test_evidence_count_independent_urls_mentions_not_merged_ids(cache):
    base = evidence()
    same_article = evidence("dup", url="https://guides.test/db?utm_source=other#frag")
    extra = evidence("second", url="https://guides.test/second")
    original = poi(merged_from=["old-1", "old-2", "old-3", "old-4"])
    mentions = [{"place_id": "park", "raw_name": "中心公园", "source_url": "https://guides.test/mention", "snippet": "中心公园适合散步", "provider": "xhs"}] * 4
    cache["hit"] = hit(places=[original], evidences=[base, same_article, extra], mentions=mentions)
    bundle = run(NativeModel(usual_steps(submit())))
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 3
    # 同一次调用先记 RUNNING 再记终态；审计看最后一次，进度消费者才逐条看到。
    recall = [t for t in bundle.extras["recommendation"]["tool_trace"] if t["tool"] == "recall_city_guides"]
    assert recall[-1]["status"] == "OK"
    assert bundle.discovery["database"]["status"] == "OK"
    assert bundle.discovery["database"]["result_count"] == 3


def test_same_name_cross_district_without_coordinates_is_not_merged_or_linked(cache):
    east = poi("east", "同名公园", district="东区", area="东街", lat=None, lng=None)
    west = poi("west", "同名公园", district="西区", area="西街", lat=None, lng=None)
    guide = evidence(text="东区同名公园适合散步。", mentions=["同名公园"])
    cache["hit"] = hit(places=[east, west], evidences=[guide])
    model = NativeModel(usual_steps(submit("east")))
    bundle = run(model)
    assert [p.place_id for p in bundle.places] == ["east"]
    data = json.loads(next(m.content for m in model.seen[1] if isinstance(m, ToolMessage)))
    assert len(data["candidates"]) == 2
    assert next(p for p in data["candidates"] if p["place_id"] == "west")["evidence_count"] == 0


def test_normalized_dedupe_preserves_sources_and_real_evidence(cache):
    duplicate = poi("park-copy", "成都中心公园")
    mentions = [{"place_id": "park-copy", "raw_name": "成都中心公园", "source_url": "https://guides.test/another", "snippet": "成都中心公园适合城市游"}]
    cache["hit"] = hit(places=[poi(), duplicate], mentions=mentions)
    def choose(messages, tools):
        payload = json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)
        candidate = payload["candidates"][0]
        assert len(payload["candidates"]) == 1
        assert candidate["evidence_count"] == 2
        return call("submit_recommendations", submit(candidate["place_id"], eid=candidate["evidence_ids"][0]))
    bundle = run(NativeModel([*usual_steps(submit())[:2], choose]))
    assert len(bundle.places) == 1
    assert len(bundle.extras["raw_candidates"]) == 2
    assert bundle.places[0].merged_from
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 2
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert all(p["source_id"] == "poi-source" for p in bundle.extras["raw_candidates"])


def test_web_extracts_only_grounded_concrete_names_and_verifies_exact_poi(cache):
    cache["hit"] = None
    text = "城市游可以去花溪公园欣赏水景，也可以休息散步。这份攻略只介绍花溪公园，没有介绍其他景点和住宿区域。"
    items = [{"title": "公园攻略", "snippet": text, "url": "https://guides.test/new"}]
    def extraction(messages):
        match = rec.re.search(r"证据 id：([^\n]+)", messages[-1].content)
        eid = match.group(1)
        return AIMessage(content=json.dumps({"results": [{"evidence_id": eid, "places": [{"name": "花溪公园"}, {"name": "幻觉景点"}, {"name": "茶馆"}]}]}, ensure_ascii=False))
    def choose(messages, tools):
        payload = json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)
        new = next(p for p in payload["candidates"] if p["place_id"] == "new")
        return call("submit_recommendations", submit("new", eid=new["evidence_ids"][0]))
    model = NativeModel([call("recall_city_guides"), call("search_web_guides", {"query": "城市公园攻略"}), choose], extraction)
    hub = Hub(items, poi_results={"花溪公园": [poi("new", "花溪公园"), poi("unrelated", "别处公园")]})
    bundle = run(model, hub)
    assert hub.poi_calls == [("花溪公园", "成都", 5)]
    assert [p.place_id for p in bundle.places] == ["new"]
    assert "花溪公园" in bundle.evidences[0].place_mentions
    assert bundle.discovery_status == "READY"
    assert len(model.extractions) == 1
    assert any(c["tag"].startswith("extract_places") for c in bundle.extras["llm_calls"])


def test_none_store_never_opens_default_db_and_missing_city_does_not_call_tools(cache):
    hub = Hub()
    bundle = rec.recommend_guided(hub, LLM(), TripIntent(destination=["成都"]), store=None, session_id="none")
    assert bundle.discovery_status == "FAILED" and cache["calls"] == []
    hub = Hub()
    bundle = rec.recommend_guided(hub, LLM(), TripIntent(), store=None, session_id="none")
    assert bundle.discovery_status == "FAILED" and hub.web_calls == hub.geo_calls == []


def test_tool_errors_and_callback_are_safe(cache, monkeypatch):
    secret = "private-api-key-123456"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    cache["error"] = RuntimeError("token=" + secret)
    def broken_progress(_):
        raise RuntimeError(secret)
    bundle = run(NativeModel([RuntimeError(secret)]), Hub(web_error=RuntimeError(secret)), progress=broken_progress)
    assert secret not in json.dumps(bundle.dump())
    assert bundle.discovery_status == "FAILED"


def test_real_cache_tmp_sqlite_is_read_only(tmp_path, monkeypatch):
    from app.store import TravelPlanStore

    store = TravelPlanStore(tmp_path / "recommendations.db")
    city_cache.write_candidates("成都", [poi()], [evidence()], store=store)
    before = store.get_city_cache_rows("成都")
    monkeypatch.setattr(city_cache, "TravelPlanStore", lambda *a, **k: pytest.fail("不许创建默认数据库"))
    model = NativeModel(usual_steps(submit()))
    # 城市缓存重新编号证据，原生模型从工具实际返回ID选，不依赖旧 session ID。
    def choose(messages, tools):
        data = json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)
        candidate = data["candidates"][0]
        return call("submit_recommendations", submit(candidate["place_id"], eid=candidate["evidence_ids"][0]))
    model.steps[-1] = choose
    bundle = run(model, store=store)
    assert [p.place_id for p in bundle.places] == ["park"]
    assert store.get_city_cache_rows("成都") == before
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 1


def test_five_explicit_empty_categories_are_valid(cache):
    bundle = run(NativeModel(usual_steps(submit(pid=None))))
    assert bundle.discovery_status == "READY"
    assert bundle.places == bundle.hotel_areas == []
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert bundle.extras["recommended_cards"] == []
    assert bundle.extras["raw_candidates"]


def test_poi_only_cache_never_becomes_evidence_fallback(cache):
    cache["hit"] = hit(places=[poi(merged_from=["fake-count"] * 100)], evidences=[])
    bundle = run(NativeModel([RuntimeError("offline")]))
    assert bundle.discovery_status == "FAILED"
    assert bundle.places == []
    assert len(bundle.extras["raw_candidates"]) == 1


def test_web_verification_has_a_global_concrete_name_budget(cache):
    cache["hit"] = None
    names = [f"第{i}文化公园" for i in range(20)]
    text = "、".join(names) + "。成都茶馆有很多，这是一份市内散步攻略。"
    def extraction(messages):
        eid = rec.re.search(r"证据 id：([^\n]+)", messages[-1].content).group(1)
        return AIMessage(content=json.dumps({"results": [{"evidence_id": eid, "places": [
            {"name": name} for name in [*names, "成都茶馆"]]}]}, ensure_ascii=False))
    hub = Hub([{"title": "城市公园", "snippet": text, "url": "https://guides.test/many"}])
    bundle = run(NativeModel(usual_steps(submit(pid=None)), extraction), hub)
    assert len(hub.poi_calls) == rec._MAX_POI_QUERIES
    assert all(query in names for query, _, _ in hub.poi_calls)
    assert bundle.places == []


def test_duplicate_tool_call_ids_and_batch_limit_cannot_spin(cache):
    response = AIMessage(content="", tool_calls=[{"name": "recall_city_guides", "args": {}, "id": "duplicate"}] * 5)
    model = NativeModel([response])
    bundle = run(model)
    assert len(model.seen) == 1
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert bundle.discovery_status == "PARTIAL"


def test_web_only_recommendation_hands_sources_over_without_broken_evidence(cache, tmp_path):
    """没有城市缓存、纯 Web 推荐时，证据的 source_id 必须能过户到正式 run，否则外键直接断。"""

    from app.providers import ProviderCall
    from app.store import TravelPlanStore
    from app.workflow import _adopt_prefetch_sources
    from datetime import datetime, timezone

    cache["hit"] = None
    fetched_at = datetime(2026, 9, 22, tzinfo=timezone.utc)
    text = "成都城市游推荐去花溪公园散步，园区绿荫多、适合慢慢走半天。这份攻略只介绍花溪公园这一个地方，没有提到住宿区域和其他景点。"

    class WebHub(Hub):
        """真实 Hub 会同时给出 result.calls 与 audit_entries；两处的 source_id 必须一致。"""

        def web_search(self, query, *, max_results):
            result = super().web_search(query, max_results=max_results)
            result.calls = [ProviderCall(
                source_id="web-src-1", provider="tavily", source_type="web", tool="web_search",
                query={"query": query}, status="OK", fetched_at=fetched_at,
            )]
            return result

        def audit_entries(self):
            return [{
                "source_id": "web-src-1", "provider": "tavily", "source_type": "web",
                "tool": "web_search", "arguments": {"query": "成都 城市攻略"}, "status": "OK",
                "fetched_at": fetched_at.isoformat(), "item_count": 1,
            }]

    def extraction(messages):
        eid = rec.re.search(r"证据 id：([^\n]+)", messages[-1].content).group(1)
        return AIMessage(content=json.dumps({"results": [{"evidence_id": eid, "places": [{"name": "花溪公园"}]}]}, ensure_ascii=False))
    def choose(messages, tools):
        payload = json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)
        new = next(p for p in payload["candidates"] if p["place_id"] == "new")
        return call("submit_recommendations", submit("new", eid=new["evidence_ids"][0]))
    hub = WebHub([{"title": "公园攻略", "snippet": text, "url": "https://guides.test/only"}],
                 poi_results={"花溪公园": [poi("new", "花溪公园")]})
    bundle = run(NativeModel([call("recall_city_guides"), call("search_web_guides", {"query": "成都 城市攻略"}), choose], extraction), hub)
    assert bundle.extras["recommendation"]["source"] == "llm", bundle.extras["recommendation"]["notes"]

    store = TravelPlanStore(tmp_path / "handoff.db")
    store.create_run("tp-handoff", source="guided", source_session_id="offline-session")
    adopted = _adopt_prefetch_sources(store, "tp-handoff", bundle)
    assert [row["source_id"] for row in adopted] == ["web-src-1"]
    # 过户之后再写证据：外键打开时这一步必须能过。
    for item in bundle.evidences:
        store.save_evidence("tp-handoff", item, source_id=item.source_id)
    saved = [item for item in bundle.evidences if item.source_id == "web-src-1"]
    assert saved, "网页证据必须带着真实 source_id"


def test_secrets_in_guide_content_are_redacted_in_tools_and_bundle(cache, monkeypatch):
    secret = "guide-private-secret-123456"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    cache["hit"] = hit(evidences=[evidence(text="中心公园适合散步。api_key=" + secret)])
    model = NativeModel(usual_steps(submit()))
    bundle = run(model)
    assert secret not in json.dumps(bundle.dump(), ensure_ascii=False)
    for messages in model.seen:
        assert all(secret not in message.content for message in messages if isinstance(message, ToolMessage))
