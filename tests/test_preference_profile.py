"""角色 B（docs/14 决策与规划质量）的验证：动态偏好画像、住宿区域、候选池、
契约 2 字段、契约 4 阶段码，以及 Jev 不可用时的 LLM Ranking 兜底（E1/E3/E4/E7）。

为什么这些点必须被单独锁住：它们全是"软决策"，一旦写错不会报错，只会让
"用户说主要想吃，系统还是按景点权重排"这种问题悄悄回来。
"""

from __future__ import annotations

from datetime import date, timedelta

from app import discovery, planner, selection
from app.decision.jev import JevResult
from app.decision.planner_decision import (
    DECISION_LLM_RANKING,
    choose_plan,
    llm_rank_candidates,
)
from app.decision.profile import emphasis, generate_preference_profile
from app.discovery import extract_hotel_areas, split_poi_pools
from app.models import (
    Evidence,
    HotelOption,
    ItineraryDay,
    ItineraryItem,
    Place,
    PreferenceProfile,
    TripIntent,
    TripPlan,
)
from app.planner import PlanCandidate, plan_quality
from app.workflow import (
    STAGE_BUDGET_CHECKED,
    STAGE_DAYS_ARRANGED,
    STAGE_HOTEL_AREA_SELECTED,
    STAGE_HOTELS_COMPARED,
    STAGE_MEALS_MATCHED,
    STAGE_ROUTES_CHECKED,
    _emit_progress_stage,
    _hotel_score,
    _select_hotel,
    _user_journey_summary,
    execute_travel_run,
)
from tests.fakes import QUERY, FakeHub, FakeJev, FakeLLM, make_store

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

    def test_hotel_price_term_scales_with_profile(self):
        hotel = HotelOption(hotel_id="h1", name="经济酒店", price_per_night=300.0, rating=4.0)
        baseline, _ = _hotel_score(hotel, INTENT)
        thrifty = PreferenceProfile.from_llm(
            {"travel_style": "x", "hotel": {"price": 1.0}}
        )
        priced, parts = _hotel_score(hotel, INTENT, profile=thrifty)
        assert priced > baseline
        assert parts["每晚价格"] > 0

    def test_reject_stays_hard_excluded_under_any_profile(self):
        places = [_place("p1", "某网红店"), _place("p2", "武侯祠")]
        outcome = selection.apply_user_place_preferences(places, {"p1": "REJECT"})
        assert [p.place_id for p in outcome.excluded] == ["p1"]
        assert "p1" not in [p.place_id for p in outcome.kept]

    def test_hotel_area_selection_prefers_hotel_in_area(self):
        in_area = HotelOption(hotel_id="h1", name="春熙路智选", business_area="春熙路", price_per_night=500.0, rating=4.0)
        out_area = HotelOption(hotel_id="h2", name="郊区快捷", business_area="双流", price_per_night=120.0, rating=4.0)
        areas = [{"key": "春熙路", "name": "春熙路", "reason": "", "tags": [], "fit_score": 0.9}]
        selected, _, reason = _select_hotel([in_area, out_area], INTENT, hotel_areas=areas)
        # 便宜得多的郊区酒店本会胜出，但"先选区域"让它落在推荐区域内的对手反超。
        assert selected.hotel_id == "h1"
        assert "春熙路" in reason


# ======================================================================
# 4. 契约 4：可读阶段码
# ======================================================================


class TestContractFourStages:
    def test_emit_progress_stage_calls_hook(self):
        seen: list[tuple[str, str]] = []

        def hook(stage_id, status, message, facts, started_at, finished_at, **kwargs):
            seen.append((stage_id, message))

        _emit_progress_stage({"progress_hook": hook}, STAGE_DAYS_ARRANGED, "已安排 3 天")
        assert seen == [(STAGE_DAYS_ARRANGED, "已安排 3 天")]

    def test_emit_is_noop_without_hook(self):
        _emit_progress_stage({}, STAGE_DAYS_ARRANGED, "x")  # 不抛异常即可

    def test_stage_codes_are_the_frozen_contract(self):
        assert {
            STAGE_HOTEL_AREA_SELECTED,
            STAGE_HOTELS_COMPARED,
            STAGE_DAYS_ARRANGED,
            STAGE_MEALS_MATCHED,
            STAGE_ROUTES_CHECKED,
            STAGE_BUDGET_CHECKED,
        } == {
            "hotel_area_selected",
            "hotels_compared",
            "days_arranged",
            "meals_matched",
            "routes_checked",
            "budget_checked",
        }


# ======================================================================
# 5. 契约 2：run 侧画像 / 住宿区域 / 候选池
# ======================================================================


class TestJourneyContractFields:
    def test_user_journey_carries_profile_areas_and_pools(self):
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


# ======================================================================
# 6. E7：Jev 不可用时的 LLM Ranking 兜底
# ======================================================================


def _candidate(label, areas):
    days = []
    for index, area in enumerate(areas):
        days.append(
            ItineraryDay(
                day_index=index,
                date=date(2026, 10, 1) + timedelta(days=index),
                area=area,
                items=[
                    ItineraryItem(
                        id=f"d{index}-{area}-{n}",
                        type="attraction",
                        place_id=f"p-{area}-{n}",
                        name=f"{area}景点{n}",
                        duration_minutes=120,
                        start_time="09:00",
                        end_time="11:00",
                    )
                    for n in (1, 2)
                ],
            )
        )
    return PlanCandidate(label=label, variant="nearest", days=days, quality=plan_quality(days, INTENT))


