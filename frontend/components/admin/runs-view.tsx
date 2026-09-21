"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FilterSelect, FILTER_ALL, type FilterOption } from "@/components/admin/filter-select";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView } from "@/components/admin/admin-states";
import { SourceTag, StatusBadge, ToneBadge, type AdminTone } from "@/components/admin/status-badge";
import {
  formatCost,
  formatDurationMs,
  formatNumber,
  formatRatio,
  statusLabel,
} from "@/components/admin/format";
import { formatStamp } from "@/lib/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminRuns } from "@/lib/admin-api";
import { ADMIN_GRADE_LABELS, type AdminRunList, type AdminRunSummary } from "@/types/admin";

/** 契约里没写 run 状态枚举，这里给已知集合，运行时再合并实际出现的状态。 */
const KNOWN_STATUSES = ["RUNNING", "SUCCESS", "DEGRADED", "FAILED", "CANCELLED"];

const LIMIT_OPTIONS: FilterOption[] = [
  { value: "20", label: "每页 20 条" },
  { value: "50", label: "每页 50 条" },
  { value: "100", label: "每页 100 条" },
];

/** 质量等级 → 色调：与后端 `_grade()` 的四档一一对应，UNKNOWN 保持中性灰。 */
const GRADE_TONES: Record<string, AdminTone> = {
  GOOD: "success",
  FAIR: "warning",
  POOR: "danger",
  UNKNOWN: "muted",
};

/** 搜索防抖：run_id 是渐进的输入，每敲一个字都请求会把后端打满。 */
const SEARCH_DEBOUNCE_MS = 350;

