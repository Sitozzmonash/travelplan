"use client";

import { useState } from "react";
import { ChevronRight, Gauge } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AdminDetailDialog } from "@/components/admin/detail-dialog";
import { formatDurationMs, formatNumber, formatRatio } from "@/components/admin/format";
import { StatCard } from "@/components/admin/stat-card";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import type {
  AdminJevCall,
  AdminLlmCall,
  AdminRecord,
  AdminRunMetrics,
  AdminStageDetail,
  AdminTraceSpan,
  AdminUserJourney,
} from "@/types/admin";

/**
 * Run Detail 的性能摘要（任务 Part M）。
 *
 * 目标只有一个：让操作员一眼看出「这次 run 的时间花在哪」。
 *
 * 数据来源分两级，全部取自后端**已经返回**的字段：
 * 1) 后端把性能摘要写进 `run_metrics`（`metrics.performance_summary` 或同名扁平字段）时优先使用；
 * 2) 尚未落地时，前端按 `/runs/{id}/stages` 的阶段耗时 + Trace 里的 workflow / tool / llm / jev
 *    span 现场推导。
 *
 * 绝不编造：任何一处推不出来就显示「—」，并把缺口写进 `gaps`，在明细弹窗里如实列出。
 */

/* ------------------------------ 类型 ------------------------------ */

export interface PerformanceMetric {
  key: string;
  label: string;
  ms: number | null;
  /** 这个数字从哪来的（后端摘要 / 哪个阶段的耗时 / Trace 里的哪类 span）。 */
  provenance: string;
}

export interface PerformanceCount {
  key: string;
  label: string;
  value: string;
  hint?: string;
}

export interface PerformanceStageRow {
  stageId: string;
  title: string;
  status: string | null;
  ms: number | null;
  /** 归入的摘要维度（交通 / 酒店 / …）；未归入时为 null。 */
  bucket: string | null;
}

export interface PerformanceSummary {
  /** 数据来源标签：后端性能摘要 / 前端按阶段推导。 */
  sourceLabel: string;
  sourceTone: "info" | "muted";
  totalMs: number | null;
  metrics: PerformanceMetric[];
  counts: PerformanceCount[];
  /** 全部阶段（含未被 8 个维度覆盖的），用于明细弹窗，保证不隐藏任何耗时。 */
  allStages: PerformanceStageRow[];
  /** 真实存在的缺口：API 没给、也确实推不出来的指标。 */
  gaps: string[];
}

/* ------------------------------ 常量 ------------------------------ */

const BUCKET_LABELS: Record<string, string> = {
  transport: "交通",
  hotel: "酒店",
  discovery: "发现",
  poi_verify: "POI 核验",
  route: "路线",
  planner: "规划",
  llm: "LLM",
  jev: "Jev",
};

/** 维度 → 对应的 workflow 阶段 id。 */
const STAGE_BUCKETS: Record<string, string[]> = {
  transport: ["search_intercity_transport"],
  hotel: ["search_hotels"],
  discovery: ["search_social_guides", "extract_and_normalize_places"],
  poi_verify: ["verify_poi_and_routes"],
  planner: ["score_candidates", "build_initial_plan"],
};

/** 展示顺序固定，避免移动端每次渲染顺序跳动。 */
const METRIC_ORDER = [
  "transport",
  "hotel",
  "discovery",
  "poi_verify",
  "route",
  "planner",
  "llm",
  "jev",
] as const;

/* ------------------------------ 工具 ------------------------------ */

