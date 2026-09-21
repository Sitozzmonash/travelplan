"""把一次 run 的工程数据转录成"人能从第一行读到最后一行的执行轨迹"。

为什么不直接给前端原始 trace：`trace_spans` 是按 component 分类的工程结构
（workflow / llm / tool / provider / …），父子里还夹着一层"Provider 分组 span"，
人读起来要在三张表之间来回跳。本模块只做一件事：把 span、Provider 账本、决策、
校验结果与 Bad Case **归并成同一条时间轴上带类型的事件**：

    SYSTEM     运行配置（模型 / 预算 / 超时）
    USER       用户到底要什么（原始请求 + 结构化意图 + MUST/WANT/REJECT）
    CONTEXT    本次带进了什么上下文（PlanningSession / Prefetch / Discovery 数量）
    ASSISTANT  模型每一步做了什么（tag / 模型 / 耗时 / 状态）
    TOOL       调了哪个工具、参数、返回多少、是不是命中了缓存
    PROVIDER   底层数据源的真实状态（SUCCESS / EMPTY / TIMEOUT / AUTH ERROR / FALLBACK）
    VALIDATION Python 硬校验（时间、营业时间、返程、硬预算、MUST / REJECT）
    ERROR      异常 / 降级 / fallback / Bad Case 触发

三条硬约定
----------
1. **同一份事实只读一次**：不新增埋点、不新建日志表。所有输入都来自既有 store 读接口。
2. **拿不到就写拿不到**：模型输入/输出正文**没有**被持久化（trace 只记 tag / 模型 /
   字符数 / 耗时），因此事件里 ``input_preview`` 只能是 ``None``，并在 ``notes`` 里
   说明原因 —— 不拿摘要冒充原文。
3. **预览必须脱敏 + 截断**：Tool 参数与返回来自第三方，可能带 Key 或几百 KB 正文。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, Depends, HTTPException

from .models import TripPlan
from .observability import now_iso
from .providers import _scrub_secrets
from .store import TravelPlanStore
from .workflow import LLM_STAGE_MAP, PROVIDER_STAGE_MAP

#: 八类事件。前端按它做筛选与配色，因此词表只在这里定义一次。
EVENT_TYPES: tuple[str, ...] = (
    "SYSTEM",
    "USER",
    "CONTEXT",
    "ASSISTANT",
    "TOOL",
    "PROVIDER",
    "VALIDATION",
    "ERROR",
)

EVENT_TYPE_LABELS: dict[str, str] = {
    "SYSTEM": "系统",
    "USER": "用户",
    "CONTEXT": "上下文",
    "ASSISTANT": "助手",
    "TOOL": "工具",
    "PROVIDER": "Provider",
    "VALIDATION": "校验",
    "ERROR": "错误",
}

#: 顶部轨道概览的行。顺序即绘制顺序。
TRACK_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("model", "模型", ("ASSISTANT",)),
    ("tool", "工具", ("TOOL",)),
    ("provider", "Provider", ("PROVIDER",)),
    ("validation", "校验", ("VALIDATION",)),
    ("error", "错误", ("ERROR",)),
)

#: 预览的最大长度。第三方返回动辄几百 KB，全铺出来这个页面就没法看了。
_PREVIEW_LIMIT = 400
#: 单次返回的事件上限：超长 run 只截断展示，不静默丢数据。
_MAX_EVENTS = 800

_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|authorization|secret|password|cookie)"
    r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-./+]{6,})"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-./+=]{6,}")

#: 状态里算"这里出过问题"的那些。
_PROBLEM_STATUSES = ("FAILED", "TIMEOUT", "ERROR", "AUTH_ERROR", "RATE_LIMITED", "INVALID")


def _scrub(text: str) -> str:
    """兜底脱敏：环境变量里的 Key + 常见凭据字样。

    复用 ``providers._scrub_secrets`` 而不是再写一份：那是全仓唯一一份"当前进程里
    各个 Key 长什么样"的清单，各写一份必然会漏掉新加的 Key。
    """

    text = _scrub_secrets(text)
    text = _BEARER_PATTERN.sub("Bearer ***", text)
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)


def _preview(value: Any, *, limit: int = _PREVIEW_LIMIT) -> str | None:
    """把任意值压成一段可安全展示的预览。空值返回 ``None``（而不是空字符串）。"""

    if value is None or value == {} or value == [] or value == "":
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = _scrub(text.strip())
    if not text:
        return None
    if len(text) > limit:
        return f"{text[:limit]}…（已截断，原文 {len(text)} 字符）"
    return text


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ms_between(start: Any, end: Any) -> int | None:
    left, right = _parse_ts(start), _parse_ts(end)
    if left is None or right is None:
        return None
    return int((right - left).total_seconds() * 1000)


def _span_duration(span: Mapping[str, Any]) -> int | None:
    """span 的耗时。

    `trace_spans` **没有** duration_ms 列：耗时写在 attributes 里（tool / llm 都写），
    所以只能先看 attributes，再退回"用起止时间相减"。只看列的话所有事件的耗时都是 null，
    顶部轨道的时长条会全部塌成 0 宽。
    """

    attributes = span.get("attributes") or {}
    value = attributes.get("duration_ms")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(span.get("duration_ms"), (int, float)):
        return int(span["duration_ms"])
    return _ms_between(span.get("started_at"), span.get("finished_at"))


def _stage_for_tool(tool: str) -> str | None:
    """工具名 → 阶段。先精确匹配，再按后缀匹配。

    后缀匹配是必要的：账本里的工具名带 Provider 前缀（``tuniu_search_hotels``、
    ``tikhub_search_xiaohongshu``），而 `PROVIDER_STAGE_MAP` 的键是不带前缀的
    ``search_hotels``。只精确匹配会让绝大多数工具挂不上阶段，轨迹的分阶段筛选就废了。
    """

    if not tool:
        return None
    exact = PROVIDER_STAGE_MAP.get(tool)
    if exact:
        return exact
    for name, stage_id in PROVIDER_STAGE_MAP.items():
        if tool.endswith(name):
            return stage_id
    return None


# ======================================================================
# 阶段归属
# ======================================================================


class _StageIndex:
    """把 span / 工具 / 模型调用归到 workflow 阶段。

    归属规则有三条，优先用最可靠的那条：
      1. 父 span 以 ``:{stage_id}`` 结尾 —— trace 自己写下的父子关系；
      2. 工具名 → 阶段（``PROVIDER_STAGE_MAP``）—— 静态映射，来自 workflow；
      3. 模型 tag → 阶段（``LLM_STAGE_MAP``），按前缀匹配（tag 带序号）。
    三条都命不中就不挂阶段（前端显示"未归属"），而不是随便猜一个。
    """

    def __init__(self, stages: Sequence[Mapping[str, Any]], run_id: str) -> None:
        self.order: list[str] = []
        self.titles: dict[str, str] = {}
        self.status: dict[str, Any] = {}
        for stage in stages:
            stage_id = str(stage.get("stage_id") or "")
            if not stage_id:
                continue
            if stage_id not in self.order:
                self.order.append(stage_id)
            self.titles[stage_id] = str(stage.get("message") or stage_id) or stage_id
            self.status[stage_id] = stage.get("status")
        self._index = {stage_id: position for position, stage_id in enumerate(self.order)}
        self._run_id = run_id

    def of_span(self, span: Mapping[str, Any]) -> str | None:
        parent = str(span.get("parent_span_id") or "")
        prefix = f"{self._run_id}:"
        if parent.startswith(prefix):
            tail = parent[len(prefix) :].split(":", 1)[0]
            if tail in self._index:
                return tail
        attributes = span.get("attributes") or {}
        component = str(span.get("component") or "")
        if component in ("tool", "provider", "mcp"):
            tool = str(attributes.get("tool") or span.get("name") or "")
            mapped = _stage_for_tool(tool)
            if mapped in self._index:
                return mapped
        if component == "llm":
            tag = str(attributes.get("tag") or span.get("name") or "")
            for prefix_key, stage_id in LLM_STAGE_MAP.items():
                if tag.startswith(prefix_key) and stage_id in self._index:
                    return stage_id
        return None

    def ordinal(self, stage_id: str | None) -> int | None:
        if not stage_id:
            return None
        return self._index.get(stage_id)


# ======================================================================
# 事件构造
# ======================================================================


def _event(
    *,
    event_id: str,
    run_id: str,
    event_type: str,
    title: str,
    summary: str | None = None,
    stage: str | None = None,
    round_index: int | None = None,
    status: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    parent_event_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    tool: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost: float | None = None,
    cache_hit: bool | None = None,
    prefetch_reused: bool | None = None,
    fallback: bool | None = None,
    input_preview: str | None = None,
    output_preview: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    notes: Iterable[str] = (),
    badcases: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """统一事件形状（文档 §6 的字段表）。所有键**一定存在**，缺的给 None。

    为什么要让空键也在：前端按类型渲染时不必写一堆 ``?.`` 判断，也不会因为
    某个字段这次没值就把整块渲染掉。
    """

    return {
        "event_id": event_id,
        "run_id": run_id,
        "round_index": round_index,
        "event_type": event_type,
        "event_type_label": EVENT_TYPE_LABELS.get(event_type, event_type),
        "stage": stage,
        "title": title,
        "summary": summary,
        "input_preview": input_preview,
        "output_preview": output_preview,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "parent_event_id": parent_event_id,
        "model": model,
        "provider": provider,
        "tool": tool,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost": cost,
        "cache_hit": cache_hit,
        "prefetch_reused": prefetch_reused,
        "fallback": fallback,
        "metadata": dict(metadata or {}),
        "notes": list(notes),
        "badcases": [dict(case) for case in badcases],
    }


def _badcase_brief(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "badcase_id": str(case.get("badcase_id")),
        "category": case.get("category"),
        "severity": case.get("severity"),
        "symptom": _preview(case.get("symptom"), limit=200),
        "analysis_status": case.get("analysis_status"),
    }


def _tool_events(
    *,
    run_id: str,
    spans: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    stages: _StageIndex,
    badcase_by_ref: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """工具调用事件：span 给"发生了什么"，source 给"参数与返回"。"""

    sources_by_id = {str(source.get("source_id")): source for source in sources}
    events: list[dict[str, Any]] = []
    matched: set[str] = set()
    for span in spans:
        if str(span.get("component")) not in ("tool", "mcp"):
            continue
        attributes = span.get("attributes") or {}
        source_id = str(attributes.get("source_id") or "")
        source = sources_by_id.get(source_id)
        if source:
            matched.add(source_id)
        tool = str(attributes.get("tool") or span.get("name") or "tool")
        provider = str(attributes.get("provider") or "") or None
        stage = stages.of_span(span)
        status = str(attributes.get("status") or span.get("status") or "UNKNOWN")
        returned = attributes.get("returned")
        duration = _span_duration(span)
        notes: list[str] = []
        if str(attributes.get("hedge_discarded") or "").lower() in ("true", "1"):
            notes.append("这条并发请求被对冲策略丢弃（另一条先返回）")
        if str(attributes.get("reused_from_discovery") or "").lower() in ("true", "1"):
            notes.append("结果来自 Discovery 阶段，本次 run 没有再打一次 Provider")
        if source and source.get("source_type") == "discovery":
            notes.append("该调用属于 Discovery 阶段，本次 run 复用其结果")

        events.append(
            _event(
                event_id=str(span.get("span_id")),
                run_id=run_id,
                event_type="TOOL",
                title=f"{provider or 'Provider'} · {tool}" if provider else tool,
                summary=(
                    f"{status}"
                    + (f"，返回 {returned} 条" if isinstance(returned, int) else "")
                    + (f"，{round(duration / 1000, 1)}s" if duration else "")
                ),
                stage=stage,
                round_index=stages.ordinal(stage),
                status=status,
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=_span_duration(span),
                parent_event_id=span.get("parent_span_id"),
                provider=provider,
                tool=tool,
                cache_hit=bool(attributes.get("cache_hit")) if "cache_hit" in attributes else None,
                prefetch_reused=(
                    bool(attributes.get("prefetch_reused")) if "prefetch_reused" in attributes else None
                ),
                fallback=(
                    bool(attributes.get("fallback_query")) if "fallback_query" in attributes else None
                ),
                input_preview=_preview(
                    (source or {}).get("query") or attributes.get("query")
                ),
                output_preview=_preview(
                    ((source or {}).get("normalized") or {}).get("items")
                    or attributes.get("note")
                    or returned
                ),
                metadata={
                    "source_id": source_id or None,
                    "source_url": (source or {}).get("source_url"),
                    "returned": returned,
                    "timeout": attributes.get("timeout"),
                    "provider_called": attributes.get("provider_called"),
                    "marker": attributes.get("marker"),
                },
                notes=notes,
                badcases=badcase_by_ref.get(str(span.get("span_id")), []),
            )
        )
    return events


def _provider_events(
    *,
    run_id: str,
    spans: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    matched_source_ids: set[str],
    stages: _StageIndex,
) -> list[dict[str, Any]]:
    """底层数据源事件：分组 span 给"这家 Provider 调了几次"，
    未被任何 tool span 覆盖的 source 行按"复用 Discovery / 未落 span"单独列出来。"""

    events: list[dict[str, Any]] = []
    grouped_stage: dict[str, Counter[str]] = defaultdict(Counter)
    for span in spans:
        if str(span.get("component")) not in ("tool", "mcp"):
            continue
        stage = stages.of_span(span)
        provider = str((span.get("attributes") or {}).get("provider") or "")
        if stage and provider:
            grouped_stage[provider][stage] += 1

    for span in spans:
        if str(span.get("component")) not in ("provider", "mcp"):
            continue
        attributes = span.get("attributes") or {}
        provider = str(attributes.get("provider") or span.get("name") or "")
        stage = grouped_stage.get(provider, Counter()).most_common(1)
        stage_id = stage[0][0] if stage else None
        calls = attributes.get("calls")
        events.append(
            _event(
                event_id=str(span.get("span_id")),
                run_id=run_id,
                event_type="PROVIDER",
                title=provider or "Provider",
                summary=f"本次调用 {calls} 次"
                + (f"，传输 {attributes.get('transport')}" if attributes.get("transport") else ""),
                stage=stage_id,
                round_index=stages.ordinal(stage_id),
                status=str(span.get("status") or "SUCCESS"),
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=_span_duration(span),
                parent_event_id=span.get("parent_span_id"),
                provider=provider or None,
                metadata={"calls": calls, "transport": attributes.get("transport")},
            )
        )

    orphan_sources = [
        source
        for source in sources
        if str(source.get("source_id")) not in matched_source_ids
    ]
    for source in orphan_sources:
        status = str(source.get("status") or "UNKNOWN")
        reused = str(source.get("source_type")) == "discovery"
        events.append(
            _event(
                event_id=f"{run_id}:source:{source.get('source_id')}",
                run_id=run_id,
                event_type="PROVIDER",
                title=f"{source.get('provider')} · {source.get('source_type') or 'call'}",
                summary=f"{status}（未落到 tool span，按审计账本列出）",
                status=status,
                started_at=source.get("fetched_at"),
                finished_at=source.get("fetched_at"),
                provider=str(source.get("provider") or "") or None,
                prefetch_reused=reused or None,
                input_preview=_preview(source.get("query")),
                output_preview=_preview(
                    (source.get("normalized") or {}).get("items") or source.get("raw")
                ),
                notes=["这条调用没有对应的 tool span（多为 Discovery 复用或早期版本记录）"],
                metadata={"source_type": source.get("source_type"), "source_url": source.get("source_url")},
            )
        )
    return events


def _assistant_events(
    *,
    run_id: str,
    spans: Sequence[Mapping[str, Any]],
    stages: _StageIndex,
    badcase_by_ref: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """模型侧事件：一次 LLM 调用就是一条。

    为什么**不**把 decisions 表铺进来：那是逐实体（每个 POI / 酒店 / 方案）的取舍记录，
    一次 run 有一两百条，铺进时间轴会把真正的执行过程淹没。决策链在运行详情页
    已有独立区块（``/admin/runs/{run_id}`` 的 ``decisions``），这里只回答"模型在哪一刻
    被调用、花了多久"。
    """

    events: list[dict[str, Any]] = []
    for span in spans:
        if str(span.get("component")) != "llm":
            continue
        attributes = span.get("attributes") or {}
        stage = stages.of_span(span)
        tag = str(attributes.get("tag") or span.get("name") or "llm")
        duration = _span_duration(span)
        events.append(
            _event(
                event_id=str(span.get("span_id")),
                run_id=run_id,
                event_type="ASSISTANT",
                title=f"模型调用 · {tag}",
                summary=f"{attributes.get('status') or span.get('status') or 'OK'}"
                + (f"，{round(duration / 1000, 1)}s" if duration else "")
                + (f"，输出 {attributes.get('chars')} 字符" if attributes.get("chars") else ""),
                stage=stage,
                round_index=stages.ordinal(stage),
                status=str(attributes.get("status") or span.get("status") or "OK"),
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=duration,
                parent_event_id=span.get("parent_span_id"),
                model=str(attributes.get("model") or "") or None,
                metadata={"tag": tag, "chars": attributes.get("chars")},
                notes=[
                    "模型输入/输出正文未持久化（trace 只记 tag / 模型 / 字符数 / 耗时），"
                    "因此这里没有 Prompt 预览",
                    "token 只能按整次 run 统计：Provider 不按调用回报用量",
                ],
                badcases=badcase_by_ref.get(str(span.get("span_id")), []),
            )
        )
    return events


def _validation_events(
    *,
    run_id: str,
    spans: Sequence[Mapping[str, Any]],
    plan: TripPlan | None,
    stages: _StageIndex,
    badcase_by_ref: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Python 侧硬校验事件：预算、可行性、硬约束复检，外加计划里剩下的告警。"""

    events: list[dict[str, Any]] = []
    for span in spans:
        if str(span.get("component")) not in ("budget", "feasibility", "planner"):
            continue
        attributes = span.get("attributes") or {}
        name = str(span.get("name") or "")
        if str(span.get("component")) == "planner" and name not in ("hard_constraint_recheck",):
            continue
        stage = stages.of_span(span) or ("check_budget" if str(span.get("component")) == "budget" else None)
        if str(span.get("component")) == "feasibility":
            stage = stage or "check_feasibility"
        if str(span.get("component")) == "planner":
            stage = stage or "critic_and_revise"
        violations = attributes.get("violations")
        count = attributes.get("count")
        status = str(span.get("status") or "SUCCESS")
        if isinstance(count, int) and count > 0:
            status = "WARNING"
        events.append(
            _event(
                event_id=str(span.get("span_id")),
                run_id=run_id,
                event_type="VALIDATION",
                title=f"硬校验 · {name or span.get('component')}",
                summary=(
                    f"{status}"
                    + (f"，问题 {count} 项" if isinstance(count, int) else "")
                    + (f"，未解决 {attributes.get('unresolved_errors')} 项" if attributes.get("unresolved_errors") is not None else "")
                ),
                stage=stage,
                round_index=stages.ordinal(stage),
                status=status,
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=_span_duration(span),
                parent_event_id=span.get("parent_span_id"),
                input_preview=_preview(violations),
                output_preview=_preview(
                    {
                        key: attributes.get(key)
                        for key in ("auto_revised", "arrival_index", "last_day_deadline_minutes", "label", "status", "projected_total")
                        if attributes.get(key) is not None
                    }
                ),
                metadata=dict(attributes),
                badcases=badcase_by_ref.get(str(span.get("span_id")), []),
            )
        )

    if plan is not None and plan.warnings:
        unresolved = [issue for issue in plan.warnings if not issue.resolved]
        events.append(
            _event(
                event_id=f"{run_id}:event:plan-warnings",
                run_id=run_id,
                event_type="VALIDATION",
                title="交付时的行程告警",
                summary=f"共 {len(plan.warnings)} 条（未解决 {len(unresolved)} 条）",
                status="WARNING" if unresolved else "SUCCESS",
                output_preview=_preview(
                    [
                        {
                            "code": issue.code,
                            "severity": issue.severity,
                            "day_index": issue.day_index,
                            "reason": issue.reason,
                            "resolved": issue.resolved,
                        }
                        for issue in plan.warnings
                    ],
                    limit=1200,
                ),
                metadata={
                    "codes": dict(Counter(str(issue.code) for issue in plan.warnings)),
                    "unresolved": len(unresolved),
                },
                notes=["这些是离开系统时仍在计划里的告警（已 resolved 的也一并列出，便于对照）"],
            )
        )
    return events


