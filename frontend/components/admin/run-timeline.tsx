"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Cpu,
  MessagesSquare,
  RefreshCw,
  Search,
  Timer,
  Workflow,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { JsonBlock, KeyValueList, PreviewBlock } from "@/components/admin/detail-dialog";
import { FILTER_ALL, FilterSelect, type FilterOption } from "@/components/admin/filter-select";
import {
  badcaseCategoryLabel,
  cleanBadcaseSymptom,
  formatCostWithCurrency,
  formatDurationMs,
  formatNumber,
  statusLabel,
} from "@/components/admin/format";
import { StatusBadge, ToneBadge, type AdminTone } from "@/components/admin/status-badge";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminRunTimeline } from "@/lib/admin-api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  AdminAgentInfo,
  AdminAgentStep,
  AdminRecord,
  AdminTimeline,
  AdminTimelineTrack,
  AdminTimelineTrackBlock,
  AdminTraceBadcaseBrief,
  AdminTraceEvent,
} from "@/types/admin";

/**
 * Agent Execution Timeline（docs §12–19）。
 *
 * 四个刻意的取舍：
 * 1) 顶部轨道只画「有时间戳」的节点；没有起点的节点不静默丢掉，而是在轨道下方如实列出，
 *    否则读者会以为这次 run 根本没调过那个工具。
 * 2) 事件默认只给类型 / 标题 / 摘要 / 状态 / 耗时（§17），Input / Output / metadata 折叠在详情里。
 * 3) 事件级 token 与成本通常是 null（Provider 不按调用回报用量），这里不显示裸「—」，
 *    而是明确写「按整次 run 统计」，避免把「没有逐条口径」读成「没有消耗」。
 * 4) 同一份事实给两个视图（feedback §3）：第一个视图按步骤 / 阶段组织，第二个视图按真实
 *    执行顺序串成对话。两个视图共用同一次取数、同一套筛选与同一段详情渲染，切视图不重新
 *    请求、不重算。
 *
 * 主路径已换成 Agent Loop（`app/agent_runner.py`）之后的第一个视图
 * ------------------------------------------------------------------
 * Agent 自己调工具出计划，**没有固定 12 步的阶段**，所以第一个视图按 run 的类型切换内容：
 *
 * * Agent run（`timeline.agent.is_agent_run`）→ **Agent View**：先给一张 Agent 运行卡
 *   （prompt 版本、出计划方式、truncation、步数与墙钟上限、整 run token 汇总），下面是一条
 *   按真实发生顺序排好的步骤流（工具/模型 + 中文步骤名 + 状态 + 耗时 + 每步 token），
 *   顺序与序号都来自后端算好的 `timeline.agent.steps`，前端不重排；
 * * 旧 run（固定 12 步）→ 原来的 Workflow View：按 workflow 阶段分组的事件列表。
 *
 * 视图的 key 仍然是 `workflow`（只是标签与内容随 run 类型切换）：深链、父组件状态与既有
 * 习惯都不动一处，也不会留下一个「永远空白」的固定流程页签。
 */

/** 轨迹视图（feedback §3）。`workflow` 这个 key 同时承载 Agent View（见文件头说明）。 */
export type TraceViewMode = "workflow" | "conversation";

interface TraceViewSpec {
  key: TraceViewMode;
  label: string;
  description: string;
  icon: "agent" | "workflow" | "conversation";
}

/** 两个视图的说明文案：切换器上要一眼看出「这个视图回答什么问题」。 */
function traceViews(isAgentRun: boolean): TraceViewSpec[] {
  return [
    isAgentRun
      ? {
          key: "workflow",
          label: "Agent View",
          icon: "agent",
          description:
            "按 Agent 的每一步看：工具中文名 + 状态 + 耗时、每步 token、整 run token 汇总，外加 prompt 版本 / 交卷方式 / 截断。回答「Agent 每一步在做什么、花了多少」。",
        }
      : {
          key: "workflow",
          label: "Workflow View",
          icon: "workflow",
          description:
            "按 workflow 阶段看执行过程：轨道概览 + 阶段内事件。回答「这次 run 走到哪一步、哪一步慢、哪一步错」。",
        },
    {
      key: "conversation",
      label: "Conversation View",
      icon: "conversation",
      description:
        "按真实执行顺序串成一条对话：System → User → Context → Assistant → Tool（含 Tool Result）→ Assistant。回答「模型实际收到了什么、实际返回了什么」。",
    },
  ];
}

/** Agent 步骤选中的说明（步骤流与对话视图共用的角色词表在这里对齐）。 */
const AGENT_STEP_KINDS: Record<string, { label: string; tone: AdminTone; hint: string }> = {
  tool: { label: "工具", tone: "success", hint: "Agent 调了一次工具（外部取数）" },
  model: { label: "模型", tone: "info", hint: "Agent 的一次模型调用（token 在这里）" },
};

/** 「未归属阶段」的筛选取值：真实 stage_id 不会等于它。 */
const STAGE_NONE = "__none__";
/** 挂不上阶段的运行前置事件（系统 / 用户 / 上下文）单独成组，排在所有阶段之前。 */
const STAGE_PROLOGUE = "__prologue__";
const PROLOGUE_TYPES = ["SYSTEM", "USER", "CONTEXT"];

const EVENT_TONES: Record<string, AdminTone> = {
  SYSTEM: "muted",
  USER: "info",
  CONTEXT: "muted",
  ASSISTANT: "info",
  TOOL: "success",
  PROVIDER: "warning",
  VALIDATION: "warning",
  ERROR: "danger",
};

/** 轨道色块配色：只区分「哪一类事件」，严重性由状态徽标与 ⚠ 表达。 */
const EVENT_SEGMENT_CLASS: Record<string, string> = {
  SYSTEM: "bg-foreground/40",
  USER: "bg-info",
  CONTEXT: "bg-info/50",
  ASSISTANT: "bg-primary",
  TOOL: "bg-success",
  PROVIDER: "bg-warning",
  VALIDATION: "bg-muted-foreground",
  ERROR: "bg-danger",
};

/** 事件类型 → 轨道默认色：事件被截断时（events 里找不到对应项）仍要有颜色。 */
const TRACK_EVENT_TYPE: Record<string, string> = {
  model: "ASSISTANT",
  tool: "TOOL",
  provider: "PROVIDER",
  validation: "VALIDATION",
  error: "ERROR",
};

const FALLBACK_TYPE_OPTIONS: { key: string; label: string }[] = [
  { key: "SYSTEM", label: "系统" },
  { key: "USER", label: "用户" },
  { key: "CONTEXT", label: "上下文" },
  { key: "ASSISTANT", label: "助手" },
  { key: "TOOL", label: "工具" },
  { key: "PROVIDER", label: "Provider" },
  { key: "VALIDATION", label: "校验" },
  { key: "ERROR", label: "错误" },
];

/**
 * 对话视图的角色标签。
 *
 * 这里刻意用英文角色名（System / User / …）而不是中文：它对应的是模型 API 里的 role，
 * 排查 Prompt / Context 问题时跟日志、跟后端字段名对得上号，比「助手」更省一次翻译。
 */
const CONVERSATION_ROLES: Record<string, { label: string; tone: AdminTone; hint: string }> = {
  SYSTEM: { label: "System", tone: "muted", hint: "系统提示 / 运行配置" },
  USER: { label: "User", tone: "info", hint: "用户请求" },
  CONTEXT: { label: "Context", tone: "muted", hint: "注入到模型上下文的内容" },
  ASSISTANT: { label: "Assistant", tone: "info", hint: "模型调用：收到什么 → 返回什么" },
  TOOL: { label: "Tool", tone: "success", hint: "工具调用与工具返回（Tool Result）" },
  PROVIDER: { label: "Provider", tone: "warning", hint: "外部数据源调用" },
  VALIDATION: { label: "Validation", tone: "warning", hint: "硬校验与告警" },
  ERROR: { label: "Error", tone: "danger", hint: "错误" },
};

const MISSING_PREVIEW_HINT =
  "后端尚未落库脱敏预览（feedback §3 的 system / user / context / assistant preview 字段）；" +
  "在它到位之前，这几段只能按上面的缺口口径留空，管理台不会拿别的内容顶替。";

/** 工具返回（对话里的 Tool Result）：TOOL 事件的 output_preview 就是它。 */
const TOOL_RESULT_LABEL = "工具返回（Tool Result）";

export interface RunTimelineProps {
  runId: string;
  /** 打开时预置的阶段筛选（stage_id）。 */
  initialStage?: string | null;
  /** 打开时预置的关键词（可以传工具名 / Provider，如 `search_social_guides`）。 */
  initialQuery?: string;
  /** 变化时把筛选重置为 initialStage / initialQuery：性能页每次跳过来都能重新定位。 */
  focusToken?: number;
  /** 跳到性能页（阶段级）。 */
  onSelectStage?: (stage: string | null) => void;
  /** 跳到性能页（事件级），用于回答「这次调用慢在哪」。 */
  onOpenPerformance?: (eventId: string) => void;
  /**
   * 当前轨迹视图（受控）。不传时组件内部自己管，方便单独引用；
   * 运行详情页把状态放在自己这一层，是为了像 TimelineFocus 一样支持深链（`?view=conversation`）。
   */
  view?: TraceViewMode;
  onViewChange?: (view: TraceViewMode) => void;
  className?: string;
}

