"""城市推荐缓存：同城命中整页零 LLM、TTL/池新鲜度/ID 校验与 run_discovery 接线。

数据全部来自临时 SQLite（TravelPlanStore(db_path=tmp_path / ...)）与原生工具替身，
零网络、零默认数据库。假 LLM 复用 tests/fakes.py 的 FakeLLM 约定（model="fake"）与
`test_recommendations.NativeModel` 的原生工具协议，驱动 recommend_guided 的真实引擎。
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app import city_cache, recommendations as rec, sessions
from app.llm import LLM
from app.models import Evidence, Place, TripIntent
from app.store import TravelPlanStore


@pytest.fixture
def store(tmp_path):
    return TravelPlanStore(tmp_path / "recommendation_cache.sqlite")


# ----------------------------------------------------------------------
# 造数（与 test_recommendations.py 同构）
# ----------------------------------------------------------------------


def poi(pid="park", name="中心公园", *, lat=30.66, lng=104.07, **kwargs):
    return Place(place_id=pid, name=name, city="成都", district="青羊区", business_area="宽窄巷子",
                 lat=lat, lng=lng, type="风景名胜;公园", amap_verified=True,
                 source_id="poi-source", **kwargs)


def guide(eid="g1", *, text="中心公园适合散步，推荐住在宽窄巷子。", title="城市游攻略"):
    return Evidence(id=eid, title=title, text=text, provider="xhs",
                    source_url=f"https://guides.test/{eid}")


def seed(store, places, evidences, city="成都"):
    city_cache.write_candidates(city, places, evidences, store=store)
    return store


TWO_PLACES = [poi(), poi("p2", "湖边公园", lat=30.67, lng=104.06)]
TWO_GUIDES = [
    guide("g1", text="中心公园适合散步，推荐住在宽窄巷子。"),
    guide("g2", text="湖边公园适合拍照，也值得一去。"),
]


# ----------------------------------------------------------------------
# 原生工具替身（驱动 recommend_guided 的真实 _Run 引擎）
# ----------------------------------------------------------------------


_call_seq = itertools.count(1)


def _call_id():
    return f"call-{next(_call_seq)}"


def call(name, args=None, cid=None):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid or _call_id()}])


class NativeModel:
    """只走原生 bind_tools 的工具替身（LLM.invoke_tools 会调 model.bind_tools().invoke()）。

    与 test_recommendations.NativeModel 同构：steps 是每次调用的返回（AIMessage 或异常），
    每次调用必须对应一条 ToolMessage（工具结果不能悬空）。
    """

    model_name = "offline-native"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.bindings = []
        self.seen = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))

        def invoke(messages):
            self.seen.append(list(messages))
            if not self.steps:
                raise RuntimeError("script exhausted")
            step = self.steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(messages, tools) if callable(step) else step

        return SimpleNamespace(invoke=invoke)


def recall_payload(messages):
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == "recall_city_guides":
            return json.loads(message.content)
    raise AssertionError("没有读到 recall_city_guides 的工具结果")


def row_of(payload, pid):
    return next(item for item in payload["candidates"] if item["place_id"] == pid)


def card_for(payload, pid, category="attraction"):
    row = row_of(payload, pid)
    return {"place_id": pid, "category": category, "reason": "攻略里明确提到，适合这次行程",
            "evidence_ids": [row["evidence_ids"][0]]}


def submit(cards):
    categories = {key: [] for key in rec._CATEGORIES}
    for item in cards:
        categories[item["category"]].append(item)
    return {"categories": categories, "hotel_areas": [], "explanation": "依据本次城市攻略库推荐；缺证据的类别留空。"}


def submit_step(build):
    def step(messages, tools):
        return call("submit_recommendations", submit(build(recall_payload(messages))))
    return step


def recommend_once(store, *, places=TWO_PLACES, guides=TWO_GUIDES, session_id="s1", city="成都"):
    """真实跑一次 LLM 推荐并落库，返回本轮 bundle。"""
    seed(store, places, guides, city=city)
    model = NativeModel([call("recall_city_guides"),
                         submit_step(lambda payload: [card_for(payload, "park"), card_for(payload, "p2")])])
    return rec.recommend_guided(LLM(model=model), TripIntent(destination=[city], days=3),
                                store=store, session_id=session_id)


# ----------------------------------------------------------------------
# 落库钩子
# ----------------------------------------------------------------------


def test_llm_recommendation_is_persisted(store):
    bundle = recommend_once(store)
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert bundle.discovery_status == "READY"
    row = store.get_city_recommendation("成都")
    assert row is not None
    assert row["payload"]["recommendation"]["place_ids"] == ["park", "p2"]
    assert [card["place_id"] for card in row["payload"]["recommended_cards"]] == ["park", "p2"]
    assert row["payload"]["hotel_areas"] == []
    assert row["payload"]["recommendation"]["source"] == "llm"


def test_evidence_fallback_is_never_cached(store):
    # 模型没给出有效工具调用 → 证据降级（place_ids 非空）；绝不能冒充 LLM 推荐落库。
    seed(store, TWO_PLACES, TWO_GUIDES)
    model = NativeModel([AIMessage(content="推荐中心公园，这里很适合散步")])
    bundle = rec.recommend_guided(LLM(model=model), TripIntent(destination=["成都"], days=3),
                                  store=store, session_id="s1")
    assert bundle.extras["recommendation"]["source"] == "evidence_fallback"
    assert bundle.places, "证据降级仍应保留候选"
    assert store.get_city_recommendation("成都") is None


def test_empty_recommendation_is_never_cached(store):
    # 空缓存：没有攻略候选 → FAILED，place_ids 为空 → 不落库。
    model = NativeModel([call("recall_city_guides")])
    bundle = rec.recommend_guided(LLM(model=model), TripIntent(destination=["成都"], days=3),
                                  store=store, session_id="s1")
    assert bundle.extras["recommendation"]["place_ids"] == []
    assert store.get_city_recommendation("成都") is None


# ----------------------------------------------------------------------
# 缓存命中 / 失效
# ----------------------------------------------------------------------


def test_second_call_hits_cache_with_zero_llm(store, monkeypatch):
    recommend_once(store)

    def boom(*args, **kwargs):
        raise AssertionError("缓存命中时绝不能构造 _Run / 调用模型")

    monkeypatch.setattr(rec, "_Run", boom)
    bundle = rec.recommendation_from_cache(TripIntent(destination=["成都"], days=3), store, "s2")
    assert bundle is not None
    assert bundle.extras["recommendation"]["cached"] is True
    assert bundle.extras["recommendation"]["status"] == "READY"
    assert bundle.extras["recommendation"]["source"] == "llm"
    assert bundle.extras["recommendation"]["place_ids"] == ["park", "p2"]
    # places 顺序 = 缓存 place_ids 顺序。
    assert [place.place_id for place in bundle.places] == ["park", "p2"]
    assert [card["place_id"] for card in bundle.extras["recommended_cards"]] == ["park", "p2"]
    assert bundle.discovery_status == "READY"
    assert bundle.ledger == {"city_cache_hits": 1, "recommendation_cache_hits": 1}
    assert bundle.discovery["recommendation"]["status"] == "CACHE"
    assert bundle.discovery["database"]["status"] == "CACHE"
    assert bundle.provider_calls == [] and bundle.degradations == []
    assert bundle.outbound == bundle.inbound == bundle.hotels == []


def test_expired_recommendation_is_ignored(store, monkeypatch):
    recommend_once(store)
    assert store.get_city_recommendation("成都") is not None
    # TTL 归零：created_at 严格早于 now，必然过期 → 不命中。
    monkeypatch.setattr(rec, "current_config", lambda: SimpleNamespace(city_recommendation_ttl_days=0))
    assert rec.recommendation_from_cache(TripIntent(destination=["成都"], days=3), store, "s2") is None


def test_fresher_city_pool_invalidates_cache(store):
    recommend_once(store)
    newer = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    # 把城市池全部行刷成比推荐新：池子变过，推荐可能过期。
    with store._connect() as conn:
        for table in ("city_pois", "city_poi_mentions", "city_evidences", "city_cache_meta"):
            conn.execute(f"UPDATE {table} SET updated_at=? WHERE city='成都'", (newer,))
    assert rec.recommendation_from_cache(TripIntent(destination=["成都"], days=3), store, "s2") is None


def test_place_id_validation_filters_partial_and_rejects_all(store):
    recommend_once(store)
    intent = TripIntent(destination=["成都"], days=3)
    # 删掉一个地点的池行：恰一半有效 → 仍命中，但只保留幸存地点。
    with store._connect() as conn:
        conn.execute("DELETE FROM city_pois WHERE place_id='p2'")
    bundle = rec.recommendation_from_cache(intent, store, "s2")
    assert bundle is not None
    assert [place.place_id for place in bundle.places] == ["park"]
    assert [card["place_id"] for card in bundle.extras["recommended_cards"]] == ["park"]
    # 全部失效 → None。
    with store._connect() as conn:
        conn.execute("DELETE FROM city_pois WHERE place_id='park'")
    assert rec.recommendation_from_cache(intent, store, "s2") is None


# ----------------------------------------------------------------------
# run_discovery 接线（缓存优先）
# ----------------------------------------------------------------------


def test_run_discovery_second_session_hits_cache_without_llm(store):
    seed(store, TWO_PLACES, TWO_GUIDES)
    basic = {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 3}
    s1 = sessions.create_session(store, basic)["session_id"]
    first = NativeModel([call("recall_city_guides"),
                         submit_step(lambda payload: [card_for(payload, "park"), card_for(payload, "p2")])])
    sessions.run_discovery(store, s1, llm_factory=lambda: LLM(model=first))
    assert store.get_planning_session(s1)["discovery_status"] == "READY"
    assert store.get_city_recommendation("成都") is not None

    # 第二次同城：缓存命中，连构造 LLM 都不该发生。
    s2 = sessions.create_session(store, {**basic, "start_date": "2026-10-03", "days": 2})["session_id"]
    constructed = []

    def exploding_factory():
        constructed.append(True)
        raise AssertionError("缓存命中时绝不能构造 LLM")

    sessions.run_discovery(store, s2, llm_factory=exploding_factory)
    assert constructed == []
    saved = store.get_planning_session(s2)
    assert saved["status"] == sessions.SESSION_READY
    assert saved["discovery_status"] == "READY"
    assert [card["place_id"] for card in saved["place_candidates"]] == ["park", "p2"]
    assert saved["prefetch"]["recommendation"]["cached"] is True
    assert saved["prefetch"]["recommendation"]["place_ids"] == ["park", "p2"]
    assert saved["prefetch"]["discovery"]["recommendation"]["status"] == "READY"
    assert saved["degradations"] == []
