"""app/workflow.py 的单元测试（PRD §24 / §25 / §28）。

workflow 就是那条固定 12 步流程：决定「做什么」的是代码，模型只在 4 处被调用。
所以本文件分三层：

1. **纯函数层**（规则意图解析、预算折算、交通/住宿比选、证据汇总、plan_payload）
   —— 直接构造 pydantic 模型，完全离线、确定，不需要任何替身；
2. **节点层**（node_parse_intent）—— 注入一个假模型，验证"模型漏字段时规则补回、
   模型给了就尊重模型"；
3. **全流程层**（execute_travel_run）—— 注入**假 ProviderHub + 假模型 + tmp_path 库**，
   验证产物落盘、run 状态、`RunResult.ok` 与 `status` 一致，以及"没识别出目的地时
   一次 Provider 都不许调"。

铁律：**本文件不联网、不读 API Key、不写 data/travelplan.db**。
所有 Provider 数据都是合成的测试输入（`FakeHub` 只回放构造好的对象）。
没有任何一条断言在说"真实查询成功了"；断言的对象是"代码拿这些输入做了什么"
（选哪个方案、扣了多少分、有没有落库、有没有调用 Provider、有没有如实降级）。
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any, get_args, get_origin, get_type_hints

import pytest

from app.llm import LLM
from app.models import (
    BUDGET_CATEGORY_TRANSPORT,
    Evidence,
    FlightOption,
    HotelOption,
    ItineraryDay,
    ItineraryItem,
    Place,
    RouteOption,
    SourceRef,
    TrainOption,
    TripIntent,
    TripPlan,
)
from app.prompts import (
    CRITIC_PROMPT,
    EXTRACT_PLACES_BATCH_PROMPT,
    EXTRACT_PLACES_PROMPT,
    FINAL_ANSWER_PROMPT,
    INTENT_PARSE_PROMPT,
)
from app.providers import ProviderCall, ProviderResult
from app.store import TravelPlanStore
from app.config import current_config
from app.workflow import (
    HOTEL_PREFERENCE_BONUS,
    HOTEL_UNKNOWN_PRICE_PENALTY,
    RESEARCH_QUERY_EXPANSION_PROMPT,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    TRANSPORT_OFF_DATE_PENALTY,
    TRANSPORT_PREFERENCE_BONUS,
    TRANSPORT_UNKNOWN_DURATION_PENALTY,
    TRANSPORT_UNKNOWN_PRICE_PENALTY,
    TravelState,
    _BUDGET_BARE_RE,
    _BUDGET_RES,
    _BUDGET_UNIT_RE,
    _backfill_intent,
    _budget_from,
    _door_to_door_minutes,
    _evidence_summary,
    _hotel_issues,
    _hotel_score,
    _inbound_span_issues,
    _off_date_issues,
    _raw_has,
    _rule_based_intent,
    _select_hotel,
    _select_transport,
    _transit_span_issues,
    _transport_preference_bonus,
    _transport_score,
    build_travel_graph,
    execute_travel_run,
    node_parse_intent,
    plan_payload,
    travel_graph,
)

# 固定参照日期：所有 fixture 都用它，测试不随系统时间腐烂。
TRIP_DATE = date(2026, 10, 1)
RUN_ID = "tp-test-run"


# ==================================================
# 零、合成测试输入（不是任何 Provider 的真实返回）
# ==================================================


def _intent(**overrides: Any) -> TripIntent:
    """一份完整的合成意图：北京 → 成都，2026-10-01 起 3 天 2 人。"""
    data: dict[str, Any] = {
        "destination": ["成都"],
        "origin": "北京",
        "start_date": TRIP_DATE,
        "days": 3,
        "travelers": 2,
        "budget_total": 6000.0,
    }
    data.update(overrides)
    return TripIntent(**data)


def _train(
    train_no: str = "G1",
    *,
    price: float | None = 800.0,
    duration: int | None = 600,
    **overrides: Any,
) -> TrainOption:
    data: dict[str, Any] = {
        "train_no": train_no,
        "provider": "12306",
        "train_type": "高铁",
        "duration_minutes": duration,
        "departure_at": datetime(2026, 10, 1, 8, 0),
        "arrival_at": datetime(2026, 10, 1, 18, 0),
    }
    if price is not None:
        data["price_range"] = {"min": price}
    data.update(overrides)
    return TrainOption(**data)


def _flight(
    flight_no: str = "CA4101",
    *,
    price: float | None = 880.0,
    duration: int | None = 220,
    **overrides: Any,
) -> FlightOption:
    data: dict[str, Any] = {
        "flight_no": flight_no,
        "provider": "tuniu",
        "airline": "中国国航",
        "duration_minutes": duration,
        "departure_at": datetime(2026, 10, 1, 8, 30),
        "arrival_at": datetime(2026, 10, 1, 12, 10),
    }
    if price is not None:
        data["price"] = price
    data.update(overrides)
    return FlightOption(**data)


def _hotel(
    name: str = "成都某酒店",
    *,
    nightly: float | None = 480.0,
    rating: float | None = 4.6,
    **overrides: Any,
) -> HotelOption:
    data: dict[str, Any] = {
        "hotel_id": f"H-{name}",
        "name": name,
        "provider": "tuniu",
        "rating": rating,
        "business_area": "天府广场",
        "room_type": "标准大床房",
        "address": "成都市青羊区人民中路",
    }
    if nightly is not None:
        data["price_per_night"] = nightly
        data["price_note"] = f"{nightly:g} 起价/晚"
    data.update(overrides)
    return HotelOption(**data)


def _plan(run_id: str = RUN_ID) -> TripPlan:
    """一份合成的 TripPlan（形状与 tests/test_store.py 一致）。"""
    item = ItineraryItem(
        id="i1",
        type="attraction",
        name="宽窄巷子",
        place_id="P1",
        lat=30.6691,
        lng=104.0555,
        start_time="09:00",
        end_time="11:00",
        duration_minutes=120,
        price=50.0,
        price_type="realtime",
        trust_score=80.0,
        ad_risk=5.0,
        evidence_ids=["e1"],
    )
    return TripPlan(
        run_id=run_id,
        query="成都三日",
        intent=TripIntent(destination=["成都"], origin="北京", days=1, travelers=2),
        days=[ItineraryDay(day_index=0, date=TRIP_DATE, area="青羊区", items=[item])],
    )


# ==================================================
# 一、假 ProviderHub / 假模型（全流程测试用）
# ==================================================


class FakeHub:
    """`ProviderHub` 的替身：只回放构造好的对象，并记录"被调用过什么"。

    刻意不复刻 ProviderHub 的寻址 / 缓存 / fallback —— 那些不是 workflow 的职责。
    它做两件真实的事情：把每次调用记进 `calls`（供断言"到底有没有调 Provider"），
    以及通过 store.save_source 落一条 source（evidence 的外键依赖它，不落就写不进库）。
    """

    #: 替身支持的全部能力。任何不在这个集合里的方法名都说明 workflow 越界了。
    KNOWN = (
        "search_trains",
        "search_flights",
        "search_hotels",
        "search_xiaohongshu",
        "search_douyin",
        "web_search",
        "search_poi",
        "poi_detail",
        "route",
        "geocode",
        "search_scenic_tickets",
    )

    def __init__(
        self,
        *,
        store: TravelPlanStore,
        run_id: str,
        trains: dict[tuple[str, str], list[TrainOption]] | None = None,
        flights: dict[tuple[str, str], list[FlightOption]] | None = None,
        hotels: list[HotelOption] | None = None,
        pois: list[Place] | None = None,
        web_items: list[dict[str, Any]] | None = None,
        ticket_price: float | None = 60.0,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.trains = trains or {}
        self.flights = flights or {}
        self.hotels = hotels or []
        self.pois = pois or []
        self.web_items = web_items or []
        self.ticket_price = ticket_price
        #: 方法名，按调用顺序（断言"调了哪几个能力"）。
        self.calls: list[str] = []
        self.call_args: list[tuple[str, dict[str, Any]]] = []
        self._details: list[ProviderCall] = []

    # ------------------------------------------------------------------
    def _note(self, tool: str, **kwargs: Any) -> int:
        self.calls.append(tool)
        self.call_args.append((tool, kwargs))
        return len(self._details)

    def _source_id(self, tool: str, index: int) -> str:
        return f"{self.run_id}-{tool}-{index}"

    def _finish(self, provider: str, source_type: str, tool: str, items: list[Any], index: int):
        status = "OK" if items else "EMPTY"
        call = ProviderCall(
            source_id=self._source_id(tool, index),
            provider=provider,
            source_type=source_type,
            tool=tool,
            query={"index": index},
            status=status,
            fetched_at=datetime.now(timezone.utc),
            duration_ms=1,
        )
        self.store.save_source(self.run_id, call.to_source())
        self._details.append(call)
        return ProviderResult(status=status, items=list(items), calls=[call], provider=provider)

    def _social_evidence(self, keyword: str, index: int) -> list[Evidence]:
        """每个检索词给 1 条带 place_mentions 的攻略（provider=xhs）。

        place_mentions 直接写成本次 POI 的名字：这样地点与证据的关联是**构造出来的**
        事实，而不是 workflow 猜出来的。
        """
        source_id = self._source_id("search_xiaohongshu", index)
        return [
            Evidence(
                id=f"{self.run_id}-e-xhs-{index}",
                provider="xhs",
                source_type="social",
                source_id=source_id,
                source_url=f"https://example.test/xhs/{index}",
                title=f"{keyword} 实测",
                author="本地人小王",
                published_at=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
                text="本地人常来，宽窄巷子早上人少，龙抄手人均 40 元，第二次来了",
                place_mentions=[place.name for place in self.pois],
                specific_dishes=["龙抄手"],
                raw_metrics={"likes": 1200, "comments": 80},
            )
        ]

    # --- 大交通 ---
    def search_trains(
        self, origin: str, destination: str, on: date, *, hedge: bool = False
    ) -> ProviderResult[TrainOption]:
        # 这个假 Hub 只放一份数据源，所以对冲与串行在这里没有区别；接受这个参数是为了
        # 与真实 Hub 的签名一致（真实 Hub 会按它决定要不要提前并发发途牛备胎）。
        index = self._note("search_trains", origin=origin, destination=destination, date=str(on))
        items = list(self.trains.get((origin, destination), []))
        return self._finish("12306", "train", "search_trains", items, index)

    def search_flights(
        self, origin: str, destination: str, on: date, *, travelers: int = 1
    ) -> ProviderResult[FlightOption]:
        index = self._note(
            "search_flights",
            origin=origin,
            destination=destination,
            date=str(on),
            travelers=travelers,
        )
        items = list(self.flights.get((origin, destination), []))
        return self._finish("tuniu", "flight", "search_flights", items, index)

    # --- 住宿 ---
    def search_hotels(
        self, city: str, check_in: date, check_out: date, *, travelers: int = 1
    ) -> ProviderResult[HotelOption]:
        index = self._note(
            "search_hotels", city=city, check_in=str(check_in), check_out=str(check_out)
        )
        return self._finish("tuniu", "hotel", "search_hotels", list(self.hotels), index)

    # --- 攻略 / 网页 ---
    def search_xiaohongshu(self, keyword: str, *, page: int = 1) -> ProviderResult[Evidence]:
        index = self._note("search_xiaohongshu", keyword=str(keyword))
        items = self._social_evidence(str(keyword), index)
        return self._finish("xhs", "social", "search_xiaohongshu", items, index)

    def search_douyin(self, keyword: str, *, page: int = 1) -> ProviderResult[Evidence]:
        index = self._note("search_douyin", keyword=str(keyword))
        return self._finish("douyin", "social", "search_douyin", [], index)

    def web_search(self, query: str, *, max_results: int = 5) -> ProviderResult[dict]:
        index = self._note("web_search", query=str(query), max_results=max_results)
        return self._finish("tavily", "web", "web_search", list(self.web_items), index)

    # --- 高德 ---
    def search_poi(
        self, keyword: str, city: str, *, page_size: int = 10, type_hint: str | None = None
    ) -> ProviderResult[Place]:
        index = self._note("search_poi", keyword=str(keyword), city=city)
        return self._finish("amap", "poi", "search_poi", list(self.pois), index)

    def poi_detail(self, poi_id: str) -> ProviderResult[dict]:
        index = self._note("poi_detail", poi_id=poi_id)
        return self._finish("amap", "poi", "poi_detail", [{"opening_hours": "09:00-22:00"}], index)

    def route(self, start: Any, end: Any, mode: str = "transit", *, city: str | None = None):
        index = self._note("route", start=str(start), end=str(end), mode=mode)
        items = [
            RouteOption(
                mode="transit",
                distance_meters=4200,
                duration_seconds=900,
                estimated_cost=4.0,
                provider="amap",
                verified=True,
            )
        ]
        return self._finish("amap", "route", "route", items, index)

    def geocode(self, address: str, city: str | None = None) -> ProviderResult[dict]:
        index = self._note("geocode", address=str(address), city=city)
        return self._finish(
            "amap",
            "geocode",
            "geocode",
            [{"longitude": 104.0640, "latitude": 30.6570, "city": "成都"}],
            index,
        )

    def search_scenic_tickets(self, scenic_name: str) -> ProviderResult[dict]:
        index = self._note("search_scenic_tickets", scenic_name=str(scenic_name))
        items = [] if self.ticket_price is None else [{"price": self.ticket_price}]
        return self._finish("tuniu", "ticket", "search_scenic_tickets", items, index)

    # --- 审计 / 来源 ---
    def source_refs(self) -> list[dict]:
        return [call.to_source_ref() for call in self._details]

    def audit_entries(self) -> list[dict]:
        return [
            {
                "provider": call.provider,
                "tool": call.tool,
                "status": call.status,
                "arguments": call.query,
                "item_count": 0,
                "duration_ms": call.duration_ms,
                "fetched_at": call.fetched_at.isoformat(),
                "source_id": call.source_id,
                "notes": [],
                "error": None,
            }
            for call in self._details
        ]

    def close(self) -> None:
        """注入的替身不该被 workflow 关掉：关了就说明 owns_hub 的判断错了。"""
        raise AssertionError("workflow 不应该关闭被注入的 hub")


class FakeChatModel:
    """LangChain ChatModel 的替身：按 system prompt 这个"调用点标识"分派预置的 JSON / 文本。

    它的价值在于让 workflow 的每一次模型调用都有确定的输入输出，
    从而能断言"模型给了什么 → 流程怎么用"，而不是断言"模型跑通了"。
    """

    def __init__(
        self,
        *,
        intent: dict[str, Any] | str | None = None,
        queries: list[str] | None = None,
        places: list[dict[str, Any]] | None = None,
        critic: dict[str, Any] | None = None,
        prose: str = "这是模型写的行程说明，数字全部照抄给定输入。",
    ) -> None:
        self.model_name = "fake-model"
        self.intent = intent
        self.queries = queries
        self.places = places
        self.critic = critic
        self.prose = prose
        self.prompts: list[str] = []

    def invoke(self, messages: list[Any]) -> Any:
        system = str(getattr(messages[0], "content", "")) if messages else ""
        self.prompts.append(system)
        user = str(getattr(messages[1], "content", "")) if len(messages) > 1 else ""
        # 按 prompt 对象本身分派，而不是靠关键词猜：关键词猜错会让整条模型分支
        # 静默退化成"模型不可用"，测试却照样绿。
        if system == INTENT_PARSE_PROMPT:
            payload: Any = self.intent
        elif system == RESEARCH_QUERY_EXPANSION_PROMPT:
            payload = self.queries if self.queries is not None else []
        elif system == EXTRACT_PLACES_BATCH_PROMPT:
            # 批量抽取：按输入里给的证据 id 逐条归属，这样"同一份攻略不做第二遍"
            # 与"归属不能串篇"这两件事都在测试里可断言。
            ids = [
                line.split("：", 1)[1].strip()
                for line in user.splitlines()
                if line.startswith("### 证据 id：") and line.split("：", 1)[1].strip()
            ]
            payload = {
                "results": [
                    {"evidence_id": item_id, "places": self.places or []} for item_id in ids
                ]
            }
        elif system == EXTRACT_PLACES_PROMPT:
            payload = {"places": self.places or []}
        elif system == CRITIC_PROMPT:
            payload = self.critic if self.critic is not None else {
                "verdict": "ok",
                "issues": [],
                "confidence": 80,
            }
        else:
            # FINAL_ANSWER_PROMPT（成文）走这里：回一段散文，不是 JSON。
            payload = None
        if payload is None:
            return SimpleNamespace(content=self.prose)
        if isinstance(payload, str):
            return SimpleNamespace(content=payload)
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class UnparseableModel:
    """只会回一句中文的模型：验证"模型不可用 → 规则兜底 + 不去查 Provider"。"""

    def __init__(self) -> None:
        self.model_name = "unparseable-model"

    def invoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(content="抱歉，我无法从这句话里解析出结构化需求。")


def _pois() -> list[Place]:
    """4 个合成 POI（真实量级坐标，但不是从高德抄来的）。"""
    raw = [
        ("B0FF1", "宽窄巷子", "30.6691", "104.0555", "青羊区", "宽窄巷子", "09:00-22:00", "风景名胜"),
        ("B0FF2", "成都博物馆", "30.6570", "104.0640", "青羊区", "天府广场", "09:00-17:00", "文化场馆"),
        ("B0FF3", "武侯祠", "30.6469", "104.0488", "武侯区", "武侯祠", "08:00-18:00", "风景名胜"),
        ("B0FF4", "龙抄手总店", "30.6572", "104.0730", "锦江区", "春熙路", "08:00-21:00", "餐饮服务"),
    ]
    places: list[Place] = []
    for poi_id, name, lat, lng, district, business_area, hours, kind in raw:
        places.append(
            Place.from_poi(
                {
                    "poi_id": poi_id,
                    "name": name,
                    "type": kind,
                    "latitude": lat,
                    "longitude": lng,
                    "address": f"成都市{district}",
                    "city": "成都",
                    "district": district,
                    "business_area": business_area,
                    "opening_hours": hours,
                },
                city="成都",
            )
        )
    return places


def make_hub(store: TravelPlanStore, *, ticket_price: float | None = 60.0) -> FakeHub:
    """一套能跑通全流程的合成数据源（去程/回程火车+航班、2 家酒店、4 个 POI）。"""
    outbound_train = _train(
        "G89", price=780.0, duration=450, origin_station="北京西", destination_station="成都东"
    )
    inbound_train = _train(
        "G90",
        price=780.0,
        duration=450,
        origin_station="成都东",
        destination_station="北京西",
        departure_at=datetime(2026, 10, 3, 13, 0),
        arrival_at=datetime(2026, 10, 3, 20, 30),
    )
    outbound_flight = _flight(
        "CA4101", price=880.0, departure_airport="首都机场T3", arrival_airport="双流机场T2"
    )
    inbound_flight = _flight(
        "CA4102",
        price=900.0,
        departure_airport="双流机场T2",
        arrival_airport="首都机场T3",
        departure_at=datetime(2026, 10, 3, 18, 0),
        arrival_at=datetime(2026, 10, 3, 21, 40),
    )
    hotels = [
        _hotel("成都天府广场智选假日酒店", nightly=480.0, rating=4.6),
        _hotel("成都宽窄巷子民宿", nightly=320.0, rating=4.2),
    ]
    web_items = [
        {
            "title": "成都三日游路线参考",
            "snippet": (
                "宽窄巷子、武侯祠、成都博物馆按顺序走比较顺，市区内地铁票价 2-4 元；"
                "人民公园下午喝盖碗茶最合适，锦里傍晚去人少。"
            ),
            "url": "https://example.test/web/1",
        }
    ]
    return FakeHub(
        store=store,
        run_id=RUN_ID,
        trains={("北京", "成都"): [outbound_train], ("成都", "北京"): [inbound_train]},
        flights={("北京", "成都"): [outbound_flight], ("成都", "北京"): [inbound_flight]},
        hotels=hotels,
        pois=_pois(),
        web_items=web_items,
        ticket_price=ticket_price,
    )


@pytest.fixture()
def store(tmp_path) -> TravelPlanStore:
    """每个用例一个独立库文件：绝不碰 data/travelplan.db。"""
    return TravelPlanStore(tmp_path / "nested" / "travelplan.db")


# ==================================================
# 二、规则兜底意图解析（三个真实修过的正则 bug 的回归）
# ==================================================


class TestRuleBasedIntent:
    def test_full_sentence_extracts_every_field(self):
        """`10月1日` 里的「1日」曾经冒充行程天数，把 5 天解析成 1 天。"""
        intent = _rule_based_intent("10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照")

        assert intent.origin == "北京"
        assert intent.destination == ["成都"]
        assert (intent.start_date.month, intent.start_date.day) == (10, 1)
        assert intent.start_date.year == date.today().year  # 只有月日 → 取当年
        assert intent.days == 5  # 必须是 5，不是被「1日」骗出来的 1
        assert intent.travelers == 2
        assert intent.budget_total == 6000.0

    def test_destination_followed_by_chinese_comma(self):
        """目的地后面直接跟中文逗号，正则不能因为「不是玩/旅游/行尾」就整个不认。"""
        intent = _rule_based_intent("三天两晚从上海去杭州，两个人")

        assert intent.origin == "上海"
        assert intent.destination == ["杭州"]
        assert intent.days == 3
        assert intent.travelers == 2

    def test_days_survives_when_written_as_day_tour(self):
        """「5日游」的「日」不算行程单位，必须靠 `N日游` 这条独立规则捞回来。"""
        intent = _rule_based_intent("从长春去成都，5日游，一个人，预算3000元")

        assert intent.origin == "长春"
        assert intent.destination == ["成都"]
        assert intent.days == 5
        assert intent.travelers == 1
        assert intent.budget_total == 3000.0

    def test_wan_multiplier_and_chinese_day_count(self):
        """「1.5万」= 15000，不能被截成 1.5；「四天」要能认出中文数字。"""
        intent = _rule_based_intent("国庆从广州到重庆玩四天，3个人，预算1.5万")

        assert intent.origin == "广州"
        assert intent.destination == ["重庆"]
        assert intent.days == 4
        assert intent.travelers == 3
        assert intent.budget_total == 15000.0

    def test_unrecognizable_query_keeps_everything_empty(self):
        """宁可什么都不认，也不要猜一个城市继续往下跑。"""
        intent = _rule_based_intent("帮我规划一下")

        assert intent.destination == []
        assert intent.origin is None
        assert intent.start_date is None
        assert intent.days == 1
        assert intent.travelers == 1
        assert intent.budget_total is None

    def test_transport_preference_mapping(self):
        assert _rule_based_intent("从北京坐高铁去成都玩3天").transport_preferences == ["火车"]
        assert _rule_based_intent("从北京坐飞机去成都玩3天").transport_preferences == ["飞机"]
        assert _rule_based_intent("从北京去成都玩3天").transport_preferences == []


class TestBudgetFrom:
    def test_wan_scales_by_ten_thousand(self):
        assert _budget_from(_BUDGET_BARE_RE.search("预算1.5万")) == 15000.0

    def test_yuan_unit_pattern(self):
        assert _budget_from(_BUDGET_UNIT_RE.search("两个人花5000元")) == 5000.0

    def test_comma_separated_amount(self):
        assert _budget_from(_BUDGET_BARE_RE.search("预算12,000")) == 12000.0

    def test_res_order_prefers_budget_prefixed_pattern(self):
        """`_BUDGET_RES` 先试「预算N」再试「N元」，两条都命中时以预算为准。"""
        first, second = _BUDGET_RES

        assert first is _BUDGET_BARE_RE and second is _BUDGET_UNIT_RE
        assert _rule_based_intent("去成都玩3天，2个人，预算8000，回程机票2000元").budget_total == 8000.0

    def test_unparseable_amount_returns_none(self):
        @dataclass
        class _StubMatch:
            def group(self, index: int):
                return {1: "没有数字", 2: None}[index]

        assert _budget_from(_StubMatch()) is None


# ==================================================
# 三、意图字段契约（prompt 让模型给什么，解析就必须认什么）
# ==================================================


class TestIntentFieldContract:
    def test_accepts_the_field_names_the_prompt_actually_asks_for(self):
        """只认别的拼法，会让"照着 prompt 回答"的模型丢掉目的地。"""
        intent = TripIntent.from_llm(
            {
                "origin_city": "北京",
                "destination_city": "成都",
                "start_date": "2026-10-01",
                "days": 5,
                "travelers": 2,
                "budget": 6000,
                "transport_preference": "高铁",
                "hotel_level": "四星",
                "avoid": ["爬山"],
                "preferences": ["美食"],
            }
        )

        assert intent.destination == ["成都"]
        assert intent.origin == "北京"
        assert intent.days == 5
        assert intent.travelers == 2
        assert intent.budget_total == 6000.0
        assert intent.transport_preferences == ["高铁"]
        assert intent.hotel_preferences == ["四星"]
        assert intent.constraints == ["爬山"]

    def test_prompt_is_asking_for_those_names(self):
        """锁住"prompt 与解析器对得上"这件事，改 prompt 时会被提醒。"""
        for key in ("origin_city", "destination_city", "transport_preference", "hotel_level", "avoid"):
            assert key in INTENT_PARSE_PROMPT


class TestRawHas:
    def test_present_value_counts_as_given(self):
        assert _raw_has({"destination_city": "成都"}, "destination") is True
        assert _raw_has({"days": 3}, "days") is True
        assert _raw_has({"budget": 0}, "budget_total") is True

    def test_empty_values_count_as_not_given(self):
        for empty in (None, "", [], {}):
            assert _raw_has({"destination_city": empty}, "destination") is False

    def test_absent_key_counts_as_not_given(self):
        assert _raw_has({"days": 3}, "destination") is False
        assert _raw_has({}, "origin") is False

    def test_alternate_spellings_are_all_checked(self):
        for key in ("destination", "destination_city", "city", "目的地"):
            assert _raw_has({key: "成都"}, "destination") is True


class TestBackfillIntent:
    def test_fills_only_the_fields_the_model_omitted(self):
        rules = _rule_based_intent("10月1日从北京去成都玩5天，两个人，预算6000")
        model_intent = TripIntent.from_llm({"destination_city": "成都", "days": 2})

        filled, labels = _backfill_intent(
            model_intent, {"destination_city": "成都", "days": 2}, rules
        )

        assert filled.destination == ["成都"]
        assert filled.days == 2  # 模型给了 → 尊重模型，不覆盖成 5
        assert filled.origin == "北京"  # 模型没给 → 规则补回
        assert filled.start_date == rules.start_date
        assert filled.travelers == 2
        assert filled.budget_total == 6000.0
        assert set(labels) == {"出发地", "出发日期", "人数", "预算"}
        assert "目的地" not in labels and "天数" not in labels

    def test_nothing_to_fill_returns_the_same_intent(self):
        raw = {
            "destination_city": "成都",
            "origin_city": "北京",
            "start_date": "2026-10-01",
            "days": 5,
            "travelers": 2,
            "budget": 6000,
        }
        intent = TripIntent.from_llm(raw)

        filled, labels = _backfill_intent(intent, raw, _rule_based_intent("从北京去成都玩5天"))

        assert labels == []
        assert filled is intent  # 没有需要补的字段时不做无意义复制

    def test_empty_model_values_are_treated_as_missing(self):
        """模型把字段写成 null / 空串时，必须当成"没给"，由规则补上。

        若把 `None`/`""` 当成"给过"，目的地为空会让流程停下追问 ——
        而用户明明说了「从北京去成都」。
        """
        rules = _rule_based_intent("从北京去成都玩5天")
        model_intent = TripIntent.from_llm({"destination_city": None, "origin_city": ""})

        filled, labels = _backfill_intent(
            model_intent, {"destination_city": None, "origin_city": ""}, rules
        )

        assert filled.destination == ["成都"]
        assert filled.origin == "北京"
        assert filled.days == 5  # 模型没给天数 → 用规则的 5
        assert {"目的地", "出发地", "天数"} <= set(labels)


class TestNodeParseIntent:
    def test_backfills_origin_the_model_left_out(self, store):
        """端到端一点：假模型只给目的地/天数/人数，出发地必须由规则补回并留痕。"""
        store.create_run(RUN_ID)
        state = {
            "run_id": RUN_ID,
            "query": "10月1日从北京去成都玩5天，两个人",
            "llm": LLM(model=FakeChatModel(intent={"destination_city": "成都", "days": 3, "travelers": 2})),
            "store": store,
            "hub": None,
        }

        out = node_parse_intent(state)

        intent = out["intent"]
        assert intent.destination == ["成都"]
        assert intent.origin == "北京"
        assert intent.days == 3  # 模型给了天数，规则里的 5 天不许覆盖
        assert intent.start_date == TRIP_DATE
        assert intent.travelers == 2
        assert out["status"] == ""  # 有目的地 → 不追问
        steps = out["stages"][0]["steps"]
        assert any("模型未给出出发地" in step for step in steps)

    def test_unparseable_model_falls_back_to_rules_and_asks_for_destination(self, store):
        store.create_run(RUN_ID)
        state = {
            "run_id": RUN_ID,
            "query": "帮我规划一下",
            "llm": LLM(model=UnparseableModel()),
            "store": store,
            "hub": None,
        }

        out = node_parse_intent(state)

        assert out["status"] == STATUS_NEEDS_CLARIFICATION
        assert out["intent"].destination == []
        assert "目的地" in out["clarify"]
        assert out["degradations"]  # 如实记录了模型这次没成功


# ==================================================
# 四、交通比选（PRD §24）
# ==================================================


class TestDoorToDoorMinutes:
    def test_flight_includes_airport_buffers_and_transfer(self):
        from app.planner import (
            AIRPORT_BAGGAGE_EXIT_MINUTES,
            AIRPORT_CHECKIN_BUFFER_MINUTES,
            AIRPORT_CITY_TRANSFER_MINUTES,
        )

        assert _door_to_door_minutes(_flight(duration=220)) == (
            220
            + AIRPORT_CHECKIN_BUFFER_MINUTES
            + AIRPORT_BAGGAGE_EXIT_MINUTES
            + AIRPORT_CITY_TRANSFER_MINUTES
        )

    def test_train_uses_train_buffers(self):
        from app.planner import (
            TRAIN_EXIT_BUFFER_MINUTES,
            TRAIN_STATION_BUFFER_MINUTES,
            TRAIN_STATION_TRANSFER_MINUTES,
        )

        assert _door_to_door_minutes(_train(duration=450)) == (
            450
            + TRAIN_STATION_BUFFER_MINUTES
            + TRAIN_EXIT_BUFFER_MINUTES
            + TRAIN_STATION_TRANSFER_MINUTES
        )

    def test_unknown_duration_is_none_not_zero(self):
        assert _door_to_door_minutes(_train(duration=None)) is None


class TestTransportScore:
    def test_known_option_scores_from_price_and_door_to_door(self):
        score, parts = _transport_score(
            _train(price=800.0, duration=600), direction="outbound", intent=_intent()
        )

        assert parts["价格"] == 8.0  # 800 / 100
        assert parts["门到门时长"] == 69.0  # (600+45+15+30) / 10
        assert parts["首末天影响"] == 0.0
        assert parts["偏好"] == 0.0
        assert score == 77.0

    def test_unknown_price_is_penalised_not_treated_as_free(self):
        _, parts = _transport_score(_train(price=None), direction="outbound", intent=_intent())

        assert parts["价格"] == TRANSPORT_UNKNOWN_PRICE_PENALTY
        assert parts["价格"] > 0

    def test_unknown_duration_is_penalised_not_treated_as_instant(self):
        _, parts = _transport_score(_train(duration=None), direction="outbound", intent=_intent())

        assert parts["门到门时长"] == TRANSPORT_UNKNOWN_DURATION_PENALTY
        assert parts["门到门时长"] > 0

    def test_outbound_late_arrival_is_penalised(self):
        late = _flight(arrival_at=datetime(2026, 10, 1, 21, 40))

        _, parts = _transport_score(late, direction="outbound", intent=_intent())

        assert parts["首末天影响"] == 25.0

    def test_inbound_early_departure_is_penalised(self):
        early = _train(
            departure_at=datetime(2026, 10, 3, 10, 0), arrival_at=datetime(2026, 10, 3, 18, 0)
        )

        _, parts = _transport_score(early, direction="inbound", intent=_intent())

        assert parts["首末天影响"] == 25.0

    def test_inbound_does_not_use_the_outbound_rule(self):
        """回程看的是"最晚几点必须收工"，不是"抵达后能不能出门"。"""
        evening = _train(
            departure_at=datetime(2026, 10, 3, 18, 0), arrival_at=datetime(2026, 10, 3, 23, 0)
        )

        _, parts = _transport_score(evening, direction="inbound", intent=_intent())

        assert parts["首末天影响"] == 0.0

    def test_departing_on_the_agreed_day_costs_nothing(self):
        _, parts = _transport_score(_train(), direction="outbound", intent=_intent())

        assert parts["日期一致"] == 0.0

    def test_departing_a_day_early_is_penalised(self):
        """实测 12306 查 10-02 会回 10-01 的车；不罚的话它靠"早一天"就能赢价格。"""
        early = _train(
            departure_at=datetime(2026, 9, 30, 8, 0), arrival_at=datetime(2026, 9, 30, 18, 0)
        )

        _, parts = _transport_score(early, direction="outbound", intent=_intent())

        assert parts["日期一致"] == TRANSPORT_OFF_DATE_PENALTY

    def test_off_date_penalty_scales_with_the_gap(self):
        two_days_early = _train(
            departure_at=datetime(2026, 9, 29, 8, 0), arrival_at=datetime(2026, 9, 29, 18, 0)
        )

        _, parts = _transport_score(two_days_early, direction="outbound", intent=_intent())

        assert parts["日期一致"] == TRANSPORT_OFF_DATE_PENALTY * 2

    def test_inbound_is_scored_against_the_last_day(self):
        """回程的基准是最后一天（10-01 起 3 天 → 10-03），不是第一天。"""
        on_time = _train(
            departure_at=datetime(2026, 10, 3, 9, 0), arrival_at=datetime(2026, 10, 3, 15, 0)
        )

        _, parts = _transport_score(on_time, direction="inbound", intent=_intent())

        assert parts["日期一致"] == 0.0

    def test_no_departure_time_is_not_treated_as_on_time(self):
        """取不到发车时间是无从比较，不是"日期一致"。"""
        from app.planner import departure_day_offset

        assert departure_day_offset(_train(departure_at=None), TRIP_DATE) is None
        assert departure_day_offset(_train(), None) is None

    def test_preference_bonus_only_applies_to_the_matching_mode(self):
        intent = _intent(transport_preferences=["高铁"])

        assert _transport_preference_bonus(_train(), intent) == TRANSPORT_PREFERENCE_BONUS
        assert _transport_preference_bonus(_flight(), intent) == 0.0

        _, train_parts = _transport_score(_train(), direction="outbound", intent=intent)
        _, flight_parts = _transport_score(_flight(), direction="outbound", intent=intent)
        assert train_parts["偏好"] == -TRANSPORT_PREFERENCE_BONUS
        assert flight_parts["偏好"] == 0.0

    def test_flight_preference_bonus_matches_flight(self):
        intent = _intent(transport_preferences=["飞机"])

        assert _transport_preference_bonus(_flight(), intent) == TRANSPORT_PREFERENCE_BONUS
        assert _transport_preference_bonus(_train(), intent) == 0.0


class TestSelectTransport:
    def test_prefers_the_lower_total_score(self):
        cheap = _train("G1", price=800.0, duration=600)
        pricey = _train("G2", price=1200.0, duration=600)

        selected, alternatives, reason, scores = _select_transport(
            [pricey, cheap], direction="outbound", intent=_intent()
        )

        assert selected.train_no == "G1"
        assert [option.train_no for option in alternatives] == ["G2"]
        assert scores["G1 高铁 ¥800"]["价格"] == 8.0
        assert scores["G2 高铁 ¥1200"]["价格"] == 12.0
        assert "G1 高铁 ¥800" in reason
        assert selected.selection_reason == reason

    def test_unknown_duration_option_cannot_win_by_looking_cheap(self):
        """最容易出的错：没时长的方案因为"算不出时长"被当成 0 分钟而胜出。"""
        unknown = _train("G2", price=10.0, duration=None)
        known = _train("G1", price=800.0, duration=600)

        selected, alternatives, reason, _ = _select_transport(
            [unknown, known], direction="outbound", intent=_intent()
        )

        assert selected.train_no == "G1"
        assert all(option.train_no != "G2" for option in alternatives)
        assert "在 1 个候选里综合分最低" in reason

    def test_preference_bonus_can_flip_the_decision(self):
        flight = _flight("CA1", price=900.0)
        train = _train("G1", price=900.0, duration=600)

        assert _transport_score(flight, direction="outbound", intent=_intent())[0] < (
            _transport_score(train, direction="outbound", intent=_intent())[0]
        )
        selected, _, _, _ = _select_transport(
            [flight, train], direction="outbound", intent=_intent(transport_preferences=["火车"])
        )

        assert selected.train_no == "G1"

    def test_empty_candidates_returns_none_without_fabricating(self):
        selected, alternatives, reason, scores = _select_transport(
            [], direction="outbound", intent=_intent()
        )

        assert selected is None
        assert alternatives == []
        assert reason == ""
        assert scores == {}

    def test_a_cheaper_off_date_train_cannot_beat_the_one_on_the_right_day(self):
        """真事故的形状：12306 返回早一天的车，它便宜 50 块，于是行程整体前移了一天。"""
        on_time = _train(
            "G1", price=850.0, duration=600,
            departure_at=datetime(2026, 10, 1, 8, 0), arrival_at=datetime(2026, 10, 1, 18, 0),
        )
        off_date = _train(
            "D203", price=800.0, duration=540,
            departure_at=datetime(2026, 9, 30, 8, 16), arrival_at=datetime(2026, 9, 30, 9, 46),
        )

        selected, _, _, _ = _select_transport(
            [off_date, on_time], direction="outbound", intent=_intent()
        )

        assert selected.train_no == "G1"

    def test_when_every_candidate_is_off_date_the_reason_says_so(self):
        """没有一天对得上时不能装作没事：选最接近的，并把日期不符写进理由。"""
        off_date = _train(
            "D203", price=800.0, duration=540,
            departure_at=datetime(2026, 9, 30, 8, 16), arrival_at=datetime(2026, 9, 30, 9, 46),
        )
        further_off = _train(
            "K1", price=100.0, duration=540,
            departure_at=datetime(2026, 9, 29, 8, 0), arrival_at=datetime(2026, 9, 29, 9, 30),
        )

        selected, _, reason, _ = _select_transport(
            [further_off, off_date], direction="outbound", intent=_intent()
        )

        assert selected.train_no == "D203"
        assert "2026-09-30" in reason
        assert "2026-10-01" in reason
        assert "-1" in reason or "差" in reason

    def test_the_reason_stays_quiet_when_the_date_matches(self):
        _, _, reason, _ = _select_transport([_train()], direction="outbound", intent=_intent())

        assert "相差" not in reason


    def test_alternatives_carry_a_reject_reason(self):
        options = [_train(f"G{i}", price=800.0 + 50 * i) for i in range(1, 4)]

        _, alternatives, _, _ = _select_transport(options, direction="inbound", intent=_intent())

        assert len(alternatives) == 2
        for option in alternatives:
            assert "未选中" in option.selection_reason

    def test_reason_admits_the_transfer_is_an_estimate(self):
        _, _, reason, _ = _select_transport(
            [_train("G1", price=800.0)], direction="outbound", intent=_intent()
        )

        assert "门到门" in reason
        assert "常量估算" in reason  # 没有真实接驳路线时如实说明，不假装是实测


class TestOffDateIssues:
    def test_on_time_transport_raises_nothing(self):
        assert _off_date_issues(_intent(), _train(), None) == []

    def test_late_outbound_is_reported_as_off_date(self):
        late = _train(
            departure_at=datetime(2026, 9, 30, 8, 0), arrival_at=datetime(2026, 9, 30, 18, 0)
        )

        issues = _off_date_issues(_intent(), late, None)

        assert [issue.code for issue in issues] == ["OUTBOUND_OFF_DATE"]
        assert issues[0].severity == "warning"
        assert "2026-09-30" in issues[0].reason
        assert "2026-10-01" in issues[0].reason

    def test_inbound_is_compared_against_the_last_day(self):
        early_back = _train(
            departure_at=datetime(2026, 10, 2, 9, 0), arrival_at=datetime(2026, 10, 2, 15, 0)
        )

        issues = _off_date_issues(_intent(), None, early_back)

        assert [issue.code for issue in issues] == ["INBOUND_OFF_DATE"]
        assert issues[0].day_index == 2  # 10-01 起 3 天 → 最后一天是 index 2

    def test_both_ends_off_date_produces_two_issues(self):
        late = _train(
            departure_at=datetime(2026, 9, 30, 8, 0), arrival_at=datetime(2026, 9, 30, 18, 0)
        )
        early_back = _train(
            departure_at=datetime(2026, 10, 2, 9, 0), arrival_at=datetime(2026, 10, 2, 15, 0)
        )

        issues = _off_date_issues(_intent(), late, early_back)

        assert sorted(issue.code for issue in issues) == [
            "INBOUND_OFF_DATE",
            "OUTBOUND_OFF_DATE",
        ]

    def test_missing_dates_do_not_invent_a_mismatch(self):
        assert _off_date_issues(_intent(start_date=None), _train(), None) == []


class TestInboundSpanIssues:
    """回程"到不了家"与"要坐两天"都必须写出来。

    真机事故（2026-09-18）：
      * 上海→杭州两个数据源都不可用，一个回程候选都没有，行程里没有回家的那一段，
        `plan.warnings` 里也一个字都没提；
      * 长春→成都只有一趟 K546（10-06 07:58 开、10-08 06:45 到，48 小时），
        行程表排到 10-06 就结束了，10-07 / 10-08 两天的在途没有任何交代。
    """

    def test_a_same_day_return_raises_nothing(self):
        same_day = _train(
            departure_at=datetime(2026, 10, 2, 20, 0), arrival_at=datetime(2026, 10, 2, 23, 0)
        )

        assert _inbound_span_issues(_intent(), same_day) == []

    def test_no_return_option_is_disclosed(self):
        issues = _inbound_span_issues(_intent(), None)

        assert [issue.code for issue in issues] == ["INBOUND_UNAVAILABLE"]
        assert issues[0].severity == "warning"
        assert "没有选中任何回程方案" in issues[0].reason
        assert issues[0].day_index == 2  # 10-01 起 3 天 → 最后一天是 index 2

    def test_a_two_day_return_is_disclosed(self):
        long_ride = _train(
            departure_at=datetime(2026, 10, 2, 7, 58), arrival_at=datetime(2026, 10, 4, 6, 45)
        )

        issues = _inbound_span_issues(_intent(), long_ride)

        assert [issue.code for issue in issues] == ["INBOUND_MULTI_DAY"]
        assert issues[0].severity == "warning"
        assert "跨 3 天" in issues[0].reason
        assert "2026-10-04" in issues[0].reason
        assert "2026-10-03" in issues[0].reason  # 行程最后一天

    def test_a_return_landing_on_the_last_day_is_not_flagged(self):
        """末日深夜到家的红眼回程不算跨天：arrival.date() 仍是最后一天。"""
        red_eye_back = _train(
            departure_at=datetime(2026, 10, 3, 21, 30), arrival_at=datetime(2026, 10, 3, 23, 59)
        )

        assert _inbound_span_issues(_intent(), red_eye_back) == []

    def test_missing_dates_do_not_invent_a_span(self):
        assert _inbound_span_issues(_intent(start_date=None), _train()) == []


class TestMissingCandidatesAreDisclosed:
    """去程 / 住宿一个候选都没有时，必须进 plan.warnings，不能只留在 audit 的降级记录里。

    真机事故（2026-09-18，广州→重庆 `tp-20260918-232247-ebca73`）：12306 与途牛航班
    同时 TIMEOUT、途牛酒店 UNAVAILABLE，于是 4 天行程里既没有去程也没有住宿 ——
    D0 08:30 就在重庆逛景点、`budget.住宿 = 0` 而 `projected_total` 还显示"预算内"，
    `plan.warnings` 一条都没提，只有 audit.degradations 写了两行（前端把它汇总成
    "有 2 个数据源为降级返回"这种笼统计数）。这与回程的 INBOUND_UNAVAILABLE 不对称。
    """

    def test_no_outbound_option_is_disclosed(self):
        issues = _transit_span_issues(_intent(), None, 0)

        assert [issue.code for issue in issues] == ["OUTBOUND_UNAVAILABLE"]
        assert issues[0].severity == "warning"
        assert issues[0].day_index == 0
        assert "成都" in issues[0].reason  # 得说清是哪个目的地
        assert "不替这次空缺编一趟车" in issues[0].reason

    def test_an_outbound_option_suppresses_the_warning(self):
        same_day = _train(
            departure_at=datetime(2026, 10, 1, 8, 0), arrival_at=datetime(2026, 10, 1, 16, 0)
        )

        assert _transit_span_issues(_intent(), same_day, 0) == []

    def test_no_hotel_is_disclosed_with_the_number_of_nights(self):
        issues = _hotel_issues(_intent(), None)  # 3 天 → 2 晚

        assert [issue.code for issue in issues] == ["HOTEL_UNAVAILABLE"]
        assert issues[0].severity == "warning"
        assert issues[0].day_index == 0
        assert "2 晚" in issues[0].reason
        assert "没有用估算价代替" in issues[0].reason

    def test_a_selected_hotel_suppresses_the_warning(self):
        assert _hotel_issues(_intent(), _hotel(nightly=400.0)) == []

    def test_a_day_trip_does_not_need_a_hotel(self):
        """当天往返不需要住宿，不该报"缺住宿"。"""
        assert _hotel_issues(_intent(days=1), None) == []


# ==================================================
# 五、住宿比选
# ==================================================


class TestHotelScoring:
    def test_score_uses_nightly_price_and_rating(self):
        score, parts = _hotel_score(_hotel(nightly=400.0, rating=4.5), _intent())

        assert parts["每晚价格"] == 4.0
        assert parts["评分"] == -9.0
        assert parts["住宿偏好"] == 0.0
        assert score == -5.0

    def test_unknown_price_is_penalised(self):
        _, parts = _hotel_score(_hotel(nightly=None), _intent())

        assert parts["每晚价格"] == HOTEL_UNKNOWN_PRICE_PENALTY

    def test_preference_hit_gives_a_bonus(self):
        hotel = _hotel(nightly=400.0, rating=4.5, room_type="标准大床房（含早餐）")

        _, parts = _hotel_score(hotel, _intent(hotel_preferences=["含早餐"]))

        assert parts["住宿偏好"] == -HOTEL_PREFERENCE_BONUS
        assert parts["命中偏好"] == 1

    def test_preference_absent_from_text_is_not_a_hit(self):
        _, parts = _hotel_score(_hotel(nightly=400.0), _intent(hotel_preferences=["含早餐"]))

        assert parts["住宿偏好"] == 0.0
        assert parts["命中偏好"] == 0


class TestSelectHotel:
    def test_prefers_the_lower_score(self):
        cheaper = _hotel("便宜酒店", nightly=300.0, rating=4.5)
        pricey = _hotel("贵的酒店", nightly=900.0, rating=4.5)

        selected, alternatives, reason = _select_hotel([pricey, cheaper], _intent())

        assert selected.name == "便宜酒店"
        assert [hotel.name for hotel in alternatives] == ["贵的酒店"]
        assert "便宜酒店" in reason
        assert selected.selection_reason == reason

    def test_preference_bonus_flips_the_decision(self):
        plain = _hotel("A酒店", nightly=300.0, rating=4.5)
        with_breakfast = _hotel("B酒店", nightly=300.0, rating=4.5, room_type="标准间（含早餐）")

        assert _select_hotel([plain, with_breakfast], _intent())[0].name == "A酒店"
        selected, _, _ = _select_hotel(
            [plain, with_breakfast], _intent(hotel_preferences=["含早餐"])
        )

        assert selected.name == "B酒店"

    def test_unknown_price_hotel_loses_to_a_priced_one(self):
        """没价格的酒店不能被当成免费，从而"因为便宜"胜出。"""
        unknown = _hotel("没价格酒店", nightly=None, rating=5.0, room_type="豪华套房")
        priced = _hotel("有价格酒店", nightly=500.0, rating=4.0)

        selected, _, _ = _select_hotel([unknown, priced], _intent())

        assert selected.name == "有价格酒店"

    def test_empty_candidates_is_the_truthful_empty_result(self):
        selected, alternatives, reason = _select_hotel([], _intent())

        assert selected is None
        assert alternatives == []
        assert reason == ""

    def test_unknown_price_is_stated_in_the_reason(self):
        selected, _, reason = _select_hotel(
            [_hotel("没价格酒店", nightly=None, rating=4.0)], _intent()
        )

        assert selected is not None
        assert "未返回价格" in reason
        assert "不计住宿费用" in reason


# ==================================================
# 六、证据汇总 / plan_payload
# ==================================================


class TestEvidenceSummary:
    def test_returns_the_three_documented_keys(self):
        plan = TripPlan(
            run_id=RUN_ID,
            intent=_intent(),
            sources=[SourceRef(source_id="s1"), SourceRef(source_id="s2")],
            days=[
                ItineraryDay(
                    day_index=0,
                    items=[ItineraryItem(id="i1", type="attraction", name="宽窄巷子", place_id="P1")],
                )
            ],
        )
        state = {
            "places": [
                Place(place_id="P1", name="宽窄巷子", amap_verified=True),
                Place(place_id="P2", name="武侯祠", amap_verified=False),
            ],
            "candidate_scores": {"P1": {}, "P2": {}, "P3": {}},
        }

        summary = _evidence_summary(state, plan)

        assert set(summary) == {"sources_used", "places_verified", "low_trust_filtered"}
        assert summary == {"sources_used": 2, "places_verified": 1, "low_trust_filtered": 2}
        assert all(value >= 0 for value in summary.values())

    def test_low_trust_filtered_never_goes_negative(self):
        plan = TripPlan(
            run_id=RUN_ID,
            intent=_intent(),
            days=[
                ItineraryDay(
                    day_index=0,
                    items=[
                        ItineraryItem(id="i1", type="attraction", name="A", place_id="P1"),
                        ItineraryItem(id="i2", type="attraction", name="B", place_id="P2"),
                    ],
                )
            ],
        )

        summary = _evidence_summary({"places": [], "candidate_scores": {}}, plan)

        assert summary["low_trust_filtered"] == 0

    def test_missing_state_keys_do_not_crash(self):
        summary = _evidence_summary({}, TripPlan(run_id=RUN_ID, intent=_intent()))

        assert summary == {"sources_used": 0, "places_verified": 0, "low_trust_filtered": 0}


class TestPlanPayload:
    def test_adds_evidence_from_the_store_without_mutating_the_plan(self, store):
        store.create_run(RUN_ID)
        store.save_source(RUN_ID, {"source_id": "src1", "provider": "mediacrawler"})
        store.save_evidence(
            RUN_ID,
            Evidence(
                id="e1",
                provider="mediacrawler",
                source_type="note",
                source_id="src1",
                text="龙抄手人均 40 元",
            ),
        )
        plan = _plan()
        before = plan.to_json_dict()

        payload = plan_payload(plan, store)

        assert [row["id"] for row in payload["evidence"]] == ["e1"]
        assert payload["evidence"][0]["text"] == "龙抄手人均 40 元"
        assert "evidence" not in before
        assert plan.to_json_dict() == before  # plan 对象没有被就地改写
        assert "evidence" not in TripPlan.model_fields

    def test_without_store_there_is_no_evidence_key(self):
        payload = plan_payload(_plan())

        assert "evidence" not in payload
        assert payload["run_id"] == RUN_ID

    def test_evidence_is_scoped_to_the_run(self, store):
        store.create_run(RUN_ID)
        store.create_run("other-run")
        store.save_evidence(RUN_ID, Evidence(id="e1", provider="xhs", text="成都"))
        store.save_evidence("other-run", Evidence(id="e2", provider="xhs", text="杭州"))

        payload = plan_payload(_plan(), store)

        assert [row["id"] for row in payload["evidence"]] == ["e1"]


# ==================================================
# 七、图结构
# ==================================================


class TestTravelGraph:
    NODES = (
        "parse_intent",
        "search_intercity_transport",
        "search_hotels",
        "search_social_guides",
        "extract_and_normalize_places",
        "verify_poi_and_routes",
        "score_candidates",
        "build_initial_plan",
        "check_budget",
        "check_feasibility",
        "critic_and_revise",
        "finalize",
    )

    def test_graph_registers_the_fixed_twelve_nodes(self):
        nodes = travel_graph().get_graph().nodes

        assert set(self.NODES) <= set(nodes)

    def test_graph_is_compiled_once_per_process(self):
        assert travel_graph() is travel_graph()
        assert isinstance(build_travel_graph(), type(travel_graph()))

    def test_only_the_audit_channels_use_operator_add(self):
        # workflow 用了 `from __future__ import annotations`，注解是字符串，
        # 必须先 get_type_hints 解析成真正的 Annotated 才能看 reducer；
        # 还要带 include_extras=True，否则 Annotated 的元数据会被剥掉。
        hints = get_type_hints(TravelState, include_extras=True)

        accumulating = {
            key
            for key, hint in hints.items()
            if get_origin(hint) is Annotated and operator.add in hint.__metadata__
        }

        # 累加语义只允许这几条审计通道有：多一条就意味着某个业务字段会被叠加。
        # jev_calls 与其余四条同类 —— 它也只做追加（build_initial_plan 与
        # critic_and_revise 都写它），不参与任何旅行决策。
        assert accumulating == {"decisions", "timeline", "stages", "degradations", "jev_calls"}
        for key in accumulating:
            assert get_origin(get_args(hints[key])[0]) is list, key


# ==================================================
# 八、检索词扩写：prompt 要的是数组，解析就必须按数组处理
# ==================================================


class TestQueryExpansionContract:
    def test_prompt_asks_for_a_top_level_json_array(self):
        assert "JSON 数组" in RESEARCH_QUERY_EXPANSION_PROMPT

    def test_invoke_json_returns_a_list_for_that_prompt(self):
        queries = ["成都 必去景点", "成都 避坑 不值得"]
        llm = LLM(model=FakeChatModel(queries=queries))

        result = llm.invoke_json(RESEARCH_QUERY_EXPANSION_PROMPT, "{}", tag="query_expansion")

        assert result.ok is True
        assert result.value == queries  # workflow 正是按 list 判断的


# ==================================================
# 九、全流程（注入假 hub / 假模型 / tmp_path 库）
# ==================================================


class TestExecuteTravelRunEndToEnd:
    QUERY = "10月1日从北京去成都玩3天，两个人，预算6000，喜欢美食"

    def _run(self, store, tmp_path, *, model=None, hub=None, run_id=RUN_ID):
        if model is None:
            model = FakeChatModel(
                intent={
                    "origin_city": "北京",
                    "destination_city": "成都",
                    "start_date": "2026-10-01",
                    "days": 3,
                    "travelers": 2,
                    "budget": 6000,
                }
            )
        return execute_travel_run(
            self.QUERY,
            store=store,
            hub=hub if hub is not None else make_hub(store),
            model=model,
            run_id=run_id,
            output_dir=tmp_path,
        )

    def test_writes_artifacts_and_reaches_completed(self, store, tmp_path):
        hub = make_hub(store)

        result = self._run(store, tmp_path, hub=hub)

        assert result.status == STATUS_COMPLETED
        assert result.ok is True
        assert result.run_id == RUN_ID

        target = tmp_path / RUN_ID
        for filename in ("plan.json", "plan.md", "audit_report.json"):
            assert (target / filename).is_file(), filename
            assert result.outputs[filename] == str(target / filename)

        written = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        assert written["run_id"] == RUN_ID
        assert written["intent"]["destination"] == ["成都"]
        assert (target / "plan.md").read_text(encoding="utf-8").startswith("# ")

        audit = json.loads((target / "audit_report.json").read_text(encoding="utf-8"))
        assert audit["run_id"] == RUN_ID
        assert audit["provider_calls"]
        assert audit["llm_calls"]
        assert set(audit["evidence_summary"]) == {
            "sources_used",
            "places_verified",
            "low_trust_filtered",
        }

    def test_run_row_reaches_a_terminal_status(self, store, tmp_path):
        result = self._run(store, tmp_path, hub=make_hub(store))

        assert store.get_run(RUN_ID)["status"] == STATUS_COMPLETED
        assert store.get_run(RUN_ID)["status"] == result.status
        assert store.get_plan(RUN_ID) is not None

    def test_plan_is_consistent_with_the_injected_inputs(self, store, tmp_path):
        result = self._run(store, tmp_path, hub=make_hub(store))

        assert result.plan is not None
        assert result.plan.intent.origin == "北京"
        assert result.plan.intent.destination == ["成都"]
        assert result.plan.intent.days == 3
        assert len(result.plan.days) == 3
        assert result.plan.transport is not None
        assert result.plan.transport.selected is not None
        assert result.plan.hotel is not None
        assert result.plan.hotel.selected is not None
        assert result.plan.budget.projected_total >= 0
        assert result.plan_md.startswith("# 行程说明")  # 假模型给了成文
        assert "结构化明细" in result.plan_md

    def test_what_the_workflow_did_with_the_fake_hub(self, store, tmp_path):
        """断言代码"用替身做了什么"，而不是断言"某个网络调用成功了"。"""
        hub = make_hub(store)

        result = self._run(store, tmp_path, hub=hub)

        assert hub.calls.count("search_trains") == 2  # 去程 + 回程
        assert hub.calls.count("search_flights") == 2
        assert hub.calls.count("search_hotels") == 1
        assert hub.calls.count("search_xiaohongshu") == current_config().social_query_limit
        assert "search_poi" in hub.calls
        assert "search_scenic_tickets" in hub.calls
        assert "route" in hub.calls
        # 只有替身被调用过：任何真实 Provider 都不会出现在这个列表里
        assert set(hub.calls) <= set(FakeHub.KNOWN)

        tags = {entry["tag"] for entry in result.audit["llm_calls"]}
        assert {"parse_intent", "query_expansion", "critic", "final_answer"} <= tags

    def test_the_model_intent_is_what_lands_in_the_plan(self, store, tmp_path):
        """模型解析成功时，plan 里的意图必须来自模型，而不是悄悄退化成规则兜底。

        探针用 `preferences`：规则解析器从不产出这个字段。它出现在 plan.intent 里，
        就说明"模型解析"这条分支真的跑通了。（回归背景：假模型曾按 prompt 关键词
        分派，而 INTENT_PARSE_PROMPT 里没有那个关键词，于是模型分支静默退化，
        全部断言却依然全绿。）
        """
        model = FakeChatModel(
            intent={
                "origin_city": "北京",
                "destination_city": "成都",
                "start_date": "2026-10-01",
                "days": 3,
                "travelers": 2,
                "preferences": ["美食", "拍照"],
            }
        )

        result = self._run(store, tmp_path, model=model, hub=make_hub(store))

        assert result.plan.intent.preferences == ["美食", "拍照"]  # 只有模型会给
        assert result.plan.intent.days == 3
        parse_stage = next(
            stage for stage in result.audit["stages"] if stage["id"] == "parse_intent"
        )
        assert any("模型解析" in step for step in parse_stage["steps"])

    def test_model_expanded_queries_drive_the_searches(self, store, tmp_path):
        """模型扩写出的检索词要真的被拿去搜，并且按各平台的预算截断。"""
        queries = ["成都 小众", "成都 避坑", "成都 夜宵", "成都 住宿"]
        hub = make_hub(store)

        result = self._run(
            store,
            tmp_path,
            model=FakeChatModel(
                intent={
                    "origin_city": "北京",
                    "destination_city": "成都",
                    "start_date": "2026-10-01",
                    "days": 3,
                    "travelers": 2,
                },
                queries=queries,
            ),
            hub=hub,
        )

        assert result.status == STATUS_COMPLETED
        social_kw = [kwargs["keyword"] for name, kwargs in hub.call_args if name == "search_xiaohongshu"]
        assert social_kw == queries[: current_config().social_query_limit]
        web = [kwargs["query"] for name, kwargs in hub.call_args if name == "web_search"]
        assert web == queries[: current_config().web_query_limit]
        social_stage = next(
            stage for stage in result.audit["stages"] if stage["id"] == "search_social_guides"
        )
        assert any("模型扩写" in step for step in social_stage["steps"])

    def test_the_model_is_only_consulted_at_the_five_documented_sites(self, store, tmp_path):
        """决定"做什么"的是代码；模型只在 5 个调用点被问。

        多一处就意味着流程把判断权交给了模型 —— 这条断言会把那种改动拦下来。
        """
        model = FakeChatModel(
            intent={
                "origin_city": "北京",
                "destination_city": "成都",
                "start_date": "2026-10-01",
                "days": 3,
                "travelers": 2,
            },
            places=[{"name": "宽窄巷子", "category": "attraction"}],
        )

        result = self._run(store, tmp_path, model=model, hub=make_hub(store))

        assert result.status == STATUS_COMPLETED
        assert set(model.prompts) == {
            INTENT_PARSE_PROMPT,
            RESEARCH_QUERY_EXPANSION_PROMPT,
            EXTRACT_PLACES_BATCH_PROMPT,
            CRITIC_PROMPT,
            FINAL_ANSWER_PROMPT,
        }
        # 没带 place_mentions 的网页证据确实走了模型抽取（tag 带证据 id）。
        tags = {entry["tag"] for entry in result.audit["llm_calls"]}
        assert any(tag.startswith("extract_places:") for tag in tags)
        # 每次调用（含失败）都留痕，审计能回答"模型被调了几次、让它干了什么"。
        assert all({"tag", "model", "status"} <= set(entry) for entry in result.audit["llm_calls"])

    def test_plan_md_falls_back_to_code_when_prose_is_empty(self, store, tmp_path):
        """模型成文不可用时：run 照常完成，plan.md 全部由代码生成并如实留痕。"""
        model = FakeChatModel(
            intent={
                "origin_city": "北京",
                "destination_city": "成都",
                "start_date": "2026-10-01",
                "days": 3,
                "travelers": 2,
            },
            prose="",
        )

        result = self._run(store, tmp_path, model=model, hub=make_hub(store))

        assert result.status == STATUS_COMPLETED
        assert result.plan_md.startswith("# ")  # 纯代码成文
        assert "这是模型写的行程说明" not in result.plan_md  # 没有模型散文
        # 成文失败必须写进审计的 degradations，不能静默。
        assert any("plan.md" in note for note in result.audit["degradations"])

    def test_empty_data_sources_degrade_truthfully(self, store, tmp_path):
        """数据源全空时：行程照排，但价格必须标成未知，而不是补一个"合理值"。"""
        empty = FakeHub(store=store, run_id=RUN_ID, pois=_pois(), ticket_price=None)

        result = self._run(store, tmp_path, hub=empty)

        assert result.status == STATUS_COMPLETED
        assert result.plan is not None
        assert result.plan.transport.selected is None
        assert result.plan.transport.provider_status["去程火车"] == "EMPTY"
        assert result.plan.hotel.selected is None
        assert result.plan.budget.breakdown[BUDGET_CATEGORY_TRANSPORT] == 0.0
        assert result.plan.budget.price_notes  # 未取到价格必须写进备注
        assert result.degradations  # 降级原因不得静默
        # 光有 audit.degradations 不够：用户看的是 plan.warnings（前端"时间与可行性检查"），
        # "没有去程""没有住宿"必须在那里也各有一条，否则行程看起来是完整的。
        codes = {issue.code for issue in result.plan.warnings}
        assert {"OUTBOUND_UNAVAILABLE", "INBOUND_UNAVAILABLE", "HOTEL_UNAVAILABLE"} <= codes
        statuses = {entry["status"] for entry in result.audit["provider_calls"]}
        assert "EMPTY" in statuses
        assert "OK" not in statuses or empty.ticket_price is None

    def test_repeated_run_id_overwrites_instead_of_duplicating(self, store, tmp_path):
        self._run(store, tmp_path, hub=make_hub(store))
        second = self._run(store, tmp_path, hub=make_hub(store))

        assert second.status == STATUS_COMPLETED
        stored = store.get_plan(RUN_ID)
        assert len(stored["items"]) == len(
            [item for day in second.plan.days for item in day.items]
        )

    def test_an_off_date_outbound_reaches_plan_warnings(self, store, tmp_path):
        """完整链路：比选发现日期不符 → state.warnings → plan.warnings。

        单测只证明 `_off_date_issues` 会造出这条 issue；这里证明它真的走完
        check_feasibility → plan_payload → plan.warnings，而不是在中间被丢掉。
        （回归背景：12306 查 10-02 返回 09-30/10-01 的车次，比选只看价格就选中了它。）
        """
        hub = make_hub(store)
        # 去程只剩"早一天发车"这一个候选（航班清空，否则它与日期无关的算法会赢）。
        hub.trains[("北京", "成都")] = [
            _train(
                "D203",
                price=780.0,
                duration=450,
                origin_station="北京西",
                destination_station="成都东",
                departure_at=datetime(2026, 9, 30, 8, 0),
                arrival_at=datetime(2026, 9, 30, 16, 0),
            )
        ]
        hub.flights[("北京", "成都")] = []

        result = self._run(store, tmp_path, hub=hub)

        assert result.plan is not None
        assert result.plan.transport.selected is not None
        assert result.plan.transport.selected.train_no == "D203"
        assert "OUTBOUND_OFF_DATE" in {issue.code for issue in result.plan.warnings}
        assert "相差" in result.plan.transport.selection_reason
        # 日期没有被改写：车次还是 09-30 那天开的
        assert result.plan.transport.selected.departure_at.date() == date(2026, 9, 30)


class TestExecuteTravelRunNeedsClarification:
    def test_no_destination_stops_before_any_provider_call(self, store, tmp_path):
        hub = FakeHub(store=store, run_id=RUN_ID)

        result = execute_travel_run(
            "帮我规划一下",
            store=store,
            hub=hub,
            model=UnparseableModel(),
            run_id=RUN_ID,
            output_dir=tmp_path,
        )

        assert result.status == STATUS_NEEDS_CLARIFICATION
        assert result.ok is False
        assert result.plan is None
        assert result.plan_md == ""
        assert result.clarification  # 有明确的追问文案
        assert "目的地" in result.clarification
        assert hub.calls == []  # 一次 Provider 调用都不许发生

    def test_store_marks_the_run_as_needing_clarification(self, store, tmp_path):
        hub = FakeHub(store=store, run_id=RUN_ID)

        execute_travel_run(
            "帮我规划一下",
            store=store,
            hub=hub,
            model=UnparseableModel(),
            run_id=RUN_ID,
            output_dir=tmp_path,
        )

        assert store.get_run(RUN_ID)["status"] == STATUS_NEEDS_CLARIFICATION
        assert store.get_plan(RUN_ID) is None
        assert not (tmp_path / RUN_ID).exists()  # 没有产出就不该有产物目录

    def test_degradation_reason_is_reported(self, store, tmp_path):
        result = execute_travel_run(
            "帮我规划一下",
            store=store,
            hub=FakeHub(store=store, run_id=RUN_ID),
            model=UnparseableModel(),
            run_id=RUN_ID,
            output_dir=tmp_path,
        )

        assert any("规则" in note for note in result.degradations)


class TestExecuteTravelRunFailure:
    def test_unexpected_exception_marks_the_run_failed(self, store, tmp_path, monkeypatch):
        """任何未预期异常都必须落一个 failed run 并如实报错，而不是抛给调用方。"""

        def boom(state):
            raise RuntimeError("模拟图执行失败")

        monkeypatch.setattr("app.workflow.travel_graph", lambda: SimpleNamespace(invoke=boom))

        result = execute_travel_run(
            "从北京去成都玩3天",
            store=store,
            hub=FakeHub(store=store, run_id=RUN_ID),
            model=FakeChatModel(intent={"destination_city": "成都"}),
            run_id=RUN_ID,
            output_dir=tmp_path,
        )

        assert result.status == STATUS_FAILED
        assert result.ok is False
        assert result.plan is None
        assert "RuntimeError" in (result.error or "")
        assert store.get_run(RUN_ID)["status"] == STATUS_FAILED

    def test_missing_status_is_not_reported_as_success(self, store, tmp_path, monkeypatch):
        """图正常结束但没写 status 时，RunResult 不能自称成功。"""
        monkeypatch.setattr(
            "app.workflow.travel_graph",
            lambda: SimpleNamespace(invoke=lambda state: {"status": ""}),
        )

        result = execute_travel_run(
            "从北京去成都玩3天",
            store=store,
            hub=FakeHub(store=store, run_id=RUN_ID),
            model=FakeChatModel(intent={"destination_city": "成都"}),
            run_id=RUN_ID,
            output_dir=tmp_path,
        )

        assert result.status == STATUS_FAILED
        assert result.ok is False


class TestRunResult:
    def test_ok_only_for_completed(self):
        from app.workflow import RunResult

        assert RunResult(run_id="r", status=STATUS_COMPLETED).ok is True
        assert RunResult(run_id="r", status=STATUS_FAILED).ok is False
        assert RunResult(run_id="r", status=STATUS_NEEDS_CLARIFICATION).ok is False

    def test_clarification_is_the_error_text(self):
        from app.workflow import RunResult

        assert (
            RunResult(run_id="r", status=STATUS_NEEDS_CLARIFICATION, error="请补充目的地").clarification
            == "请补充目的地"
        )
        assert RunResult(run_id="r", status=STATUS_COMPLETED).clarification == ""
