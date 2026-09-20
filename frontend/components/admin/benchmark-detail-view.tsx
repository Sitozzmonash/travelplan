"use client";

import Link from "next/link";
import { ArrowLeft, FlaskConical, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PageHeader } from "@/components/admin/page-header";
import { PartialBanner, ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import { BooleanBadge, StatusBadge } from "@/components/admin/status-badge";
import { formatMetricKey, formatMetricValue, formatNumber } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminBenchmarkRun } from "@/lib/admin-api";
import {
  BENCHMARK_DIMENSIONS,
  BENCHMARK_SUITES,
  type AdminBenchmarkBaseline,
  type AdminBenchmarkCaseResult,
  type AdminBenchmarkDetail,
  type BenchmarkDimensionKey,
} from "@/types/admin";

/**
 * Benchmark 详情。
 *
 * 三条硬性展示规则：
 * 1) 六个维度分开展示，不给"总分" —— 聚合分会掩盖掉具体哪一维退化。
 * 2) 六个维度都是固定文案，不因为后端返回空就少渲染一节（空就是"没有指标"）。
 * 3) 用例按 suite 分组，每组给出通过 / 失败数量。
 */
export function BenchmarkDetailView({ benchmarkRunId }: { benchmarkRunId: string }) {
  const resource = useAdminResource<AdminBenchmarkDetail>(
    `admin-benchmark:${benchmarkRunId}`,
    () => getAdminBenchmarkRun(benchmarkRunId),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Benchmark 详情"
        description="六个评测维度、按 suite 分组的用例结果，以及 Jev OFF / ON 的基线对比（如果这次运行有对照）。"
        badge={
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] break-all text-muted-foreground">
            {benchmarkRunId}
          </span>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <Button variant="outline" size="sm" onClick={resource.reload}>
              <RefreshCw />
              刷新
            </Button>
            <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/admin/benchmark" />}>
              <ArrowLeft />
              返回列表
            </Button>
          </div>
        }
      />

      <ResourceView resource={resource} loadingRows={6}>
        {(data) => <BenchmarkDetailBody data={data} />}
      </ResourceView>
    </div>
  );
}

function BenchmarkDetailBody({ data }: { data: AdminBenchmarkDetail }) {
  const { run } = data;
  const missingDimensions = BENCHMARK_DIMENSIONS.filter((dimension) => {
    const value = data.metrics[dimension.key as BenchmarkDimensionKey];
    return value === null || value === undefined;
  });
  const suites = groupBySuite(data.case_results);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <span>运行信息</span>
            <StatusBadge status={run.status} />
            <BooleanBadge
              value={run.jev_enabled}
              onLabel="Jev ON"
              offLabel="Jev OFF"
              onTone="info"
            />
          </CardTitle>
          <CardDescription>
            评测基线要连同版本、模型与 fixture 版本一起读，否则无法复现。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DescriptionList
            items={[
              { label: "Suite", value: run.suite || "全部 suite" },
              { label: "版本", value: run.version || "—" },
              { label: "模型", value: run.model ?? "—" },
              { label: "TravelPlan commit", value: run.travelplan_commit ?? "—" },
              { label: "SuperHarness commit", value: run.superharness_commit ?? "—" },
              { label: "Fixture 版本", value: run.fixture_version ?? "—" },
              { label: "开始", value: run.started_at ?? "—" },
              { label: "结束", value: run.finished_at ?? "—" },
              {
                label: "用例",
                value: `共 ${formatNumber(run.case_count)}，通过 ${formatNumber(run.passed)}，失败 ${formatNumber(run.failed)}`,
              },
              { label: "备注", value: run.notes ?? "—" },
            ]}
          />
        </CardContent>
      </Card>

      {missingDimensions.length > 0 ? (
        <PartialBanner
          title="部分维度没有指标"
          description={`后端这次只返回了部分维度，缺少：${missingDimensions
            .map((dimension) => dimension.label)
            .join("、")}。其余维度仍然可用。`}
        />
      ) : null}

      {BENCHMARK_DIMENSIONS.map((dimension) => (
        <CollapsibleSection
          key={dimension.key}
          title={dimension.label}
          description={dimension.description}
          icon={FlaskConical}
          defaultOpen
        >
          <MetricList
            metrics={data.metrics[dimension.key as BenchmarkDimensionKey]}
            emptyText={`后端没有返回「${dimension.label}」维度的指标。这一节保留是为了让缺失可见，而不是被静默省略。`}
          />
        </CollapsibleSection>
      ))}

      <CollapsibleSection
        title="用例结果"
        description="按 suite 分组，逐条给出通过 / 不通过"
        count={data.case_results.length}
        defaultOpen
      >
        {suites.length === 0 ? (
          <SectionEmpty
            title="没有用例结果"
            description={
              run.case_count && run.case_count > 0
                ? `运行信息说这次有 ${run.case_count} 条用例，但后端没有返回 case_results，因此无法逐条核对。`
                : "这次运行没有产生用例结果。"
            }
          />
        ) : (
          <div className="flex flex-col gap-3">
            {suites.map((group) => {
              const passed = group.cases.filter((item) => item.status === "pass").length;
              return (
                <CollapsibleSection
                  key={group.suite}
                  title={group.label}
                  description={`通过 ${passed} / ${group.cases.length}`}
                  count={group.cases.length}
                  defaultOpen
                >
                  <ul className="flex flex-col gap-2.5">
                    {group.cases.map((result) => (
                      <CaseResultCard key={`${result.suite}-${result.case_id}`} result={result} />
                    ))}
                  </ul>
                </CollapsibleSection>
              );
            })}
          </div>
        )}
      </CollapsibleSection>

      <CollapsibleSection
        title="Jev OFF / ON 基线对比"
        description="同一批用例在 Jev 关闭与开启时的指标差异"
        icon={FlaskConical}
        defaultOpen={Boolean(data.baseline_compare)}
      >
        {data.baseline_compare ? (
          <BaselineCompare baseline={data.baseline_compare} />
        ) : (
          <SectionEmpty
            title="这次运行没有基线对照"
            description="后端没有返回 baseline_compare。要得到对照，需要在 Jev 关闭与开启两种配置下各跑一次同一批用例。"
          />
        )}
      </CollapsibleSection>

      <CollapsibleSection
        title="原始运行指标"
        description="run.metrics 的原始键值（未分组）"
        count={Object.keys(run.metrics).length}
      >
        <MetricList metrics={run.metrics} />
      </CollapsibleSection>
    </div>
  );
}

