"""Evolution：把待分析的 Bad Case 变成可验证的经验（接管任务 §8）。

这个模块的边界是**刻意画死的**：

    Bad Case（analysis_status=pending）
      → 按失败模式聚类
      → 提出一个**具体的**候选改动（配置杠杆，不是一句空话）
      → 用 Benchmark 在同一批用例上跑「改动前 vs 改动后」
      → ACCEPT / REJECT / ROLLBACK
      → 落一条 Experience 并标记这些 Bad Case 已分析

不做的事：
  * **不改代码、不自动上线**。候选改动只是"建议"，是否落地由人决定。
  * **不重复处理已分析的 Bad Case**。除非调用方显式把它们重新打开为 pending。
  * **不伪造指标**。拿不到评测结果（没有 Benchmark / 用例跑不起来）就 REJECT，
    并写明原因 —— 而不是凭"看起来应该会更好"给一个 ACCEPT。

评测入口是注入的：``measure(config_overrides) -> dict[str, float] | None``。
这样 ``app/`` 不依赖 ``benchmark/``（那里反过来依赖 app），同时单测可以注入假评测。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from app.config import PlannerTuning
from app.observability import now_iso
from app.store import TravelPlanStore

DECISION_ACCEPT = "ACCEPT"
DECISION_REJECT = "REJECT"
DECISION_ROLLBACK = "ROLLBACK"

#: 评测入口：给定一组配置覆盖，返回关键指标；返回 None 表示"这次评测没有可信结果"。
MeasureFn = Callable[[Mapping[str, str]], "dict[str, float] | None"]

#: 判定用的指标方向：True = 越大越好。只有出现在这里的指标才参与比较，
#: 避免"改了一个数字、某个无关指标漂了"被当成改进。
METRIC_DIRECTIONS: dict[str, bool] = {
    "hard_constraint_violation_rate": False,
    "date_mismatch_rate": False,
    "budget_math_error_rate": False,
    "hallucinated_price_rate": False,
    "missing_source_rate": False,
    "unsupported_recommendation_rate": False,
    "provider_success_rate": True,
    "evidence_coverage": True,
    "poi_verified_ratio": True,
    "preference_coverage": True,
    "daily_load_balance": True,
    "backtracking_score": True,
    "meal_time_quality": True,
    "pace_match": True,
    "category_diversity": True,
}

#: 只要有一个指标变差超过这个幅度就拒绝候选 —— 用"回归净收益"而不是单点提升做判断。
REGRESSION_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Lever:
    """一个可验证的候选改动。

    ``overrides`` 直接对应 ``app.config.override_env`` 能接受的键，所以"建议"是可执行的：
    把它写进环境变量就能复现同一份对照。``module`` 告诉人要改哪个文件（对应 docs/README
    的定位表）。``rationale`` 是这个杠杆为什么能缓解该类失败模式。
    """

    affected_module: str
    recommended_change: str
    rationale: str
    overrides: dict[str, str]


def _tuning_lever(module: str, change: str, rationale: str, **overrides: str) -> Lever:
    return Lever(
        affected_module=module,
        recommended_change=change,
        rationale=rationale,
        overrides={key: str(value) for key, value in overrides.items()},
    )


#: 失败模式 → 候选杠杆。**只列真的有对应旋钮的**；没有旋钮的失败模式会被判成
#: "需要人工改代码"，这比硬凑一个无效改动诚实得多。
LEVERS: dict[str, Lever] = {
    "hard_constraint": _tuning_lever(
        "app/planner.py / app/config.py",
        "下调每天停留点数上限（TP_MAX_ITEMS_PER_DAY=3）并重跑回归",
        "时间冲突多半来自一天排太满；减少每天点数能直接降低冲突概率",
        # 与推荐描述必须一致：候选改动是**可执行**的环境变量覆盖，不是一句建议。
        TP_MAX_ITEMS_PER_DAY="3",
    ),
    "route_unverified": _tuning_lever(
        "app/providers.py / app/planner.py",
        "提高路线核实优先级（高德失败时的降级路径需要人工确认）",
        "未核实路段是 Provider 降级的结果，不是排程参数能修的",
    ),
    "budget": _tuning_lever(
        "app/planner.py",
        "提高候选入选门槛（TP_MIN_CANDIDATE_SCORE），减少高价候选进入行程",
        "入选更严 → 行程里的高价项更少 → 更容易落在预算内",
        TP_MIN_CANDIDATE_SCORE="4",
    ),
    "budget_math": _tuning_lever(
        "app/planner.py",
        "预算重算与 breakdown 必须同源（代码修复，无配置杠杆）",
        "分项与总额不一致是代码缺陷，不是阈值问题",
    ),
    "evidence_gap": _tuning_lever(
        "app/prompts.py / app/workflow.py",
        "抽取与归一化阶段需要更完整的证据关联（代码修复）",
        "证据缺失来自抽取/关联逻辑，不是阈值",
    ),
    "missing_disclosure": _tuning_lever(
        "app/providers.py / app/workflow.py",
        "交通/住宿全失败时必须显式披露（现有告警已覆盖，需人工确认）",
        "披露缺失来自 Provider 无数据，属于降级路径",
    ),
    "hallucinated_price": _tuning_lever(
        "app/planner.py",
        "实时价必须带来源引用（代码修复）",
        "无来源的实时价是代码缺陷",
    ),
    "degradation": _tuning_lever(
        "app/config.py",
        "按降级项逐个排查（模型 / Provider）",
        "降级是外部能力问题，配置层只能调超时与开关",
    ),
    "provider_failure": _tuning_lever(
        "app/providers.py",
        "调整 Provider 优先级与 fallback 顺序（代码/配置修复）",
        "Provider 失败需要换主备或换查询参数，没有通用阈值",
    ),
    "provider_degraded": _tuning_lever(
        "app/config.py",
        "按降级项排查",
        "降级项需要逐项确认",
    ),
}


@dataclass(slots=True)
class BadCaseCluster:
    """一组同因的 Bad Case。"""

    key: str
    category: str
    badcase_ids: list[str]
    run_ids: list[str]
    symptom: str
    severity: str
    suspected_root_cause: str
    lever: Lever | None

    @property
    def size(self) -> int:
        return len(self.badcase_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "badcase_ids": self.badcase_ids,
            "run_ids": self.run_ids,
            "symptom": self.symptom,
            "severity": self.severity,
            "suspected_root_cause": self.suspected_root_cause,
            "affected_module": self.lever.affected_module if self.lever else None,
            "recommended_change": self.lever.recommended_change if self.lever else None,
            "overrides": dict(self.lever.overrides) if self.lever else {},
        }


def _cluster_key(category: str, symptom: str) -> str:
    """同类同症状归为一簇。

    symptom 里常带具体对象名与 id（例如 ``《宽窄巷子》同时出现在第 1 天和第 2 天``），
    直接当 key 会把同一个失败模式拆成一堆只出现一次的簇，Evolution 就永远看不出模式。
    所以取 category + symptom 的首段（冒号前）作为模式指纹。
    """

    head = symptom.split(":")[0].split("@")[0].strip()[:48]
    return f"{category}|{head}"


def cluster_badcases(badcases: list[dict[str, Any]]) -> list[BadCaseCluster]:
    """按 (category, 症状指纹) 聚类，簇内按严重度取代表值。"""

    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in badcases:
        buckets.setdefault(_cluster_key(str(item["category"]), str(item["symptom"])), []).append(item)

    order = {"high": 0, "medium": 1, "low": 2}
    clusters: list[BadCaseCluster] = []
    for key, items in buckets.items():
        items.sort(key=lambda item: (order.get(str(item.get("severity")), 3), item["badcase_id"]))
        representative = items[0]
        causes = Counter(
            str(item.get("suspected_root_cause") or "") for item in items if item.get("suspected_root_cause")
        )
        clusters.append(
            BadCaseCluster(
                key=key,
                category=str(representative["category"]),
                badcase_ids=[str(item["badcase_id"]) for item in items],
                run_ids=sorted({str(item["run_id"]) for item in items}),
                symptom=str(representative["symptom"]),
                severity=str(representative.get("severity") or "medium"),
                suspected_root_cause=(causes.most_common(1)[0][0] if causes else "未确定"),
                lever=LEVERS.get(str(representative["category"])),
            )
        )
    clusters.sort(key=lambda cluster: (order.get(cluster.severity, 3), -cluster.size, cluster.key))
    return clusters


def _compare(before: Mapping[str, float], after: Mapping[str, float]) -> tuple[bool, list[str], dict[str, float]]:
    """改动前 vs 改动后的净收益判定。返回 (是否接受, 说明, 差值)。"""

    deltas: dict[str, float] = {}
    regressions: list[str] = []
    improvements: list[str] = []
    for metric, higher_is_better in METRIC_DIRECTIONS.items():
        if metric not in before or metric not in after:
            continue
        delta = float(after[metric]) - float(before[metric])
        deltas[metric] = round(delta, 6)
        if abs(delta) <= REGRESSION_TOLERANCE:
            continue
        improved = delta > 0 if higher_is_better else delta < 0
        (improvements if improved else regressions).append(f"{metric} {delta:+.4f}")

    if regressions:
        return False, f"有指标回退：{'；'.join(regressions[:5])}", deltas
    if not improvements:
        return False, "没有任何指标改善（候选改动无效）", deltas
    return True, f"改善：{'；'.join(improvements[:5])}", deltas


def _experience_id(evolution_run_id: str, cluster_key: str) -> str:
    digest = hashlib.sha1(cluster_key.encode("utf-8")).hexdigest()[:10]
    return f"exp-{evolution_run_id}-{digest}"


def run_evolution(
    store: TravelPlanStore,
    *,
    measure: MeasureFn | None = None,
    limit: int = 50,
    evolution_run_id: str | None = None,
    analyze: bool = True,
) -> dict[str, Any]:
    """处理 ``analysis_status=pending`` 的 Bad Case。

    ``measure`` 是评测入口（通常由 API 层接到 Benchmark）。
    ``analyze=False`` 只做聚类与建议，不跑评测也不改动 Bad Case 状态 —— 供"先看一眼"
    的场景使用。
    """

    resolved_id = evolution_run_id or f"evo-{now_iso().replace(':', '').replace('-', '')[:15]}-{hashlib.sha1(str(limit).encode()).hexdigest()[:4]}"
    pending, total_pending = store.list_badcases(limit=limit, analysis_status="pending")

    steps: list[dict[str, str]] = [
        {"step": "collect", "detail": f"待分析 Bad Case {total_pending} 条，本次处理 {len(pending)} 条"}
    ]

    if not pending:
        store.save_evolution_run(
            {
                "evolution_run_id": resolved_id,
                "status": "SUCCESS",
                "badcase_count": 0,
                "experience_count": 0,
                "decision": "NOOP",
                "summary": "没有 analysis_status=pending 的 Bad Case，本轮不处理已分析过的历史记录",
                "steps": steps,
                "finished_at": now_iso(),
            }
        )
        return store.get_evolution_run(resolved_id) or {}

    clusters = cluster_badcases(pending)
    steps.append(
        {
            "step": "cluster",
            "detail": f"聚成 {len(clusters)} 个失败模式：" + "、".join(f"{c.category}×{c.size}" for c in clusters[:6]),
        }
    )

    # 先把 run 行落下来（状态 RUNNING）：Experience 有指向它的外键，而且管理端在长任务
    # 执行期间就应该能看到"正在进行"。结束后再补结论。
    store.save_evolution_run(
        {
            "evolution_run_id": resolved_id,
            "status": "RUNNING",
            "badcase_count": len(pending),
            "summary": "正在聚类与验证候选改动",
            "steps": steps,
        }
    )

    before_metrics: dict[str, float] = {}
    after_metrics: dict[str, float] = {}
    experiences: list[dict[str, Any]] = []
    decisions: list[str] = []

    for cluster in clusters:
        # 每个簇的候选改动都相对**基线**评测一次：基线只需要跑一遍。
        if cluster.lever is None:
            decision = DECISION_REJECT
            reason = f"该类失败没有配置杠杆（{cluster.category}），需要人工修改代码"
            deltas: dict[str, float] = {}
        else:
            if measure is None:
                decision = DECISION_REJECT
                reason = "没有可用的评测入口，无法验证候选改动"
                deltas = {}
            else:
                if not before_metrics:
                    measured = measure({})
                    before_metrics = dict(measured or {})
                measured_after = measure(cluster.lever.overrides)
                after_metrics = dict(measured_after or {})
                if not before_metrics or not after_metrics:
                    decision = DECISION_REJECT
                    reason = "评测没有返回可信指标（Baseline 或候选为空）"
                    deltas = {}
                else:
                    accepted, detail, deltas = _compare(before_metrics, after_metrics)
                    decision = DECISION_ACCEPT if accepted else DECISION_REJECT
                    reason = detail

        steps.append(
            {
                "step": "verify",
                "detail": (
                    f"{cluster.category}（{cluster.size} 条）→ {decision}：{reason}"
                    + (f"；候选改动：{cluster.lever.recommended_change}" if cluster.lever else "")
                ),
            }
        )
        decisions.append(decision)
        experiences.append(
            {
                "experience_id": _experience_id(resolved_id, cluster.key),
                "evolution_run_id": resolved_id,
                "source_badcases": cluster.badcase_ids,
                "failure_pattern": f"{cluster.category}：{cluster.symptom}（{cluster.size} 条，涉及 run {len(cluster.run_ids)} 个）",
                "verified_root_cause": (
                    f"{cluster.suspected_root_cause}（规则推测；{('评测对比结论：' + reason) if cluster.lever else '未做评测'}）"
                ),
                "affected_module": cluster.lever.affected_module if cluster.lever else "需要人工定位",
                "recommended_change": cluster.lever.recommended_change if cluster.lever else "需要人工分析",
                "before_metrics": before_metrics,
                "after_metrics": after_metrics,
                "decision": decision,
            }
        )

    for experience in experiences:
        store.save_experience(experience)

    if analyze:
        # 标记已分析：历史已分析的不再重复处理，除非显式重新打开为 pending。
        for cluster in clusters:
            for badcase_id in cluster.badcase_ids:
                store.update_badcase(badcase_id, {"analysis_status": "analyzed"})
        steps.append(
            {"step": "mark", "detail": f"已把 {len(pending)} 条 Bad Case 标记为 analysis_status=analyzed"}
        )

    accepted = sum(1 for decision in decisions if decision == DECISION_ACCEPT)
    overall = DECISION_ACCEPT if accepted else DECISION_REJECT
    summary = (
        f"{len(clusters)} 个失败模式：建议采纳 {accepted} 个、拒绝 {len(decisions) - accepted} 个"
        f"（本轮不自动改代码，也不自动上线）"
    )
    store.save_evolution_run(
        {
            "evolution_run_id": resolved_id,
            "status": "SUCCESS",
            "badcase_count": len(pending),
            "experience_count": len(experiences),
            "decision": overall,
            "summary": summary,
            "steps": steps,
            "before_metrics": before_metrics,
            "after_metrics": after_metrics,
            "created_at": (store.get_evolution_run(resolved_id) or {}).get("created_at"),
            "finished_at": now_iso(),
        }
    )
    return store.get_evolution_run(resolved_id) or {}


def tuning_snapshot() -> dict[str, Any]:
    """当前 Planner 阈值。管理端展示"Evolution 可用的旋钮"时直接读它。"""

    return PlannerTuning.from_env().public_dict()


@dataclass(slots=True)
class LeverCatalogEntry:
    category: str
    affected_module: str
    recommended_change: str
    overrides: dict[str, str] = field(default_factory=dict)


def lever_catalog() -> list[dict[str, Any]]:
    """全部可用杠杆（管理端"这个失败模式能改什么"面板的数据源）。"""

    return [
        asdict(LeverCatalogEntry(category=category, affected_module=lever.affected_module,
                                 recommended_change=lever.recommended_change,
                                 overrides=dict(lever.overrides)))
        for category, lever in sorted(LEVERS.items())
    ]
