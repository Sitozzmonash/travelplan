"""app/models.py 的单元测试（PRD §16）。

重点覆盖三件事：
1. **容错**：LLM 的输出脏得离谱时也必须构造出对象而不是抛异常（PRD §16.1）；
2. **契约**：Provider 信封 → 模型的映射必须和工具真实返回的字段名对齐，
   且拿不到的值必须是 None，不能出现"看起来合理的假值"；
3. **序列化**：`model_dump(mode="json")` 必须是纯 JSON 可编码的（要落库、要进 plan.json）。

全部离线，不发任何网络请求。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    BudgetSummary,
    Decision,
    DecisionStatus,
    Evidence,
    FeasibilityIssue,
    FlightOption,
    HotelOption,
    ItineraryDay,
    ItineraryItem,
    Place,
    ProviderProvenance,
    RouteOption,
    TrainOption,
    TravelLeg,
    TripIntent,
    TripPlan,
    basic_name_key,
    envelope_items,
    parse_date_text,
    parse_envelope,
    utcnow,
)

# 一份真实形状的 tuniu flight item（字段名对齐 tools/flight.py::_normalize_item）。
FLIGHT_ITEM = {
    "flight_no": "CA4101",
    "airline": "中国国航",
    "aircraft": "A321",
    "departure_airport": "首都机场T3",
    "arrival_airport": "双流机场T2",
    "departure_time": "2026-10-01 08:30",
    "arrival_time": "2026-10-01 12:10",
    "duration_minutes": "220",
    "is_direct": True,
    "price": 880.0,
    "currency": "CNY",
    "price_breakdown": {"base": 800.0, "tax": 80.0},
    "remaining_seats": 5,
}

# 真实形状的 route 信封：路径字段平铺在顶层（tools/route.py）。
ROUTE_ENVELOPE = {
    "status": "OK",
    "provider": "amap",
    "fetched_at": "2026-10-01T08:00:00+00:00",
    "query": {"origin": "104.05,30.64", "destination": "104.06,30.66"},
    "count": 1,
    "items": [{"mode": "transit", "distance_meters": 4200, "duration_seconds": 901, "estimated_cost": 4.0}],
    "error": None,
    "distance_meters": 4200,
    "duration_seconds": 901,
    "estimated_cost": 4.0,
    "mode": "transit",
}


class TestTripIntentFromLlm:
    """LLM 输出 → TripIntent 的降级规则（PRD §16.1：任何字段都可能缺失或类型错误）。"""

    def test_none_gives_empty_intent_instead_of_crashing(self):
        intent = TripIntent.from_llm(None)

        assert intent.destination == []
        assert intent.start_date is None
        assert intent.days == 1
        assert intent.travelers == 1
        assert intent.budget_total is None
        assert intent.pace == "balanced"

    def test_null_fields_are_not_filled_with_guesses(self):
        """用户没说预算/日期时必须是 None，不能填一个"看起来合理"的默认值。"""
        intent = TripIntent.from_llm(
            {
                "destination": None,
                "start_date": None,
                "end_date": None,
                "budget": None,
                "travelers": None,
                "preferences": None,
                "pace": None,
            }
        )

        assert intent.budget_total is None
        assert intent.start_date is None
        assert intent.end_date is None
        assert intent.preferences == []
        assert intent.travelers == 1
        assert intent.pace == "balanced"

    def test_dirty_json_string_still_parses(self):
        intent = TripIntent.from_llm(
            '{"destination":"成都","days":"五天","travelers":"两个人","budget":"人均3000"}'
        )

        assert intent.destination == ["成都"]  # 单个字符串被包成 list
        assert intent.days == 5  # 中文数字
        assert intent.travelers == 2
        assert intent.budget_total == 3000.0

    def test_invalid_json_degrades_to_empty_intent(self):
        """脏 JSON 不允许让一次 run 直接失败：退化成空意图交给上层追问。"""
        intent = TripIntent.from_llm("{这不是 JSON")

        assert intent.destination == []
        assert intent.days == 1

    def test_illegal_pace_falls_back_to_balanced(self):
        assert TripIntent.from_llm({"pace": "悠闲"}).pace == "balanced"
        assert TripIntent.from_llm({"pace": "PACKED"}).pace == "packed"

    def test_days_derived_from_date_range(self):
        intent = TripIntent.from_llm(
            {"destination": ["成都"], "start_date": "2026-10-01", "end_date": "2026/10/04"}
        )

        assert intent.start_date == date(2026, 10, 1)
        assert intent.end_date == date(2026, 10, 4)
        assert intent.days == 4

    def test_unparsable_date_becomes_none(self):
        intent = TripIntent.from_llm({"start_date": "国庆前后", "days": None})

        assert intent.start_date is None
        assert intent.days == 1

    def test_days_and_travelers_are_clamped(self):
        assert TripIntent.from_llm({"days": 999}).days == 60
        assert TripIntent.from_llm({"days": 0}).days == 1
        assert TripIntent.from_llm({"travelers": -3}).travelers == 1


class TestTrippIntentDerivedFields:
    def test_nights_and_date_range_text(self):
        intent = TripIntent(
            destination=["成都"],
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 3),
            days=3,
        )

        assert intent.nights == 2
        assert "2026-10-01" in intent.date_range
        assert "3 天 2 晚" in intent.date_range

    def test_one_day_trip_has_no_nights(self):
        assert TripIntent(days=1).nights == 0

    def test_missing_dates_are_reported_as_undecided(self):
        assert "未定" in TripIntent(days=2).date_range


class TestProvenance:
    def test_naive_datetime_is_treated_as_utc(self):
        """审计里不允许出现"不知道时区"的时间。"""
        provenance = ProviderProvenance(fetched_at=datetime(2026, 1, 1, 12, 0))

        assert provenance.fetched_at.tzinfo is not None
        assert provenance.fetched_at.utcoffset().total_seconds() == 0

    def test_blank_strings_become_none(self):
        provenance = ProviderProvenance(source_url="", source_id="  ")

        assert provenance.source_url is None
        assert provenance.source_id is None

    def test_fetched_at_defaults_to_now(self):
        provenance = ProviderProvenance()

        assert abs((provenance.fetched_at - utcnow()).total_seconds()) < 60


class TestTransportFromItem:
    def test_flight_item_mapping(self):
        flight = FlightOption.from_item(FLIGHT_ITEM, provider="tuniu")

        assert flight.flight_no == "CA4101"
        assert flight.departure_airport == "首都机场T3"
        assert flight.duration_minutes == 220
        assert flight.price == 880.0
        assert flight.is_direct is True
        assert flight.remaining_seats == 5
        assert flight.fetched_at.tzinfo is not None
        assert "880" in flight.label

    def test_flight_without_price_says_price_unknown(self):
        """价格拿不到就写"价格未知"，不允许编一个数。"""
        flight = FlightOption.from_item({"flight_no": "CA4101", "price": None})

        assert flight.price is None
        assert "价格未知" in flight.label

    def test_train_item_accepts_seats_and_price_range(self):
        train = TrainOption.from_item(
            {
                "train_no": "G89",
                "origin_station": "北京西",
                "destination_station": "成都东",
                "departure_time": "2026-10-01 08:00",
                "arrival_time": "2026-10-01 21:00",
                "duration_minutes": "780",
                "seats_available": {"二等座": 12},
                "prices": {"二等座": 780.5, "一等座": 1250.0},
                "train_type": "高铁",
            }
        )

        assert train.seats == {"二等座": 12}  # seats_available → seats
        assert train.price_range["min"] == 780.5  # prices → price_range
        assert train.price_range["max"] == 1250.0
        assert train.duration_minutes == 780

    def test_transport_union_is_decidable_by_required_field(self):
        """FlightOption | TrainOption 靠必填字段区分，不需要 discriminator。"""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(FlightOption | TrainOption)

        assert isinstance(adapter.validate_python(FLIGHT_ITEM), FlightOption)
        assert isinstance(adapter.validate_python({"train_no": "G89"}), TrainOption)


class TestHotelFromItem:
    def test_total_price_is_unit_price_times_nights_times_rooms(self):
        """3 人 → 2 间（按 2 人 1 间），2 晚，¥420/晚 ⇒ 1680。这是真实单价做的乘法，不是编价。"""
        hotel = HotelOption.from_item(
            {"hotel_id": "h1", "name": "某酒店", "price_per_night": 420.0, "price_unit": "元起/晚"},
            check_in=date(2026, 10, 1),
            check_out=date(2026, 10, 3),
            travelers=3,
        )

        assert hotel.total_price == 1680.0
        assert hotel.nightly == 420.0

    def test_price_note_keeps_the_starting_price_caveat(self):
        """途牛只给"起价"，必须保留这个免责说明，不能当成确定价。"""
        hotel = HotelOption.from_item({"name": "某酒店", "price_per_night": 420.0})

        assert hotel.price_note is not None
        assert "起价" in hotel.price_note

    def test_missing_price_stays_none(self):
        hotel = HotelOption.from_item({"name": "某酒店"})

        assert hotel.price_per_night is None
        assert hotel.total_price is None
        assert hotel.price_note is None


class TestPlace:
    def test_from_poi_maps_longitude_latitude(self):
        place = Place.from_poi(
            {
                "poi_id": "B001",
                "name": "宽窄巷子",
                "longitude": "104.0555",
                "latitude": "30.6691",
                "type": "风景名胜",
                "city": "成都",
                "opening_hours": "09:00-22:00",
            }
        )

        assert place.place_id == "B001"
        assert place.lng == 104.0555
        assert place.lat == 30.6691
        assert place.coords == (30.6691, 104.0555)
        assert place.amap_verified is True  # 有 POI ID 才算高德验证过

    def test_place_without_poi_id_is_not_amap_verified(self):
        place = Place.from_poi({"name": "某个帖子提到的店"})

        assert place.amap_verified is False
        assert place.place_id == "某个帖子提到的店"
        assert place.coords is None

    def test_alias_key_normalizes_variants_to_one_key(self):
        """PRD §20 的三种写法必须归到同一个键。"""
        variants = ["宽窄巷子", "宽窄巷子景区", "成都宽窄巷子"]
        keys = {Place(place_id=f"p{i}", name=name, city="成都").alias_key for i, name in enumerate(variants)}

        assert len(keys) == 1


class TestEvidence:
    def test_from_content_maps_metrics_and_dishes(self):
        evidence = Evidence.from_content(
            {
                "title": "成都三天",
                "desc": "点了龙抄手，人均40元",
                "nickname": "小明",
                "create_time": "2026-03-01 10:00",
                "metrics": {"likes": 1200, "comments": 30},
                "specific_dishes": ["龙抄手"],
            },
            evidence_id="e1",
            source_type="note",
            provider="mediacrawler",
            source_url="https://example.com/1",
        )

        assert evidence.id == "e1"
        assert evidence.author == "小明"
        assert evidence.raw_metrics["likes"] == 1200
        assert evidence.specific_dishes == ["龙抄手"]
        assert evidence.published_at is not None


class TestRouteOption:
    def test_ok_envelope_is_verified(self):
        route = RouteOption.from_envelope(json.dumps(ROUTE_ENVELOPE, ensure_ascii=False))

        assert route is not None
        assert route.distance_meters == 4200
        assert route.duration_seconds == 901
        assert route.duration_minutes == 16  # 901 秒向上取整
        assert route.verified is True
        assert route.fetched_at.tzinfo is not None

    def test_failed_envelope_returns_none(self):
        """PRD §32：路线拿不到就返回 None，绝不允许猜一条。"""
        failed = {**ROUTE_ENVELOPE, "status": "UNAVAILABLE", "error": "INVALID_PARAMS"}

        assert RouteOption.from_envelope(json.dumps(failed)) is None

    def test_garbage_input_returns_none(self):
        assert RouteOption.from_envelope("not json") is None
        assert RouteOption.from_envelope(None) is None
        assert RouteOption.from_envelope({"status": "OK"}) is None

    def test_estimated_cost_comes_from_provider_not_guessed(self):
        """route 工具明确"取不到就是 null"，模型也必须保持 null。"""
        payload = {**ROUTE_ENVELOPE, "estimated_cost": None}
        route = RouteOption.from_envelope(json.dumps(payload))

        assert route is not None
        assert route.estimated_cost is None


class TestItineraryItem:
    def test_clock_helpers(self):
        item = ItineraryItem(id="i1", name="宽窄巷子", start_time="09:05", end_time="11:30")

        assert item.start_minutes == 545
        assert item.end_minutes == 690

    def test_missing_clock_is_none(self):
        item = ItineraryItem(id="i1", name="机动")

        assert item.start_minutes is None
        assert item.end_minutes is None

    def test_price_type_is_constrained(self):
        """价格类型只允许 realtime / estimated / unknown，写错必须在构造期就炸。"""
        with pytest.raises(ValidationError):
            ItineraryItem(id="i1", name="x", price_type="cheap")

    def test_coords_none_when_incomplete(self):
        assert ItineraryItem(id="i1", name="x", lat=30.6).coords is None
        assert ItineraryItem(id="i1", name="x", lat=30.6, lng=104.0).coords == (30.6, 104.0)


class TestTravelLeg:
    def test_from_verified_route_keeps_duration_and_flag(self):
        route = RouteOption(
            mode="driving",
            distance_meters=21000,
            duration_seconds=4200,
            provider="amap",
            fetched_at=utcnow(),
        )
        leg = TravelLeg.from_route(route, from_place_id="a", to_place_id="b", mode="driving")

        assert leg.verified is True
        assert leg.estimated is False
        assert leg.duration_seconds == 4200
        assert leg.distance_meters == 21000

    def test_from_missing_route_falls_back_to_estimate_and_is_marked(self):
        leg = TravelLeg.from_route(None, from_place_id="a", to_place_id="b", mode="transit", fallback_minutes=15)

        assert leg.verified is False
        assert leg.estimated is True
        assert leg.provider is None
        assert leg.duration_seconds == 15 * 60


class TestBudgetSummary:
    def test_over_by_is_zero_when_remaining_unknown(self):
        """没给预算时 remaining 是 None，over_by 取 0 而不是瞎报一个超支额。"""
        summary = BudgetSummary(budget_total=None, projected_total=1000.0)

        assert summary.remaining is None
        assert summary.over_by == 0.0

    def test_over_by_reports_positive_shortfall(self):
        summary = BudgetSummary(
            budget_total=5000.0,
            known_real_cost=4600.0,
            estimated_cost=1000.0,
            projected_total=5600.0,
            remaining=-600.0,
            status="over_budget",
        )

        assert summary.over_by == 600.0

    def test_over_by_is_zero_when_within(self):
        summary = BudgetSummary(budget_total=5000.0, projected_total=4200.0, status="within_budget")

        assert summary.over_by == 0.0

    def test_real_and_estimated_money_are_separate_fields(self):
        """PRD §23：不允许把估算写成实时价格，因此两者必须是不同字段。"""
        summary = BudgetSummary(
            budget_total=6000.0,
            known_real_cost=4200.0,
            estimated_cost=900.0,
            projected_total=5100.0,
            remaining=900.0,
            status="within_budget",
            breakdown={"交通": 4200.0, "餐饮估算": 900.0},
            breakdown_price_type={"交通": "realtime", "餐饮估算": "estimated"},
        )

        assert summary.known_real_cost + summary.estimated_cost == summary.projected_total
        assert summary.breakdown_price_type["餐饮估算"] == "estimated"


class TestTripPlanSerialization:
    def _plan(self) -> TripPlan:
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
            travel_from_previous=TravelLeg.from_route(None, mode="transit", fallback_minutes=10),
        )
        day = ItineraryDay(day_index=0, date=date(2026, 10, 1), area="青羊区", items=[item])
        return TripPlan(
            run_id="run1",
            query="成都三日",
            intent=TripIntent(destination=["成都"], days=1),
            days=[day],
            budget=BudgetSummary(budget_total=6000.0, projected_total=1000.0, status="within_budget"),
            warnings=[FeasibilityIssue(day_index=0, item_id="i1", code="ROUTE_UNVERIFIED", reason="未核实")],
            generated_at=utcnow(),
            decisions=[
                Decision(entity_id="P1", status=DecisionStatus.KEEP, reason_codes=["representative"])
            ],
        )

    def test_json_dump_is_encodable(self):
        plan = self._plan()
        payload = json.loads(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False))

        assert payload["days"][0]["date"] == "2026-10-01"
        assert payload["days"][0]["items"][0]["price_type"] == "realtime"
        assert payload["decisions"][0]["status"] == "KEEP"

    def test_to_json_dict_round_trips_through_models(self):
        """plan.json 写盘再读回来必须还能构造出 TripPlan（审计要能复盘）。"""
        plan = self._plan()
        payload = plan.to_json_dict()
        restored = TripPlan.model_validate(payload)

        assert restored.run_id == plan.run_id
        assert restored.day_count == 1
        assert restored.days[0].items[0].name == "宽窄巷子"
        assert restored.days[0].items[0].start_minutes == 540
        assert restored.warnings[0].code == "ROUTE_UNVERIFIED"

    def test_day_count_follows_days(self):
        assert self._plan().day_count == 1
        assert TripPlan(run_id="r", intent=TripIntent(), generated_at=utcnow()).day_count == 0


class TestDecisionStatus:
    def test_expected_status_values(self):
        assert {status.value for status in DecisionStatus} == {
            "KEEP",
            "REJECT",
            "SELECT",
            "PASS",
            "USED",
            "NOT_USED",
            "REVISION",
        }

    def test_decision_dumps_status_as_plain_string(self):
        decision = Decision(entity_id="P1", status=DecisionStatus.SELECT, reason_codes=["cheapest"])

        dumped = decision.model_dump(mode="json")

        assert dumped["status"] == "SELECT"
        assert isinstance(dumped["timestamp"], str)


class TestEnvelopeHelpers:
    def test_parse_envelope_accepts_string_and_dict(self):
        assert parse_envelope('{"status":"OK"}') == {"status": "OK"}
        assert parse_envelope({"status": "OK"}) == {"status": "OK"}

    def test_parse_envelope_on_garbage_returns_empty_dict(self):
        assert parse_envelope("not json") == {}
        assert parse_envelope(None) == {}

    def test_envelope_items_reads_items_list(self):
        assert envelope_items({"items": [{"a": 1}]}) == [{"a": 1}]
        assert envelope_items({"items": None}) == []
        assert envelope_items("{}") == []


class TestHelperFunctions:
    def test_basic_name_key_strips_punctuation_and_case(self):
        assert basic_name_key("宽窄巷子(景区)  A区") == basic_name_key("宽窄巷子（景区）a区")
        assert basic_name_key("") == ""

    def test_parse_date_text_rolls_year_forward_for_past_dates(self):
        """只说"10月1日"时，若今年那一天已过，就取明年，避免排出过去的行程。"""
        assert parse_date_text("10月1日", date(2026, 11, 20)) == date(2027, 10, 1)
        assert parse_date_text("10月1日", date(2026, 2, 20)) == date(2026, 10, 1)

    def test_parse_date_text_rejects_non_dates(self):
        assert parse_date_text("下个月第一个周末", date(2026, 1, 1)) is None
        assert parse_date_text(None, date(2026, 1, 1)) is None

    def test_utcnow_is_timezone_aware(self):
        now = utcnow()

        assert now.tzinfo == timezone.utc