def _error_events(
    *,
    run_id: str,
    spans: Sequence[Mapping[str, Any]],
    badcases: Sequence[Mapping[str, Any]],
    stages: _StageIndex,
    badcase_by_ref: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """错误事件：带 error 的 span + 触发但没挂到具体 span 的 Bad Case。"""

    events: list[dict[str, Any]] = []
    for span in spans:
        attributes = span.get("attributes") or {}
        error = span.get("error")
        status = str(attributes.get("status") or span.get("status") or "")
        failed = bool(error) or status.upper() in _PROBLEM_STATUSES
        if not failed:
            continue
        stage = stages.of_span(span)
        events.append(
            _event(
                event_id=f"{str(span.get('span_id'))}:error",
                run_id=run_id,
                event_type="ERROR",
                title=f"{span.get('component')} · {span.get('name')} 失败",
                summary=str(status or "FAILED"),
                stage=stage,
                round_index=stages.ordinal(stage),
                status=str(status or "FAILED"),
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=_span_duration(span),
                parent_event_id=str(span.get("span_id")),
                provider=str(attributes.get("provider") or "") or None,
                tool=str(attributes.get("tool") or "") or None,
                output_preview=_preview(error),
                metadata={"component": span.get("component"), "name": span.get("name")},
            )
        )

    attached = {str(case.get("badcase_id")) for cases in badcase_by_ref.values() for case in cases}
    for case in badcases:
        if str(case.get("badcase_id")) in attached:
            continue
        refs = [str(ref) for ref in (case.get("trace_refs") or [])]
        events.append(
            _event(
                event_id=f"{run_id}:event:badcase:{case.get('badcase_id')}",
                run_id=run_id,
                event_type="ERROR",
                title=f"Bad Case · {case.get('category')}",
                summary=_preview(case.get("symptom"), limit=300),
                status="BAD_CASE",
                provider=None,
                metadata={"trace_refs": refs, "detected_by": case.get("detected_by")},
                notes=["这条 Bad Case 的 trace_refs 没有指向本次 run 里存在的 span，单独列出"],
                badcases=[_badcase_brief(case)],
            )
        )
    return events


# ======================================================================
# 主入口
# ======================================================================


def run_timeline(store: TravelPlanStore, run_id: str) -> dict[str, Any]:
    """把一次 run 转录成统一时间轴。**只读**。"""

    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")

    progress = store.get_run_progress(run_id) or {}
    spans = store.get_trace_spans(run_id)
    sources = store.list_sources(run_id)
    badcases, _ = store.list_badcases(run_id=run_id, limit=200)
    metrics = store.get_run_metrics(run_id) or {}

    plan: TripPlan | None = None
    stored = store.get_plan(run_id)
    if stored and isinstance(stored.get("plan"), dict):
        try:
            plan = TripPlan.model_validate(stored["plan"])
        except Exception:  # noqa: BLE001 —— 计划解析失败不该让整条轨迹打不开
            plan = None

    # 质量画像复用聚合层的快照（同一份 plan、同一个进程内缓存），
    # 免得详情页为了显示一个质量分把 200KB 的 plan_json 再解一遍。
    from .admin_analytics import grade, plan_snapshot

    snapshot = plan_snapshot(store, run_id)

    stages = _StageIndex(progress.get("stages") or [], run_id)

    badcase_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in badcases:
        for ref in case.get("trace_refs") or []:
            badcase_by_ref[str(ref)].append(_badcase_brief(case))

    events: list[dict[str, Any]] = []

    # --- SYSTEM：运行配置 ---
    from .config import current_config

    config = current_config()
    model_name = getattr(config, "model_name", None)
    run_started = progress.get("started_at") or run.get("created_at")
    events.append(
        _event(
            event_id=f"{run_id}:event:system",
            run_id=run_id,
            event_type="SYSTEM",
            title="运行配置",
            summary="、".join(
                part
                for part in (
                    f"模型 {model_name}" if model_name else None,
                    f"预算上限 {'开' if config.budget_enabled else '关'}",
                    f"Jev {'开' if config.jev_enabled else '关'}",
                    f"计划来源 {run.get('source') or 'quick'}",
                )
                if part
            ),
            status="INFO",
            started_at=_iso_or_none(run_started),
            finished_at=_iso_or_none(run_started),
            model=model_name,
            metadata={
                "source": run.get("source"),
                "source_session_id": run.get("source_session_id"),
                "max_run_tokens": getattr(config, "max_run_tokens", None),
                "max_run_cost": getattr(config, "max_run_cost", None),
                "max_run_seconds": getattr(config, "max_run_seconds", None),
                "budget_enabled": config.budget_enabled,
                "jev_enabled": config.jev_enabled,
            },
            notes=[
                "系统提示词正文不落库：trace 只记录模型调用的 tag / 模型 / 字符数 / 耗时，"
                "因此这里不展示 Prompt 内容",
            ],
        )
    )

    # --- USER：用户要什么 ---
    intent_brief: dict[str, Any] = {}
    if plan is not None:
        intent = plan.intent
        intent_brief = {
            "destination": getattr(intent, "destination", None),
            "origin": getattr(intent, "origin", None),
            "start_date": getattr(intent, "start_date", None),
            "days": getattr(intent, "days", None),
            "travelers": getattr(intent, "travelers", None),
            "budget_total": getattr(intent, "budget_total", None),
            "preferences": list(getattr(intent, "preferences", None) or []),
            "pace": getattr(intent, "pace", None),
            "hotel_priority": getattr(intent, "hotel_priority", None),
            "place_selections": getattr(intent, "place_selections", None),
        }
    session = None
    if run.get("source_session_id"):
        session = store.get_planning_session(str(run["source_session_id"]))
    selections = (session or {}).get("poi_selections") or {}
    must = sorted(key for key, value in selections.items() if str(value).upper() == "MUST")
    reject = sorted(key for key, value in selections.items() if str(value).upper() == "REJECT")
    events.append(
        _event(
            event_id=f"{run_id}:event:user",
            run_id=run_id,
            event_type="USER",
            title="用户原始请求",
            summary=_preview(run.get("original_query"), limit=300),
            status="INFO",
            started_at=_iso_or_none(run_started),
            finished_at=_iso_or_none(run_started),
            input_preview=_preview(run.get("original_query"), limit=600),
            output_preview=_preview(intent_brief),
            metadata={
                "intent": intent_brief,
                "must": must[:20],
                "reject": reject[:20],
                "guided": bool(session),
                "session_id": run.get("source_session_id"),
            },
            notes=[] if session else ["本次 run 不是引导式会话创建的，没有结构化选择"],
        )
    )

    # --- CONTEXT：带进了什么上下文 ---
    prefetch = (session or {}).get("prefetch") or {}
    context_summary: dict[str, Any] = {}
    if prefetch:
        context_summary = {
            "transport_candidates": len(prefetch.get("outbound") or []) + len(prefetch.get("inbound") or []),
            "hotel_candidates": len(prefetch.get("hotels") or []),
            "evidence": len(prefetch.get("evidences") or []),
            "place_candidates": len(prefetch.get("places") or []),
            "provider_calls": len(prefetch.get("provider_calls") or []),
            "city_cache": prefetch.get("city_cache"),
            "discovery_status": prefetch.get("discovery_status") or session.get("discovery_status"),
        }
    events.append(
        _event(
            event_id=f"{run_id}:event:context",
            run_id=run_id,
            event_type="CONTEXT",
            title="本次注入的上下文",
            summary=(
                "、".join(f"{key} {value}" for key, value in context_summary.items() if isinstance(value, int))
                or "没有可复用的会话上下文"
            ),
            status="INFO" if context_summary else "EMPTY",
            started_at=_iso_or_none(run_started),
            finished_at=_iso_or_none(run_started),
            output_preview=_preview(context_summary),
            metadata={"prefetch": context_summary, "degradations": (session or {}).get("degradations") or []},
            notes=[] if context_summary else ["本次 run 没有 PlanningSession 预取（Quick 模式或会话已过期）"],
        )
    )

    # --- 其余事件 ---
    tool_events = _tool_events(
        run_id=run_id, spans=spans, sources=sources, stages=stages, badcase_by_ref=badcase_by_ref
    )
    matched_source_ids = {
        str((span.get("attributes") or {}).get("source_id"))
        for span in spans
        if str(span.get("component")) in ("tool", "mcp") and (span.get("attributes") or {}).get("source_id")
    }
    events.extend(tool_events)
    events.extend(
        _provider_events(
            run_id=run_id,
            spans=spans,
            sources=sources,
            matched_source_ids=matched_source_ids,
            stages=stages,
        )
    )
    events.extend(
        _assistant_events(run_id=run_id, spans=spans, stages=stages, badcase_by_ref=badcase_by_ref)
    )
    events.extend(
        _validation_events(run_id=run_id, spans=spans, plan=plan, stages=stages, badcase_by_ref=badcase_by_ref)
    )
    events.extend(
        _error_events(
            run_id=run_id, spans=spans, badcases=badcases, stages=stages, badcase_by_ref=badcase_by_ref
        )
    )

    # --- 排序：固定引导事件在前，其余按真实时间 ---
    head_order = {"SYSTEM": 0, "USER": 1, "CONTEXT": 2}
    for event in events:
        event["_sort"] = (
            0 if event["event_type"] in head_order else 1,
            head_order.get(event["event_type"], 3),
            _parse_ts(event.get("started_at")) or datetime.max.replace(tzinfo=timezone.utc),
            event["event_id"],
        )
    events.sort(key=lambda item: item.pop("_sort"))

    truncated_events = len(events) > _MAX_EVENTS
    if truncated_events:
        events = events[:_MAX_EVENTS]

    # --- 顶部轨道概览 ---
    base = _parse_ts(run_started)
    tracks = []
    for key, label, types in TRACK_SPECS:
        blocks = []
        for event in events:
            if event["event_type"] not in types:
                continue
            start = _parse_ts(event.get("started_at"))
            if start is None:
                continue
            offset = int((start - base).total_seconds() * 1000) if base else None
            blocks.append(
                {
                    "event_id": event["event_id"],
                    "title": event["title"],
                    "status": event["status"],
                    "stage": event["stage"],
                    "start_ms": offset,
                    "duration_ms": event.get("duration_ms"),
                    "badcase_count": len(event.get("badcases") or []),
                }
            )
        tracks.append({"key": key, "label": label, "count": len(blocks), "blocks": blocks})

    by_type = Counter(event["event_type"] for event in events)
    fallbacks = [event for event in events if event.get("fallback")]
    errors = [event for event in events if event["event_type"] == "ERROR"]
    notes = [
        "模型输入/输出正文与 System Prompt 未持久化，事件里只保留 tag / 模型 / 字符数 / 耗时",
        "逐条决策（选了什么 / 为什么）不在这条时间轴上：见运行详情页的决策链区块",
        "token 与成本按整次 run 统计：Provider 不按调用回报用量，因此事件级 tokens/cost 为空",
    ]
    if truncated_events:
        notes.append(f"事件数超过 {_MAX_EVENTS} 条，仅返回前 {_MAX_EVENTS} 条")

    return {
        "run_id": run_id,
        "generated_at": now_iso(),
        "run": {
            "run_id": run_id,
            "status": run.get("status"),
            "source": run.get("source"),
            "original_query": run.get("original_query"),
            "created_at": _iso_or_none(run.get("created_at")),
            "started_at": _iso_or_none(run_started),
            "finished_at": _iso_or_none(progress.get("finished_at")),
            "duration_ms": metrics.get("duration_ms"),
            "cost": metrics.get("cost"),
            "total_tokens": metrics.get("total_tokens"),
            "badcase_count": metrics.get("badcase_count"),
            "error": progress.get("error"),
        },
        "summary": {
            "events": len(events),
            "by_type": {key: by_type.get(key, 0) for key in EVENT_TYPES},
            "errors": len(errors),
            "fallbacks": len(fallbacks),
            "badcases": len(badcases),
            "stages": len(stages.order),
        },
        "stages": [
            {
                "stage_id": stage_id,
                "title": stages.titles.get(stage_id) or stage_id,
                "status": stages.status.get(stage_id),
                "ordinal": index,
                "started_at": _iso_or_none(next((s.get("started_at") for s in progress.get("stages") or [] if str(s.get("stage_id")) == stage_id), None)),
                "finished_at": _iso_or_none(next((s.get("finished_at") for s in progress.get("stages") or [] if str(s.get("stage_id")) == stage_id), None)),
            }
            for index, stage_id in enumerate(stages.order)
        ],
        "tracks": tracks,
        "events": events,
        "badcases": [_badcase_brief(case) for case in badcases],
        "quality": {
            "score": snapshot.score,
            "grade": grade(snapshot.score),
            "metrics": snapshot.quality,
            "error": snapshot.error,
        },
        "type_options": [{"key": key, "label": EVENT_TYPE_LABELS[key]} for key in EVENT_TYPES],
        "notes": notes,
    }


def _iso_or_none(value: Any) -> str | None:
    parsed = _parse_ts(value)
    if parsed is not None:
        return parsed.isoformat()
    return str(value) if value else None


def build_timeline_router(
    get_store: Callable[[], TravelPlanStore], require_admin: Callable[..., None]
) -> APIRouter:
    """轨迹路由。依赖注入的理由同 ``admin_analytics.build_admin_router``。"""

    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin)])

    @router.get("/runs/{run_id}/timeline")
    def admin_run_timeline(run_id: str) -> dict[str, Any]:
        """一次 run 的统一执行轨迹（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL/PROVIDER/VALIDATION/ERROR）。"""

        return run_timeline(get_store(), run_id)

    return router


__all__ = [
    "EVENT_TYPES",
    "EVENT_TYPE_LABELS",
    "build_timeline_router",
    "run_timeline",
]