function isRecord(value: unknown): value is AdminRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function durationBetween(startedAt: string | null | undefined, finishedAt: string | null | undefined): number | null {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

/** span 耗时：优先 attributes.duration_ms（后端就是这么落的），否则按起止时间算。 */
function spanDurationMs(span: AdminTraceSpan): number | null {
  const fromAttributes = toFiniteNumber(span.attributes?.duration_ms);
  if (fromAttributes !== null) return fromAttributes;
  return durationBetween(span.started_at, span.finished_at);
}

function spanTool(span: AdminTraceSpan): string {
  const tool = span.attributes?.tool;
  return typeof tool === "string" ? tool : "";
}

/* ------------------------------ 后端性能摘要读取 ------------------------------ */

/**
 * 从 `run_metrics` 里读一个性能值。后端可能把它放在：
 * `metrics.performance_summary`（推荐）、`performance_summary.stages`、
 * `performance_summary.stage_durations`，或直接平铺在 `metrics.*` 上。
 * 逐个容器找第一个命中，避免因为后端换了一种信封就整块失效。
 */
function readSummaryNumber(
  perf: AdminRecord | null,
  metrics: AdminRecord,
  keys: readonly string[],
): number | null {
  const containers: AdminRecord[] = [];
  if (perf) {
    containers.push(perf);
    if (isRecord(perf.stages)) containers.push(perf.stages);
    if (isRecord(perf.stage_durations)) containers.push(perf.stage_durations);
    if (isRecord(perf.durations)) containers.push(perf.durations);
  }
  containers.push(metrics);
  for (const container of containers) {
    for (const key of keys) {
      const value = toFiniteNumber(container[key]);
      if (value !== null) return value;
    }
  }
  return null;
}

const SUMMARY_MS_KEYS: Record<(typeof METRIC_ORDER)[number], readonly string[]> = {
  transport: ["transport_ms"],
  hotel: ["hotel_ms", "hotels_ms"],
  discovery: ["discovery_ms", "prefetch_ms"],
  poi_verify: ["poi_verify_ms", "poi_verification_ms", "verify_ms"],
  route: ["route_ms", "route_lookup_ms"],
  planner: ["planner_ms", "plan_ms", "planning_ms"],
  llm: ["llm_ms", "llm_duration_ms"],
  jev: ["jev_ms", "jev_duration_ms"],
};

/* ------------------------------ 阶段耗时 ------------------------------ */

/** 阶段 id → 耗时（毫秒）。stages 缺失时退回 Trace 的 workflow span。 */
function buildStageDurationMap(stages: AdminStageDetail[], trace: AdminTraceSpan[]): Map<string, number | null> {
  const map = new Map<string, number | null>();
  for (const stage of stages) {
    const ms = stage.duration_ms ?? durationBetween(stage.started_at, stage.finished_at);
    if (stage.stage_id) map.set(stage.stage_id, ms);
  }
  if (map.size > 0) return map;
  for (const span of trace) {
    if (span.component !== "workflow") continue;
    map.set(span.name, spanDurationMs(span));
  }
  return map;
}

function sumStageDurations(map: Map<string, number | null>, ids: string[]): number | null {
  let sum = 0;
  let hasAny = false;
  for (const id of ids) {
    const value = map.get(id);
    if (typeof value === "number") {
      sum += value;
      hasAny = true;
    }
  }
  return hasAny ? sum : null;
}

/** 路线：Trace 里 tool=route 的 span。 */
function routeFromTrace(trace: AdminTraceSpan[]): { ms: number | null; count: number } {
  let sum = 0;
  let count = 0;
  for (const span of trace) {
    if (span.component !== "tool" || spanTool(span) !== "route") continue;
    count += 1;
    sum += spanDurationMs(span) ?? 0;
  }
  return { ms: count > 0 ? sum : null, count };
}

function sumDurations(values: (number | null | undefined)[]): number | null {
  let sum = 0;
  let hasAny = false;
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) {
      sum += value;
      hasAny = true;
    }
  }
  return hasAny ? sum : null;
}

/* ------------------------------ 交接（复用 / 补查） ------------------------------ */

export interface HandoffRow {
  /** transport / hotels / social / places */
  key: string;
  label: string;
  /** reused | fallback_query | unavailable | null */
  handoff: string | null;
}

const HANDOFF_LINES: { key: string; label: string }[] = [
  { key: "transport", label: "交通" },
  { key: "hotels", label: "酒店" },
  { key: "social", label: "攻略" },
  { key: "places", label: "地点" },
];

/**
 * 读出四条线的交接结果。
 * 优先用 `user_journey.discovery`；后端还没把这一块接进 API 时，退回 Trace 里的
 * `discovery_handoff` span（workflow finalize 时落库，属性名就是这四条线）。
 */
