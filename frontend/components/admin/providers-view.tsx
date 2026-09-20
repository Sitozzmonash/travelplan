"use client";

import { useState } from "react";
import { ExternalLink, RefreshCw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/admin/page-header";
import { PartialBanner, ResourceView } from "@/components/admin/admin-states";
import { StatCard } from "@/components/admin/stat-card";
import { BooleanBadge, StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { formatLatency, formatNumber, formatSampleRatio } from "@/components/admin/format";
import { ProviderDetailDialog } from "@/components/admin/provider-detail-dialog";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { clearAdminToken, getAdminProviders } from "@/lib/admin-api";
import {
  providerSourceLabel,
  providerStatusLabel,
  type AdminProviderList,
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
        description="按最近的真实调用账本聚合，快速判断哪个外部数据源最近不稳定。没有调用记录显示 UNKNOWN，不代表故障。"
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
          这些 Provider 的状态是 DEGRADED / UNAVAILABLE，建议结合调用来源与最近错误排查。
        </p>
      </div>
    </div>
  );
}

/* ------------------------------ 卡片 ------------------------------ */

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
      aria-label={`查看 ${item.label} 的最近调用`}
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

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted-foreground">密钥</span>
        {item.configured === null ? (
          <span className="text-[11px] text-muted-foreground">—（后端未声明）</span>
        ) : (
          <BooleanBadge
            value={item.configured}
            onLabel="已配置"
            offLabel="未配置"
            onTone="success"
          />
        )}
        <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground">
          查看最近调用
          <ExternalLink className="size-3" aria-hidden />
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
        <Metric label="最近一次调用" value={formatDateTime(item.last_call_at)} />
        <Metric label="最近成功" value={formatDateTime(item.last_success_at)} />
        <Metric label="最近失败" value={formatDateTime(item.last_failure_at)} />
        <Metric label="调用次数" value={formatNumber(calls)} />
        <Metric label="成功率" value={formatSampleRatio(item.success_rate)} />
        <Metric label="失败率" value={formatSampleRatio(item.failure_rate)} />
        <Metric label="Timeout 次数" value={formatNumber(item.timeouts)} />
        <Metric label="Fallback 次数" value={formatNumber(item.fallback_count)} />
        <Metric label="平均延迟" value={formatLatency(item.avg_latency_ms)} />
        <Metric label="P95 延迟" value={formatLatency(item.p95_latency_ms)} />
      </dl>

      <TagRow label="来源" values={item.sources ?? []} renderLabel={providerSourceLabel} />
      <TagRow label="工具" values={item.tools ?? []} />

      {item.last_error ? (
        <p
          className="rounded-md bg-danger-subtle px-2.5 py-1.5 text-[11px] leading-4 break-words text-danger-subtle-foreground"
          title={item.last_error}
        >
          最近错误：{item.last_error}
        </p>
      ) : null}

      {noHistory ? (
        <p className="rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-4 text-muted-foreground">
          这段时间没有调用记录，不代表故障。状态按 UNKNOWN 展示，成功 / 失败率为「—」。
        </p>
      ) : null}

      {insufficientSample ? (
        <p className="rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-4 text-muted-foreground">
          最近调用样本偏少，后端暂不下健康结论；这不代表故障。
        </p>
      ) : null}
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

function TagRow({
  label,
  values,
  renderLabel,
}: {
  label: string;
  values: string[];
  renderLabel?: (value: string | null | undefined) => string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      {values.length === 0 ? (
        <span className="text-[11px] text-muted-foreground">—</span>
      ) : (
        values.map((value) => (
          <ToneBadge key={value} tone="muted" className="text-[0.6875rem]">
            <span title={value}>{renderLabel ? renderLabel(value) : value}</span>
          </ToneBadge>
        ))
      )}
    </div>
  );
}
