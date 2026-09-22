"""Dynamic Preference Profile 的生成与打分接入（docs/14 §4，角色 B）。

这里的职责只有一件：把"用户想要什么样的旅行"变成一组**可审计的动态权重**，
让 Python 后续的软取舍（住哪、一天排几个点、绕不绕路）随用户而变。

三条硬规矩：
  1. 生成失败（模型不可用 / 返回空 / 结构不合法）一律回退到**均衡基线**，
     绝不让一次调用失败把整次规划打挂；
  2. Profile 只调**软权重**，不碰任何硬事实（价格、路线、营业时间、REJECT/MUST）；
  3. 只有 LLM 真的生成了差异化画像（``profile.personalized``）才改变打分 ——
     均衡基线必须与"没有 profile"的行为**完全一致**，否则一次模型抖动就会悄悄改排程。
"""

from __future__ import annotations

from typing import Any, Sequence

from app import models
from app.models import PreferenceProfile, TripIntent
from app.prompts import PREFERENCE_PROFILE_PROMPT

#: 强调系数（emphasis = profile_weight / default_weight）的安全上下界。
#: 超出这个范围意味着 LLM 给了极端权重，钳住它避免"一个维度把整组权重吃掉"。
_EMPHASIS_MIN = 0.25
_EMPHASIS_MAX = 3.0

_DEFAULT_GROUPS: dict[str, dict[str, float]] = {
    "hotel_area": models.DEFAULT_HOTEL_AREA_WEIGHTS,
    "hotel": models.DEFAULT_HOTEL_WEIGHTS,
    "attraction": models.DEFAULT_ATTRACTION_WEIGHTS,
    "food": models.DEFAULT_FOOD_WEIGHTS,
}


def emphasis(profile: PreferenceProfile | None, group: str, key: str) -> float:
    """把 profile 的某组某个键翻译成"相对均衡基线的强调系数"。

    无 profile 或非个性化画像 → 1.0（与历史打分完全一致）；个性化画像 → 有界比值。
    """
    if profile is None or not profile.personalized:
        return 1.0
    baseline = _DEFAULT_GROUPS.get(group, {}).get(key)
    if not baseline:
        return 1.0
    value = profile.weight(group, key, default=baseline)
    ratio = value / baseline
    return min(_EMPHASIS_MAX, max(_EMPHASIS_MIN, ratio))


def _intent_summary(intent: TripIntent) -> str:
    destination = "、".join(intent.destination) if intent.destination else "未指定"
    prefs = "、".join(intent.preferences) if intent.preferences else "无特别偏好"
    return (
        f"目的地：{destination}\n"
        f"天数：{intent.days} 天，{intent.travelers} 人\n"
        f"节奏：{intent.pace}；交通方式 {intent.transport_mode or 'auto'}；"
        f"住宿策略 {intent.hotel_priority or 'auto'}\n"
        f"偏好关键词：{prefs}\n"
        f"预算：¥{intent.budget_total:g}" if intent.budget_total else f"预算：未指定"
    )


def generate_preference_profile(
    intent: TripIntent,
    llm: Any,
    *,
    evidences: Sequence[Any] = (),
) -> PreferenceProfile:
    """生成一次旅行专属的动态偏好画像；任何失败都退回均衡基线。

    ``evidences`` 预留（未来可能把攻略里的"美食 vs 景点"信号喂给 profile），
    当前生成只依赖用户已点选/口述的意图 —— 这正是"偏好来自用户"而非"模型替用户猜"。
    """

    if llm is None:
        return PreferenceProfile.default_profile()
    try:
        result = llm.invoke_json(
            PREFERENCE_PROFILE_PROMPT, _intent_summary(intent), tag="preference_profile"
        )
    except Exception:  # noqa: BLE001 —— 画像生成绝不能拖垮规划
        return PreferenceProfile.default_profile()
    if not (result.ok and isinstance(result.value, dict)):
        profile = PreferenceProfile.default_profile()
        profile.reason = f"画像生成不可用（{getattr(result, 'status', '?')}），采用均衡基线"
        return profile
    profile = PreferenceProfile.from_llm(result.value)
    return profile


__all__ = [
    "PreferenceProfile",
    "generate_preference_profile",
    "emphasis",
]