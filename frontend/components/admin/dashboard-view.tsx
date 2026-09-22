"use client";

import { useContext, useState } from "react";
import Link from "next/link";
import { Activity, FlaskConical, Gauge, HeartPulse, RefreshCw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AttentionPanel } from "@/components/admin/attention-panel";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PanelSection } from "@/components/admin/metric-list";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { StatCard, type StatCardTrend } from "@/components/admin/stat-card";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { Sparkline } from "@/components/admin/trend-sparkline";
import { WindowSelect, windowLabel } from "@/components/admin/window-select";
import {
  formatCost,
  formatDurationMs,
  formatNumber,
  formatPassRate,
  formatRatio,
  formatTokens,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { AdminOverviewContext } from "@/components/admin/admin-shell";
import { getAdminDashboard, getAdminOverview } from "@/lib/admin-api";
import {
  DEFAULT_ADMIN_WINDOW,
  type AdminDashboard,
  type AdminKpi,
  type AdminOverview,
  type AdminTrendSeries,
  type AdminWindowKey,
} from "@/types/admin";

/**
 * 仪表盘：一屏回答「现在系统健康吗、最该修什么」。
 *
 * 与旧版的区别（docs/TravelPlan_Admin_整体优化方案.md §3）：
 *   * 首屏只留关键指标，且全部**带环比**与时间范围 —— 历史累计量（总 Token / 总工具调用）
 *     不再占首屏，它们被收进页面底部的「累计用量（历史总量）」折叠区；
 *   * 「当前需要关注」提到最显眼的位置，每条都带可点的入口；
 *   * Jev 不再是核心健康指标（文档 §10 把它降为可选增强），相关明细与累计用量放在一起。
 */
export function DashboardView() {
  const [windowKey, setWindowKey] = useState<AdminWindowKey>(DEFAULT_ADMIN_WINDOW);
  const dashboard = useAdminResource<AdminDashboard>(
    `admin-dashboard:${windowKey}`,
    () => getAdminDashboard(windowKey),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="仪表盘"
        description="按时间范围统计的关键指标、环比与当前需要关注的问题。所有数字都来自后端聚合，管理台不做二次计算。"
        actions={
          <div className="flex items-end gap-2">
            <WindowSelect value={windowKey} onChange={setWindowKey} />
            <Button variant="outline" size="sm" onClick={dashboard.reload}>
              <RefreshCw />
              刷新
            </Button>
          </div>
        }
      />

      <ResourceView resource={dashboard} loadingRows={6}>
        {(data) => (
          <div className="flex flex-col gap-4">
            <p className="text-[11px] text-muted-foreground">
              {windowLabel(data.window, data.window_label)} · 更新于 {data.generated_at} ·
              环比对照的是等长上一周期。
            </p>

            <KpiGrid kpis={data.kpis} />

            <AttentionPanel items={data.attention} />

            <TrendGrid trends={data.trends} />

            <div className="grid gap-3 lg:grid-cols-2">
              <StatusDistribution data={data} />
              <DecisionHealth data={data} />
              <ProviderSnapshot data={data} />
              <QualityClosure data={data} />
            </div>

            {data.notes.length > 0 ? (
              <section className="rounded-xl border border-border bg-muted/30 p-3.5">
                <p className="text-xs font-medium text-foreground">口径与数据缺口</p>
                <ul className="mt-1.5 list-disc space-y-1 pl-4 text-[11px] leading-5 text-muted-foreground">
                  {data.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              </section>
            ) : null}

            <LifetimeUsage />
          </div>
        )}
      </ResourceView>
    </div>
  );
}

/* ------------------------------ 关键指标 ------------------------------ */

/**
 * 环比文案：delta 与指标**同单位**，所以格式必须跟着 unit 走。
 * 比率类的 delta 是百分点差（0.021 → 2.1%），不是相对变化率 —— 这是后端定义的口径。
 */
function formatDelta(kpi: AdminKpi): string | null {
  if (kpi.delta === null || kpi.direction === "flat") return null;
  const absolute = Math.abs(kpi.delta);
  const unit = kpi.unit ?? "";
  if (unit === "ratio" || unit === "score") return formatRatio(absolute);
  if (unit === "tokens") return formatTokens(absolute);
  if (unit === "ms") return formatDurationMs(absolute);
  if (unit === "cny") return formatCost(absolute);
  return formatNumber(absolute);
}

function formatKpiValue(kpi: AdminKpi): string {
  if (kpi.value === null) return "—";
  // 质量分虽然按比率存（0~1），但页面上用「分」比用「%」更贴合它的含义。
  // 这是**唯一**按 key 的特判：新增指标（如 token）一律按 unit 走，未知 unit 退回普通数字，
  // 这样后端加指标时前端不必再加一个 key 分支，也不会把 0.86 这种值渲染成「1」。
  if (kpi.key === "quality_score") return `${(kpi.value * 100).toFixed(0)} 分`;
  const unit = kpi.unit ?? "";
  if (unit === "ratio" || unit === "score") return formatRatio(kpi.value);
  // 后端新增的 token KPI 可能带 unit="tokens"，也可能复用 count / 干脆不给 unit；
  // 只有明确是 tokens 时走大数格式化，其余落到 formatNumber 兜底（已是千分位，可读）。
  if (unit === "tokens") return formatTokens(kpi.value);
  if (unit === "ms") return formatDurationMs(kpi.value);
  if (unit === "cny") return formatCost(kpi.value);
  return formatNumber(kpi.value);
}

function kpiTone(kpi: AdminKpi): "muted" | "success" | "warning" | "danger" | "info" {
  if (kpi.better === true) return "success";
  if (kpi.better === false) return "danger";
  return "muted";
}

function KpiGrid({ kpis }: { kpis: AdminKpi[] }) {
  if (kpis.length === 0) {
    return <SectionEmpty title="后端没有返回关键指标" description="这一屏靠 /admin/dashboard 聚合，返回空对象时不做本地兜底推算。" />;
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {kpis.map((kpi) => {
        const delta = formatDelta(kpi);
        const trend: StatCardTrend | null =
          delta === null
            ? null
            : {
                direction: kpi.direction === "flat" ? "flat" : kpi.direction === "up" ? "up" : "down",
                label: delta,
                better: kpi.better,
              };
        return (
          <StatCard
            key={kpi.key}
            label={kpi.label}
            value={formatKpiValue(kpi)}
            hint={kpi.hint ?? undefined}
            tone={kpiTone(kpi)}
            trend={trend}
          />
        );
      })}
    </div>
  );
}

/* ------------------------------ 趋势 ------------------------------ */

function formatTrendValue(series: AdminTrendSeries, value: number | null): string {
  if (value === null) return "—";
  if (series.unit === "ratio") return formatRatio(value);
  if (series.unit === "ms") return formatDurationMs(value);
  if (series.unit === "cny") return formatCost(value);
  return formatNumber(value);
}

function TrendGrid({ trends }: { trends: AdminTrendSeries[] }) {
  if (trends.length === 0) return null;
  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {trends.map((series) => (
        <div
          key={series.key}
          className="flex flex-col gap-1 rounded-xl bg-card p-3.5 ring-1 ring-foreground/10"
        >
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs text-muted-foreground">{series.label}</span>
            <span className="tabular text-sm font-medium text-foreground">
              {formatTrendValue(series, series.summary.latest)}
            </span>
          </div>
          <Sparkline
            points={series.points.map((point) => point.value)}
            ariaLabel={`${series.label} 趋势`}
            tone={series.key === "badcases" ? "warning" : "info"}
            variant={series.key === "badcases" ? "bar" : "line"}
          />
          <span className="text-[10px] text-muted-foreground">
            最低 {formatTrendValue(series, series.summary.min)} · 最高{" "}
            {formatTrendValue(series, series.summary.max)} · 均值{" "}
            {formatTrendValue(series, series.summary.avg)}
          </span>
        </div>
      ))}
    </section>
  );
}

/* ------------------------------ 次要卡片 ------------------------------ */

function Card({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2 rounded-xl bg-card p-3.5 ring-1 ring-foreground/10">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex flex-col">
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
          {description ? (
            <span className="text-[11px] leading-4 text-muted-foreground">{description}</span>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function StatusDistribution({ data }: { data: AdminDashboard }) {
  // statuses 在契约里是 AdminRecord（值 unknown）：先收窄成数字再排序，
  // 否则统计口径还没稳定时这里会拿字符串去做减法。
  const entries = Object.entries(data.statuses)
    .map(([status, count]) => ({ status, count: typeof count === "number" ? count : 0 }))
    .sort((left, right) => right.count - left.count);
  return (
    <Card
      title="运行状态分布"
      description={`${data.window_label}内 ${data.volume.runs} 次运行（上一周期 ${data.volume.previous_runs} 次）`}
      action={
        <Link
          href="/admin/runs"
          className="text-[11px] font-medium text-primary underline-offset-4 hover:underline"
        >
          查看运行记录
        </Link>
      }
    >
      {entries.length === 0 ? (
        <p className="text-xs text-muted-foreground">这个时间窗内没有运行。</p>
      ) : (
        <ul className="flex flex-wrap gap-2">
          {entries.map((entry) => (
            <li key={entry.status} className="flex items-center gap-1.5">
              <StatusBadge status={entry.status} />
              <span className="tabular text-xs text-foreground">{formatNumber(entry.count)}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DecisionHealth({ data }: { data: AdminDashboard }) {
  const health = data.decision_health;
  const items: { label: string; value: string; hint?: string }[] = [
    {
      label: "动态偏好画像成功率",
      value: health.profile_success_rate === null ? "—" : formatRatio(health.profile_success_rate),
      hint: `分母是 ${health.guided_runs} 个引导式运行（Quick 没有画像）`,
    },
    { label: "画像由 LLM 生成", value: formatNumber(health.profile_llm) },
    { label: "画像走 fallback", value: formatNumber(health.profile_fallback) },
    { label: "带硬错误的运行", value: formatNumber(health.runs_with_hard_errors) },
    { label: "REJECT 进了计划", value: formatNumber(health.runs_with_rejected_in_plan) },
    { label: "丢失 MUST 的运行", value: formatNumber(health.runs_missing_must) },
  ];
  return (
    <Card
      title="决策健康"
      description={health.note || "按抽样的运行统计"}
      action={
        <Link
          href="/admin/travel-quality?focus=preference_match"
          className="text-[11px] font-medium text-primary underline-offset-4 hover:underline"
        >
          打开质量页
        </Link>
      }
    >
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        {items.map((item) => (
          <div key={item.label} className="flex flex-col gap-0.5" title={item.hint}>
            <dt className="text-[11px] text-muted-foreground">{item.label}</dt>
            <dd className="tabular text-xs font-medium text-foreground">{item.value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function ProviderSnapshot({ data }: { data: AdminDashboard }) {
  const unhealthy = data.providers.items.filter((item) => item.needs_attention);
  return (
    <Card
      title="Provider 健康"
      description={data.providers.window_note || "按每个 Provider 最近的调用统计"}
      action={
        <Link
          href="/admin/providers"
          className="text-[11px] font-medium text-primary underline-offset-4 hover:underline"
        >
          查看详情
        </Link>
      }
    >
      {data.providers.total === 0 ? (
        <p className="text-xs text-muted-foreground">还没有 Provider 调用记录。</p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <HeartPulse className="size-4 text-muted-foreground" aria-hidden />
            <span className="text-xs text-foreground">
              {data.providers.total} 个 Provider，{data.providers.unhealthy} 个需要关注
            </span>
          </div>
          {unhealthy.length > 0 ? (
            <ul className="flex flex-wrap gap-2">
              {unhealthy.map((item) => (
                <li key={item.provider} className="flex items-center gap-1.5">
                  <ToneBadge tone={item.state === "UNAVAILABLE" ? "danger" : "warning"}>
                    {item.state}
                  </ToneBadge>
                  <span className="text-xs text-foreground">{item.provider}</span>
                  <span className="tabular text-[11px] text-muted-foreground">
                    {item.failure_rate === null ? "—" : formatRatio(item.failure_rate)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">没有处于降级/不可用的 Provider。</p>
          )}
        </div>
      )}
    </Card>
  );
}

function QualityClosure({ data }: { data: AdminDashboard }) {
  return (
    <Card title="质量闭环" description="质量问题、评测与自动优化的当前状态">
      <ul className="flex flex-col gap-2 text-xs text-foreground">
        <li className="flex items-center gap-2">
          <TriangleAlert className="size-4 text-muted-foreground" aria-hidden />
          <span>
            未处理 Bad Case {formatNumber(data.badcases.open)} 条（{data.window_label}新增{" "}
            {formatNumber(data.badcases.window)}，上一周期 {formatNumber(data.badcases.previous)}）
          </span>
          <Link
            href="/admin/badcases"
            className="ml-auto shrink-0 text-[11px] font-medium text-primary underline-offset-4 hover:underline"
          >
            问题中心
          </Link>
        </li>
        <li className="flex items-center gap-2">
          <FlaskConical className="size-4 text-muted-foreground" aria-hidden />
          <span>
            最新 Benchmark 通过率 {formatPassRate(data.benchmark.latest_pass_rate)}
            {data.benchmark.latest_run_id ? `（${data.benchmark.latest_run_id}）` : "（还没有跑过）"}
          </span>
          <Link
            href="/admin/benchmark"
            className="ml-auto shrink-0 text-[11px] font-medium text-primary underline-offset-4 hover:underline"
          >
            查看
          </Link>
        </li>
        <li className="flex items-center gap-2">
          <Activity className="size-4 text-muted-foreground" aria-hidden />
          <span>
            Evolution {data.evolution.enabled ? "已启用" : "未启用"}
            {data.evolution.last_decision ? ` · 最近决策 ${data.evolution.last_decision}` : ""}
          </span>
          <Link
            href="/admin/evolution"
            className="ml-auto shrink-0 text-[11px] font-medium text-primary underline-offset-4 hover:underline"
          >
            查看
          </Link>
        </li>
        <li className="flex items-center gap-2">
          <Gauge className="size-4 text-muted-foreground" aria-hidden />
          <span>
            行程质量分{" "}
            {data.quality.score === null
              ? "—"
              : `${(data.quality.score * 100).toFixed(0)} 分（${data.quality.grade}）`}
            {data.quality.sampled ? "（抽样）" : ""}
          </span>
          <Link
            href="/admin/travel-quality"
            className="ml-auto shrink-0 text-[11px] font-medium text-primary underline-offset-4 hover:underline"
          >
            质量页
          </Link>
        </li>
      </ul>
    </Card>
  );
}

/* ------------------------------ 累计用量（下沉） ------------------------------ */

/**
 * 历史累计量与 Jev 明细。
 *
 * 为什么不放在首屏：`总 Token = 2,000,000`、`总工具调用 = 18,000` 这类数字**没法指导
 * 下一步该修什么**（文档 §3.1）。但它们仍然有用（容量与成本核对），所以下沉到一个
 * 按需加载的折叠区，而不是删掉。
 *
 * 数据来源优先复用外壳 Token 门禁那次 `/admin/overview`（见 admin-shell 的
 * AdminOverviewContext）：同一端点、同一份数据，展开时不再重复发请求。
 * 脱离外壳渲染（拿不到 context）时才退回「展开才自己请求」的老行为。
 */
function LifetimeUsage() {
  const shellOverview = useContext(AdminOverviewContext);
  const [loaded, setLoaded] = useState(false);
  const fallback = useAdminResource<AdminOverview>(
    "admin-overview",
    getAdminOverview,
    loaded && shellOverview === null,
  );
  const overview = shellOverview ?? fallback;

  return (
    <CollapsibleSection
      title="累计用量（历史总量）"
      description="历史累计，不是当前时间窗的指标；复用外壳的 /admin/overview 结果，不额外发请求。"
      onOpenChange={(open) => {
        if (open) setLoaded(true);
      }}
    >
      {!loaded ? (
        <p className="text-xs text-muted-foreground">展开后加载。</p>
      ) : (
        <ResourceView resource={overview} loadingRows={3}>
          {(data) => (
            <div className="flex flex-col gap-3">
              <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 md:grid-cols-4">
                {(
                  [
                    ["运行总数", formatNumber(data.run_count)],
                    ["总 Token", formatNumber(data.total_tokens)],
                    ["LLM 调用", formatNumber(data.total_llm_calls)],
                    ["工具调用", formatNumber(data.total_tool_calls)],
                    ["Provider 失败", formatNumber(data.provider_failures)],
                    ["运行中", formatNumber(data.running_count)],
                    ["降级完成", formatNumber(data.degraded_count)],
                    ["失败", formatNumber(data.failed_count)],
                  ] as const
                ).map(([label, value]) => (
                  <div key={label} className="flex flex-col gap-0.5">
                    <dt className="text-[11px] text-muted-foreground">{label}</dt>
                    <dd className="tabular text-xs font-medium text-foreground">{value}</dd>
                  </div>
                ))}
              </dl>
              <PanelSection
                title="Jev（可选增强）"
                description="文档 §10 已把 Jev 降为可选增强，因此它只在累计用量里出现，不再是首屏核心指标。"
              >
                <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 md:grid-cols-4">
                  {(
                    [
                      ["开关", data.jev.enabled ? "已启用" : "未启用"],
                      ["密钥", data.jev.configured ? "已配置" : "未配置"],
                      ["调用", formatNumber(data.jev.calls)],
                      ["fallback", formatNumber(data.jev.fallback)],
                      ["超时", formatNumber(data.jev.timeout)],
                      ["低置信", formatNumber(data.jev.low_confidence)],
                    ] as const
                  ).map(([label, value]) => (
                    <div key={label} className="flex flex-col gap-0.5">
                      <dt className="text-[11px] text-muted-foreground">{label}</dt>
                      <dd className="text-xs font-medium text-foreground">{value}</dd>
                    </div>
                  ))}
                </dl>
              </PanelSection>
              <p className="text-[11px] text-muted-foreground">
                状态枚举说明：当前累计里出现{" "}
                {Object.keys(data.statuses)
                  .map((status) => statusLabel(status))
                  .join("、") || "无"}。
              </p>
            </div>
          )}
        </ResourceView>
      )}
    </CollapsibleSection>
  );
}
