"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, RefreshCw, Search, Timer } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { JsonBlock, KeyValueList } from "@/components/admin/detail-dialog";
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
 * 三个刻意的取舍：
 * 1) 顶部轨道只画「有时间戳」的节点；没有起点的节点不静默丢掉，而是在轨道下方如实列出，
 *    否则读者会以为这次 run 根本没调过那个工具。
 * 2) 事件默认只给类型 / 标题 / 摘要 / 状态 / 耗时（§17），Input / Output / metadata 折叠在详情里。
 * 3) 事件级 token 与成本通常是 null（Provider 不按调用回报用量），这里不显示裸「—」，
 *    而是明确写「按整次 run 统计」，避免把「没有逐条口径」读成「没有消耗」。
 */

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
  className?: string;
}

export function RunTimeline({
  runId,
  initialStage,
  initialQuery,
  focusToken,
  onSelectStage,
  onOpenPerformance,
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
          />
        )}
      </ResourceView>
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
}: {
  timeline: AdminTimeline;
  onReload: () => void;
  initialStage?: string | null;
  initialQuery?: string;
  focusToken?: number;
  onSelectStage?: (stage: string | null) => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  const [typeFilter, setTypeFilter] = useState<string>(FILTER_ALL);
  const [stageFilter, setStageFilter] = useState<string>(initialStage ?? FILTER_ALL);
  const [statusFilter, setStatusFilter] = useState<string>(FILTER_ALL);
  const [query, setQuery] = useState<string>(initialQuery ?? "");
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
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

  return (
    <div className="flex flex-col gap-4">
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
          </CardTitle>
          <CardDescription>
            按真实执行顺序把 trace / Provider 账本 / 校验与 Bad Case 归并成一条时间轴；点类型徽标可只看该类事件。
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

      <TrackOverview timeline={timeline} onPickBlock={pickBlock} />

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
          label="Workflow 阶段"
          value={stageFilter}
          onChange={setStageFilter}
          className="w-44"
          options={[
            { value: FILTER_ALL, label: "全部阶段" },
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
              placeholder="标题 / 摘要 / 工具 / Provider / 阶段 / 预览"
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

      <Card>
        <CardHeader>
          <CardTitle>详细事件时间线</CardTitle>
          <CardDescription>
            左侧竖线是 workflow 阶段的分组；默认只给类型 / 标题 / 摘要 / 状态 / 耗时，展开详情才看 Input、Output 与 metadata。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {visible.length === 0 ? (
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
                          onSelect={() =>
                            setSelectedEventId((current) => (current === event.event_id ? null : event.event_id))
                          }
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

/* ------------------------------ 事件行 ------------------------------ */

function TimelineEventRow({
  event,
  runTotalTokens,
  selected,
  onSelect,
  onOpenPerformance,
}: {
  event: AdminTraceEvent;
  runTotalTokens: number | null;
  selected: boolean;
  onSelect: () => void;
  onOpenPerformance?: (eventId: string) => void;
}) {
  const tone = EVENT_TONES[event.event_type] ?? "muted";
  const hasTokenFacts = event.tokens_in !== null || event.tokens_out !== null || event.cost !== null;

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
          <ToneBadge tone={tone} className="text-[0.6875rem]">
            {event.event_type_label || event.event_type}
          </ToneBadge>
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
          {event.round_index !== null ? <span>阶段序 #{event.round_index + 1}</span> : null}
          {hasTokenFacts ? (
            <>
              {event.tokens_in !== null || event.tokens_out !== null ? (
                <span className="tabular">
                  Token {formatNumber(event.tokens_in)} → {formatNumber(event.tokens_out)}
                </span>
              ) : null}
              {event.cost !== null ? <span className="tabular">成本 {formatCostWithCurrency(event.cost, null)}</span> : null}
            </>
          ) : runTotalTokens !== null ? (
            <span>token 与成本按整次 run 统计</span>
          ) : null}
          <span>展开详情看 Input / Output / metadata</span>
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

      <CollapsibleSection
        title="展开详情"
        description="Input / Output 预览、metadata 与口径说明"
        count={event.notes.length > 0 ? `${event.notes.length} 条口径` : undefined}
        className="rounded-none bg-transparent ring-0"
      >
        <div className="flex flex-col gap-3">
          <KeyValueList
            entries={[
              { key: "event_id", label: "事件 ID", value: <span className="font-mono text-[11px]">{event.event_id}</span> },
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
                value: eventTokenText(event.tokens_in, runTotalTokens),
              },
              {
                key: "tokens_out",
                label: "输出 Token",
                value: eventTokenText(event.tokens_out, runTotalTokens),
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

          <PreviewBlock label="Input 预览" value={event.input_preview} />
          <PreviewBlock label="Output 预览" value={event.output_preview} />

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
      </CollapsibleSection>
    </li>
  );
}

function PreviewBlock({ label, value }: { label: string; value: string | null }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-medium text-foreground">{label}</h3>
      {value ? (
        <pre className="max-h-60 overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-[11px] leading-4 break-all whitespace-pre-wrap text-foreground">
          {value}
        </pre>
      ) : (
        <p className="text-[11px] leading-5 text-muted-foreground">
          —（这次事件没有记录该预览；模型输入 / 输出正文按口径不落库）
        </p>
      )}
    </section>
  );
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

function matchesQuery(event: AdminTraceEvent, needle: string): boolean {
  const haystack = [
    event.title,
    event.summary,
    event.tool,
    event.provider,
    event.stage,
    event.input_preview,
    event.output_preview,
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
