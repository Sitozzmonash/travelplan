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
2. **拿不到就写拿不到**：模型调用现在会把 prompt / 输出以**脱敏 + 截断**的预览随 llm span
   一起持久化（见 ``app/llm.py::LLMResult.to_audit``），所以 ASSISTANT 事件能给出
   ``input_preview`` / ``output_preview``；早期版本记下的 run 没有这些字段，那时给
   ``None`` 并在 ``notes`` 里说明"这条 span 没写预览"，而不是拿摘要冒充原文。
3. **预览必须脱敏 + 截断**：Tool 参数与返回来自第三方，可能带 Key 或几百 KB 正文。

Agent Loop 这一路（主路径已换成它）
----------------------------------
`app/agent_runner.py::execute_agent_run` 里 Agent 自己调工具出计划，落进 trace 的事实是：

* 工具 span（component=``tool``，``name`` = 工具名）—— 每次调用一条，attributes 带
  ``tool / status / duration_ms / seq / stage_key / query / note``；
* 模型 span（component=``llm``）—— 每次调用一条，attributes 带
  ``input_tokens / output_tokens / cached_tokens / total_tokens / cumulative_*``；
* ``run_stages`` 的 ``stage_id`` 就是**中文展示名**（「在查酒店」），逐次调用的完整事实
  在 span 上（同名工具多次调用共享同一行 stage，后写覆盖先写）。

所以本模块除了逐条事件，还会给出一份 ``agent`` 块：``steps``（按真实发生顺序排好的
工具 / 模型步骤，带每步 token）、``tokens``（run_metrics 的整 run 汇总）、``meta``
（prompt 版本 / 交卷方式 / truncation 与上限）。**固定 12 步的那些阶段码在这一路里不存在**，
事件与步骤一律按新口径归属，旧 run 走原来的三条规则（见 ``_StageIndex``）。
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, Depends, HTTPException

from .models import TripPlan
from .observability import now_iso
from .redact import clip, scrub
from .store import TravelPlanStore
from .workflow import DEFAULT_OUTPUT_DIR, LLM_STAGE_MAP, PROVIDER_STAGE_MAP

#: Agent 交卷工具名。与 `app/agent_runner.py::SUBMIT_TOOL_NAME` 必须一致（有一处单测守着）。
#: 刻意不 import agent_runner：那个模块会拉起 langchain / superharness，而这个模块是
#: 只读聚合，不该因为写一个新实现而多背一份重依赖。
SUBMIT_TOOL_NAME = "submit_final_plan"

#: `audit["agent"]["plan_origin"]` → 人话（管理端直接把这句话显示出来）。
PLAN_ORIGIN_LABELS: dict[str, str] = {
    SUBMIT_TOOL_NAME: "Agent 调用 submit_final_plan 交卷",
    "message_json": "Agent 未交卷，行程由最后一条回复里的 JSON 解析（message_json 兜底）",
}

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

#: 状态里算"这里出过问题"的那些。
_PROBLEM_STATUSES = ("FAILED", "TIMEOUT", "ERROR", "AUTH_ERROR", "RATE_LIMITED", "INVALID")


def _scrub(text: str) -> str:
    """兜底脱敏：实现只有一份，在 ``app/redact.py``（环境变量里的 Key + 常见凭据字样）。

    保留这个函数只是为了不改变本模块已有的调用点；新增脱敏一律走 `app.redact`。
    """

    return scrub(text)


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
    return clip(text, limit=limit) or None