export function RunTimeline({
  runId,
  initialStage,
  initialQuery,
  focusToken,
  onSelectStage,
  onOpenPerformance,
  view,
  onViewChange,
  className,
}: RunTimelineProps) {
  const resource = useAdminResource<AdminTimeline>(
    "admin-run-timeline:" + runId,
    () => getAdminRunTimeline(runId),
    Boolean(runId),
  );

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      <ResourceView
        resource={resource}
        loadingRows={8}
        isEmpty={(data) => data.events.length === 0}
        emptyTitle="这次运行没有可展示的事件"
        emptyDescription="后端把 trace / Provider 账本 / 决策与校验归并后仍然没有事件：可能是这次 run 还没跑到落库，或者它只写了进度而没有 span。"
        emptyAction={
          <Button variant="outline" size="xs" onClick={resource.reload}>
            <RefreshCw />
            重新拉取轨迹
          </Button>
        }
      >
        {(timeline) => (
          <TimelineBody
            timeline={timeline}
            onReload={resource.reload}
            initialStage={initialStage}
            initialQuery={initialQuery}
            focusToken={focusToken}
            onSelectStage={onSelectStage}
            onOpenPerformance={onOpenPerformance}
            view={view}
            onViewChange={onViewChange}
          />
        )}
      </ResourceView>
    </div>
  );
}

/* ------------------------------ 视图切换 ------------------------------ */

/**
 * Workflow / Agent / Conversation 切换器。
 *
 * 为什么把说明文案跟切换器放在一起：两个视图的差别不是排版而是「回答哪个问题」，
 * 不放说明的话运营只会记住默认那个视图。
 */
function TraceViewSwitch({
  views,
  value,
  onChange,
}: {
  views: TraceViewSpec[];
  value: TraceViewMode;
  onChange: (view: TraceViewMode) => void;
}) {
  const active = views.find((item) => item.key === value) ?? views[0];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
      <div role="tablist" aria-label="轨迹视图" className="flex items-center gap-1 rounded-lg bg-muted p-[3px]">
        {views.map((item) => {
          const selected = item.key === value;
          return (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => onChange(item.key)}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-xs font-medium whitespace-nowrap transition-all focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                selected ? "bg-background text-foreground shadow-sm" : "text-foreground/60 hover:text-foreground",
              )}
            >
              {item.icon === "agent" ? (
                <Cpu className="size-3.5" aria-hidden />
              ) : item.icon === "workflow" ? (
                <Workflow className="size-3.5" aria-hidden />
              ) : (
                <MessagesSquare className="size-3.5" aria-hidden />
              )}
              {item.label}
            </button>
          );
        })}
      </div>
      <p className="min-w-[15rem] flex-1 text-[11px] leading-5 text-muted-foreground">{active.description}</p>
    </div>
  );
}

/* ------------------------------ 主体 ------------------------------ */

