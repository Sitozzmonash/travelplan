"""动态偏好画像（docs/14 §4，角色 B）的验证：画像本身、强调系数、住宿区域、
候选池，以及画像真的会改变软打分。

为什么这些点必须被单独锁住：它们全是"软决策"，一旦写错不会报错，只会让
"用户说主要想吃，系统还是按景点权重排"这种问题悄悄回来。

（原先还有 LLM Ranking 兜底与"固定流程 6 个阶段码"两组用例：它们随 12 步固定流程
与软决策层一起退役，相关断言一并删除。）
"""

from __future__ import annotations

from datetime import date

from app import discovery, planner, selection
from app.discovery import extract_hotel_areas, split_poi_pools
from app.models import Evidence, Place, PreferenceProfile, TripIntent, TripPlan
from app.profile import emphasis, generate_preference_profile
from app.workflow import _user_journey_summary
from tests.fakes import FakeHub, FakeLLM

INTENT = TripIntent(
    origin="北京",
    destination=["成都"],
    days=3,
    start_date=date(2026, 10, 1),
    travelers=2,
    preferences=["美食", "拍照"],
)

GUIDED_INTENT = TripIntent(
    origin="北京",
    destination=["成都"],
    days=4,
    start_date=date(2026, 10, 1),
    travelers=2,
    source="guided",
)


# ======================================================================
# 1. PreferenceProfile 模型（E1）
# ======================================================================


class TestPreferenceProfile:
    def test_llm_profile_is_personalized_and_normalized(self):
        profile = PreferenceProfile.from_llm(
            {
                "travel_style": "food_commercial_centered",
                "reason": "主要想吃、喜欢热闹",
                "hotel_area": {"food_density": 0.9, "commercial_area": 0.8, "nightlife": 0.6},
                "attraction": {"user_interest": 0.9, "route_fit": 0.2},
                "pace": {"target_poi_per_day": 3, "prefer_free_time": False},
            }
        )

        assert profile.source == "llm"
        assert profile.personalized is True
        assert profile.travel_style == "food_commercial_centered"
        assert abs(sum(profile.hotel_area.values()) - 1.0) < 1e-6
        assert profile.hotel_area["food_density"] > profile.hotel_area["nightlife"]
        assert profile.pace["target_poi_per_day"] == 3
        assert profile.pace["prefer_free_time"] is False

    def test_empty_or_broken_payload_is_fallback_not_personalized(self):
        for payload in ({}, None, "not-a-dict", {"hotel_area": {}}, []):
            profile = PreferenceProfile.from_llm(payload)
            assert profile.source == "fallback", payload
            assert profile.personalized is False

    def test_default_profile_is_balanced_baseline(self):
        profile = PreferenceProfile.default_profile()
        assert profile.personalized is False
        assert abs(sum(profile.hotel.values()) - 1.0) < 1e-6

    def test_unknown_keys_are_dropped_and_values_clamped(self):
        profile = PreferenceProfile.from_llm(
            {"travel_style": "x", "hotel": {"location": 5.0, "bogus_key": 1.0}}
        )
        assert "bogus_key" not in profile.hotel
        # 5.0 被钳到 1.0；缺的键保持默认值 → location 仍是最高权重项。
        assert profile.hotel["location"] == max(profile.hotel.values())

    def test_generate_uses_llm_when_available(self):
        class _Ok:
            def __init__(self, value):
                self.ok = True
                self.value = value
                self.status = "OK"

        class _LLM:
            def invoke_json(self, system, user, *, tag=""):
                return _Ok({"travel_style": "scenic_photo", "attraction": {"photo_value": 1.0}})

        profile = generate_preference_profile(INTENT, _LLM())
        assert profile.source == "llm"
        assert profile.travel_style == "scenic_photo"

    def test_generate_falls_back_when_llm_is_missing_or_empty(self):
        class _Empty:
            def invoke_json(self, system, user, *, tag=""):
                return _Ok({})

        class _Ok:
            def __init__(self, value):
                self.ok = True
                self.value = value
                self.status = "OK"

        assert generate_preference_profile(INTENT, None).source == "fallback"
        assert generate_preference_profile(INTENT, _Empty()).source == "fallback"


class TestEmphasis:
    def test_none_and_fallback_are_identity(self):
        assert emphasis(None, "hotel", "price") == 1.0
        assert emphasis(PreferenceProfile.default_profile(), "hotel", "price") == 1.0

    def test_personalized_emphasis_moves_and_is_bounded(self):
        profile = PreferenceProfile.from_llm(
            {"travel_style": "x", "hotel_area": {"food_density": 1.0}}
        )
        # food_density 被抬高；quietness 被压低到下限。
        assert emphasis(profile, "hotel_area", "food_density") > 1.0
        assert emphasis(profile, "hotel_area", "food_density") <= 3.0
        assert emphasis(profile, "hotel_area", "quietness") >= 0.25


# ======================================================================
# 2. 候选池（E3）与住宿区域（E4）
# ======================================================================


def _place(pid, name, *, business_area=None, district=None, type=None, lat=30.6, lng=104.06):
    return Place(
        place_id=pid,
        name=name,
        type=type or "",
        business_area=business_area,
        district=district,
        lat=lat,
        lng=lng,
    )


class TestPoiPools:
    def test_pools_split_by_category(self):
        places = [
            _place("p1", "武侯祠", business_area="武侯区", type="历史古迹"),
            _place("p2", "老火锅", business_area="春熙路", type="餐饮"),
            _place("p3", "太古里", business_area="春熙路", type="商圈"),
        ]
        pools = split_poi_pools(places)
        assert set(pools) == {"attraction", "food", "experience"}
        assert "p1" in pools["attraction"]
        assert "p2" in pools["food"]
        assert "p3" in pools["experience"]

    def test_empty_places_gives_empty_pools(self):
        assert split_poi_pools([]) == {"attraction": [], "food": [], "experience": []}


