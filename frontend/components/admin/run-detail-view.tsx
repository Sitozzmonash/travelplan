"use client";

import Link from "next/link";
import { useState } from "react";
import {
  ArrowLeft,
  Bug,
  ChevronRight,
  Coins,
  Cpu,
  GitBranch,
  RefreshCw,
  Share2,
  Sparkles,
  Wrench,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PageHeader } from "@/components/admin/page-header";
import { InlineError, ResourceView, SectionEmpty, SectionSkeleton } from "@/components/admin/admin-states";
import { AdminDetailDialog, KeyValueList } from "@/components/admin/detail-dialog";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import { StatCard } from "@/components/admin/stat-card";
import { SourceTag, StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { TraceTree } from "@/components/admin/trace-tree";
import {
  badcaseCategoryLabel,
  cleanBadcaseSymptom,
  formatCostWithCurrency,
  formatDurationMs,
  formatLatency,
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
  type AdminApiError,
} from "@/lib/admin-api";
import { formatDateTime, formatProviderQuery } from "@/lib/format";
import type {
  AdminBadcase,
  AdminCostBreakdown,
  AdminDecision,
  AdminJevCall,
  AdminLlmCall,
  AdminProviderCall,
  AdminRunDetail,
  AdminStageDetail,
  AdminStageLlmCall,
  AdminStageProgress,
  AdminStageTokens,
  AdminTraceSpan,
} from "@/types/admin";

/**
 * 运行详情。
 *
 * 默认展开的只有 Trace 与阶段这两块「回答运行卡在哪」的最短路径；
 * 所有明细（阶段详情、LLM / Jev 调用、Bad Case）都以弹窗呈现，主页面只留摘要。
 */
export function RunDetailView({ runId }: { runId: string }) {
  const resource = useAdminResource<AdminRunDetail>(`admin-run:${runId}`, () => getAdminRun(runId));
  // 阶段明细来自独立端点，404/未实现时只影响「阶段」这一块，不会让整页失败。
  const stages = useAdminResource<AdminStageDetail[]>(`admin-run-stages:${runId}`, () =>
    getAdminRunStages(runId),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="运行详情"
        description="Trace 按 span 的父子关系展开；阶段来自后端 workflow 的落库进度。点击任意一行查看完整明细。"
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
        {(data) => <RunDetailBody data={data} stages={stages} />}
      </ResourceView>
    </div>
  );
}

function RunDetailBody({
  data,
  stages,
}: {
  data: AdminRunDetail;
  stages: ReturnType<typeof useAdminResource<AdminStageDetail[]>>;
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
            <StatCard label="Jev 调用" value={formatNumber(metrics.jev_calls)} icon={Sparkles} />
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

      <CostPanel metrics={metrics} />

      <Card>
        <CardHeader>
          <CardTitle>阶段</CardTitle>
          <CardDescription>
            workflow 的 {stageRows.length} 个阶段；点击任意阶段查看步骤、事实、工具 / 模型调用与 Token。
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
                <StageRow key={stage.stage_id} stage={stage} runMetrics={metrics} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Trace</CardTitle>
          <CardDescription>
            共 {data.trace.length} 个 span；根节点是 parent_span_id 为空的 span。点击任意 span 查看完整属性与错误。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <TraceTree spans={data.trace} />
        </CardContent>
      </Card>

      <CollapsibleSection
        title="LLM 调用"
        description="模型调用的标签、模型、状态、耗时与字符数"
        count={llmCalls.length}
        icon={Cpu}
      >
        <LlmCallList calls={llmCalls} />
      </CollapsibleSection>

      <CollapsibleSection
        title="Jev 调用"
        description="决策类型、输入摘要、选择结果、置信度与 fallback 原因"
        count={data.jev_calls.length}
        icon={Sparkles}
      >
        <JevCallList calls={data.jev_calls} />
      </CollapsibleSection>

      <CollapsibleSection
        title="Provider 调用"
        description="外部数据源的逐次请求与返回情况"
        count={data.provider_calls.length}
        icon={Share2}
      >
        {data.provider_calls.length === 0 ? (
          <SectionEmpty
            title="没有 Provider 调用记录"
            description="这次运行可能全部命中缓存，或者后端没有落库 Provider 明细。"
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
      </CollapsibleSection>

      <CollapsibleSection title="Token 明细" description="输入 / 输出 / 缓存的拆分" count="4 项" icon={Coins}>
        <TokenBreakdown tokens={metrics} />
      </CollapsibleSection>

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

      <CollapsibleSection
        title="Bad Cases"
        description="本次运行被自动或人工登记的问题案例"
        count={data.badcases.length}
        icon={Bug}
        actions={
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
            全部案例
          </Button>
        }
      >
        <RunBadcaseList badcases={data.badcases} />
      </CollapsibleSection>

      {tokensMissing ? (
        <p className="text-[11px] text-muted-foreground">
          提示：本次运行没有 usage（Benchmark 用例运行使用假模型），因此 Token 与成本都显示为「未知」。
        </p>
      ) : null}
    </div>
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

function StageRow({
  stage,
  runMetrics,
}: {
  stage: AdminStageDetail;
  runMetrics: AdminRunDetail["metrics"];
}) {
  const [open, setOpen] = useState(false);
  const duration = stage.duration_ms ?? durationBetween(stage.started_at, stage.finished_at);
  const steps = stage.steps ?? [];
  const facts = stage.facts ?? null;
  const toolCalls = stage.tool_calls ?? [];
  const llmCalls = stage.llm_calls ?? [];

  return (
    <li>
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

/* ------------------------------ LLM / Jev ------------------------------ */

function LlmCallList({ calls }: { calls: AdminLlmCall[] }) {
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
        {calls.map((call, index) => (
          <li key={`${call.tag}-${index}`}>
            <button
              type="button"
              onClick={() => setSelected(call)}
              className="flex w-full min-w-0 items-center gap-2 rounded-lg border border-border px-2.5 py-2 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-foreground" title={call.tag}>
                {call.tag || "未命名"}
              </span>
              <StatusBadge status={call.status ?? null} />
              <span className="tabular shrink-0 text-[11px] text-muted-foreground">
                {formatDurationMs(call.duration_ms)}
              </span>
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </button>
          </li>
        ))}
      </ul>

      <AdminDetailDialog
        open={selected !== null}
        onOpenChange={(open) => (open ? null : setSelected(null))}
        title={selected?.tag || "LLM 调用"}
        badge={<StatusBadge status={selected?.status ?? null} />}
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

function JevCallList({ calls }: { calls: AdminJevCall[] }) {
  const [selected, setSelected] = useState<AdminJevCall | null>(null);
  if (calls.length === 0) {
    return (
      <SectionEmpty
        title="这次运行没有 Jev 调用"
        description="可能 Jev 未启用、未配置密钥，或者本次规划没有触发需要模型判断的取舍。"
      />
    );
  }
  return (
    <>
      <ul className="flex flex-col gap-2">
        {calls.map((call, index) => (
          <li key={`${call.tag}-${index}`}>
            <button
              type="button"
              onClick={() => setSelected(call)}
              className="flex w-full min-w-0 flex-wrap items-center gap-2 rounded-lg border border-border px-2.5 py-2 text-left hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                {call.tag}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                {missingOr(call.decision_type)}
              </span>
              <StatusBadge status={call.status ?? null} />
              {call.fallback ? <ToneBadge tone="warning">已 fallback</ToneBadge> : null}
              <span className="tabular shrink-0 text-[11px] text-muted-foreground">
                {formatLatency(call.latency_ms)}
              </span>
              <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </button>
          </li>
        ))}
      </ul>

      <AdminDetailDialog
        open={selected !== null}
        onOpenChange={(open) => (open ? null : setSelected(null))}
        title={selected?.decision_type ? `${selected.decision_type}` : "Jev 调用"}
        description={selected?.tag}
        badge={selected ? <StatusBadge status={selected.status ?? null} /> : undefined}
      >
        {selected ? (
          <KeyValueList
            entries={[
              { key: "tag", label: "标签", value: <span className="font-mono">{missingOr(selected.tag)}</span> },
              { key: "type", label: "决策类型", value: missingOr(selected.decision_type) },
              { key: "status", label: "状态", value: <StatusBadge status={selected.status ?? null} /> },
              { key: "model", label: "模型", value: <span className="font-mono">{missingOr(selected.model)}</span> },
              { key: "choice", label: "输出选择", value: missingOr(selected.choice) },
              { key: "confidence", label: "置信度", value: formatConfidence(selected.confidence) },
              { key: "latency", label: "时延", value: formatLatency(selected.latency_ms) },
              {
                key: "fallback",
                label: "fallback",
                value: selected.fallback ? <ToneBadge tone="warning">已触发</ToneBadge> : "未触发",
              },
              {
                key: "fallback_reason",
                label: "fallback 原因",
                value: missingOr(selected.fallback_reason),
              },
              { key: "quota", label: "配额", value: missingOr(formatMetricValue(selected.quota)) },
              { key: "input_summary", label: "输入摘要", value: missingOr(selected.input_summary) },
              { key: "criteria", label: "判定标准", value: missingOr(formatMetricValue(selected.criteria)) },
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

function UserJourneySection({ data }: { data: AdminRunDetail }) {
  const journey = data.user_journey;
  if (!journey) {
    return (
      <SectionEmpty
        title="这次不是引导式创建的"
        description="后端没有返回 user_journey，说明这次运行来自一句话、命令行或 Benchmark，而不是引导式旅程。"
      />
    );
  }
  const selections = journey.place_selections;
  return (
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
        { label: "发现状态", value: missingOr(journey.discovery_status) },
        {
          label: "复用预取",
          value: journey.prefetch_reused === null || journey.prefetch_reused === undefined
            ? "未返回"
            : journey.prefetch_reused
              ? "是"
              : "否",
        },
        { label: "必去", value: selections && selections.must.length > 0 ? selections.must.join("、") : "无" },
        { label: "想去", value: selections && selections.want.length > 0 ? selections.want.join("、") : "无" },
        { label: "排除", value: selections && selections.reject.length > 0 ? selections.reject.join("、") : "无" },
      ]}
    />
  );
}

/* ------------------------------ 工具函数 ------------------------------ */

function ErrorText({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-muted-foreground">未返回</span>;
  return <span className="font-mono text-[11px] break-words text-danger-subtle-foreground">{value}</span>;
}

/** null / undefined / 空串统一成「未返回」，避免弹窗里出现空白行。 */
function missingOr(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim().length === 0) return "未返回";
  return value;
}

function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未返回";
  return formatRatio(value);
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