export function readHandoffRows(
  journey: AdminUserJourney | null,
  trace: AdminTraceSpan[],
): HandoffRow[] | null {
  const discovery = journey?.discovery;
  if (discovery && Object.keys(discovery).length > 0) {
    return HANDOFF_LINES.map(({ key, label }) => {
      const info = discovery[key];
      const handoff = info && typeof info.handoff === "string" ? info.handoff : null;
      return { key, label: info?.label || label, handoff };
    });
  }
  const span = trace.find((item) => item.name === "discovery_handoff");
  if (!span) return null;
  const attributes = span.attributes ?? {};
  const anyKnown = HANDOFF_LINES.some(({ key }) => typeof attributes[key] === "string");
  if (!anyKnown) return null;
  return HANDOFF_LINES.map(({ key, label }) => ({
    key,
    label,
    handoff: typeof attributes[key] === "string" ? (attributes[key] as string) : null,
  }));
}

/** Discovery 交接等待：优先 user_journey.grace_waited_ms，否则 Trace 的 discovery_handoff span。 */
export function readGraceWaitedMs(journey: AdminUserJourney | null, trace: AdminTraceSpan[]): number | null {
  const direct = toFiniteNumber(journey?.grace_waited_ms);
  if (direct !== null) return direct;
  const span = trace.find((item) => item.name === "discovery_handoff");
  return span ? toFiniteNumber(span.attributes?.grace_waited_ms) : null;
}

/** 交接值 → 中文标签 + 色调。 */
export function handoffPresentation(value: string | null): {
  label: string;
  tone: "success" | "warning" | "muted";
  description: string;
} {
  switch (value) {
    case "reused":
      return { label: "复用预取", tone: "success", description: "直接使用 Discovery 已经查好的结果" };
    case "fallback_query":
      return { label: "本次补查", tone: "warning", description: "Discovery 没兜住，本次重新查询并拿到了结果" };
    case "unavailable":
      return { label: "无可用结果", tone: "muted", description: "本次补查过，但仍然没有可用结果" };
    default:
      return { label: "未返回", tone: "muted", description: "后端没有给出这条线的交接结论" };
  }
}

/* ------------------------------ 主推导 ------------------------------ */

