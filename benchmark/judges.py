"""Benchmark 的判卷器（v0.1 只做确定性判定）。

为什么不引入 LLM Judge：v0.1 要回答的是"**客观事实对不对**"（日期、披露、来源、
Bad Case 有没有留下），这类问题有唯一正确答案，用模型判卷反而引入不可复现性。
"这份行程好不好玩"这类主观问题，v0.1 用可复现的软指标（Plan Quality）近似表达。

case 的 `expect` 字段支持这些断言（全部可选，缺省即不检查）：
  hard_constraints_ok        没有未解决的 error 级可行性问题，且交通/住宿都已披露
  min_items                  停留项数量下限
  expect_badcase_categories  必须出现的 Bad Case 类别（子集断言）
  expect_no_badcase_categories  不允许出现的类别
  expect_jev_choice          Jev 的方案选择结果
  expect_jev_fallback        方案选择是否走了确定性兜底
  expect_replan              质量门是否要求过重排
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.badcase import EVIDENCE_REQUIRED_TYPES


def _stay_items(plan: Any) -> list[Any]:
    return [
        item
        for day in plan.days
        for item in day.items
        if item.type not in ("transport", "free_time")
    ]


def hard_constraints_ok(plan: Any) -> tuple[bool, str]:
    if plan is None:
        return False, "没有产出 plan"
    unresolved = [issue for issue in plan.warnings if issue.severity == "error" and not issue.resolved]
    if unresolved:
        return False, f"有 {len(unresolved)} 个未解决的可行性错误：{unresolved[0].code}"
    if plan.transport is None or plan.transport.selected is None:
        return False, "没有披露去程交通"
    if plan.hotel is None or plan.hotel.selected is None:
        return False, "没有披露住宿"
    return True, "硬约束通过"


def judge(case: Mapping[str, Any], observation: Mapping[str, Any]) -> tuple[str, list[str]]:
    """返回 (pass/fail, 失败原因列表)。任意一条不满足即 fail。"""

    expect = dict(case.get("expect") or {})
    failures: list[str] = []
    plan = observation.get("plan")
    categories = set(observation.get("badcase_categories") or ())
    choice = observation.get("plan_choice") or {}
    gate = observation.get("quality_gate") or {}

    if expect.get("hard_constraints_ok"):
        ok, detail = hard_constraints_ok(plan)
        if not ok:
            failures.append(f"硬约束未通过：{detail}")

    if "min_items" in expect:
        items = len(_stay_items(plan)) if plan is not None else 0
        if items < int(expect["min_items"]):
            failures.append(f"停留项只有 {items} 个，少于期望的 {expect['min_items']} 个")

    for category in expect.get("expect_badcase_categories") or ():
        if category not in categories:
            failures.append(f"缺少期望的 Bad Case 类别 {category}（实际：{sorted(categories)}）")

    for category in expect.get("expect_no_badcase_categories") or ():
        if category in categories:
            failures.append(f"出现了不该有的 Bad Case 类别 {category}")

    if "expect_jev_choice" in expect and expect["expect_jev_choice"] is not None:
        if choice.get("selected") != expect["expect_jev_choice"]:
            failures.append(
                f"Jev 选择 {choice.get('selected')}，期望 {expect['expect_jev_choice']}"
            )

    if "expect_jev_fallback" in expect and expect["expect_jev_fallback"] is not None:
        if bool(choice.get("fallback")) is not bool(expect["expect_jev_fallback"]):
            failures.append(
                f"Jev fallback={choice.get('fallback')}，期望 {expect['expect_jev_fallback']}"
            )

    if "expect_replan" in expect and expect["expect_replan"] is not None:
        actually_replan = str(gate.get("decision") or "").upper() == "REPLAN"
        if actually_replan is not bool(expect["expect_replan"]):
            failures.append(f"质量门 decision={gate.get('decision')}，期望 REPLAN={expect['expect_replan']}")

    if str(observation.get("status")) not in {"completed"} and not expect.get("allow_failed"):
        failures.append(f"run 状态是 {observation.get('status')}，不是 completed")

    return ("pass" if not failures else "fail"), failures


def data_integrity_checks(plan: Any) -> list[str]:
    """与 `expect` 无关的通用诚实性断言。

    它们不参与 pass/fail（有些是已知的降级表现），但会写进 case detail：
    维护者一眼能看到"这次 run 有没有出现无来源的价格"。
    """

    problems: list[str] = []
    if plan is None:
        return ["没有 plan"]
    for day in plan.days:
        for item in day.items:
            if item.price is not None and item.price_type == "realtime" and not item.source_ids:
                problems.append(f"{item.name} 标了实时价但没有来源引用")
            if item.type in EVIDENCE_REQUIRED_TYPES and not item.evidence_ids:
                problems.append(f"{item.name} 没有任何证据关联")
    return problems


def summarize_failures(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """失败原因 → 次数。用来看"这一版主要坏在哪"。"""

    counts: dict[str, int] = {}
    for result in results:
        for reason in result.get("failures") or ():
            key = str(reason).split("：")[0][:60]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda entry: -entry[1]))