class TestHotelAreas:
    def test_clusters_by_business_area_and_ranks_by_evidence(self):
        places = [
            _place("p1", "太古里", business_area="春熙路", type="商圈"),
            _place("p2", "老火锅", business_area="春熙路", type="餐饮"),
            _place("p3", "第二火锅", business_area="春熙路", type="餐饮"),
            _place("p4", "武侯祠", business_area="武侯区", type="历史古迹"),
        ]
        evidences = [Evidence(id="e1", title="成都住宿", text="住春熙路附近，逛街吃饭都方便")]
        areas = extract_hotel_areas(INTENT, evidences, places)
        assert areas and areas[0]["name"] == "春熙路"
        assert set(areas[0]) == {"key", "name", "reason", "tags", "fit_score"}
        assert areas[0]["fit_score"] >= areas[-1]["fit_score"]
        assert "美食多" in areas[0]["tags"]

    def test_food_profile_raises_food_area_fit(self):
        places = [
            _place("p1", "老火锅", business_area="春熙路", type="餐饮"),
            _place("p2", "第二火锅", business_area="春熙路", type="餐饮"),
            _place("p3", "武侯祠", business_area="武侯区", type="历史古迹"),
            _place("p4", "锦里", business_area="武侯区", type="景点"),
        ]
        baseline = extract_hotel_areas(INTENT, [], places)
        foodie = PreferenceProfile.from_llm(
            {"travel_style": "food", "hotel_area": {"food_density": 1.0, "poi_centrality": 0.01}}
        )
        boosted = extract_hotel_areas(INTENT, [], places, profile=foodie)
        chunxi = next(a for a in baseline if a["name"] == "春熙路")
        chunxi_foodie = next(a for a in boosted if a["name"] == "春熙路")
        assert chunxi_foodie["fit_score"] > chunxi["fit_score"]

    def test_no_places_gives_no_areas(self):
        assert extract_hotel_areas(INTENT, [], []) == []


# ======================================================================
# 3. 画像真的改变打分（E1），且硬规则不被破坏（E2）
# ======================================================================


class TestProfileChangesScoring:
    def test_attraction_preference_bonus_scales_with_profile(self):
        place = _place("p1", "火锅一条街", type="餐饮")
        baseline, baseline_detail = planner.score_candidate(place, 80.0, 10.0, ["美食"], 0.5)
        photo = PreferenceProfile.from_llm(
            {"travel_style": "x", "attraction": {"user_interest": 1.0}}
        )
        _interested, detail = planner.score_candidate(
            place, 80.0, 10.0, ["美食"], 0.5, profile=photo
        )
        # 更看重"用户兴趣"的画像让命中偏好的那一项加分被放大（软权重真的起作用）。
        assert detail["preference_bonus"] > baseline_detail["preference_bonus"]
        assert baseline > 0  # 兜一圈避免未使用告警

    def test_reject_stays_hard_excluded_under_any_profile(self):
        places = [_place("p1", "某网红店"), _place("p2", "武侯祠")]
        outcome = selection.apply_user_place_preferences(places, {"p1": "REJECT"})
        assert [p.place_id for p in outcome.excluded] == ["p1"]
        assert "p1" not in [p.place_id for p in outcome.kept]


# ======================================================================
# 4. 契约 2：画像 / 住宿区域 / 候选池要能被读出来
# ======================================================================


class TestContractTwoDiscoveryFields:
    def test_prefetch_exposes_profile_areas_and_pools(self):
        hub = FakeHub(store=None, run_id="ps-c2", poi_spread=0.25)
        result = discovery.prefetch(hub, FakeLLM(), GUIDED_INTENT, hotel_pages=2)

        assert set(result["poi_pools"]) == {"attraction", "food", "experience"}
        assert result["hotel_areas"], "有真实地点的 Discovery 必须产出住宿区域候选"
        area = result["hotel_areas"][0]
        assert set(area) == {"key", "name", "reason", "tags", "fit_score"}
        # profile 即使退化也必须是完整结构（contract 2 固定字段）。
        assert set(result["profile"]) >= {
            "travel_style",
            "hotel_area",
            "hotel",
            "attraction",
            "food",
            "pace",
            "source",
        }


class TestJourneyContractFields:
    def test_user_journey_carries_profile_areas_and_pools(self):
        """run 侧审计的 user_journey 必须带上画像 / 住宿区域 / 候选池。"""

        profile = PreferenceProfile.from_llm({"travel_style": "food_commercial_centered"})
        places = [
            _place("p1", "武侯祠", business_area="武侯区", type="历史古迹"),
            _place("p2", "老火锅", business_area="春熙路", type="餐饮"),
        ]
        plan = TripPlan(run_id="r1", intent=INTENT, days=[], transport=None, hotel=None)
        state = {
            "places": places,
            "profile": profile,
            "hotel_areas": [{"key": "春熙路", "name": "春熙路", "reason": "x", "tags": ["美食多"], "fit_score": 0.9}],
            "hotel_area_selected": "春熙路",
        }

        journey = _user_journey_summary(state, plan)

        assert journey["profile"]["travel_style"] == "food_commercial_centered"
        assert journey["hotel_area_selected"] == "春熙路"
        assert journey["hotel_areas"][0]["name"] == "春熙路"
        assert journey["poi_pools"]["attraction"] == ["p1"]
        assert journey["poi_pools"]["food"] == ["p2"]