export function buildPerformanceSummary({
  metrics,
  stages,
  trace,
  llmCalls,
  jevCalls,
  journey,
}: {
  metrics: AdminRunMetrics;
  stages: AdminStageDetail[];
  trace: AdminTraceSpan[];
  llmCalls: AdminLlmCall[];
  jevCalls: AdminJevCall[];
  journey: AdminUserJourney | null;
}): PerformanceSummary {
  const metricRecord = metrics as AdminRecord;
  const perf = isRecord(metricRecord.performance_summary) ? metricRecord.performance_summary : null;
  const gaps: string[] = [];

  const stageMap = buildStageDurationMap(stages, trace);

  // ---- 各阶段耗时：后端摘要优先，其次按阶段推导 ----
  const derived: Record<(typeof METRIC_ORDER)[number], { ms: number | null; provenance: string }> = {
    transport: { ms: sumStageDurations(stageMap, STAGE_BUCKETS.transport), provenance: "阶段 search_intercity_transport" },
    hotel: { ms: sumStageDurations(stageMap, STAGE_BUCKETS.hotel), provenance: "阶段 search_hotels" },
    discovery: {
      ms: sumStageDurations(stageMap, STAGE_BUCKETS.discovery),
      provenance: "阶段 社交攻略 + 地点抽取",
    },
    poi_verify: { ms: null, provenance: "阶段 verify_poi_and_routes" },
    route: { ms: null, provenance: "Trace 中 tool=route 的 span（同一工具多次调用会合并成一条）" },
    planner: {
      ms: sumStageDurations(stageMap, STAGE_BUCKETS.planner),
      provenance: "阶段 候选打分 + 生成初始行程",
    },
    llm: { ms: sumDurations(llmCalls.map((call) => call.duration_ms)), provenance: "累计所有 LLM 调用耗时" },
    jev: { ms: sumDurations(jevCalls.map((call) => call.latency_ms)), provenance: "累计所有 Jev 决策时延" },
  };

  // POI 核验与路线共用同一个阶段：POI 核验取阶段耗时，路线单列 Trace。
  const verifyStageMs = sumStageDurations(stageMap, STAGE_BUCKETS.poi_verify);
  derived.poi_verify.ms = verifyStageMs;
  const route = routeFromTrace(trace);
  derived.route.ms = route.ms;

  const metricsList: PerformanceMetric[] = METRIC_ORDER.map((key) => {
    const backendMs = readSummaryNumber(perf, metricRecord, SUMMARY_MS_KEYS[key]);
    if (backendMs !== null) {
      return { key, label: BUCKET_LABELS[key], ms: backendMs, provenance: "后端性能摘要" };
    }
    return {
      key,
      label: BUCKET_LABELS[key],
      ms: derived[key].ms,
      provenance: derived[key].ms === null ? "—" : derived[key].provenance,
    };
  });

  // ---- 总量 ----
  const backendTotal = readSummaryNumber(perf, metricRecord, ["total_duration_ms", "total_ms"]);
  const totalMs = backendTotal ?? toFiniteNumber(metricRecord.duration_ms) ?? sumStageDurations(stageMap, [...stageMap.keys()]);

  // ---- 计数类 ----
  const stageCounts = derivedStageCounts(journey, trace);

  const backendProviderCalls = readSummaryNumber(perf, metricRecord, ["provider_calls", "provider_call_count"]);
  const providerCalls =
    backendProviderCalls ??
    toFiniteNumber(metricRecord.tool_calls) ??
    (typeof metricRecord.provider_calls === "number" ? metricRecord.provider_calls : null);

  const cacheHits = readSummaryNumber(perf, metricRecord, ["cache_hits", "cached_hits", "cache_hit_count"]);

  const backendPrefetchReused = readSummaryNumber(perf, metricRecord, ["prefetch_reused", "prefetch_reused_count"]);
  const prefetchReused = backendPrefetchReused ?? stageCounts.reused;
  // 逐条线的交接拿不到时，退回 user_journey.prefetch_reused 的布尔口径（旧 run 只有它）。
  const journeyReusedText = prefetchReusedBooleanText(journey);

  const backendFallback = readSummaryNumber(perf, metricRecord, ["fallback_count", "fallback_query_count"]);
  const fallbackCount = backendFallback ?? stageCounts.fallback;

  if (cacheHits === null) {
    gaps.push(
      "缓存命中：当前 API 的 run 详情没有返回缓存命中次数（sources 表不含 cached 标记），后端性能摘要落地前无法展示。",
    );
  }
  if (route.count > 1) {
    gaps.push(
      `路线耗时：Trace 按工具名合并 span，${route.count} 次 route 调用只剩一条记录，因此「路线」耗时是单次值而非总和；后端性能摘要提供 route_ms 后可消除该误差。`,
    );
  }
  if (journey && stageCounts.reused === null && backendPrefetchReused === null && journeyReusedText === null) {
    gaps.push("预取复用：user_journey 与 Trace 都没有 Discovery 交接信息，无法判断每条线是否复用。");
  }
  if (!journey) {
    gaps.push("本次运行不是引导式创建，没有 user_journey，因此没有 Discovery 复用 / 补查信息。");
  }

  const prefetchValue = stageCounts.reusedText ?? journeyReusedText ?? (prefetchReused === null ? "—" : formatNumber(prefetchReused));
  const fallbackValue = stageCounts.fallbackText ?? (fallbackCount === null ? "—" : formatNumber(fallbackCount));

  const counts: PerformanceCount[] = [
    {
      key: "provider_calls",
      label: "Provider 调用",
      value: providerCalls === null ? "—" : formatNumber(providerCalls),
      hint: backendProviderCalls !== null ? "来自后端性能摘要" : providerCalls === null ? "API 未返回" : "run 的 provider 调用次数",
    },
    {
      key: "cache_hits",
      label: "缓存命中",
      value: cacheHits === null ? "—" : formatNumber(cacheHits),
      hint: cacheHits === null ? "API 未返回" : "来自后端性能摘要",
    },
    {
      key: "prefetch_reused",
      label: "预取复用",
      value: prefetchValue,
      hint:
        prefetchValue === "—"
          ? "API 未返回"
          : stageCounts.reusedText
            ? "已复用的 Discovery 线路"
            : "user_journey 口径（未提供逐条线结果）",
    },
    {
      key: "fallback_count",
      label: "补查 / fallback",
      value: fallbackValue,
      hint:
        fallbackValue === "—"
          ? "API 未返回"
          : stageCounts.fallbackText
            ? "本次重新查询的线路数"
            : "来自后端性能摘要",
    },
  ];

  const allStages: PerformanceStageRow[] = stages.map((stage) => ({
    stageId: stage.stage_id,
    title: stage.title || stage.stage_id,
    status: stage.status ?? null,
    ms: stage.duration_ms ?? durationBetween(stage.started_at, stage.finished_at),
    bucket: bucketOf(stage.stage_id),
  }));

  const hasBackendSummary = perf !== null;
  return {
    sourceLabel: hasBackendSummary ? "后端性能摘要" : "按阶段推导",
    sourceTone: hasBackendSummary ? "info" : "muted",
    totalMs,
    metrics: metricsList,
    counts,
    allStages,
    gaps: Array.from(new Set(gaps)),
  };
}

