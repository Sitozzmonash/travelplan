"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Bug,
  ChevronRight,
  Coins,
  Cpu,
  GitBranch,
  MessagesSquare,
  RefreshCw,
  Wrench,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PageHeader } from "@/components/admin/page-header";
import { InlineError, ResourceView, SectionEmpty, SectionSkeleton } from "@/components/admin/admin-states";
import { AdminDetailDialog, JsonBlock, KeyValueList } from "@/components/admin/detail-dialog";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import {
  RunPerformanceSummary,
  handoffPresentation,
  readGraceWaitedMs,
  readHandoffRows,
} from "@/components/admin/run-performance";
import { RunTimeline, type TraceViewMode } from "@/components/admin/run-timeline";
import { StatCard } from "@/components/admin/stat-card";
import { SourceTag, StatusBadge, ToneBadge, type AdminTone } from "@/components/admin/status-badge";
import { TraceTree } from "@/components/admin/trace-tree";
import {
  badcaseCategoryLabel,
  cleanBadcaseSymptom,
  formatCostWithCurrency,
  formatDurationMs,
  formatMetricValue,
  formatNumber,
  formatRatio,
  formatTokenCount,
  formatUnitPrice,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import {
  getAdminRun,
  getAdminRunStages,
  getAdminRunTimeline,
  type AdminApiError,
} from "@/lib/admin-api";
import { formatDateTime, formatProviderQuery } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  AdminBadcase,
  AdminCostBreakdown,
  AdminDecision,
  AdminLlmCall,
  AdminProviderCall,
  AdminRecord,
  AdminRunDetail,
  AdminStageDetail,
  AdminStageLlmCall,
  AdminStageProgress,
  AdminStageTokens,
  AdminTimeline,
  AdminTraceSpan,
  AdminUserJourney,
} from "@/types/admin";

/**
 * 运行详情。
 *
 * 五个页签回答五个不同的问题：概览回答「用户要什么 / 系统给了什么 / 质量怎么样 / 哪里有问题」，
 * 轨迹回答「这次 run 到底发生了什么」，性能回答「哪里最慢」，Provider 回答「数据源稳不稳」，
 * Bad Case 回答「这次触发了哪些问题」。技术明细（LLM / Token / 原始 Trace）不再堆在首屏，
 * 但全部保留在对应页签里，只是位置变了。
 */
type RunTabKey = "overview" | "timeline" | "performance" | "provider" | "badcase";

const RUN_TABS: { key: RunTabKey; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "timeline", label: "轨迹" },
  { key: "performance", label: "性能" },
  { key: "provider", label: "Provider" },
  { key: "badcase", label: "Bad Case" },
];

interface TimelineFocus {
  stage: string | null;
  query: string;
  /** 每次跳转自增：轨迹页据此重新应用筛选（同一个阶段也可以反复跳）。 */
  token: number;
}

interface StageFocus {
  stage: string | null;
  token: number;
}

/**
 * 轨迹视图的深链：`/admin/runs/{id}?view=conversation` 直接落到轨迹页的对话视图。
 *
 * 只读不写 URL（不引 router 状态同步，静态导出下少一处水合风险）；挂载时读一次，
 * 与 runs-view / badcases-view 的 `readInitialParam` 是同一套习惯。
 */
function readInitialTraceView(): TraceViewMode | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("view") === "conversation" ? "conversation" : null;
}

export function RunDetailView({ runId }: { runId: string }) {
  const resource = useAdminResource<AdminRunDetail>(`admin-run:${runId}`, () => getAdminRun(runId));
  // 阶段明细来自独立端点，404/未实现时只影响「阶段」这一块，不会让整页失败。
  const stages = useAdminResource<AdminStageDetail[]>(`admin-run-stages:${runId}`, () =>
    getAdminRunStages(runId),
  );
  // 首屏的「质量 / 告警 / Bad Case」要用轨迹侧的派生字段；轨迹面板由 RunTimeline 自行拉取同一 key。
  const timeline = useAdminResource<AdminTimeline>(
    `admin-run-timeline:${runId}`,
    () => getAdminRunTimeline(runId),
    Boolean(runId),
  );

  // 视图状态放在这一层（跟 tab 同级）：RunTimeline 只负责渲染，
  // 这样「从别处跳进来看对话」与「用户自己切视图」是同一份状态，不会出现两套。
  const [tab, setTab] = useState<RunTabKey>(() => (readInitialTraceView() ? "timeline" : "overview"));
  const [traceView, setTraceView] = useState<TraceViewMode>(() => readInitialTraceView() ?? "workflow");
  const [timelineFocus, setTimelineFocus] = useState<TimelineFocus>({ stage: null, query: "", token: 0 });
  const [stageFocus, setStageFocus] = useState<StageFocus>({ stage: null, token: 0 });

  const openTimeline = (stage: string | null, query = "", view?: TraceViewMode) => {
    setTimelineFocus((previous) => ({ stage, query, token: previous.token + 1 }));
    if (view) setTraceView(view);
    setTab("timeline");
  };
  const openPerformance = (stage: string | null) => {
    setStageFocus((previous) => ({ stage, token: previous.token + 1 }));
    setTab("performance");
  };

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="运行详情"
        description="轨迹有两个视图：Workflow View 按 12 个阶段看执行过程，Conversation View 按真实执行顺序看模型收到了什么 / 返回了什么；原始 Trace 按 span 父子关系展开。点击任意一行查看完整明细。"
        badge={
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] break-all text-muted-foreground">
            {runId}
          </span>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                resource.reload();
                stages.reload();
                timeline.reload();
              }}
            >
              <RefreshCw />
              刷新
            </Button>
            <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/admin/runs" />}>
              <ArrowLeft />
              运行记录
            </Button>
          </div>
        }
      />

      <ResourceView resource={resource} loadingRows={6}>
        {(data) => (
          <RunDetailBody
            data={data}
            stages={stages}
            timeline={timeline.data}
            tab={tab}
            onTabChange={setTab}
            traceView={traceView}
            onTraceViewChange={setTraceView}
            timelineFocus={timelineFocus}
            stageFocus={stageFocus}
            onOpenTimeline={openTimeline}
            onOpenPerformance={openPerformance}
          />
        )}
      </ResourceView>
    </div>
  );
}

/**
 * 页签条。
 *
 * 为什么不用 components/ui/tabs：这一页不路由，且「轨迹」面板必须常驻（切走再切回来
 * 不该丢掉筛选条件、也不该重新拉一次事件）；基础组件会给每个面板做挂载 / 卸载。
 * 造型照抄 ui/tabs 的默认变体：muted 底、选中项浮起。
 */
