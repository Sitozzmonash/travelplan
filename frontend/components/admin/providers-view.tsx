"use client";

import { useState } from "react";
import Link from "next/link";
import { ExternalLink, RefreshCw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AdminDetailDialog, JsonBlock, KeyValueList } from "@/components/admin/detail-dialog";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { FilterSelect } from "@/components/admin/filter-select";
import { PageHeader } from "@/components/admin/page-header";
import { PartialBanner, ResourceView, SectionEmpty, InlineSpinner } from "@/components/admin/admin-states";
import { PanelSection } from "@/components/admin/metric-list";
import { StatCard } from "@/components/admin/stat-card";
import { BooleanBadge, StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import {
  formatDurationMs,
  formatLatency,
  formatMetricValue,
  formatNumber,
  formatSampleRatio,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { cn } from "@/lib/utils";
import { clearAdminToken, getAdminProvider, getAdminProviders } from "@/lib/admin-api";
import { formatDateTime, formatStamp } from "@/lib/format";
import {
  providerSourceLabel,
  providerStatusLabel,
  type AdminProviderDetail,
  type AdminProviderHealthCall,
  type AdminProviderList,
  type AdminProviderStats,
  type AdminProviderStatus,
  type AdminProviderSummary,
} from "@/types/admin";

/**
 * Provider 健康总览。
 *
 * 数据来源是后端的**真实调用账本**（最近每个 Provider 200 次调用），页面不做假探活、
 * 不主动打第三方付费接口、也永远不回显任何密钥。
 *
 * 两个必须守住的展示规则：
 * 1) `calls === 0` → UNKNOWN（中性灰）+ 文案说明「没有调用记录，不代表故障」，
 *    绝不画成红色失败；
 * 2) `success_rate / failure_rate === null` → 显示「—（无样本）」，绝不落成 0%。
 *
 * 密度约定（方案 §8）：默认卡片只留「扫一眼就能判断健康」的字段 ——
 * 成功率 / P95 / Timeout / Fallback / 最近错误；调用次数、失败率、平均延迟、来源、工具、
 * 最近调用表、原始错误、查询参数全部收进详情弹窗，卡片不再一次铺开十几个数字。
 */

/** 不同状态的卡片描边：只有真正的故障才用危险色。 */
const STATUS_RING: Record<AdminProviderStatus, string> = {
  HEALTHY: "ring-foreground/10",
  DEGRADED: "ring-warning/30",
  UNAVAILABLE: "ring-danger/30",
  UNKNOWN: "ring-foreground/10",
};

export function ProvidersView() {
  const [selected, setSelected] = useState<string | null>(null);
  const resource = useAdminResource<AdminProviderList>("admin-providers", () => getAdminProviders());

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Provider 健康"
        description="按最近的真实调用账本聚合，快速判断哪个外部数据源最近不稳定。卡片只给关键读数，点开看完整口径。没有调用记录显示 UNKNOWN，不代表故障。"
        actions={
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <ResourceView
        resource={resource}
        loadingRows={6}
        onClearToken={clearAdminToken}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="还没有任何 Provider 调用记录"
        emptyDescription="后端 provider_calls 账本为空：可能还没产生真实调用，或这是一个全新的库。这不代表任何 Provider 故障。"
      >
        {(data) => (
          <div className="flex flex-col gap-4">
            <ProviderSummaryStrip data={data} />
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {data.items.map((item) => (
                <ProviderCard key={item.provider} item={item} onSelect={setSelected} />
              ))}
            </div>
          </div>
        )}
      </ResourceView>

      {selected ? (
        <ProviderDetailDialog
          provider={selected}
          onOpenChange={(open) => {
            if (!open) setSelected(null);
          }}
        />
      ) : null}
    </div>
  );
}

/* ------------------------------ 顶部摘要条 ------------------------------ */

function ProviderSummaryStrip({ data }: { data: AdminProviderList }) {
  const summary = data.summary;
  const labelById = new Map(data.items.map((item) => [item.provider, item.label]));
  // 有卡片缺 `calls`（后端只给了一部分统计）时，也按 Partial 提示，而不是假装完整。
  const partialItems = data.items.filter((item) => item.calls === null || item.calls === undefined).length;

  return (
    <div className="flex flex-col gap-3">
      {summary ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <StatCard
              label="Provider 总数"
              value={formatNumber(summary.providers)}
              hint="配置了或出现过的数据源"
            />
            <StatCard
              label="健康"
              value={formatNumber(summary.healthy)}
              tone="success"
              hint="最近样本成功率正常"
            />
            <StatCard
              label="降级"
              value={formatNumber(summary.degraded)}
              tone={countTone(summary.degraded, "warning")}
              hint="失败率偏高"
            />
            <StatCard
              label="不可用"
              value={formatNumber(summary.unavailable)}
              tone={countTone(summary.unavailable, "danger")}
              hint="连续鉴权失败或失败率很高"
            />
            <StatCard
              label="未知（无结论）"
              value={formatNumber(summary.unknown)}
              tone="muted"
              hint="没有调用记录或样本不足，都不代表故障"
            />
          </div>

          {summary.needs_attention.length > 0 ? (
            <NeedsAttention items={summary.needs_attention} labelById={labelById} />
          ) : (
            <p className="text-[11px] leading-4 text-muted-foreground">
              当前没有需要关注的 Provider。
            </p>
          )}
        </>
      ) : (
        <PartialBanner
          title="后端没有返回摘要统计"
          description="下面的卡片仍然可用。摘要缺失时不做二次推断，避免用一个算出来的数字下错误的健康结论。"
        />
      )}

      {partialItems > 0 ? (
        <PartialBanner
          title={`有 ${partialItems} 个 Provider 的统计字段不完整`}
          description="缺失的字段统一以「—」占位；这不影响其它 Provider 的判断。"
        />
      ) : null}

      {data.note ? (
        <p className="text-[11px] leading-4 text-muted-foreground">{data.note}</p>
      ) : null}
    </div>
  );
}

/** 计数为 0 时不用危险色，避免「零次=红色」这种误导。 */
function countTone(
  value: number | null,
  tone: "warning" | "danger",
): "warning" | "danger" | "muted" {
  return value !== null && value > 0 ? tone : "muted";
}

function NeedsAttention({
  items,
  labelById,
}: {
  items: string[];
  labelById: Map<string, string>;
}) {
  const labels = items.map((provider) => labelById.get(provider) ?? provider);
  return (
    <div className="flex gap-2.5 rounded-lg border border-warning/30 bg-warning-subtle px-3.5 py-3 text-xs leading-5">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
      <div className="min-w-0">
        <p className="font-medium text-warning-subtle-foreground">需要关注</p>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-warning-subtle-foreground/90">
          <span>需要关注：</span>
          {items.map((provider, index) => (
            <span key={provider} className="inline-flex items-center gap-1">
              {labels[index]}
              <span className="font-mono text-[10px] text-warning-subtle-foreground/70">
                ({provider})
              </span>
              {index < items.length - 1 ? <span>、</span> : null}
            </span>
          ))}
        </p>
        <p className="mt-0.5 text-warning-subtle-foreground/75">
          这些 Provider 的状态是 DEGRADED / UNAVAILABLE，建议点开卡片看完整工具、调用来源与原始错误。
        </p>
      </div>
    </div>
  );
}

/* ------------------------------ 卡片（默认只给关键读数） ------------------------------ */

function ProviderCard({
  item,
  onSelect,
}: {
  item: AdminProviderSummary;
  onSelect: (provider: string) => void;
}) {
  const calls = item.calls ?? null;
  const noHistory = calls === 0;
  // UNKNOWN 也可能是「有调用但样本太少」：这时不能写「没有调用记录」。
  const insufficientSample = item.status === "UNKNOWN" && (calls ?? 0) > 0;

  return (
    <button
      type="button"
      onClick={() => onSelect(item.provider)}
      aria-label={`查看 ${item.label} 的调用明细`}
      className={cn(
        "flex min-w-0 flex-col gap-3 rounded-xl bg-card p-3.5 text-left ring-1 transition-colors hover:bg-muted/30 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
        STATUS_RING[item.status],
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground" title={item.label}>
            {item.label}
          </p>
          <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground" title={item.provider}>
            {item.provider}
          </p>
        </div>
        <StatusBadge status={item.status} label={providerStatusLabel(item.status, calls)} />
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
        <Metric label="成功率" value={formatSampleRatio(item.success_rate)} />
        <Metric label="P95 延迟" value={formatLatency(item.p95_latency_ms)} />
        <Metric label="Timeout 次数" value={formatNumber(item.timeouts)} />
        <Metric label="Fallback 次数" value={formatNumber(item.fallback_count)} />
      </dl>

      {/* 错误文本只给一行：完整内容放在 title 与详情弹窗里，卡片保持可扫读。 */}
      <p
        className={cn(
          "rounded-md px-2.5 py-1.5 text-[11px] leading-4",
          item.last_error
            ? "bg-danger-subtle text-danger-subtle-foreground"
            : "bg-muted/60 text-muted-foreground",
        )}
        title={item.last_error ?? "没有最近错误"}
      >
        <span className="block truncate">最近错误：{item.last_error || "—"}</span>
      </p>

      {noHistory ? (
        <p className="rounded-md bg-muted/60 px-2.5 py-1.5 text-[11px] leading-4 text-muted-foreground">
          没有调用记录，不代表故障。
        </p>
      ) : null}

      {insufficientSample ? (
        <p className="rounded-md bg-muted/60 px-2.5 py-1.5 text-[11px] leading-4 text-muted-foreground">
          样本偏少，后端暂不下健康结论；这不代表故障。
        </p>
      ) : null}

      <span className="self-end inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        查看调用明细
        <ExternalLink className="size-3" aria-hidden />
      </span>
    </button>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-2 border-b border-border/60 pb-1">
      <dt className="shrink-0 text-[11px] text-muted-foreground">{label}</dt>
      <dd className="tabular min-w-0 truncate text-right text-xs font-medium text-foreground" title={value}>
        {value}
      </dd>
    </div>
  );
}

/* ------------------------------ 详情弹窗 ------------------------------ */

/** 明细条数：把「看多深」交给操作员，而不是写死后端默认的 20 条。 */
const CALL_LIMIT_OPTIONS = [
  { value: "20", label: "最近 20 次调用" },
  { value: "100", label: "最近 100 次调用" },
  { value: "200", label: "最近 200 次调用" },
];

/**
 * 详情弹窗：卡片上被删掉的技术字段全部收在这里（调用来源 / 工具 / 平均延迟 / 原始错误 …）。
 *
 * 「看多深」交给操作员（20 / 100 / 200 条），而不是写死后端默认的 20 条 ——
 * 排查 TikHub 这类高频 Provider 时，20 条样本根本看不出失败是不是集中在某个工具上。
 */
function ProviderDetailDialog({
  provider,
  onOpenChange,
}: {
  provider: string;
  onOpenChange: (open: boolean) => void;
}) {
  const [limitKey, setLimitKey] = useState("20");
  const limit = Number(limitKey);

  const resource = useAdminResource<AdminProviderDetail>(
    `admin-provider:${provider}:${limit}`,
    () => getAdminProvider(provider, limit),
  );

  const data = resource.data;
  const notFound = resource.error?.kind === "not_found";

  return (
    <AdminDetailDialog
      open
      onOpenChange={onOpenChange}
      title={data?.label ?? provider}
      description={<span className="font-mono">{provider}</span>}
      badge={
        data ? (
          <StatusBadge
            status={data.status}
            label={providerStatusLabel(data.status, data.stats?.calls)}
          />
        ) : null
      }
    >
      <div className="flex flex-col gap-1">
        <FilterSelect
          label="明细条数"
          value={limitKey}
          options={CALL_LIMIT_OPTIONS}
          onChange={setLimitKey}
          placeholder="最近 20 次调用"
          className="w-48"
        />
        {/* 换条数时 useAdminResource 会保留上一批明细，这里明说正在重取，避免旧数据被当成新样本。 */}
        {resource.loading && data ? <InlineSpinner label="正在重新取调用明细" /> : null}
      </div>

      {notFound ? (
        <SectionEmpty
          title="没有这个 Provider 的调用记录"
          description={`后端没有 provider=${provider} 的任何调用账本。它可能只是这段时间没被用到，也可能这个 id 不存在 —— 两者都不代表故障。`}
          hint="可以返回列表，选择一个出现在卡片里的 Provider 再看明细。"
        />
      ) : (
        <ResourceView resource={resource} loadingRows={5} onClearToken={clearAdminToken}>
          {(detail) => <ProviderDetailBody data={detail} limit={limit} />}
        </ResourceView>
      )}
    </AdminDetailDialog>
  );
}

function ProviderDetailBody({ data, limit }: { data: AdminProviderDetail; limit: number }) {
  const withQuery = data.calls.filter((call) => Object.keys(call.query).length > 0);

  return (
    <div className="flex flex-col gap-5">
      {!data.stats ? (
        <PartialBanner
          title="后端没有返回这个 Provider 的聚合统计"
          description="最近调用明细仍然可用；统计数字缺失时不做二次推断，避免给出错误的健康结论。"
        />
      ) : null}

      <PanelSection
        title="统计口径"
        description={data.note || "基于真实调用账本聚合，不做主动探活。"}
      >
        <KeyValueList entries={buildStatEntries(data.stats, data.configured)} />
      </PanelSection>

      <PanelSection
        title="最近调用"
        description={`后端按时间倒序返回 ${formatNumber(data.calls.length)} 条（本次请求 ${formatNumber(
          limit,
        )} 条）。逐条耗时就是延迟的分布读数：后端没有分位数直方图口径，这里不额外画分布图。`}
      >
        {data.calls.length > 0 ? (
          <AdminTable
            columns={CALL_COLUMNS}
            rows={data.calls}
            getRowKey={(call) => call.call_id ?? providerKey(call)}
          />
        ) : (
          <SectionEmpty
            title="这个 Provider 最近没有调用记录"
            description="没有调用历史不代表故障：它可能只是这段时间没被用到。"
          />
        )}
      </PanelSection>

      <PanelSection
        title="原始错误"
        description="未经改写的文本，便于直接对照后端日志；同一条错误只列一次。"
      >
        <RawErrors data={data} />
      </PanelSection>

      <PanelSection
        title="查询参数（默认折叠）"
        description="后端已脱敏；这里只展示解释「查了什么」的键值，不会出现任何密钥。"
      >
        {withQuery.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {withQuery.map((call, index) => (
              <li key={call.call_id ?? `query-${index}`}>
                <details className="rounded-lg border border-border p-2.5">
                  <summary className="cursor-pointer text-xs text-foreground">
                    {formatStamp(call.fetched_at)} · {call.tool ?? "未知工具"} ·{" "}
                    {statusLabel(call.status)}
                  </summary>
                  <JsonBlock className="mt-2" value={call.query} maxHeight="16rem" />
                </details>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">这批调用没有可展开的查询参数。</p>
        )}
      </PanelSection>
    </div>
  );
}

function RawErrors({ data }: { data: AdminProviderDetail }) {
  const lastError = data.stats?.last_error ?? null;
  const errors = distinctErrors(data.calls);

  if (!lastError && errors.length === 0) {
    return <p className="text-xs text-muted-foreground">这批调用没有错误记录，也没有最近错误。</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      {lastError ? (
        <p className="text-xs leading-5 break-words text-danger-subtle-foreground">
          <span className="text-[11px] text-muted-foreground">最近错误：</span>
          {lastError}
        </p>
      ) : null}
      {errors.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {errors.map((text) => (
            <li
              key={text}
              className="rounded-md bg-muted/50 px-2.5 py-1.5 font-mono text-[11px] leading-4 break-words text-foreground"
            >
              {text}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function distinctErrors(calls: AdminProviderHealthCall[]): string[] {
  const values = calls
    .map((call) => call.error)
    .filter((error): error is string => typeof error === "string" && error.length > 0);
  return Array.from(new Set(values));
}

function configuredNode(configured: boolean | null) {
  if (configured === null) return <span className="text-muted-foreground">—（后端未声明）</span>;
  return <BooleanBadge value={configured} onLabel="已配置" offLabel="未配置" onTone="success" />;
}

function buildStatEntries(stats: AdminProviderStats | null, configured: boolean | null) {
  const s = stats ?? {};
  return [
    { key: "configured", label: "密钥配置", value: configuredNode(configured) },
    { key: "calls", label: "调用次数", value: formatNumber(s.calls) },
    {
      key: "success",
      label: "成功 / 失败",
      value: `${formatNumber(s.successes)} / ${formatNumber(s.failures)}`,
    },
    { key: "success_rate", label: "成功率", value: formatSampleRatio(s.success_rate) },
    { key: "failure_rate", label: "失败率", value: formatSampleRatio(s.failure_rate) },
    { key: "timeouts", label: "Timeout 次数", value: formatNumber(s.timeouts) },
    { key: "auth_errors", label: "鉴权失败", value: formatNumber(s.auth_errors) },
    { key: "rate_limited", label: "被限流", value: formatNumber(s.rate_limited) },
    { key: "empty", label: "空结果", value: formatNumber(s.empty) },
    { key: "fallback_count", label: "Fallback 次数", value: formatNumber(s.fallback_count) },
    { key: "avg_latency_ms", label: "平均延迟", value: formatLatency(s.avg_latency_ms) },
    { key: "p95_latency_ms", label: "P95 延迟", value: formatLatency(s.p95_latency_ms) },
    { key: "last_call_at", label: "最近调用", value: formatDateTime(s.last_call_at) },
    { key: "last_success_at", label: "最近成功", value: formatDateTime(s.last_success_at) },
    { key: "last_failure_at", label: "最近失败", value: formatDateTime(s.last_failure_at) },
    {
      key: "last_status",
      label: "最近一次状态",
      value: <StatusBadge status={s.last_status ?? null} />,
    },
    {
      key: "last_error",
      label: "最近错误",
      value: s.last_error ? (
        <span className="break-words text-danger-subtle-foreground">{s.last_error}</span>
      ) : (
        "—"
      ),
    },
    { key: "tools", label: "工具", value: joinOrDash(s.tools) },
    {
      key: "sources",
      label: "调用来源",
      value:
        s.sources && s.sources.length > 0
          ? s.sources.map((source) => providerSourceLabel(source)).join("、")
          : "—",
    },
  ];
}

function joinOrDash(values: string[] | null | undefined): string {
  return values && values.length > 0 ? values.join("、") : "—";
}

function providerKey(call: AdminProviderHealthCall): string {
  return `${call.provider ?? "provider"}-${call.tool ?? "tool"}-${call.fetched_at ?? "time"}`;
}

const CALL_COLUMNS: AdminColumn<AdminProviderHealthCall>[] = [
  {
    key: "tool",
    header: "工具 / 状态",
    primary: true,
    cell: (call) => (
      <span className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-xs text-foreground">{call.tool ?? "未知工具"}</span>
        <StatusBadge status={call.status} />
      </span>
    ),
  },
  {
    key: "time",
    header: "时间",
    cell: (call) => (
      <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
        {formatStamp(call.fetched_at)}
      </span>
    ),
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>,
  },
  {
    key: "returned",
    header: "返回",
    align: "right",
    cell: (call) => (
      <span className="tabular text-xs text-foreground">{formatMetricValue(call.returned)}</span>
    ),
  },
  {
    key: "source",
    header: "来源",
    mobileHidden: true,
    cell: (call) => (
      <ToneBadge tone={call.source_type === "discovery" ? "info" : "muted"} className="text-[0.6875rem]">
        <span title={call.source_type ?? "未知来源"}>{providerSourceLabel(call.source_type)}</span>
      </ToneBadge>
    ),
  },
  {
    key: "fallback",
    header: "Fallback",
    cell: (call) => (
      <BooleanBadge
        value={call.fallback}
        onLabel="是"
        offLabel="否"
        onTone="warning"
        offTone="muted"
      />
    ),
  },
  {
    key: "error",
    header: "错误",
    mobileHidden: true,
    cell: (call) =>
      call.error ? (
        <span
          className="block max-w-[16rem] truncate text-[11px] text-danger-subtle-foreground"
          title={call.error}
        >
          {call.error}
        </span>
      ) : (
        <span className="text-[11px] text-muted-foreground">—</span>
      ),
  },
  {
    key: "link",
    header: "Run / Session",
    mobileHidden: true,
    cell: (call) => <RunOrSessionLink call={call} />,
  },
];

function RunOrSessionLink({ call }: { call: AdminProviderHealthCall }) {
  if (call.run_id) {
    return (
      <Link
        href={`/admin/runs/${encodeURIComponent(call.run_id)}`}
        title={`run_id=${call.run_id}`}
        className="font-mono text-[11px] text-primary underline-offset-4 hover:underline"
      >
        {call.run_id}
      </Link>
    );
  }
  if (call.session_id) {
    return (
      <Link
        href={`/admin/sessions?q=${encodeURIComponent(call.session_id)}`}
        title={`session_id=${call.session_id}`}
        className="font-mono text-[11px] text-primary underline-offset-4 hover:underline"
      >
        {call.session_id}
      </Link>
    );
  }
  return <span className="text-[11px] text-muted-foreground">—</span>;
}
