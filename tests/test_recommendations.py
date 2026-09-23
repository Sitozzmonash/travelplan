"""纯离线：数据只来自 tmp_path SQLite 与原生工具调用替身。

这一版推荐引擎是「只读城市库 + 原生 function call」：没有任何 Web 搜索、正文抽名、
POI 核验与目的地地理编码。所以本文件里的 `offline_only` fixture 会把
`city_cache.TravelPlanStore`、`discovery.prefetch`、`app.providers.ProviderHub`
全部设成失败 —— 任何一个被碰到都会让用例当场炸掉，而不是"跑得通但偷偷出了网"。
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app import city_cache, discovery, providers, recommendations as rec
from app.llm import LLM
from app.models import Evidence, Place, TripIntent
from app.store import TravelPlanStore


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """零网络 + 零默认数据库：触碰即失败。"""
    monkeypatch.setattr(city_cache, "TravelPlanStore", lambda *a, **k: pytest.fail("不能隐式打开默认数据库"))
    monkeypatch.setattr(discovery, "prefetch", lambda *a, **k: pytest.fail("不能回退 discovery.prefetch 查机酒"))
    monkeypatch.setattr(providers, "ProviderHub", lambda *a, **k: pytest.fail("零网络：不能构造 ProviderHub"))


@pytest.fixture
def store(tmp_path):
    return TravelPlanStore(tmp_path / "recommendations.sqlite")


@pytest.fixture
def reads(monkeypatch):
    """统计真实读库次数，并强制调用方传注入的 store 且 allow_stale=True。"""
    calls = []
    real = city_cache.read_candidates

    def wrapper(city, **kwargs):
        calls.append((city, kwargs))
        assert kwargs.get("store") is not None, "必须用注入的 store 读库"
        assert kwargs.get("allow_stale") is True
        return real(city, **kwargs)

    monkeypatch.setattr(city_cache, "read_candidates", wrapper)
    return calls


# ----------------------------------------------------------------------
# 替身与构造
# ----------------------------------------------------------------------


def poi(pid="park", name="中心公园", *, district="青羊区", area="宽窄巷子", lat=30.66, lng=104.07,
        kind="风景名胜;公园", **kwargs):
    return Place(place_id=pid, name=name, city="成都", district=district, business_area=area,
                 lat=lat, lng=lng, type=kind, amap_verified=True, source_id="poi-source", **kwargs)


def guide(eid="g1", *, text="中心公园适合散步，推荐住在宽窄巷子，出门就能走到中心公园。",
          url="https://guides.test/g1", title="城市游攻略", mentions=None):
    return Evidence(id=eid, title=title, text=text, provider="xhs", source_url=url,
                    place_mentions=list(mentions or []))


def seed(store, places, evidences, city="成都"):
    city_cache.write_candidates(city, places, evidences, store=store)
    return store


_call_seq = itertools.count(1)


def _call_id():
    return f"call-{next(_call_seq)}"


def call(name, args=None, cid=None):
    """每次调用给一个唯一 ID：重复 ID 会被引擎当作异常批次拒绝。"""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid or _call_id()}])


class NativeModel:
    """只走原生 bind_tools；非工具调用一律视为替身配置错误。"""

    model_name = "offline-native"

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []
        self.bindings = []
        self.assertion_errors = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))

        def invoke(messages):
            self.seen.append(list(messages))
            # 每个先前原生调用都必须有匹配的 ToolMessage；内容必须还是合法 JSON。
            pending = set()
            for message in messages:
                if isinstance(message, AIMessage):
                    pending.update(item["id"] for item in message.tool_calls)
                elif isinstance(message, ToolMessage):
                    assert message.tool_call_id in pending, "工具结果必须对应一次原生调用"
                    pending.discard(message.tool_call_id)
                    json.loads(message.content)
            assert not pending, "每个原生调用都必须有匹配的 ToolMessage"
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
        raise AssertionError("攻略推荐只允许原生 function calling")


def run(model, store, *, intent=None, progress=None):
    bundle = rec.recommend_guided(LLM(model=model), intent or TripIntent(destination=["成都"], days=3),
                                  store=store, session_id="offline-session", on_progress=progress)
    assert not model.assertion_errors, "模型替身内部断言失败不能被降级掩盖"
    return bundle


def recall_payload(messages):
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == "recall_city_guides":
            return json.loads(message.content)
    raise AssertionError("没有读到 recall_city_guides 的工具结果")


def tool_results(messages, name):
    return [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == name]


def row_of(payload, pid):
    return next(item for item in payload["candidates"] if item["place_id"] == pid)


def card(pid, category="attraction", reason="攻略里明确提到，适合这次行程", evidence_ids=None):
    return {"place_id": pid, "category": category, "reason": reason,
            "evidence_ids": list(evidence_ids)}


def card_for(payload, pid, category="attraction", reason="攻略里明确提到，适合这次行程", index=0):
    row = row_of(payload, pid)
    return card(pid, category, reason, [row["evidence_ids"][index]])


def submit(cards, *, areas=None, explanation="依据本次城市攻略库推荐；缺证据的类别留空。"):
    categories = {key: [] for key in rec._CATEGORIES}
    for item in cards:
        categories[item["category"]].append(
            {key: item[key] for key in ("place_id", "category", "reason", "evidence_ids")})
    return {"categories": categories, "hotel_areas": list(areas or []), "explanation": explanation}


def submit_step(build):
    def step(messages, tools):
        return call("submit_recommendations", submit(build(recall_payload(messages))))
    return step


def area(name="宽窄巷子", place_ids=("park",), evidence_ids=("city-g1",), *, scope="in_city",
         reason="攻略建议住在宽窄巷子，可步行到中心公园"):
    return {"name": name, "reason": reason, "evidence_ids": list(evidence_ids),
            "place_ids": list(place_ids), "scope": scope}


# ----------------------------------------------------------------------
# 正常路径与零网络
# ----------------------------------------------------------------------


def test_prompt_asks_for_up_to_max_cards_per_category(store):
    """2026-09-23 拍板：每类尽量给足到 _MAX_CARDS（10）个，但校验仍不可放松。"""
    assert rec._MAX_CARDS == 10, "契约变了要同步改这条用例"
    assert "每类都要尽量给足" in rec._SYSTEM and f"{rec._MAX_CARDS} 个" in rec._SYSTEM
    assert "不要只挑 2-3 个顶流" in rec._SYSTEM
    assert "数量多不是目的" in rec._SYSTEM
    assert "地点/证据/类别/营业状态校验" in rec._SYSTEM
    submit = next(tool for tool in [rec._SUBMIT]
                  if tool["function"]["name"] == "submit_recommendations")
    assert f"每类最多 {rec._MAX_CARDS} 个" in submit["function"]["description"]


def test_two_round_conversation_recommends_only_database_candidates(store, reads):
    seed(store, [poi(), poi("unused", "城市博物馆", kind="博物馆", lat=30.67, lng=104.06)], [guide()])
    progress = []
    model = NativeModel([call("recall_city_guides"), submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store, progress=progress.append)

    assert bundle.discovery_status == "READY"
    assert [place.place_id for place in bundle.places] == ["park"]
    assert bundle.hotels == bundle.outbound == bundle.inbound == []
    assert bundle.provider_calls == []
    assert len(reads) == 1
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert bundle.extras["recommendation"]["place_ids"] == ["park"]
    assert len(bundle.extras["llm_calls"]) == 2
    assert len(bundle.extras["raw_candidates"]) == 2
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 1
    assert bundle.poi_pools == discovery.split_poi_pools(bundle.places)
    restored = discovery.PrefetchBundle.load(bundle.dump())
    assert restored.extras["recommended_cards"] == bundle.extras["recommended_cards"]

    # 第 0 轮强制先读库；之后才允许（且只允许）提交。
    assert model.bindings[0][1] == {"tool_choice": "recall_city_guides"}
    offered = [{tool["function"]["name"] for tool in tools} for tools, _ in model.bindings]
    assert offered[0] == {"recall_city_guides"}
    assert offered[1] == {"recall_city_guides", "submit_recommendations"}
    assert [message.name for message in model.seen[1] if isinstance(message, ToolMessage)] == ["recall_city_guides"]


def test_evidence_count_counts_independent_articles_not_merged_from(store):
    places = [poi(merged_from=["old-1", "old-2", "old-3"])]
    evidences = [
        guide("a", url="https://guides.test/a", title="攻略A"),
        # 同一篇文章换了 utm 参数再抓一次：算同一篇。
        guide("b", url="https://guides.test/a?utm_source=other#frag", title="攻略A"),
        guide("c", url="https://guides.test/c", title="攻略C"),
    ]
    seed(store, places, evidences)
    captured = {}

    def step(messages, tools):
        payload = recall_payload(messages)
        captured["row"] = row_of(payload, "park")
        return call("submit_recommendations", submit([card_for(payload, "park", index=0)]))

    bundle = run(NativeModel([call("recall_city_guides"), step]), store)
    assert captured["row"]["evidence_count"] == 2
    assert bundle.extras["recommended_cards"][0]["evidence_count"] == 2
    assert len(bundle.extras["recommended_cards"][0]["evidence_ids"]) == 1
    assert bundle.discovery_status == "READY"


@pytest.mark.parametrize("failure", [
    RuntimeError("model down"),
    AIMessage(content="推荐中心公园，这里很适合散步"),
    AIMessage(content='{"tool_calls": [{"name": "submit_recommendations"}]}'),
    AIMessage(content="", invalid_tool_calls=[{"id": "broken", "name": "recall_city_guides", "args": "{", "error": "invalid"}]),
])
def test_natural_language_or_json_text_is_never_a_recommendation(store, failure):
    """模型失败时退回证据筛一遍：source=evidence_fallback 且必须 PARTIAL；全程零 Provider。"""
    seed(store, [poi()], [guide()])
    model = NativeModel([failure])
    bundle = run(model, store)
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert [place.place_id for place in bundle.places] == ["park"]
    assert "非模型推荐" in bundle.extras["recommended_cards"][0]["reason"]
    assert bundle.provider_calls == []
    assert bundle.hotel_areas == []
    assert any(entry["status"] == "OK" and entry["actor"] == "evidence_fallback"
               for entry in bundle.extras["recommendation"]["tool_trace"])


def test_empty_cache_is_failed_and_never_fakes_success(store, reads):
    model = NativeModel([call("recall_city_guides")])
    bundle = run(model, store)
    assert bundle.discovery_status == "FAILED"
    assert bundle.places == [] and bundle.hotel_areas == [] and bundle.evidences == []
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert len(model.seen) == 1, "没有候选就不该再让模型空转一轮"
    database = bundle.discovery["database"]
    assert {key: database[key] for key in ("status", "actor", "stage", "result_count")} == {
        "status": "OK", "actor": "llm", "stage": "database", "result_count": 0}
    assert len(reads) == 1


def test_no_candidate_ever_becomes_a_recommendation(store):
    """库里只有闭业的点：既不能提交成功，也不能在降级里复活。"""
    seed(store, [poi(opening_hours="暂停营业")], [guide()])
    model = NativeModel([call("recall_city_guides"), submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store)
    assert bundle.discovery_status == "FAILED"
    assert bundle.places == []
    assert any("闭业" in entry["error"] for entry in bundle.extras["recommendation"]["tool_trace"]
               if "error" in entry)


# ----------------------------------------------------------------------
# 校验：地点 / 证据 / 类别 / 重复
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad,expected", [
    ("unknown_id", "不在本次读到的候选"),
    ("foreign_evidence", "不属于本次该地点的真实证据"),
    ("closed", "闭业"),
    ("facility", "附属设施"),
    ("tea_shopping", "类别"),
    ("duplicate", "重复提交"),
    ("density_reason", "密度"),
])
def test_invalid_submissions_are_rejected_with_readable_errors(store, bad, expected):
    places = [poi(), poi("museum", "城市博物馆", kind="博物馆", lat=30.67, lng=104.06)]
    evidences = [guide("g1", url="https://guides.test/a", title="攻略A"),
                 guide("g2", text="城市博物馆值得一看，推荐住在宽窄巷子。",
                       url="https://guides.test/b", title="攻略B")]
    if bad == "closed":
        places = [poi(opening_hours="暂停营业"), places[1]]
    elif bad == "facility":
        places = [poi(name="中心公园停车场", kind="交通设施服务;停车场"), places[1]]
        evidences = [guide("g1", text="中心公园停车场只提供车位。", url="https://guides.test/a", title="攻略A"),
                     evidences[1]]
    elif bad == "tea_shopping":
        places = [poi("tea", "茗香茶叶专卖店", kind="购物服务;专卖店;茶叶"), places[1]]
        evidences = [guide("g1", text="茗香茶叶专卖店出售礼品茶。", url="https://guides.test/a", title="攻略A"),
                     evidences[1]]
    seed(store, places, evidences)

    def build(payload):
        pid = "tea" if bad == "tea_shopping" else "park"
        category = "shopping" if bad == "tea_shopping" else "attraction"
        if bad == "unknown_id":
            return [card("invented", "attraction", "编造的ID", ["invented-evidence"])]
        if bad == "foreign_evidence":
            return [card(pid, category, "引用了别处的证据", [row_of(payload, "museum")["evidence_ids"][0]])]
        if bad == "duplicate":
            return [card_for(payload, pid, category), card_for(payload, pid, "food")]
        if bad == "density_reason":
            return [card_for(payload, pid, category, reason="POI密度高，攻略比例也高")]
        return [card_for(payload, pid, category)]

    model = NativeModel([call("recall_city_guides"), submit_step(build), RuntimeError("停止：降级")])
    bundle = run(model, store)
    results = tool_results(model.seen[-1], "submit_recommendations")
    assert results and results[-1]["status"] == "ERROR"
    assert expected in results[-1]["error"], results[-1]["error"]
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert bundle.discovery_status == "PARTIAL"
    rejected_pid = {"tea_shopping": "tea"}.get(bad, "park")
    if bad in {"closed", "facility", "tea_shopping"}:
        assert rejected_pid not in {place.place_id for place in bundle.places}, "被拒的地点不能在降级里复活"
    else:
        assert bundle.places, "其它候选仍应有证据降级结果"


def test_guide_mentioned_closure_blocks_the_card(store):
    seed(store, [poi()], [guide(text="中心公园暂停开放，请不要前往。")])
    def build(payload):
        return [card_for(payload, "park")]
    model = NativeModel([call("recall_city_guides"), submit_step(build), RuntimeError("停止")])
    bundle = run(model, store)
    assert bundle.places == []
    assert bundle.discovery_status == "FAILED"
    error = tool_results(model.seen[-1], "submit_recommendations")[-1]["error"]
    assert "闭业" in error


def test_same_name_cross_district_stays_two_entities(store):
    places = [poi("east", "同名公园", district="东区", area="东街", lat=30.60, lng=104.00),
              poi("west", "同名公园", district="西区", area="西街", lat=30.85, lng=104.30)]
    evidences = [guide("e1", text="东区同名公园适合散步。", url="https://guides.test/e", title="东区攻略"),
                 guide("e2", text="西区同名公园有湖景。", url="https://guides.test/w", title="西区攻略")]
    seed(store, places, evidences)

    def build(payload):
        assert len(payload["candidates"]) == 2, "同名不同区必须是两个实体"
        cards = []
        for pid in ("east", "west"):
            row = row_of(payload, pid)
            assert len(row["evidence_ids"]) == 1, "各区的证据不能互相挂"
            cards.append(card_for(payload, pid))
        return cards

    bundle = run(NativeModel([call("recall_city_guides"), submit_step(build)]), store)
    assert sorted(place.place_id for place in bundle.places) == ["east", "west"]
    assert len(bundle.extras["recommended_cards"]) == 2
    assert {item["evidence_count"] for item in bundle.extras["recommended_cards"]} == {1}
    assert bundle.discovery_status == "READY"


def test_repeated_recall_is_replayed_without_rereading_cache(store, reads):
    seed(store, [poi()], [guide()])
    def step(messages, tools):
        results = tool_results(messages, "recall_city_guides")
        assert len(results) == 2 and results[0] == results[1], "回放必须给同一份库数据"
        return call("submit_recommendations", submit([card_for(results[0], "park")]))

    model = NativeModel([call("recall_city_guides", cid="r1"), call("recall_city_guides", cid="r2"), step])
    bundle = run(model, store)
    assert len(reads) == 1
    assert bundle.discovery_status == "READY"
    trace = bundle.extras["recommendation"]["tool_trace"]
    assert sum(entry["status"] == "REPLAY" for entry in trace) == 1


def test_duplicate_call_ids_and_batch_limit_cannot_spin(store, reads):
    seed(store, [poi()], [guide()])
    duplicate = AIMessage(content="", tool_calls=[
        {"name": "recall_city_guides", "args": {}, "id": "same"},
        {"name": "recall_city_guides", "args": {}, "id": "same"}])
    model = NativeModel([duplicate, RuntimeError("不该再有下一轮")])
    bundle = run(model, store)
    assert len(model.seen) == 1, "非法批次必须当场收口"
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert [place.place_id for place in bundle.places] == ["park"]


def test_submit_before_recall_is_refused(store, reads):
    seed(store, [poi()], [guide()])
    early = call("submit_recommendations", submit([card("invented", reason="编造", evidence_ids=["x"])]))
    model = NativeModel([early, RuntimeError("停止")])
    bundle = run(model, store)
    assert len(model.seen) == 1, "第 0 轮就提交失败，不该再空转一轮"
    refused = [entry for entry in bundle.extras["recommendation"]["tool_trace"]
               if entry["tool"] == "submit_recommendations" and entry["status"] == "ERROR"]
    assert refused and "尚未读取城市攻略库" in refused[-1]["error"]
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert len(reads) == 1, "降级时才真正读库，且只读一次"


# ----------------------------------------------------------------------
# 住宿区域
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mutation,expected", [
    ("no_stay_advice", "没有明确建议住这里"),
    ("activity_outside_area", "不在其关联活动地点的区县/商圈/名称里"),
    ("density_reason", "密度或攻略比例"),
    ("bad_scope", "scope 只能是"),
])
def test_hotel_area_needs_stay_advice_linked_activity_and_clean_reason(store, mutation, expected):
    text = "中心公园适合散步，宽窄巷子有很多小店。"
    if mutation == "activity_outside_area":
        text = "中心公园适合散步，推荐住在春熙路。"
    seed(store, [poi()], [guide(text=text)])

    def bad_area(payload):
        item = area(evidence_ids=row_of(payload, "park")["evidence_ids"])
        if mutation == "activity_outside_area":
            item["name"] = "春熙路"
        elif mutation == "density_reason":
            item["reason"] = "POI密度高，100%的攻略都在这里"
        elif mutation == "bad_scope":
            item["scope"] = "downtown"
        return item

    def first(messages, tools):
        payload = recall_payload(messages)
        return call("submit_recommendations",
                    submit([card_for(payload, "park")], areas=[bad_area(payload)]))

    model = NativeModel([call("recall_city_guides"), first, submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store)
    error = tool_results(model.seen[-1], "submit_recommendations")[-1]["error"]
    assert expected in error, error
    assert bundle.hotel_areas == []
    # 修正后的第二次提交成功；第一次被拒留下降级留痕，所以整体是 PARTIAL 而不是 READY。
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert [place.place_id for place in bundle.places] == ["park"]
    assert bundle.discovery_status == "PARTIAL"


def test_area_scope_reflects_real_geography(store):
    seed(store, [poi()], [guide()])
    def first(messages, tools):
        payload = recall_payload(messages)
        wrong = area(evidence_ids=row_of(payload, "park")["evidence_ids"], scope="outskirts")
        return call("submit_recommendations", submit([card_for(payload, "park")], areas=[wrong]))
    model = NativeModel([call("recall_city_guides"), first, submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store)
    error = tool_results(model.seen[-1], "submit_recommendations")[-1]["error"]
    assert "不应声明为远郊" in error
    assert bundle.hotel_areas == []


REMOTE_PLACES = [
    poi(),
    poi("museum", "城市博物馆", kind="博物馆", lat=30.67, lng=104.06),
    poi("valley", "山谷公园", district="大邑县", area="山谷镇", lat=30.60, lng=103.30),
]
REMOTE_GUIDES = [
    guide("g1", text="中心公园与城市博物馆都值得去，推荐住在宽窄巷子。",
          url="https://guides.test/1", title="市区攻略"),
    guide("g2", text="山谷公园适合远足，推荐住在山谷镇。", url="https://guides.test/2", title="山谷攻略"),
]


def remote_payload(payload):
    cards = [card_for(payload, pid) for pid in ("park", "museum", "valley")]
    return cards


@pytest.mark.parametrize("scope,requested,rejected", [
    ("in_city", False, True),
    ("outskirts", False, True),
    ("in_city", True, True),
    ("outskirts", True, False),
])
def test_remote_hotel_area_requires_explicit_outskirts_request(store, scope, requested, rejected):
    seed(store, REMOTE_PLACES, REMOTE_GUIDES)
    intent = TripIntent(destination=["成都"], days=3,
                        hotel_preferences=["山谷镇度假住宿"] if requested else [])

    def first(messages, tools):
        payload = recall_payload(messages)
        proposed = area(name="山谷镇", place_ids=["valley"],
                        evidence_ids=row_of(payload, "valley")["evidence_ids"], scope=scope)
        return call("submit_recommendations", submit(remote_payload(payload), areas=[proposed]))

    def second(messages, tools):
        return call("submit_recommendations", submit(remote_payload(recall_payload(messages))))

    model = NativeModel([call("recall_city_guides"), first, second])
    bundle = run(model, store, intent=intent)
    if rejected:
        error = tool_results(model.seen[-1], "submit_recommendations")[-1]["error"]
        assert "远郊" in error, error
        assert bundle.hotel_areas == []
    else:
        assert bundle.hotel_areas and bundle.hotel_areas[0]["scope"] == "outskirts"
    # 推荐集合本身不受住宿区域结论影响；远郊卡片如实标注距行程重心。
    assert sorted(place.place_id for place in bundle.places) == ["museum", "park", "valley"]
    far = next(item for item in bundle.extras["recommended_cards"] if item["place_id"] == "valley")
    assert "距行程重心约" in far["reason"]


def test_card_reason_says_trip_centroid_never_city_center(store):
    seed(store, REMOTE_PLACES, REMOTE_GUIDES)
    model = NativeModel([call("recall_city_guides"), submit_step(remote_payload)])
    bundle = run(model, store)
    reasons = " ".join(item["reason"] for item in bundle.extras["recommended_cards"])
    assert "行程重心" in reasons
    assert "市中心" not in reasons and "目的地中心" not in reasons


# ----------------------------------------------------------------------
# 上下文瘦身 / 进度 / 只读
# ----------------------------------------------------------------------


def test_context_is_slimmed_from_second_round_and_db_read_once(store, reads):
    long_text = "中心公园适合散步。" + "很好" * 900
    seed(store, [poi()], [guide(text=long_text)])

    def retry(messages, tools):
        payload = recall_payload(messages)
        assert payload.get("context_slimmed"), "第 2 轮起不应再重复整段攻略正文"
        assert "很好" not in json.dumps(payload, ensure_ascii=False)
        return call("submit_recommendations", submit([card_for(payload, "park")]))

    model = NativeModel([
        call("recall_city_guides"),
        call("submit_recommendations", {"categories": {key: [] for key in rec._CATEGORIES}}),
        retry,
    ])
    bundle = run(model, store)

    first = recall_payload(model.seen[1])["evidences"][0]
    assert len(first["text"]) <= rec._EVIDENCE_TEXT_CHARS + 32 and "已截断" in first["text"]
    last = recall_payload(model.seen[2])["evidences"][0]
    assert last["text"] == "" and last["text_chars"] == len(long_text)
    # 第一次提交被拒 → 留下降级留痕，所以整体 PARTIAL；重点是上下文与读库次数。
    assert bundle.discovery_status == "PARTIAL"
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert len(reads) == 1, "上下文瘦身不等于重复读库"
    assert len(model.seen) == 3


def test_candidate_and_evidence_contexts_are_capped(store):
    places = [poi(f"p{i}", f"测试地点{i}", district=f"区{i}", lat=30.0 + i * 0.02, lng=104.0 + i * 0.02)
              for i in range(70)]
    evidences = [guide(f"g{i}", text=f"测试地点{i}值得一去。", url=f"https://guides.test/{i}", title=f"攻略{i}")
                 for i in range(25)]
    seed(store, places, evidences)
    model = NativeModel([call("recall_city_guides"), RuntimeError("停止")])
    bundle = run(model, store)
    payload = recall_payload(model.seen[1])
    assert len(payload["evidences"]) == rec._EVIDENCE_LIMIT
    assert len(payload["candidates"]) == rec._MAX_CANDIDATES
    assert len(bundle.extras["raw_candidates"]) == 70, "原始候选仍完整保留在 extras 里"


def test_progress_events_and_discovery_keys_only_cover_three_stages(store):
    seed(store, [poi()], [guide()])
    events = []
    model = NativeModel([call("recall_city_guides"), submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store, progress=events.append)

    required = {"tool", "status", "actor", "stage", "result_count"}
    assert all(required <= set(event) for event in events)
    assert {event["stage"] for event in events} == {"database", "recommendation", "places"}
    assert set(bundle.discovery) == {"database", "recommendation", "places"}
    assert "web" not in bundle.discovery and "web" not in bundle.dump()
    assert [event["status"] for event in events if event["tool"] == "recall_city_guides"] == ["RUNNING", "OK"]
    assert [event["status"] for event in events if event["tool"] == "submit_recommendations"] == ["RUNNING", "OK"]
    assert events[-1]["tool"] == "finalize_places" and events[-1]["stage"] == "places"
    assert events[-1]["result_count"] == 1


def test_cache_rows_are_never_written(store, reads):
    seed(store, [poi()], [guide()])
    before = store.get_city_cache_rows("成都")
    evidence_before = store.get_city_evidence_rows("成都")
    model = NativeModel([call("recall_city_guides"), submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store)
    assert [place.place_id for place in bundle.places] == ["park"]
    assert store.get_city_cache_rows("成都") == before
    assert store.get_city_evidence_rows("成都") == evidence_before
    assert len(reads) == 1


# ----------------------------------------------------------------------
# 数据源与脱敏
# ----------------------------------------------------------------------


def test_store_is_required_and_default_db_is_never_opened(monkeypatch):
    monkeypatch.setattr(city_cache, "read_candidates",
                        lambda *a, **k: pytest.fail("store=None 时不该读库"))
    model = NativeModel([])
    bundle = rec.recommend_guided(LLM(model=model), TripIntent(destination=["成都"]),
                                  store=None, session_id="none")
    assert bundle.discovery_status == "FAILED"
    assert bundle.places == [] and bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert model.seen == [], "没有数据源就不该调用模型"
    assert bundle.extras["recommendation"]["notes"]


def test_missing_destination_never_calls_the_model(store):
    model = NativeModel([])
    bundle = rec.recommend_guided(LLM(model=model), TripIntent(), store=store, session_id="none")
    assert bundle.discovery_status == "FAILED" and bundle.places == []
    assert model.seen == []


def test_secrets_in_guide_text_are_redacted(store, monkeypatch):
    secret = "guide-private-secret-123456"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    seed(store, [poi()], [guide(text="中心公园适合散步。api_key=" + secret)])
    model = NativeModel([call("recall_city_guides"), submit_step(lambda payload: [card_for(payload, "park")])])
    bundle = run(model, store)
    assert secret not in json.dumps(bundle.dump(), ensure_ascii=False)
    for messages in model.seen:
        assert all(secret not in message.content for message in messages if isinstance(message, ToolMessage))