function bucketOf(stageId: string): string | null {
  for (const [bucket, ids] of Object.entries(STAGE_BUCKETS)) {
    if (ids.includes(stageId)) return BUCKET_LABELS[bucket] ?? bucket;
  }
  return null;
}

interface StageCounts {
  reused: number | null;
  reusedText: string | null;
  reusedTotal: number | null;
  fallback: number | null;
  fallbackText: string | null;
}

/** 从 user_journey.discovery（优先）或 Trace 的 discovery_handoff span 统计复用 / 补查线路数。 */
function derivedStageCounts(journey: AdminUserJourney | null, trace: AdminTraceSpan[]): StageCounts {
  const rows = readHandoffRows(journey, trace);
  if (!rows || rows.length === 0) return emptyCounts();
  const reused = rows.filter((row) => row.handoff === "reused").length;
  const fallback = rows.filter((row) => row.handoff === "fallback_query").length;
  const unavailable = rows.filter((row) => row.handoff === "unavailable").length;
  const known = reused + fallback + unavailable;
  if (known === 0) return emptyCounts();
  const total = rows.length;
  return {
    reused,
    reusedText: `${reused} / ${total} 条线`,
    reusedTotal: total,
    fallback,
    fallbackText: `${fallback} / ${total} 条线`,
  };
}

function emptyCounts(): StageCounts {
  return { reused: null, reusedText: null, reusedTotal: null, fallback: null, fallbackText: null };
}

/**
 * `user_journey.prefetch_reused` 的两种口径：旧 run 是布尔，新 run 是「每条线的布尔」。
 * 逐条线的 discovery 拿不到时用它兜底，避免旧 run 的复用信息整块消失。
 */
function prefetchReusedBooleanText(journey: AdminUserJourney | null): string | null {
  const value = journey?.prefetch_reused;
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value ? "是" : "否";
  const entries = Object.entries(value);
  if (entries.length === 0) return null;
  const reused = entries.filter(([, flag]) => flag).length;
  return `${reused} / ${entries.length} 条线`;
}

/* ------------------------------ 组件 ------------------------------ */

