"""规划结果层：run 的对外契约、成文、落盘产物与审计。

12 步固定 LangGraph（`travel_graph` / `node_*` / `execute_travel_run`）已经退役 ——
主规划路径是 `app/agent_runner.py::execute_agent_run`（Agent 自己在循环里调工具出 plan）。
本模块只留下来**两条路径共用**的那部分：

  * run 契约：`RunResult` / `STATUS_*` / `WORKFLOW_NAME` / `new_run_id`；
  * 成文与落盘：`_render_markdown` / `_write_artifacts` / `_write_run_artifacts`；
  * 审计与 Bad Case：`_build_audit` / `_detect_and_save_badcases` / `plan_payload`；
  * Discovery 过户：`_adopt_prefetch_sources` / `_materialize_city_evidence`；
  * 管理端时间线用的工具名/模型 tag → 阶段码映射（`PROVIDER_STAGE_MAP` / `LLM_STAGE_MAP`）。

这些函数都只读一个普通 dict（`Mapping[str, Any]`），不再依赖任何图 State：
Agent 路径把"本次真的发生了什么"如实塞进去，函数只取自己需要的键。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app import discovery, selection
from app.badcase import BadCaseContext, detect_badcases
from app.badcase import summarize as summarize_badcases
from app.config import current_config
from app.models import (
    Decision,
    Evidence,
    PreferenceProfile,
    TripIntent,
    TripPlan,
    coerce_str,
    utcnow,
)
from app.store import TravelPlanStore
from app.version import travelplan_commit

# ==================================================
# 常量
# ==================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"



WORKFLOW_NAME = "travelplan_workflow"


#: 这段描述会被 Workflow Router 拿去和用户 Query 做 BM25，所以要写用户会说的话。
WORKFLOW_DESCRIPTION = (
    "规划一次中国境内旅行：查大交通（火车票/机票）、查酒店、查景点与攻略、"
    "排每天行程、算预算、检查时间是否来得及，最后产出可执行的行程方案。"
)



STATUS_COMPLETED = "completed"


STATUS_FAILED = "failed"


#: 城市知识库复用过来的证据在 `sources.status` 上的取值。刻意**不复用** Provider 词表里的
#: `OK`：这条证据不是本次调用拿到的，写 OK 会让审计看起来像"这次真的打了 Provider"。
STATUS_CACHED = "CACHED"



# ==================================================
# 小工具
# ==================================================


def new_run_id() -> str:
    """`tp-` 前缀与前端 mock 数据的 run_id 形状保持一致（`tp-...`）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"tp-{stamp}-{uuid.uuid4().hex[:6]}"




def _stamp(stage: str, text: str) -> dict[str, Any]:
    return {"at": utcnow().isoformat(), "stage": stage, "text": text}




def _stage(stage_id: str, title: str, summary: str, steps: list[str], **stats: Any) -> dict:
    return {
        "id": stage_id,
        "title": title,
        "summary": summary,
        "steps": [step for step in steps if step],
        "stats": [{"label": key, "value": str(value)} for key, value in stats.items()],
    }




def _destination(intent: TripIntent) -> str | None:
    return intent.destination[0] if intent.destination else None




def _profile_payload(profile: Any) -> dict[str, Any] | None:
    """画像的 JSON 载荷（审计/前端用）；没生成过就返回 None，不编一个空画像。"""
    if isinstance(profile, PreferenceProfile):
        return profile.model_dump(mode="json")
    return None




