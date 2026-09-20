"use client";

import Link from "next/link";
import {
  Activity,
  Bug,
  Coins,
  Cpu,
  FlaskConical,
  Gauge,
  RefreshCw,
  Sparkles,
  Wrench,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { BooleanBadge, StatusBadge } from "@/components/admin/status-badge";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { StatCard } from "@/components/admin/stat-card";
import {
  formatCost,
  formatDurationMs,
  formatNumber,
  formatPassRate,
  formatMetricValue,
  formatTokens,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { describeAdminError, getAdminOverview, getAdminRuns } from "@/lib/admin-api";
import type { AdminOverview, AdminRunList, AdminRunSummary } from "@/types/admin";

/**
 * 仪表盘：默认只放汇总卡。
 * 逐条运行的用量明细放在「详细用量明细」里，且首屏折叠 ——
 * 操作员打开这一页通常只想确认"现在有没有出问题"，不需要 50 行明细。
 */
export function DashboardView() {
  const overview = useAdminResource<AdminOverview>("admin-overview", getAdminOverview);
  const runs = useAdminResource<AdminRunList>("admin-overview-usage", () =>
    getAdminRuns({ limit: 50, offset: 0 }),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="仪表盘"
        description="运行总量、调用用量与质量闭环的当前状态。所有数字都来自后端聚合，管理台不做二次计算。"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              overview.reload();
              runs.reload();
            }}
          >
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <ResourceView resource={overview} loadingRows={5}>
        {(data) => <OverviewBody data={data} />}
      </ResourceView>

      <CollapsibleSection
        title="详细用量明细"
        description="按运行列出 Token、LLM / Jev / 工具调用次数"
        count={runs.data ? runs.data.total : undefined}
        icon={Coins}
      >
        <ResourceView
          resource={runs}
          loadingRows={4}
          isEmpty={(data) => data.items.length === 0}
          emptyTitle="还没有任何运行记录"
          emptyDescription="后端 runs 存储为空。发起一次规划后，这里会出现逐条用量。"
        >
          {(data) => <UsageTable data={data} />}
        </ResourceView>
      </CollapsibleSection>

      {overview.error && overview.data ? (
        <p className="text-[11px] text-muted-foreground">
          上面的数字是上一次成功读取的结果，最近一次刷新失败（{describeAdminError(overview.error)}）
        </p>
      ) : null}
    </div>
  );
}