def _int_or_none(value: Any) -> int | None:
    """span 属性里的整数：取不到（或根本不是数字）就给 ``None``。

    0 与"没有"必须分得开：0 会被读成"确实一次都没有"，而 None 才表示"这次没回报"。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _scrub_opt(text: Any) -> str | None:
    """预览字段一律在**读出来的时候**再过一次脱敏。

    写这些字段的进程可能不是当前进程（早期版本、别的部署），也可能环境里的 Key 换过；
    不能假定落库时已经擦干净 —— 页面是最后一道出口，泄出去就收不回来了。
    非字符串一律给 None：宁可少显示，也不把不确定类型的东西塞进预览。
    """

    if not isinstance(text, str) or not text:
        return None
    return _scrub(text) or None


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


def _base_stage_key(key: str) -> str:
    """``tool:search_hotels:2`` → ``tool:search_hotels``；``llm:gpt-4o`` 原样返回。

    同一个工具的第 N 次调用共享同一行 stage（后写覆盖先写），facts 里只留下最后那次的
    ``stage_key``；去掉尾部的调用序号，前几次才能挂回同一行。
    """

    head, separator, tail = key.rpartition(":")
    if separator and tail.isdigit():
        return head
    return key


class _StageIndex:
    """把 span / 工具 / 模型调用归到 workflow 阶段。

    归属规则按可靠性排序，先用前面那条：
      1. **Agent Loop 的步骤身份**：span 属性里的 ``stage_key``（``tool:search_hotels:2``）、
         工具名、模型名与 ``run_stages.facts`` 对上 —— 中文展示名就是 stage_id 本身；
      2. 父 span 以 ``:{stage_id}`` 结尾 —— trace 自己写下的父子关系；
      3. 工具名 → 阶段（``PROVIDER_STAGE_MAP``）/ 模型 tag → 阶段（``LLM_STAGE_MAP``）
         —— 固定 12 步流程的静态映射，只在旧 run 上有意义。
    三条都命不中就不挂阶段（前端显示"未归属"），而不是随便猜一个。
    """

    def __init__(self, stages: Sequence[Mapping[str, Any]], run_id: str) -> None:
        self.order: list[str] = []
        self.titles: dict[str, str] = {}
        self.status: dict[str, Any] = {}
        #: Agent Loop 的三个反查表：stage_key / 工具名 / 模型名 → stage_id。
        self._by_key: dict[str, str] = {}
        self._by_tool: dict[str, str] = {}
        self._by_model: dict[str, str] = {}
        #: 模型名 → facts 里的 stage_key。模型调用的 span 不写 stage_key（工具 span 才写），
        #: 而所有模型调用共享「在思考行程」这一行，它对每次模型调用都是同一个值，可以安全回填。
        self._key_by_model: dict[str, str] = {}
        for stage in stages:
            stage_id = str(stage.get("stage_id") or "")
            if not stage_id:
                continue
            if stage_id not in self.order:
                self.order.append(stage_id)
            self.titles[stage_id] = str(stage.get("message") or stage_id) or stage_id
            self.status[stage_id] = stage.get("status")
            facts = stage.get("facts")
            facts = facts if isinstance(facts, Mapping) else {}
            stage_key = str(facts.get("stage_key") or "")
            if stage_key:
                self._by_key.setdefault(stage_key, stage_id)
                base = _base_stage_key(stage_key)
                if base != stage_key:
                    self._by_key.setdefault(base, stage_id)
            tool = str(facts.get("tool") or "")
            if tool:
                self._by_tool.setdefault(tool, stage_id)
            model = str(facts.get("model") or "")
            if model:
                self._by_model.setdefault(model, stage_id)
                if stage_key:
                    self._key_by_model.setdefault(model, stage_key)
        self._index = {stage_id: position for position, stage_id in enumerate(self.order)}
        self._run_id = run_id

    @staticmethod
    def _suffix_lookup(index: Mapping[str, str], name: str) -> str | None:
        """带 Provider 前缀的工具名（``tuniu_search_hotels``）按后缀挂回它的阶段。"""

        if not name:
            return None
        for known, stage_id in index.items():
            if name.endswith(known):
                return stage_id
        return None

    def key_for_model(self, model: str) -> str:
        """模型名 → 那行 stage 的 ``facts.stage_key``（拿不到就给空串）。"""

        return self._key_by_model.get(model) or ""

    def of_span(self, span: Mapping[str, Any]) -> str | None:
        attributes = span.get("attributes") or {}
        component = str(span.get("component") or "")
        # 1) Agent Loop：span 上带着 run_stages 里那份步骤身份。
        stage_key = str(attributes.get("stage_key") or "")
        if stage_key:
            mapped = self._by_key.get(stage_key) or self._by_key.get(_base_stage_key(stage_key))
            if mapped:
                return mapped
        if component in ("tool", "mcp"):
            tool = str(attributes.get("tool") or span.get("name") or "")
            mapped = self._by_tool.get(tool) or self._suffix_lookup(self._by_tool, tool)
            if mapped:
                return mapped
        if component == "llm":
            name = str(attributes.get("model") or attributes.get("tag") or span.get("name") or "")
            mapped = self._by_model.get(name) or self._suffix_lookup(self._by_model, name)
            if mapped:
                return mapped
        # 2) 父 span 写下的父子关系（固定 12 步流程）。
        parent = str(span.get("parent_span_id") or "")
        prefix = f"{self._run_id}:"
        if parent.startswith(prefix):
            tail = parent[len(prefix) :].split(":", 1)[0]
            if tail in self._index:
                return tail
        # 3) 静态映射（旧 run 的工具名 / 模型 tag）。
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
    sources_by_span: Mapping[str, Mapping[str, Any]],
    stages: _StageIndex,
    badcase_by_ref: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """工具调用事件：span 给"发生了什么"，source 给"参数与返回"。

    ``sources_by_span`` 由 ``_match_sources_to_spans`` 算好（显式 source_id 优先，其次按
    时间窗口对齐）—— Agent 路径的 tool span 不带 source_id，只能靠时间窗口找回 Provider
    与返回条数。
    """

    events: list[dict[str, Any]] = []
    for span in spans:
        if str(span.get("component")) not in ("tool", "mcp"):
            continue
        attributes = span.get("attributes") or {}
        source = sources_by_span.get(str(span.get("span_id") or ""))
        tool = str(attributes.get("tool") or span.get("name") or "tool")
        # Provider 优先用 span 自己写的（固定流程）；没有就用对齐上的账本行 —— Agent 路径的
        # tool span 不带 provider，不补的话每一行都只能显示工具名。
        provider = (
            str(attributes.get("provider") or "")
            or (str(source.get("provider") or "") if source else "")
            or None
        )
        stage = stages.of_span(span)
        status = str(attributes.get("status") or span.get("status") or "UNKNOWN")
        returned = attributes.get("returned")
        if returned is None and source is not None:
            normalized = source.get("normalized")
            if isinstance(normalized, Mapping) and isinstance(normalized.get("count"), int):
                returned = normalized["count"]
        duration = _span_duration(span)
        notes: list[str] = []
        if str(attributes.get("hedge_discarded") or "").lower() in ("true", "1"):
            notes.append("这条并发请求被对冲策略丢弃（另一条先返回）")
        if str(attributes.get("reused_from_discovery") or "").lower() in ("true", "1"):
            notes.append("结果来自 Discovery 阶段，本次 run 没有再打一次 Provider")
        if source and source.get("source_type") == "discovery":
            notes.append("该调用属于 Discovery 阶段，本次 run 复用其结果")
        if source is not None and not attributes.get("source_id"):
            notes.append(
                "Provider 与返回条数按**时间窗口**对齐到这次工具调用"
                "（Agent 路径的 tool span 不写 source_id；窗口内唯一命中才认）"
            )

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
                    "source_id": str((source or {}).get("source_id") or "") or None,
                    "source_url": (source or {}).get("source_url"),
                    "returned": returned,
                    "timeout": attributes.get("timeout"),
                    "provider_called": attributes.get("provider_called"),
                    "marker": attributes.get("marker"),
                    "stage_key": attributes.get("stage_key"),
                    "seq": attributes.get("seq"),
                    "output_chars": attributes.get("output_chars"),
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
    agent_run: bool = False,
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
                notes=[
                    (
                        "Agent 路径的 tool span 不带 source_id，这条账本行按时间窗口也没能唯一"
                        "对齐到某次工具调用（多为预取复用、并发对冲或没有对应 span 的调用）"
                        if agent_run
                        else "这条调用没有对应的 tool span（多为 Discovery 复用或早期版本记录）"
                    )
                ],
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
    被调用、花了多久、收到与回了什么"。

    正文与 token 都来自 llm span 上由 ``app/llm.py`` 写下的字段（脱敏 + 截断后的预览、
    模型回报的 usage）。span 上没有这些字段时（早期版本记录的 run）如实留 None 并
    在 ``notes`` 里说明"这条 span 没写"，而不是笼统宣称"从来没持久化过"。
    """

    events: list[dict[str, Any]] = []
    for span in spans:
        if str(span.get("component")) != "llm":
            continue
        attributes = span.get("attributes") or {}
        stage = stages.of_span(span)
        tag = str(attributes.get("tag") or span.get("name") or "llm")
        duration = _span_duration(span)
        system_preview = _scrub_opt(attributes.get("system_preview"))
        user_preview = _scrub_opt(attributes.get("user_preview"))
        context_preview = _scrub_opt(attributes.get("context_preview"))
        assistant_preview = _scrub_opt(attributes.get("assistant_preview"))
        tokens_in = _int_or_none(attributes.get("input_tokens"))
        tokens_out = _int_or_none(attributes.get("output_tokens"))
        cached_tokens = _int_or_none(attributes.get("cached_tokens"))
        total_tokens = _int_or_none(attributes.get("total_tokens"))
        # input_preview 用已有的字段承载"模型收到了什么"：逐段标注来源，
        # 否则三段拼在一起没人分得清哪段是需求、哪段是带进来的上下文。
        # 这里不再按 _PREVIEW_LIMIT 二次截断：各段已带自己的截断尾注，再截一次会把
        # "原文 N 字符"写成一段预览的长度，读的人会误以为模型只收到 400 字符。
        sections = [
            f"[{label}]\n{value}"
            for label, value in (
                ("system", system_preview),
                ("user", user_preview),
                ("context", context_preview),
            )
            if value
        ]
        notes: list[str] = []
        if not any((system_preview, user_preview, context_preview, assistant_preview)):
            notes.append(
                "这条 span 没有 Prompt 预览：该 run 由早期版本记录（span 不写预览字段），"
                "或预览脱敏后为空；不是被隐藏"
            )
        if total_tokens is None:
            notes.append("这次调用没有 token 用量：模型未回报 usage，字段留白而不是 0")
        events.append(
            _event(
                event_id=str(span.get("span_id")),
                run_id=run_id,
                event_type="ASSISTANT",
                title=f"模型调用 · {tag}",
                summary=f"{attributes.get('status') or span.get('status') or 'OK'}"
                + (f"，{round(duration / 1000, 1)}s" if duration else "")
                + (f"，输出 {attributes.get('chars')} 字符" if attributes.get("chars") else "")
                + (f"，{total_tokens} tokens" if total_tokens is not None else ""),
                stage=stage,
                round_index=stages.ordinal(stage),
                status=str(attributes.get("status") or span.get("status") or "OK"),
                started_at=span.get("started_at"),
                finished_at=span.get("finished_at"),
                duration_ms=duration,
                parent_event_id=span.get("parent_span_id"),
                model=str(attributes.get("model") or "") or None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                input_preview="\n\n".join(sections) or None,
                output_preview=assistant_preview or None,
                metadata={
                    "tag": tag,
                    "chars": attributes.get("chars"),
                    "system_preview": system_preview,
                    "user_preview": user_preview,
                    "context_preview": context_preview,
                    "assistant_preview": assistant_preview,
                    "prompt_version": attributes.get("prompt_version"),
                    "prompt_hash": attributes.get("prompt_hash"),
                    "input_tokens": tokens_in,
                    "output_tokens": tokens_out,
                    "cached_tokens": cached_tokens,
                    "total_tokens": total_tokens,
                },
                notes=notes,
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
# Agent Loop：步骤流 / token / 元信息
# ======================================================================

#: 没有开始时刻的事件排到最后（而不是排到 1970 年去）。
_SORT_MAX = datetime.max.replace(tzinfo=timezone.utc)


def _output_dir() -> Path:
    """run 级产物的目录（与 `app/api.py::_output_dir` 同一份口径：环境变量优先）。"""

    return Path(os.environ.get("TRAVELPLAN_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))


def _run_audit(run_id: str) -> dict[str, Any]:
    """读回该 run 的 `audit_report.json`（读不到就给空 dict，绝不因此让轨迹打不开）。

    为什么这里要读文件：`audit["agent"]` 里的 ``plan_origin`` / ``truncation`` /
    ``max_steps`` / ``timeout_seconds`` **没有对应的库表列**（run_metrics 表结构固定，
    `store.save_run_metrics` 只落那几个汇总数字；这几项当时只写进了内存里的 metrics 与
    审计文件）。库侧能拿到的只有 run_metrics 汇总与 trace，所以这一块要么读文件、
    要么留空 —— 这里选择读文件并把"读不到"如实标注（见 payload 的 notes）。
    """

    path = _output_dir() / run_id / "audit_report.json"
    try:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_agent_run(spans: Sequence[Mapping[str, Any]], prompt_span: Mapping[str, Any] | None) -> bool:
    """这次 run 是不是 Agent Loop 出的计划。

    两条证据任一成立即可：`_record_prompt_version` 写下的 workflow span 上标了
    ``agent=superharness``；或者有 tool span 带 ``stage_key``（StepReporter 只有这一路用）。
    旧 run 两条都命不中 —— 于是它们继续走固定 12 步的展示口径。
    """

    attributes = (prompt_span or {}).get("attributes") or {}
    if str(attributes.get("agent") or "") == "superharness":
        return True
    return any(
        str(span.get("component")) in ("tool", "mcp") and (span.get("attributes") or {}).get("stage_key")
        for span in spans
    )


def _prompt_span(spans: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for span in spans:
        if str(span.get("component")) == "workflow" and str(span.get("name")) == "prompt_version":
            return span
    return None


def _match_sources_to_spans(
    spans: Sequence[Mapping[str, Any]], sources: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    """把 Provider 账本的行对齐到工具调用 span，返回 `(span_id → source, 已认领的 source_id)`。

    两条规则，都可以回溯：
      1. span 自己写着 ``source_id``（固定流程的 tool span 就是这么记的）—— 显式引用，最可靠；
      2. 退一步按**时间窗口**：账本行的 ``fetched_at`` 落在某个工具 span 的起止之间。
         Agent 路径的 tool span 不带 source_id，只有这条能把"这次工具调用背后是谁返回的、
         返回几条"挂回来。窗口内落进多条就都不认 —— 宁可留白，也不把 A 次调用的返回
         挂到 B 次调用上。
    """

    sources_by_id = {str(source.get("source_id") or ""): source for source in sources}
    claimed: dict[str, Mapping[str, Any]] = {}
    matched: set[str] = set()
    windows: list[tuple[str, datetime, datetime]] = []
    for span in spans:
        if str(span.get("component")) not in ("tool", "mcp"):
            continue
        span_key = str(span.get("span_id") or "")
        attributes = span.get("attributes") or {}
        explicit = str(attributes.get("source_id") or "")
        if explicit and explicit in sources_by_id:
            claimed[span_key] = sources_by_id[explicit]
            matched.add(explicit)
            continue
        started = _parse_ts(span.get("started_at"))
        finished = _parse_ts(span.get("finished_at")) or started
        if started is None or finished is None:
            continue
        windows.append((span_key, started, finished))
    if not windows:
        return claimed, matched

    taken: set[str] = set()
    for source in sources:
        source_id = str(source.get("source_id") or "")
        if source_id in matched:
            continue
        stamp = _parse_ts(source.get("fetched_at"))
        if stamp is None:
            continue
        fits = [
            span_key
            for span_key, started, finished in windows
            if started <= stamp <= finished and span_key not in taken
        ]
        if len(fits) != 1:
            continue
        claimed[fits[0]] = source
        taken.add(fits[0])
        matched.add(source_id)
    return claimed, matched


def _span_tokens(attributes: Mapping[str, Any]) -> dict[str, int | None] | None:
    """一次模型调用的 token 四项（取不到就给 None，绝不写 0 冒充）。

    ``total_tokens`` 优先用 span 上写好的（StepReporter 按 input + output 算过，
    cached 已含在 input 内）；只有早期 span 缺 total 时才自己加。
    """

    tokens_in = _int_or_none(attributes.get("input_tokens"))
    tokens_out = _int_or_none(attributes.get("output_tokens"))
    cached = _int_or_none(attributes.get("cached_tokens"))
    total = _int_or_none(attributes.get("total_tokens"))
    if total is None and (tokens_in is not None or tokens_out is not None):
        total = int(tokens_in or 0) + int(tokens_out or 0)
    if tokens_in is None and tokens_out is None and cached is None and total is None:
        return None
    return {"input": tokens_in, "output": tokens_out, "cached": cached, "total": total}


def _agent_steps(
    *,
    spans: Sequence[Mapping[str, Any]],
    stages: _StageIndex,
    sources_by_span: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Agent 的步骤流：一行 = 一次真实调用（tool span 或 llm span）。

    为什么不直接拿 ``run_stages`` 当步骤流：它的 ``stage_id`` 是中文展示名，同名工具的
    第 N 次调用共享同一行（后写覆盖先写），逐次事实只在 span 上。这里按 span 归拢，
    ``stage`` 仍回到那行展示名，于是"工具中文名 + 状态 + 耗时 + 这一步的 token"一次给全。

    顺序按 ``started_at``（真实发生顺序）；同一时刻再用 span 上的 ``seq`` 与 span_id 兜底
    —— 工具 span 的 ``seq`` 是"该工具的第几次调用"，它排不了跨工具 / 跨模型的先后。
    """

    rows: list[dict[str, Any]] = []
    for span in spans:
        component = str(span.get("component") or "")
        if component not in ("tool", "mcp", "llm"):
            continue
        attributes = span.get("attributes") or {}
        kind = "model" if component == "llm" else "tool"
        stage = stages.of_span(span)
        source = sources_by_span.get(str(span.get("span_id") or "")) if kind == "tool" else None
        model_name = str(attributes.get("model") or span.get("name") or "") if kind == "model" else ""
        # 步骤身份：工具 span 自己写着 stage_key；模型 span 不写，用「在思考行程」那行 facts 里的
        # key 回填（它按模型名记录，对每一次模型调用都是同一个值，不是逐次推断出来的）。
        step_key = str(attributes.get("stage_key") or "") or (
            stages.key_for_model(model_name) if kind == "model" else ""
        )
        returned = attributes.get("returned")
        if returned is None and source is not None:
            normalized = source.get("normalized")
            if isinstance(normalized, Mapping) and isinstance(normalized.get("count"), int):
                returned = normalized["count"]
        rows.append(
            {
                "order": 0,  # 排完序统一编号
                "kind": kind,
                "seq": _int_or_none(attributes.get("seq")),
                "span_id": str(span.get("span_id") or ""),
                "event_id": str(span.get("span_id") or ""),
                "stage": stage,
                "stage_title": stages.titles.get(stage) if stage else None,
                "step_key": step_key or None,
                "tool": str(attributes.get("tool") or span.get("name") or "") or None
                if kind == "tool"
                else None,
                "model": model_name or None,
                "provider": str(attributes.get("provider") or "")
                or (str(source.get("provider") or "") if source else "")
                or None,
                "status": str(attributes.get("status") or span.get("status") or "") or None,
                "started_at": _iso_or_none(span.get("started_at")),
                "finished_at": _iso_or_none(span.get("finished_at")),
                "duration_ms": _span_duration(span),
                "query": _preview((source or {}).get("query") or attributes.get("query")),
                "note": _preview(attributes.get("note"))
                or _preview((source or {}).get("normalized")),
                "returned": returned,
                "error": _scrub_opt(span.get("error")),
                "tokens": _span_tokens(attributes) if kind == "model" else None,
                "cumulative_total_tokens": _int_or_none(attributes.get("cumulative_total_tokens")),
            }
        )
    rows.sort(
        key=lambda row: (
            _parse_ts(row["started_at"]) or _SORT_MAX,
            row["seq"] or 0,
            row["span_id"],
        )
    )
    for index, row in enumerate(rows, start=1):
        row["order"] = index
    return rows


def _agent_block(
    *,
    spans: Sequence[Mapping[str, Any]],
    stages: _StageIndex,
    sources_by_span: Mapping[str, Mapping[str, Any]],
    metrics: Mapping[str, Any],
    prompt_span: Mapping[str, Any] | None,
    audit: Mapping[str, Any],
    is_agent_run: bool,
    has_plan: bool,
) -> dict[str, Any]:
    """Agent 这一路的管理端事实：步骤流 + 整 run token + 元信息（交卷方式 / prompt 版本 / 上限）。

    ``plan_origin`` 与 ``truncation`` 的首选来源是 audit_report.json（库里没有这两列）。
    audit 读不到时的降级只有一条：**从 trace 反推**交卷方式（`submit_final_plan` 的 tool
    span 在不在 + 有没有落 plan），并在 ``notes`` 里说明这是反推的；``truncation`` 反推
    不出来 —— 它如实留空，而不是拿"没有 submit span"当成"超时了"。
    """

    steps = _agent_steps(spans=spans, stages=stages, sources_by_span=sources_by_span)
    audit_agent = audit.get("agent")
    audit_agent = audit_agent if isinstance(audit_agent, Mapping) else {}
    prompt_attributes = (prompt_span or {}).get("attributes") or {}
    notes: list[str] = []

    prompt_version = (
        str(audit_agent.get("prompt_version") or "") or str(prompt_attributes.get("prompt_version") or "")
    ) or None
    if prompt_version is None:
        notes.append(
            "这次 run 没有留下 prompt 版本：audit_report.json 与 trace 的 prompt_version span 都没有"
        )
    elif not audit_agent.get("prompt_version"):
        notes.append("prompt 版本来自 trace 的 prompt_version span（audit_report.json 没读到）")

    origin = str(audit_agent.get("plan_origin") or "") or None
    origin_source = "audit_report.json" if origin else None
    submitted = any(step["kind"] == "tool" and step["tool"] == SUBMIT_TOOL_NAME for step in steps)
    if origin is None:
        if submitted:
            origin, origin_source = SUBMIT_TOOL_NAME, "trace_spans"
            notes.append(
                "交卷方式由 trace 反推（看到 submit_final_plan 的 tool span）；"
                "权威口径在 audit_report.json 的 agent.plan_origin"
            )
        elif has_plan:
            origin, origin_source = "message_json", "trace_spans"
            notes.append(
                "交卷方式由 trace 反推（没有 submit_final_plan 的 tool span、但落了计划）；"
                "权威口径在 audit_report.json 的 agent.plan_origin"
            )

    truncation = str(audit_agent.get("truncation") or "") or None
    if truncation is None and not audit_agent:
        notes.append(
            "truncation（超时 / 超步数 / 成本护栏）只在 audit_report.json 的 agent 里，"
            "本次没读到该文件 —— 留空而不是从别的事实猜一个"
        )

    # 与 run_timeline 同一处延迟导入：这个模块的 import 链不背配置层。
    from .config import current_config

    config = current_config()
    limits_from_audit = any(
        audit_agent.get(key) is not None for key in ("max_steps", "timeout_seconds")
    )
    limits = {
        "max_steps": audit_agent.get("max_steps")
        if audit_agent.get("max_steps") is not None
        else getattr(config, "max_agent_steps", None),
        "timeout_seconds": audit_agent.get("timeout_seconds")
        if audit_agent.get("timeout_seconds") is not None
        else getattr(config, "agent_run_timeout_seconds", None),
        "from_audit": limits_from_audit,
        "note": (
            "上限来自本次 run 的 audit_report.json"
            if limits_from_audit
            else "上限取自**当前**配置（该 run 当时的快照只在 audit_report.json 里，本次没读到）"
        ),
    }

    model_steps = [step for step in steps if step["kind"] == "model"]
    tool_steps = [step for step in steps if step["kind"] == "tool"]
    step_totals = [step["tokens"]["total"] for step in model_steps if step["tokens"]]
    tokens = {
        "input": _int_or_none(metrics.get("input_tokens")),
        "output": _int_or_none(metrics.get("output_tokens")),
        "cached": _int_or_none(metrics.get("cached_tokens")),
        "total": _int_or_none(metrics.get("total_tokens")),
        "llm_calls": _int_or_none(metrics.get("llm_calls")),
        "tool_calls": _int_or_none(metrics.get("tool_calls")),
        "duration_ms": _int_or_none(metrics.get("duration_ms")),
        "source": "run_metrics",
        #: 逐条 span 相加的自算值：给"汇总与明细对不对得上"一个当场可核对的数字。
        "step_total": sum(step_totals) if step_totals else None,
        "steps_llm": len(model_steps),
        "steps_tool": len(tool_steps),
    }
    if not metrics:
        notes.append("这次 run 没有 run_metrics 行：总 token / 耗时只能靠逐条 span，页面会显示「无汇总」")
    elif tokens["total"] is not None and tokens["step_total"] is not None and tokens["total"] != tokens["step_total"]:
        notes.append(
            f"汇总 token（{tokens['total']}）与逐条 span 相加（{tokens['step_total']}）不一致："
            "汇总还包含被截断/未落 span 的调用"
        )

    tools = prompt_attributes.get("tools")
    return {
        "is_agent_run": is_agent_run,
        "prompt_version": prompt_version,
        "system_prompt_chars": _int_or_none(prompt_attributes.get("system_prompt_chars")),
        "tools": [str(item) for item in tools] if isinstance(tools, Sequence) and not isinstance(tools, str) else [],
        "plan_origin": origin,
        "plan_origin_label": PLAN_ORIGIN_LABELS.get(origin or "", None),
        "plan_origin_source": origin_source,
        "truncation": truncation,
        "limits": limits,
        "steps": steps,
        "tokens": tokens,
        "notes": notes,
    }


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
    # Agent Loop 的步骤身份 / Provider 对齐：两份都在这里算一次，
    # 事件、步骤流与 Provider 事件共用同一份结果（同一个事实只算一次）。
    prompt_span = _prompt_span(spans)
    agent_run = _is_agent_run(spans, prompt_span)
    sources_by_span, matched_source_ids = _match_sources_to_spans(spans, sources)
    audit = _run_audit(run_id)
    agent = _agent_block(
        spans=spans,
        stages=stages,
        sources_by_span=sources_by_span,
        metrics=metrics,
        prompt_span=prompt_span,
        audit=audit,
        is_agent_run=agent_run,
        has_plan=plan is not None,
    )

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
    events.extend(
        _tool_events(
            run_id=run_id,
            spans=spans,
            sources_by_span=sources_by_span,
            stages=stages,
            badcase_by_ref=badcase_by_ref,
        )
    )
    events.extend(
        _provider_events(
            run_id=run_id,
            spans=spans,
            sources=sources,
            matched_source_ids=matched_source_ids,
            stages=stages,
            agent_run=agent_run,
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
        "ASSISTANT 事件的 Prompt/输出正文是**脱敏 + 截断**后的预览（单段最多保留前 200 字符）；"
        "早期版本记录的 run 的 span 里没有预览字段，此时预览为空并在该事件上标注",
        "逐次调用的 token 来自模型回报的 usage（取不到留白，不是 0）；成本仍按整次 run 统计"
        "（单价是用户填的估计值）",
        "逐条决策（选了什么 / 为什么）不在这条时间轴上：见运行详情页的决策链区块",
    ]
    if agent_run:
        notes.append(
            "这次 run 由 Agent Loop 出计划：步骤按 trace_spans 的 tool / llm span 逐条列出"
            "（run_stages 的 stage_id 是中文步骤名，同名工具的第 N 次调用共享同一行，"
            "逐次事实以 span 为准）"
        )
        notes.append(
            "整 run 的 token 汇总来自 run_metrics（agent.tokens.step_total 是逐条 span 相加，"
            "两者对不上时会在 agent.notes 里说明）"
        )
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
            "agent_steps": len(agent["steps"]),
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
        #: Agent Loop 这一路的步骤流 / 每步 token / 整 run 汇总 / 元信息。
        #: 旧 run（固定 12 步）走 workflow 视图，这里如实给 `is_agent_run=False` 与空步骤，
        #: 而不是让前端自己去猜这次跑的是哪条路。
        "agent": agent,
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
    "PLAN_ORIGIN_LABELS",
    "SUBMIT_TOOL_NAME",
    "build_timeline_router",
    "run_timeline",
]
