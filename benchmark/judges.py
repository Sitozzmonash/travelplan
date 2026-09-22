"""判卷器：只做**确定性**判定，不引入模型打分。

为什么不用 LLM Judge：本套件要回答的是"客观事实对不对"（天/项是否非空、数字有没有
来源、REJECT 有没有混进来、时间冲突有没有被修订、降级说明与事实是否一致），这类问题有
唯一正确答案，用模型判卷反而引入不可复现性。"这份行程好不好玩"不在本套件的射程内。

case 的 `expect` 全部可选，缺省即不检查。**不检查**与"检查了但通过"是两件事，
所以每条检查都会在 `checks` 里留痕（管理端详情的 detail 会显示）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 时间/路线上"没解决"的严重级别。warning（例如 ROUTE_UNVERIFIED）不算冲突，
#: 因为它描述的是"这次没有真实路线数据"，不是"排不下"。
ERROR_SEVERITY = "error"


# ======================================================================
# 通用诚实性断言（与 expect 无关，进 detail，参与 pass/fail 的只有其中一部分）
# ======================================================================


def grounding_report(observation: Any, *, tolerance: float = 1.0) -> dict[str, Any]:
    """行程里的数字/标识能不能指回假工具**真的返回过**的东西。

    检查项（有值才查）：
    * `item.price`            → 必须是某次工具返回里的价格（0 视作免费，放行）
    * `item.place_id`         → 必须是 search_poi / poi_detail 真的给过的 place_id
    * `item.travel_from_previous.duration_seconds` → 必须是 route 工具真的给过的耗时
    * 去程/回程 `no` / `price` / `duration_minutes`
    * `hotel.selected.name` / `price_per_night`
    * `plan.sources[].source_id` → 必须落在本次 run 真的写下的 sources 里

    返回 `{"checked": n, "grounded": n, "unsupported": [...]}`；`unsupported` 里的每一项
    都是"行程声称存在、但取数从来没给出过"的数字 —— 这正是编造。
    """

    plan = observation.plan
    facts = observation.facts or {}
    supported_prices = {round(float(value), 2) for value in facts.get("prices") or ()}
    supported_route_seconds = {int(value) for value in facts.get("route_seconds") or ()}
    supported_place_ids = {str(value) for value in facts.get("place_ids") or ()}
    supported_hotels = {str(value) for value in facts.get("hotel_names") or ()}
    supported_numbers = {str(value) for value in facts.get("transport_numbers") or ()}
    known_sources = {str(value) for value in facts.get("source_ids") or ()}
    known_sources |= {str(row.get("source_id")) for row in observation.sources}

    checked = 0
    grounded = 0
    unsupported: list[str] = []

    def check(where: str, value: Any, pool: set[Any], *, kind: str, number: bool = False) -> None:
        nonlocal checked, grounded
        if value is None or value == "":
            return
        checked += 1
        if number:
            try:
                hit = any(abs(float(value) - float(candidate)) <= tolerance for candidate in pool)
            except (TypeError, ValueError):
                hit = False
        else:
            hit = str(value) in pool
        if hit:
            grounded += 1
        else:
            unsupported.append(f"{where} 的{kind}={value!r} 不在本次取数结果里")

    if plan is None:
        return {"checked": 0, "grounded": 0, "unsupported": [], "rate": None}

    for day in plan.days:
        for item in day.items:
            where = f"D{day.day_index} {item.name}"
            # 免费（0）不是"编造的价格"，任何取数都不需要给出 0 才允许它出现。
            if item.price is not None and float(item.price) != 0.0:
                check(where, item.price, supported_prices, kind="价格", number=True)
            check(where, item.place_id, supported_place_ids, kind="place_id")
            leg = item.travel_from_previous
            if leg is not None and leg.duration_seconds is not None and not leg.estimated:
                check(where + " 的路段", leg.duration_seconds, supported_route_seconds,
                      kind="路上耗时(秒)", number=True)

    transport = getattr(plan, "transport", None)
    for label, option in (
        ("去程", getattr(transport, "selected", None)),
        ("回程", getattr(transport, "inbound_selected", None)),
    ):
        if option is None:
            continue
        check(label, getattr(option, "train_no", None) or getattr(option, "flight_no", None),
              supported_numbers, kind="班次")
        price = getattr(option, "price", None)
        if price is None:
            price_range = getattr(option, "price_range", None) or {}
            price = price_range.get("min") if isinstance(price_range, Mapping) else None
        if price is not None and float(price) != 0.0:
            check(label, price, supported_prices, kind="票价", number=True)
        check(label, getattr(option, "duration_minutes", None),
              {int(value) for value in facts.get("durations") or ()}, kind="历时(分钟)", number=True)

    hotel = getattr(plan, "hotel", None)
    selected = getattr(hotel, "selected", None)
    if selected is not None:
        check("住宿", getattr(selected, "name", None), supported_hotels, kind="店名")
        if getattr(selected, "price_per_night", None) is not None:
            check("住宿", selected.price_per_night, supported_prices, kind="房价", number=True)

    for source in plan.sources:
        check("来源", source.source_id, known_sources, kind="source_id")

    rate = round(grounded / checked, 4) if checked else None
    return {"checked": checked, "grounded": grounded, "unsupported": unsupported, "rate": rate}


def honesty_notes(observation: Any) -> list[str]:
    """降级说明是否与事实一致（进 detail，不直接决定 pass/fail）。

    两条口径：
    * 循环被截断 / 时间冲突没解决 / 行程来自消息 JSON → `degradations` **必须**非空；
    * 干净完成（无截断、无未解决冲突、走的是 submit 工具）→ 不该无端报降级。
    """

    notes: list[str] = []
    degradations = list(observation.degradations or ())
    audit = dict(getattr(observation, "audit", None) or {})
    agent = dict(audit.get("agent") or {})
    truncated = bool(agent.get("truncation"))
    if truncated and not degradations:
        notes.append("循环被截断（audit.agent.truncation 有值）却没有写进 degradations")
    if observation.unresolved_conflicts and not any("冲突" in note for note in degradations):
        notes.append(
            f"有 {observation.unresolved_conflicts} 个未解决的时间冲突，degradations 里没有任何说明"
        )
    return notes


# ======================================================================
# 判分
# ======================================================================


def judge(case: Mapping[str, Any], observation: Any) -> tuple[str, list[str], dict[str, Any]]:
    """返回 `(status, failures, checks)`；`status ∈ {"pass", "fail"}`。

    注意："pass" 不表示 run 一定成功 —— 有的用例的期望**就是**"如实失败"
    （例如工具返回空时不许编造行程）。判定的是"结果 whether 符合预期"。
    """

    expect = dict(case.get("expect") or {})
    failures: list[str] = []
    checks: dict[str, Any] = {}
    expected_status = str(expect.get("expect_status") or "completed")

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failures.append(f"{name}：{detail}" if detail else name)

    # --- 1. run 状态 ---
    record(
        "run_status",
        observation.status == expected_status,
        f"实际 {observation.status}（期望 {expected_status}）"
        + (f"，error={observation.error}" if observation.error else ""),
    )

    # --- 2. plan 的存在性 ---
    expect_no_plan = bool(expect.get("expect_no_plan"))
    if expect_no_plan:
        record("no_plan", observation.plan is None, "工具没给出数据时必须不产出 plan（不编造）")
    elif expected_status == "completed":
        record("plan_present", observation.plan is not None, "completed 却没有 plan")

    plan = observation.plan

    # --- 3. days / items 非空 ---
    if plan is not None:
        days = len(plan.days)
        items = sum(len(day.items) for day in plan.days)
        if "min_days" in expect:
            record("days_nonempty", days >= int(expect["min_days"]),
                   f"实际 {days} 天（至少 {expect['min_days']}）")
        else:
            record("days_nonempty", days > 0 and items > 0, f"实际 {days} 天 / {items} 项")
        if "min_items" in expect:
            record("min_items", items >= int(expect["min_items"]),
                   f"实际 {items} 项（至少 {expect['min_items']}）")

    # --- 4. 数字能不能指回工具返回 ---
    grounding = dict(observation.grounding or {})
    if plan is not None and expect.get("require_grounded_numbers", True):
        unsupported = list(grounding.get("unsupported") or ())
        record("grounded_numbers", not unsupported,
               "；".join(unsupported[:4]) if unsupported else "行程里的数字都有来源")

    # --- 5. REJECT 侵入 ---
    if expect.get("no_reject_intrusion"):
        intruders = list(observation.reject_intruders or ())
        record("no_reject_intrusion", not intruders,
               "用户 REJECT 的 place_id 出现在行程里：" + "、".join(intruders)
               if intruders else "REJECT 一个都没进")

    # --- 6. 时间冲突 ---
    if "max_unresolved_time_conflicts" in expect:
        allowed = int(expect["max_unresolved_time_conflicts"])
        record("unresolved_time_conflicts", observation.unresolved_conflicts <= allowed,
               f"未解决冲突 {observation.unresolved_conflicts} 个（上限 {allowed}）")
    code = expect.get("expect_time_conflict_code")
    if code:
        matched = [issue for issue in observation.warnings if getattr(issue, "code", "") == code]
        record("time_conflict_detected", bool(matched), f"警告里没有 {code}")
        record("time_conflict_resolved", all(getattr(issue, "resolved", False) for issue in matched),
               f"{code} 没有被修订（resolved=False）")

    # --- 7. 预算 ---
    if expect.get("expect_budget_within") and plan is not None:
        budget = plan.budget
        over = (
            budget.projected_total is not None
            and budget.budget_total is not None
            and budget.projected_total > budget.budget_total
        )
        record("budget_within", not over,
               f"预计 {budget.projected_total} / 预算 {budget.budget_total}")

    # --- 7b. "没查到"不许被填成具体的值 ---
    if plan is not None:
        if expect.get("expect_transport_absent"):
            selected = getattr(getattr(plan, "transport", None), "selected", None)
            record("transport_absent", selected is None,
                   f"没查到交通却填了 {getattr(selected, 'train_no', None) or getattr(selected, 'flight_no', None)}")
        if expect.get("expect_hotel_absent"):
            selected_hotel = getattr(getattr(plan, "hotel", None), "selected", None)
            record("hotel_absent", selected_hotel is None,
                   f"没查到酒店却填了 {getattr(selected_hotel, 'name', None)}")
        expected_inbound = expect.get("expect_inbound_transport_no")
        if expected_inbound:
            selected = getattr(getattr(plan, "transport", None), "inbound_selected", None)
            actual = getattr(selected, "train_no", None) or getattr(selected, "flight_no", None)
            record("inbound_transport_no", str(actual) == str(expected_inbound),
                   f"回程班次 {actual}（期望 {expected_inbound}）")
        expected_places = expect.get("expect_place_ids")
        if expected_places:
            missing = sorted(set(map(str, expected_places)) - set(observation.plan_place_ids))
            record("expected_place_ids", not missing,
                   f"行程里缺少：{missing}")

    # --- 7c. 产物：拿不到行程就不许有 plan 三件套 ---
    for name in expect.get("expect_missing_artifacts") or ():
        record(f"missing_artifact[{name}]", name not in observation.artifacts,
               f"{name} 不该存在（实际产物：{observation.artifacts}）")

    # --- 8. 降级说明 ---
    for text in expect.get("expect_degradation_contains") or ():
        record(f"degradation_contains[{text}]",
               any(text in note for note in observation.degradations),
               f"degradations={list(observation.degradations)}")
    if expect.get("forbid_degradations"):
        record("no_degradation", not observation.degradations,
               f"不该有降级却报了：{list(observation.degradations)}")

    # --- 9. 注入是否真的生效（否则用例在"空转"） ---
    for tool in expect.get("expect_injected") or ():
        record(f"injected[{tool}]", tool in observation.injected,
               f"注入没生效（实际：{observation.injected}）")
    for tool in expect.get("expect_failed_calls") or ():
        failed = [
            row for row in observation.provider_calls
            if row.get("tool") == tool and row.get("status") not in ("OK", None)
        ]
        record(f"failed_call[{tool}]", bool(failed), f"{tool} 没有留下失败调用记录")

    # --- 10. Provider 侧最少调用量 ---
    if "min_provider_calls" in expect:
        record("min_provider_calls", len(observation.provider_calls) >= int(expect["min_provider_calls"]),
               f"实际 {len(observation.provider_calls)} 次取数")

    # 通用诚实性：只有"降级说明与事实相反"才判 fail（其它仅为提示）。
    for note in honesty_notes(observation):
        if "没有写进 degradations" in note or "degradations 里没有任何说明" in note:
            record("degradations_honest", False, note)

    return ("pass" if not failures else "fail"), failures, checks


def summarize_failures(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """失败原因 → 次数。用来看"这一版主要坏在哪"。"""

    counts: dict[str, int] = {}
    for result in results:
        for reason in result.get("failures") or ():
            key = str(reason).split("：")[0][:60]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda entry: -entry[1]))


__all__ = ["grounding_report", "honesty_notes", "judge", "summarize_failures"]