export function RunsView() {
  // 首屏筛选支持深链：Dashboard 的「当前需要关注」跳到 /admin/runs?status=FAILED（或 ?q=…）。
  // 用惰性初始化读 window 而不是 useSearchParams（静态导出需要 Suspense 边界），
  // 也不用 effect + setState（ESLint react-hooks/set-state-in-effect 视为错误）；
  // 管理台外壳在 hydration 前只渲染骨架，因此这里首次求值时 window 一定可用。
  const [status, setStatus] = useState<string>(() => readInitialParam("status") ?? FILTER_ALL);
  const [limit, setLimit] = useState<number>(20);
  const [offset, setOffset] = useState<number>(0);
  const [queryDraft, setQueryDraft] = useState(() => readInitialParam("q") ?? "");
  const [includeBenchmark, setIncludeBenchmark] = useState(false);
  const q = useDebouncedValue(queryDraft.trim(), SEARCH_DEBOUNCE_MS);

  const queryStatus = status === FILTER_ALL ? undefined : status;
  const resource = useAdminResource<AdminRunList>(
    `admin-runs:${queryStatus ?? "all"}:${q}:${includeBenchmark}:${limit}:${offset}`,
    () => getAdminRuns({ limit, offset, status: queryStatus, q: q || undefined, includeBenchmark }),
  );

  const items = resource.data?.items ?? [];
  const statusOptions = buildStatusOptions(items, status);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="运行记录"
        description="每一次运行的行程质量与问题数量（Token / LLM / 工具等指标在运行详情里）。可以按 run_id 搜索；Benchmark 用例运行默认不显示。点运行编号进入详情。"
        actions={
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <div className="flex flex-col gap-3 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
        <div className="grid gap-3 sm:grid-cols-2 lg:max-w-2xl">
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

        <div className="grid gap-3 sm:grid-cols-2 lg:max-w-2xl">
          <div className="flex min-w-0 flex-col gap-1">
            <label className="text-[11px] text-muted-foreground" htmlFor="runs-search">
              Run 编号搜索
            </label>
            <div className="flex items-center gap-1.5">
              <Input
                id="runs-search"
                value={queryDraft}
                placeholder="输入完整或部分 run_id"
                spellCheck={false}
                onChange={(event) => {
                  setQueryDraft(event.target.value);
                  setOffset(0);
                }}
              />
              {queryDraft ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="清空搜索"
                  onClick={() => {
                    setQueryDraft("");
                    setOffset(0);
                  }}
                >
                  <X />
                </Button>
              ) : (
                <span className="flex size-7 items-center justify-center text-muted-foreground" aria-hidden>
                  <Search className="size-3.5" />
                </span>
              )}
            </div>
          </div>

          <label className="flex min-w-0 items-center gap-2 self-end pb-1.5 text-xs text-foreground">
            <input
              type="checkbox"
              className="size-4 accent-primary"
              checked={includeBenchmark}
              onChange={(event) => {
                setIncludeBenchmark(event.target.checked);
                setOffset(0);
              }}
            />
            <span>包含 Benchmark 用例运行</span>
            <span className="text-[11px] text-muted-foreground">
              （默认关闭，避免它们淹没有效运行）
            </span>
          </label>
        </div>
      </div>

      <ResourceView
        resource={resource}
        loadingRows={5}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="没有符合条件的运行记录"
        emptyDescription={
          q
            ? `没有匹配「${q}」的 run_id。可以换一个片段，或者检查是否把 Benchmark 用例排除在外了。`
            : "当前筛选条件下后端返回了空集合。可以切换状态、清空搜索，或勾选「包含 Benchmark 用例运行」。"
        }
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
            <p className="text-[11px] text-muted-foreground">
              技术指标（Token / LLM / 工具 / Provider 失败）已下沉到运行详情，列表只保留产品质量视角的 8 列。
            </p>
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

/** 读 URL 查询串里的首个筛选值；没有（或空串）时返回 null。 */
function readInitialParam(key: string): string | null {
  if (typeof window === "undefined") return null;
  const value = new URLSearchParams(window.location.search).get(key);
  return value && value.length > 0 ? value : null;
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/**
 * 状态下拉：已知枚举 + 本页出现的状态 + 深链带来的状态。
 * 深链（?status=FAILED）必须出现在选项里，否则下拉会显示成「全部状态」而列表其实在筛选。
 */
function buildStatusOptions(items: AdminRunSummary[], current: string): FilterOption[] {
  const seen = new Set<string>(KNOWN_STATUSES);
  for (const item of items) seen.add(item.status);
  if (current !== FILTER_ALL) seen.add(current);
  return [
    { value: FILTER_ALL, label: "全部状态" },
    ...[...seen].map((value) => ({ value, label: statusLabel(value) })),
  ];
}

/** 质量：拿到分数才显示百分比；没有分数时把后端给的原因放进 title，绝不把 null 画成 0。 */
function QualityCell({ run }: { run: AdminRunSummary }) {
  if (run.quality_score === null || run.quality_score === undefined) {
    return (
      <span
        className="text-xs text-muted-foreground"
        title={run.quality_error ?? "后端没有返回质量分"}
      >
        —
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="tabular text-xs">{formatRatio(run.quality_score)}</span>
      {run.quality_grade ? (
        <ToneBadge tone={GRADE_TONES[run.quality_grade] ?? "muted"} className="text-[0.625rem]">
          {ADMIN_GRADE_LABELS[run.quality_grade] ?? run.quality_grade}
        </ToneBadge>
      ) : null}
    </span>
  );
}

/** 问题：Bad Case 与未解决告警合并成一列；两者都为 0 / 缺失时写「无」。 */
function IssueCell({ run }: { run: AdminRunSummary }) {
  const cases = run.issue_count ?? 0;
  const warnings = run.warnings ?? 0;
  if (cases === 0 && warnings === 0) {
    return <span className="text-xs text-muted-foreground">无</span>;
  }
  return (
    <Link
      href={`/admin/runs/${encodeURIComponent(run.run_id)}`}
      title="打开运行详情查看这些问题"
      className="tabular text-xs whitespace-nowrap text-primary underline-offset-4 hover:underline"
    >
      {formatNumber(cases)} 案例 · {formatNumber(warnings)} 告警
    </Link>
  );
}

const RUN_COLUMNS: AdminColumn<AdminRunSummary>[] = [
  {
    key: "status",
    header: "状态",
    primary: true,
    cell: (run) => (
      <span className="flex flex-col gap-1">
        <StatusBadge status={run.status} />
        {/* Run 编号列已从列表移除：编号跟着状态放在首列，保证仍然点得进运行详情。 */}
        <Link
          href={`/admin/runs/${encodeURIComponent(run.run_id)}`}
          title={run.run_id}
          className="block max-w-[12rem] truncate font-mono text-[11px] text-primary underline-offset-4 hover:underline"
        >
          {run.run_id}
        </Link>
      </span>
    ),
  },
  {
    key: "query",
    header: "用户请求 · 目的地",
    className: "max-w-[24rem]",
    cell: (run) => (
      <span className="flex flex-col gap-1">
        {run.destination ? (
          <span className="flex flex-wrap items-center gap-1.5">
            <ToneBadge tone="info" className="text-[0.625rem]">
              {run.destination}
            </ToneBadge>
          </span>
        ) : null}
        <span className="line-clamp-2 text-xs leading-5 text-muted-foreground">
          {run.original_query || "后端没有记录原始请求文本"}
        </span>
      </span>
    ),
  },
  { key: "source", header: "来源", cell: (run) => <SourceTag source={run.source} /> },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (run) => <span className="tabular text-xs">{formatDurationMs(run.duration_ms)}</span>,
  },
  {
    key: "quality",
    header: "质量",
    align: "right",
    cell: (run) => <QualityCell run={run} />,
  },
  {
    key: "issues",
    header: "问题",
    align: "right",
    cell: (run) => <IssueCell run={run} />,
  },
  {
    key: "cost",
    header: "成本",
    align: "right",
    mobileHidden: true,
    cell: (run) => <span className="tabular text-xs">{formatCost(run.cost)}</span>,
  },
  {
    key: "started_at",
    header: "开始时间",
    mobileHidden: true,
    cell: (run) => (
      <span
        className="font-mono text-[11px] whitespace-nowrap text-muted-foreground"
        title={run.started_at ?? run.created_at ?? undefined}
      >
        {formatStamp(run.started_at ?? run.created_at)}
      </span>
    ),
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
