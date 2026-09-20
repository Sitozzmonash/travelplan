"""app/planner.py 的单元测试（PRD §18 - §24、§32、§34.1）。

planner 是"Evidence First, LLM Second"真正落地的地方：它是纯函数集合，不含任何 LLM /
网络 / 随机数，所以本套测试可以完全离线、确定性地跑。

覆盖范围（PRD §34.1 的最低清单，这里只多不少）：
* POI 归一化与去重（Place normalize / dedupe）
* Trust Score
* Ad Risk
* 预算引擎 Budget
* 时间可行性 Feasibility
* 时间冲突修订（revise_day / apply_feasibility_pass）
* 地理聚类与候选排序、初始计划生成、路线查找、Critic

测试数据的说明
------------
**本文件里所有航班 / 酒店 / 路线 / POI 数据都是合成的测试输入，不是任何 Provider 的
真实返回。** 只有需要验证"信封解析"的用例才构造信封形状的 dict，并在用例里标明它来自
哪个工具的真实字段形状；其余一律直接用 pydantic 模型构造，避免把"我编的数据"当成
"查到的数据"。日期固定用 2026-10-01 附近的显式日期，这样测试不会随系统时间腐烂。

只读 app/*.py：如果测试行为与预期不符，先怀疑预期；确认是代码与 PRD 相悖的地方，
用注释标出"与 PRD 偏差"并锁住**实际行为**（见 test_build_initial_plan_requires_district...）。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta

import pytest

from app.models import (
    BudgetSummary,
    DecisionStatus,
    Evidence,
    FeasibilityIssue,
    FlightOption,
    HotelOption,
    ItineraryDay,
    ItineraryItem,
    Place,
    RouteOption,
    TrainOption,
    TransportPlan,
    TravelLeg,
    TripIntent,
    TripPlan,
)
from app.planner import (
    AD_RISK_HIGH,
    AIRPORT_BAGGAGE_EXIT_MINUTES,
    AIRPORT_CITY_TRANSFER_MINUTES,
    AIRPORT_CHECKIN_BUFFER_MINUTES,
    DAY_END_MINUTES,
    DAY_START_MINUTES,
    DEFAULT_CLUSTER_KM,
    HOTEL_CHECKIN_MINUTES,
    HOTEL_STANDARD_CHECKIN_MINUTES,
    LATE_ARRIVAL_WINDOW_MINUTES,
    LUNCH_WINDOW,
    DINNER_WINDOW,
    MAX_DAY_MINUTES,
    MAX_ITEMS_PER_DAY,
    MEAL_ESTIMATE_PER_DAY,
    TRAIN_EXIT_BUFFER_MINUTES,
    TRAIN_STATION_BUFFER_MINUTES,
    TRAIN_STATION_TRANSFER_MINUTES,
    TRAVELERS_PER_ROOM,
    TRUST_WEIGHT_DETAIL,
    TRUST_WEIGHT_LOCAL,
    TRUST_WEIGHT_OFFICIAL,
    TRUST_WEIGHT_PERSISTENCE,
    TRUST_WEIGHT_SCALE,
    TRUST_WEIGHT_SOURCES,
    ad_risk,
    apply_feasibility_pass,
    arrival_beyond_trip,
    arrival_day_index,
    arrival_day_start_minutes,
    arrival_ready_minutes,
    boarding_buffer_minutes,
    build_budget,
    build_initial_plan,
    check_feasibility,
    city_tier,
    cluster_places,
    critique,
    dedupe_places,
    departure_deadline_minutes,
    detect_backtracking,
    estimate_route_minutes,
    haversine_meters,
    lookup_route,
    minutes_to_clock,
    name_similarity,
    normalize_place_name,
    order_by_proximity,
    parse_clock,
    parse_opening_hours,
    preference_hits,
    revise_day,
    route_efficiency_penalty,
    score_candidate,
    transfer_buffer_minutes,
    trust_score,
)

# 项目参照日期附近的一个固定日期：所有 fixture 都用它，避免测试随系统时间腐烂。
BASE_DATE = date(2026, 10, 1)
# 成都宽窄巷子与天府广场附近的合成坐标（真实量级的经纬度，不是从任何 Provider 抄的）。
CD_LAT, CD_LNG = 30.6691, 104.0555


# ==================================================
# 零、构造 fixture 的本地小工具
# ==================================================
# 全部是**合成测试输入**：直接构造 pydantic 模型，不假装来自某个 Provider。


def make_place(place_id: str = "P1", name: str = "宽窄巷子", **overrides) -> Place:
    """一个合成 Place；默认带成都坐标与行政区的完整字段。"""
    data: dict = {"place_id": place_id, "name": name, "city": "成都", "district": "青羊区"}
    data.update(overrides)
    return Place(**data)


def make_place_at(place_id: str, name: str, lat: float, lng: float, **overrides) -> Place:
    """带显式坐标的合成 Place（聚类 / 路线测试用）。"""
    return make_place(place_id, name, lat=lat, lng=lng, **overrides)


def make_evidence(eid: str = "e1", text: str = "", **overrides) -> Evidence:
    """一个合成 Evidence（provider/source_type 可覆盖，用于验证多来源分量）。"""
    data: dict = {
        "id": eid,
        "provider": "xiaohongshu",
        "source_type": "social",
        "text": text,
        "source_url": f"https://example.test/{eid}",
    }
    data.update(overrides)
    return Evidence(**data)


def make_flight(**overrides) -> FlightOption:
    """合成航班：默认 2026-10-01 08:30 起飞、12:10 落地（PRD §4.3 的场景时刻）。"""
    data: dict = {
        "flight_no": "CA4101",
        "provider": "tuniu",
        "price": 880.0,
        "departure_at": datetime(2026, 10, 1, 8, 30),
        "arrival_at": datetime(2026, 10, 1, 12, 10),
    }
    data.update(overrides)
    return FlightOption(**data)


def make_train(**overrides) -> TrainOption:
    """合成高铁。"""
    data: dict = {
        "train_no": "G89",
        "provider": "12306",
        "departure_at": datetime(2026, 10, 1, 8, 0),
        "arrival_at": datetime(2026, 10, 1, 12, 0),
        "price_range": {"min": 780.5, "max": 1250.0},
    }
    data.update(overrides)
    return TrainOption(**data)


def make_hotel(**overrides) -> HotelOption:
    """合成酒店；默认只有"起价"，用来验证 total 由每晚价乘出来。"""
    data: dict = {
        "hotel_id": "h1",
        "name": "某酒店",
        "provider": "tuniu",
        "price_per_night": 420.0,
        "lat": 30.66,
        "lng": 104.06,
    }
    data.update(overrides)
    return HotelOption(**data)


def make_item(item_id: str = "i1", **overrides) -> ItineraryItem:
    """合成 item；默认不指定起止时间，由可行性引擎负责推算。"""
    data: dict = {"id": item_id, "name": "宽窄巷子"}
    data.update(overrides)
    return ItineraryItem(**data)


def make_day(items: list[ItineraryItem] | None = None, day_index: int = 0, **overrides) -> ItineraryDay:
    """合成一天：日期由 day_index 从 BASE_DATE 推出。"""
    data: dict = {
        "day_index": day_index,
        "date": BASE_DATE + timedelta(days=day_index),
        "items": list(items or []),
    }
    data.update(overrides)
    return ItineraryDay(**data)


def make_plan(**overrides) -> TripPlan:
    """合成 TripPlan，供 Critic 测试使用。"""
    data: dict = {
        "run_id": "run-test",
        "query": "成都两天",
        "intent": TripIntent(destination=["成都"], days=2, travelers=2),
        "days": [],
        "budget": BudgetSummary(),
    }
    data.update(overrides)
    return TripPlan(**data)


def codes(issues) -> set[str]:
    """问题列表 → code 集合，让断言读起来像"有没有这类问题"。"""
    return {issue.code for issue in issues}


def decision_codes(decisions) -> set[str]:
    """决策列表 → 第一顺位 reason_code 集合。"""
    return {d.reason_codes[0] for d in decisions if d.reason_codes}


# ==================================================
# 一、时钟工具（parse_clock / minutes_to_clock）
# ==================================================


class TestParseClock:
    """时间解析必须"宁缺勿猜"：解析不出来就是 None，绝不默认成 09:00。"""

    def test_common_string_formats(self):
        assert parse_clock("14:00") == 840
        assert parse_clock("09:05") == 545
        assert parse_clock("14:00:00") == 840
        assert parse_clock("2026-10-01T14:00:00") == 840

    def test_fullwidth_and_loose_separators(self):
        """全角冒号 / 多余空格在很多来源里都会出现，必须等价解析。"""
        assert parse_clock("14：00") == 840
        assert parse_clock("14 : 00") == 840

    def test_numeric_minutes_pass_through(self):
        assert parse_clock(840) == 840

    def test_plain_digit_string_is_not_a_clock(self):
        # 与 docstring 里"纯数字"的说法有偏差：只有 int/float 才按分钟解释，
        # 字符串 "840" 没有冒号，返回 None（宁可跳过也不猜这是 8:40 还是 840 分钟）。
        assert parse_clock("840") is None

    def test_midnight_and_boundary_values(self):
        assert parse_clock("00:00") == 0
        assert parse_clock("24:00") == 1440

    def test_invalid_values_return_none(self):
        """25:00 / 12:60 / 乱码 / None / bool 都必须返回 None。"""
        assert parse_clock("25:00") is None
        assert parse_clock("12:60") is None
        assert parse_clock("看心情") is None
        assert parse_clock(None) is None
        assert parse_clock(True) is None

    def test_date_object_is_not_a_clock(self):
        assert parse_clock(BASE_DATE) is None


class TestMinutesToClock:
    def test_round_trip_with_parse_clock(self):
        for value in (0, 1, 540, 840, 1439):
            assert parse_clock(minutes_to_clock(value)) == value

    def test_midnight(self):
        assert minutes_to_clock(0) == "00:00"

    def test_past_midnight_is_labelled(self):
        """跨过午夜必须标"次日"，否则会输出 25:30 这种不可读时间。"""
        assert minutes_to_clock(1440) == "00:00(次日)"
        assert minutes_to_clock(25 * 60 + 30) == "01:30(次日)"

    def test_none_and_negative_return_none(self):
        assert minutes_to_clock(None) is None
        assert minutes_to_clock(-5) is None


# ==================================================
# 二、距离与路线（haversine / estimate / lookup / penalty）
# ==================================================


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_meters((CD_LAT, CD_LNG), (CD_LAT, CD_LNG)) == 0.0

    def test_short_distance_matches_known_value(self):
        """约 65m 的两点：这是球面距离的真实量级，不是随便给的常数。"""
        distance = haversine_meters((30.6691, 104.0555), (30.6695, 104.0560))

        assert distance == pytest.approx(65.3, abs=0.5)

    def test_missing_coords_return_none(self):
        """缺坐标返回 None：不允许用 0 米假装"就在旁边"。"""
        assert haversine_meters(None, (30.0, 104.0)) is None
        assert haversine_meters((30.0, 104.0), None) is None


class TestEstimateRouteMinutes:
    def test_uses_straight_line_times_detour_over_speed(self):
        minutes, meta = estimate_route_minutes((30.669, 104.055), (30.657, 104.065))

        assert minutes == 7
        assert meta["method"] == "haversine*detour"
        assert meta["detour_factor"] == pytest.approx(1.35)
        assert meta["speed_kmh"] == 18.0

    def test_mode_changes_speed(self):
        transit, _ = estimate_route_minutes((30.669, 104.055), (30.657, 104.065), "transit")
        walking, walking_meta = estimate_route_minutes((30.669, 104.055), (30.657, 104.065), "walking")

        assert walking > transit  # 走路一定比公交慢
        assert walking_meta["speed_kmh"] == 4.5

    def test_never_returns_less_than_three_minutes(self):
        """再近的两点也要留 3 分钟：出楼、找路、等电梯都真实存在。"""
        minutes, _ = estimate_route_minutes((30.6691, 104.0555), (30.66901, 104.05501))

        assert minutes == 3

    def test_without_coords_is_explicitly_unavailable(self):
        minutes, meta = estimate_route_minutes(None, None)

        assert minutes is None
        assert meta["method"] == "unavailable"


class TestLookupRoute:
    """routes 的键形式在 workflow 里不统一，lookup_route 必须一次兜完。"""

    def test_key_shapes_are_all_supported(self):
        route = RouteOption(distance_meters=4200, duration_seconds=901, provider="amap")

        assert lookup_route({("A", "B"): route}, "A", "B").duration_minutes == 16
        assert lookup_route({"A->B": route}, "A", "B").duration_minutes == 16
        assert lookup_route({"A|B": route}, "A", "B").duration_minutes == 16
        assert lookup_route({"A→B": route}, "A", "B").duration_minutes == 16
        assert lookup_route({"A": {"B": route}}, "A", "B").duration_minutes == 16
        assert lookup_route({"A": route.model_dump()}, "A", "B").duration_minutes == 16

    def test_envelope_string_is_parsed(self):
        """高德 route 工具返回信封 JSON 字符串，真实形状照抄自 tools/route.py 的平铺字段。"""
        envelope = json.dumps({"status": "OK", "distance_meters": 100, "duration_seconds": 60})

        route = lookup_route({"A|B": envelope}, "A", "B")

        assert route is not None
        assert route.duration_minutes == 1

    def test_failed_envelope_yields_no_route(self):
        """PRD §32：高德失败就返回 None，绝不允许在这里猜一条路线。"""
        envelope = json.dumps({"status": "UNAVAILABLE", "error": "INVALID_PARAMS", "distance_meters": 100})

        assert lookup_route({"A|B": envelope}, "A", "B") is None

    def test_missing_route_returns_none(self):
        assert lookup_route({}, "A", "B") is None
        assert lookup_route(None, "A", "B") is None
        assert lookup_route({"X": object()}, "A", "B") is None

    def test_missing_ids_return_none(self):
        route = RouteOption(distance_meters=100, duration_seconds=60)

        assert lookup_route({"A|B": route}, None, "B") is None
        assert lookup_route({"A|B": route}, "A", None) is None


class TestRouteEfficiencyPenalty:
    def test_missing_route_is_the_worst_case(self):
        assert route_efficiency_penalty(None) == pytest.approx(40.0 + 20.0)

    def test_verified_short_route_has_no_penalty(self):
        route = RouteOption(distance_meters=1000, duration_seconds=600, verified=True)

        assert route_efficiency_penalty(route) == 0.0

    def test_unverified_route_is_penalised(self):
        route = RouteOption(distance_meters=1000, duration_seconds=600, verified=False)

        assert route_efficiency_penalty(route) == pytest.approx(20.0)

    def test_long_distance_and_duration_add_penalty(self):
        """>3km 每公里 +5、>35 分钟每分钟 +1，两者叠加。"""
        route = RouteOption(distance_meters=6000, duration_seconds=3000, verified=True)

        assert route_efficiency_penalty(route) == pytest.approx(15.0 + 15.0)


# ==================================================
# 三、POI 归一化（PRD §20）
# ==================================================


class TestNormalizePlaceName:
    def test_prd_three_variants_collapse_to_one_key(self):
        """PRD §20 点名的三种写法必须是同一个键，否则证据永远分散在三条上。"""
        keys = {
            normalize_place_name("宽窄巷子", "成都"),
            normalize_place_name("宽窄巷子景区", "成都"),
            normalize_place_name("成都宽窄巷子", "成都"),
        }

        assert keys == {"宽窄巷子"}

    def test_fullwidth_punctuation_and_brackets_are_stripped(self):
        """全角/半角括号与大小写差异不应该产生两个键。"""
        assert normalize_place_name("宽窄巷子（景区）") == normalize_place_name("宽窄巷子(景区)")
        assert normalize_place_name("宽窄巷子Ａ区") == normalize_place_name("宽窄巷子a区")

    def test_semantic_suffixes_are_protected(self):
        """剥"公园/广场"这类语义后缀会把人民公园和人民广场并成一个，必须保守。"""
        assert normalize_place_name("人民公园", "成都") != normalize_place_name("人民广场", "成都")
        assert normalize_place_name("四川省博物馆", "成都") != normalize_place_name("四川省图书馆", "成都")

    def test_shop_suffix_is_not_truncated(self):
        """单字后缀一律不剥："明婷饭店"不能变成"明婷饭"。"""
        assert normalize_place_name("明婷饭店") == "明婷饭店"

    def test_city_like_shop_name_is_not_stripped(self):
        """"重庆火锅"是以城市名开头的店名，剥完只剩"火锅"就会和别家撞车。"""
        assert normalize_place_name("重庆火锅") == "重庆火锅"
        assert normalize_place_name("哈尔冰") == "哈尔冰"

    def test_explicit_city_prefix_is_stripped_with_two_char_remainder(self):
        """city 参数明确匹配时，剥完留 2 个字即可（"成都火锅" → "火锅" 只在明确城市下成立）。"""
        assert normalize_place_name("成都火锅", "成都") == "火锅"

    def test_museum_suffix_collapses_to_base_name(self):
        """实际行为：WEAK_SUFFIXES 里的"博物馆"会被剥掉，所以"杜甫草堂博物馆"归一到"杜甫草堂"。"""
        assert normalize_place_name("杜甫草堂博物馆", "成都") == "杜甫草堂"
        assert normalize_place_name("杜甫草堂", "成都") == "杜甫草堂"

    def test_blank_name_returns_empty_key(self):
        assert normalize_place_name("") == ""
        assert normalize_place_name("（）") == ""


class TestNameSimilarity:
    def test_equal_normalized_names_score_one(self):
        assert name_similarity("宽窄巷子", "宽窄巷子景区") == 1.0

    def test_containment_scores_by_length_ratio(self):
        """"锦里" vs "锦里古街"：包含关系但不是同一件事，按长度比给 0.5。"""
        assert name_similarity("锦里", "锦里古街") == pytest.approx(0.5)

    def test_unrelated_names_score_low(self):
        assert name_similarity("宽窄巷子", "锦里") == 0.0

    def test_partial_overlap_uses_char_and_sequence_terms(self):
        score = name_similarity("明婷饭店", "明婷小馆")

        assert 0.0 < score < 1.0
        assert score == pytest.approx(0.4167, abs=0.01)

    def test_blank_names_score_zero(self):
        assert name_similarity("", "宽窄巷子") == 0.0


class TestDedupePlaces:
    def test_three_variants_merge_into_one_with_full_evidence_chain(self):
        """合并后必须保留被吸收的 place_id：证据挂在 place_id 上，丢了就等于丢证据。"""
        places = [
            make_place("B1", "宽窄巷子", lat=30.6691, lng=104.0555),
            make_place("B2", "宽窄巷子景区", lat=30.6695, lng=104.0560),
            make_place("B3", "成都宽窄巷子", lat=30.6690, lng=104.0550),
        ]

        merged, decisions = dedupe_places(places)

        assert len(merged) == 1
        # 代表点取"名字更长（更具体）"者：宽窄巷子景区 (6 字) 胜过 宽窄巷子 (4 字)。
        assert merged[0].place_id == "B2"
        assert merged[0].merged_from == ["B1", "B3"]
        assert sorted({merged[0].place_id, *merged[0].merged_from}) == ["B1", "B2", "B3"]
        assert set(merged[0].aliases) >= {"宽窄巷子", "成都宽窄巷子"}
        assert merged[0].normalized_name == "宽窄巷子"
        assert len(decisions) == 3

    def test_decisions_record_representative_and_duplicates(self):
        merged, decisions = dedupe_places(
            # A 有高德验证标记，字段更完整，必然是代表点（被吸收的仍是 B）。
            [make_place("A", "宽窄巷子", lat=30.669, lng=104.055, amap_verified=True),
             make_place("B", "宽窄巷子景区", lat=30.6691, lng=104.0551)]
        )

        keep = [d for d in decisions if d.status == DecisionStatus.KEEP]
        reject = [d for d in decisions if d.status == DecisionStatus.REJECT]

        assert keep[0].entity_id == "A"
        assert keep[0].reason_codes[0] == "representative"
        assert keep[0].scores["merged_count"] == 1
        assert reject[0].entity_id == "B"
        assert "duplicate_of:A" in reject[0].reason_codes
        assert reject[0].reason_text  # 可审计：必须有原因文本
        assert reject[0].scores  # 必须带判定依据明细

    def test_geo_signal_is_recorded_in_scores(self):
        _, decisions = dedupe_places(
            [make_place("A", "宽窄巷子", lat=30.669, lng=104.055),
             make_place("B", "宽窄巷子景区", lat=30.6691, lng=104.0551)]
        )

        duplicate = decisions[-1]

        assert "geo_name" in duplicate.reason_codes
        assert duplicate.scores["distance_meters"] < 150
        assert duplicate.scores["name_similarity"] == 1.0

    def test_same_poi_id_merges_even_when_coordinates_conflict(self):
        """高德 POI ID 优先级最高：id 相同就是同一实体，坐标冲突也要合并。"""
        merged, decisions = dedupe_places(
            [make_place("X", "某店", lat=30.60, lng=104.00),
             make_place("X", "另一个名字", lat=31.50, lng=105.50)]
        )

        assert len(merged) == 1
        assert any("same_poi_id" in d.reason_codes for d in decisions)

    def test_representative_prefers_amap_verified(self):
        """代表点选：高德验证过 > 字段更完整 > 名字更长。"""
        merged, _ = dedupe_places(
            [make_place("A", "宽窄巷子", lat=30.669, lng=104.055),
             make_place("B", "宽窄巷子景区", lat=30.6691, lng=104.0551, amap_verified=True,
                        address="青羊区长顺街", district="青羊区")]
        )

        assert merged[0].place_id == "B"
        assert merged[0].merged_from == ["A"]

    def test_coordinates_are_backfilled_from_absorbed_place(self):
        """代表点没有坐标时，必须从被吸收的候选里补上，否则地点变成"位置未知"。"""
        merged, _ = dedupe_places(
            [make_place("A", "宽窄巷子"),
             make_place("B", "宽窄巷子景区", lat=30.6691, lng=104.0555)]
        )

        assert merged[0].coords == pytest.approx((30.6691, 104.0555))

    def test_normalized_name_merges_when_both_have_no_coordinates(self):
        """没有坐标时只能靠归一化名字合并；GEOM 护栏失效，这是代码的取舍。"""
        merged, decisions = dedupe_places(
            [make_place("A", "宽窄巷子"), make_place("B", "成都宽窄巷子景区")]
        )

        assert len(merged) == 1
        assert any("normalized_name" in d.reason_codes for d in decisions)

    def test_same_name_with_far_coordinates_is_not_merged(self):
        """PRD §20：同名不同地不合并。宁可留两条候选，也不要错并。"""
        merged, decisions = dedupe_places(
            [make_place("A", "杜甫草堂", lat=30.66, lng=104.02),
             make_place("B", "杜甫草堂博物馆", lat=30.95, lng=104.30)]
        )

        assert {p.place_id for p in merged} == {"A", "B"}
        assert decisions == []

    def test_different_places_are_not_merged(self):
        merged, decisions = dedupe_places(
            [make_place("A", "宽窄巷子", lat=30.669, lng=104.055),
             make_place("B", "锦里", lat=30.640, lng=104.040)]
        )

        assert len(merged) == 2
        assert decisions == []

    def test_alias_hit_merges(self):
        merged, decisions = dedupe_places(
            [make_place("A", "龙抄手", aliases=["龙抄手总店"]), make_place("B", "龙抄手总店")]
        )

        assert len(merged) == 1
        assert any("alias_hit" in d.reason_codes for d in decisions)

    def test_共享类别词别名不会合并不同地点(self):
        """类别词别名（"景点"）说的是"哪一类地方"，不是"哪一个地方"，不能用来判重。

        真实 run 里检索词被登记成每个 POI 的别名，8 个检索词把 64 个互不相关的地点
        错并成 8 个，5 天行程里有 3 天没有任何安排。
        """
        merged, decisions = dedupe_places(
            [
                make_place("A", "宽窄巷子", aliases=["景点"], lat=30.669, lng=104.055),
                make_place("B", "锦里", aliases=["景点", "美食"], lat=30.640, lng=104.040),
            ]
        )

        assert {p.place_id for p in merged} == {"A", "B"}
        assert decisions == []

    def test_共享有辨识度的别名仍然合并(self):
        """护栏只该挡住类别词，不该把"别名命中"这个正常信号一起砍掉。"""
        merged, decisions = dedupe_places(
            [
                make_place("A", "甲天下川菜", aliases=["春熙路"]),
                make_place("B", "乙家川菜小馆", aliases=["春熙路"]),
            ]
        )

        assert len(merged) == 1
        assert any("alias_hit" in d.reason_codes for d in decisions)

    def test_identical_address_merges(self):
        merged, decisions = dedupe_places(
            [make_place("A", "明婷饭店", address="武侯区人民南路1号"),
             make_place("B", "明婷小馆", address="武侯区人民南路1号")]
        )

        assert len(merged) == 1
        assert any("same_address" in d.reason_codes for d in decisions)

    def test_address_city_prefix_is_not_normalised_away(self):
        """与 PRD 偏差（已在报告中标出）：_address_key 的 docstring 说"成都市武侯区X路1号"
        应与"武侯区X路1号"对上，实际只剥**末尾**的行政区字，所以两者并不相等、不会合并。
        这里锁住实际行为。"""
        merged, decisions = dedupe_places(
            [make_place("A", "明婷饭店", address="成都市武侯区人民南路1号"),
             make_place("B", "明婷小馆", address="武侯区人民南路1号")]
        )

        assert len(merged) == 2
        assert decisions == []

    def test_empty_input(self):
        assert dedupe_places([]) == ([], [])


# ==================================================
# 四、Trust Score（PRD §18）
# ==================================================

DETAIL_TEXT = "点了龙抄手和钟水饺，人均40元，排队大约20分钟，分量很足"
VAGUE_TEXT = "这家店还不错，大家可以去看看，环境挺好的，服务也可以，整体感觉值得一试"


class TestTrustScoreWeights:
    def test_component_maxima_match_prd_weights(self):
        """PRD §18 的六项权重：25 / 20 / 20 / 15 / 10 / 10，合计 100。"""
        _, details = trust_score(make_place(), [])

        expected = {
            "multi_source": TRUST_WEIGHT_SOURCES,
            "scale": TRUST_WEIGHT_SCALE,
            "specific_detail": TRUST_WEIGHT_DETAIL,
            "persistence": TRUST_WEIGHT_PERSISTENCE,
            "local_repeat": TRUST_WEIGHT_LOCAL,
            "official_consistency": TRUST_WEIGHT_OFFICIAL,
        }
        assert sum(expected.values()) == 100.0
        for key, max_score in expected.items():
            assert details[key]["max"] == max_score

    def test_details_document_every_component(self):
        """明细要能直接进 audit：每项都要有 score / max / basis。"""
        score, details = trust_score(make_place(), [make_evidence(text=DETAIL_TEXT)])

        assert details["total"] == score
        for key in ("multi_source", "scale", "specific_detail", "persistence",
                    "local_repeat", "official_consistency"):
            assert details[key]["basis"]  # 有人话依据
            assert 0.0 <= details[key]["score"] <= details[key]["max"]

    def test_score_is_within_0_100(self):
        score, _ = trust_score(make_place(amap_verified=True, address="青羊区长顺街", opening_hours="09:00-22:00"),
                               [make_evidence(str(i), text=DETAIL_TEXT, raw_metrics={"likes": 10 ** 6})
                                for i in range(6)])

        assert 0.0 <= score <= 100.0


class TestTrustScoreNoEvidence:
    def test_no_evidence_scores_zero(self):
        """没有证据就是 0：Evidence First 的含义是"没证据拿不到 Trust"，不给中性默认值。"""
        score, details = trust_score(make_place(), [])

        assert score == 0.0
        assert details["evidence_count"] == 0
        assert details["official_consistency"]["basis"] == "没有高德或官方来源"

    def test_no_evidence_only_keeps_amap_points(self):
        """高德验证过的地点即使没有社媒证据，也只剩那 6 分（+6 是 POI 验证）。"""
        score, details = trust_score(make_place(amap_verified=True), [])

        assert score == 6.0
        assert "高德 POI 已验证 +6" in details["official_consistency"]["basis"]

    def test_amap_verified_argument_equals_place_flag(self):
        """显式传入的 amap_verified 与 Place 自身字段等价，都只加 +6。"""
        by_arg, _ = trust_score(make_place(), [], amap_verified=True)
        by_flag, _ = trust_score(make_place(amap_verified=True), [])

        assert by_arg == by_flag == 6.0

    def test_scale_degrades_to_appearance_count_without_metrics(self):
        """没有互动量时"规模"退化：同样条数只能拿到互动量路径的 4 成。"""
        with_metrics, _ = trust_score(make_place(), [make_evidence("e1", raw_metrics={"likes": 50000})])
        without_metrics, without_details = trust_score(
            make_place(), [make_evidence(str(i)) for i in range(5)]
        )

        assert without_details["scale"]["score"] == pytest.approx(8.0)
        assert with_metrics > without_metrics


class TestTrustScoreMonotonicity:
    def test_more_independent_platforms_score_higher(self):
        one_platform, _ = trust_score(make_place(), [make_evidence("e1")])
        three_platforms, _ = trust_score(
            make_place(),
            [make_evidence("e1", provider="a"), make_evidence("e2", provider="b"),
             make_evidence("e3", provider="c")],
        )

        assert three_platforms > one_platform

    def test_more_evidence_scores_higher_on_same_platform(self):
        one, _ = trust_score(make_place(), [make_evidence("e1")])
        three, _ = trust_score(make_place(), [make_evidence(f"e{i}") for i in range(3)])

        assert three > one

    def test_more_specific_detail_scores_higher(self):
        vague, _ = trust_score(make_place(), [make_evidence("e1", text=VAGUE_TEXT)])
        specific, specific_details = trust_score(make_place(), [make_evidence("e1", text=DETAIL_TEXT)])

        assert specific > vague
        assert specific_details["specific_detail"]["detail_ratio"] == 1.0

    def test_specific_dishes_boost_the_detail_component(self):
        dish, details = trust_score(
            make_place(), [make_evidence("e1", text="好吃", specific_dishes=["龙抄手"])]
        )
        dish_component = details["specific_detail"]

        assert dish_component["dish_ratio"] == 1.0
        assert dish_component["dishes"] == ["龙抄手"]
        assert dish > 0.0

    def test_longer_time_span_raises_persistence(self):
        single, _ = trust_score(make_place(), [make_evidence("e1", published_at=datetime(2026, 1, 1))])
        spread, spread_details = trust_score(
            make_place(),
            [make_evidence("e1", published_at=datetime(2026, 1, 1)),
             make_evidence("e2", published_at=datetime(2026, 4, 1)),
             make_evidence("e3", published_at=datetime(2026, 8, 1))],
        )

        assert spread > single
        assert spread_details["persistence"]["distinct_months"] == 3

    def test_local_hints_raise_the_local_component(self):
        _, none = trust_score(make_place(), [make_evidence("e1", text="随便逛逛")])
        _, some = trust_score(make_place(), [make_evidence("e1", text="本地人推荐，第二次来吃")])

        assert none["local_repeat"]["score"] == 0.0
        assert some["local_repeat"]["score"] > 0.0
        assert "本地人" in some["local_repeat"]["hits"]

    def test_engagement_saturates_at_target(self):
        _, details = trust_score(make_place(), [make_evidence("e1", raw_metrics={"likes": 50000})])

        assert details["scale"]["score"] == TRUST_WEIGHT_SCALE
        assert details["scale"]["engagement"] == 50000

    def test_engagement_takes_max_field_not_sum(self):
        """likes+views 相加会制造"规模是别人十倍"的假象，只能取最大字段。"""
        _, details = trust_score(
            make_place(), [make_evidence("e1", raw_metrics={"likes": 5000, "views": 9000})]
        )

        assert details["scale"]["engagement"] == 9000


class TestTrustScoreOfficial:
    def test_amap_detail_fields_add_two(self):
        score, details = trust_score(
            make_place(address="青羊区长顺街", opening_hours="09:00-22:00"), []
        )

        assert score == 2.0
        assert "地址与营业时间" in details["official_consistency"]["basis"]

    def test_web_or_official_source_adds_two(self):
        _, details = trust_score(make_place(), [make_evidence("e1", provider="tavily", source_type="web")])

        assert "官方/网页来源" in details["official_consistency"]["basis"]

    def test_official_component_is_capped_at_ten(self):
        score, details = trust_score(
            make_place(amap_verified=True, address="青羊区长顺街", opening_hours="09:00-22:00"),
            [make_evidence("e1", provider="amap", source_type="official")],
        )
        # 6（POI）+ 2（详情）+ 2（官方来源）= 10，正好封顶
        assert details["official_consistency"]["score"] == 10.0
        assert score >= 10.0


# ==================================================
# 五、Ad Risk（PRD §19）
# ==================================================

AD_TEXT = "这家店真的绝绝子yyds必吃天花板，探店合作品牌方，超级好吃太赞了，一定要来打卡"
NATURAL_TEXT = "本地人第二次来，点了龙抄手，人均40元，但是排队有点久，分量足"
NATURAL_TEXT_2 = "老店，经常来吃，吃了钟水饺，人均30元，缺点是偏贵，不过味道稳定"


class TestAdRiskHigh:
    def test_marketing_template_scores_high(self):
        score, details = ad_risk(make_place(), [make_evidence("a1", text=AD_TEXT)])

        assert score >= 60.0
        assert details["commercial_signal"]["score"] == 20.0
        assert details["hype_words"]["score"] == 20.0
        assert details["vague"]["score"] == 15.0

    def test_hits_are_listed_for_audit(self):
        _, details = ad_risk(make_place(), [make_evidence("a1", text=AD_TEXT)])

        assert {"探店", "合作", "品牌方"} <= set(details["commercial_signal"]["hits"])
        assert {"yyds", "天花板"} <= set(details["hype_words"]["hits"])

    def test_no_evidence_scores_no_risk_because_nothing_is_evaluable(self):
        """完全没有证据时 Ad Risk 记 0。

        "含糊""只夸不贬"描述的是"有人这么说，但说得不具体"，不是"没人说过"。
        把证据缺失折算成 19 分风险会让任何只有高德 POI 的地点综合分变负、永远进不了
        行程 —— 缺证据的代价已经由 Trust 承担，不该再罚一次。
        """
        score, details = ad_risk(make_place(), [])

        assert score == 0.0
        assert details["vague"]["score"] == 0.0
        assert "无从判断" in details["vague"]["basis"]
        assert details["emotion_no_downside"]["score"] == 0.0
        assert "无从判断" in details["emotion_no_downside"]["basis"]

    def test_same_period_cluster_raises_risk(self):
        """同期集中投放（30 天内 ≥5 条）记满 10 分。"""
        evidences = [
            make_evidence(f"c{i}", text=f"{NATURAL_TEXT}{i}", published_at=datetime(2026, 3, 1 + i))
            for i in range(5)
        ]
        _, details = ad_risk(make_place(), evidences)

        assert details["same_period_cluster"]["max_in_window"] == 5
        assert details["same_period_cluster"]["score"] == 10.0


class TestAdRiskLow:
    def test_personal_specific_text_scores_low(self):
        score, details = ad_risk(make_place(), [make_evidence("o1", text=NATURAL_TEXT)])

        assert score <= 20.0
        assert details["relief"]["score"] < 0  # 有降风险信号

    def test_downsides_and_repeat_visits_lower_risk(self):
        _, details = ad_risk(
            make_place(),
            [make_evidence("o1", provider="a", text=NATURAL_TEXT),
             make_evidence("o2", provider="b", text=NATURAL_TEXT_2)],
        )

        assert details["relief"]["score"] <= -20.0
        assert any("缺点" in part for part in [details["relief"]["basis"]])

    def test_score_is_bounded_and_documented(self):
        for evidences in ([], [make_evidence("a1", text=AD_TEXT)], [make_evidence("o1", text=NATURAL_TEXT)]):
            score, details = ad_risk(make_place(), evidences)

            assert 0.0 <= score <= 100.0
            assert details["total"] == score
            assert details["evidence_count"] == len(evidences)
            for key in ("repeated_copy", "commercial_signal", "hype_words", "vague",
                        "same_period_cluster", "emotion_no_downside", "relief"):
                assert details[key]["basis"]


class TestAdRiskSimilarity:
    def test_single_text_cannot_be_compared(self):
        """只有一条文本时相似度分量必须是 0，不能凭一条文本判"模板化"。"""
        _, details = ad_risk(make_place(), [make_evidence("a1", text=AD_TEXT)])

        assert details["repeated_copy"]["score"] == 0.0

    def test_near_duplicate_texts_hit_the_similarity_component(self):
        near = NATURAL_TEXT[:-1] + "呀"
        _, details = ad_risk(
            make_place(), [make_evidence("a1", text=NATURAL_TEXT), make_evidence("a2", text=near)]
        )

        assert details["repeated_copy"]["avg_similarity"] > 0.85
        assert details["repeated_copy"]["score"] == 25.0

    def test_identical_texts_are_maximally_suspicious(self):
        score, details = ad_risk(
            make_place(), [make_evidence("a1", text=NATURAL_TEXT), make_evidence("a2", text=NATURAL_TEXT)]
        )

        assert details["repeated_copy"]["avg_similarity"] == 1.0
        assert score > ad_risk(make_place(), [make_evidence("a1", text=NATURAL_TEXT)])[0]


# ==================================================
# 六、预算引擎（PRD §23）
# ==================================================


class TestCityTier:
    def test_tier1_city(self):
        assert city_tier("北京") == "tier1"

    def test_tier2_city(self):
        assert city_tier("成都") == "tier2"

    def test_unknown_city_falls_to_tier3_but_empty_is_default(self):
        """认不出的城市按最低档（tier3）算；完全没给城市时才用全国中位 tier2。"""
        assert city_tier("张掖") == "tier3"
        assert city_tier(None) == "tier2"


class TestBudgetSeparation:
    def test_realtime_and_estimated_are_strictly_separated(self):
        """PRD §23：不允许把估算写成实时价格，两者必须是不同字段 + 不同 breakdown_price_type。"""
        intent = TripIntent(destination=["成都"], days=2, travelers=1)
        summary = build_budget(intent, make_flight(), make_hotel(), city="成都")

        assert summary.breakdown_price_type["交通"] == "realtime"
        assert summary.breakdown_price_type["住宿"] == "realtime"
        assert summary.breakdown_price_type["餐饮估算"] == "estimated"
        assert summary.known_real_cost + summary.estimated_cost == pytest.approx(summary.projected_total)

    def test_nothing_selected_leaves_zero_but_is_noted(self):
        summary = build_budget(TripIntent(destination=["成都"], days=1), None, None, city="成都")

        assert summary.breakdown["交通"] == 0.0
        assert summary.breakdown["住宿"] == 0.0
        assert any("没有选定大交通方案" in note for note in summary.price_notes)
        assert any("没有选定酒店" in note for note in summary.price_notes)

    def test_missing_flight_price_contributes_zero_and_is_noted(self):
        """拿不到真实价就计 0 并写进 notes，绝不猜一个数。"""
        summary = build_budget(
            TripIntent(destination=["成都"], days=1), make_flight(price=None), None, city="成都"
        )

        assert summary.breakdown["交通"] == 0.0
        assert summary.breakdown_price_type["交通"] == "unknown"
        assert any("未返回价格" in note for note in summary.price_notes)

    def test_missing_hotel_price_contributes_zero_and_is_noted(self):
        summary = build_budget(
            TripIntent(destination=["成都"], days=2), None, make_hotel(price_per_night=None), city="成都"
        )

        assert summary.breakdown["住宿"] == 0.0
        assert summary.breakdown_price_type["住宿"] == "unknown"
        assert any("没有返回价格" in note for note in summary.price_notes)


class TestBudgetHotelMaths:
    def test_per_night_times_nights_times_rooms(self):
        """3 人 → 2 间（每间 2 人）× 2 晚 × ¥420 = ¥1680，房间数口径写进 notes。"""
        intent = TripIntent(destination=["成都"], days=3, travelers=3)
        hotel = make_hotel(check_in=BASE_DATE, check_out=date(2026, 10, 3))

        summary = build_budget(intent, None, hotel, city="成都")

        assert len(hotel.model_dump()) > 0  # 防止误删字段导致下面断言失去意义
        assert summary.breakdown["住宿"] == pytest.approx(420.0 * 2 * 2)
        assert any(f"住宿按 2 晚 × {math.ceil(3 / TRAVELERS_PER_ROOM)} 间折算" == note
                   for note in summary.price_notes)

    def test_total_price_takes_precedence_over_nightly(self):
        """Provider 直接给了总价就用总价，不再自己乘。"""
        hotel = make_hotel(
            price_per_night=420.0, total_price=900.0,
            check_in=BASE_DATE, check_out=date(2026, 10, 3),
        )
        summary = build_budget(TripIntent(destination=["成都"], days=3, travelers=2), None, hotel, city="成都")

        assert summary.breakdown["住宿"] == pytest.approx(900.0)

    def test_unknown_nights_means_hotel_is_not_charged(self):
        """晚数为 0（当天来回）时不能凭空乘以 1 晚。"""
        summary = build_budget(
            TripIntent(destination=["成都"], days=1, travelers=2), None, make_hotel(), city="成都"
        )

        assert summary.breakdown["住宿"] == 0.0
        assert any("0 晚" in note for note in summary.price_notes)

    def test_day_count_override_controls_nights(self):
        hotel = make_hotel(price_per_night=300.0)
        summary = build_budget(
            TripIntent(destination=["成都"], days=3, travelers=1), None, hotel, city="成都", day_count=4
        )

        assert summary.breakdown["住宿"] == pytest.approx(300.0 * 3 * 1)

    def test_price_note_caveat_is_preserved(self):
        """途牛只给"起价"，预算备注必须保留这个免责说明，不能当成确定价。"""
        hotel = make_hotel(price_note="420 起价/晚")

        summary = build_budget(TripIntent(destination=["成都"], days=2), None, hotel, city="成都")

        joined = "；".join(summary.price_notes)
        assert "420 起价/晚" in joined
        assert "最终以预订页为准" in joined


class TestBudgetTickets:
    def test_only_realtime_ticket_prices_count(self):
        day = make_day([
            make_item("i1", type="attraction", place_id="P1", name="有票价", price=50.0, price_type="realtime"),
            make_item("i2", type="attraction", place_id="P2", name="无票价", price_type="unknown"),
        ])
        summary = build_budget(
            TripIntent(destination=["成都"], days=1, travelers=2), None, None, [day], city="成都"
        )

        assert summary.breakdown["门票"] == pytest.approx(100.0)
        assert any("1 个景点没有实时门票价" in note for note in summary.price_notes)

    def test_estimated_ticket_price_is_not_treated_as_real(self):
        """price_type=estimated 的门票不能进真实门票口径。"""
        day = make_day([
            make_item("i1", type="attraction", place_id="P1", name="估算票", price=80.0, price_type="estimated"),
        ])
        summary = build_budget(TripIntent(destination=["成都"], days=1), None, None, [day], city="成都")

        assert summary.breakdown["门票"] == 0.0

    def test_ticket_lookup_by_place_id_or_name(self):
        day = make_day([
            make_item("i1", type="attraction", place_id="P1", name="按ID查"),
            make_item("i2", type="attraction", place_id="P2", name="按名字查"),
        ])
        summary = build_budget(
            TripIntent(destination=["成都"], days=1), None, None, [day],
            {"P1": 30.0, "按名字查": 20.0}, city="成都",
        )

        assert summary.breakdown["门票"] == pytest.approx(50.0)

    def test_ticket_counted_per_traveller(self):
        day = make_day([make_item("i1", type="attraction", place_id="P1", name="x", price=60.0,
                                  price_type="realtime")])
        summary = build_budget(TripIntent(destination=["成都"], days=1, travelers=4), None, None, [day],
                               city="成都")

        assert summary.breakdown["门票"] == pytest.approx(240.0)


class TestBudgetCityTransport:
    def test_amap_fare_is_real_and_missing_legs_are_estimated(self):
        """高德返回票价的段算 realtime，没有票价的段才按 6 元/段估算。"""
        day = make_day([
            make_item("i1", type="attraction", place_id="P1", name="A",
                      travel_from_previous=TravelLeg.from_route(None, fallback_minutes=10)),
            make_item("i2", type="attraction", place_id="P2", name="B",
                      travel_from_previous=TravelLeg(mode="transit", estimated_cost=4.0, verified=True)),
            make_item("i3", type="attraction", place_id="P3", name="C",
                      travel_from_previous=TravelLeg.from_route(None, fallback_minutes=15)),
        ])
        summary = build_budget(TripIntent(destination=["成都"], days=1, travelers=2), None, None, [day],
                               city="成都")

        # 真实票价 4×2 = 8；2 段没有票价 → 2×6×2 = 24
        assert summary.breakdown["市内交通"] == pytest.approx(8.0 + 24.0)
        assert summary.breakdown_price_type["市内交通"] == "estimated"
        assert any("2 段没有高德票价" in note for note in summary.price_notes)

    def test_day_without_any_route_is_charged_by_day(self):
        """一天里完全没有高德路线数据时，按城市档的"每天"兜底估算。"""
        day = make_day([
            make_item("i1", type="attraction", place_id="P1", name="A", start_time="09:00"),
            make_item("i2", type="attraction", place_id="P2", name="B", start_time="11:00"),
        ])
        summary = build_budget(TripIntent(destination=["成都"], days=1), None, None, [day], city="成都")

        assert summary.breakdown["市内交通"] == pytest.approx(30.0)


class TestBudgetMealsAndTotals:
    def test_meal_estimate_follows_city_tier(self):
        intent = TripIntent(destination=["北京"], days=2, travelers=2)
        tier1 = build_budget(intent, None, None, city="北京")
        tier3 = build_budget(intent, None, None, city="张掖")

        assert tier1.breakdown["餐饮估算"] == pytest.approx(MEAL_ESTIMATE_PER_DAY["tier1"] * 2 * 2)
        assert tier3.breakdown["餐饮估算"] == pytest.approx(MEAL_ESTIMATE_PER_DAY["tier3"] * 2 * 2)
        assert tier1.breakdown["餐饮估算"] > tier3.breakdown["餐饮估算"]

    def test_travellers_and_days_multiply_the_total(self):
        one = build_budget(TripIntent(destination=["成都"], days=1, travelers=1), None, None, city="成都")
        more = build_budget(TripIntent(destination=["成都"], days=3, travelers=2), None, None, city="成都")

        assert more.projected_total > one.projected_total
        assert more.projected_total == pytest.approx(one.breakdown["餐饮估算"] * 6)

    def test_other_cost_is_estimated(self):
        summary = build_budget(
            TripIntent(destination=["成都"], days=1), None, None, city="成都", other_cost=200.0
        )

        assert summary.breakdown["其他"] == pytest.approx(200.0)
        assert summary.breakdown_price_type["其他"] == "estimated"

    def test_inbound_transport_is_summed(self):
        plan = TransportPlan(selected=make_flight(price=880.0), inbound_selected=make_flight(price=900.0))
        summary = build_budget(TripIntent(destination=["成都"], days=2, travelers=2), plan, None, city="成都")

        assert summary.breakdown["交通"] == pytest.approx((880.0 + 900.0) * 2)

    def test_meal_note_says_it_is_an_estimate(self):
        summary = build_budget(TripIntent(destination=["成都"], days=1), None, None, city="成都")

        assert any("估算值，不是实时价格" in note for note in summary.price_notes)


class TestBudgetOverBudget:
    def test_status_and_over_by(self):
        intent = TripIntent(destination=["成都"], days=3, travelers=3, budget_total=5000.0)
        hotel = make_hotel(check_in=BASE_DATE, check_out=date(2026, 10, 3))
        summary = build_budget(intent, make_flight(), hotel, city="成都")

        assert summary.status == "over_budget"
        assert summary.remaining is not None and summary.remaining < 0
        assert summary.over_by == pytest.approx(-summary.remaining)

    def test_unknown_budget_status_and_no_suggestions(self):
        summary = build_budget(TripIntent(destination=["成都"], days=1, budget_total=None), None, None,
                               city="成都")

        assert summary.status == "unknown"
        assert summary.remaining is None
        assert summary.optimization_suggestions == []

    def test_within_budget_has_no_shortfall(self):
        summary = build_budget(
            TripIntent(destination=["成都"], days=1, budget_total=10000.0), None, None, city="成都"
        )

        assert summary.status == "within_budget"
        assert summary.over_by == 0.0

    def test_suggestions_come_from_real_alternative_prices(self):
        """建议只能来自真实候选的价差：这里给出更便宜的酒店和更便宜的航班。"""
        intent = TripIntent(destination=["成都"], days=3, travelers=1, budget_total=500.0)
        hotel = make_hotel(price_per_night=600.0, check_in=BASE_DATE, check_out=date(2026, 10, 3))
        plan = TransportPlan(
            selected=make_flight(price=1000.0),
            alternatives=[make_flight(flight_no="CA2", price=600.0)],
        )
        summary = build_budget(intent, plan, hotel, city="成都",
                               hotel_alternatives=[make_hotel(hotel_id="h2", name="便宜酒店",
                                                              price_per_night=300.0)])

        assert summary.status == "over_budget"
        assert len(summary.optimization_suggestions) == 2
        joined = "；".join(summary.optimization_suggestions)
        # 省钱最多的排前面（机票省 400 > 酒店省 300×2=600？这里按 saving 降序）
        assert "CA2" in joined
        assert "便宜酒店" in joined
        assert all("可省" in suggestion for suggestion in summary.optimization_suggestions)

    def test_dearer_alternatives_are_ignored(self):
        intent = TripIntent(destination=["成都"], days=2, travelers=1, budget_total=100.0)
        plan = TransportPlan(selected=make_flight(price=500.0),
                             alternatives=[make_flight(flight_no="CA9", price=900.0)])
        summary = build_budget(intent, plan, make_hotel(), city="成都")

        assert summary.optimization_suggestions
        assert "CA9" not in summary.optimization_suggestions[0]

    def test_no_cheaper_candidate_is_stated_honestly(self):
        """没有更便宜的候选时必须如实说明，而不是编一条"建议换酒店"。"""
        intent = TripIntent(destination=["成都"], days=3, travelers=3, budget_total=1000.0)
        hotel = make_hotel(check_in=BASE_DATE, check_out=date(2026, 10, 3))
        summary = build_budget(intent, make_flight(), hotel, city="成都")

        assert len(summary.optimization_suggestions) == 1
        assert "没有更便宜的方案" in summary.optimization_suggestions[0]
        assert "缺口" in summary.optimization_suggestions[0]


# ==================================================
# 七、营业时间解析（PRD §22）
# ==================================================


class TestParseOpeningHours:
    def test_simple_range(self):
        opening = parse_opening_hours("08:30-18:30")

        assert opening.open_minutes == 510
        assert opening.close_minutes == 1110
        assert opening.last_entry_minutes is None

    def test_all_day_open(self):
        opening = parse_opening_hours("全天开放")

        assert (opening.open_minutes, opening.close_minutes) == (0, 1440)
        assert opening.note == "全天开放"

    def test_last_entry_is_a_hard_constraint(self):
        opening = parse_opening_hours("周一至周日 09:00-17:00 最晚进入16:30")
        stopped = parse_opening_hours("09:00-17:00 停止入场 16:00")
        parked = parse_opening_hours("08:00-17:30（最晚入园 17:00）")

        assert opening.last_entry_minutes == 16 * 60 + 30
        assert stopped.last_entry_minutes == 16 * 60
        assert parked.last_entry_minutes == 17 * 60

    def test_split_sessions_only_get_a_note(self):
        """分时段与固定闭馆日只提示不建模：宁可审计看到"待确认"，也不假装知道午休。"""
        opening = parse_opening_hours("09:00-12:00,13:00-17:00")

        assert opening.open_minutes == 540
        assert opening.close_minutes == 1020
        assert "分时段" in opening.note

    def test_closed_day_gets_a_confirmation_warning(self):
        opening = parse_opening_hours("周一至周日 09:00-17:00 周一闭馆")

        assert "闭馆" in opening.note

    def test_fullwidth_colon_and_dash_are_normalised(self):
        opening = parse_opening_hours("08：30－18：30")

        assert (opening.open_minutes, opening.close_minutes) == (510, 1110)

    def test_a_closing_time_before_midnight_rolls_to_the_next_day(self):
        """`08:00-00:30` 的 00:30 是次日营业到凌晨，不是"00:30 开门、08:00 关门"。

        真机事故（2026-09-18）：德庄火锅的营业时间是 `周一至周日 08:00-00:30`，
        按 min/max 取时刻被读成"00:30 开门、08:00 关门"，于是当天 13:43 吃火锅
        被报成 `CLOSING_TIME_OVERRUN`（error 级）—— 一个完全错误的结论。
        """
        opening = parse_opening_hours("周一至周日 08:00-00:30")

        assert opening.open_minutes == 8 * 60
        assert opening.close_minutes == 24 * 60 + 30
        assert "跨零点" in opening.note

    def test_a_shift_into_the_small_hours_is_kept_distinct(self):
        """`20:00-02:00` 同理：开门 20:00、关门次日 02:00。"""
        opening = parse_opening_hours("20:00-02:00")

        assert opening.open_minutes == 20 * 60
        assert opening.close_minutes == 26 * 60

    def test_closing_at_24_00_means_end_of_day(self):
        opening = parse_opening_hours("周一至周日 15:00-24:00")

        assert opening.open_minutes == 15 * 60
        assert opening.close_minutes == 24 * 60

    def test_an_ordinary_range_is_not_shifted(self):
        """回归：只有"关门早于开门"才跨零点，普通区间必须原样。"""
        opening = parse_opening_hours("08:30-18:30 最晚进入17:30")

        assert opening.open_minutes == 8 * 60 + 30
        assert opening.close_minutes == 18 * 60 + 30
        assert opening.last_entry_minutes == 17 * 60 + 30

    def test_unparsable_and_blank(self):
        unparsable = parse_opening_hours("看心情")

        assert unparsable.open_minutes is None
        assert unparsable.close_minutes is None
        assert "无法解析" in unparsable.note
        assert parse_opening_hours(None) is None
        assert parse_opening_hours("") is None


# ==================================================
# 八、抵达链与返程截止（PRD §4.3、§21.2）
# ==================================================


class TestArrivalReadyMinutes:
    def test_prd_4_3_arrival_chain(self):
        """PRD §4.3：12:10 落地 + 45 出机场 + 70 到酒店 + 20 入住 = 14:25 才能出门。"""
        result = arrival_ready_minutes(make_flight(), transfer_minutes=70)

        assert result == 12 * 60 + 10 + AIRPORT_BAGGAGE_EXIT_MINUTES + 70 + HOTEL_CHECKIN_MINUTES
        assert minutes_to_clock(result) == "14:25"

    def test_default_transfer_buffer_is_used_when_no_real_route(self):
        result = arrival_ready_minutes(make_flight())

        assert result == (12 * 60 + 10) + 45 + AIRPORT_CITY_TRANSFER_MINUTES + 20

    def test_without_checkin(self):
        with_checkin = arrival_ready_minutes(make_flight(), with_checkin=True)
        without = arrival_ready_minutes(make_flight(), with_checkin=False)

        assert with_checkin - without == HOTEL_CHECKIN_MINUTES

    def test_missing_option_or_arrival_returns_none(self):
        assert arrival_ready_minutes(None) is None
        assert arrival_ready_minutes(make_flight(arrival_at=None)) is None


class TestDepartureDeadline:
    def test_flight_deadline_subtracts_boarding_and_transfer(self):
        flight = make_flight(departure_at=datetime(2026, 10, 3, 18, 0), arrival_at=datetime(2026, 10, 3, 21, 0))

        deadline = departure_deadline_minutes(flight)

        assert deadline == 18 * 60 - AIRPORT_CHECKIN_BUFFER_MINUTES - AIRPORT_CITY_TRANSFER_MINUTES
        assert minutes_to_clock(deadline) == "15:00"

    def test_train_uses_train_buffers(self):
        train = make_train(departure_at=datetime(2026, 10, 3, 20, 0), arrival_at=datetime(2026, 10, 3, 23, 0))

        assert departure_deadline_minutes(train) == 20 * 60 - TRAIN_STATION_BUFFER_MINUTES - TRAIN_STATION_TRANSFER_MINUTES

    def test_missing_departure_returns_none(self):
        assert departure_deadline_minutes(make_flight(departure_at=None)) is None
        assert departure_deadline_minutes(None) is None


class TestTransportBuffers:
    def test_boarding_buffers_differ_by_mode(self):
        """飞机要提前 2 小时，高铁 45 分钟：不能共用一套 buffer。"""
        assert boarding_buffer_minutes(make_flight()) == AIRPORT_CHECKIN_BUFFER_MINUTES
        assert boarding_buffer_minutes(make_train()) == TRAIN_STATION_BUFFER_MINUTES

    def test_city_transfer_fallbacks_differ_by_mode(self):
        assert transfer_buffer_minutes(make_flight()) == AIRPORT_CITY_TRANSFER_MINUTES
        assert transfer_buffer_minutes(make_train()) == TRAIN_STATION_TRANSFER_MINUTES


# ==================================================
# 九、时间可行性引擎（PRD §22）
# ==================================================


class TestCheckFeasibilityArrivalChain:
    def test_prd_4_3_scenario_is_flagged_and_revised(self):
        """PRD §4.3 的完整场景：12:10 落地 → 机场到酒店 70 分钟 → 14:00 进景点必须被识别并修订。

        这里用真实的 70 分钟路线（RouteOption）表达"机场→酒店"，让接驳时间进入不等式，
        而不是靠调用方手动传 transfer_minutes。
        """
        from app.planner import _hotel_item, _transport_item

        flight = make_flight()
        hotel = make_hotel()
        transport_item = _transport_item(flight, item_id="d0-out", role="outbound")
        hotel_item = _hotel_item(hotel, item_id="d0-hotel", check_in=True)
        attraction = make_item("d0-a", type="attraction", place_id="P1", name="武侯祠",
                               start_time="14:00", duration_minutes=120, lat=30.646, lng=104.036)
        day = make_day([transport_item, hotel_item, attraction], day_index=0)
        routes = {
            (transport_item.place_id, hotel.hotel_id): RouteOption(
                distance_meters=21000, duration_seconds=4200, provider="amap"
            ),
            (hotel.hotel_id, "P1"): RouteOption(
                distance_meters=6000, duration_seconds=1200, provider="amap"
            ),
        }

        issues = check_feasibility(day, routes, day_start_floor="14:25")

        assert "TRANSFER_TIME_SHORTFALL" in codes(issues)
        conflict = next(issue for issue in issues if issue.code == "TRANSFER_TIME_SHORTFALL")
        assert conflict.original_start == "14:00"
        assert conflict.revised_start == "15:10"

        revised, final = revise_day(day, issues, routes, day_start_floor="14:25")

        assert revised.item_by_id("d0-a").start_time == "15:10"
        assert revised.item_by_id("d0-a").end_time == "17:10"
        assert check_feasibility(revised, routes, day_start_floor="14:25") == []
        assert all(issue.resolved for issue in final)

    def test_first_item_before_day_floor_is_arrival_chain_conflict(self):
        """当天最早可开始时间之后才允许排景点：14:00 早于 14:25 必须报错。"""
        day = make_day([make_item("a", type="attraction", place_id="P1", name="景点",
                                  start_time="14:00", duration_minutes=120)])

        issues = check_feasibility(day, day_start_floor="14:25")

        assert "ARRIVAL_CHAIN_CONFLICT" in codes(issues)
        conflict = next(issue for issue in issues if issue.code == "ARRIVAL_CHAIN_CONFLICT")
        assert conflict.severity == "error"
        assert conflict.revised_start == "14:25"

    def test_floor_does_not_apply_to_the_first_transport_item(self):
        """航班时刻是工具返回的真实数据，floor 约束的是它之后那一段。"""
        from app.planner import _transport_item

        day = make_day([_transport_item(make_flight(), item_id="d0-out", role="outbound")])

        assert "ARRIVAL_CHAIN_CONFLICT" not in codes(check_feasibility(day, day_start_floor="14:25"))


class TestCheckFeasibilityTransfer:
    def test_transfer_time_shortfall_is_reported(self):
        day = make_day([
            make_item("a", type="attraction", place_id="P1", name="A", start_time="09:00",
                      duration_minutes=60, lat=30.60, lng=104.00),
            make_item("b", type="attraction", place_id="P2", name="B", start_time="09:30",
                      duration_minutes=60, lat=30.70, lng=104.10),
        ])

        issues = check_feasibility(day)

        assert "TRANSFER_TIME_SHORTFALL" in codes(issues)
        assert "ROUTE_UNVERIFIED" in codes(issues)

    def test_real_route_beats_estimate_and_is_verified(self):
        """有高德真实路线时不再猜：不出现 ROUTE_UNVERIFIED，且到达时间按真实耗时算。"""
        day = make_day([
            make_item("a", type="attraction", place_id="A", name="A", start_time="09:00",
                      duration_minutes=60, lat=30.60, lng=104.00),
            make_item("b", type="attraction", place_id="B", name="B", start_time="10:00",
                      duration_minutes=60, lat=30.70, lng=104.10),
        ])
        route = RouteOption(origin_place_id="A", destination_place_id="B",
                            distance_meters=12000, duration_seconds=3000, provider="amap")

        issues = check_feasibility(day, {("A", "B"): route})

        assert "ROUTE_UNVERIFIED" not in codes(issues)
        shortfall = next(issue for issue in issues if issue.code == "TRANSFER_TIME_SHORTFALL")
        assert shortfall.revised_start == "11:10"  # 10:00 + 10 缓冲 + 50 分钟真实车程 + 10 到达缓冲

    def test_missing_start_time_is_filled_from_the_previous_leg(self):
        day = make_day([make_item("a", type="attraction", place_id="P1", name="没定时间")])

        issues = check_feasibility(day)

        assert "MISSING_START_TIME" in codes(issues)
        assert next(i for i in issues if i.code == "MISSING_START_TIME").revised_start == minutes_to_clock(DAY_START_MINUTES)


class TestCheckFeasibilityWindowAndReturn:
    def test_last_day_return_departure_conflict(self):
        day = make_day([
            make_item("a", type="attraction", place_id="P1", name="A", start_time="10:00", duration_minutes=120),
            make_item("b", type="attraction", place_id="P2", name="B", start_time="13:30", duration_minutes=120),
        ], day_index=2)

        issues = check_feasibility(day, day_end_ceiling="15:00")

        conflict = next(issue for issue in issues if issue.code == "RETURN_DEPARTURE_CONFLICT")
        assert conflict.severity == "error"
        assert conflict.item_id == "b"

    def test_transport_item_is_exempt_from_the_return_ceiling(self):
        from app.planner import _transport_item

        flight_back = make_flight(flight_no="CA4102",
                                  departure_at=datetime(2026, 10, 3, 20, 0),
                                  arrival_at=datetime(2026, 10, 3, 23, 0))
        day = make_day([_transport_item(flight_back, item_id="d2-in", role="inbound")], day_index=2)

        assert "RETURN_DEPARTURE_CONFLICT" not in codes(check_feasibility(day, day_end_ceiling="15:00"))


class TestCheckFeasibilityOpeningHours:
    def test_opening_time_conflict(self):
        places = {"P1": make_place("P1", "开晚的馆", opening_hours="10:00-18:00")}
        day = make_day([make_item("a", type="attraction", place_id="P1", name="开晚的馆",
                                  start_time="09:00", duration_minutes=60)])

        conflict = next(issue for issue in check_feasibility(day, places=places)
                        if issue.code == "OPENING_TIME_CONFLICT")

        assert conflict.revised_start == "10:00"

    def test_last_entry_missed(self):
        places = {"P1": make_place("P1", "只能早进", opening_hours="10:00-18:00 最晚进入13:00")}
        day = make_day([make_item("a", type="attraction", place_id="P1", name="只能早进",
                                  start_time="14:00", duration_minutes=60)])

        missed = next(issue for issue in check_feasibility(day, places=places)
                      if issue.code == "LAST_ENTRY_MISSED")

        assert missed.severity == "error"
        assert missed.revised_start is None  # 赶不上就是赶不上，不假装能修

    def test_closing_time_overrun(self):
        places = {"P1": make_place("P1", "早关门", opening_hours="09:00-11:00")}
        day = make_day([make_item("a", type="attraction", place_id="P1", name="早关门",
                                  start_time="10:30", duration_minutes=120)])

        assert "CLOSING_TIME_OVERRUN" in codes(check_feasibility(day, places=places))

    def test_closed_day_needs_confirmation(self):
        places = {"P1": make_place("P1", "闭馆日", opening_hours="周一至周日 09:00-17:00 周一闭馆")}
        day = make_day([make_item("a", type="attraction", place_id="P1", name="闭馆日",
                                  start_time="10:00", duration_minutes=60)])

        issue = next(i for i in check_feasibility(day, places=places)
                     if i.code == "OPENING_HOURS_NEEDS_CONFIRMATION")

        assert issue.severity == "warning"


class TestCheckFeasibilityMealsHotelOverload:
    def test_meal_time_oddity_for_late_lunch(self):
        day = make_day([make_item("m", type="food", place_id="F", name="午餐", start_time="10:30")])

        assert "MEAL_TIME_ODDITY" in codes(check_feasibility(day))

    def test_meal_time_oddity_for_early_dinner(self):
        day = make_day([make_item("m", type="food", place_id="F", name="晚餐", start_time="16:30")])

        assert "MEAL_TIME_ODDITY" in codes(check_feasibility(day))

    def test_normal_meal_times_are_clean(self):
        lunch = make_day([make_item("m", type="food", place_id="F", name="午餐", start_time="12:00")])
        dinner = make_day([make_item("m", type="food", place_id="F", name="晚餐", start_time="18:30")])

        assert "MEAL_TIME_ODDITY" not in codes(check_feasibility(lunch))
        assert "MEAL_TIME_ODDITY" not in codes(check_feasibility(dinner))

    def test_hotel_checkin_before_standard_time_warns(self):
        day = make_day([make_item("h", type="hotel", place_id="H", name="酒店",
                                  start_time="11:00", duration_minutes=HOTEL_CHECKIN_MINUTES,
                                  booking_note="check-in")])

        issue = next(i for i in check_feasibility(day) if i.code == "HOTEL_CHECKIN_EARLY")

        assert "14:00" in issue.reason
        assert HOTEL_STANDARD_CHECKIN_MINUTES == 14 * 60

    def test_luggage_storage_note_suppresses_the_warning(self):
        """只有存在明确依据（写明可寄存）时才不告警。"""
        day = make_day([make_item("h", type="hotel", place_id="H", name="酒店",
                                  start_time="11:00", duration_minutes=10,
                                  booking_note="check-in 可寄存行李")])

        assert "HOTEL_CHECKIN_EARLY" not in codes(check_feasibility(day))

    def test_too_many_items_marks_the_day_overloaded(self):
        items = [make_item(f"i{k}", type="attraction", place_id=f"P{k}", name=f"点{k}",
                           start_time="09:00", duration_minutes=120) for k in range(MAX_ITEMS_PER_DAY + 1)]
        day = make_day(items)

        overloaded = [i for i in check_feasibility(day) if i.code == "DAY_OVERLOADED"]

        assert any("停留点" in issue.reason for issue in overloaded)

    def test_too_many_active_minutes_marks_the_day_overloaded(self):
        items = [make_item(f"i{k}", type="attraction", place_id=f"P{k}", name=f"点{k}",
                           start_time="08:30", duration_minutes=300) for k in range(2)]
        day = make_day(items)

        overloaded = [i for i in check_feasibility(day) if i.code == "DAY_OVERLOADED"]

        assert any("有效游览" in issue.reason for issue in overloaded)

    def test_backtracking_is_surfaced_as_an_issue(self):
        day = make_day([
            make_item("a", type="attraction", place_id="A", name="宽窄巷子", start_time="09:00",
                      duration_minutes=60, lat=30.669, lng=104.055),
            make_item("b", type="attraction", place_id="B", name="天府广场", start_time="10:00",
                      duration_minutes=60, lat=30.657, lng=104.065),
            make_item("c", type="attraction", place_id="A", name="宽窄巷子", start_time="11:00",
                      duration_minutes=60, lat=30.669, lng=104.055),
        ])

        issues = [i for i in check_feasibility(day) if i.code == "BACKTRACKING"]

        assert issues and "折返" in issues[0].reason


class TestDetectBacktracking:
    def test_a_b_a_is_detected(self):
        items = [
            make_item("a", place_id="A", name="宽窄巷子", lat=CD_LAT, lng=CD_LNG),
            make_item("b", place_id="B", name="天府广场", lat=30.657, lng=104.065),
            make_item("c", place_id="A", name="宽窄巷子", lat=CD_LAT, lng=CD_LNG),
        ]

        reasons = detect_backtracking(items)

        assert any("同一天回到同一地点" in reason for reason in reasons)

    def test_opposite_long_legs_are_detected(self):
        items = [
            make_item("a", place_id="A", name="a", lat=30.60, lng=104.00),
            make_item("b", place_id="B", name="b", lat=30.70, lng=104.00),
            make_item("c", place_id="C", name="c", lat=30.60, lng=104.00),
        ]

        reasons = "；".join(detect_backtracking(items))

        assert "来回横跨" in reasons
        assert "总移动" in reasons

    def test_clean_route_has_no_reasons(self):
        items = [
            make_item("a", place_id="A", name="a", lat=30.660, lng=104.050),
            make_item("b", place_id="B", name="b", lat=30.662, lng=104.052),
            make_item("c", place_id="C", name="c", lat=30.664, lng=104.054),
        ]

        assert detect_backtracking(items) == []

    def test_items_without_coordinates_are_ignored(self):
        items = [make_item("a", place_id="A", name="a"), make_item("b", place_id="B", name="b")]

        assert detect_backtracking(items) == []
        assert detect_backtracking([]) == []


class TestReviseDay:
    def test_shifts_and_cascades_the_whole_day(self):
        day = make_day([
            make_item("a", type="attraction", place_id="P1", name="A", start_time="14:00", duration_minutes=60),
            make_item("b", type="attraction", place_id="P2", name="B", start_time="15:30", duration_minutes=60),
        ])
        issues = check_feasibility(day, day_start_floor="14:25")

        revised, final = revise_day(day, issues, day_start_floor="14:25")

        assert revised.item_by_id("a").start_time == "14:25"
        assert revised.item_by_id("b").start_time > "14:25"
        # 路线未核实是 warning，修订不会把它"修好"；这里要求的是时间类 error 全部消失。
        assert not [i for i in check_feasibility(revised, day_start_floor="14:25") if i.severity == "error"]
        assert all(issue.resolved for issue in final if issue.code != "ROUTE_UNVERIFIED")

    def test_no_pending_issues_returns_the_original_day(self):
        day = make_day([make_item("a", type="attraction", place_id="P1", name="A", start_time="09:00",
                                  duration_minutes=60)])

        revised, final = revise_day(day, [])

        assert revised is day
        assert final == []

    def test_return_conflict_removes_the_item_and_records_it(self):
        day = make_day([
            make_item("a", type="attraction", place_id="P1", name="A", start_time="10:00", duration_minutes=120),
            make_item("b", type="attraction", place_id="P2", name="B", start_time="13:30", duration_minutes=120),
            make_item("t", type="transport", place_id="TR", name="回程", start_time="18:00",
                      end_time="21:00", transport_mode="flight", duration_minutes=180),
        ], day_index=2)
        issues = check_feasibility(day, day_end_ceiling="15:00")

        revised, final = revise_day(day, issues, day_end_ceiling="15:00")

        assert revised.item_by_id("b") is None
        assert revised.item_by_id("t") is not None  # 返程交通本身必须保留
        assert any("因返程时间不足已移除" in note for note in revised.notes)
        resolve_issue = next(i for i in final if i.code == "RETURN_DEPARTURE_CONFLICT")
        assert resolve_issue.resolved is True
        assert "移除该 item" in resolve_issue.reason

    def test_opening_hours_conflict_removes_the_item_and_records_it(self):
        """营业时间来不及的点必须**从行程里移除**，而不是"报个错但留着"。

        早先的实现把 LAST_ENTRY_MISSED / CLOSING_TIME_OVERRUN 保持未解决，理由是
        "不假装修好了"。但用户拿到的 plan 里因此会出现"17:51 去一个 18:00 关门的祠堂"
        这种条目 —— 我们自己的检查判它是 error，行程里却照样写着。真实数据里寺庙
        18:00 关门很常见，历史 run 每一次都留下了这类未解决冲突，所以改成
        "排不下就不排"，并如实说明为什么少了一个点。
        """
        places = {"P1": make_place("P1", "只能早进", opening_hours="10:00-18:00 最晚进入13:00")}
        day = make_day([make_item("a", type="attraction", place_id="P1", name="只能早进",
                                  start_time="14:00", duration_minutes=60)])
        issues = check_feasibility(day, places=places)

        revised, final = revise_day(day, issues, places=places)

        # 1) 行程里不再有那个安排不下的点；
        assert revised.item_by_id("a") is None
        # 2) 重新检查不会再有这个冲突（这才是"修好了"的证明）；
        assert "LAST_ENTRY_MISSED" not in codes(check_feasibility(revised, places=places))
        # 3) 决策链里如实记下修订动作与原因，而不是静默消失。
        issue = next(i for i in final if i.code == "LAST_ENTRY_MISSED")
        assert issue.resolved is True
        assert "已从当天移除" in issue.reason
        assert any("因营业时间内安排不下已移除" in note for note in revised.notes)
        # 4) 一天的点全被移除时给一条机动时间，而不是渲染成空白卡片。
        assert any(item.type == "free_time" for item in revised.items)

    def test_revision_records_original_and_revised_start(self):
        day = make_day([make_item("a", type="attraction", place_id="P1", name="A",
                                  start_time="14:00", duration_minutes=60)])
        issues = check_feasibility(day, day_start_floor="14:25")

        _, final = revise_day(day, issues, day_start_floor="14:25")
        issue = final[0]

        assert issue.original_start == "14:00"
        assert issue.revised_start == "14:25"
        assert issue.resolved is True

    def test_travel_legs_are_recomputed_after_revision(self):
        day = make_day([
            make_item("a", type="attraction", place_id="P1", name="A", start_time="09:00",
                      duration_minutes=60, lat=30.60, lng=104.00),
            make_item("b", type="attraction", place_id="P2", name="B", start_time="09:30",
                      duration_minutes=60, lat=30.61, lng=104.01),
        ])

        revised, _ = revise_day(day, check_feasibility(day))

        assert revised.items[1].travel_from_previous is not None
        assert revised.items[1].start_time > revised.items[0].end_time


class TestApplyFeasibilityPass:
    def test_floor_and_ceiling_are_applied_to_the_right_days(self):
        day0 = make_day([make_item("x", type="attraction", place_id="P1", name="X",
                                   start_time="14:00", duration_minutes=60)], day_index=0)
        day2 = make_day([make_item("y", type="attraction", place_id="P2", name="Y",
                                   start_time="10:00", duration_minutes=600)], day_index=2)

        days, issues = apply_feasibility_pass([day0, day2], first_day_start="14:25",
                                              last_day_deadline="15:00")

        assert days[0].item_by_id("x").start_time == "14:25"
        assert "RETURN_DEPARTURE_CONFLICT" in codes(issues)
        assert [d.day_index for d in days] == [0, 2]

    def test_returns_one_day_per_input(self):
        days, issues = apply_feasibility_pass(
            [make_day([], day_index=i) for i in range(3)]
        )

        assert len(days) == 3
        assert issues == []


# ==================================================
# 十、聚类 / 排序 / 候选打分（PRD §21、§24）
# ==================================================


class TestClusterPlaces:
    def test_nearby_places_share_a_cluster(self):
        places = [
            make_place_at("a", "a", 30.660, 104.050),
            make_place_at("b", "b", 30.662, 104.052),
            make_place_at("c", "c", 30.700, 104.050),
        ]

        clusters = cluster_places(places)

        assert [[p.place_id for p in cluster] for cluster in clusters] == [["a", "b"], ["c"]]

    def test_places_without_coordinates_get_their_own_cluster(self):
        """位置未知就不该被塞进任何区域，否则"顺路"就是编出来的。"""
        places = [
            make_place_at("a", "a", 30.660, 104.050),
            make_place_at("b", "b", 30.661, 104.051),
            make_place("c", "没有坐标"),
        ]

        clusters = cluster_places(places)

        assert ["c"] in [[p.place_id for p in cluster] for cluster in clusters]

    def test_threshold_is_respected(self):
        places = [make_place_at("a", "a", 30.660, 104.050), make_place_at("b", "b", 30.662, 104.050)]

        assert len(cluster_places(places, max_cluster_km=DEFAULT_CLUSTER_KM)) == 1
        assert len(cluster_places(places, max_cluster_km=0.1)) == 2

    def test_ordering_is_deterministic(self):
        """同样大小按首元素名字排序，保证同一输入永远同一输出。"""
        clusters = cluster_places([make_place("b", "b"), make_place("a", "a")])

        assert [cluster[0].name for cluster in clusters] == ["a", "b"]


class TestOrderByProximity:
    def test_nearest_neighbour_from_anchor(self):
        places = [
            make_place_at("far", "far", 30.90, 104.30),
            make_place_at("near", "near", 30.660, 104.050),
        ]

        ordered = order_by_proximity(places, (30.660, 104.050))

        assert [p.place_id for p in ordered] == ["near", "far"]

    def test_coordinateless_places_come_last(self):
        places = [make_place("none", "没有坐标"), make_place_at("near", "near", 30.660, 104.050)]

        ordered = order_by_proximity(places, (30.660, 104.050))

        assert [p.place_id for p in ordered] == ["near", "none"]

    def test_without_anchor_falls_back_to_name_order(self):
        places = [make_place("b", "b"), make_place("a", "a")]

        assert [p.place_id for p in order_by_proximity(places, None)] == ["a", "b"]


class TestScoreCandidate:
    def test_formula_matches_constants(self):
        place = make_place("P1", "成都火锅", type="餐饮")

        score, detail = score_candidate(place, 90.0, 10.0, ["美食"], 0.8)

        assert score == pytest.approx(0.55 * 90 - 0.45 * 10 + 12 + 15 * 0.8)
        assert detail["preference_hits"] == ["美食"]
        assert detail["trust_component"] == pytest.approx(49.5)
        assert detail["ad_risk_component"] == pytest.approx(-4.5)
        assert detail["preference_bonus"] == pytest.approx(12.0)
        assert detail["route_fit_bonus"] == pytest.approx(12.0)

    def test_high_ad_risk_can_push_a_candidate_negative(self):
        """Ad Risk 高不等于删除，但风险盖过证据时会被挤到门槛以下。"""
        score, detail = score_candidate(make_place(), 40.0, 80.0, [], 0.0)

        assert score < 0.0
        assert detail["preference_hits"] == []

    def test_preference_bonus_is_capped(self):
        place = make_place(name="宽窄巷子历史文化步行街", type="公园")

        _, detail = score_candidate(place, 60.0, 20.0, ["历史文化", "自然", "购物"], 0.5)

        assert len(detail["preference_hits"]) == 3
        assert detail["preference_bonus"] == pytest.approx(24.0)

    def test_route_fit_is_clamped_to_unit_interval(self):
        _, above = score_candidate(make_place(), 50.0, 10.0, [], 5.0)
        _, below = score_candidate(make_place(), 50.0, 10.0, [], -3.0)

        assert above["route_fit"] == 1.0
        assert below["route_fit"] == 0.0

    def test_basis_text_is_human_readable(self):
        _, detail = score_candidate(make_place(), 90.0, 10.0, ["美食"], 0.8)

        assert "Trust 90" in detail["basis"]
        assert "AdRisk 10" in detail["basis"]


class TestPreferenceHits:
    def test_keyword_table_matches_name_and_type(self):
        assert preference_hits(make_place(name="成都火锅", type="餐饮"), ["美食"]) == ["美食"]
        assert preference_hits(make_place(name="武侯祠", type="风景名胜"), ["历史文化"]) == ["历史文化"]

    def test_miss_returns_empty(self):
        assert preference_hits(make_place(name="武侯祠", type="风景名胜"), ["购物"]) == []

    def test_custom_preference_uses_substring_match_without_fuzzy_credit(self):
        """自定义偏好直接子串匹配：匹配不到就是没命中，不给"大概符合"的分。"""
        assert preference_hits(make_place(name="成都大熊猫繁育研究基地"), ["大熊猫"]) == ["大熊猫"]
        assert preference_hits(make_place(name="熊猫基地"), ["大熊猫"]) == []


# ==================================================
# 十一、初始计划生成（PRD §21）
# ==================================================


def plan_fixture_places() -> list[Place]:
    """一组合成的成都候选点（有坐标、有行政区，用于初始计划测试）。"""
    return [
        make_place("P1", "宽窄巷子", district="青羊区", lat=30.6691, lng=104.0555,
                   type="风景名胜", opening_hours="09:00-22:00", expected_duration_minutes=120),
        make_place("P2", "人民公园", district="青羊区", lat=30.6612, lng=104.0560,
                   type="公园", opening_hours="06:00-22:00"),
        make_place("P3", "武侯祠", district="武侯区", lat=30.6460, lng=104.0360,
                   type="风景名胜", opening_hours="08:00-18:00"),
        make_place("P4", "龙抄手总店", district="锦江区", lat=30.6570, lng=104.0800, type="餐饮"),
        make_place("P5", "陈麻婆豆腐", district="青羊区", lat=30.6700, lng=104.0600, type="餐饮"),
    ]


PLAN_TRUST = {"P1": 88.0, "P2": 70.0, "P3": 75.0, "P4": 65.0, "P5": 62.0}
PLAN_RISK = {"P1": 5.0, "P2": 10.0, "P3": 15.0, "P4": 20.0, "P5": 18.0}


class TestBuildInitialPlan:
    def test_returns_exactly_intent_days(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=3, travelers=2)

        days = build_initial_plan(intent, plan_fixture_places())

        assert len(days) == 3
        assert [d.day_index for d in days] == [0, 1, 2]
        assert days[0].date == BASE_DATE
        assert days[2].date == date(2026, 10, 3)

    def test_first_day_starts_from_the_real_arrival_time(self):
        """有去程航班时，第一天的停留安排必须从抵达链推算，而不是套一个 08:30 的常规开门时间。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)
        flight = make_flight()

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=flight, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        first = days[0].items[0]
        assert first.type == "transport"
        assert (first.start_time, first.end_time) == ("08:30", "12:10")
        checkin = next(item for item in days[0].items if item.type == "hotel")
        landing = flight.arrival_at.hour * 60 + flight.arrival_at.minute
        assert checkin.start_minutes > landing
        assert checkin.start_minutes >= landing + AIRPORT_BAGGAGE_EXIT_MINUTES
        assert checkin.start_minutes != DAY_START_MINUTES
        # 后面所有停留点都排在落地之后，不会被"09:00 开门"这种默认时间拉到中午之前。
        for item in days[0].items[1:]:
            assert item.start_minutes is None or item.start_minutes >= landing

    def test_without_transport_the_day_starts_at_the_normal_floor(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)

        days = build_initial_plan(intent, plan_fixture_places(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK)

        assert days[0].items[0].start_time == minutes_to_clock(DAY_START_MINUTES)

    def test_all_items_stay_inside_the_day_window(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)
        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=make_flight(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK)

        for day in days:
            for item in day.items:
                assert item.start_time is not None
                assert item.end_minutes is not None
                assert item.end_minutes <= DAY_END_MINUTES

    def test_last_day_respects_the_return_deadline(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)
        inbound = make_flight(flight_no="CA4102",
                              departure_at=datetime(2026, 10, 2, 20, 0),
                              arrival_at=datetime(2026, 10, 2, 23, 0))

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  inbound=inbound, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        deadline = departure_deadline_minutes(inbound)
        for item in days[-1].items:
            if item.type == "transport":
                continue
            assert item.end_minutes <= deadline

    def test_meals_are_placed_in_lunch_and_dinner_windows(self):
        """餐厅不能排在下午三点还硬说"合理"。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=make_flight(), inbound=make_flight(
                                      flight_no="CA4102",
                                      departure_at=datetime(2026, 10, 2, 20, 0),
                                      arrival_at=datetime(2026, 10, 2, 23, 0)),
                                  trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        foods = [item for day in days for item in day.items if item.type == "food"]
        assert foods
        for item in foods:
            in_lunch = LUNCH_WINDOW[0] <= item.start_minutes <= LUNCH_WINDOW[1]
            in_dinner = DINNER_WINDOW[0] <= item.start_minutes <= DINNER_WINDOW[1]
            assert in_lunch or in_dinner

    def test_hotel_checkin_and_checkout_items_are_inserted(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        assert any(item.booking_note == "check-in" for item in days[0].items)
        assert any(item.booking_note == "check-out" for item in days[-1].items)

    def test_area_is_the_dominant_district(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)

        days = build_initial_plan(intent, plan_fixture_places(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK)

        assert days[0].area == "青羊区"

    def test_rejected_candidates_are_recorded_in_notes(self):
        """证据不足以支撑、风险又高于收益的候选不排进计划，但必须如实写进 notes。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        bad = make_place("BAD", "网红店", district="锦江区", type="餐饮")

        days = build_initial_plan(intent, [bad], trust_scores={"BAD": 10.0}, ad_risks={"BAD": 95.0})

        assert any("未纳入候选" in note for note in days[0].notes)

    def test_no_candidates_becomes_free_time_not_fabricated_items(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2)

        days = build_initial_plan(intent, [])

        assert len(days) == 2
        for day in days:
            assert [item.type for item in day.items] == ["free_time"]
            assert "不编造行程" in day.items[0].reason

    def test_poi_without_any_evidence_is_still_scheduled(self):
        """只有高德 POI、没有任何证据的地点仍要排进行程。

        它的 Ad Risk 走真实的 `ad_risk(place, [])`：曾经那里把"没有证据文本"折算成
        15 + 4 分风险，于是每个 POI-only 候选的综合分都是负的、被整批剔除，行程空掉。
        """
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        place = make_place("P1", "宽窄巷子", district="青羊区", type="风景名胜")
        risk, _ = ad_risk(place, [])

        days = build_initial_plan(
            intent, [place], trust_scores={"P1": 8.0}, ad_risks={"P1": risk}
        )

        assert risk == 0.0
        assert [item.place_id for item in days[0].items if item.place_id] == ["P1"]

    def test_trust_and_ad_risk_are_carried_into_items(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)

        days = build_initial_plan(intent, plan_fixture_places(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK)

        item = next(i for i in days[0].items if i.place_id == "P1")
        assert item.trust_score == pytest.approx(88.0)
        assert item.ad_risk == pytest.approx(5.0)
        assert "候选得分" in item.reason

    def test_realtime_ticket_price_is_attached(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)

        days = build_initial_plan(intent, plan_fixture_places(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK, ticket_prices={"P1": 50.0})

        item = next(i for i in days[0].items if i.place_id == "P1")
        assert item.price == pytest.approx(50.0)
        assert item.price_type == "realtime"

    def test_evidence_ids_are_carried_for_auditability(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        evidences = {"P1": [make_evidence("ev-1"), make_evidence("ev-2")]}

        days = build_initial_plan(intent, plan_fixture_places(), trust_scores=PLAN_TRUST,
                                  ad_risks=PLAN_RISK, evidences=evidences)

        item = next(i for i in days[0].items if i.place_id == "P1")
        assert item.evidence_ids == ["ev-1", "ev-2"]

    def test_overflow_is_carried_to_the_next_day(self):
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2)
        many = [make_place(f"M{i}", f"点{i}", district="成华区", lat=30.60 + i * 0.0001,
                           lng=104.05, type="风景名胜") for i in range(MAX_ITEMS_PER_DAY + 4)]

        days = build_initial_plan(intent, many)

        assert len([i for i in days[0].items if i.type != "free_time"]) == MAX_ITEMS_PER_DAY
        assert any(i.place_id for i in days[1].items)

    def test_area_falls_back_to_business_area(self):
        """item 的 area 取 district，缺 district 时退到商圈（高德 POI 的 business_area）。

        高德对不少 POI 只给 business_area 不给 district，所以两者都可能是 None。
        两者都没有的 item 会继承当天的区域（PRD §21"每天一个主要区域"），
        所以它不应该崩，而应该等于 day.area。
        """
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        with_district = make_place("P1", "有行政区的点", district="武侯区",
                                   business_area="高升桥", lat=30.66, lng=104.05,
                                   type="风景名胜")
        business_only = make_place("P2", "只有商圈的点", district=None,
                                   business_area="高升桥", lat=30.66, lng=104.05,
                                   type="风景名胜")
        neither = make_place("P3", "两者都缺的点", district=None, business_area=None,
                             lat=30.66, lng=104.05, type="风景名胜")

        days = build_initial_plan(intent, [with_district, business_only, neither])

        by_place = {item.place_id: item for day in days for item in day.items if item.place_id}
        assert by_place["P1"].area == "武侯区"
        assert by_place["P2"].area == "高升桥"
        # P3 自己没有区域信息 → 继承当天主区域（这里由 P1 的district 决定）
        assert by_place["P3"].area == days[0].area

    def test_area_falls_back_to_city_then_none(self):
        """区域标签的三级降级：district → business_area → 当天城市名 → None。

        没有任何点给出行政区的日子，区域退到城市名（"成都"）；连城市都没有才是 None。
        这一条把三级都钉住，避免后人把"退到城市"误当成"编了一个区域"。
        """
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        places = [
            make_place(f"Q{index}", f"无区域点{index}", district=None, business_area=None,
                       lat=30.66 + index * 0.0001, lng=104.05, type="风景名胜")
            for index in range(3)
        ]

        days = build_initial_plan(intent, places)

        assert days[0].area == "成都"
        assert all(item.area == "成都" for item in days[0].items if item.place_id)

    def test_area_is_none_when_city_is_unknown_too(self):
        """连 city 都没有时区域是 None —— 不编一个地名。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=1)
        places = [
            make_place(f"Z{index}", f"无任何位置字段{index}", city=None, district=None,
                       business_area=None, lat=30.66 + index * 0.0001, lng=104.05,
                       type="风景名胜")
            for index in range(2)
        ]

        days = build_initial_plan(intent, places)

        assert days[0].area is None
        assert all(item.area is None for item in days[0].items if item.place_id)

    def test_plan_survives_place_without_district_or_business_area(self):
        """缺 district 与 business_area 的候选点不能让整次规划崩掉（曾经的 AttributeError）。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2)
        places = [
            make_place(f"Q{index}", f"无区域点{index}", district=None, business_area=None,
                       lat=30.66 + index * 0.0001, lng=104.05, type="风景名胜")
            for index in range(3)
        ]

        days = build_initial_plan(intent, places)

        assert len(days) == 2
        assert any(item.place_id for day in days for item in day.items)


# ==================================================
# 十二、Critic（PRD §25、§32）
# ==================================================


def critique_fixture(**overrides) -> TripPlan:
    """合成 TripPlan：一天一个景点 item，便于逐条添加"问题 / 风险"再断言决策。"""
    item = make_item("i1", type="attraction", place_id="P1", name="宽窄巷子", start_time="09:00",
                     end_time="11:00", duration_minutes=120, trust_score=85.0, ad_risk=5.0)
    data: dict = {"days": [make_day([item])]}
    data.update(overrides)
    return make_plan(**data)


class TestCritiqueFeasibility:
    def test_unresolved_error_is_rejected(self):
        issue = FeasibilityIssue(day_index=0, item_id="i1", severity="error",
                                 code="LAST_ENTRY_MISSED", reason="最晚进入已过")
        plan = critique_fixture(warnings=[issue])

        decisions = critique(plan)

        reject = next(d for d in decisions if d.entity_id == "i1" and d.status == DecisionStatus.REJECT)
        assert "FEASIBILITY_ERROR" in reject.reason_codes
        assert "LAST_ENTRY_MISSED" in reject.reason_codes
        assert reject.reason_text

    def test_resolved_issue_passes_and_records_the_revision(self):
        issue = FeasibilityIssue(day_index=0, item_id="i1", severity="error",
                                 code="TRANSFER_TIME_SHORTFALL", reason="时间不够",
                                 original_start="14:00", revised_start="14:25", resolved=True)

        decisions = critique(critique_fixture(warnings=[issue]))

        passed = next(d for d in decisions if d.reason_codes[0] == "AUTO_REVISED")
        assert passed.status == DecisionStatus.PASS
        assert passed.scores["original_start"] == "14:00"
        assert passed.scores["revised_start"] == "14:25"

    def test_unresolved_error_lowers_plan_confidence_and_status(self):
        issue = FeasibilityIssue(day_index=0, item_id="i1", severity="error",
                                 code="CLOSING_TIME_OVERRUN", reason="超时")

        decisions = critique(critique_fixture(warnings=[issue]))
        summary = next(d for d in decisions if d.entity_id == "run-test")

        assert summary.status == DecisionStatus.REVISION
        assert "HAS_UNRESOLVED_ERROR" in summary.reason_codes
        assert summary.scores["confidence"] == pytest.approx(0.95 - 0.2)


class TestCritiqueItemRisk:
    def test_high_ad_risk_low_trust_item_needs_revision(self):
        item = make_item("i1", type="attraction", place_id="P1", name="网红店",
                         ad_risk=85.0, trust_score=30.0)

        decisions = critique(critique_fixture(days=[make_day([item])]))

        revision = next(d for d in decisions if d.status == DecisionStatus.REVISION)
        assert "HIGH_AD_RISK" in revision.reason_codes
        assert "LOW_TRUST" in revision.reason_codes
        assert revision.scores["ad_risk"] >= AD_RISK_HIGH

    def test_low_risk_item_is_not_flagged(self):
        item = make_item("i1", type="attraction", place_id="P1", name="博物馆",
                         ad_risk=20.0, trust_score=80.0)

        decisions = critique(critique_fixture(days=[make_day([item])]))

        assert not any(d.status == DecisionStatus.REVISION for d in decisions)

    def test_high_ad_risk_but_high_trust_is_kept_for_manual_check_only(self):
        """Ad Risk 高不等于删除：Trust 过线时只给 SUGGEST_DROP 的修订建议。"""
        item = make_item("i1", type="attraction", place_id="P1", name="网红店",
                         ad_risk=85.0, trust_score=60.0)

        decisions = critique(critique_fixture(days=[make_day([item])]))

        assert not any(d.status == DecisionStatus.REVISION for d in decisions)

    def test_unverified_amap_route_needs_revision(self):
        item = make_item("i1", type="attraction", place_id="P1", name="宽窄巷子",
                         travel_from_previous=TravelLeg.from_route(None, mode="transit", fallback_minutes=15))

        decisions = critique(critique_fixture(days=[make_day([item])]))

        revision = next(d for d in decisions if d.status == DecisionStatus.REVISION)
        assert "ROUTE_UNVERIFIED" in revision.reason_codes
        assert "CONFIDENCE_REDUCED" in revision.reason_codes
        assert revision.scores["verified"] is False

    def test_verified_route_is_not_criticised(self):
        item = make_item("i1", type="attraction", place_id="P1", name="宽窄巷子",
                         travel_from_previous=TravelLeg(mode="transit", duration_seconds=900,
                                                        estimated_cost=4.0, verified=True))

        decisions = critique(critique_fixture(days=[make_day([item])]))

        assert not any("ROUTE_UNVERIFIED" in d.reason_codes for d in decisions)


class TestCritiqueBudget:
    def test_over_budget_is_rejected_with_suggestions(self):
        budget = BudgetSummary(budget_total=5000.0, known_real_cost=5000.0, estimated_cost=1000.0,
                               projected_total=6000.0, remaining=-1000.0, status="over_budget",
                               optimization_suggestions=["酒店换至「便宜酒店」可省约 ¥600"])

        decisions = critique(critique_fixture(budget=budget))

        reject = next(d for d in decisions if d.reason_codes[0] == "OVER_BUDGET")
        assert reject.status == DecisionStatus.REJECT
        assert "便宜酒店" in reject.reason_text
        assert reject.scores["budget_total"] == 5000.0

    def test_unknown_budget_is_reported_as_pass(self):
        decisions = critique(critique_fixture(budget=BudgetSummary(status="unknown", projected_total=1234.0)))

        passed = next(d for d in decisions if d.reason_codes[0] == "BUDGET_UNKNOWN")
        assert passed.status == DecisionStatus.PASS
        assert "1,234" in passed.reason_text

    def test_over_budget_lowers_confidence(self):
        budget = BudgetSummary(budget_total=100.0, projected_total=200.0, remaining=-100.0,
                               status="over_budget")

        summary = next(d for d in critique(critique_fixture(budget=budget)) if d.entity_id == "run-test")

        assert summary.scores["confidence"] == pytest.approx(0.95 - 0.1)


class TestCritiqueEvidence:
    def test_used_and_unused_evidence_are_both_reported(self):
        used_item = make_item("i1", type="attraction", place_id="P1", name="A", evidence_ids=["e-used"])
        unused = make_evidence("e-unused")
        plan = critique_fixture(days=[make_day([used_item])])

        decisions = critique(plan, evidences=[make_evidence("e-used"), unused])

        assert next(d for d in decisions if d.entity_id == "e-used").status == DecisionStatus.USED
        not_used = next(d for d in decisions if d.entity_id == "e-unused")
        assert not_used.status == DecisionStatus.NOT_USED
        assert not_used.scores["provider"] == "xiaohongshu"


class TestCritiqueConfidence:
    def test_clean_plan_has_full_confidence(self):
        summary = next(d for d in critique(critique_fixture()) if d.entity_id == "run-test")

        assert summary.status == DecisionStatus.PASS
        assert summary.scores["confidence"] == pytest.approx(0.95)

    def test_degraded_amap_lowers_confidence(self):
        decisions = critique(critique_fixture(), amap_status="DEGRADED")

        summary = next(d for d in decisions if d.entity_id == "run-test")
        assert "AMAP_DEGRADED" in summary.reason_codes
        assert summary.scores["confidence"] == pytest.approx(0.95 - 0.3)
        assert "高德状态 DEGRADED" in summary.reason_text

    def test_unverified_legs_reduce_confidence(self):
        item = make_item("i1", type="attraction", place_id="P1", name="A",
                         travel_from_previous=TravelLeg.from_route(None, fallback_minutes=15))

        summary = next(d for d in critique(critique_fixture(days=[make_day([item])]))
                       if d.entity_id == "run-test")

        assert summary.scores["unverified_legs"] == 1
        assert summary.scores["confidence"] == pytest.approx(0.95 - 0.15)
        assert "HAS_UNVERIFIED_ROUTE" in summary.reason_codes

    def test_confidence_penalties_stack(self):
        item = make_item("i1", type="attraction", place_id="P1", name="A",
                         travel_from_previous=TravelLeg.from_route(None, fallback_minutes=15))
        budget = BudgetSummary(budget_total=100.0, projected_total=200.0, remaining=-100.0,
                               status="over_budget")

        summary = next(d for d in critique(critique_fixture(days=[make_day([item])], budget=budget),
                                           amap_status="ERROR") if d.entity_id == "run-test")

        assert summary.scores["confidence"] == pytest.approx(0.95 - 0.15 - 0.1 - 0.3)

    def test_confidence_never_drops_below_floor(self):
        items = [make_item(f"i{k}", type="attraction", place_id=f"P{k}", name=f"A{k}",
                           travel_from_previous=TravelLeg.from_route(None, fallback_minutes=15))
                 for k in range(6)]
        budget = BudgetSummary(budget_total=0.0, projected_total=100.0, remaining=-100.0,
                               status="over_budget")

        summary = next(d for d in critique(critique_fixture(days=[make_day(items)], budget=budget),
                                           amap_status="ERROR") if d.entity_id == "run-test")

        assert summary.scores["confidence"] == pytest.approx(0.05)


class TestCritiqueAuditability:
    def test_every_decision_has_codes_and_text(self):
        issue = FeasibilityIssue(day_index=0, item_id="i1", severity="error", code="LAST_ENTRY_MISSED",
                                 reason="赶不上")

        for decision in critique(critique_fixture(warnings=[issue]),
                                 evidences=[make_evidence("e-unused")]):
            assert decision.reason_codes
            assert decision.reason_text
            assert decision.agent_or_stage == "critic"

    def test_stage_can_be_overridden(self):
        decisions = critique(critique_fixture(), stage="critic_pass_2")

        assert all(d.agent_or_stage == "critic_pass_2" for d in decisions)

    def test_existing_decisions_are_not_duplicated(self):
        """幂等：把上一轮的决策传回来，同一 (entity_id, code) 不再产出。"""
        first = critique(critique_fixture())
        second = critique(critique_fixture(decisions=first), first)

        assert len(first) > 0
        assert second == []

    def test_only_new_findings_are_returned_when_partially_seen(self):
        first = critique(critique_fixture())
        new_issue = FeasibilityIssue(day_index=0, item_id="i2", severity="error",
                                     code="RETURN_DEPARTURE_CONFLICT", reason="赶不上返程")
        plan = critique_fixture(decisions=first, warnings=[new_issue])

        second = critique(plan, first)

        assert decision_codes(second) == {"FEASIBILITY_ERROR"}


# ======================================================================
# 跨午夜 / 跨天去程：抵达日才算第一天（真机回归：长春→成都 K548 46h38m）
# ======================================================================


class TestCrossMidnightArrival:
    """去程落在次日/多日之后时，行程必须从**抵达日**开始。

    真实事故：北京 10-01 21:30 起飞的 MU6649 次日 00:15 落地，旧实现把"抵达链"
    套在出发日上，排出「10-01 08:30 在成都逛景点、当晚 21:30 才起飞」；
    长春 10-01 22:57 发的 K548 要开到 10-03 21:35，旧实现把首日推到 22:40 起步，
    一路排到次日 06:50（博物馆凌晨 1:35 开门）。
    """

    def test_arrival_day_index_is_zero_without_a_late_arrival(self):
        assert arrival_day_index(make_flight(), BASE_DATE, 3) == 0
        assert arrival_day_index(None, BASE_DATE, 3) == 0

    def test_arrival_day_index_counts_the_real_arrival_date(self):
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))
        slow_train = make_train(train_no="K548",
                                departure_at=datetime(2026, 10, 1, 22, 57),
                                arrival_at=datetime(2026, 10, 3, 21, 35))

        assert arrival_day_index(red_eye, BASE_DATE, 5) == 1
        assert arrival_day_index(slow_train, BASE_DATE, 6) == 2

    def test_arrival_day_index_is_clamped_to_the_trip(self):
        """抵达日超出天数时夹到最后一天，并让 arrival_beyond_trip 去报警。"""
        slow_train = make_train(departure_at=datetime(2026, 10, 1, 22, 57),
                                arrival_at=datetime(2026, 10, 3, 21, 35))

        assert arrival_day_index(slow_train, BASE_DATE, 2) == 1
        assert arrival_beyond_trip(slow_train, BASE_DATE, 2) is True
        assert arrival_beyond_trip(slow_train, BASE_DATE, 6) is False

    def test_no_sightseeing_before_the_traveller_lands(self):
        """红眼航班：第 0 天只能是"在途 + 起飞"，不能出现目的地景点。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=3, travelers=2)
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=red_eye, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        # 出发日：只有去程交通，没有任何停留点。
        assert [item.type for item in days[0].items] == ["transport"]
        assert days[0].items[0].start_time == "21:30"
        assert days[0].notes and "在途" in days[0].notes[0]
        # 抵达日：从抵达链之后开始，且酒店入住挂在这一天。
        assert days[1].date == date(2026, 10, 2)
        checkin = next(item for item in days[1].items if item.type == "hotel")
        assert checkin.start_minutes >= 0 * 60 + 15 + AIRPORT_BAGGAGE_EXIT_MINUTES
        # 出发日不允许出现任何游玩点。
        for day in days[:1]:
            assert not [item for item in day.items if item.type in {"activity", "attraction", "food"}]

    def test_a_late_arrival_day_holds_only_the_hotel_checkin(self):
        """深夜抵达当天不该再排景点：窗口顺延到入住为止，且绝不跨过 24:00。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=6, travelers=2)
        slow_train = make_train(train_no="K548",
                                departure_at=datetime(2026, 10, 1, 22, 57),
                                arrival_at=datetime(2026, 10, 3, 21, 35))

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=slow_train, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        assert [item.type for item in days[0].items] == ["transport"]
        # 第 1 天（10-02）整日在路上：既不排点，也不办入住（人还没到），只有一条"在途"说明。
        transit = [item for item in days[1].items if item.type != "hotel"]
        assert len(transit) == 1
        assert transit[0].name == "在途（交通日）"
        assert not any(item.type == "hotel" for item in days[1].items)
        assert days[1].notes and "在途" in days[1].notes[0]
        arrival_day = days[2]
        assert arrival_day.date == date(2026, 10, 3)
        for item in arrival_day.items:
            if item.type == "transport":
                continue
            assert item.type == "hotel", f"{item.name} 不该出现在深夜抵达日"
        # 全行程都不允许出现 24:00 之后的时间。
        for day in days:
            for item in day.items:
                if item.type == "transport":
                    continue
                assert item.end_minutes is not None
                assert item.end_minutes <= MAX_DAY_MINUTES

    def test_late_arrival_window_extends_but_never_past_midnight(self):
        """窗口放宽的边界：22:40 抵达 → 最晚到 24:00，不会出现 25:30。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=red_eye, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        for day in days:
            for item in day.items:
                if item.type == "transport":
                    continue
                assert item.end_minutes <= MAX_DAY_MINUTES

    def test_feasibility_floor_belongs_to_the_arrival_day(self):
        """apply_feasibility_pass 的 floor 只能套在抵达日：套错天就是把整天推到深夜。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=3, travelers=2)
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))
        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=red_eye, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)
        floor = arrival_ready_minutes(red_eye, with_checkin=True)

        revised, _issues = apply_feasibility_pass(
            days, {}, intent, places={}, first_day_start=floor, first_day_index=1
        )

        # 出发日（第 0 天）只有交通段，且没有被 floor 推到深夜。
        assert [item.type for item in revised[0].items] == ["transport"]
        assert revised[0].items[0].start_time == "21:30"
        # 抵达日的活动都排在抵达链之后。
        for item in revised[1].items:
            if item.type == "transport":
                continue
            assert item.start_minutes is None or item.start_minutes >= floor - HOTEL_CHECKIN_MINUTES

    def test_same_day_arrival_is_unchanged(self):
        """同日抵达（默认 fixture）语义不变：第一天仍有交通段 + 抵达链 floor。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=2, travelers=2)

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=make_flight(), trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        assert days[0].items[0].type == "transport"
        assert all("在途" not in note for note in days[0].notes)
        assert any(item.type == "hotel" for item in days[0].items)


class TestArrivalDayStart:
    """抵达日**起排时刻**：物理上最早能开始 ≠ 应当从那时开始排。

    `arrival_ready_minutes` 回答"几点能出门"，是事实；把它直接当成当天的排程 floor
    就是缺陷：红眼航班 00:15 落地 → 02:20 入住，旧实现排出「02:20 入住、
    03:15 夜景打卡、05:06 吃晚饭」—— 那时旅客在睡觉，景点和餐馆也没开门。
    这组用例锁住"落在起床时刻之前的抵达一律从 DAY_START_MINUTES 起排"，同时锁住
    "正常时刻抵达的语义完全不变"，以及"原始抵达时刻没有被抹掉"。
    """

    def test_red_eye_arrival_starts_the_day_at_a_human_hour(self):
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        assert arrival_ready_minutes(red_eye) == 2 * 60 + 20
        assert arrival_day_start_minutes(red_eye) == DAY_START_MINUTES

    def test_a_daytime_arrival_keeps_the_real_arrival_chain(self):
        """12:10 落地 → 14:15 可出门，晚于 08:30，必须原样保留，不能被压回上午。"""
        flight = make_flight(arrival_at=datetime(2026, 10, 1, 12, 10))

        ready = arrival_ready_minutes(flight)
        assert ready == 14 * 60 + 15
        assert arrival_day_start_minutes(flight) == ready

    def test_an_early_morning_arrival_is_clamped_too(self):
        """05:00 落地 → 07:05 到酒店，仍在起床之前，同样从 08:30 起排。"""
        early = make_flight(arrival_at=datetime(2026, 10, 1, 5, 0))

        assert arrival_ready_minutes(early) == 7 * 60 + 5
        assert arrival_day_start_minutes(early) == DAY_START_MINUTES

    def test_the_floor_never_moves_backwards(self):
        """任何抵达时刻下，起排时刻都不早于 DAY_START_MINUTES。"""
        for hour in (0, 3, 5, 7, 8, 10, 12, 14, 18, 23):
            flight = make_flight(arrival_at=datetime(2026, 10, 1, hour, 15))
            assert arrival_day_start_minutes(flight) >= DAY_START_MINUTES

    def test_a_real_transfer_estimate_can_push_past_the_clamp(self):
        """高德实测接驳耗时够长时，起排时刻由抵达链决定，而不是被 DAY_START 顶掉。"""
        flight = make_flight(arrival_at=datetime(2026, 10, 1, 7, 0))

        assert arrival_day_start_minutes(flight, transfer_minutes=30) == 8 * 60 + 35
        assert arrival_day_start_minutes(flight, transfer_minutes=180) == 7 * 60 + 45 + 180 + 20

    def test_without_checkin_the_hotel_time_is_not_charged(self):
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        assert arrival_day_start_minutes(red_eye, with_checkin=False) == DAY_START_MINUTES

    def test_missing_arrival_time_stays_none(self):
        """取不到抵达时刻就是无从判断，不拿 DAY_START_MINUTES 冒充一个答案。"""
        assert arrival_day_start_minutes(None) is None
        assert arrival_day_start_minutes(make_flight(arrival_at=None)) is None
        assert arrival_day_start_minutes(make_train(arrival_at=None)) is None

    def test_the_clamped_day_contains_no_dawn_schedule(self):
        """端到端：红眼抵达日的入住与游玩都不早于 08:30，且不再有 05:06 的晚饭。"""
        intent = TripIntent(destination=["成都"], start_date=BASE_DATE, days=3, travelers=2)
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        days = build_initial_plan(intent, plan_fixture_places(), hotel=make_hotel(),
                                  outbound=red_eye, trust_scores=PLAN_TRUST, ad_risks=PLAN_RISK)

        assert days[0].items and all(item.type == "transport" for item in days[0].items)
        arrival_day = days[1]
        assert arrival_day.date == date(2026, 10, 2)
        for item in arrival_day.items:
            if item.type == "transport" or item.start_minutes is None:
                continue
            assert item.start_minutes >= DAY_START_MINUTES, (
                f"{item.name} 排在 {item.start_time}，抵达日凌晨不该有行程"
            )
        checkin = next(item for item in arrival_day.items if item.type == "hotel")
        assert checkin.start_minutes >= DAY_START_MINUTES

    def test_the_raw_landing_time_is_still_reported(self):
        """起排时刻取整不能把事实抹掉：24h 内的原始抵达时刻仍以 00:15 呈现。"""
        red_eye = make_flight(departure_at=datetime(2026, 10, 1, 21, 30),
                              arrival_at=datetime(2026, 10, 2, 0, 15))

        assert red_eye.arrival_at.strftime("%H:%M") == "00:15"
        assert minutes_to_clock(red_eye.arrival_at.hour * 60 + red_eye.arrival_at.minute) == "00:15"


def _many_places(count: int = 24) -> list[Place]:
    """一批同城的合成景点：用来把若干天的容量填满，逼出末日的裁剪路径。"""
    return [
        make_place(
            f"P{i}",
            f"景点{i}",
            district="渝中区",
            lat=29.55 + i * 0.001,
            lng=106.55 + i * 0.001,
            type="风景名胜",
            opening_hours="08:00-22:00",
        )
        for i in range(count)
    ]


def _many_trust(places: list[Place]) -> tuple[dict, dict]:
    return {p.place_id: 80.0 for p in places}, {p.place_id: 0.0 for p in places}


class TestReturnLegSurvivesTrimming:
    """末日返程段是既成事实，不能因为"最后一个景点装不下"被一起裁掉。

    真机事故（2026-09-18）：4 个 case 里有 3 个的行程里**根本没有回家的那一段** ——
    返程只写在 `transport.inbound_selected` 里，日程里找不到。根因是
    `planner._trim_to_window` 掐窗口时执行 `working_items[first:]` 整段切片，
    而返程段固定排在当天最后，于是"第一个超窗的景点"把后面的交通段一并带走。
    """

    def test_the_return_leg_is_kept_when_earlier_stops_overflow(self):
        intent = TripIntent(destination=["重庆"], start_date=date(2026, 10, 3), days=4, travelers=3)
        outbound = make_flight(flight_no="JD5227",
                               departure_at=datetime(2026, 10, 3, 6, 55),
                               arrival_at=datetime(2026, 10, 3, 8, 55))
        inbound = make_flight(flight_no="CA4341",
                              departure_at=datetime(2026, 10, 6, 16, 55),
                              arrival_at=datetime(2026, 10, 6, 18, 50))
        places = _many_places()
        trust, risk = _many_trust(places)

        days = build_initial_plan(intent, places, hotel=make_hotel(), outbound=outbound,
                                  inbound=inbound, trust_scores=trust, ad_risks=risk)

        last = days[-1]
        inbound_items = [item for item in last.items if item.id.endswith("-inbound")]
        assert len(inbound_items) == 1, (
            f"末日只剩 {[i.type for i in last.items]}，返程段被裁掉了"
        )
        assert inbound_items[0].start_time == "16:55"
        assert inbound_items[0].end_time == "18:50"

    def test_the_return_leg_survives_even_when_the_window_shuts_before_it(self):
        """末日窗口比起床时刻还早（赶早班车）时，返程段同样必须留在日程里。"""
        intent = TripIntent(destination=["成都"], start_date=date(2026, 10, 1), days=6, travelers=2)
        outbound = make_flight(departure_at=datetime(2026, 10, 1, 9, 10),
                               arrival_at=datetime(2026, 10, 1, 13, 30))
        early_return = make_train(train_no="K546",
                                  departure_at=datetime(2026, 10, 6, 7, 58),
                                  arrival_at=datetime(2026, 10, 8, 6, 45))
        places = _many_places()
        trust, risk = _many_trust(places)

        days = build_initial_plan(intent, places, hotel=make_hotel(), outbound=outbound,
                                  inbound=early_return, trust_scores=trust, ad_risks=risk)

        last = days[-1]
        assert any(item.id.endswith("-inbound") for item in last.items), (
            f"末日只剩 {[(i.type, i.name) for i in last.items]}，早班返程被裁掉了"
        )

    def test_no_day_is_ever_handed_over_empty(self):
        """任何一天都不能是一张空白日程卡 —— 排不下就说排不下，不是什么都不说。"""
        intent = TripIntent(destination=["成都"], start_date=date(2026, 10, 1), days=6, travelers=2)
        early_return = make_train(train_no="K546",
                                  departure_at=datetime(2026, 10, 6, 7, 58),
                                  arrival_at=datetime(2026, 10, 8, 6, 45))
        places = _many_places()
        trust, risk = _many_trust(places)

        days = build_initial_plan(intent, places, hotel=make_hotel(),
                                  inbound=early_return, trust_scores=trust, ad_risks=risk)

        for day in days:
            assert day.items, f"{day.date} 是空白日程卡"
        # 只有一条机动安排的（排不下的那种）必须写清为什么，不能是一句"自由活动"了事。
        for day in days:
            if len(day.items) == 1 and day.items[0].type == "free_time":
                assert "排不下" in (day.items[0].reason or "") or "在途" in (
                    day.items[0].reason or ""
                )

    def test_an_overflowing_stop_is_still_dropped_with_a_note(self):
        """回归：保住返程段不等于不裁景点 —— 装不下的停留点照样要裁并如实说明。"""
        intent = TripIntent(destination=["重庆"], start_date=date(2026, 10, 3), days=4, travelers=3)
        inbound = make_flight(flight_no="CA4341",
                              departure_at=datetime(2026, 10, 6, 16, 55),
                              arrival_at=datetime(2026, 10, 6, 18, 50))
        places = _many_places()
        trust, risk = _many_trust(places)

        days = build_initial_plan(intent, places, hotel=make_hotel(), inbound=inbound,
                                  trust_scores=trust, ad_risks=risk)

        for item in days[-1].items:
            if item.type == "transport":
                continue
            assert item.end_minutes is None or item.end_minutes <= departure_deadline_minutes(inbound)
