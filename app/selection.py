"""用户选择层：把"用户点过的按钮"翻译成 Planner 能执行的分值。

这个模块存在的唯一理由（接管任务 §9 / 用户旅程文档 §7 §9 §10）：**按钮必须真的改变结果**。
"必去/想去/不感兴趣"如果只是写进一个字符串、而评分逻辑照旧，那这次改造就是假的。
所以凡是"用户表达过的偏好如何影响排程"的规则，都集中在这里，一处可查、一处可改。

四个职责：

1. 取值归一化（`normalize_*`）—— 前端传什么都别炸，认不出就退回 `auto`；
2. 策略 → 权重（`transport_weights` / `hotel_weights`）—— 权重来自 `app/config.py`，
   这里只做"策略选哪一组权重"的选择，不散落魔数；
3. 节奏自动（`resolve_pace`）—— `auto` 必须是确定性推导，不是随机；
4. POI 用户选择（`apply_user_place_preferences`）—— REJECT 硬排除、MUST 强优先、
   WANT 加分、NEUTRAL 照旧；并且把"MUST 排不进去"如实暴露出来（不能偷偷丢）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.config import PlannerTuning, current_tuning
from app.models import Decision, DecisionStatus, Evidence, Place, TripIntent, coerce_float, coerce_str

# ======================================================================
# 取值归一化
# ======================================================================

TRANSPORT_MODES = ("train", "flight", "any", "auto")
TRANSPORT_PRIORITIES = ("value", "fastest", "cheapest", "comfort", "auto")
TRANSPORT_CONSTRAINTS = ("few_transfers", "no_early", "no_red_eye", "any_time")
HOTEL_PRIORITIES = ("value", "location", "rating", "comfort", "transit", "auto")
PACES = ("relaxed", "balanced", "packed", "auto")

#: 中文标签。前端展示与文档都引用这里，避免两处各写一份。
TRANSPORT_MODE_LABELS = {"train": "高铁", "flight": "飞机", "any": "都可以", "auto": "帮我选"}
TRANSPORT_PRIORITY_LABELS = {
    "value": "性价比优先",
    "fastest": "时间最短",
    "cheapest": "价格最低",
    "comfort": "舒适优先",
    "auto": "帮我选",
}
TRANSPORT_CONSTRAINT_LABELS = {
    "few_transfers": "少换乘",
    "no_early": "不要太早",
    "no_red_eye": "不要红眼",
    "any_time": "时间都可以",
}
HOTEL_PRIORITY_LABELS = {
    "value": "性价比优先",
    "location": "位置优先",
    "rating": "评分优先",
    "comfort": "舒适优先",
    "transit": "交通方便",
    "auto": "帮我选",
}
PACE_LABELS = {"relaxed": "轻松一点", "balanced": "正常", "packed": "多玩一些", "auto": "帮我安排"}

#: POI 用户选择（用户旅程 §7）。NEUTRAL = 用户没点，交给 Planner 自动决定。
MUST = "MUST"
WANT = "WANT"
REJECT = "REJECT"
NEUTRAL = "NEUTRAL"
PLACE_SELECTIONS = (MUST, WANT, REJECT, NEUTRAL)

PLACE_SELECTION_LABELS = {MUST: "必去", WANT: "想去", REJECT: "不感兴趣", NEUTRAL: "未选择"}


def normalize_transport_mode(value: Any) -> str:
    return _normalize(value, TRANSPORT_MODES)


def normalize_transport_priority(value: Any) -> str:
    return _normalize(value, TRANSPORT_PRIORITIES)


def normalize_hotel_priority(value: Any) -> str:
    return _normalize(value, HOTEL_PRIORITIES)


def normalize_pace(value: Any) -> str:
    return _normalize(value, PACES)


def normalize_transport_constraints(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in values:
        text = str(item).strip().lower()
        if text in TRANSPORT_CONSTRAINTS and text not in result:
            result.append(text)
    # "时间都可以" 与其它约束互斥：它表达的是"没有约束"。
    if "any_time" in result:
        return ["any_time"]
    return result


def normalize_place_selections(values: Any) -> dict[str, str]:
    if not isinstance(values, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, value in values.items():
        state = str(value).strip().upper()
        if state in (MUST, WANT, REJECT):
            result[str(key)] = state
    return result


def _normalize(value: Any, allowed: Sequence[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else "auto"


# ======================================================================
# 策略 → 权重
# ======================================================================


@dataclass(frozen=True, slots=True)
class StrategyWeights:
    """一组打分权重 + 人话说明（说明会进决策链，用户能看懂为什么这么选）。"""

    values: dict[str, float]
    label: str


def transport_weights(intent: TripIntent) -> StrategyWeights:
    """交通排序策略 → 权重。

    默认（`auto` / `any`）的策略是**确定性**的，顺序即优先级：
        日期正确 → 不浪费首末日 → 综合门到门时间与价格 → 舒适度。
    这就是"帮我选"的语义 —— 不是随机，而是一套固定的规则。
    """

    tuning = current_tuning()
    mode = normalize_transport_mode(intent.transport_mode)
    priority = normalize_transport_priority(intent.transport_priority)
    base = {
        "price": tuning.transport_price_weight,
        "duration": tuning.transport_duration_weight,
        "comfort": tuning.transport_comfort_weight,
        "preference": tuning.transport_preference_weight,
    }
    labels = {**TRANSPORT_PRIORITY_LABELS, "auto": "帮我选（固定规则：日期→首末日→门到门时间与价格→舒适）"}
    if priority == "cheapest":
        base["price"] *= 3.0
        base["duration"] *= 0.4
    elif priority == "fastest":
        base["duration"] *= 3.0
        base["price"] *= 0.4
    elif priority == "comfort":
        base["comfort"] *= 3.0
        base["preference"] *= 1.5
    elif priority == "value":
        # 性价比 = 时间与价格同等重要，但都不压倒对方（保持默认相对关系并略微抬价）。
        base["price"] *= 1.4
        base["duration"] *= 1.4
    # 交通方式偏好：不是硬过滤（用户说"高铁"时如果当天没有高铁，硬过滤会让行程没有交通），
    # 而是强烈偏好 —— 与"必须如实披露"的原则一致。
    if mode == "train":
        base["mode_train"] = tuning.transport_mode_match_bonus
    elif mode == "flight":
        base["mode_flight"] = tuning.transport_mode_match_bonus
    constraints = normalize_transport_constraints(intent.transport_constraints)
    if "no_red_eye" in constraints:
        base["red_eye_penalty"] = tuning.transport_red_eye_penalty
    if "no_early" in constraints:
        base["early_departure_penalty"] = tuning.transport_early_departure_penalty
    if "few_transfers" in constraints:
        base["transfer_penalty"] = tuning.transport_transfer_penalty
    return StrategyWeights(values=base, label=_transport_strategy_label(mode, priority, constraints))


def _transport_strategy_label(mode: str, priority: str, constraints: Sequence[str]) -> str:
    bits = [f"方式 {TRANSPORT_MODE_LABELS.get(mode, mode)}", f"排序 {TRANSPORT_PRIORITY_LABELS.get(priority, priority)}"]
    if constraints:
        bits.append("约束 " + "/".join(TRANSPORT_CONSTRAINT_LABELS.get(item, item) for item in constraints))
    return "；".join(bits)


def hotel_weights(intent: TripIntent) -> StrategyWeights:
    """酒店策略 → 权重。`auto` 的固定规则：位置/交通不能明显差 → 评分达到合理水平 → 再比价格。"""

    tuning = current_tuning()
    priority = normalize_hotel_priority(intent.hotel_priority)
    base = {
        "price": tuning.hotel_price_weight,
        "rating": tuning.hotel_rating_weight,
        "location": tuning.hotel_location_weight,
        "preference": tuning.hotel_preference_bonus,
    }
    if priority == "value":
        base["price"] *= 1.8
        base["rating"] *= 1.2
    elif priority == "location":
        base["location"] *= 2.5
    elif priority == "rating":
        base["rating"] *= 2.5
    elif priority == "comfort":
        base["rating"] *= 1.6
        base["price"] *= 0.5  # 舒适优先时对价格不敏感
    elif priority == "transit":
        base["location"] *= 1.5
        base["comfort"] = tuning.hotel_transit_bonus
    return StrategyWeights(
        values=base,
        label=f"酒店策略 {HOTEL_PRIORITY_LABELS.get(priority, priority)}",
    )


# ======================================================================
# 节奏 auto
# ======================================================================


def resolve_pace(
    intent: TripIntent,
    *,
    candidate_count: int = 0,
    playable_days: int | None = None,
    first_day_short: bool = False,
    last_day_short: bool = False,
) -> str:
    """把 `auto` 推导成具体节奏（确定性）。

    依据（用户旅程 §10）：天数、候选 POI 数量、首末日交通、每日可用时间。
    规则写死在这里并进决策链，用户能看到"为什么给我排成正常节奏"。
    """

    pace = normalize_pace(intent.pace)
    if pace != "auto":
        return pace
    tuning = current_tuning()
    days = max(1, playable_days if playable_days is not None else intent.days)
    if days <= 0:
        return "balanced"
    per_day = candidate_count / days
    # 首末日照样要玩，但可用时间明显更少 —— 相当于"有效天数少一点"。
    if first_day_short:
        per_day *= 1.0 + tuning.pace_auto_short_day_bonus
    if last_day_short:
        per_day *= 1.0 + tuning.pace_auto_short_day_bonus
    if per_day <= tuning.pace_auto_relaxed_max_per_day:
        return "relaxed"
    if per_day >= tuning.pace_auto_packed_min_per_day:
        return "packed"
    return "balanced"


def resolve_pace_with_reason(intent: TripIntent, **kwargs: Any) -> tuple[str, str]:
    pace = resolve_pace(intent, **kwargs)
    if normalize_pace(intent.pace) == "auto":
        reason = (
            f"节奏由系统推导为「{PACE_LABELS.get(pace, pace)}」"
            f"（候选 {kwargs.get('candidate_count', 0)} 个 / 可玩 {kwargs.get('playable_days') or intent.days} 天，"
            "固定规则：候选密度低→轻松、高→多玩一些）"
        )
    else:
        reason = f"节奏由用户指定为「{PACE_LABELS.get(pace, pace)}」"
    return pace, reason


# ======================================================================
# POI 用户选择 → Planner
# ======================================================================


@dataclass(slots=True)
class PlacePreferenceOutcome:
    """用户选择层的结果。"""

    kept: list[Place]
    excluded: list[Place]
    must_places: list[Place]
    want_places: list[Place]
    decisions: list[Decision] = field(default_factory=list)
    #: MUST 的点里，客观排不进去的（必须如实告知用户，不能静默丢弃）
    must_missing: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "must": len(self.must_places),
            "want": len(self.want_places),
            "reject": len(self.excluded),
            "kept": len(self.kept),
        }


def selection_of(place: Place, selections: Mapping[str, str]) -> str:
    raw = selections.get(place.place_id) or selections.get(place.name)
    state = str(raw or "").strip().upper()
    return state if state in (MUST, WANT, REJECT) else NEUTRAL


def apply_user_place_preferences(
    places: Sequence[Place],
    selections: Mapping[str, str],
    *,
    scores: Mapping[str, float] | None = None,
) -> PlacePreferenceOutcome:
    """把 POI 选择作用到候选集合上。

    规则（用户旅程 §9，硬规则最高优先级）：
      * REJECT —— 硬排除，绝不进入最终行程（这是用户明确说"不要"的东西）；
      * MUST   —— 强优先级：加远超普通偏好的分，但不越过硬约束（时间/营业/预算仍然说话）；
      * WANT   —— 明显加分，Planner 优先放进去；
      * NEUTRAL —— 不改分，交给现有 Trust / AdRisk / 路线 / 多样性逻辑。
    """

    tuning = current_tuning()
    kept: list[Place] = []
    excluded: list[Place] = []
    must_places: list[Place] = []
    want_places: list[Place] = []
    decisions: list[Decision] = []

    for place in places:
        state = selection_of(place, selections)
        if state == REJECT:
            excluded.append(place)
            decisions.append(
                Decision(
                    entity_id=place.place_id,
                    status=DecisionStatus.REJECT,
                    agent_or_stage="user_selection",
                    reason_codes=["USER_REJECT", "HARD_EXCLUDE"],
                    reason_text=f"用户明确表示对「{place.name}」不感兴趣，已从候选里硬排除",
                    scores={"user_selection": state},
                )
            )
            continue
        if state == MUST:
            must_places.append(place)
        elif state == WANT:
            want_places.append(place)
        kept.append(place)

    return PlacePreferenceOutcome(
        kept=kept,
        excluded=excluded,
        must_places=must_places,
        want_places=want_places,
        decisions=decisions,
    )


def preference_bonus(place: Place, selections: Mapping[str, str]) -> tuple[float, str | None]:
    """单个地点的用户选择加分（越低越好之外的分值体系里，这里返回的是"越大越好"的加项）。

    返回值是 `(加分, 原因)`；`NEUTRAL` 返回 `(0, None)`，调用方据此决定要不要写进说明。
    """

    tuning = current_tuning()
    state = selection_of(place, selections)
    if state == MUST:
        return tuning.must_place_bonus, f"用户标记「必去」：+{tuning.must_place_bonus:g} 分"
    if state == WANT:
        return tuning.want_place_bonus, f"用户标记「想去」：+{tuning.want_place_bonus:g} 分"
    return 0.0, None


def must_place_shortfall(
    plan_place_ids: Sequence[str],
    selections: Mapping[str, str],
    places: Sequence[Place],
) -> list[str]:
    """找出用户标了 MUST、但最终行程里没有的地点名字。"""

    placed = {str(item) for item in plan_place_ids}
    names: list[str] = []
    for place in places:
        if selection_of(place, selections) == MUST and place.place_id not in placed:
            names.append(place.name)
    return names


def rejected_place_intruders(
    plan_place_ids: Sequence[str],
    selections: Mapping[str, str],
) -> list[str]:
    """最终行程里出现了用户 REJECT 的点 —— 这必须是 0，否则是高严重度 Bad Case。"""

    intruders: list[str] = []
    for place_id in plan_place_ids:
        state = str(selections.get(str(place_id), "")).strip().upper()
        if state == REJECT:
            intruders.append(str(place_id))
    return intruders


# ======================================================================
# POI 分类与推荐理由（Discovery 与前端都用同一套，避免两处不一致）
# ======================================================================

#: 类别 → 中文标签（用户旅程 §7 的分类）。
PLACE_CATEGORY_LABELS: dict[str, str] = {
    "attraction": "热门景点",
    "food": "美食",
    "nightview": "夜景",
    "shopping": "商圈",
    "experience": "特色体验",
    "photo": "适合拍照",
    "nature": "自然",
    "history": "历史",
    "family": "亲子",
    "other": "其它",
}

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("food", ("火锅", "小吃", "面", "餐", "美食", "夜市", "串", "烧烤", "咖啡", "茶馆", "甜", "饭", "食")),
    ("nightview", ("夜", "灯光", "观景", "塔", "江", "livehouse", "酒吧")),
    ("shopping", ("商圈", "步行街", "购物", "太古里", "春熙", "百货", "市集", "market")),
    ("photo", ("拍照", "机位", "出片", "网红", "打卡", "摄影")),
    ("nature", ("公园", "山", "湖", "湿地", "森林", "自然", "江", "河", "草")),
    ("history", ("博物", "古迹", "古镇", "祠", "寺", "遗址", "故居", "塔", "宫", "陵")),
    ("family", ("亲子", "动物", "乐园", "科技馆", "海洋", "熊猫")),
    ("experience", ("体验", "演出", "剧场", "手作", "温泉", "游船", "滑雪", "演艺")),
)


def place_category(place: Place) -> str:
    """把地点的类型/名字映射到用户的分类（确定性关键词匹配，不调模型）。"""

    haystack = " ".join(
        filter(None, [place.name, place.type or "", place.business_area or "", place.district or ""])
    )
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    if place.type and any(term in place.type for term in ("餐饮", "美食", "restaurant")):
        return "food"
    return "attraction"


def place_reason(
    place: Place,
    evidences: Sequence[Evidence] = (),
    *,
    trust: float | None = None,
    ad_risk: float | None = None,
) -> str:
    """一句给用户看的推荐理由（由代码生成，不从模型来）。"""

    parts: list[str] = []
    count = len(evidences)
    if count:
        parts.append(f"来自 {count} 篇攻略")
    if place.district or place.business_area:
        parts.append(f"位于{place.district or place.business_area}")
    if trust is not None:
        parts.append(f"可信度 {trust:.0f}")
    if ad_risk is not None and ad_risk >= 40:
        parts.append(f"广告嫌疑 {ad_risk:.0f}（建议自行确认）")
    if place.amap_verified:
        parts.append("高德已核实地点")
    if not parts:
        parts.append("候选地点（证据较少）")
    return "，".join(parts)


def tuning() -> PlannerTuning:
    """当前生效的行程阈值（与 app/planner.py:planner.tuning 同一份来源）。"""

    return current_tuning()


def coerce_str_safe(value: Any) -> str:
    return coerce_str(value)


def coerce_float_safe(value: Any) -> float | None:
    return coerce_float(value)