function CaseResultCard({ result }: { result: AdminBenchmarkCaseResult }) {
  const metricsCount = Object.keys(result.metrics).length;
  const detailCount = Object.keys(result.detail).length;
  return (
    <li className="rounded-lg border border-border p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] break-all text-muted-foreground">{result.case_id}</span>
        <span className="text-xs font-medium text-foreground">{result.title || "未命名用例"}</span>
        <StatusBadge status={result.status} />
      </div>
      {metricsCount > 0 ? <MetricList metrics={result.metrics} className="mt-2" /> : null}
      {detailCount > 0 ? (
        <details className="group/detail mt-2">
          <summary className="cursor-pointer list-none text-[11px] text-primary underline-offset-4 hover:underline [&::-webkit-details-marker]:hidden">
            展开用例细节
          </summary>
          <MetricList metrics={result.detail} className="mt-1.5" />
        </details>
      ) : null}
    </li>
  );
}

interface BaselineRow {
  key: string;
  off: unknown;
  on: unknown;
  delta: unknown;
}

function buildBaselineRows(baseline: AdminBenchmarkBaseline): BaselineRow[] {
  const keys = new Set<string>();
  for (const key of Object.keys(baseline.off)) keys.add(key);
  for (const key of Object.keys(baseline.on)) keys.add(key);
  for (const key of Object.keys(baseline.delta_pct)) keys.add(key);
  return [...keys].map((key) => ({
    key,
    off: baseline.off[key],
    on: baseline.on[key],
    delta: baseline.delta_pct[key],
  }));
}

const BASELINE_COLUMNS: AdminColumn<BaselineRow>[] = [
  {
    key: "metric",
    header: "指标",
    primary: true,
    cell: (row) => <span className="text-xs text-foreground">{formatMetricKey(row.key)}</span>,
  },
  {
    key: "off",
    header: "Jev OFF",
    align: "right",
    cell: (row) => <span className="tabular text-xs">{formatMetricValue(row.off)}</span>,
  },
  {
    key: "on",
    header: "Jev ON",
    align: "right",
    cell: (row) => <span className="tabular text-xs">{formatMetricValue(row.on)}</span>,
  },
  {
    key: "delta",
    header: "变化",
    align: "right",
    cell: (row) => <span className="tabular text-xs">{formatMetricValue(row.delta)}</span>,
  },
];

function BaselineCompare({ baseline }: { baseline: AdminBenchmarkBaseline }) {
  const rows = buildBaselineRows(baseline);
  if (rows.length === 0) {
    return (
      <SectionEmpty
        title="基线对比为空"
        description="后端返回了 baseline_compare，但三个分组都是空对象，因此没有可对比的指标。"
      />
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <AdminTable columns={BASELINE_COLUMNS} rows={rows} getRowKey={(row) => row.key} />
      <p className="text-[11px] leading-4 text-muted-foreground">
        变化值直接来自后端的 delta_pct，管理台不做方向判断：耗时与错误的上升是坏消息，通过率的上升是好消息，
        两者都会显示成同一个正数。
      </p>
    </div>
  );
}

interface SuiteGroup {
  suite: string;
  label: string;
  cases: AdminBenchmarkCaseResult[];
}

/** 按契约固定的 suite 顺序分组；未登记的新 suite 排在最后，不会被丢掉。 */
function groupBySuite(cases: AdminBenchmarkCaseResult[]): SuiteGroup[] {
  const labels: Record<string, string> = {
    basic: "基础用例",
    hard: "困难用例",
    badcase_regression: "Bad Case 回归",
    provider_failure: "Provider 故障",
    jev_decision: "Jev 决策",
    live_smoke: "线上冒烟",
  };
  const order: string[] = [...BENCHMARK_SUITES];
  const buckets = new Map<string, AdminBenchmarkCaseResult[]>();
  for (const item of cases) {
    const suite = item.suite || "未分组";
    const bucket = buckets.get(suite);
    if (bucket) bucket.push(item);
    else buckets.set(suite, [item]);
    if (!order.includes(suite)) order.push(suite);
  }
  return order
    .filter((suite) => buckets.has(suite))
    .map((suite) => ({
      suite,
      label: labels[suite] ?? suite,
      cases: buckets.get(suite) ?? [],
    }));
}