def _render_markdown(plan: TripPlan, *, degradations: list[str], critic: dict) -> str:
    """确定性成文：所有数字直接取自 plan 对象，不经过模型改写。

    这是 plan.md 的"骨架"，模型写的那段说明只是附在它上面的散文 —— 这样即使模型
    胡说，用户照着下面的明细走依然是对的。
    """
    intent = plan.intent
    lines: list[str] = []
    lines.append(f"# {plan.run_id} · {' → '.join(intent.destination) or '未指定目的地'}")
    lines.append("")
    lines.append(f"- 原始需求：{plan.query}")
    lines.append(f"- 出发地：{intent.origin or '未提供'}")
    lines.append(f"- 时间：{intent.date_range}")
    lines.append(f"- 人数：{intent.travelers} 人")
    lines.append(
        f"- 预算：{'¥%g' % intent.budget_total if intent.budget_total else '用户未提供'}"
    )
    if intent.preferences:
        lines.append(f"- 偏好：{'、'.join(intent.preferences)}")
    lines.append("")

    transport = plan.transport
    if transport is not None and transport.selected is not None:
        selected = transport.selected
        lines.append("## 大交通")
        lines.append("")
        lines.append(f"- 去程：{selected.label}")
        if selected.departure_at and selected.arrival_at:
            lines.append(
                f"  - {selected.departure_at:%Y-%m-%d %H:%M} → {selected.arrival_at:%Y-%m-%d %H:%M}"
                f"（{selected.duration_minutes or '未知'} 分钟）"
            )
        lines.append(f"  - 门到门约 {selected.door_to_door_minutes or '未知'} 分钟（含提前到站/机场与市区接驳）")
        lines.append(f"  - 来源：{selected.provider}，查询于 {selected.fetched_at:%Y-%m-%d %H:%M} UTC")
        if transport.inbound_selected is not None:
            lines.append(f"- 回程：{transport.inbound_selected.label}")
        lines.append("")
        lines.append(f"选择理由：{transport.selection_reason}")
        if transport.alternatives:
            lines.append("")
            lines.append("其它候选：")
            for option in transport.alternatives:
                lines.append(f"- {option.label} —— {option.selection_reason or '未选中'}")
        lines.append("")

    hotel = plan.hotel
    if hotel is not None and hotel.selected is not None:
        selected = hotel.selected
        lines.append("## 住宿")
        lines.append("")
        lines.append(f"- {selected.name}（{selected.room_type or '房型未返回'}）")
        if selected.price_per_night is not None:
            lines.append(f"  - ¥{selected.price_per_night:g} / 晚（{selected.price_note or '起价'}），查询于 {selected.fetched_at:%Y-%m-%d %H:%M} UTC")
        else:
            lines.append("  - 本次未取得实时价格，住宿费用未计入预算")
        lines.append(f"  - 来源：{selected.provider}")
        lines.append("")
        lines.append(f"选择理由：{hotel.selection_reason}")
        lines.append("")

    lines.append("## 逐日行程")
    lines.append("")
    for day in plan.days:
        title = f"### 第 {day.day_index + 1} 天"
        if day.date:
            title += f" · {day.date.isoformat()}"
        if day.area:
            title += f" · {day.area}"
        lines.append(title)
        lines.append("")
        for item in day.items:
            span = f"{item.start_time or '—'}–{item.end_time or '—'}"
            lines.append(f"- **{span}** {item.name}（{item.type}）")
            if item.travel_from_previous is not None:
                leg = item.travel_from_previous
                leg_bits = [f"{leg.mode}"]
                if leg.duration_minutes is not None:
                    leg_bits.append(f"{leg.duration_minutes} 分钟")
                if leg.distance_meters:
                    leg_bits.append(f"{leg.distance_meters / 1000:.1f} km")
                if leg.estimated_cost is not None:
                    leg_bits.append(f"¥{leg.estimated_cost:g}")
                state_cn = "已核实（高德）" if leg.verified else "本次未取得实时路线，时长仅供参考"
                lines.append(f"  - 从上一站：{'、'.join(leg_bits)}（{state_cn}）")
            if item.price is not None:
                price_label = {"realtime": "实时价", "estimated": "估算", "unknown": "价格未知"}[item.price_type]
                lines.append(f"  - 费用：¥{item.price:g}（{price_label}）")
            if item.trust_score is not None:
                lines.append(f"  - Trust {item.trust_score:.0f} / AdRisk {item.ad_risk or 0:.0f}")
            if item.reason:
                lines.append(f"  - 理由：{item.reason}")
            if item.risk_note:
                lines.append(f"  - 风险：{item.risk_note}")
            if item.evidence_ids:
                lines.append(f"  - 证据：{', '.join(item.evidence_ids)}")
        for note in day.notes or []:
            lines.append(f"- 说明：{note}")
        lines.append("")

    budget = plan.budget
    lines.append("## 预算")
    lines.append("")
    lines.append(f"- 状态：{budget.status}")
    lines.append(f"- 真实花费（Provider 报价）：¥{budget.known_real_cost:,.0f}")
    lines.append(f"- 估算花费：¥{budget.estimated_cost:,.0f}")
    lines.append(f"- 预计总额：¥{budget.projected_total:,.0f}")
    if budget.budget_total is not None:
        lines.append(f"- 预算：¥{budget.budget_total:,.0f}，结余 ¥{(budget.remaining or 0):,.0f}")
    lines.append("")
    lines.append("| 分类 | 金额 | 价格类型 |")
    lines.append("| --- | --- | --- |")
    for category, amount in budget.breakdown.items():
        lines.append(f"| {category} | ¥{amount:,.0f} | {budget.breakdown_price_type.get(category, 'unknown')} |")
    lines.append("")
    for note in budget.price_notes:
        lines.append(f"- {note}")
    for suggestion in budget.optimization_suggestions:
        lines.append(f"- 优化建议：{suggestion}")
    lines.append("")

    if plan.warnings:
        lines.append("## 时间/营业时间问题")
        lines.append("")
        for issue in plan.warnings:
            mark = "已自动修订" if issue.resolved else "未解决"
            lines.append(
                f"- [{mark}] 第 {issue.day_index + 1} 天 {issue.code}：{issue.reason}"
                + (f"（{issue.original_start} → {issue.revised_start}）" if issue.original_start else "")
            )
        lines.append("")

    if critic.get("issues"):
        lines.append("## 审查意见")
        lines.append("")
        for issue in critic["issues"]:
            lines.append(
                f"- [{coerce_str(issue.get('severity'))}] {coerce_str(issue.get('scope'))}："
                f"{coerce_str(issue.get('problem'))} → {coerce_str(issue.get('suggestion'))}"
                f"（依据：{coerce_str(issue.get('evidence'), '无')}）"
            )
        lines.append("")

    if degradations:
        lines.append("## 需要你确认 / 本次未获取到")
        lines.append("")
        for note in dict.fromkeys(degradations):
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("以上价格均为查询时点报价，价格可能变化，请以实际下单页面为准。")
    lines.append("被标为「估算」的金额不是 Provider 报价；被标为「未取得实时路线」的路段时长仅供参考。")
    return "\n".join(lines)