function TimelineBody({
  timeline,
  onReload,
  initialStage,
  initialQuery,
  focusToken,
  onSelectStage,
  onOpenPerformance,
  view,
  onViewChange,
}: {
  timeline: AdminTimeline;
  onReload: () => void;
  initialStage?: string | null;
  initialQuery?: string;
  focusToken?: number;
  onSelectStage?: (stage: string | null) => void;
  onOpenPerformance?: (eventId: string) => void;
  view?: TraceViewMode;
  onViewChange?: (view: TraceViewMode) => void;
}) {
  const [typeFilter, setTypeFilter] = useState<string>(FILTER_ALL);
  const [stageFilter, setStageFilter] = useState<string>(initialStage ?? FILTER_ALL);
  const [statusFilter, setStatusFilter] = useState<string>(FILTER_ALL);
  const [query, setQuery] = useState<string>(initialQuery ?? "");
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  // 视图默认 Workflow：多数人是上来定位「哪一步出问题」，Conversation 是顺着问到底时才切。
  const [internalView, setInternalView] = useState<TraceViewMode>("workflow");
  const viewMode = view ?? internalView;
  const changeView = (next: TraceViewMode) => {
    setInternalView(next);
    onViewChange?.(next);
  };
  // 待滚动目标存 ref 而不是 state：滚动是「渲染完成后同步 DOM」，不需要再触发一次渲染。
  const pendingScrollRef = useRef<string | null>(null);

  // 只在下发的 token 变化时重置筛选，避免父组件每次重渲染都把用户改过的条件打回去。
  const appliedTokenRef = useRef(focusToken);
  useEffect(() => {
    if (focusToken === appliedTokenRef.current) return;
    appliedTokenRef.current = focusToken;
    setTypeFilter(FILTER_ALL);
    setStatusFilter(FILTER_ALL);
    setStageFilter(initialStage ?? FILTER_ALL);
    setQuery(initialQuery ?? "");
  }, [focusToken, initialStage, initialQuery]);

  const typeOptions = timeline.type_options.length > 0 ? timeline.type_options : FALLBACK_TYPE_OPTIONS;
  const runTotalTokens = timeline.run.total_tokens;
  // Agent Loop 的 run：第一个视图换成「步骤流 + Agent 元信息」，旧 run 保持 Workflow View。
  const agentInfo = timeline.agent ?? null;
  const isAgentRun = Boolean(agentInfo?.is_agent_run);
  const views = traceViews(isAgentRun);
  // 步骤流按后端算好的顺序给序号：前端不重排（重排会改掉「真实先后」）。
  const stepByEventId = useMemo(() => {
    const map = new Map<string, AdminAgentStep>();
    for (const step of agentInfo?.steps ?? []) {
      if (step.event_id) map.set(step.event_id, step);
    }
    return map;
  }, [agentInfo]);

  const stageRows = useMemo(() => {
    const rows = timeline.stages.map((stage) => ({
      id: stage.stage_id,
      title: stage.title || stage.stage_id,
      status: stage.status,
    }));
    const known = new Set(rows.map((row) => row.id));
    // 事件挂到了阶段列表之外的 stage（后端改过阶段码 / 阶段没落库）也要能筛。
    for (const event of timeline.events) {
      if (event.stage && !known.has(event.stage)) {
        known.add(event.stage);
        rows.push({ id: event.stage, title: event.stage, status: null });
      }
    }
    return rows;
  }, [timeline.stages, timeline.events]);

  const statusOptions = useMemo<FilterOption[]>(() => {
    const seen = new Set<string>();
    for (const event of timeline.events) {
      if (event.status) seen.add(event.status);
    }
    return [
      { value: FILTER_ALL, label: "全部状态" },
      ...[...seen].sort().map((status) => ({ value: status, label: statusLabel(status) })),
    ];
  }, [timeline.events]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return timeline.events.filter((event) => {
      if (typeFilter !== FILTER_ALL && event.event_type !== typeFilter) return false;
      if (stageFilter === STAGE_NONE) {
        if (event.stage) return false;
      } else if (stageFilter !== FILTER_ALL && event.stage !== stageFilter) return false;
      if (statusFilter !== FILTER_ALL && event.status !== statusFilter) return false;
      if (needle && !matchesQuery(event, needle)) return false;
      return true;
    });
  }, [timeline.events, typeFilter, stageFilter, statusFilter, query]);

  // 分组顺序 = 运行前置（系统 / 用户 / 上下文）→ workflow 阶段（stage.ordinal）→
  // 未登记阶段（按首次出现）→ 挂不上阶段的其余事件。
  const groups = useMemo(() => {
    const order = new Map<string, number>();
    timeline.stages.forEach((stage, index) => order.set(stage.stage_id, index));
    for (const event of timeline.events) {
      if (event.stage && !order.has(event.stage)) order.set(event.stage, order.size);
    }
    const buckets = new Map<string, AdminTraceEvent[]>();
    for (const event of visible) {
      const key = event.stage ?? (PROLOGUE_TYPES.includes(event.event_type) ? STAGE_PROLOGUE : STAGE_NONE);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(event);
      else buckets.set(key, [event]);
    }
    return [...buckets.entries()]
      .map(([key, events]) => ({ key, events }))
      .sort((left, right) => rankGroup(left.key, order) - rankGroup(right.key, order));
  }, [visible, timeline.stages, timeline.events]);

  const stageTitles = useMemo(() => {
    const map = new Map<string, { title: string; status: string | null; ordinal: number }>();
    timeline.stages.forEach((stage, index) =>
      map.set(stage.stage_id, {
        title: stage.title || stage.stage_id,
        status: stage.status,
        ordinal: index + 1,
      }),
    );
    return map;
  }, [timeline.stages]);

  // 滚动必须在 DOM 更新之后：先记下目标，渲染完成再用 id 找节点。
  useEffect(() => {
    const target = pendingScrollRef.current;
    if (!target) return;
    const node = document.getElementById(eventAnchorId(target));
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    pendingScrollRef.current = null;
  }, [visible, selectedEventId]);

  function pickBlock(block: AdminTimelineTrackBlock) {
    setSelectedEventId(block.event_id);
    setStageFilter(block.stage ?? STAGE_NONE);
    setTypeFilter(FILTER_ALL);
    setStatusFilter(FILTER_ALL);
    setQuery("");
    pendingScrollRef.current = block.event_id;
  }

  const filtered = visible.length !== timeline.events.length;

  // 筛选条两个视图共用：Provider / Validation 事件筛选、关键词定位、阶段定位在对话视图里同样有用
  // （例如「只看 ASSISTANT + 某阶段」就是把一次模型交互从别的噪音里拎出来的最快方式）。
  const filterBar = (
    <div className="flex flex-wrap items-end gap-2 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
      <FilterSelect
        label="事件类型"
        value={typeFilter}
        onChange={setTypeFilter}
        className="w-36"
        options={[
          { value: FILTER_ALL, label: "全部类型" },
          ...typeOptions.map((option) => ({
            value: option.key,
            label: `${option.label}（${formatNumber(readCount(timeline.summary.by_type, option.key) ?? 0)}）`,
          })),
        ]}
      />
      <FilterSelect
        label={isAgentRun ? "Agent 步骤 / 阶段" : "Workflow 阶段"}
        value={stageFilter}
        onChange={setStageFilter}
        className="w-44"
        options={[
          { value: FILTER_ALL, label: isAgentRun ? "全部步骤" : "全部阶段" },
          ...stageRows.map((stage) => ({ value: stage.id, label: stage.title })),
          { value: STAGE_NONE, label: "未归属阶段" },
        ]}
      />
      <FilterSelect
        label="状态"
        value={statusFilter}
        onChange={setStatusFilter}
        className="w-32"
        options={statusOptions}
      />
      <div className="flex min-w-[12rem] flex-1 flex-col gap-1">
        <span className="text-[11px] text-muted-foreground">关键词</span>
        <div className="relative">
          <Search
            className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="标题 / 摘要 / 工具 / Provider / 阶段 / 预览正文"
            className="h-8 pl-7"
            aria-label="按关键词过滤事件"
          />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="tabular text-[11px] text-muted-foreground">
          {formatNumber(visible.length)} / {formatNumber(timeline.events.length)} 条
        </span>
        {filtered ? (
          <Button
            variant="outline"
            size="xs"
            onClick={() => {
              setTypeFilter(FILTER_ALL);
              setStageFilter(FILTER_ALL);
              setStatusFilter(FILTER_ALL);
              setQuery("");
            }}
          >
            清空筛选
          </Button>
        ) : null}
        <Button variant="outline" size="xs" onClick={onReload}>
          <RefreshCw />
          刷新轨迹
        </Button>
      </div>
    </div>
  );

  return (
    <div className="flex flex-col gap-4">
      <TraceViewSwitch views={views} value={viewMode} onChange={changeView} />

      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <span>执行摘要</span>
            <ToneBadge tone="muted" className="text-[0.6875rem]">
              事件 {formatNumber(timeline.summary.events)}
            </ToneBadge>
            <ToneBadge tone={timeline.summary.errors > 0 ? "danger" : "muted"} className="text-[0.6875rem]">
              错误 {formatNumber(timeline.summary.errors)}
            </ToneBadge>
            <ToneBadge tone={timeline.summary.fallbacks > 0 ? "warning" : "muted"} className="text-[0.6875rem]">
              fallback {formatNumber(timeline.summary.fallbacks)}
            </ToneBadge>
            <ToneBadge tone={timeline.summary.badcases > 0 ? "warning" : "muted"} className="text-[0.6875rem]">
              Bad Case {formatNumber(timeline.summary.badcases)}
            </ToneBadge>
            <ToneBadge tone="muted" className="text-[0.6875rem]">
              阶段 {formatNumber(timeline.summary.stages)}
            </ToneBadge>
            {isAgentRun ? (
              <ToneBadge tone="info" className="text-[0.6875rem]">
                Agent 步骤 {formatNumber(timeline.summary.agent_steps ?? agentInfo?.steps.length ?? 0)}
              </ToneBadge>
            ) : null}
            {isAgentRun && agentInfo?.truncation ? (
              <ToneBadge tone="warning" className="text-[0.6875rem]">
                循环被截断
              </ToneBadge>
            ) : null}
          </CardTitle>
          <CardDescription>
            {isAgentRun
              ? "这次 run 由 Agent Loop 出计划：按 trace_spans 的逐步事实（工具 / 模型调用）串成时间轴，点类型徽标可只看该类事件。"
              : "按真实执行顺序把 trace / Provider 账本 / 校验与 Bad Case 归并成一条时间轴；点类型徽标可只看该类事件。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-1.5">
          {typeOptions.map((option) => {
            const count = readCount(timeline.summary.by_type, option.key);
            if (count === null || count === 0) return null;
            const active = typeFilter === option.key;
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => setTypeFilter(active ? FILTER_ALL : option.key)}
                className="rounded-full focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                aria-pressed={active}
              >
                <ToneBadge
                  tone={EVENT_TONES[option.key] ?? "muted"}
                  className={cn("text-[0.6875rem]", active && "ring-2 ring-foreground/25")}
                >
                  {option.label} {formatNumber(count)}
                </ToneBadge>
              </button>
            );
          })}
        </CardContent>
      </Card>

      {viewMode === "workflow" ? (
        <>
          {isAgentRun && agentInfo ? (
            <AgentMetaCard agent={agentInfo} runDurationMs={timeline.run.duration_ms} cost={timeline.run.cost} />
          ) : null}

          <TrackOverview timeline={timeline} onPickBlock={pickBlock} />

          {filterBar}

          {isAgentRun ? (
            <AgentStepStream
              events={visible}
              stepByEventId={stepByEventId}
              runTotalTokens={runTotalTokens}
              selectedEventId={selectedEventId}
              onSelectEvent={(eventId) =>
                setSelectedEventId((current) => (current === eventId ? null : eventId))
              }
              onOpenPerformance={onOpenPerformance}
            />
          ) : (
            <WorkflowEventCard
              groups={groups}
              stageTitles={stageTitles}
              runTotalTokens={runTotalTokens}
              selectedEventId={selectedEventId}
              onSelectEvent={(eventId) =>
                setSelectedEventId((current) => (current === eventId ? null : eventId))
              }
              onSelectStage={onSelectStage}
              onOpenPerformance={onOpenPerformance}
              visibleCount={visible.length}
            />
          )}
        </>
      ) : (
        <>
          {filterBar}

          <ConversationTimeline
            events={visible}
            stageTitles={stageTitles}
            runTotalTokens={runTotalTokens}
            selectedEventId={selectedEventId}
            onSelectEvent={(eventId) =>
              setSelectedEventId((current) => (current === eventId ? null : eventId))
            }
            onOpenPerformance={onOpenPerformance}
          />
        </>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <BadcaseSummary badcases={timeline.badcases} />
        <NotesPanel notes={timeline.notes} />
      </div>
    </div>
  );
}

/* ------------------------------ 轨道概览（docs §12.1） ------------------------------ */

function TrackOverview({
  timeline,
  onPickBlock,
}: {
  timeline: AdminTimeline;
  onPickBlock: (block: AdminTimelineTrackBlock) => void;
}) {
  const eventTypeById = useMemo(() => {
    const map = new Map<string, string>();
    for (const event of timeline.events) map.set(event.event_id, event.event_type);
    return map;
  }, [timeline.events]);

  const { span, placed, total } = useMemo(() => {
    let max = 0;
    let placedCount = 0;
    let blockCount = 0;
    for (const track of timeline.tracks) {
      for (const block of track.blocks) {
        blockCount += 1;
        if (block.start_ms === null) continue;
        placedCount += 1;
        max = Math.max(max, block.start_ms + Math.max(0, block.duration_ms ?? 0));
      }
    }
    return { span: max > 0 ? max : 1000, placed: placedCount, total: blockCount };
  }, [timeline.tracks]);

  const legend = timeline.type_options.length > 0 ? timeline.type_options : FALLBACK_TYPE_OPTIONS;

  return (
    <Card>
      <CardHeader>
        <CardTitle>轨道概览</CardTitle>
        <CardDescription>
          每条轨道一行，色块按开始时间与耗时铺开（相对 run 开始时刻）。点色块定位到下方事件；带 ⚠ 的节点说明它挂上了 Bad Case。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-muted-foreground">
          {legend.map((option) => (
            <li key={option.key} className="flex items-center gap-1.5">
              <span
                className={cn("size-2.5 rounded-sm", EVENT_SEGMENT_CLASS[option.key] ?? "bg-muted-foreground")}
                aria-hidden
              />
              {option.label}
            </li>
          ))}
        </ul>

        {timeline.tracks.length === 0 ? (
          <p className="text-xs text-muted-foreground">后端没有返回轨道数据。</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {timeline.tracks.map((track) => (
              <TrackRow
                key={track.key}
                track={track}
                span={span}
                eventTypeById={eventTypeById}
                onPickBlock={onPickBlock}
              />
            ))}
          </ul>
        )}

        <p className="text-[11px] text-muted-foreground">
          时间轴跨度 {formatDurationMs(span)}；{formatNumber(placed)} / {formatNumber(total)} 个节点有时间戳
          {placed < total ? "（没有起点的节点不画色块，见下方事件列表）" : ""}。
        </p>
      </CardContent>
    </Card>
  );
}