function TabBar({
  value,
  onChange,
  counts,
}: {
  value: RunTabKey;
  onChange: (key: RunTabKey) => void;
  counts: Partial<Record<RunTabKey, number | null>>;
}) {
  return (
    <div
      role="tablist"
      aria-label="运行详情分区"
      className="flex w-full items-center gap-1 overflow-x-auto rounded-lg bg-muted p-[3px] sm:w-fit"
    >
      {RUN_TABS.map((item) => {
        const active = item.key === value;
        const count = counts[item.key];
        return (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(item.key)}
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-sm font-medium whitespace-nowrap transition-all focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
              active ? "bg-background text-foreground shadow-sm" : "text-foreground/60 hover:text-foreground",
            )}
          >
            {item.label}
            {typeof count === "number" ? (
              <Badge variant="secondary" className="tabular text-[0.625rem]">
                {formatNumber(count)}
              </Badge>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function RunDetailBody({
  data,
  stages,
  timeline,
  tab,
  onTabChange,
  traceView,
  onTraceViewChange,
  timelineFocus,
  stageFocus,
  onOpenTimeline,
  onOpenPerformance,
}: {
  data: AdminRunDetail;
  stages: ReturnType<typeof useAdminResource<AdminStageDetail[]>>;
  timeline: AdminTimeline | null;
  tab: RunTabKey;
  onTabChange: (key: RunTabKey) => void;
  traceView: TraceViewMode;
  onTraceViewChange: (view: TraceViewMode) => void;
  timelineFocus: TimelineFocus;
  stageFocus: StageFocus;
  onOpenTimeline: (stage: string | null, query?: string, view?: TraceViewMode) => void;
  onOpenPerformance: (stage: string | null) => void;
}) {
  const { run, metrics, progress } = data;
  const llmCalls = data.llm_calls.length > 0 ? data.llm_calls : deriveLlmCalls(data.trace);
  const stageRows =
    stages.data && stages.data.length > 0 ? stages.data : progress.stages.map(progressToStage);
  const tokensMissing =
    metrics.input_tokens === null &&
    metrics.output_tokens === null &&
    metrics.cached_tokens === null &&
    metrics.total_tokens === null;

  return (
    <div className="flex flex-col gap-4">
      <TabBar
        value={tab}
        onChange={onTabChange}
        counts={{
          timeline: timeline ? timeline.summary.events : null,
          provider: data.provider_calls.length,
          badcase: data.badcases.length,
        }}
      />

      {tab === "overview" ? (
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex flex-wrap items-center gap-2">
                <span>概览</span>
                <StatusBadge status={run.status} />
                <SourceTag source={run.source} />
              </CardTitle>
              <CardDescription className="break-words whitespace-pre-wrap">
                {run.original_query || "后端没有记录原始请求文本。"}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                <StatCard label="总耗时" value={formatDurationMs(metrics.duration_ms)} />
                <StatCard
                  label="总 Token"
                  value={metrics.total_tokens === null ? "未知" : formatTokenCount(metrics.total_tokens)}
                  icon={Coins}
                  hint={metrics.total_tokens === null ? "后端未返回 usage" : undefined}
                />
                <StatCard label="LLM 调用" value={formatNumber(metrics.llm_calls)} icon={Cpu} />
                <StatCard label="工具调用" value={formatNumber(metrics.tool_calls)} icon={Wrench} />
                <StatCard
                  label="Provider 失败"
                  value={formatNumber(metrics.provider_failures)}
                  tone={metrics.provider_failures && metrics.provider_failures > 0 ? "warning" : "muted"}
                />
                <StatCard
                  label="Bad Case"
                  value={formatNumber(metrics.badcase_count)}
                  tone={metrics.badcase_count && metrics.badcase_count > 0 ? "warning" : "muted"}
                  icon={Bug}
                />
                <StatCard
                  label="成本"
                  value={formatCostWithCurrency(metrics.cost, metrics.cost_currency)}
                  hint={
                    metrics.cost_source === "user_price"
                      ? "按「系统配置」里的每百万 token 单价计算"
                      : metrics.cost_source
                        ? "按后端定价计算"
                        : "未配置单价"
                  }
                />
              </div>
              <DescriptionList
                items={[
                  { label: "开始", value: formatDateTime(run.started_at ?? run.created_at) },
                  { label: "结束", value: formatDateTime(run.finished_at) },
                  { label: "来源", value: <SourceTag source={run.source} /> },
                  {
                    label: "来源会话",
                    value: run.source_session_id ? (
                      <Link
                        href={`/admin/sessions?q=${encodeURIComponent(run.source_session_id)}`}
                        className="font-mono text-primary underline-offset-4 hover:underline"
                      >
                        {run.source_session_id}
                      </Link>
                    ) : (
                      "无（这次不是引导式创建的）"
                    ),
                  },
                  { label: "进度状态", value: progress.status ? statusLabel(progress.status) : "—" },
                  { label: "进度说明", value: progress.message || "后端没有给出说明" },
                ]}
              />
            </CardContent>
          </Card>

          <RunFirstScreen data={data} timeline={timeline} onOpenTimeline={onOpenTimeline} />

          <CostPanel metrics={metrics} />

          <CollapsibleSection
            title="引导式来源"
            description="这次 run 对应的用户前置选择（没有就是不是引导式创建的）"
            icon={GitBranch}
          >
            <UserJourneySection data={data} />
          </CollapsibleSection>

          <CollapsibleSection
            title="决策链"
            description="每个实体的取舍结论、原因码与打分"
            count={data.decisions.length}
            icon={GitBranch}
          >
            <DecisionList decisions={data.decisions} />
          </CollapsibleSection>
        </div>
      ) : null}

      {/* 轨迹面板常驻：切换页签不重拉事件、不丢筛选与滚动位置。 */}
      <div className={cn("flex-col gap-4", tab === "timeline" ? "flex" : "hidden")}>
        <RunTimeline
          runId={run.run_id || timeline?.run_id || ""}
          initialStage={timelineFocus.stage}
          initialQuery={timelineFocus.query}
          focusToken={timelineFocus.token}
          view={traceView}
          onViewChange={onTraceViewChange}
          onSelectStage={onOpenPerformance}
          onOpenPerformance={(eventId) => {
            // 事件级跳转：性能页是按阶段组织的，因此先把事件映射到它所属的阶段再跳。
            const event = timeline?.events.find((item) => item.event_id === eventId);
            onOpenPerformance(event?.stage ?? null);
          }}
        />

        <CollapsibleSection
          title="原始 Trace（工程视图）"
          description="按 span 父子关系展开，看更底层的采集字段；轨迹的两个视图已经把同一份事实归并成人话"
          count={data.trace.length}
          icon={Cpu}
        >
          <TraceTree spans={data.trace} />
        </CollapsibleSection>
      </div>

      {tab === "performance" ? (
        <div className="flex flex-col gap-4">
          <RunPerformanceSummary
            metrics={metrics}
            stages={stageRows}
            trace={data.trace}
            llmCalls={llmCalls}
            journey={data.user_journey}
          />

          <StageDetailCard
            stages={stages}
            stageRows={stageRows}
            runMetrics={metrics}
            focus={stageFocus}
            onOpenTimeline={onOpenTimeline}
          />

          <CollapsibleSection
            title="LLM 调用"
            description="模型调用的标签、模型、状态、耗时、字符数与 token；要还原「模型收到 / 返回了什么」去轨迹的对话视图"
            count={llmCalls.length}
            icon={Cpu}
          >
            <LlmCallList calls={llmCalls} onOpenTimeline={onOpenTimeline} />
          </CollapsibleSection>

          <CollapsibleSection
            title="Token 明细"
            description="输入 / 输出 / 缓存的拆分（Token 与成本都按整次 run 统计）"
            count="4 项"
            icon={Coins}
          >
            <TokenBreakdown tokens={metrics} />
          </CollapsibleSection>

          {tokensMissing ? (
            <p className="text-[11px] text-muted-foreground">
              提示：本次运行没有 usage（Benchmark 用例运行使用假模型），因此 Token 与成本都显示为「未知」。
            </p>
          ) : null}
        </div>
      ) : null}

      {tab === "provider" ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2">
              <span>Provider 调用</span>
              <Badge variant="secondary" className="tabular text-[0.6875rem]">
                {formatNumber(data.provider_calls.length)}
              </Badge>
            </CardTitle>
            <CardDescription>外部数据源的逐次请求与返回情况；调用次数、状态与耗时都在这里。</CardDescription>
          </CardHeader>
          <CardContent>
            {data.provider_calls.length === 0 ? (
              <SectionEmpty
                title="没有 Provider 调用记录"
                description="这次运行可能全部命中缓存，或者后端没有落库 Provider 明细。"
                action={
                  <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/providers" />}>
                    打开 Provider 健康
                  </Button>
                }
              />
            ) : (
              <AdminTable
                columns={PROVIDER_COLUMNS}
                rows={data.provider_calls}
                getRowKey={(call) =>
                  `${call.provider}-${call.tool}-${call.fetched_at ?? ""}-${formatProviderQuery(call.query)}`
                }
              />
            )}
          </CardContent>
        </Card>
      ) : null}

      {tab === "badcase" ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center gap-2">
              <span>Bad Cases</span>
              <Badge variant="secondary" className="tabular text-[0.6875rem]">
                {formatNumber(data.badcases.length)}
              </Badge>
            </CardTitle>
            <CardDescription>本次运行被自动或人工登记的问题案例。</CardDescription>
          </CardHeader>
          <CardContent>
            <RunBadcaseList badcases={data.badcases} />
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

/* ------------------------------ 首屏四问（docs §11.1） ------------------------------ */

/**
 * 第一屏要能直接回答：用户要什么 / 系统给了什么 / 质量怎么样 / 哪里有问题。
 * 所有字段都取自已经返回的数据：轨迹事件（intent / 告警）、user_journey（画像 / 区域 / 候选池）、
 * 决策链（选中的酒店）与 run 指标；任何一项拿不到就写「—」并说明缺口，不用 0 冒充。
 */
function RunFirstScreen({
  data,
  timeline,
  onOpenTimeline,
}: {
  data: AdminRunDetail;
  timeline: AdminTimeline | null;
  onOpenTimeline: (stage: string | null, query?: string, view?: TraceViewMode) => void;
}) {
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <UserAskCard data={data} timeline={timeline} />
      <SystemGaveCard data={data} timeline={timeline} />
      <QualityCard data={data} timeline={timeline} onOpenTimeline={onOpenTimeline} />
      <IssuesCard data={data} timeline={timeline} onOpenTimeline={onOpenTimeline} />
    </div>
  );
}

function UserAskCard({ data, timeline }: { data: AdminRunDetail; timeline: AdminTimeline | null }) {
  const journey = data.user_journey;
  const intent = readIntentHighlights(timeline);
  const profile = readProfile(journey);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>用户要什么</span>
          <SourceTag source={data.run.source} />
        </CardTitle>
        <CardDescription>原始请求 + 结构化意图 + 动态偏好画像。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-foreground">原始请求</h3>
          <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs leading-5 break-words whitespace-pre-wrap text-foreground">
            {data.run.original_query || timeline?.run.original_query || "后端没有记录原始请求文本。"}
          </p>
        </section>

        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-foreground">结构化意图（TripIntent）</h3>
          {intent ? (
            <DescriptionList
              items={[
                { label: "目的地", value: intent.destination || "—" },
                { label: "出发地", value: intent.origin || "—" },
                { label: "出发日期", value: intent.startDate || "—" },
                {
                  label: "天数 / 人数",
                  value: `${intent.days === null ? "—" : `${intent.days} 天`} / ${
                    intent.travelers === null ? "—" : `${intent.travelers} 人`
                  }`,
                },
                {
                  label: "预算",
                  value: intent.budgetTotal === null ? "—" : `¥${formatNumber(intent.budgetTotal)}`,
                },
                {
                  label: "偏好关键词",
                  value: intent.preferences.length > 0 ? intent.preferences.join("、") : "—",
                },
                { label: "节奏 / 住宿策略", value: `${intent.pace || "—"} / ${intent.hotelPriority || "—"}` },
                { label: "MUST / WANT / REJECT", value: placeSelectionText(journey) },
              ]}
            />
          ) : (
            <p className="text-xs leading-5 text-muted-foreground">
              {timeline
                ? "这次 run 没有 user 事件（或没有落库 intent），看不到结构化意图；下面的引导式选择与原始请求仍然可用。"
                : "正在加载轨迹，稍后这里会补上结构化意图。"}
            </p>
          )}
        </section>

        <section className="flex flex-col gap-1.5">
          <h3 className="text-xs font-medium text-foreground">动态偏好画像</h3>
          {profile ? (
            <ProfileSummary profile={profile} />
          ) : (
            <p className="text-xs leading-5 text-muted-foreground">
              user_journey 里没有 profile：这次 run 可能还没生成画像（非引导式、或旧版本记录）。
            </p>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function ProfileSummary({ profile }: { profile: AdminRecord }) {
  const style = readString(profile, "travel_style");
  const source = readString(profile, "source");
  const reason = readString(profile, "reason");
  const pace = isRecord(profile.pace) ? profile.pace : null;
  const targetPerDay = pace ? readNumber(pace, "target_poi_per_day") : null;
  const personalized = source === "llm";
  const groups = PROFILE_GROUP_KEYS.filter((group) => isRecord(profile[group.key]));

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <ToneBadge tone={personalized ? "info" : "muted"}>
          {personalized ? "已个性化" : "均衡基线"}
        </ToneBadge>
        <span className="text-xs text-foreground">{style || "未命名风格"}</span>
        {targetPerDay !== null ? (
          <span className="text-[11px] text-muted-foreground">每天约 {formatNumber(targetPerDay)} 个点</span>
        ) : null}
      </div>
      {reason ? <p className="text-[11px] leading-5 break-words text-muted-foreground">{reason}</p> : null}
      {groups.length > 0 ? (
        <CollapsibleSection title="完整动态权重" description="画像对软取舍的加权（不影响硬规则）">
          <div className="flex flex-col gap-3">
            {groups.map((group) => (
              <section key={group.key} className="flex flex-col gap-1.5">
                <h4 className="text-[11px] font-medium text-foreground">{group.label}</h4>
                <WeightList weights={profile[group.key] as AdminRecord} />
              </section>
            ))}
          </div>
        </CollapsibleSection>
      ) : (
        <p className="text-[11px] text-muted-foreground">后端没有返回权重分组，只能看到风格与来源。</p>
      )}
    </div>
  );
}

/** 画像权重：键是后端内部英文名，这里补中文（未登记的键原样显示，新权重不会消失）。 */
function WeightList({ weights }: { weights: AdminRecord }) {
  const entries = Object.entries(weights);
  if (entries.length === 0) return <p className="text-[11px] text-muted-foreground">这一组没有权重。</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {entries.map(([key, value]) => (
        <li key={key} className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {PROFILE_WEIGHT_LABELS[key] ?? key}{" "}
          <span className="tabular text-foreground">{formatMetricValue(value)}</span>
        </li>
      ))}
    </ul>
  );
}

const PROFILE_WEIGHT_LABELS: Record<string, string> = {
  food_density: "美食密度",
  commercial_area: "商圈",
  nightlife: "夜生活",
  poi_centrality: "中心性",
  evidence: "攻略推荐",
  price: "价格",
  rating: "评分",
  location: "位置",
  user_interest: "用户兴趣",
  route_fit: "路线契合",
  target_poi_per_day: "每天点数",
  prefer_free_time: "偏好自由时间",
};

function SystemGaveCard({ data, timeline }: { data: AdminRunDetail; timeline: AdminTimeline | null }) {
  const journey = data.user_journey;
  const areas = readHotelAreas(journey);
  const hotel = readSelectedHotel(data.decisions);
  const pools = readPoiPools(journey);
  const intent = readIntentHighlights(timeline);
  const selectedArea = areas.selected || areas.areas[0]?.name || null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>系统给了什么</CardTitle>
        <CardDescription>住宿区域 / 酒店 / 候选池与预算。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <DescriptionList
          items={[
            {
              label: "住宿区域",
              value: selectedArea ? (
                <span>
                  {selectedArea}
                  {areas.selected ? "" : "（取契合度最高的区域）"}
                </span>
              ) : (
                "—（后端没有返回 hotel_areas，这次可能没走到区域选择）"
              ),
            },
            {
              label: "酒店",
              value: hotel ? (
                <span className="flex flex-col gap-0.5">
                  <span>{hotel.label}</span>
                  {hotel.reason ? (
                    <span className="text-[11px] break-words text-muted-foreground">{hotel.reason}</span>
                  ) : null}
                </span>
              ) : (
                "—（决策链里没有 search_hotels 的选定记录）"
              ),
            },
            {
              label: "核心景点 / 餐厅",
              value:
                pools.attraction === null && pools.food === null
                  ? "—（后端没有返回 poi_pools）"
                  : `${pools.attraction === null ? "—" : `${formatNumber(pools.attraction)} 个景点`} / ${
                      pools.food === null ? "—" : `${formatNumber(pools.food)} 个餐厅`
                    }${pools.experience === null ? "" : `（另有 ${formatNumber(pools.experience)} 个体验类）`}`,
            },
            {
              label: "预算",
              value: intent?.budgetTotal === null || intent === null ? "—" : `¥${formatNumber(intent.budgetTotal)}`,
            },
            {
              label: "意图来源",
              value: intent ? "轨迹 USER 事件的 TripIntent" : "轨迹未返回 intent，预算与意图暂缺",
            },
          ]}
        />

        {areas.areas.length > 0 ? (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-xs font-medium text-foreground">住宿区域候选（契合度前 {Math.min(3, areas.areas.length)}）</h3>
            <ul className="flex flex-col gap-1.5">
              {areas.areas.slice(0, 3).map((area) => (
                <li
                  key={area.name}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2"
                >
                  <span className="text-xs font-medium text-foreground">{area.name}</span>
                  {area.fitScore !== null ? (
                    <span className="tabular text-[11px] text-muted-foreground">
                      契合度 {formatRatio(area.fitScore)}
                    </span>
                  ) : null}
                  {area.tags.slice(0, 3).map((tag) => (
                    <ToneBadge key={tag} tone="muted" className="text-[0.625rem]">
                      {tag}
                    </ToneBadge>
                  ))}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

function QualityCard({
  data,
  timeline,
  onOpenTimeline,
}: {
  data: AdminRunDetail;
  timeline: AdminTimeline | null;
  onOpenTimeline: (stage: string | null, query?: string) => void;
}) {
  const quality = readTimelineQuality(timeline);
  const score = quality?.score ?? readNumber(data.run as unknown as AdminRecord, "quality_score");
  const grade = quality?.grade ?? readString(data.run as unknown as AdminRecord, "quality_grade");
  const error = quality?.error ?? readString(data.run as unknown as AdminRecord, "quality_error");
  const warnings = readWarningCount(data, timeline);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>质量怎么样</span>
          {grade ? (
            <ToneBadge tone={QUALITY_GRADE_TONES[grade] ?? "muted"} className="text-[0.6875rem]">
              {QUALITY_GRADE_LABELS[grade] ?? grade}
            </ToneBadge>
          ) : null}
        </CardTitle>
        <CardDescription>分值全部来自后端对已落库计划的评估，管理台不做二次打分。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <StatCard
            label="质量分"
            value={score === null ? "—" : formatRatio(score)}
            tone={score === null ? "muted" : score >= 0.8 ? "success" : score >= 0.6 ? "warning" : "danger"}
            hint={
              quality
                ? "来自轨迹接口的 quality"
                : score === null
                  ? error || "后端未返回质量分"
                  : "来自运行记录现算质量"
            }
          />
          <StatCard
            label="交付时告警"
            value={warnings === null ? "—" : `${formatNumber(warnings)} 条`}
            tone={warnings !== null && warnings > 0 ? "warning" : "muted"}
            hint={warnings === null ? "后端未返回" : "离开系统时仍未解决的告警"}
          />
        </div>

        {quality?.metrics ? (
          <section className="flex flex-col gap-1.5">
            <h3 className="text-xs font-medium text-foreground">单 run 软指标</h3>
            <MetricList metrics={quality.metrics} emptyText="后端没有返回质量指标明细。" />
          </section>
        ) : (
          <p className="text-[11px] leading-5 text-muted-foreground">
            没有质量指标明细{error ? `（${error}）` : "：后端这次没有随轨迹返回 quality，或这次 run 没有可评估的计划"}。
          </p>
        )}

        <div>
          <Button variant="outline" size="xs" onClick={() => onOpenTimeline(null, "硬校验")}>
            在轨迹里看校验事件
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function IssuesCard({
  data,
  timeline,
  onOpenTimeline,
}: {
  data: AdminRunDetail;
  timeline: AdminTimeline | null;
  onOpenTimeline: (stage: string | null, query?: string, view?: TraceViewMode) => void;
}) {
  const warnings = readWarningCount(data, timeline);
  const errors = timeline?.summary.errors ?? null;
  const fallbacks = timeline?.summary.fallbacks ?? null;
  const runError = timeline?.run.error ?? data.progress.message ?? null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <AlertTriangle
            className={cn(
              "size-4",
              data.badcases.length > 0 || (warnings ?? 0) > 0 ? "text-warning" : "text-muted-foreground",
            )}
            aria-hidden
          />
          <span>哪里有问题</span>
        </CardTitle>
        <CardDescription>告警、Bad Case、Provider 失败与错误 / fallback 的汇总。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="未解决告警"
            value={warnings === null ? "—" : formatNumber(warnings)}
            tone={warnings !== null && warnings > 0 ? "warning" : "muted"}
          />
          <StatCard
            label="Bad Case"
            value={formatNumber(data.badcases.length)}
            tone={data.badcases.length > 0 ? "warning" : "muted"}
          />
          <StatCard
            label="Provider 失败"
            value={formatNumber(data.metrics.provider_failures)}
            tone={data.metrics.provider_failures && data.metrics.provider_failures > 0 ? "warning" : "muted"}
          />
          <StatCard
            label="错误 / fallback"
            value={errors === null ? "—" : `${formatNumber(errors)} / ${formatNumber(fallbacks ?? 0)}`}
            tone={errors !== null && errors > 0 ? "danger" : "muted"}
            hint={errors === null ? "轨迹未加载" : "来自轨迹事件统计"}
          />
        </div>

        {data.badcases.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {data.badcases.slice(0, 4).map((badcase) => (
              <li key={badcase.badcase_id} className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-foreground">{badcaseCategoryLabel(badcase.category)}</span>
                <StatusBadge status={badcase.severity} />
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-muted-foreground">这次运行没有登记 Bad Case。</p>
        )}

        {runError ? (
          <p className="rounded-lg border border-danger/25 bg-danger-subtle px-3 py-2 text-[11px] leading-5 break-words text-danger-subtle-foreground">
            {runError}
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="xs" onClick={() => onOpenTimeline(null, "", "conversation")}>
            <MessagesSquare />
            去对话视图顺着看这次交互
          </Button>
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
            打开问题案例页
          </Button>
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/providers" />}>
            Provider 健康
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/* ------------------------------ 成本 ------------------------------ */

function CostPanel({ metrics }: { metrics: AdminRunDetail["metrics"] }) {
  const [open, setOpen] = useState(false);
  const breakdown = metrics.cost_breakdown ?? null;
  const hasPrices =
    breakdown !== null &&
    (breakdown.input_price_per_million !== null ||
      breakdown.output_price_per_million !== null ||
      breakdown.cached_price_per_million !== null);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span>成本</span>
          {metrics.cost_source === "user_price" ? (
            <ToneBadge tone="info">按用户单价</ToneBadge>
          ) : null}
        </CardTitle>
        <CardDescription>成本由后端计算；管理台只展示拆分，不做二次折算。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {metrics.cost_source === null ? (
          <div className="rounded-lg border border-warning/30 bg-warning-subtle px-3.5 py-3 text-xs leading-5 text-warning-subtle-foreground">
            未配置单价（去「系统配置」里填每百万 token 单价）
            <Button
              variant="outline"
              size="xs"
              className="ml-2 align-middle"
              nativeButton={false}
              render={<Link href="/admin/config" />}
            >
              打开系统配置
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <span className="tabular text-lg font-semibold text-foreground">
              {formatCostWithCurrency(metrics.cost, metrics.cost_currency)}
            </span>
            {metrics.cost_currency ? (
              <span className="text-[11px] text-muted-foreground">货币：{metrics.cost_currency}</span>
            ) : null}
            {hasPrices ? (
              <Button variant="outline" size="xs" onClick={() => setOpen(true)}>
                查看成本明细
              </Button>
            ) : null}
          </div>
        )}

        {!hasPrices && metrics.cost_source !== null ? (
          <p className="text-[11px] text-muted-foreground">
            后端没有给出 cost_breakdown，因此无法展示单价与 token 的乘积拆分。
          </p>
        ) : null}
      </CardContent>

      <AdminDetailDialog
        open={open}
        onOpenChange={setOpen}
        title="成本明细"
        description={`${formatCostWithCurrency(metrics.cost, metrics.cost_currency)} · 每百万 token 单价`}
      >
        {breakdown ? <CostBreakdownDetail breakdown={breakdown} currency={metrics.cost_currency} /> : null}
      </AdminDetailDialog>
    </Card>
  );
}

function CostBreakdownDetail({
  breakdown,
  currency,
}: {
  breakdown: AdminCostBreakdown;
  currency: string | null | undefined;
}) {
  const rows: { key: string; label: string; tokens: number | null; price: number | null }[] = [
    {
      key: "input",
      label: "输入 Token",
      tokens: breakdown.input_tokens,
      price: breakdown.input_price_per_million,
    },
    {
      key: "output",
      label: "输出 Token",
      tokens: breakdown.output_tokens,
      price: breakdown.output_price_per_million,
    },
    {
      key: "cached",
      label: "命中缓存 Token",
      tokens: breakdown.cached_tokens,
      price: breakdown.cached_price_per_million,
    },
  ];
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="border-b border-border text-muted-foreground">
          <th className="py-1.5 text-left font-medium">项目</th>
          <th className="py-1.5 text-right font-medium">Token</th>
          <th className="py-1.5 text-right font-medium">单价</th>
          <th className="py-1.5 text-right font-medium">小计</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key} className="border-b border-border/60 last:border-0">
            <td className="py-1.5 text-foreground">{row.label}</td>
            <td className="tabular py-1.5 text-right text-foreground">{formatTokenCount(row.tokens)}</td>
            <td className="tabular py-1.5 text-right text-muted-foreground">
              {formatUnitPrice(row.price, currency)}
            </td>
            <td className="tabular py-1.5 text-right text-foreground">
              {formatSubtotal(row.tokens, row.price, currency)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatSubtotal(
  tokens: number | null,
  price: number | null,
  currency: string | null | undefined,
): string {
  if (tokens === null || price === null) return "未知";
  return formatCostWithCurrency((tokens * price) / 1_000_000, currency);
}

/* ------------------------------ 阶段 ------------------------------ */

function stageAnchorId(stageId: string): string {
  return `run-stage-${stageId}`;
}

function StageDetailCard({
  stages,
  stageRows,
  runMetrics,
  focus,
  onOpenTimeline,
}: {
  stages: ReturnType<typeof useAdminResource<AdminStageDetail[]>>;
  stageRows: AdminStageDetail[];
  runMetrics: AdminRunDetail["metrics"];
  focus: StageFocus;
  onOpenTimeline: (stage: string | null, query?: string) => void;
}) {
  const appliedToken = useRef<number | null>(null);

  // 从轨迹页跳过来时定位到目标阶段并短暂高亮；同一个 token 只做一次。
  // 高亮直接操作 classList 而不是 state：这是「滚动完成后同步 DOM」，不需要额外一次渲染。
  useEffect(() => {
    if (appliedToken.current === focus.token) return;
    appliedToken.current = focus.token;
    if (!focus.stage) return;
    const node = document.getElementById(stageAnchorId(focus.stage));
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    node.classList.add("ring-2", "ring-primary/40");
    const timer = window.setTimeout(() => node.classList.remove("ring-2", "ring-primary/40"), 2400);
    return () => {
      window.clearTimeout(timer);
      node.classList.remove("ring-2", "ring-primary/40");
    };
  }, [focus]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>阶段</CardTitle>
        <CardDescription>
          workflow 的 {stageRows.length} 个阶段；点击任意阶段查看步骤、事实、工具 / 模型调用与 Token，
          也可以用「在轨迹中定位」跳回轨迹页看这个阶段发生了什么。
        </CardDescription>
      </CardHeader>
      <CardContent>
        {stages.error ? (
          <InlineError
            title="阶段明细没能加载"
            description="阶段端点可能尚未部署或暂时不可用；下面的阶段摘要来自运行详情，仍然可以查看。"
            detail={(stages.error as AdminApiError).detail}
            onRetry={stages.reload}
            className="mb-3"
          />
        ) : null}
        {stages.loading && !stages.data ? (
          <SectionSkeleton rows={3} />
        ) : stageRows.length === 0 ? (
          <SectionEmpty
            title="没有阶段进度"
            description="这次运行没有写入阶段记录，因此看不到分步进度。Trace 里通常仍有更细的 span。"
          />
        ) : (
          <ul className="flex flex-col gap-2.5">
            {stageRows.map((stage) => (
              <StageRow
                key={stage.stage_id}
                stage={stage}
                runMetrics={runMetrics}
                anchorId={stageAnchorId(stage.stage_id)}
                onOpenTimeline={onOpenTimeline}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function StageRow({
  stage,
  runMetrics,
  anchorId,
  onOpenTimeline,
}: {
  stage: AdminStageDetail;
  runMetrics: AdminRunDetail["metrics"];
  anchorId?: string;
  onOpenTimeline?: (stage: string | null, query?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const duration = stage.duration_ms ?? durationBetween(stage.started_at, stage.finished_at);
  const steps = stage.steps ?? [];
  const facts = stage.facts ?? null;
  const toolCalls = stage.tool_calls ?? [];
  const llmCalls = stage.llm_calls ?? [];

  return (
    <li id={anchorId} className="rounded-lg">
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex w-full min-w-0 flex-wrap items-center gap-2 rounded-lg border border-border p-2.5 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <span className="text-xs font-medium text-foreground">{stage.title || stage.stage_id}</span>
        <StatusBadge status={stage.status} />
        <span className="tabular ml-auto text-[11px] text-muted-foreground">{formatDurationMs(duration)}</span>
        <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        {stage.message ? (
          <span className="line-clamp-1 w-full text-xs leading-5 break-words text-muted-foreground">
            {stage.message}
          </span>
        ) : null}
      </button>

      <AdminDetailDialog
        open={open}
        onOpenChange={setOpen}
        title={stage.title || stage.stage_id}
        description={stage.stage_id}
        badge={<StatusBadge status={stage.status} />}
        footer={
          onOpenTimeline ? (
            <Button variant="outline" size="sm" onClick={() => onOpenTimeline(stage.stage_id)}>
              在轨迹中定位这个阶段
            </Button>
          ) : undefined
        }
      >
        <KeyValueList
          entries={[
            { key: "started", label: "开始", value: <span className="font-mono">{formatDateTime(stage.started_at)}</span> },
            { key: "finished", label: "结束", value: <span className="font-mono">{formatDateTime(stage.finished_at)}</span> },
            { key: "duration", label: "耗时", value: formatDurationMs(duration) },
          ]}
        />

        {stage.message ? (
          <section className="flex flex-col gap-1">
            <h3 className="text-xs font-medium text-foreground">说明</h3>
            <p className="text-xs leading-5 break-words whitespace-pre-wrap text-muted-foreground">
              {stage.message}
            </p>
          </section>
        ) : null}

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">步骤</h3>
          {steps.length === 0 ? (
            <p className="text-xs text-muted-foreground">这个阶段没有记录步骤。</p>
          ) : (
            <ol className="flex list-decimal flex-col gap-1 pl-5 text-xs leading-5 text-foreground">
              {steps.map((step, index) => (
                <li key={`${index}-${step}`} className="break-words">
                  {step}
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">事实</h3>
          <MetricList metrics={facts} emptyText="这个阶段没有记录事实。" />
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">工具调用（{toolCalls.length}）</h3>
          {toolCalls.length === 0 ? (
            <p className="text-xs text-muted-foreground">这个阶段没有工具调用。</p>
          ) : (
            <AdminTable
              columns={STAGE_TOOL_COLUMNS}
              rows={toolCalls}
              getRowKey={(call) => `${call.provider ?? ""}-${call.tool ?? ""}-${call.status ?? ""}-${call.duration_ms ?? ""}`}
            />
          )}
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">模型调用（{llmCalls.length}）</h3>
          {llmCalls.length === 0 ? (
            <p className="text-xs text-muted-foreground">这个阶段没有模型调用。</p>
          ) : (
            <AdminTable
              columns={STAGE_LLM_COLUMNS}
              rows={llmCalls}
              getRowKey={(call) => `${call.tag ?? ""}-${call.model ?? ""}-${call.status ?? ""}-${call.duration_ms ?? ""}`}
            />
          )}
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">该阶段 Token 与成本</h3>
          <TokenBreakdown tokens={stage.tokens} />
          <StageCost tokens={stage.tokens} breakdown={runMetrics.cost_breakdown ?? null} currency={runMetrics.cost_currency} />
        </section>
      </AdminDetailDialog>
    </li>
  );
}

function StageCost({
  tokens,
  breakdown,
  currency,
}: {
  tokens: AdminStageTokens | null | undefined;
  breakdown: AdminCostBreakdown | null;
  currency: string | null | undefined;
}) {
  if (!tokens || !breakdown) {
    return (
      <p className="text-[11px] text-muted-foreground">
        未配置单价（去
        <Link href="/admin/config" className="mx-1 text-primary underline-offset-4 hover:underline">
          系统配置
        </Link>
        里填每百万 token 单价），或后端未返回该阶段 usage。
      </p>
    );
  }
  const parts = [
    { tokens: tokens.input_tokens, price: breakdown.input_price_per_million },
    { tokens: tokens.output_tokens, price: breakdown.output_price_per_million },
    { tokens: tokens.cached_tokens, price: breakdown.cached_price_per_million },
  ];
  let total = 0;
  let known = false;
  for (const part of parts) {
    if (part.tokens !== null && part.tokens !== undefined && part.price !== null && part.price !== undefined) {
      total += (part.tokens * part.price) / 1_000_000;
      known = true;
    }
  }
  if (!known) {
    return <p className="text-[11px] text-muted-foreground">未配置单价，无法估算该阶段成本。</p>;
  }
  return (
    <p className="text-xs text-foreground">
      估算成本：
      <span className="tabular font-medium">{formatCostWithCurrency(total, currency)}</span>
      <span className="ml-2 text-[11px] text-muted-foreground">按 run 的每百万 token 单价估算</span>
    </p>
  );
}

const STAGE_TOOL_COLUMNS: AdminColumn<NonNullable<AdminStageDetail["tool_calls"]>[number]>[] = [
  {
    key: "provider",
    header: "Provider / 工具",
    primary: true,
    cell: (call) => (
      <div className="min-w-0">
        <p className="truncate text-xs font-medium text-foreground" title={call.provider ?? undefined}>
          {call.provider || "未知来源"}
        </p>
        <p className="truncate font-mono text-[10px] text-muted-foreground">{call.tool || "未知工具"}</p>
      </div>
    ),
  },
  { key: "status", header: "状态", cell: (call) => <StatusBadge status={call.status ?? null} /> },
  {
    key: "returned",
    header: "返回",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatMetricValue(call.returned)}</span>,
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>,
  },
];

const STAGE_LLM_COLUMNS: AdminColumn<AdminStageLlmCall>[] = [
  {
    key: "tag",
    header: "标签",
    primary: true,
    cell: (call) => (
      <span className="block max-w-[14rem] truncate font-mono text-xs text-foreground" title={call.tag ?? undefined}>
        {call.tag || "未命名"}
      </span>
    ),
  },
  {
    key: "model",
    header: "模型",
    cell: (call) => (
      <span className="block max-w-[12rem] truncate font-mono text-[11px] text-muted-foreground" title={call.model ?? undefined}>
        {missingOr(call.model)}
      </span>
    ),
  },
  { key: "status", header: "状态", cell: (call) => <StatusBadge status={call.status ?? null} /> },
  {
    key: "tokens",
    header: "Token",
    align: "right",
    mobileHidden: true,
    cell: (call) => (
      <span className="tabular text-[11px] text-muted-foreground">{callTokenParts(call).join(" / ") || "—"}</span>
    ),
  },
  {
    key: "chars",
    header: "字符数",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatNumber(call.chars)}</span>,
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>,
  },
];

/**
 * 逐次调用的 token 用量。
 *
 * 为什么按 unknown 读而不是直接用字段类型：这几个字段是后端补 `usage` 之后才有的，
 * 而 `llm_calls` 在 admin-api 里是原样透传（raw cast），不是逐字段归一 ——
 * 后端哪天把数字写成字符串，这里必须降级成「后端未返回 usage」而不是渲染出 NaN。
 */
function callTokenParts(call: AdminLlmCall | AdminStageLlmCall): string[] {
  const record = call as unknown as AdminRecord;
  const read = (key: string): number | null => {
    const value = record[key];
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const parts: string[] = [];
  const input = read("input_tokens");
  const output = read("output_tokens");
  const cached = read("cached_tokens");
  if (input !== null) parts.push(`输入 ${formatNumber(input)}`);
  if (output !== null) parts.push(`输出 ${formatNumber(output)}`);
  if (cached !== null) parts.push(`缓存 ${formatNumber(cached)}`);
  return parts;
}

function progressToStage(stage: AdminStageProgress): AdminStageDetail {
  return {
    stage_id: stage.stage_id,
    title: stage.title,
    status: stage.status,
    message: stage.message,
    started_at: stage.started_at,
    finished_at: stage.finished_at,
    duration_ms: durationBetween(stage.started_at, stage.finished_at),
    steps: [],
    facts: stage.facts,
    spans: [],
    llm_calls: [],
    tool_calls: [],
    tokens: null,
  };
}

/* ------------------------------ Token ------------------------------ */

function TokenBreakdown({ tokens }: { tokens: AdminStageTokens | AdminRunDetail["metrics"] | null | undefined }) {
  const rows = [
    { key: "input", label: "输入 Token", value: tokens?.input_tokens },
    { key: "output", label: "输出 Token", value: tokens?.output_tokens },
    { key: "cached", label: "命中缓存 Token", value: tokens?.cached_tokens },
    { key: "total", label: "总 Token", value: tokens?.total_tokens },
  ];
  const allMissing = rows.every((row) => row.value === null || row.value === undefined);
  if (allMissing) {
    return (
      <SectionEmpty
        title="未知（后端未返回 usage）"
        description="这次运行没有记录 Token 用量。Benchmark 用例运行使用假模型，通常就没有 usage。"
      />
    );
  }
  return (
    <dl className="grid gap-x-5 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
      {rows.map((row) => (
        <div key={row.key} className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5">
          <dt className="text-xs text-muted-foreground">{row.label}</dt>
          <dd className="tabular text-right text-xs font-medium text-foreground">
            {formatTokenCount(row.value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/* ------------------------------ LLM / Token ------------------------------ */

function LlmCallList({
  calls,
  onOpenTimeline,
}: {
  calls: AdminLlmCall[];
  onOpenTimeline?: (stage: string | null, query?: string, view?: TraceViewMode) => void;
}) {
  const [selected, setSelected] = useState<AdminLlmCall | null>(null);
  if (calls.length === 0) {
    return (
      <SectionEmpty
        title="这次运行没有 LLM 调用"
        description="可能本次规划没有触发模型调用（例如全部命中缓存），或者 Trace 里没有 component=llm 的 span。"
      />
    );
  }
  return (
    <>
      <ul className="flex flex-col gap-2">
        {calls.map((call, index) => {
          const tokens = callTokenParts(call);
          return (
            <li key={`${call.tag}-${index}`}>
              <button
                type="button"
                onClick={() => setSelected(call)}
                className="flex w-full min-w-0 items-center gap-2 rounded-lg border border-border px-2.5 py-2 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={call.tag}>
                  {call.tag || "未命名"}
                </span>
                {tokens.length > 0 ? (
                  <span className="tabular shrink-0 text-[11px] text-muted-foreground">
                    {tokens.join(" / ")}
                  </span>
                ) : null}
                <StatusBadge status={call.status ?? null} />
                <span className="tabular shrink-0 text-[11px] text-muted-foreground">
                  {formatDurationMs(call.duration_ms)}
                </span>
                <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
              </button>
            </li>
          );
        })}
      </ul>

      <AdminDetailDialog
        open={selected !== null}
        onOpenChange={(open) => (open ? null : setSelected(null))}
        title={selected?.tag || "LLM 调用"}
        badge={<StatusBadge status={selected?.status ?? null} />}
        footer={
          selected && onOpenTimeline ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOpenTimeline(null, selected.tag, "conversation")}
            >
              <MessagesSquare />
              在轨迹的对话视图里找这次调用
            </Button>
          ) : undefined
        }
      >
        {selected ? (
          <KeyValueList
            entries={[
              { key: "tag", label: "标签", value: <span className="font-mono">{missingOr(selected.tag)}</span> },
              { key: "model", label: "模型", value: <span className="font-mono">{missingOr(selected.model)}</span> },
              { key: "status", label: "状态", value: <StatusBadge status={selected.status ?? null} /> },
              { key: "duration", label: "耗时", value: formatDurationMs(selected.duration_ms) },
              { key: "chars", label: "字符数", value: formatNumber(selected.chars) },
              {
                key: "tokens",
                label: "Token",
                value:
                  callTokenParts(selected).join(" / ") ||
                  <span className="text-muted-foreground">—（后端未返回 usage）</span>,
              },
              {
                key: "started",
                label: "开始时间",
                value: <span className="font-mono">{formatDateTime(selected.started_at)}</span>,
              },
              { key: "error", label: "错误", value: <ErrorText value={selected.error} /> },
            ]}
          />
        ) : null}
      </AdminDetailDialog>
    </>
  );
}

/* ------------------------------ Provider / 决策 / BadCase ------------------------------ */

const PROVIDER_COLUMNS: AdminColumn<AdminProviderCall>[] = [
  {
    key: "provider",
    header: "Provider",
    primary: true,
    cell: (call) => (
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{call.provider}</p>
        <p className="font-mono text-[10px] text-muted-foreground">{call.tool}</p>
      </div>
    ),
  },
  {
    key: "query",
    header: "查询",
    mobileHidden: true,
    cell: (call) => (
      <span className="line-clamp-2 block max-w-[22rem] text-xs text-muted-foreground">
        {formatProviderQuery(call.query)}
      </span>
    ),
  },
  { key: "status", header: "状态", cell: (call) => <StatusBadge status={call.status} /> },
  {
    key: "returned",
    header: "返回",
    align: "right",
    cell: (call) => (
      <span className="tabular text-xs break-words">{formatMetricValue(call.returned)}</span>
    ),
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>,
  },
  {
    key: "fetched_at",
    header: "抓取时间",
    mobileHidden: true,
    cell: (call) => (
      <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
        {formatDateTime(call.fetched_at)}
      </span>
    ),
  },
  {
    key: "source",
    header: "来源",
    mobileHidden: true,
    cell: (call) => (
      <span className="font-mono text-[11px] break-all text-muted-foreground">{call.source_id ?? "—"}</span>
    ),
  },
  {
    key: "note",
    header: "备注",
    mobileHidden: true,
    cell: (call) => (
      <span className="text-[11px] break-words text-muted-foreground">{call.note ?? "—"}</span>
    ),
  },
];

function DecisionList({ decisions }: { decisions: AdminDecision[] }) {
  if (decisions.length === 0) {
    return (
      <SectionEmpty
        title="没有决策记录"
        description="这次运行没有把取舍结论落库，因此看不到决策链。"
      />
    );
  }
  return (
    <ul className="flex flex-col gap-2.5">
      {decisions.map((decision) => (
        <li key={decision.entity_id} className="rounded-lg border border-border p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[11px] break-all text-muted-foreground">
              {decision.entity_id}
            </span>
            <StatusBadge status={decision.status} />
            <span className="text-xs text-muted-foreground">{decision.agent_or_stage}</span>
          </div>
          {decision.reason_text ? (
            <p className="mt-1.5 text-xs leading-5 break-words text-foreground">{decision.reason_text}</p>
          ) : null}
          {decision.reason_codes.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {decision.reason_codes.map((code) => (
                <ToneBadge key={code} tone="muted">
                  {code}
                </ToneBadge>
              ))}
            </div>
          ) : null}
          {decision.scores && Object.keys(decision.scores).length > 0 ? (
            <MetricList metrics={decision.scores} className="mt-2" />
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function RunBadcaseList({ badcases }: { badcases: AdminBadcase[] }) {
  const [selected, setSelected] = useState<AdminBadcase | null>(null);

  if (badcases.length === 0) {
    return (
      <SectionEmpty
        title="这次运行没有登记 Bad Case"
        description="规则判定、人工标注与模型评审都没有为本次运行登记问题。"
        action={
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
            打开问题案例页
          </Button>
        }
      />
    );
  }
  return (
    <>
      <ul className="flex flex-col gap-2">
        {badcases.map((badcase) => (
          <li key={badcase.badcase_id}>
            <button
              type="button"
              onClick={() => setSelected(badcase)}
              className="flex w-full min-w-0 flex-wrap items-center gap-2 rounded-lg border border-border px-2.5 py-2 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="shrink-0 text-xs font-medium text-foreground">
                {badcaseCategoryLabel(badcase.category)}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
              </span>
              <StatusBadge status={badcase.severity} />
              <StatusBadge status={badcase.analysis_status} />
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </button>
          </li>
        ))}
      </ul>

      <AdminDetailDialog
        open={selected !== null}
        onOpenChange={(open) => (open ? null : setSelected(null))}
        title={selected ? badcaseCategoryLabel(selected.category) : "问题案例"}
        description={selected?.badcase_id}
        badge={selected ? <StatusBadge status={selected.severity} /> : undefined}
        footer={
          <Button variant="outline" size="sm" nativeButton={false} render={<Link href="/admin/badcases" />}>
            去问题案例页处理
          </Button>
        }
      >
        {selected ? (
          <DescriptionList
            items={[
              { label: "症状", value: cleanBadcaseSymptom(selected.symptom, selected.category) },
              { label: "期望", value: selected.expected || "未返回" },
              { label: "实际", value: selected.actual || "未返回" },
              { label: "疑似根因", value: selected.suspected_root_cause || "未返回" },
              { label: "根因状态", value: <StatusBadge status={selected.root_cause_status} /> },
              { label: "发现方式", value: <StatusBadge status={selected.detected_by} /> },
              { label: "分析状态", value: <StatusBadge status={selected.analysis_status} /> },
              { label: "修复状态", value: <StatusBadge status={selected.fixed_status} /> },
              { label: "引入版本", value: selected.introduced_in || "未返回" },
              { label: "修复版本", value: selected.fixed_in || "未返回" },
              { label: "登记时间", value: formatDateTime(selected.created_at) },
            ]}
          />
        ) : null}
      </AdminDetailDialog>
    </>
  );
}

/* ------------------------------ 引导式来源 ------------------------------ */

/**
 * 引导式来源 + Discovery 交接。
 *
 * 「交接」回答一个具体问题：正式 run 里，交通 / 酒店 / 攻略 / 地点这四条线
 * 到底是复用了预取（reused）、本次补查了（fallback_query），还是补查后仍然没结果（unavailable）。
 * 数据优先取 `user_journey.discovery`；后端尚未把该字段接进 API 时，退回 Trace 里的
 * `discovery_handoff` span（workflow 落库，属性名就是这四条线）。
 */
function UserJourneySection({ data }: { data: AdminRunDetail }) {
  const journey = data.user_journey;
  const [stagesOpen, setStagesOpen] = useState(false);

  if (!journey) {
    return (
      <SectionEmpty
        title="这次不是引导式创建的"
        description="后端没有返回 user_journey，说明这次运行来自一句话、命令行或 Benchmark，而不是引导式旅程。"
      />
    );
  }

  const handoffRows = readHandoffRows(journey, data.trace);
  const graceWaitedMs = readGraceWaitedMs(journey, data.trace);
  const prefetchStages = journey.prefetch_stages ?? null;
  const prefetchStageEntries = prefetchStages ? Object.entries(prefetchStages) : [];

  return (
    <div className="flex flex-col gap-4">
      <DescriptionList
        items={[
          { label: "来源", value: <SourceTag source={journey.source} /> },
          {
            label: "来源会话",
            value: journey.source_session_id ? (
              <Link
                href={`/admin/sessions?q=${encodeURIComponent(journey.source_session_id)}`}
                className="font-mono text-primary underline-offset-4 hover:underline"
              >
                {journey.source_session_id}
              </Link>
            ) : (
              "未返回"
            ),
          },
          { label: "交通方式", value: missingOr(journey.transport_mode) },
          { label: "交通偏好", value: missingOr(journey.transport_priority) },
          { label: "住宿偏好", value: missingOr(journey.hotel_priority) },
          { label: "节奏", value: missingOr(journey.pace) },
          { label: "发现状态", value: missingOr(journey.discovery_status ?? journey.prefetch_status) },
          { label: "复用预取", value: prefetchReusedText(journey.prefetch_reused) },
          { label: "必去", value: formatPlaceCount(journey.place_selections?.must) },
          { label: "想去", value: formatPlaceCount(journey.place_selections?.want) },
          { label: "排除", value: formatPlaceCount(journey.place_selections?.reject) },
        ]}
      />

      <section className="flex flex-col gap-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-xs font-medium text-foreground">Discovery 交接</h3>
          <span className="text-[11px] text-muted-foreground">
            Discovery 等待：{formatGraceWaited(graceWaitedMs)}
          </span>
        </div>
        {handoffRows === null ? (
          <p className="text-xs leading-5 text-muted-foreground">
            后端没有返回每条线的交接结论（user_journey.discovery 与 Trace 的 discovery_handoff 都缺失），
            无法判断哪些部分复用了预取。
          </p>
        ) : (
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {handoffRows.map((row) => {
              const presentation = handoffPresentation(row.handoff);
              return (
                <li
                  key={row.key}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border px-3 py-2"
                >
                  <span className="text-xs font-medium text-foreground">{row.label}</span>
                  <span title={presentation.description}>
                    <ToneBadge tone={presentation.tone} className="text-[0.6875rem]">
                      <span title={row.handoff ?? "未返回"}>{presentation.label}</span>
                    </ToneBadge>
                  </span>
                </li>
              );
            })}
          </ul>
        )}

        {prefetchStages ? (
          <div>
            <Button variant="outline" size="xs" onClick={() => setStagesOpen(true)}>
              查看预取原始数据
              <ChevronRight />
            </Button>
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground">
            预取原始数据：后端本次没有返回 prefetch_stages，无法展开各线原始状态。
          </p>
        )}
      </section>

      <AdminDetailDialog
        open={stagesOpen}
        onOpenChange={setStagesOpen}
        title="预取原始数据"
        description="Discovery 各线原始状态（status / 耗时 / 结果数 / 降级）"
      >
        {prefetchStageEntries.length === 0 ? (
          <p className="text-xs text-muted-foreground">后端没有返回 prefetch_stages。</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {prefetchStageEntries.map(([key, stage]) => (
              <li key={key} className="flex flex-col gap-1.5 rounded-lg border border-border px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-medium text-foreground">{stageLabel(key)}</span>
                  <StatusBadge status={stage?.status ?? null} />
                  {stage?.degraded ? <ToneBadge tone="warning">已降级</ToneBadge> : null}
                </div>
                <dl className="grid grid-cols-1 gap-x-5 gap-y-1 sm:grid-cols-3">
                  <StageFact label="耗时" value={formatDurationMs(stage?.duration_ms)} />
                  <StageFact
                    label="结果数"
                    value={stage?.result_count === null || stage?.result_count === undefined ? "—" : formatNumber(stage.result_count)}
                  />
                  <StageFact label="错误" value={stage?.error ? stage.error : "无"} />
                </dl>
              </li>
            ))}
          </ul>
        )}
        <section className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-foreground">原始 JSON</h3>
          <JsonBlock value={prefetchStages ?? {}} />
        </section>
      </AdminDetailDialog>
    </div>
  );
}

function StageFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1 sm:border-0 sm:pb-0">
      <dt className="shrink-0 text-[11px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right text-[11px] break-words text-foreground">{value}</dd>
    </div>
  );
}

const STAGE_LABELS: Record<string, string> = {
  transport: "交通",
  hotels: "酒店",
  social: "攻略",
  places: "地点",
};

function stageLabel(key: string): string {
  return STAGE_LABELS[key] ?? key;
}

/**
 * 复用预取的文案：旧口径是布尔，新口径是「每条线的布尔」。
 * 两种都要能读，否则后端一改口径这里就会显示成「是」（对象恒真）。
 */
function prefetchReusedText(value: boolean | Record<string, boolean> | null | undefined): string {
  if (value === null || value === undefined) return "未返回";
  if (typeof value === "boolean") return value ? "是" : "否";
  const entries = Object.entries(value);
  if (entries.length === 0) return "未返回";
  const reused = entries.filter(([, reusedFlag]) => reusedFlag).length;
  return `${reused} / ${entries.length} 条线复用（${entries
    .map(([key, flag]) => `${stageLabel(key)}${flag ? "✓" : "✗"}`)
    .join("、")}）`;
}

/** 交接等待：0 表示 Discovery 已经完成，无需等待。 */
function formatGraceWaited(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未返回";
  if (value <= 0) return "无需等待（Discovery 已完成）";
  return `等待 ${formatDurationMs(value)}`;
}

/** 用户选择：旧接口给名称数组，新接口给计数。 */
function formatPlaceCount(value: string[] | number | null | undefined): string {
  if (value === null || value === undefined) return "无";
  if (typeof value === "number") return value > 0 ? `${formatNumber(value)} 个` : "无";
  return value.length > 0 ? value.join("、") : "无";
}

/* ------------------------------ 首屏派生数据 ------------------------------ */

/** 后端返回的 user_journey 里有几个字段还没进前端类型（画像 / 区域 / 候选池），按 unknown 读。 */
function journeyRecord(journey: AdminUserJourney | null): AdminRecord {
  return journey ? (journey as unknown as AdminRecord) : {};
}

interface IntentHighlights {
  destination: string | null;
  origin: string | null;
  startDate: string | null;
  days: number | null;
  travelers: number | null;
  budgetTotal: number | null;
  preferences: string[];
  pace: string | null;
  hotelPriority: string | null;
}

/** TripIntent 只在轨迹的 USER 事件里（metadata.intent），运行详情本身没有计划。 */
function readIntentHighlights(timeline: AdminTimeline | null): IntentHighlights | null {
  const event = timeline?.events.find((item) => item.event_type === "USER");
  const intent = event && isRecord(event.metadata.intent) ? event.metadata.intent : null;
  if (!intent) return null;
  return {
    destination: readText(intent, "destination"),
    origin: readText(intent, "origin"),
    startDate: readText(intent, "start_date"),
    days: readNumber(intent, "days"),
    travelers: readNumber(intent, "travelers"),
    budgetTotal: readNumber(intent, "budget_total"),
    preferences: readStringArray(intent, "preferences"),
    pace: readText(intent, "pace"),
    hotelPriority: readText(intent, "hotel_priority"),
  };
}

function readProfile(journey: AdminUserJourney | null): AdminRecord | null {
  const profile = journeyRecord(journey).profile;
  return isRecord(profile) ? profile : null;
}

interface AreaBrief {
  name: string;
  tags: string[];
  fitScore: number | null;
}

function readHotelAreas(journey: AdminUserJourney | null): { selected: string | null; areas: AreaBrief[] } {
  const record = journeyRecord(journey);
  const raw = Array.isArray(record.hotel_areas) ? record.hotel_areas : [];
  const areas: AreaBrief[] = raw.filter(isRecord).map((area) => ({
    name: readString(area, "name") ?? readString(area, "key") ?? "未命名区域",
    tags: readStringArray(area, "tags"),
    fitScore: readNumber(area, "fit_score"),
  }));
  return { selected: readString(record, "hotel_area_selected"), areas };
}

/**
 * 选中的酒店：计划本身不在运行详情里，只能读决策链里 `search_hotels` 的选定记录。
 * 两种字段口径都要认（`agent_or_stage`/`status` 与原始落库的 `stage`/`decision`）。
 */
function readSelectedHotel(decisions: AdminDecision[]): { label: string; reason: string | null } | null {
  for (const decision of decisions) {
    const record = decision as unknown as AdminRecord;
    const stage = readString(record, "agent_or_stage") ?? readString(record, "stage");
    if (stage !== "search_hotels") continue;
    const status = readString(record, "status") ?? readString(record, "decision");
    if (status && !["SELECT", "ACCEPT"].includes(status.toUpperCase())) continue;
    const label = readString(record, "entity_id");
    if (!label) continue;
    return { label, reason: readString(record, "reason_text") ?? readString(record, "reason") };
  }
  return null;
}

function readPoiPools(journey: AdminUserJourney | null): {
  attraction: number | null;
  food: number | null;
  experience: number | null;
} {
  const pools = journeyRecord(journey).poi_pools;
  if (!isRecord(pools)) return { attraction: null, food: null, experience: null };
  return {
    attraction: readArrayLength(pools, "attraction"),
    food: readArrayLength(pools, "food"),
    experience: readArrayLength(pools, "experience"),
  };
}

interface TimelineQuality {
  score: number | null;
  grade: string | null;
  metrics: AdminRecord | null;
  error: string | null;
}

/**
 * 质量画像。
 *
 * 后端 `/timeline` 已经返回 `quality`，但前端类型与归一化还没透出这个字段，
 * 因此按 unknown 读：契约补上后这里会自动生效；在那之前退回运行记录的 quality_score。
 */
function readTimelineQuality(timeline: AdminTimeline | null): TimelineQuality | null {
  if (!timeline) return null;
  const quality = (timeline as unknown as AdminRecord).quality;
  if (!isRecord(quality)) return null;
  return {
    score: readNumber(quality, "score"),
    grade: readString(quality, "grade"),
    metrics: isRecord(quality.metrics) ? quality.metrics : null,
    error: readString(quality, "error"),
  };
}

/** 未解决告警：优先运行记录字段，否则取轨迹里那条「交付时的行程告警」事件。 */
function readWarningCount(data: AdminRunDetail, timeline: AdminTimeline | null): number | null {
  if (typeof data.run.warnings === "number") return data.run.warnings;
  const event = timeline?.events.find((item) => item.title === "交付时的行程告警");
  if (!event) return null;
  const unresolved = readNumber(event.metadata, "unresolved");
  if (unresolved !== null) return unresolved;
  const codes = event.metadata.codes;
  if (isRecord(codes)) {
    let total = 0;
    for (const value of Object.values(codes)) {
      if (typeof value === "number" && Number.isFinite(value)) total += value;
    }
    return total;
  }
  return null;
}

function placeSelectionText(journey: AdminUserJourney | null): string {
  const selections = journey?.place_selections;
  if (!selections) return "—";
  return `${formatPlaceCount(selections.must)} / ${formatPlaceCount(selections.want)} / ${formatPlaceCount(selections.reject)}`;
}

const QUALITY_GRADE_TONES: Record<string, AdminTone> = {
  GOOD: "success",
  FAIR: "warning",
  POOR: "danger",
  UNKNOWN: "muted",
};

const QUALITY_GRADE_LABELS: Record<string, string> = {
  GOOD: "好",
  FAIR: "一般",
  POOR: "差",
  UNKNOWN: "未评级",
};

const PROFILE_GROUP_KEYS: { key: string; label: string }[] = [
  { key: "hotel_area", label: "住宿区域权重" },
  { key: "hotel", label: "酒店权重" },
  { key: "attraction", label: "景点权重" },
  { key: "food", label: "餐饮权重" },
];

/* ------------------------------ 工具函数 ------------------------------ */

function isRecord(value: unknown): value is AdminRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(record: AdminRecord, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function readNumber(record: AdminRecord, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readStringArray(record: AdminRecord, key: string): string[] {
  const value = record[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** TripIntent.destination 是数组（一次可以带多个目的地），其余大多是字符串。 */
function readText(record: AdminRecord, key: string): string | null {
  const value = record[key];
  if (typeof value === "string" && value.trim().length > 0) return value;
  if (Array.isArray(value)) {
    const parts = value.filter((item): item is string => typeof item === "string");
    return parts.length > 0 ? parts.join("、") : null;
  }
  return null;
}

function readArrayLength(record: AdminRecord, key: string): number | null {
  const value = record[key];
  return Array.isArray(value) ? value.length : null;
}

function ErrorText({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-muted-foreground">未返回</span>;
  return <span className="font-mono text-[11px] break-words text-danger-subtle-foreground">{value}</span>;
}

/** null / undefined / 空串统一成「未返回」，避免弹窗里出现空白行。 */
function missingOr(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) return "未返回";
  return value;
}

function durationBetween(startedAt: string | null | undefined, finishedAt: string | null | undefined): number | null {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

/** 契约新增了 llm_calls；旧后端没有时从 trace 兜底派生，避免整块消失。 */
function deriveLlmCalls(spans: AdminTraceSpan[]): AdminLlmCall[] {
  const hints = ["llm", "model", "agent", "planner", "critic", "router", "judge", "reason"];
  const attributeHints = ["model", "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"];
  const calls: AdminLlmCall[] = [];
  for (const span of spans) {
    const component = span.component.toLowerCase();
    const name = span.name.toLowerCase();
    const attributes = span.attributes ?? {};
    const hit =
      hints.some((hint) => component.includes(hint) || name.includes(hint)) ||
      attributeHints.some((key) => key in attributes);
    if (!hit) continue;
    const totalTokens = attributes.total_tokens;
    calls.push({
      tag: span.name,
      model: typeof attributes.model === "string" ? attributes.model : null,
      status: span.status,
      duration_ms: span.duration_ms,
      chars: typeof totalTokens === "number" ? totalTokens : null,
      error: span.error,
      started_at: span.started_at,
    });
  }
  return calls;
}