export function RunPerformanceSummary({
  metrics,
  stages,
  trace,
  llmCalls,
  jevCalls,
  journey,
}: {
  metrics: AdminRunMetrics;
  stages: AdminStageDetail[];
  trace: AdminTraceSpan[];
  llmCalls: AdminLlmCall[];
  jevCalls: AdminJevCall[];
  journey: AdminUserJourney | null;
}) {
  const [open, setOpen] = useState(false);
  const summary = buildPerformanceSummary({ metrics, stages, trace, llmCalls, jevCalls, journey });
  const total = summary.totalMs && summary.totalMs > 0 ? summary.totalMs : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>性能摘要</span>
          <ToneBadge tone={summary.sourceTone} className="text-[0.6875rem]">
            {summary.sourceLabel}
          </ToneBadge>
        </CardTitle>
        <CardDescription>
          各阶段耗时与调用统计，用于快速判断这次 run 的瓶颈在哪一段。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <StatCard
            label="总耗时"
            value={formatDurationMs(summary.totalMs)}
            icon={Gauge}
            tone="info"
            className="col-span-2 sm:col-span-1"
          />
          {summary.counts.map((count) => (
            <StatCard key={count.key} label={count.label} value={count.value} hint={count.hint} />
          ))}
        </div>

        <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {summary.metrics.map((metric) => (
            <li key={metric.key} className="flex min-w-0 flex-col gap-1 rounded-lg border border-border px-3 py-2">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-medium text-foreground">{metric.label}</span>
                <span className="tabular shrink-0 text-xs text-foreground">{formatDurationMs(metric.ms)}</span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden>
                <div
                  className="h-full rounded-full bg-primary/70"
                  style={{ width: shareWidth(metric.ms, total) }}
                />
              </div>
              <span className="text-[11px] leading-4 break-words text-muted-foreground">
                {shareText(metric.ms, total)}
                {metric.provenance !== "—" ? ` · ${metric.provenance}` : ""}
              </span>
            </li>
          ))}
        </ul>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" size="xs" onClick={() => setOpen(true)}>
            查看性能明细
            <ChevronRight />
          </Button>
          {summary.gaps.length > 0 ? (
            <span className="text-[11px] text-muted-foreground">
              {summary.gaps.length} 项指标 API 未返回，见明细。
            </span>
          ) : null}
        </div>
      </CardContent>

      <AdminDetailDialog
        open={open}
        onOpenChange={setOpen}
        title="性能明细"
        description={`数据来源：${summary.sourceLabel}`}
      >
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">阶段耗时</h3>
          <p className="text-[11px] leading-4 text-muted-foreground">
            LLM / Jev / 路线发生在所属阶段内部，与阶段耗时有重叠，占比合计可能超过 100%。
          </p>
          <dl className="grid grid-cols-1 gap-x-5 gap-y-2 sm:grid-cols-2">
            {summary.metrics.map((metric) => (
              <div
                key={metric.key}
                className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
              >
                <dt className="min-w-0 truncate text-xs text-muted-foreground" title={metric.label}>
                  {metric.label}
                </dt>
                <dd className="tabular min-w-0 shrink-0 text-right text-xs font-medium text-foreground">
                  {formatDurationMs(metric.ms)}
                  <span className="ml-2 font-normal text-muted-foreground">{shareText(metric.ms, total)}</span>
                </dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">调用统计</h3>
          <dl className="grid grid-cols-1 gap-x-5 gap-y-2 sm:grid-cols-2">
            {summary.counts.map((count) => (
              <div
                key={count.key}
                className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
              >
                <dt className="min-w-0 truncate text-xs text-muted-foreground">{count.label}</dt>
                <dd className="tabular min-w-0 shrink-0 text-right text-xs font-medium text-foreground">
                  {count.value}
                </dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">
            全部阶段耗时（{summary.allStages.length}）
          </h3>
          {summary.allStages.length === 0 ? (
            <p className="text-xs text-muted-foreground">后端没有返回阶段列表。</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {summary.allStages.map((stage) => (
                <li
                  key={stage.stageId}
                  className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/60 pb-1.5 text-xs last:border-0"
                >
                  <span className="min-w-0 flex-1 truncate font-medium text-foreground" title={stage.stageId}>
                    {stage.title}
                  </span>
                  <StatusBadge status={stage.status} />
                  {stage.bucket ? (
                    <ToneBadge tone="muted" className="text-[0.625rem]">
                      归入 {stage.bucket}
                    </ToneBadge>
                  ) : null}
                  <span className="tabular ml-auto shrink-0 text-muted-foreground">
                    {formatDurationMs(stage.ms)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {summary.gaps.length > 0 ? (
          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-foreground">API 未提供的指标</h3>
            <ul className="flex list-disc flex-col gap-1 pl-5 text-[11px] leading-5 text-muted-foreground">
              {summary.gaps.map((gap) => (
                <li key={gap} className="break-words">
                  {gap}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </AdminDetailDialog>
    </Card>
  );
}

function shareText(ms: number | null, total: number | null): string {
  if (ms === null || total === null || total <= 0) return "—";
  return formatRatio(ms / total);
}

function shareWidth(ms: number | null, total: number | null): string {
  if (ms === null || total === null || total <= 0) return "0%";
  const ratio = Math.max(0, Math.min(1, ms / total));
  return `${(ratio * 100).toFixed(1)}%`;
}