def _write_artifacts(
    *, plan: TripPlan, plan_md: str, audit: dict, output_dir: Path
) -> dict[str, str]:
    target = output_dir / plan.run_id
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for filename, payload in (
        ("plan.json", json.dumps(plan.to_json_dict(), ensure_ascii=False, indent=2)),
        ("plan.md", plan_md),
        ("audit_report.json", json.dumps(audit, ensure_ascii=False, indent=2, default=str)),
    ):
        path = target / filename
        path.write_text(payload, encoding="utf-8")
        written[filename] = str(path)
    return written




def _provider_call_rows(hub: Any) -> list[dict[str, Any]]:
    """Provider 调用账本 → audit / Bad Case / Trace 共用的行结构。

    ``cached`` 与 ``fallback`` 必须带出来（Part C）：它们是"这次调用是复用的、
    还是本次真实发出、还是主源失败后的备选"的唯一事实来源。少带一个，Trace 与
    provider_calls 表就只能靠文案猜。
    """

    rows: list[dict[str, Any]] = []
    for entry in hub.audit_entries() if hub is not None else []:
        rows.append(
            {
                "provider": entry["provider"],
                "tool": entry["tool"],
                "query": entry.get("arguments") or {},
                "status": entry["status"],
                "returned": entry.get("item_count"),
                "duration_ms": entry.get("duration_ms"),
                "fetched_at": entry.get("fetched_at"),
                "source_id": entry.get("source_id"),
                "cached": bool(entry.get("cached")),
                "fallback": bool(entry.get("fallback")),
                "discarded": bool(entry.get("discarded")),
                "note": "；".join(entry.get("notes") or []) or entry.get("error") or None,
            }
        )
    return rows




def _detect_and_save_badcases(
    state: Mapping[str, Any], *, plan: TripPlan, degradations: list[str]
) -> list[dict[str, Any]]:
    """按规则检测本次 run 的 Bad Case 并落库。

    检测失败（或写库失败）绝不能让一次成功的规划变成失败 run —— Bad Case 是"事后复盘"
    的能力，不是行程的一部分。
    """

    try:
        if not current_config().badcase_enabled:
            return []
        ctx = BadCaseContext(
            run_id=state["run_id"],
            plan=plan,
            journey=_user_journey_summary(state, plan),
            provider_calls=_provider_call_rows(state.get("hub")),
            degradations=list(dict.fromkeys(degradations)),
            metrics={},
            # 让每条 Bad Case 记住"是哪一版代码造成的"，否则回归集无法判断是否还成立。
            introduced_in=travelplan_commit(),
        )
        cases = detect_badcases(ctx)
        state["store"].save_badcases(cases)
        return cases
    except Exception:
        return []