function OverviewBody({ data }: { data: AdminOverview }) {
  const statusEntries = Object.entries(data.statuses);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatCard label="运行总数" value={formatNumber(data.run_count)} icon={Activity} />
        <StatCard
          label="运行中"
          value={formatNumber(data.running_count)}
          tone="info"
          hint="仍在执行的运行"
        />
        <StatCard
          label="降级完成"
          value={formatNumber(data.degraded_count)}
          tone="warning"
          hint="有数据源失败但仍产出行程"
        />
        <StatCard
          label="失败"
          value={formatNumber(data.failed_count)}
          tone={data.failed_count > 0 ? "danger" : "muted"}
          hint="没有产出可用行程"
        />
        <StatCard label="总 Token" value={formatTokens(data.total_tokens)} icon={Coins} />
        <StatCard label="LLM 调用" value={formatNumber(data.total_llm_calls)} icon={Cpu} />
        <StatCard label="Jev 调用" value={formatNumber(data.total_jev_calls)} icon={Sparkles} />
        <StatCard label="工具调用" value={formatNumber(data.total_tool_calls)} icon={Wrench} />
        <StatCard
          label="Provider 失败"
          value={formatNumber(data.provider_failures)}
          tone={data.provider_failures > 0 ? "warning" : "muted"}
          hint="外部数据源返回失败"
        />
        <StatCard
          label="未关闭 Bad Case"
          value={`${formatNumber(data.badcase_open)} / ${formatNumber(data.badcase_total)}`}
          tone={data.badcase_open > 0 ? "warning" : "success"}
          icon={Bug}
          hint="未关闭 / 总数"
        />
        <StatCard
          label="最新 Benchmark 通过率"
          value={formatPassRate(data.benchmark.latest_pass_rate)}
          icon={FlaskConical}
          hint={
            data.benchmark.latest_run_id
              ? `最近一次：${data.benchmark.latest_run_id}`
              : "还没有跑过 Benchmark"
          }
        />
        <StatCard
          label="Evolution 待处理"
          value={formatNumber(data.evolution.pending_badcases)}
          tone={data.evolution.pending_badcases > 0 ? "warning" : "muted"}
          icon={Gauge}
          hint={data.evolution.enabled ? "Evolution 已启用" : "Evolution 未启用"}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>运行状态分布</CardTitle>
            <CardDescription>后端 run 状态枚举的原始计数，未做归并。</CardDescription>
          </CardHeader>
          <CardContent>
            {statusEntries.length === 0 ? (
              <p className="text-xs text-muted-foreground">后端没有返回任何状态计数。</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {statusEntries.map(([status, count]) => (
                  <li key={status} className="flex items-center justify-between gap-2">
                    <StatusBadge status={status} />
                    <span className="tabular text-xs font-medium text-foreground">
                      {formatMetricValue(count)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Jev 决策健康</CardTitle>
            <CardDescription>Jev 是大模型决策层的降级开关，异常会直接体现为 fallback。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground">启用</span>
              <BooleanBadge value={data.jev.enabled} onLabel="已启用" offLabel="未启用" />
              <span className="text-xs text-muted-foreground">配置</span>
              <BooleanBadge
                value={data.jev.configured}
                onLabel="已配置密钥"
                offLabel="缺少密钥"
                onTone="success"
                offTone="warning"
              />
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
              <Metric label="调用" value={formatNumber(data.jev.calls)} />
              <Metric label="fallback" value={formatNumber(data.jev.fallback)} />
              <Metric label="超时" value={formatNumber(data.jev.timeout)} />
              <Metric label="低置信度" value={formatNumber(data.jev.low_confidence)} />
            </dl>
            <Link
              href="/admin/config"
              className="text-xs text-primary underline-offset-4 hover:underline"
            >
              查看 Jev 实时健康与系统配置
            </Link>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>最近一次 Benchmark</CardTitle>
            <CardDescription>固定用例集上的通过情况，是「改动有没有变坏」的基线。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-xs">
            {data.benchmark.latest_run_id ? (
              <>
                <p className="font-mono break-all text-foreground">{data.benchmark.latest_run_id}</p>
                <p className="text-muted-foreground">
                  覆盖 suite：
                  {data.benchmark.latest_suites.length > 0
                    ? data.benchmark.latest_suites.join("、")
                    : "未记录"}
                </p>
                <p className="text-muted-foreground">
                  通过率：<span className="tabular font-medium text-foreground">{formatPassRate(data.benchmark.latest_pass_rate)}</span>
                </p>
                <Link
                  href={`/admin/benchmark/${encodeURIComponent(data.benchmark.latest_run_id)}`}
                  className="text-primary underline-offset-4 hover:underline"
                >
                  打开这次 Benchmark 详情
                </Link>
              </>
            ) : (
              <SectionEmpty
                title="还没有 Benchmark 记录"
                description="去 Benchmark 页面选择 suite 跑一次，就能得到通过率基线。"
                hint="通过率是质量回归的第一道防线，建议在每次改动后运行。"
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Evolution 状态</CardTitle>
            <CardDescription>把 Bad Case 沉淀成可验证的经验，并决定采纳 / 拒绝 / 回滚。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-muted-foreground">开关</span>
              <BooleanBadge value={data.evolution.enabled} onLabel="已启用" offLabel="未启用" />
            </div>
            <p className="text-muted-foreground">
              待处理 Bad Case：
              <span className="tabular font-medium text-foreground">
                {formatNumber(data.evolution.pending_badcases)}
              </span>
            </p>
            <p className="text-muted-foreground">
              最近一次运行：{data.evolution.last_run_id ?? "还没有运行过"}
            </p>
            <p className="text-muted-foreground">
              最近决定：{data.evolution.last_decision ? statusLabel(data.evolution.last_decision) : "—"}
            </p>
            <Link
              href="/admin/evolution"
              className="text-primary underline-offset-4 hover:underline"
            >
              打开 Evolution 页面
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular font-medium text-foreground">{value}</dd>
    </div>
  );
}

function UsageTable({ data }: { data: AdminRunList }) {
  const columns: AdminColumn<AdminRunSummary>[] = [
    {
      key: "run_id",
      header: "Run",
      primary: true,
      cell: (run) => (
        <Link
          href={`/admin/runs/${encodeURIComponent(run.run_id)}`}
          className="font-mono text-xs text-primary underline-offset-4 hover:underline"
        >
          {run.run_id}
        </Link>
      ),
    },
    { key: "status", header: "状态", cell: (run) => <StatusBadge status={run.status} /> },
    {
      key: "tokens",
      header: "Token",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatTokens(run.total_tokens)}</span>,
    },
    {
      key: "llm",
      header: "LLM",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.llm_calls)}</span>,
    },
    {
      key: "jev",
      header: "Jev",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.jev_calls)}</span>,
    },
    {
      key: "tool",
      header: "工具",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.tool_calls)}</span>,
    },
    {
      key: "provider",
      header: "Provider 失败",
      align: "right",
      mobileHidden: true,
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.provider_failures)}</span>,
    },
    {
      key: "badcase",
      header: "Bad Case",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.badcase_count)}</span>,
    },
    {
      key: "duration",
      header: "耗时",
      align: "right",
      mobileHidden: true,
      cell: (run) => (
        <span className="tabular text-xs">{formatDurationMs(run.duration_ms)}</span>
      ),
    },
    {
      key: "cost",
      header: "成本",
      align: "right",
      mobileHidden: true,
      cell: (run) => <span className="tabular text-xs">{formatCost(run.cost)}</span>,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[11px] text-muted-foreground">
        展示最近 {data.items.length} 条（共 {formatNumber(data.total)} 条）。完整分页见「运行记录」。
      </p>
      <AdminTable columns={columns} rows={data.items} getRowKey={(run) => run.run_id} />
    </div>
  );
}