function TrackRow({
  track,
  span,
  eventTypeById,
  onPickBlock,
}: {
  track: AdminTimelineTrack;
  span: number;
  eventTypeById: Map<string, string>;
  onPickBlock: (block: AdminTimelineTrackBlock) => void;
}) {
  const defaultType = TRACK_EVENT_TYPE[track.key] ?? "SYSTEM";
  const badcaseCount = track.blocks.filter((block) => block.badcase_count > 0).length;

  return (
    <li className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
      <div className="flex items-center gap-2 sm:w-36 sm:shrink-0">
        <span className="text-xs font-medium text-foreground">{track.label}</span>
        <Badge variant="secondary" className="tabular text-[0.6875rem]">
          {formatNumber(track.count)}
        </Badge>
        {badcaseCount > 0 ? (
          <ToneBadge tone="danger" className="text-[0.625rem]">
            ⚠ {formatNumber(badcaseCount)}
          </ToneBadge>
        ) : null}
      </div>

      <div className="min-w-0 flex-1 overflow-x-auto">
        <div className="relative h-7 min-w-[520px] rounded-md bg-muted/50">
          {track.blocks.length === 0 ? (
            <span className="absolute top-1/2 left-2 -translate-y-1/2 text-[11px] text-muted-foreground">
              这个轨道没有事件
            </span>
          ) : null}
          {track.blocks.map((block) => {
            const type = eventTypeById.get(block.event_id) ?? defaultType;
            const start = block.start_ms;
            const left = start === null ? 0 : (start / span) * 100;
            // 最窄 0.6%：耗时不到总跨度 1% 的调用也要看得见。
            const width = Math.max(0.6, ((block.duration_ms ?? 0) / span) * 100);
            const label = [
              block.title,
              block.stage ? `阶段 ${block.stage}` : "未归属阶段",
              start === null ? "没有开始时间" : `开始 +${formatDurationMs(start)}`,
              `耗时 ${formatDurationMs(block.duration_ms)}`,
              `状态 ${statusLabel(block.status)}`,
              block.badcase_count > 0 ? `Bad Case ${block.badcase_count}` : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return (
              <button
                key={block.event_id}
                type="button"
                aria-label={label}
                title={label}
                onClick={() => onPickBlock(block)}
                style={{ left: `${left}%`, width: `${width}%` }}
                className={cn(
                  "absolute top-1 h-5 rounded-sm hover:opacity-80 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                  EVENT_SEGMENT_CLASS[type] ?? "bg-muted-foreground",
                  block.badcase_count > 0 && "ring-2 ring-danger",
                  start === null && "opacity-50 outline outline-dashed outline-1 outline-foreground/40",
                )}
              />
            );
          })}
        </div>
      </div>
    </li>
  );
}

/* ------------------------------ Agent View ------------------------------ */

/**
 * Agent 运行卡：这次 run 的元信息 + 整 run token 汇总（主路径换成 Agent Loop 之后新增）。
 *
 * 三条刻意的取舍：
 * 1) 「没有」与「不知道」分开写：交卷方式 / truncation 只在 audit_report.json 里有，
 *    没读到就写「审计文件没读到，无法判断」，而不是写成「没有截断」——
 *    把「没记录」读成「没发生」正是最贵的一类误判。
 * 2) 上限（步数 / 墙钟）标明来源：取自 audit 就是本次 run 的快照，取自当前配置只能当参考。
 * 3) token 同时给「整 run 汇总（run_metrics）」与「逐条 span 相加」两个数字：
 *    对不上时后端会说明（agent.notes），前端不替它对账、也不挑一个显示。
 */
function AgentMetaCard({
  agent,
  runDurationMs,
  cost,
}: {
  agent: AdminAgentInfo;
  runDurationMs: number | null;
  cost: number | null;
}) {
  const tokens = agent.tokens;
  // 没有 audit 时 truncation 是「读不到」而不是「没截断」——这两种必须能分开读。
  const truncationText = agent.truncation
    ? agent.truncation
    : agent.limits.from_audit
      ? "没有截断（循环正常收尾）"
      : "—（audit_report.json 没读到，无法判断）";
  const limitText = (value: number | null, unit: string) =>
    value === null ? "—（未记录）" : `${formatNumber(value)}${unit}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>Agent 运行</span>
          <ToneBadge tone="info" className="text-[0.6875rem]">
            SuperHarness Agent Loop
          </ToneBadge>
          {agent.truncation ? (
            <ToneBadge tone="warning" className="text-[0.6875rem]">
              循环被截断
            </ToneBadge>
          ) : null}
        </CardTitle>
        <CardDescription>
          Agent 自己调工具出计划：本卡是这次 run 的元信息与 token 汇总，下面那条「Agent 步骤流」是逐步事实。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <KeyValueList
          entries={[
            {
              key: "plan_origin",
              label: "出计划方式",
              value: (
                <span className="flex flex-wrap items-center gap-2">
                  {agent.plan_origin_label ?? "—（后端未返回）"}
                  {agent.plan_origin ? (
                    <span className="font-mono text-[10px] text-muted-foreground">{agent.plan_origin}</span>
                  ) : null}
                  <span className="text-[10px] text-muted-foreground">
                    {agent.plan_origin_source === "audit_report.json"
                      ? "来源：本次 run 的审计文件"
                      : agent.plan_origin_source === "trace_spans"
                        ? "来源：由 trace 反推（审计文件没读到）"
                        : agent.plan_origin_source === null
                          ? "来源：无（没有交卷迹象也没有落计划）"
                          : `来源：${agent.plan_origin_source}`}
                  </span>
                </span>
              ),
            },
            {
              key: "truncation",
              label: "循环截断",
              value: <span>{truncationText}</span>,
            },
            {
              key: "limits",
              label: "循环上限",
              value: (
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="tabular">
                    步数 {limitText(agent.limits.max_steps, " 步")} · 墙钟{" "}
                    {limitText(agent.limits.timeout_seconds, "s")}
                  </span>
                  <span className="text-[10px] text-muted-foreground">{agent.limits.note}</span>
                </span>
              ),
            },
            {
              key: "prompt_version",
              label: "Prompt 版本",
              value: agent.prompt_version ? (
                <span className="font-mono text-[11px]">{agent.prompt_version}</span>
              ) : (
                <span className="text-muted-foreground">—（后端未返回）</span>
              ),
            },
            {
              key: "system_prompt_chars",
              label: "System Prompt",
              value:
                agent.system_prompt_chars === null
                  ? "—（未记录字符数）"
                  : `${formatNumber(agent.system_prompt_chars)} 字符`,
            },
            {
              key: "tools",
              label: "可见工具",
              value:
                agent.tools.length > 0 ? (
                  <span className="font-mono text-[11px]">
                    {agent.tools.length} 个：{agent.tools.join("、")}
                  </span>
                ) : (
                  <span className="text-muted-foreground">—（trace 没记下拉起的工具清单）</span>
                ),
            },
            {
              key: "calls",
              label: "调用次数",
              value: (
                <span className="tabular">
                  模型 {formatNumber(tokens.steps_llm)} 次
                  {tokens.llm_calls !== null ? `（run_metrics ${formatNumber(tokens.llm_calls)}）` : ""} · 工具{" "}
                  {formatNumber(tokens.steps_tool)} 次
                  {tokens.tool_calls !== null ? `（run_metrics ${formatNumber(tokens.tool_calls)}）` : ""}
                </span>
              ),
            },
            {
              key: "tokens_total",
              label: "总 Token",
              value:
                tokens.total === null ? (
                  <span className="text-muted-foreground">—（没有 run_metrics 汇总）</span>
                ) : (
                  <span className="tabular">
                    <span className="font-medium text-foreground">{formatNumber(tokens.total)}</span>
                    {" = "}
                    输入 {formatNumber(tokens.input)} + 输出 {formatNumber(tokens.output)}
                    {tokens.cached !== null ? `（其中缓存命中 ${formatNumber(tokens.cached)}）` : ""}
                    <span className="ml-1 font-mono text-[10px] text-muted-foreground">{tokens.source}</span>
                  </span>
                ),
            },
            {
              key: "tokens_steps",
              label: "逐条相加",
              value: (
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="tabular">
                    {tokens.step_total === null ? "—（没有一条模型调用记了 token）" : formatNumber(tokens.step_total)}
                  </span>
                  {tokens.step_total !== null && tokens.total !== null && tokens.step_total !== tokens.total ? (
                    <ToneBadge tone="warning" className="text-[0.625rem]">
                      与汇总不一致
                    </ToneBadge>
                  ) : null}
                  <span className="text-[10px] text-muted-foreground">
                    span 逐条相加，用来核对上面的汇总
                  </span>
                </span>
              ),
            },
            {
              key: "duration",
              label: "耗时",
              value: (
                <span className="tabular">
                  {formatDurationMs(tokens.duration_ms ?? runDurationMs)}
                  <span className="ml-1 text-[10px] text-muted-foreground">
                    {tokens.duration_ms !== null ? "run_metrics" : "run 记录"}
                  </span>
                </span>
              ),
            },
            {
              key: "cost",
              label: "成本",
              value: cost !== null ? formatCostWithCurrency(cost, null) : "—（没有单价快照）",
            },
          ]}
        />

        {agent.notes.length > 0 ? (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-xs font-medium text-foreground">这张卡的口径说明</h3>
            <ul className="flex list-disc flex-col gap-1 pl-5 text-[11px] leading-5 text-muted-foreground">
              {agent.notes.map((note) => (
                <li key={note} className="break-words">
                  {note}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * Agent 步骤流：把事件按**真实发生顺序**平铺（不再按 workflow 阶段分组）。
 *
 * 为什么不再分组：Agent 的阶段就是"它这一步在调哪个工具"，同一个工具可能被调很多次，
 * 按阶段分组会把「第 1 次查酒店 → 查机票 → 第 2 次查酒店」的真实先后压平成两组。
 * 这里直接复用事件行（步骤序号 / 工具中文名 / 每步 token 由行自己渲染），
 * 于是筛选、展开、跳转性能页这些既有交互一个都不丢。
 */
function AgentStepStream({
  events,
  stepByEventId,
  runTotalTokens,
  selectedEventId,
  onSelectEvent,
  onOpenPerformance,
}: {
  events: AdminTraceEvent[];
  stepByEventId: Map<string, AdminAgentStep>;
  runTotalTokens: number | null;
  selectedEventId: string | null;
  onSelectEvent: (eventId: string) => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  const stepCount = events.filter((event) => stepByEventId.has(event.event_id)).length;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>Agent 步骤流</span>
          <Badge variant="secondary" className="tabular text-[0.6875rem]">
            {formatNumber(events.length)} 条
          </Badge>
          <ToneBadge tone="muted" className="text-[0.6875rem]">
            含步骤 {formatNumber(stepCount)}
          </ToneBadge>
        </CardTitle>
        <CardDescription>
          顺序就是后端记录的先后（按真实发生时间；工具 span 的 seq 是「该工具的第几次调用」）。
          每行给步骤序号 / 工具中文名 / 状态 / 耗时，模型步另有每步 token；点开才渲染参数与返回。
        </CardDescription>
      </CardHeader>
      <CardContent>
        {events.length === 0 ? (
          <SectionEmpty
            title="当前筛选下没有事件"
            description="这次运行里没有同时满足「类型 + 步骤 + 状态 + 关键词」的事件。清空筛选可以看到全部步骤。"
          />
        ) : (
          <ol className="flex flex-col gap-2 border-l border-dashed border-border pl-3 sm:pl-4">
            {events.map((event) => (
              <TimelineEventRow
                key={event.event_id}
                event={event}
                step={stepByEventId.get(event.event_id)}
                runTotalTokens={runTotalTokens}
                selected={selectedEventId === event.event_id}
                onSelect={() => onSelectEvent(event.event_id)}
                onOpenPerformance={onOpenPerformance}
              />
            ))}
          </ol>
        )}
        {stepCount < events.length ? (
          <p className="mt-2 text-[11px] leading-5 text-muted-foreground">
            其余 {formatNumber(events.length - stepCount)} 条不是 Agent 步骤（系统 / 用户 / 上下文 /
            校验 / Provider 账本 / 错误）：它们没有 span 服务端序号的步骤身份，但仍是这次 run 的真实事件，
            所以一并按时间排在流里。
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

/* ------------------------------ Workflow View ------------------------------ */

/**
 * Workflow View 的事件列表：按 workflow 阶段分组。
 *
 * 分组顺序 = 运行前置（系统 / 用户 / 上下文）→ workflow 阶段（stage.ordinal）→
 * 未登记阶段（按首次出现）→ 挂不上阶段的其余事件 —— 顺序在 TimelineBody 里算好，
 * 这里只负责渲染，保证「切视图不改动取数与排序」。
 */
function WorkflowEventCard({
  groups,
  stageTitles,
  runTotalTokens,
  selectedEventId,
  onSelectEvent,
  onSelectStage,
  onOpenPerformance,
  visibleCount,
}: {
  groups: { key: string; events: AdminTraceEvent[] }[];
  stageTitles: Map<string, { title: string; status: string | null; ordinal: number }>;
  runTotalTokens: number | null;
  selectedEventId: string | null;
  onSelectEvent: (eventId: string) => void;
  onSelectStage?: (stage: string | null) => void;
  onOpenPerformance?: (eventId: string) => void;
  visibleCount: number;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>详细事件时间线</CardTitle>
        <CardDescription>
          左侧竖线是 workflow 阶段的分组；默认只给类型 / 标题 / 摘要 / 状态 / 耗时，展开详情才看模型收到
          / 返回的预览、Input / Output 与 metadata。
        </CardDescription>
      </CardHeader>
      <CardContent>
        {visibleCount === 0 ? (
          <SectionEmpty
            title="当前筛选下没有事件"
            description="这次运行里没有同时满足「类型 + 阶段 + 状态 + 关键词」的事件。清空筛选可以看到全部事件。"
          />
        ) : (
          <ol className="flex flex-col gap-5">
            {groups.map((group) => {
              const stage = stageTitles.get(group.key);
              const title = stage
                ? stage.title
                : group.key === STAGE_PROLOGUE
                  ? "运行前置"
                  : group.key === STAGE_NONE
                    ? "未归属阶段"
                    : group.key;
              return (
                <li key={group.key} className="relative pl-5 sm:pl-7">
                  <span className="absolute top-2 left-[3px] h-[calc(100%-0.75rem)] w-px bg-border" aria-hidden />
                  <span className="absolute top-1.5 left-0 size-[7px] rounded-full bg-border" aria-hidden />
                  <div className="flex flex-wrap items-center gap-2">
                    {stage ? (
                      <Badge variant="secondary" className="tabular text-[0.6875rem]">
                        #{stage.ordinal}
                      </Badge>
                    ) : null}
                    <span className="text-xs font-medium text-foreground">{title}</span>
                    {stage ? (
                      <span className="font-mono text-[10px] text-muted-foreground">{group.key}</span>
                    ) : (
                      <span className="text-[11px] text-muted-foreground">
                        {group.key === STAGE_PROLOGUE
                          ? "系统 / 用户 / 上下文：发生在第一个阶段之前"
                          : "挂不上 workflow 阶段"}
                      </span>
                    )}
                    <StatusBadge status={stage?.status ?? null} />
                    <span className="tabular ml-auto text-[11px] text-muted-foreground">
                      {formatNumber(group.events.length)} 条
                    </span>
                    {stage && onSelectStage ? (
                      <Button variant="ghost" size="xs" onClick={() => onSelectStage(group.key)}>
                        看该阶段性能
                      </Button>
                    ) : null}
                  </div>

                  <ol className="mt-2 flex flex-col gap-2">
                    {group.events.map((event) => (
                      <TimelineEventRow
                        key={event.event_id}
                        event={event}
                        runTotalTokens={runTotalTokens}
                        selected={selectedEventId === event.event_id}
                        onSelect={() => onSelectEvent(event.event_id)}
                        onOpenPerformance={onOpenPerformance}
                      />
                    ))}
                  </ol>
                </li>
              );
            })}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------ 事件行 ------------------------------ */

function TimelineEventRow({
  event,
  step,
  runTotalTokens,
  selected,
  onSelect,
  onOpenPerformance,
}: {
  event: AdminTraceEvent;
  /** Agent 步骤身份（Agent View 才有）；其他视图不传。 */
  step?: AdminAgentStep;
  runTotalTokens: number | null;
  selected: boolean;
  onSelect: () => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  const tone = EVENT_TONES[event.event_type] ?? "muted";
  const tokens = readEventTokenFacts(event);
  const hasTokenFacts = tokens.length > 0 || event.cost !== null;
  const stepKind = step ? AGENT_STEP_KINDS[step.kind] : undefined;
  // 步骤的中文名：后端给的是 run_stages 的那份展示名（「在查酒店」）。
  const stepLabel = step?.stage_title ?? null;

  return (
    <li
      id={eventAnchorId(event.event_id)}
      data-timeline-event={event.event_id}
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card",
        selected && "ring-2 ring-primary/40",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex w-full min-w-0 flex-col gap-1.5 px-3 py-2.5 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-expanded={selected}
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          {step ? (
            <span className="tabular shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-foreground">
              步骤 #{step.order}
            </span>
          ) : null}
          <ToneBadge tone={tone} className="text-[0.6875rem]">
            {event.event_type_label || event.event_type}
          </ToneBadge>
          {stepKind ? (
            <ToneBadge tone={stepKind.tone} className="text-[0.6875rem]">
              {stepKind.label}
            </ToneBadge>
          ) : null}
          {stepLabel ? (
            <span className="shrink-0 text-[11px] font-medium text-foreground">{stepLabel}</span>
          ) : null}
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground" title={event.title}>
            {event.title || "（没有标题）"}
          </span>
          {event.fallback ? <ToneBadge tone="warning">fallback</ToneBadge> : null}
          {event.cache_hit ? <ToneBadge tone="muted">缓存命中</ToneBadge> : null}
          {event.prefetch_reused ? <ToneBadge tone="info">复用预取</ToneBadge> : null}
          {event.badcases.length > 0 ? (
            <ToneBadge tone="danger">
              ⚠ {event.badcases.length > 1 ? `${event.badcases.length} 个 Bad Case` : "Bad Case"}
            </ToneBadge>
          ) : null}
          <StatusBadge status={event.status} />
          <span className="tabular shrink-0 text-[11px] text-muted-foreground">
            {formatDurationMs(event.duration_ms)}
          </span>
        </span>

        <span className="line-clamp-2 text-xs leading-5 break-words text-muted-foreground">
          {event.summary || "—"}
        </span>

        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {event.model ? <span className="font-mono">模型 {event.model}</span> : null}
          {event.provider ? <span className="font-mono">Provider {event.provider}</span> : null}
          {event.tool ? <span className="font-mono">工具 {event.tool}</span> : null}
          {/* Agent 步骤行不再显示「阶段序 #N」：那是 run_stages 里第几个**不同步骤名**，
              与真实的第几步不是一回事（同名工具多次调用共享一行），两个序号并排只会误读。 */}
          {event.round_index !== null && !step ? <span>阶段序 #{event.round_index + 1}</span> : null}
          {hasTokenFacts ? (
            <>
              {tokens.length > 0 ? <span className="tabular">Token {tokens.join(" / ")}</span> : null}
              {event.cost !== null ? <span className="tabular">成本 {formatCostWithCurrency(event.cost, null)}</span> : null}
            </>
          ) : runTotalTokens !== null ? (
            <span>token 与成本按整次 run 统计</span>
          ) : null}
          {/* 步骤口径：模型步给这一步的 token 合计与累计；工具步明确写「不消耗模型 token」，
              免得读者把工具行的空白当成「后端漏了」。 */}
          {step ? (
            step.kind === "model" ? (
              <span className="tabular">
                本步 Token {formatNumber(step.tokens?.total ?? null)}
                {step.cumulative_total_tokens !== null
                  ? ` · 累计 ${formatNumber(step.cumulative_total_tokens)}`
                  : ""}
                {step.seq !== null ? ` · 第 ${formatNumber(step.seq)} 次模型调用` : ""}
              </span>
            ) : (
              <span className="tabular">
                工具步（不消耗模型 token）
                {step.seq !== null ? ` · 第 ${formatNumber(step.seq)} 次调用` : ""}
                {typeof step.returned === "number" ? ` · 返回 ${formatNumber(step.returned)} 条` : ""}
              </span>
            )
          ) : null}
          <span>
            点击展开模型收到 / 返回的预览
            {event.notes.length > 0 ? ` · ${event.notes.length} 条口径` : ""}
          </span>
        </span>
      </button>

      {event.badcases.length > 0 ? (
        <div className="border-t border-danger/20 bg-danger-subtle/60 px-3 py-2 text-[11px] leading-5">
          {event.badcases.map((badcase) => (
            <div key={badcase.badcase_id} className="flex flex-col gap-0.5">
              <p className="flex flex-wrap items-center gap-1.5 font-medium text-danger-subtle-foreground">
                <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
                {badcaseCategoryLabel(badcase.category)}
                <StatusBadge status={badcase.severity} />
                <StatusBadge status={badcase.analysis_status} />
              </p>
              <p className="break-words text-danger-subtle-foreground/90">
                {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
              </p>
            </div>
          ))}
          <Link
            href="/admin/badcases"
            className="mt-1 inline-block text-primary underline-offset-4 hover:underline"
          >
            查看 Bad Case
          </Link>
        </div>
      ) : null}

      {/* 与对话视图同一套交互：点这一行展开正文，展开内容才挂进 DOM。
          行上的 aria-expanded 因此是真的（旧实现用嵌套 <details>，行点击并不展开，读屏会读错）。 */}
      {selected ? (
        <div className="border-t border-border/70 px-3 py-3">
          <EventDetail
            event={event}
            step={step}
            runTotalTokens={runTotalTokens}
            onOpenPerformance={onOpenPerformance}
          />
        </div>
      ) : null}
    </li>
  );
}

/* ------------------------------ Conversation View（feedback §3） ------------------------------ */
/**
 * Conversation View：把同一份事件按**后端给出的真实执行顺序**线性串起来。
 *
 * 三条刻意的取舍：
 * 1) 不在这里重新排序。后端已经把 SYSTEM / USER / CONTEXT 这类运行前置事件排在阶段事件之前，
 *    其余按 started_at（见 `admin_timeline` 的 head_order 与 sort），前端再排一次只会改掉
 *    「真实先后」——而排查坏 Case 靠的正是先后（Tool Result 有没有进下一轮上下文，看顺序最快）。
 * 2) 每行仍是「摘要 → 点击展开」：角色 / 标题 / 状态 / 耗时 / token 一行读完，正文只在展开后渲染，
 *    并且正文**不写进 title / aria-label**（一个 hover 就能绕过折叠）。
 * 3) TOOL 事件的 summary 是调用，`output_preview` 就是对话里的 Tool Result：
 *    后端现在把「调用 + 返回」放在同一条事件里，这里不拆成两条，避免凭空造出不存在的事件。
 */
function ConversationTimeline({
  events,
  stageTitles,
  runTotalTokens,
  selectedEventId,
  onSelectEvent,
  onOpenPerformance,
}: {
  events: AdminTraceEvent[];
  stageTitles: Map<string, { title: string; status: string | null; ordinal: number }>;
  runTotalTokens: number | null;
  selectedEventId: string | null;
  onSelectEvent: (eventId: string) => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>对话顺序</span>
          <Badge variant="secondary" className="tabular text-[0.6875rem]">
            {formatNumber(events.length)} 条
          </Badge>
        </CardTitle>
        <CardDescription>
          顺序就是后端记录的先后：System → User → Context → Assistant → Tool（含 Tool Result）→
          Assistant …… 每条默认只给摘要，点开才渲染模型收到 / 返回的正文。
        </CardDescription>
      </CardHeader>
      <CardContent>
        {events.length === 0 ? (
          <SectionEmpty
            title="当前筛选下没有事件"
            description="这次运行里没有同时满足「类型 + 阶段 + 状态 + 关键词」的事件。清空筛选可以看到全部对话。"
          />
        ) : (
          <ol className="flex flex-col gap-2 border-l border-dashed border-border pl-3 sm:pl-4">
            {events.map((event, index) => (
              <ConversationEventRow
                key={event.event_id}
                event={event}
                index={index}
                stage={event.stage ? stageTitles.get(event.stage) : undefined}
                runTotalTokens={runTotalTokens}
                selected={selectedEventId === event.event_id}
                onSelect={() => onSelectEvent(event.event_id)}
                onOpenPerformance={onOpenPerformance}
              />
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function ConversationEventRow({
  event,
  index,
  stage,
  runTotalTokens,
  selected,
  onSelect,
  onOpenPerformance,
}: {
  event: AdminTraceEvent;
  index: number;
  stage?: { title: string; status: string | null; ordinal: number };
  runTotalTokens: number | null;
  selected: boolean;
  onSelect: () => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  const role =
    CONVERSATION_ROLES[event.event_type] ?? {
      label: event.event_type_label || event.event_type,
      tone: "muted" as AdminTone,
      hint: "未登记的事件类型",
    };
  const tokens = readEventTokenFacts(event);
  // Tool Result 只标「有没有」，内容仍然折叠：这句话本身就是排查「工具返回有没有进下一轮上下文」的路标。
  const carriesToolResult = event.event_type === "TOOL" && Boolean(event.output_preview);

  return (
    <li
      id={eventAnchorId(event.event_id)}
      data-timeline-event={event.event_id}
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card",
        selected && "ring-2 ring-primary/40",
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        className="flex w-full min-w-0 flex-col gap-1.5 px-3 py-2.5 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-expanded={selected}
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="tabular shrink-0 text-[10px] text-muted-foreground">#{index + 1}</span>
          <ToneBadge tone={role.tone} className="text-[0.6875rem]">
            {role.label}
          </ToneBadge>
          {stage ? (
            <span className="shrink-0 text-[11px] text-muted-foreground">
              阶段 #{stage.ordinal} {stage.title}
            </span>
          ) : (
            <span className="shrink-0 text-[11px] text-muted-foreground">未归属阶段</span>
          )}
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground" title={event.title}>
            {event.title || "（没有标题）"}
          </span>
          {carriesToolResult ? <ToneBadge tone="muted">含工具返回</ToneBadge> : null}
          {event.fallback ? <ToneBadge tone="warning">fallback</ToneBadge> : null}
          {event.cache_hit ? <ToneBadge tone="muted">缓存命中</ToneBadge> : null}
          {event.badcases.length > 0 ? (
            <ToneBadge tone="danger">
              ⚠ {event.badcases.length > 1 ? `${event.badcases.length} 个 Bad Case` : "Bad Case"}
            </ToneBadge>
          ) : null}
          <StatusBadge status={event.status} />
          <span className="tabular shrink-0 text-[11px] text-muted-foreground">
            {formatDurationMs(event.duration_ms)}
          </span>
        </span>

        <span className="line-clamp-2 text-xs leading-5 break-words text-muted-foreground">
          {event.summary || "—"}
        </span>

        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span className="shrink-0 text-muted-foreground">{role.hint}</span>
          {event.model ? <span className="font-mono">模型 {event.model}</span> : null}
          {event.provider ? <span className="font-mono">Provider {event.provider}</span> : null}
          {event.tool ? <span className="font-mono">工具 {event.tool}</span> : null}
          {tokens.length > 0 ? (
            <span className="tabular">{tokens.join(" · ")}</span>
          ) : runTotalTokens !== null ? (
            <span>token 按整次 run 统计</span>
          ) : null}
          <span>
            点击展开模型收到 / 返回的正文
            {event.notes.length > 0 ? ` · ${event.notes.length} 条口径` : ""}
          </span>
        </span>
      </button>

      {event.badcases.length > 0 ? (
        <div className="border-t border-danger/20 bg-danger-subtle/60 px-3 py-2 text-[11px] leading-5">
          {event.badcases.map((badcase) => (
            <div key={badcase.badcase_id} className="flex flex-col gap-0.5">
              <p className="flex flex-wrap items-center gap-1.5 font-medium text-danger-subtle-foreground">
                <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
                {badcaseCategoryLabel(badcase.category)}
                <StatusBadge status={badcase.severity} />
                <StatusBadge status={badcase.analysis_status} />
              </p>
              <p className="break-words text-danger-subtle-foreground/90">
                {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
              </p>
            </div>
          ))}
        </div>
      ) : null}

      {/* 展开的正文只在这一行被选中时才挂进 DOM：默认摘要、点击展开，和 feedback §3 的要求一致。 */}
      {selected ? (
        <div className="border-t border-border/70 px-3 py-3">
          <EventDetail event={event} runTotalTokens={runTotalTokens} onOpenPerformance={onOpenPerformance} />
        </div>
      ) : null}
    </li>
  );
}

/* ------------------------------ 事件详情（两个视图共用） ------------------------------ */

/**
 * 事件详情：两个视图共用同一份渲染。
 *
 * 为什么必须共用：Workflow 与 Conversation 只是同一次 run 的两种对齐方式，
 * 「一次调用到底收到了什么」不能有两个答案 —— 各渲染一套迟早会出现两处口径不一致。
 */
function EventDetail({
  event,
  step,
  runTotalTokens,
  onOpenPerformance,
}: {
  event: AdminTraceEvent;
  /** Agent 步骤身份（Agent View 才有）：补上 run_stages 侧的步骤事实。 */
  step?: AdminAgentStep;
  runTotalTokens: number | null;
  onOpenPerformance?: (eventId: string) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <KeyValueList
        entries={[
          { key: "event_id", label: "事件 ID", value: <span className="font-mono text-[11px]">{event.event_id}</span> },
          ...(step
            ? [
                {
                  key: "step",
                  label: "Agent 步骤",
                  value: (
                    <span>
                      第 {step.order} 步 · {AGENT_STEP_KINDS[step.kind]?.label ?? step.kind}
                      {step.stage_title ? ` · ${step.stage_title}` : ""}
                      {step.seq !== null ? `（第 ${step.seq} 次调用）` : ""}
                      <span className="ml-1 font-mono text-[10px] text-muted-foreground">
                        {step.step_key ?? "—"}
                      </span>
                    </span>
                  ),
                },
                {
                  key: "step_tokens",
                  label: "本步 Token",
                  value: (
                    <span className="tabular">
                      {step.kind === "model"
                        ? `输入 ${formatNumber(step.tokens?.input ?? null)} / 输出 ${formatNumber(
                            step.tokens?.output ?? null,
                          )} / 缓存 ${formatNumber(step.tokens?.cached ?? null)} / 合计 ${formatNumber(
                            step.tokens?.total ?? null,
                          )}`
                        : "工具调用不消耗模型 token"}
                    </span>
                  ),
                },
                {
                  key: "step_returned",
                  label: "返回条数",
                  value:
                    typeof step.returned === "number" ? (
                      <span className="tabular">{formatNumber(step.returned)}</span>
                    ) : (
                      <span className="text-muted-foreground">—（这条 span 没记返回条数）</span>
                    ),
                },
              ]
            : []),
          {
            key: "type",
            label: "类型",
            value: (
              <span>
                {event.event_type_label || event.event_type}
                <span className="ml-1 font-mono text-[10px] text-muted-foreground">{event.event_type}</span>
              </span>
            ),
          },
          {
            key: "stage",
            label: "阶段",
            value: event.stage ? <span className="font-mono">{event.stage}</span> : "未归属阶段",
          },
          {
            key: "round",
            label: "阶段序号",
            value: event.round_index === null ? "—" : `第 ${event.round_index + 1} 个阶段`,
          },
          { key: "status", label: "状态", value: <StatusBadge status={event.status} /> },
          { key: "started", label: "开始", value: <span className="font-mono">{formatDateTime(event.started_at)}</span> },
          { key: "finished", label: "结束", value: <span className="font-mono">{formatDateTime(event.finished_at)}</span> },
          { key: "duration", label: "耗时", value: formatDurationMs(event.duration_ms) },
          { key: "model", label: "模型", value: <span className="font-mono">{event.model ?? "—"}</span> },
          { key: "provider", label: "Provider", value: <span className="font-mono">{event.provider ?? "—"}</span> },
          { key: "tool", label: "工具", value: <span className="font-mono">{event.tool ?? "—"}</span> },
          {
            key: "tokens_in",
            label: "输入 Token",
            // 新字段（后端 A12 补的 usage）优先，旧字段（tokens_in）兜底：同一次调用的两种口径都认。
            value: eventTokenText(event.input_tokens ?? event.tokens_in, runTotalTokens),
          },
          {
            key: "tokens_out",
            label: "输出 Token",
            value: eventTokenText(event.output_tokens ?? event.tokens_out, runTotalTokens),
          },
          {
            key: "tokens_cached",
            label: "命中缓存 Token",
            value: eventTokenText(event.cached_tokens ?? null, runTotalTokens),
          },
          {
            key: "cost",
            label: "成本",
            value:
              event.cost !== null
                ? formatCostWithCurrency(event.cost, null)
                : runTotalTokens !== null
                  ? "按整次 run 统计"
                  : "—",
          },
          {
            key: "prompt_version",
            label: "Prompt 版本",
            value: a13FieldText(event.prompt_version),
          },
          { key: "prompt_hash", label: "Prompt 哈希", value: a13FieldText(event.prompt_hash) },
          { key: "cache", label: "缓存命中", value: boolText(event.cache_hit) },
          { key: "prefetch", label: "复用预取", value: boolText(event.prefetch_reused) },
          { key: "fallback", label: "fallback", value: boolText(event.fallback) },
          {
            key: "parent",
            label: "父事件",
            value: event.parent_event_id ? (
              <span className="font-mono text-[11px]">{event.parent_event_id}</span>
            ) : (
              "无（顶层事件）"
            ),
          },
        ]}
      />

      <EventBodyPreviews event={event} />

      {Object.keys(event.metadata).length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">Metadata</h3>
          <JsonBlock value={event.metadata} maxHeight="16rem" />
        </section>
      ) : null}

      {event.notes.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">这次事件的口径说明</h3>
          <ul className="flex list-disc flex-col gap-1 pl-5 text-[11px] leading-5 text-muted-foreground">
            {event.notes.map((note) => (
              <li key={note} className="break-words">
                {note}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {onOpenPerformance ? (
        <div>
          <Button variant="outline" size="xs" onClick={() => onOpenPerformance(event.event_id)}>
            <Timer />
            在性能里查看这次调用
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------ 模型交互预览（feedback §3） ------------------------------ */

type PreviewSlot = "system_preview" | "user_preview" | "context_preview" | "assistant_preview";

/** 槽位 → 短标签（聚合「哪些没返回」时用）。 */
const PREVIEW_SHORT_LABELS: Record<PreviewSlot, string> = {
  system_preview: "系统提示",
  user_preview: "用户输入",
  context_preview: "注入的上下文",
  assistant_preview: "模型返回",
};

/**
 * 各角色要展示的预览槽位。
 * ASSISTANT 才是完整的一次模型交互：三段输入 + 一段返回，缺哪段都要能一眼看出来；
 * SYSTEM / USER / CONTEXT 是这条交互的三段输入，各自只展示自己那一段。
 * 未登记的类型没有槽位，退回既有的 input_preview / output_preview 口径（见 EventBodyPreviews）。
 */
const ROLE_PREVIEW_SLOTS: Record<string, PreviewSlot[]> = {
  SYSTEM: ["system_preview"],
  USER: ["user_preview"],
  CONTEXT: ["context_preview"],
  ASSISTANT: ["system_preview", "user_preview", "context_preview", "assistant_preview"],
};

/**
 * 单个槽位的取值。
 *
 * 旧字段兜底只在语义对得上的地方做（都是后端已经脱敏 + 截断过的既有字段，前端不拼接、不改写）：
 * USER 事件的 input_preview 就是用户请求，CONTEXT 事件与 ASSISTANT 的 output_preview 是上下文摘要 / 模型输出。
 */
function previewValue(event: AdminTraceEvent, slot: PreviewSlot): string | null {
  const direct: Record<PreviewSlot, string | null | undefined> = {
    system_preview: event.system_preview,
    user_preview: event.user_preview,
    context_preview: event.context_preview,
    assistant_preview: event.assistant_preview,
  };
  const directValue = direct[slot];
  if (typeof directValue === "string" && directValue.trim().length > 0) return directValue;
  const legacy: Record<PreviewSlot, string | null> = {
    system_preview: null,
    user_preview: event.input_preview,
    context_preview: event.output_preview,
    assistant_preview: event.output_preview,
  };
  const legacyValue = legacy[slot];
  return typeof legacyValue === "string" && legacyValue.trim().length > 0 ? legacyValue : null;
}

/**
 * 「模型收到了什么 / 返回了什么」。
 *
 * 后端字段（A13）还没落地的槽位只渲染一行缺口口径（不渲染空框、也不拿别的字段顶替）——
 * 假造一段正文比空着更糟：它会让人以为模型真的收到了那段内容。
 */
function EventBodyPreviews({ event }: { event: AdminTraceEvent }) {
  const slots = ROLE_PREVIEW_SLOTS[event.event_type] ?? [];

  if (slots.length === 0) {
    // 工具 / Provider / 校验 / 错误：没有对话角色，沿用既有预览口径；
    // TOOL 的 output_preview 就是对话里的 Tool Result，标签直接写出来。
    return (
      <>
        <PreviewBlock
          label={event.event_type === "TOOL" ? "调用参数" : "Input 预览"}
          value={event.input_preview}
          missing="—（这次事件没有记录该预览）"
        />
        <PreviewBlock
          label={event.event_type === "TOOL" ? TOOL_RESULT_LABEL : "Output 预览"}
          value={event.output_preview}
          missing="—（这次事件没有记录该预览）"
        />
      </>
    );
  }

  const values = slots.map((slot) => ({
    slot,
    side: slot === "assistant_preview" ? "模型返回" : "模型收到",
    value: previewValue(event, slot),
  }));
  const present = values.filter((item) => item.value !== null);
  const absent = values.filter((item) => item.value === null);
  // 只有旧的 input_preview（不区分 system / user / context）时也要显示：它至少能回答
  // 「模型收到的是不是这一份」，只是归不了段 —— 按它自己的标签单独列出来，不冒充某一段。
  const undividedInput =
    values.every((item) => item.slot === "assistant_preview" || item.value === null)
      ? (event.input_preview ?? null)
      : null;

  return (
    <>
      {present.map((item) => (
        <PreviewBlock
          key={item.slot}
          label={`${item.side} · ${PREVIEW_SHORT_LABELS[item.slot]}（${item.slot}）`}
          value={item.value}
        />
      ))}
      {undividedInput ? (
        <PreviewBlock
          label="模型收到 · 输入预览（input_preview，未区分系统 / 用户 / 上下文）"
          value={undividedInput}
        />
      ) : null}
      {absent.length > 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-[11px] leading-5 break-words text-muted-foreground">
          未返回的正文：{absent.map((item) => `${item.side} · ${PREVIEW_SHORT_LABELS[item.slot]}`).join("、")}。
          {MISSING_PREVIEW_HINT}
        </p>
      ) : null}
    </>
  );
}

/** 后端 A13 新增的标量字段：有就显示，没有就写「—（后端未返回）」，不留空白行。 */
function a13FieldText(value: string | null | undefined) {
  if (typeof value === "string" && value.length > 0) {
    return <span className="font-mono text-[11px]">{value}</span>;
  }
  return <span className="text-muted-foreground">—（后端未返回）</span>;
}

/**
 * 事件级 token 事实。新字段（input/output/cached_tokens）优先，旧字段（tokens_in/out）兜底；
 * 一个都没有时返回空数组，让调用方写「按整次 run 统计」——而不是一个会被误读成「没消耗」的「—」。
 */
function readEventTokenFacts(event: AdminTraceEvent): string[] {
  const input = event.input_tokens ?? event.tokens_in;
  const output = event.output_tokens ?? event.tokens_out;
  const cached = event.cached_tokens ?? null;
  const parts: string[] = [];
  if (input !== null) parts.push(`输入 ${formatNumber(input)}`);
  if (output !== null) parts.push(`输出 ${formatNumber(output)}`);
  if (cached !== null) parts.push(`缓存 ${formatNumber(cached)}`);
  return parts;
}

/* ------------------------------ 汇总 ------------------------------ */

function BadcaseSummary({ badcases }: { badcases: AdminTraceBadcaseBrief[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>Bad Case</span>
          <Badge variant="secondary" className="tabular text-[0.6875rem]">
            {formatNumber(badcases.length)}
          </Badge>
        </CardTitle>
        <CardDescription>这次运行触发的全部问题案例；时间轴上的 ⚠ 标记与这里是同一批数据。</CardDescription>
      </CardHeader>
      <CardContent>
        {badcases.length === 0 ? (
          <SectionEmpty
            title="这次运行没有登记 Bad Case"
            description="规则判定、人工标注与模型评审都没有为本次运行登记问题。"
            action={
              <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
                打开问题案例页
              </Button>
            }
          />
        ) : (
          <ul className="flex flex-col gap-2">
            {badcases.map((badcase) => (
              <li key={badcase.badcase_id} className="flex flex-col gap-1 rounded-lg border border-border px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-foreground">
                    {badcaseCategoryLabel(badcase.category)}
                  </span>
                  <StatusBadge status={badcase.severity} />
                  <StatusBadge status={badcase.analysis_status} />
                </div>
                <p className="text-[11px] leading-5 break-words text-muted-foreground">
                  {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
                </p>
              </li>
            ))}
            <li>
              <Link href="/admin/badcases" className="text-xs text-primary underline-offset-4 hover:underline">
                去问题案例页处理
              </Link>
            </li>
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** 口径说明：后端明确说明「哪些数据本就不存在」，永远展示，不允许折叠隐藏。 */
function NotesPanel({ notes }: { notes: string[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>口径说明</CardTitle>
        <CardDescription>这些是这次轨迹「没有」的东西与原因，避免把缺口读成系统故障。</CardDescription>
      </CardHeader>
      <CardContent>
        {notes.length === 0 ? (
          <p className="text-xs text-muted-foreground">后端这次没有附口径说明。</p>
        ) : (
          <ul className="flex list-disc flex-col gap-1.5 pl-5 text-xs leading-5 text-muted-foreground">
            {notes.map((note) => (
              <li key={note} className="break-words">
                {note}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------ 工具函数 ------------------------------ */

export function eventAnchorId(eventId: string): string {
  return `timeline-event-${eventId}`;
}

function rankGroup(key: string, order: Map<string, number>): number {
  if (key === STAGE_PROLOGUE) return -1;
  if (key === STAGE_NONE) return Number.MAX_SAFE_INTEGER;
  return order.get(key) ?? Number.MAX_SAFE_INTEGER - 1;
}

/**
 * 关键词匹配。
 *
 * 为什么连预览正文也一起搜：feedback §3 里「Dynamic Preference 有没有传给模型」这类问题，
 * 最快的验证方式就是拿偏好关键词在轨迹里搜一遍，看它出现在哪个调用的 context / user 预览里。
 * 正文是后端脱敏截断过的，前端只是把它当作可搜文本，不渲染、不外泄。
 */
function matchesQuery(event: AdminTraceEvent, needle: string): boolean {
  const haystack = [
    event.title,
    event.summary,
    event.tool,
    event.provider,
    event.stage,
    event.input_preview,
    event.output_preview,
    event.system_preview,
    event.user_preview,
    event.context_preview,
    event.assistant_preview,
    event.prompt_version,
    event.prompt_hash,
  ]
    .filter((value): value is string => typeof value === "string" && value.length > 0)
    .join("\n")
    .toLowerCase();
  return haystack.includes(needle);
}

function readCount(record: AdminRecord, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 事件级 token：后端没有逐条口径时明确写「按整次 run 统计」，而不是一个裸「—」。 */
function eventTokenText(value: number | null, runTotalTokens: number | null): string {
  if (value !== null && Number.isFinite(value)) return formatNumber(value);
  return runTotalTokens !== null ? "按整次 run 统计" : "—";
}

function boolText(value: boolean | null): string {
  if (value === null) return "未记录";
  return value ? "是" : "否";
}
