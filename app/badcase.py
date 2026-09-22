"""Bad Case：从一次真实 Run 的事实里用规则找出"确实出问题"的地方。

设计取舍（PRD 与接管任务 §7）：
  * **只做规则检测**。普通 Run 不做重型的"自动进化"，也绝不让 LLM 来判断"这次是不是
    坏了" —— 那会产生一批无法复现的 Bad Case，把回归集污染掉。
  * **只写事实**。`expected` / `actual` 都来自已经算好的字段（plan / metrics / trace），
    不引入推断性描述。
  * **id 稳定**。同一次 run 反复检测必须命中同一行，否则 Evolution 会把同一件事
    当成多个失败模式。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from app.models import TripPlan

#: 只有这些类型的地点谈得上"需要证据支撑"。
EVIDENCE_REQUIRED_TYPES = ("attraction", "food", "activity")

#: Provider 失败到"整条能力不可用"的严重度分界：交通与地图是行程骨架。
CRITICAL_PROVIDERS = ("12306", "tuniu", "amap")

CATEGORY_PROVIDER_FAILURE = "provider_failure"
CATEGORY_DEGRADATION = "degradation"
CATEGORY_HARD_CONSTRAINT = "hard_constraint"
CATEGORY_BUDGET = "budget"
CATEGORY_BUDGET_MATH = "budget_math"
CATEGORY_EVIDENCE_GAP = "evidence_gap"
CATEGORY_MISSING_DISCLOSURE = "missing_disclosure"
CATEGORY_HALLUCINATED_PRICE = "hallucinated_price"
CATEGORY_ROUTE_UNVERIFIED = "route_unverified"

# --- 引导式旅程（用户旅程落地任务 §19 / 管理后台增量任务 §15）---
CATEGORY_DISCOVERY_EMPTY = "discovery_empty"
CATEGORY_DISCOVERY_STALE = "discovery_stale"
CATEGORY_PREFETCH_FAILED = "prefetch_failed"
CATEGORY_PREFETCH_NOT_REUSED = "prefetch_not_reused"
CATEGORY_MUST_POI_MISSING = "must_poi_missing"
CATEGORY_REJECTED_POI_IN_PLAN = "rejected_poi_in_plan"
CATEGORY_USER_PREFERENCE_IGNORED = "user_preference_ignored"
CATEGORY_GUIDED_INTENT_MISMATCH = "guided_intent_mismatch"

#: 与"用户在正式规划之前的路径"有关的类别（管理端据此单独分组）。
JOURNEY_CATEGORIES = (
    CATEGORY_DISCOVERY_EMPTY,
    CATEGORY_DISCOVERY_STALE,
    CATEGORY_PREFETCH_FAILED,
    CATEGORY_PREFETCH_NOT_REUSED,
    CATEGORY_MUST_POI_MISSING,
    CATEGORY_REJECTED_POI_IN_PLAN,
    CATEGORY_USER_PREFERENCE_IGNORED,
    CATEGORY_GUIDED_INTENT_MISMATCH,
)

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"


@dataclass(slots=True)
class BadCaseContext:
    """检测所需的全部事实。全部是已经发生过的东西，不含推测。"""

    run_id: str
    plan: TripPlan | None = None
    provider_calls: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    #: 用户旅程上下文（由 workflow 的 `_user_journey_summary` 提供）：
    #: source / prefetch_* / rejected_in_plan / must_missing / discovery 分阶段状态。
    journey: dict[str, Any] = field(default_factory=dict)
    introduced_in: str | None = None


def badcase_id(run_id: str, category: str, symptom: str) -> str:
    """稳定 id：同一次 run 的同一个症状永远指向同一条记录。"""

    digest = hashlib.sha1(f"{category}|{symptom}".encode("utf-8")).hexdigest()[:10]
    return f"bc-{run_id}-{digest}"


def _case(
    ctx: BadCaseContext,
    *,
    category: str,
    severity: str,
    symptom: str,
    expected: str,
    actual: str,
    suspected_root_cause: str,
    trace_refs: list[str],
    detected_by: str = "rule",
) -> dict[str, Any]:
    return {
        "badcase_id": badcase_id(ctx.run_id, category, symptom),
        "run_id": ctx.run_id,
        "category": category,
        "severity": severity,
        "symptom": symptom,
        "expected": expected,
        "actual": actual,
        "suspected_root_cause": suspected_root_cause,
        # 规则只能给出"疑似"根因；verified/rejected 由人或 Evolution 的验证来改。
        "root_cause_status": "suspected",
        "detected_by": detected_by,
        "trace_refs": trace_refs,
        "analysis_status": "pending",
        "fixed_status": "unfixed",
        "introduced_in": ctx.introduced_in,
        "fixed_in": None,
    }


def _provider_failures(ctx: BadCaseContext) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for call in ctx.provider_calls:
        status = str(call.get("status") or "OK")
        if status == "OK":
            continue
        provider = str(call.get("provider") or "unknown")
        tool = str(call.get("tool") or "unknown")
        critical = provider in CRITICAL_PROVIDERS
        cases.append(
            _case(
                ctx,
                category=CATEGORY_PROVIDER_FAILURE,
                severity=SEVERITY_HIGH if critical else SEVERITY_MEDIUM,
                symptom=f"{provider}/{tool} 返回 {status}",
                expected=f"{provider}.{tool} 成功返回可用数据",
                actual=f"status={status}，返回 {call.get('returned')} 条；{call.get('note') or '无附加说明'}",
                suspected_root_cause=(
                    "该 Provider 未配置或第三方服务波动；也可能是查询参数不被接受"
                ),
                trace_refs=[f"{ctx.run_id}:provider:{provider}:{tool}"],
            )
        )
    return cases


def _degradation_case(ctx: BadCaseContext) -> list[dict[str, Any]]:
    """降级是"如实记录"而不是"必然 bug"，所以只整体记一条，避免刷屏。"""

    if not ctx.degradations:
        return []
    return [
        _case(
            ctx,
            category=CATEGORY_DEGRADATION,
            severity=SEVERITY_MEDIUM,
            symptom=f"本次 run 有 {len(ctx.degradations)} 项能力降级",
            expected="所有声明的能力都能正常参与规划",
            actual="；".join(ctx.degradations[:5]),
            suspected_root_cause="外部能力（模型 / Provider）不可用，已按确定性路径降级",
            trace_refs=[f"{ctx.run_id}:finalize"],
        )
    ]


def _plan_cases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    plan = ctx.plan
    if plan is None:
        return [
            _case(
                ctx,
                category=CATEGORY_HARD_CONSTRAINT,
                severity=SEVERITY_HIGH,
                symptom="plan 为空",
                expected="流程产出可用的 TripPlan",
                actual="status=SUCCESS 但没有 plan 对象",
                suspected_root_cause="finalize 阶段未装配 plan",
                trace_refs=[f"{ctx.run_id}:finalize"],
            )
        ]

    cases: list[dict[str, Any]] = []

    unresolved = [issue for issue in plan.warnings if issue.severity == "error" and not issue.resolved]
    for issue in unresolved:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_HARD_CONSTRAINT,
                severity=SEVERITY_HIGH,
                symptom=f"{issue.code}@day{issue.day_index}",
                expected="时间/营业时间冲突被自动修订或明确告知用户",
                actual=f"未解决：{issue.reason}",
                suspected_root_cause="排程参数或 Provider 时间数据不满足约束，且自动修订未能收敛",
                trace_refs=[f"{ctx.run_id}:check_feasibility"],
            )
        )

    budget = plan.budget
    if budget is not None:
        if budget.status == "over_budget":
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_BUDGET,
                    severity=SEVERITY_MEDIUM,
                    symptom=f"超出预算 ¥{budget.over_by:,.0f}",
                    expected=f"预计花费不超过预算 ¥{budget.budget_total:,.0f}",
                    actual=f"预计 ¥{budget.projected_total:,.0f}",
                    suspected_root_cause="候选里缺少足够便宜的替代项，或用户预算本身不可行",
                    trace_refs=[f"{ctx.run_id}:check_budget"],
                )
            )
        breakdown_total = sum(float(value or 0) for value in (budget.breakdown or {}).values())
        if budget.breakdown and abs(breakdown_total - float(budget.projected_total or 0)) >= 1.0:
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_BUDGET_MATH,
                    severity=SEVERITY_HIGH,
                    symptom="分项之和与预计总额不一致",
                    expected="breakdown 各项之和等于 projected_total",
                    actual=f"分项合计 ¥{breakdown_total:,.0f} ≠ 总额 ¥{budget.projected_total:,.0f}",
                    suspected_root_cause="预算重算后没有同步更新 breakdown",
                    trace_refs=[f"{ctx.run_id}:finalize"],
                )
            )

    if plan.transport is None or plan.transport.selected is None:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_MISSING_DISCLOSURE,
                severity=SEVERITY_HIGH,
                symptom="没有披露去程交通",
                expected="plan.transport.selected 给出真实去程方案",
                actual="transport 或 selected 为空",
                suspected_root_cause="12306 / 途牛均未返回可用车次机票，且未如实告知用户",
                trace_refs=[f"{ctx.run_id}:search_intercity_transport"],
            )
        )
    if plan.hotel is None or plan.hotel.selected is None:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_MISSING_DISCLOSURE,
                severity=SEVERITY_HIGH,
                symptom="没有披露住宿",
                expected="plan.hotel.selected 给出真实住宿方案",
                actual="hotel 或 selected 为空",
                suspected_root_cause="途牛住宿未返回可用候选",
                trace_refs=[f"{ctx.run_id}:search_hotels"],
            )
        )

    seen_symptoms: set[str] = set()
    #: 低严重度的"逐项"问题先收集再聚合成一条。
    #: 为什么聚合：一次 5 天行程有十几个安排，逐项各写一条会让单次 run 产出几十条
    #: Bad Case（实测 61 条），管理端翻不动、Evolution 也只能看到一堆只出现一次的簇。
    #: 聚合成"本 run 有 N 个点路线未核实（示例：A、B、C）"才是可行动的。
    evidence_gap_names: list[str] = []
    unverified_route_names: list[str] = []
    for day in plan.days:
        for item in day.items:
            if item.type in EVIDENCE_REQUIRED_TYPES and not item.evidence_ids:
                symptom = f"evidence_gap:{item.name}"
                if symptom not in seen_symptoms:
                    seen_symptoms.add(symptom)
                    evidence_gap_names.append(item.name)
            if item.price is not None and item.price_type == "realtime" and not item.source_ids:
                cases.append(
                    _case(
                        ctx,
                        category=CATEGORY_HALLUCINATED_PRICE,
                        severity=SEVERITY_HIGH,
                        symptom=f"realtime_price_without_source:{item.name}",
                        expected="标为实时价就必须能回溯到一次 Provider 调用",
                        actual=f"price={item.price} 但 source_ids 为空",
                        suspected_root_cause="价格在归并/排程阶段被写入但丢失了来源引用",
                        trace_refs=[f"{ctx.run_id}:build_initial_plan"],
                    )
                )
            leg = item.travel_from_previous
            if leg is not None and not leg.verified and leg.estimated:
                unverified_route_names.append(item.name)

    if evidence_gap_names:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_EVIDENCE_GAP,
                severity=SEVERITY_LOW,
                symptom=f"evidence_gap:{len(evidence_gap_names)} 个安排缺少攻略证据",
                expected="推荐项有攻略证据支撑",
                actual=(
                    f"{len(evidence_gap_names)} 个安排没有任何 evidence 关联，"
                    f"示例：{'、'.join(evidence_gap_names[:6])}"
                ),
                suspected_root_cause="社交攻略里没提到这些项，或抽取阶段漏掉了它们",
                trace_refs=[f"{ctx.run_id}:score_candidates"],
            )
        )
    if unverified_route_names:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_ROUTE_UNVERIFIED,
                severity=SEVERITY_LOW,
                symptom=f"unverified_route:{len(unverified_route_names)} 段路线未核实",
                expected="相邻两点之间的路线经高德核实",
                actual=(
                    f"{len(unverified_route_names)} 段路线为估算值，示例："
                    f"{'、'.join(unverified_route_names[:6])}"
                ),
                suspected_root_cause="高德路线接口降级，或两点之间没有可用路线",
                trace_refs=[f"{ctx.run_id}:verify_poi_and_routes"],
            )
        )
    return cases


def _journey_cases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    """引导式旅程的问题：用户点了但没生效、Discovery 白查了等等。

    这一类**只在"引导式来源"的 run 上判定**：一句话规划（quick）本来就没有前置选择，
    拿它去报"用户偏好被忽略"是误报 —— 而误报会让 Bad Case 表失去可信度。
    """

    journey = dict(ctx.journey or {})
    if str(journey.get("source") or "") != "guided":
        return []

    cases: list[dict[str, Any]] = []
    session_id = journey.get("source_session_id") or "—"
    selections = journey.get("place_selections") or {}
    discovery = dict(journey.get("discovery") or {})
    ref = f"{ctx.run_id}:parse_intent"

    # 1) 用户明确说"不感兴趣"的点进了行程 —— 这是最严重的一类：
    #    用户表达过的否定被系统忽略，比"排得不好"更伤信任。
    intruders = list(journey.get("rejected_in_plan") or [])
    if intruders:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_REJECTED_POI_IN_PLAN,
                severity=SEVERITY_HIGH,
                symptom=f"rejected_poi_in_plan:{len(intruders)} 个被排除的点仍在行程里",
                expected="用户在引导式旅程里标为「不感兴趣」的地点绝不进入最终行程",
                actual=f"这些 place_id 出现在行程里：{'、'.join(intruders[:6])}",
                suspected_root_cause="用户选择没有被作用到候选集合（selection 层未被调用）",
                trace_refs=[ref, f"{ctx.run_id}:score_candidates"],
            )
        )

    # 2) MUST 的点没进去：必须能说明"用户 MUST 了什么、为什么没进去"。
    missing = list(journey.get("must_missing") or [])
    if missing:
        reasons = _must_missing_reasons(ctx, missing)
        cases.append(
            _case(
                ctx,
                category=CATEGORY_MUST_POI_MISSING,
                severity=SEVERITY_HIGH,
                symptom=f"must_poi_missing:{len(missing)} 个必去点未进入行程",
                expected="用户标为「必去」且硬约束可行的地点应被优先安排；不可行时必须明确告知原因",
                actual=f"未进入行程的必去点：{'、'.join(missing[:6])}。原因：{reasons}",
                suspected_root_cause="时间窗/营业时间/预算等硬约束导致排不下，或用户偏好未参与排程",
                trace_refs=[ref, f"{ctx.run_id}:build_initial_plan", f"{ctx.run_id}:check_feasibility"],
            )
        )

    # 3) Discovery 自己就失败了（用户在前置阶段就没拿到候选）。
    failed = [name for name, info in discovery.items() if str((info or {}).get("status")) == "FAILED"]
    if failed:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_PREFETCH_FAILED,
                severity=SEVERITY_MEDIUM,
                symptom=f"prefetch_failed:{'/'.join(sorted(failed))}",
                expected="Discovery 的交通/酒店/攻略/地点四条线都能返回候选",
                actual="；".join(
                    f"{name}: {(discovery.get(name) or {}).get('error') or 'FAILED'}" for name in sorted(failed)
                ),
                suspected_root_cause="第三方数据源不可用，或 Discovery 查询参数不被接受",
                trace_refs=[ref],
            )
        )

    # 4) Discovery 查完了、正式 run 又自己查了一遍 —— 白花的钱与时间。
    available = list(journey.get("prefetch_available") or [])
    reused = dict(journey.get("prefetch_reused") or {})
    not_reused = [key for key in available if not reused.get(key)]
    if available and not_reused:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_PREFETCH_NOT_REUSED,
                severity=SEVERITY_MEDIUM,
                symptom=f"prefetch_not_reused:{'/'.join(sorted(not_reused))}",
                expected="正式 run 复用 Discovery 已查到的候选，不重复打同一批 Provider",
                actual=f"Discovery 已有 {sorted(available)}，但正式 run 又查询了 {sorted(not_reused)}",
                suspected_root_cause="Workflow 节点尚未消费 prefetch（见 docs/10 的待办）",
                trace_refs=[f"{ctx.run_id}:search_intercity_transport", ref],
            )
        )

    # 5) Discovery 一条候选都没有：用户会在 POI 页看到空列表。
    if discovery and all(
        int((info or {}).get("result_count") or 0) == 0 for info in discovery.values()
    ):
        cases.append(
            _case(
                ctx,
                category=CATEGORY_DISCOVERY_EMPTY,
                severity=SEVERITY_HIGH,
                symptom="discovery_empty",
                expected="Discovery 至少给出可选的交通/酒店/地点候选",
                actual=f"四条线结果都是 0（session {session_id}）",
                suspected_root_cause="目的地或日期口径不对，或所有数据源同时不可用",
                trace_refs=[ref],
            )
        )

    # 6) 用户做了选择但一个都没落到行程里（与"必去缺失"互补：这里是整体失效）。
    total_selected = sum(int(selections.get(key) or 0) for key in ("must", "want"))
    if total_selected and not missing and not intruders:
        applied = _applied_selection_ratio(ctx)
        if applied == 0:
            cases.append(
                _case(
                    ctx,
                    category=CATEGORY_USER_PREFERENCE_IGNORED,
                    severity=SEVERITY_MEDIUM,
                    symptom="user_preference_ignored",
                    expected="用户标记为必去/想去的地点至少有一部分进入行程",
                    actual=f"用户选了 {total_selected} 个（必去/想去），行程里一个都没有",
                    suspected_root_cause="用户选择未参与候选打分，或硬约束把候选全部挡掉",
                    trace_refs=[f"{ctx.run_id}:score_candidates"],
                )
            )

    # 7) 引导式的目的地/日期与最终行程不一致（会话被改过、或意图被覆盖）。
    #
    #    这一条与下面第 8 条都是**回归哨兵**：按当前设计它们不该被触发 ——
    #    基础信息不允许 PATCH（`PATCHABLE_FIELDS` 白名单），Guided 的 intent 也直接由
    #    会话字段构造、不再经模型解析。留着它们是为了让"哪天有人加了一条能改基础信息的
    #    路径"立刻变成一条 Bad Case，而不是等人肉发现"拿着成都的攻略排了重庆"。
    mismatch = _intent_mismatch(journey)
    if mismatch:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_GUIDED_INTENT_MISMATCH,
                severity=SEVERITY_HIGH,
                symptom=f"guided_intent_mismatch:{'/'.join(item[0] for item in mismatch)}",
                expected="正式规划用的 intent 必须等于用户在会话里确认过的结构化基础信息",
                actual="；".join(f"{field}: 确认={before!r} → 实际={after!r}" for field, before, after in mismatch),
                suspected_root_cause=(
                    "有人加了一条能改基础信息的路径（例如把 origin/destination/start_date/days 放进可 PATCH 字段），"
                    "或 Guided 的 intent 又被模型重新解析了一遍"
                ),
                trace_refs=[ref],
            )
        )

    # 8) 复用的却是**另一次行程**的 Discovery（最容易造成"行程张冠李戴"）。
    #    比第 7 条更严重：不只是意图不一致，而是真的把别人的数据用上了。
    stale = _stale_prefetch(journey)
    if stale:
        cases.append(
            _case(
                ctx,
                category=CATEGORY_DISCOVERY_STALE,
                severity=SEVERITY_HIGH,
                symptom=f"discovery_stale:{'/'.join(item[0] for item in stale)}",
                expected="复用的 Prefetch 必须属于本次行程（同目的地、同出发日期）",
                actual="；".join(f"{field}: 预取={before!r} → 本次={after!r}" for field, before, after in stale),
                suspected_root_cause=(
                    "Discovery 结果在基础信息变化后没有被判废，仍被正式 run 复用"
                    "（旧 session 的 prefetch 串到了新行程上）"
                ),
                trace_refs=[ref, f"{ctx.run_id}:search_intercity_transport"],
            )
        )

    return cases


#: 要逐字段比对的字段名。会话的 `basic_intent` 与 run 的 `planned_intent` 用同名键，
#: 所以这里只需要一份名单，不必再维护一张映射表。
_INTENT_FIELDS = ("destination", "start_date", "days", "travelers")


def _intent_mismatch(journey: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
    """会话确认的基础信息 vs 本次 run 的意图，逐字段比对（只返回不一致的）。"""

    confirmed = dict(journey.get("confirmed_basic_intent") or {})
    planned = dict(journey.get("planned_intent") or {})
    if not confirmed or not planned:
        return []
    mismatched: list[tuple[str, Any, Any]] = []
    for field in _INTENT_FIELDS:
        before = _normalize_intent_value(confirmed.get(field))
        after = _normalize_intent_value(planned.get(field))
        if before != after:
            mismatched.append((field, confirmed.get(field), planned.get(field)))
    return mismatched


def _stale_prefetch(journey: Mapping[str, Any]) -> list[tuple[str, Any, Any]]:
    """复用了 Prefetch，而它的目的地/出发日期与本次行程不同 —— 数据张冠李戴。"""

    if not journey.get("prefetch_reused_any"):
        return []
    return [
        (field, before, after)
        for field, before, after in _intent_mismatch(journey)
        if field in ("destination", "start_date")
    ]


def _normalize_intent_value(value: Any) -> Any:
    """把两侧口径拉平再比：日期统一成字符串、数字统一成 int、空值统一成 None。

    不做归一化会怎样：`date(2026,10,1)` 与 `"2026-10-01"`、`2` 与 `"2"` 会被判成不一致，
    于是哨兵天天误报 —— 误报的哨兵等于没有哨兵。
    """

    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return text


def _must_missing_reasons(ctx: BadCaseContext, missing: list[str]) -> str:
    """从行程的 notes 里找出"为什么没排上"的原话，别自己编一个理由。"""

    plan = ctx.plan
    if plan is None:
        return "没有产出行程"
    hints: list[str] = []
    for day in plan.days:
        for note in day.notes or []:
            if any(name in note for name in missing) and note not in hints:
                hints.append(note)
    if hints:
        return "；".join(hints[:3])
    return "行程里没有给出原因（可能是时间窗/营业时间限制，建议检查 check_feasibility 的告警）"


def _applied_selection_ratio(ctx: BadCaseContext) -> float | None:
    plan = ctx.plan
    if plan is None:
        return 0.0
    planned = {item.place_id for day in plan.days for item in day.items if item.place_id}
    selections = {
        str(place_id)
        for place_id in (getattr(ctx, "journey", {}).get("selected_ids") or [])
        if str(place_id).strip()
    }
    if not selections:
        return None
    if not planned:
        return 0.0
    return len(planned & selections) / len(selections)


def detect_badcases(ctx: BadCaseContext) -> list[dict[str, Any]]:
    """跑全部规则，返回按 (severity, category, symptom) 排序的 Bad Case 列表。"""

    cases = [
        *_provider_failures(ctx),
        *_degradation_case(ctx),
        *_plan_cases(ctx),
        *_journey_cases(ctx),
    ]
    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    cases.sort(key=lambda case: (order.get(case["severity"], 3), case["category"], case["symptom"]))
    return cases


def summarize(cases: list[dict[str, Any]]) -> dict[str, int]:
    """给 metrics.json / 管理端用的计数。"""

    summary = {"total": len(cases), SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 0, SEVERITY_LOW: 0}
    for case in cases:
        severity = str(case.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return summary