CAND_A = _candidate("A", ("青羊区", "武侯区", "锦江区"))
CAND_B = _candidate("B", ("武侯区", "青羊区", "成华区"))


class _RankLLM:
    def __init__(self, choice):
        self._choice = choice
        self.calls: list[str] = []

    def invoke_json(self, system, user, *, tag=""):
        self.calls.append(tag)
        if self._choice is None:
            return JevResult(tag=tag, status="TIMEOUT", error="no")
        return _Ok({"choice": self._choice, "reason": "更符合偏好"})


class _Ok:
    def __init__(self, value):
        self.ok = True
        self.value = value
        self.status = "OK"
        self.model = "fake"
        self.duration_ms = 3
        self.error = None


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


class TestContractFourEndToEnd:
    def test_guided_run_emits_all_six_stage_codes(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        producer = FakeHub(store=None, run_id="ps-c4", poi_spread=0.25)
        result = discovery.prefetch(producer, FakeLLM(), GUIDED_INTENT, hotel_pages=2)
        bundle = discovery.PrefetchBundle(
            session_id="ps-c4",
            outbound=list(result["transport"].outbound),
            inbound=list(result["transport"].inbound),
            hotels=list(result["hotels"].items),
            evidences=list(result["social"].evidences),
            places=list(result["places"].places),
            social_queries=list(result["social"].queries),
            social_served_queries=list(result["social"].served_queries),
            provider_calls=producer.audit_entries(),
            discovery=dict(result.get("stages") or {}),
        )
        run_id = "tp-c4-e2e"
        store.create_run(run_id, source="guided", source_session_id="ps-c4")
        run = execute_travel_run(
            QUERY,
            store=store,
            hub=FakeHub(store=store, run_id=run_id, poi_spread=0.25),
            llm=FakeLLM(),
            jev=FakeJev(),
            intent=GUIDED_INTENT,
            prefetch=bundle,
            source="guided",
            source_session_id="ps-c4",
            run_id=run_id,
            output_dir=tmp_path / "out",
        )

        assert run.status == "completed", run.error
        names = {span["name"] for span in store.get_trace_spans(run_id)}
        assert {
            STAGE_HOTEL_AREA_SELECTED,
            STAGE_HOTELS_COMPARED,
            STAGE_DAYS_ARRANGED,
            STAGE_MEALS_MATCHED,
            STAGE_ROUTES_CHECKED,
            STAGE_BUDGET_CHECKED,
        } <= names
        # contract 2 的 run 侧字段也要出现在 user_journey 审计里。
        journey = run.audit["user_journey"]
        assert journey["hotel_area_selected"]
        assert journey["poi_pools"]["attraction"] or journey["poi_pools"]["experience"]


class TestLlmRankingFallback:
    def test_llm_rank_candidates_returns_label(self):
        label, record = llm_rank_candidates([CAND_A, CAND_B], INTENT, _RankLLM("B"))
        assert label == "B"
        assert record["decision_type"] == DECISION_LLM_RANKING
        assert record["fallback"] is False

    def test_llm_rank_candidates_rejects_unknown_label(self):
        label, record = llm_rank_candidates([CAND_A, CAND_B], INTENT, _RankLLM("Z"))
        assert label is None
        assert record["fallback"] is True

    def test_choose_plan_uses_ranking_when_jev_failed(self):
        class _FailingJev:
            def choose(self, *, tag, state, instructions, criteria):
                return JevResult(tag=tag, status="TIMEOUT", error="boom", attempted=True)

        choice = choose_plan(
            [CAND_A, CAND_B],
            INTENT,
            jev=_FailingJev(),
            llm_ranker=lambda cands: llm_rank_candidates(cands, INTENT, _RankLLM("B"))[0],
        )
        assert choice.candidate.label == "B"
        assert choice.fallback is True
        assert "LLM Ranking" in choice.reason

    def test_choose_plan_does_not_rank_when_jev_was_skipped(self):
        class _SkippedJev:
            def choose(self, *, tag, state, instructions, criteria):
                return JevResult(tag=tag, status="SKIPPED", error="未配置", attempted=False)

        ranked: list[bool] = []
        choice = choose_plan(
            [CAND_A, CAND_B],
            INTENT,
            jev=_SkippedJev(),
            llm_ranker=lambda cands: ranked.append(True) or "B",
        )
        # Jev 只是没配（不是失败），保持一贯行为：不加一次模型调用，直接用默认方案。
        assert ranked == []
        assert choice.candidate.label == "A"
        assert choice.fallback is True

    def test_choose_plan_ranking_respects_hard_constraints(self):
        class _FailingJev:
            def choose(self, *, tag, state, instructions, criteria):
                return JevResult(tag=tag, status="TIMEOUT", error="boom", attempted=True)

        choice = choose_plan(
            [CAND_A, CAND_B],
            INTENT,
            jev=_FailingJev(),
            llm_ranker=lambda cands: "B",
            hard_violations=lambda candidate: ["硬约束不允许"] if candidate.label == "B" else [],
        )
        assert choice.candidate.label == "A"
        assert choice.fallback is True
