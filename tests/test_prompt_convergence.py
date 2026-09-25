"""Agent Loop 收敛约束的 prompt 落地测试。

背景：评审结论要求给主规划 system prompt（TRAVEL_PLANNER_SYSTEM_PROMPT）追加
「收敛与停止」约束，限制同一个信息目标的重试次数、禁止改用户日期、事实足够就交卷，
防止 agent 无止境地补查。这些约束是纯字符串常量，直接断言关键措辞即可，
不需要跑完整 Agent Loop（与 app/prompts.py 顶部"方便单测直接断言关键约束句"的约定一致）。
"""

import re

from app.prompts import (
    TRAVEL_PLANNER_PROMPT_VERSION,
    TRAVEL_PLANNER_SYSTEM_PROMPT,
)


def _prompt_version_num() -> int:
    """解析版本号末尾数字，例如 travel-planner-v3 -> 3。"""
    match = re.search(r"v(\d+)\s*$", TRAVEL_PLANNER_PROMPT_VERSION)
    assert match, (
        f"TRAVEL_PLANNER_PROMPT_VERSION 格式不符合 travel-planner-vN："
        f"{TRAVEL_PLANNER_PROMPT_VERSION!r}"
    )
    return int(match.group(1))


def test_prompt_limits_retry_to_two_attempts():
    """同一个信息目标最多尝试 2 次，第 2 次失败就停止该目标、标 unknown/warning。"""
    assert "最多尝试 2 次" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "第 2 次仍失败就立即停止该目标" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "unknown" in TRAVEL_PLANNER_SYSTEM_PROMPT


def test_prompt_forbids_changing_user_dates():
    """不得更改用户给出的旅行日期；无班次按 unknown/warning 处理，而不是改日期重查。"""
    assert "不得更改用户给出的旅行日期" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "当天无合适班次" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "改日期重查" in TRAVEL_PLANNER_SYSTEM_PROMPT


def test_prompt_submits_when_facts_are_enough():
    """事实足够就交卷：数据已满足用户约束时，禁止继续扩大搜索，直接 submit_final_plan。"""
    assert "事实足够就交卷" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "禁止为了" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "submit_final_plan" in TRAVEL_PLANNER_SYSTEM_PROMPT


def test_convergence_section_coexists_with_retry_paragraph():
    """新章节与既有"取数失败不等于没有"并存不矛盾：一个管重试、一个管何时停。"""
    assert "取数失败不等于" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "最多试几次" in TRAVEL_PLANNER_SYSTEM_PROMPT
    assert "并存不矛盾" in TRAVEL_PLANNER_SYSTEM_PROMPT


def test_prompt_version_bumped_for_convergence_changes():
    """版本号随 prompt 内容改动而 bump（本次至少到 v3），格式仍是 travel-planner-vN。"""
    assert TRAVEL_PLANNER_PROMPT_VERSION == "travel-planner-v3"
    assert _prompt_version_num() >= 3