def _write_run_artifacts(
    *,
    run_id: str,
    store: TravelPlanStore,
    output_dir: Path,
    metrics: dict[str, Any],
    badcases: list[dict[str, Any]],
) -> dict[str, str]:
    """写 run 级产物：trace.jsonl / metrics.json / badcases.json。

    这三件必须等**整张图跑完**再写：finalize 节点执行时，它自己的 span 与
    run_metrics 都还没落库，此刻写出来的 trace 会缺最后一步、metrics 会缺总量。

    Trace 用 JSONL（一行一个 span，便于 grep/流式处理）；数据库负责查询与聚合。
    """

    target = output_dir / run_id
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    spans = store.get_trace_spans(run_id)
    trace_path = target / "trace.jsonl"
    trace_path.write_text(
        "".join(json.dumps(span, ensure_ascii=False, default=str) + "\n" for span in spans),
        encoding="utf-8",
    )
    written["trace.jsonl"] = str(trace_path)

    metrics_path = target / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    written["metrics.json"] = str(metrics_path)

    badcases_path = target / "badcases.json"
    badcases_path.write_text(
        json.dumps(
            {"run_id": run_id, "summary": summarize_badcases(badcases), "items": badcases},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    written["badcases.json"] = str(badcases_path)

    # audit_report.json 是"这份 run 有哪些产物"的索引，新增的三件要补进去，
    # 否则前端/人会以为产物还是三件。
    audit_path = target / "audit_report.json"
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            entries = audit.setdefault("artifacts", [])
            known = {item.get("filename") for item in entries if isinstance(item, dict)}
            for filename, label, fmt in (
                ("trace.jsonl", "运行 Trace", "jsonl"),
                ("metrics.json", "运行指标", "json"),
                ("badcases.json", "Bad Case", "json"),
            ):
                if filename not in known:
                    entries.append(
                        {"label": label, "filename": filename, "format": fmt, "available": True}
                    )
            audit_path.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except (OSError, ValueError):
            pass
    return written




#: Provider 调用 → 它属于哪个 workflow 节点。
#: 审计账本里没有阶段信息（真实 Hub 是通用调用器，不知道自己在哪个业务步骤），
#: 所以按"这个工具只会在哪个节点被调用"做静态映射；映射不到就**不挂父 span**，
#: 宁可树少一层，也不编一个假的归属。
PROVIDER_STAGE_MAP: dict[str, str] = {
    "search_trains": "search_intercity_transport",
    "search_flights": "search_intercity_transport",
    "search_hotels": "search_hotels",
    "search_xiaohongshu": "search_social_guides",
    "search_douyin": "search_social_guides",
    "web_search": "search_social_guides",
    "search_poi": "verify_poi_and_routes",
    "poi_detail": "verify_poi_and_routes",
    "geocode": "verify_poi_and_routes",
    "route": "verify_poi_and_routes",
    "search_scenic_tickets": "score_candidates",
}



#: LLM 调用的 tag → 所属节点。tag 带序号时按前缀匹配。
LLM_STAGE_MAP: dict[str, str] = {
    "parse_intent": "parse_intent",
    "query_expansion": "search_social_guides",
    "extract_places": "extract_and_normalize_places",
    "critic": "critic_and_revise",
    "final_answer": "finalize",
}




def _adopt_prefetch_sources(
    store: TravelPlanStore, run_id: str, bundle: Any
) -> list[dict[str, Any]]:
    """把 Discovery 的 Provider 调用写进本次 run 的 sources 表，并返回审计用的行。

    `source_id` 沿用 Discovery 的原值：被复用的 Evidence 引用的就是它，
    换 id 会让证据链断掉。会话与 run 一一对应，所以不会互相顶掉。
    """

    rows: list[dict[str, Any]] = []
    for entry in getattr(bundle, "provider_calls", None) or []:
        source_id = entry.get("source_id")
        try:
            store.save_source(
                run_id,
                {
                    "source_id": source_id,
                    "provider": entry.get("provider"),
                    "source_type": entry.get("source_type"),
                    "source_url": entry.get("source_url"),
                    "query": entry.get("arguments") or {},
                    "fetched_at": entry.get("fetched_at"),
                    "status": entry.get("status"),
                },
            )
        except Exception:  # noqa: BLE001 —— 过户失败只影响可追溯性，不该毁掉规划
            continue
        rows.append(
            {
                "provider": entry.get("provider"),
                "tool": entry.get("tool"),
                "query": entry.get("arguments") or {},
                "status": entry.get("status"),
                "returned": entry.get("item_count"),
                "duration_ms": entry.get("duration_ms"),
                "fetched_at": entry.get("fetched_at"),
                "source_id": source_id,
                "note": "Discovery 阶段已查询，本次 run 复用（未重复调用）",
                "reused": True,
            }
        )
    return rows




def _materialize_city_evidence(
    store: TravelPlanStore, run_id: str, bundle: Any
) -> list[dict[str, Any]]:
    """把城市知识库里的攻略正文过户成本次 run 的 sources + Evidence，返回审计行。

    **为什么必须重新分配 id**：`sources.source_id` 与 `evidence.evidence_id` 都是全局主键。
    同会话 Discovery→Run 之所以能沿用原 id，是因为"会话与 run 一一对应，不会互相顶掉"；
    跨会话复用不成立这个前提 —— 沿用原 id 会把**上一次** run 的 sources 行
    `INSERT OR REPLACE` 成本次 run 的，上一轮的证据链当场断掉。所以这里给每条证据新分配
    `{run_id}-cache-{n}` 的 source_id，同时把 `origin=city_cache` 与原始抓取时间写进
    `sources.normalized_json`，"这条证据从哪来、什么时候抓的"仍然可查。

    **必须在任何节点写 evidence/place 之前调用**：`evidence.source_id` 有外键，sources 行
    不存在时插入会被 `PRAGMA foreign_keys=ON` 直接拒掉。
    """

    cache_info = dict(getattr(bundle, "city_cache", None) or {})
    if str(cache_info.get("source") or "") != "city_cache":
        return []
    evidences = list(getattr(bundle, "evidences", None) or [])
    if not evidences:
        return []

    cached_at = str(cache_info.get("updated_at") or "")
    adopted: list[Evidence] = []
    rows: list[dict[str, Any]] = []
    failures = 0
    for index, evidence in enumerate(evidences, start=1):
        source_id = f"{run_id}-cache-{index:03d}"
        fetched_at = (
            evidence.fetched_at.isoformat() if evidence.fetched_at else None
        ) or cached_at or utcnow().isoformat()
        try:
            store.save_source(
                run_id,
                {
                    "source_id": source_id,
                    "provider": evidence.provider,
                    "source_type": evidence.source_type,
                    "source_url": evidence.source_url,
                    # 本次没有向 Provider 发过查询，所以 query 留空而不是编一个。
                    "query": {},
                    "fetched_at": fetched_at,
                    "status": STATUS_CACHED,
                    "normalized": {
                        "origin": "city_cache",
                        "city_cache_updated_at": cached_at,
                    },
                },
            )
        except Exception:  # noqa: BLE001 —— 登记失败只影响可追溯性，不该毁掉规划
            failures += 1
            # 登记失败时**丢掉**这条证据，而不是留一个空 source_id 继续用：
            # 留着它等于让 Trust / Ad Risk 拿一条无法追溯的文本算分。
            continue
        adopted.append(
            evidence.model_copy(update={"id": f"{source_id}-cached", "source_id": source_id})
        )
        rows.append(
            {
                "source_id": source_id,
                "evidence_id": f"{source_id}-cached",
                "provider": evidence.provider,
                "source_type": evidence.source_type,
                "source_url": evidence.source_url,
                "title": evidence.title,
                "fetched_at": fetched_at,
                "city_cache_updated_at": cached_at,
                "chars": len(evidence.text or ""),
            }
        )

    if not adopted:
        if failures:
            bundle.degradations.append(
                f"城市知识库的 {failures} 条攻略因来源登记失败被丢弃，本次攻略为空"
            )
        return []

    bundle.evidences = adopted
    age = f"，最早抓取于 {cached_at}" if cached_at else ""
    bundle.degradations.append(
        f"攻略正文复用城市知识库缓存（{len(adopted)} 条{age}），本次未重新检索社媒；"
        "正文未重新抓取，因此这批复用证据的抓取时间早于本次 run"
    )
    if failures:
        bundle.degradations.append(
            f"另有 {failures} 条缓存攻略因来源登记失败被丢弃"
        )
    return rows




# ==================================================
# audit_report.json
# ==================================================


def _build_audit(
    state: Mapping[str, Any],
    *,
    plan: TripPlan,
    decisions: list[Decision],
    degradations: list[str],
    prose_note: str,
) -> dict[str, Any]:
    """组装 audit_report.json（前端 AuditDrawer 直接消费的结构）。

    它比工程 trace 收敛：只保留"这次查了什么、谁给的、信不信、用了没有"这条链，
    不把内部 state 和 prompt 全文倒出去。
    """
    hub = state.get("hub")
    llm = state.get("llm")
    # 本次自己调的 + Discovery 过户来的（后者标 reused，避免读审计的人以为数据是凭空来的）
    provider_calls = _provider_call_rows(hub) + [
        {key: value for key, value in row.items() if key != "reused"}
        for row in (state.get("adopted_calls") or [])
    ]

    sources = plan.sources
    stages = list(state.get("stages") or [])
    artifacts = [
        {"label": "计划数据", "filename": "plan.json", "format": "json", "available": True},
        {"label": "行程说明", "filename": "plan.md", "format": "markdown", "available": True},
        {"label": "规划依据", "filename": "audit_report.json", "format": "json", "available": True},
    ]
    stages.append(
        _stage(
            "summary",
            "汇总",
            f"共调用外部数据源 {len(provider_calls)} 次、模型 {len(llm.calls) if llm else 0} 次；"
            f"产出 {len(plan.days)} 天行程、{len(decisions)} 条决策",
            [
                f"数据源调用 {len(provider_calls)} 次（成功 {sum(1 for call in provider_calls if call['status'] == 'OK')} 次）",
                f"模型调用 {len(llm.calls) if llm else 0} 次（成功 {sum(1 for call in (llm.calls if llm else []) if call.ok)} 次）",
                f"成文方式：{prose_note}",
                f"来源条目 {len(sources)} 条，证据 {len(state.get('evidences') or [])} 条"
                + (
                    f"，其中 {len(state.get('cached_evidence') or [])} 条来自城市知识库缓存"
                    if state.get("cached_evidence")
                    else ""
                ),
            ],
            数据源调用=len(provider_calls),
            模型调用=len(llm.calls) if llm else 0,
        )
    )

    timeline = list(state.get("timeline") or [])
    timeline.append(_stamp("summary", f"run {plan.run_id} 完成，状态 {state.get('status') or STATUS_COMPLETED}"))

    return {
        "run_id": plan.run_id,
        "query": plan.query,
        "generated_at": utcnow().isoformat(),
        "stages": stages,
        "decisions": [decision.model_dump(mode="json") for decision in decisions],
        "provider_calls": provider_calls,
        # 城市知识库复用过来的攻略正文。**不并入 provider_calls**：本次没有出网，
        # 并进去会让"数据源调用次数"与 Provider Health 看起来凭空多了几次调用。
        "cached_evidence": list(state.get("cached_evidence") or []),
        "llm_calls": llm.audit_entries() if llm is not None else [],
        "quality": (state.get("quality").to_dict() if state.get("quality") is not None else {}),
        "user_journey": _user_journey_summary(state, plan),
        "timeline": timeline,
        "artifacts": artifacts,
        "degradations": list(dict.fromkeys(degradations)),
        "evidence_summary": _evidence_summary(state, plan),
    }




def _user_journey_summary(state: Mapping[str, Any], plan: TripPlan) -> dict[str, Any]:
    """这次 run 的"用户前置选择"上下文（管理端 Run Detail 与 Bad Case 共用）。

    `prefetch_reused` 是**用证据推出来的**，不是标记位：如果这次 run 复用了 Discovery，
    它就不应该再自己打一遍交通/酒店/攻略。两边都发生 = 没复用（这正是要暴露的问题）。
    """

    intent = plan.intent
    selections = dict(intent.place_selections or {})
    bundle = state.get("prefetch")
    planned_ids = [item.place_id for day in plan.days for item in day.items if item.place_id]

    # Bad Case 不能只知道"用户选了几个点"：必须保留可与最终 plan 比对的身份。
    # 新入口用稳定 ID；旧客户端可能用名称、实体收敛后也可能仍带旧 ID，故按
    # selection_of 的同一套解析规则折叠到当前代表点。找不到候选的旧键仍保留，
    # 这样"用户选了但没有进入候选/行程"不会被静默吞掉。
    selected_ids = {
        str(key)
        for key, value in selections.items()
        if str(value).upper() in (selection.MUST, selection.WANT)
    }
    for place in state.get("places") or []:
        if selection.selection_of(place, selections) not in (selection.MUST, selection.WANT):
            continue
        selected_ids.difference_update({place.place_id, *place.merged_from, place.name})
        selected_ids.add(place.place_id)

    bundle_had: set[str] = set()
    if bundle is not None:
        if bundle.outbound or bundle.inbound:
            bundle_had.add("transport")
        if bundle.hotels:
            bundle_had.add("hotels")
        if bundle.evidences:
            bundle_had.add("social")
        if bundle.places:
            bundle_had.add("places")
    ran_itself = {
        str(entry.get("tool"))
        for entry in _provider_call_rows(state.get("hub"))
    }
    # 这些工具一旦出现在本次 run 的账本里，就说明它自己又查了一遍。
    #
    # 匹配的是**真实 Tool 名**，而且要把两套命名都列上：Discovery 走插件自带的工具名
    # （`flight` / `hotel` / `xiaohongshu`），正式 run 走 Hub 的包装名
    # （`tuniu_search_flights` / `search_xiaohongshu`），火车还多一个 MCP 的
    # `railway_12306_get-tickets`。上一版这里写的是 `search_trains` 这类 **Hub 方法名**，
    # 三类名字里一个都匹配不上 —— 后果是"本次真的补查了交通"会被审计写成
    # "复用了 Discovery"，读的人据此以为预取生效，实际那次 run 白等了一遍 Provider。
    tools_of_line = {
        "transport": {
            "get-tickets",
            "railway_12306_get-tickets",
            "tuniu_search_trains",
            "flight",
            "tuniu_search_flights",
        },
        "hotels": {"hotel", "tuniu_search_hotels"},
        "social": {
            "xiaohongshu",
            "search_xiaohongshu",
            "search_xhs_via_mediacrawler",
            "douyin",
            "search_douyin",
            "search_douyin_via_mediacrawler",
            "web_search",
        },
        "places": {"search_poi"},
    }
    self_queried = {
        key: bool(names & ran_itself) for key, names in tools_of_line.items()
    }
    reused = {
        key: (key in bundle_had and not self_queried[key]) for key in ("transport", "hotels", "social", "places")
    }
    # 每条线的交接结果，三种之一：
    #   reused          —— 用了 Discovery 已经查好的，本次没有重复打 Provider
    #   fallback_query  —— 本次自己补查了，并且查到了东西
    #   unavailable     —— 本次自己补查了，但仍然没有可用结果（要如实说出来）
    stage_info = dict(getattr(bundle, "discovery", None) or {}) if bundle is not None else {}
    # Agent 路径会把工具结果收敛进最终 plan / sources，而不是填回固定流程的
    # state["evidences"] / state["places"]。只看后者会把已有攻略和地点误报成「无可用结果」。
    source_providers = {str(source.provider or "").lower() for source in plan.sources}
    resolved = {
        "transport": bool(plan.transport is not None and plan.transport.selected is not None),
        "hotels": bool(plan.hotel is not None and plan.hotel.selected is not None),
        "social": bool(state.get("evidences"))
        or bool(source_providers & {"tikhub", "mediacrawler", "tavily"}),
        "places": bool(state.get("places"))
        or any(item.place_id for day in plan.days for item in day.items),
    }
    handoff: dict[str, Any] = {}
    for key, label in (
        ("transport", "交通"),
        ("hotels", "酒店"),
        ("social", "攻略"),
        ("places", "地点"),
    ):
        if reused[key]:
            status = "reused"
        elif resolved[key]:
            status = "fallback_query"
        else:
            status = "unavailable"
        handoff[key] = {
            "label": label,
            "handoff": status,
            "prefetch_available": key in bundle_had,
            "queried_in_run": self_queried[key],
            "resolved": resolved[key],
            **{k: v for k, v in (stage_info.get(key) or {}).items() if k in ("status", "result_count", "duration_ms", "degraded")},
        }
    return {
        # 每个阶段的交接状态（管理端与 Bad Case 都看这个）
        "discovery": handoff,
        "grace_waited_ms": int(getattr(bundle, "grace_waited_ms", 0) or 0) if bundle is not None else 0,
        "source": state.get("source") or "quick",
        "source_session_id": state.get("source_session_id"),
        # --- 角色 B 的决策产物（contract 2 的 run 侧）：画像 / 住宿区域 / 候选池 ---
        # 与 Discovery 的字段同名同结构，管理端与前端不必区分是哪个入口来的。
        "profile": _profile_payload(state.get("profile")),
        "hotel_areas": list(state.get("hotel_areas") or []),
        "hotel_area_selected": state.get("hotel_area_selected"),
        "poi_pools": discovery.split_poi_pools(list(state.get("places") or [])),
        "transport_mode": intent.transport_mode,
        "transport_priority": intent.transport_priority,
        "hotel_priority": intent.hotel_priority,
        "pace": intent.pace,
        "place_selections": {
            "must": sum(1 for value in selections.values() if str(value).upper() == "MUST"),
            "want": sum(1 for value in selections.values() if str(value).upper() == "WANT"),
            "reject": sum(1 for value in selections.values() if str(value).upper() == "REJECT"),
        },
        "selected_ids": sorted(selected_ids),
        "prefetch_available": sorted(bundle_had),
        "prefetch_reused": reused,
        "prefetch_reused_any": any(reused.values()),
        "prefetch_status": (bundle.discovery_status if bundle is not None else None),
        # 分阶段原始状态（status/duration/result_count…）保留在 prefetch_stages 下，
        # "discovery" 留给交接结论（reused/fallback_query/unavailable），避免同名两义。
        "prefetch_stages": dict(getattr(bundle, "discovery", None) or {}) if bundle is not None else {},
        "rejected_in_plan": selection.rejected_place_intruders(planned_ids, selections),
        "must_missing": selection.must_place_shortfall(
            planned_ids, selections, list(state.get("places") or [])
        ),
        # 会话确认时的结构化基础信息 vs 本次 run 实际用的 intent。
        #
        # 两者**本该永远一致**：Guided 的 intent 直接由会话字段构造（`intent_from_basic`），
        # 不再经模型解析，而基础信息也不允许 PATCH。所以这两块数据是给 Bad Case 当
        # **回归哨兵**用的 —— 一旦谁加了一条能改基础信息、或让模型再解析一次 intent 的
        # 路径，我们不用等人肉发现"拿着成都的攻略排了重庆"。
        "confirmed_basic_intent": (
            dict(getattr(bundle, "basic_intent", None) or {}) if bundle is not None else {}
        ),
        "planned_intent": {
            "destination": _destination(intent),
            "start_date": intent.start_date.isoformat() if intent.start_date else None,
            "days": intent.days,
            "travelers": intent.travelers,
        },
    }




def _evidence_summary(state: Mapping[str, Any], plan: TripPlan) -> dict[str, int]:
    """给前端的可信度汇总（FRONTEND_DESIGN §9）：三个数，全部来自已算好的结果。"""
    places = list(state.get("places") or [])
    scheduled = {
        item.place_id for day in plan.days for item in day.items if item.place_id is not None
    }
    return {
        "sources_used": len(plan.sources),
        "places_verified": sum(1 for place in places if place.amap_verified),
        "low_trust_filtered": max(0, len(state.get("candidate_scores") or {}) - len(scheduled)),
    }




# ==================================================
# 对外入口
# ==================================================


@dataclass(slots=True)
class RunResult:
    """一次规划的返回值。`status` 只有三种，前端/CLI 按它分支即可。"""

    run_id: str
    status: str
    plan: TripPlan | None = None
    plan_md: str = ""
    audit: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    degradations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def clarification(self) -> str:
        return self.error or ""




def plan_payload(plan: TripPlan, store: TravelPlanStore | None = None) -> dict[str, Any]:
    """给前端的 TripPlan JSON：在 plan.json 之上补 evidence 与 evidence_summary。

    `plan.json` 本身保持 PRD §28 的形状不变（sources 已经在里面），evidence 明细
    单独通过 store 取 —— 因为它是"证据库"的内容，不是"计划"的内容。
    """
    payload = plan.to_json_dict()
    if store is not None:
        payload["evidence"] = [
            {
                "id": row["evidence_id"],
                "source_type": row["source_type"],
                "provider": row["provider"],
                "source_id": row["source_id"],
                "source_url": row["source_url"],
                "title": row["title"],
                "author": row["author"],
                "published_at": row["published_at"],
                "fetched_at": row["fetched_at"],
                "text": row["text"],
                "place_mentions": row["place_mentions"],
                "specific_dishes": row["specific_dishes"],
                "raw_metrics": row["raw_metrics"],
            }
            for row in store.get_evidence(plan.run_id)
        ]
    return payload
