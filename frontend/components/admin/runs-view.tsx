"use client";

import Link from "next/link";
import { useState } from "react";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FilterSelect, FILTER_ALL, type FilterOption } from "@/components/admin/filter-select";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView } from "@/components/admin/admin-states";
import { StatusBadge } from "@/components/admin/status-badge";
import {
  formatCost,
  formatDurationMs,
  formatNumber,
  formatTokens,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminRuns } from "@/lib/admin-api";
import type { AdminRunList, AdminRunSummary } from "@/types/admin";

/** 契约里没写 run 状态枚举，这里给已知集合，运行时再合并实际出现的状态。 */
const KNOWN_STATUSES = ["RUNNING", "SUCCESS", "DEGRADED", "FAILED", "CANCELLED"];

const LIMIT_OPTIONS: FilterOption[] = [
  { value: "20", label: "每页 20 条" },
  { value: "50", label: "每页 50 条" },
  { value: "100", label: "每页 100 条" },
];

export function RunsView() {
  const [status, setStatus] = useState<string>(FILTER_ALL);
  const [limit, setLimit] = useState<number>(20);
  const [offset, setOffset] = useState<number>(0);

  const queryStatus = status === FILTER_ALL ? undefined : status;
  const resource = useAdminResource<AdminRunList>(
    `admin-runs:${queryStatus ?? "all"}:${limit}:${offset}`,
    () => getAdminRuns({ limit, offset, status: queryStatus }),
  );

  const items = resource.data?.items ?? [];
  const statusOptions = buildStatusOptions(items);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="运行记录"
        description="每一次规划运行的用量、耗时与问题数量。点 Run 编号进入详情，可以看 Trace、阶段与决策链。"
        actions={
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:max-w-xl">
        <FilterSelect
          label="状态"
          value={status}
          options={statusOptions}
          onChange={(next) => {
            setStatus(next);
            setOffset(0);
          }}
        />
        <FilterSelect
          label="每页数量"
          value={String(limit)}
          options={LIMIT_OPTIONS}
          onChange={(next) => {
            const parsed = Number.parseInt(next, 10);
            setLimit(Number.isFinite(parsed) && parsed > 0 ? parsed : 20);
            setOffset(0);
          }}
        />
      </div>

      <ResourceView
        resource={resource}
        loadingRows={5}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="没有符合条件的运行记录"
        emptyDescription="当前筛选条件下后端返回了空集合。可以切换状态，或把筛选恢复成「全部」。"
      >
        {(data) => (
          <div className="flex flex-col gap-3">
            <Pagination
              total={data.total}
              limit={limit}
              offset={data.offset}
              count={data.items.length}
              onPrevious={() => setOffset((value) => Math.max(0, value - limit))}
              onNext={() => setOffset((value) => value + limit)}
            />
            <AdminTable
              columns={RUN_COLUMNS}
              rows={data.items}
              getRowKey={(run) => run.run_id}
            />
            <Pagination
              total={data.total}
              limit={limit}
              offset={data.offset}
              count={data.items.length}
              onPrevious={() => setOffset((value) => Math.max(0, value - limit))}
              onNext={() => setOffset((value) => value + limit)}
            />
          </div>
        )}
      </ResourceView>
    </div>
  );
}

function buildStatusOptions(items: AdminRunSummary[]): FilterOption[] {
  const seen = new Set<string>();
  for (const item of items) seen.add(item.status);
  const extra = [...seen].filter((value) => !KNOWN_STATUSES.includes(value));
  return [
    { value: FILTER_ALL, label: "全部状态" },
    ...KNOWN_STATUSES.map((value) => ({ value, label: statusLabel(value) })),
    ...extra.map((value) => ({ value, label: statusLabel(value) })),
  ];
}

const RUN_COLUMNS: AdminColumn<AdminRunSummary>[] = [
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
    key: "query",
    header: "原始请求",
    mobileHidden: true,
    cell: (run) => (
      <span className="line-clamp-2 block max-w-[24rem] text-xs text-muted-foreground">
        {run.original_query}
      </span>
    ),
  },
  {
    key: "created_at",
    header: "开始时间",
    mobileHidden: true,
    cell: (run) => (
      <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
        {run.created_at ?? "—"}
      </span>
    ),
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (run) => <span className="tabular text-xs">{formatDurationMs(run.duration_ms)}</span>,
  },
  {
    key: "tokens",
    header: "Token",
    align: "right",
    cell: (run) => <span className="tabular text-xs">{formatTokens(run.total_tokens)}</span>,
  },
  {
    key: "calls",
    header: "LLM / Jev / 工具",
    align: "right",
    cell: (run) => (
      <span className="tabular text-xs whitespace-nowrap">
        {formatNumber(run.llm_calls)} / {formatNumber(run.jev_calls)} / {formatNumber(run.tool_calls)}
      </span>
    ),
  },
  {
    key: "provider_failures",
    header: "Provider 失败",
    align: "right",
    mobileHidden: true,
    cell: (run) => <span className="tabular text-xs">{formatNumber(run.provider_failures)}</span>,
  },
  {
    key: "badcase_count",
    header: "Bad Case",
    align: "right",
    cell: (run) => <span className="tabular text-xs">{formatNumber(run.badcase_count)}</span>,
  },
  {
    key: "cost",
    header: "成本",
    align: "right",
    mobileHidden: true,
    cell: (run) => <span className="tabular text-xs">{formatCost(run.cost)}</span>,
  },
];

function Pagination({
  total,
  limit,
  offset,
  count,
  onPrevious,
  onNext,
}: {
  total: number;
  limit: number;
  offset: number;
  count: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const from = count === 0 ? 0 : offset + 1;
  const to = offset + count;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span className="tabular">
        第 {formatNumber(from)}–{formatNumber(to)} 条 / 共 {formatNumber(total)} 条
      </span>
      <div className="flex items-center gap-1.5">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={onPrevious}>
          <ChevronLeft />
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={offset + limit >= total || count === 0}
          onClick={onNext}
        >
          下一页
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}
