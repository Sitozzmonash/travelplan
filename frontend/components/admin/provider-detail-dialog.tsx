"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { AdminDetailDialog, JsonBlock, KeyValueList } from "@/components/admin/detail-dialog";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { ResourceView, SectionEmpty, PartialBanner } from "@/components/admin/admin-states";
import { PanelSection } from "@/components/admin/metric-list";
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
import { clearAdminToken, getAdminProvider } from "@/lib/admin-api";
import { formatDateTime, formatStamp } from "@/lib/format";
import {
  providerSourceLabel,
  providerStatusLabel,
  type AdminProviderDetail,
  type AdminProviderHealthCall,
  type AdminProviderStats,
} from "@/types/admin";

/**
 * Provider 详情弹窗：最近调用明细 + 统计口径。
 *
 * 三条硬约束：
 * 1) 不显示任何密钥 —— 后端已脱敏，前端也不把 query 平铺在主视图，只在可展开区里看；
 * 2) 404 是「没有这个 Provider 的调用账本」，跟「服务坏了」是两回事，单独成空态；
 * 3) 表格在移动端由 AdminTable 自动换成卡片，弹窗本身在移动端是全屏。
 */
export function ProviderDetailDialog({
  provider,
  onOpenChange,
}: {
  provider: string;
  onOpenChange: (open: boolean) => void;
}) {
  const resource = useAdminResource<AdminProviderDetail>(
    `admin-provider:${provider}`,
    () => getAdminProvider(provider),
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
      {notFound ? (
        <SectionEmpty
          title="没有这个 Provider 的调用记录"
          description={`后端没有 provider=${provider} 的任何调用账本。它可能只是这段时间没被用到，也可能这个 id 不存在 —— 两者都不代表故障。`}
          hint="可以返回列表，选择一个出现在卡片里的 Provider 再看明细。"
        />
      ) : (
        <ResourceView resource={resource} loadingRows={5} onClearToken={clearAdminToken}>
          {(detail) => <ProviderDetailBody data={detail} />}
        </ResourceView>
      )}
    </AdminDetailDialog>
  );
}

function ProviderDetailBody({ data }: { data: AdminProviderDetail }) {
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

      <PanelSection title="最近调用" description={`最多 ${data.calls.length} 条（后端按时间倒序返回）。`}>
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

function configuredNode(configured: boolean | null): ReactNode {
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
      value: s.sources && s.sources.length > 0
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
    cell: (call) => (
      <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>
    ),
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
