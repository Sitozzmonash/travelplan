"use client";

import Link from "next/link";
import { useState } from "react";
import {
  ArrowRight,
  Bug,
  ChevronLeft,
  ChevronRight,
  Filter,
  Layers,
  List,
  Minus,
  RefreshCw,
  Save,
  Search,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import {
  FILTER_ALL,
  FilterSelect,
  mergeFacetOptions,
  type FilterOption,
} from "@/components/admin/filter-select";
import { PageHeader } from "@/components/admin/page-header";
import { ActionError, ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { AdminDetailDialog } from "@/components/admin/detail-dialog";
import { DescriptionList, PanelSection } from "@/components/admin/metric-list";
import { StatCard } from "@/components/admin/stat-card";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { Sparkline } from "@/components/admin/trend-sparkline";
import { WindowSelect, windowLabel } from "@/components/admin/window-select";
import { badcaseCategoryLabel, cleanBadcaseSymptom, formatNumber, statusLabel } from "@/components/admin/format";
import { useAdminResource, type AdminResource } from "@/components/admin/use-admin-resource";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";
import {
  getAdminBadcaseGroups,
  getAdminBadcases,
  patchAdminBadcase,
  toAdminApiError,
  type AdminApiError,
} from "@/lib/admin-api";
import {
  BADCASE_ANALYSIS_STATUSES,
  BADCASE_FIXED_STATUSES,
  BADCASE_ROOT_CAUSE_STATUSES,
  type AdminBadcase,
  type AdminBadcaseGroup,
  type AdminBadcaseGroups,
  type AdminBadcaseList,
  type AdminBadcasePatch,
  type AdminWindowKey,
  type BadcaseAnalysisStatus,
  type BadcaseFixedStatus,
  type BadcaseRootCauseStatus,
} from "@/types/admin";

const ANALYSIS_FALLBACK: FilterOption[] = BADCASE_ANALYSIS_STATUSES.map((value) => ({
  value,
  label: statusLabel(value),
}));
const FIXED_FALLBACK: FilterOption[] = BADCASE_FIXED_STATUSES.map((value) => ({
  value,
  label: statusLabel(value),
}));
const ROOT_CAUSE_FALLBACK: FilterOption[] = BADCASE_ROOT_CAUSE_STATUSES.map((value) => ({
  value,
  label: statusLabel(value),
}));

/** 案例列表固定每页 20 条：这一页的筛选维度已经够多，再加重置分页的控件只会增加噪声。 */
const PAGE_SIZE = 20;

/** 聚类卡片最多列几个样例运行，其余只说个数，避免一张卡片被 run_id 撑满。 */
const SAMPLE_RUN_LIMIT = 4;

/** 聚类窗口默认 7d：Bad Case 的产生频率远低于 run，24 小时常常聚不出有意义的趋势。 */
const DEFAULT_WINDOW: AdminWindowKey = "7d";

type BadcaseViewKey = "cluster" | "list";

function isAnalysisStatus(value: string): value is BadcaseAnalysisStatus {
  return (BADCASE_ANALYSIS_STATUSES as readonly string[]).includes(value);
}

function isFixedStatus(value: string): value is BadcaseFixedStatus {
  return (BADCASE_FIXED_STATUSES as readonly string[]).includes(value);
}

function isRootCauseStatus(value: string): value is BadcaseRootCauseStatus {
  return (BADCASE_ROOT_CAUSE_STATUSES as readonly string[]).includes(value);
}

/** 读一次 URL 查询参数（聚类卡片的链接会带上 category / analysis_status）。 */
function readUrlParam(key: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(key)?.trim() ?? "";
}

export function BadcasesView() {
  // 支持 `/admin/badcases?category=<category>&analysis_status=<status>` 直接落到筛选结果。
  // 惰性初始化读 window.location.search（同 sessions-view 的 `?q=`），不用 useSearchParams
  // （它需要 build 期的 Suspense 边界），也不在 effect 里 setState（仓库 ESLint 禁止）。
  // 安全前提：管理台外壳在 hydration 完成前只渲染骨架，这个组件的初始化一定发生在浏览器里。
  const [initialFilter] = useState(() => {
    const rawStatus = readUrlParam("analysis_status");
    return {
      category: readUrlParam("category"),
      // 只认登记过的状态：手输的垃圾值会让下拉显示「全部状态」却真的按它过滤。
      analysisStatus: isAnalysisStatus(rawStatus) ? rawStatus : "",
    };
  });
  // 带筛选参数进来，意图就是看那一批案例，所以直接落在「全部案例」而不是聚类。
  const [view, setView] = useState<BadcaseViewKey>(
    initialFilter.category || initialFilter.analysisStatus ? "list" : "cluster",
  );
  const [windowKey, setWindowKey] = useState<AdminWindowKey>(DEFAULT_WINDOW);
  const [analysisStatus, setAnalysisStatus] = useState(initialFilter.analysisStatus || FILTER_ALL);
  const [category, setCategory] = useState(initialFilter.category || FILTER_ALL);
  const [severity, setSeverity] = useState(FILTER_ALL);
  const [runIdDraft, setRunIdDraft] = useState("");
  const [runId, setRunId] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<AdminBadcase | null>(null);
  const limit = PAGE_SIZE;

  const query = {
    analysisStatus: analysisStatus === FILTER_ALL ? undefined : analysisStatus,
    category: category === FILTER_ALL ? undefined : category,
    severity: severity === FILTER_ALL ? undefined : severity,
    runId: runId || undefined,
    limit,
    offset,
  };

  // 两个视图各用各的资源；当前没显示的视图不请求（enabled=false），
  // 这样聚类视图的首屏不会被一份用不上的列表请求拖慢。
  const groups = useAdminResource<AdminBadcaseGroups>(
    `admin-badcase-groups:${windowKey}`,
    () => getAdminBadcaseGroups({ window: windowKey }),
    view === "cluster",
  );

  const resource = useAdminResource<AdminBadcaseList>(
    `admin-badcases:${query.analysisStatus ?? ""}:${query.category ?? ""}:${query.severity ?? ""}:${query.runId ?? ""}:${limit}:${offset}`,
    () => getAdminBadcases(query),
    view === "list",
  );

  const facets = resource.data?.facets;
  const items = resource.data?.items ?? [];

  function handleRefresh() {
    groups.reload();
    resource.reload();
  }

  /**
   * 从聚类卡片跳到「全部案例」。
   *
   * 同时改本地 state 是因为 Link 跳到同一路径时组件不会重新挂载，惰性初始化不会重跑；
   * href 上仍然带 `category`，刷新页面或把链接发给别人时同样能落到筛选结果。
   * 旧的分析 / 严重度 / Run 筛选在这里清掉：这个链接的语义是「该分类的全部案例」，
   * 留着旧筛选会让人以为聚类次数对不上。
   */
  function openCategory(next: string) {
    setCategory(next);
    setAnalysisStatus(FILTER_ALL);
    setSeverity(FILTER_ALL);
    setRunId("");
    setRunIdDraft("");
    setOffset(0);
    setView("list");
  }

  const actionColumn: AdminColumn<AdminBadcase> = {
    key: "action",
    header: "操作",
    align: "right",
    cell: (badcase) => (
      <Button variant="outline" size="xs" onClick={() => setSelected(badcase)}>
        详情
      </Button>
    ),
  };

  const columns: AdminColumn<AdminBadcase>[] = [
    {
      key: "symptom",
      header: "症状",
      primary: true,
      className: "max-w-[22rem]",
      cell: (badcase) => (
        <span
          className="line-clamp-2 block max-w-[22rem] text-xs text-foreground"
          title={badcase.symptom || badcaseCategoryLabel(badcase.category)}
        >
          {cleanBadcaseSymptom(badcase.symptom, badcase.category)}
        </span>
      ),
    },
    {
      key: "category",
      header: "分类",
      cell: (badcase) => (
        <span className="whitespace-nowrap text-xs" title={badcase.category}>
          {badcaseCategoryLabel(badcase.category)}
        </span>
      ),
    },
    { key: "severity", header: "严重度", cell: (badcase) => <StatusBadge status={badcase.severity} /> },
    {
      key: "analysis_status",
      header: "分析",
      cell: (badcase) => <StatusBadge status={badcase.analysis_status} />,
    },
    {
      key: "fixed_status",
      header: "修复",
      cell: (badcase) => <StatusBadge status={badcase.fixed_status} />,
    },
    {
      key: "root_cause_status",
      header: "根因",
      mobileHidden: true,
      cell: (badcase) => <StatusBadge status={badcase.root_cause_status} />,
    },
    {
      key: "detected_by",
      header: "发现方式",
      mobileHidden: true,
      cell: (badcase) => <StatusBadge status={badcase.detected_by} />,
    },
    {
      key: "badcase_id",
      header: "编号",
      mobileHidden: true,
      className: "max-w-[10rem]",
      cell: (badcase) => (
        <span
          className="block max-w-[10rem] truncate font-mono text-[11px] whitespace-nowrap text-muted-foreground"
          title={badcase.badcase_id}
        >
          {badcase.badcase_id}
        </span>
      ),
    },
    {
      key: "run_id",
      header: "Run",
      mobileHidden: true,
      className: "max-w-[12rem]",
      cell: (badcase) => (
        <Link
          href={`/admin/runs/${encodeURIComponent(badcase.run_id)}`}
          title={badcase.run_id}
          className="block max-w-[12rem] truncate font-mono text-[11px] whitespace-nowrap text-primary underline-offset-4 hover:underline"
        >
          {badcase.run_id}
        </Link>
      ),
    },
    {
      key: "created_at",
      header: "登记时间",
      mobileHidden: true,
      cell: (badcase) => (
        <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
          {formatDateTime(badcase.created_at)}
        </span>
      ),
    },
    actionColumn,
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="问题中心"
        description="先看「有多少个真正需要修的问题」，再逐条追溯到具体案例。聚类按 Bad Case 分类聚合，趋势对比上一个等长窗口。"
        badge={<Bug className="size-4 text-muted-foreground" aria-hidden />}
        actions={
          <>
            {view === "cluster" ? (
              <WindowSelect value={windowKey} onChange={setWindowKey} className="w-36" />
            ) : null}
            <Button variant="outline" size="sm" onClick={handleRefresh}>
              <RefreshCw />
              刷新
            </Button>
          </>
        }
      />

      <ViewSwitch value={view} onChange={setView} />

      {view === "cluster" ? (
        <ClusterView resource={groups} windowKey={windowKey} onOpenCategory={openCategory} />
      ) : (
        <>
          <div className="flex flex-col gap-3 rounded-xl bg-card p-3 ring-1 ring-foreground/10">
            <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <Filter className="size-3.5 text-muted-foreground" aria-hidden />
              筛选
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <FilterSelect
                label="分析状态"
                value={analysisStatus}
                options={mergeFacetOptions(facets?.analysis_status, ANALYSIS_FALLBACK, "全部状态")}
                onChange={(next) => {
                  setAnalysisStatus(next);
                  setOffset(0);
                }}
              />
              <FilterSelect
                label="分类"
                value={category}
                options={mergeFacetOptions(facets?.category, [], "全部分类")}
                onChange={(next) => {
                  setCategory(next);
                  setOffset(0);
                }}
              />
              <FilterSelect
                label="严重度"
                value={severity}
                options={mergeFacetOptions(facets?.severity, [], "全部严重度")}
                onChange={(next) => {
                  setSeverity(next);
                  setOffset(0);
                }}
              />
              <div className="flex min-w-0 flex-col gap-1">
                <label className="text-[11px] text-muted-foreground" htmlFor="badcase-run-filter">
                  Run 编号
                </label>
                <div className="flex items-center gap-1.5">
                  <Input
                    id="badcase-run-filter"
                    value={runIdDraft}
                    placeholder="精确匹配 run_id"
                    onChange={(event) => setRunIdDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        setRunId(runIdDraft.trim());
                        setOffset(0);
                      }
                    }}
                  />
                  <Button
                    variant="outline"
                    size="icon-sm"
                    aria-label="按 Run 编号筛选"
                    onClick={() => {
                      setRunId(runIdDraft.trim());
                      setOffset(0);
                    }}
                  >
                    <Search />
                  </Button>
                </div>
              </div>
            </div>
            {resource.data && Object.keys(resource.data.facets.category ?? {}).length === 0 ? (
              <p className="text-[11px] leading-4 text-muted-foreground">
                注意：后端返回的 facets 为空，分类与严重度的可选项暂时只有「全部」。
                当前列表本身仍然可用。
              </p>
            ) : null}
          </div>

          <ResourceView
            resource={resource}
            loadingRows={5}
            isEmpty={(data) => data.items.length === 0}
            emptyTitle="没有符合条件的问题案例"
            emptyDescription="当前筛选下后端返回了空集合。放宽筛选条件，或等待新的 Bad Case 被登记。"
          >
            {(data) => (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span className="tabular">
                    第 {formatNumber(data.items.length === 0 ? 0 : data.offset + 1)}–
                    {formatNumber(data.offset + data.items.length)} 条 / 共 {formatNumber(data.total)} 条
                  </span>
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={offset === 0}
                      onClick={() => setOffset((value) => Math.max(0, value - limit))}
                    >
                      <ChevronLeft />
                      上一页
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={offset + limit >= data.total || data.items.length === 0}
                      onClick={() => setOffset((value) => value + limit)}
                    >
                      下一页
                      <ChevronRight />
                    </Button>
                  </div>
                </div>
                <AdminTable columns={columns} rows={items} getRowKey={(badcase) => badcase.badcase_id} />
              </div>
            )}
          </ResourceView>
        </>
      )}

      <AdminDetailDialog
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
        title={selected ? badcaseCategoryLabel(selected.category) : "问题案例"}
        description={selected ? selected.badcase_id : undefined}
        badge={selected ? <StatusBadge status={selected.severity} /> : undefined}
        footer={
          selected ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] text-muted-foreground">
                修改会立即 PATCH 到后端，保存后列表会自动刷新。
              </span>
              <Button type="submit" form="badcase-patch-form" size="sm">
                <Save />
                保存修改
              </Button>
            </div>
          ) : null
        }
      >
        {selected ? (
          <BadcaseDetail
            key={selected.badcase_id}
            badcase={selected}
            onUpdated={(updated) => {
              setSelected(updated);
              resource.reload();
            }}
          />
        ) : null}
      </AdminDetailDialog>
    </div>
  );
}

/* ------------------------------ 视图切换 ------------------------------ */

const VIEW_OPTIONS: { key: BadcaseViewKey; label: string; icon: typeof Layers }[] = [
  { key: "cluster", label: "问题聚类", icon: Layers },
  { key: "list", label: "全部案例", icon: List },
];

/** 造型照抄 ui/tabs 的默认变体（muted 底、选中项浮起），与运行详情的分区条一致。 */
function ViewSwitch({
  value,
  onChange,
}: {
  value: BadcaseViewKey;
  onChange: (key: BadcaseViewKey) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="问题中心视图"
      className="flex w-fit items-center gap-1 rounded-lg bg-muted p-[3px]"
    >
      {VIEW_OPTIONS.map((option) => {
        const active = option.key === value;
        const Icon = option.icon;
        return (
          <button
            key={option.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.key)}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-sm font-medium whitespace-nowrap transition-all focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
              active ? "bg-background text-foreground shadow-sm" : "text-foreground/60 hover:text-foreground",
            )}
          >
            <Icon className="size-3.5" aria-hidden />
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------ 问题聚类 ------------------------------ */

function ClusterView({
  resource,
  windowKey,
  onOpenCategory,
}: {
  resource: AdminResource<AdminBadcaseGroups>;
  windowKey: AdminWindowKey;
  onOpenCategory: (category: string) => void;
}) {
  return (
    <ResourceView
      resource={resource}
      loadingRows={6}
      isEmpty={(data) => data.groups.length === 0}
      emptyTitle="这个窗口内没有聚类出问题"
      emptyDescription="规则、人工、Benchmark 与模型评审都没有登记 Bad Case，所以没有可聚类的问题。可以换一个更长的时间范围再看。"
    >
      {(data) => (
        <div className="flex flex-col gap-4">
          <ClusterTotals data={data} windowKey={windowKey} />
          <div className="flex flex-col gap-3">
            {data.groups.map((group) => (
              <GroupCard key={group.key || group.category} group={group} onOpenCategory={onOpenCategory} />
            ))}
          </div>
          <NotesBlock notes={data.notes} />
        </div>
      )}
    </ResourceView>
  );
}

function ClusterTotals({ data, windowKey }: { data: AdminBadcaseGroups; windowKey: AdminWindowKey }) {
  const totals = data.totals;
  const label = windowLabel(windowKey, data.window_label || null);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs leading-5 text-foreground">
        <span className="tabular font-medium">{formatNumber(totals.badcases)}</span> 条 Bad Case 聚成{" "}
        <span className="tabular font-medium">{formatNumber(totals.groups)}</span>{" "}
        个真正需要解决的问题。
      </p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <StatCard label="本窗口新增" value={formatNumber(totals.badcases)} hint={label} />
        <StatCard
          label="上一周期"
          value={formatNumber(totals.previous_badcases)}
          hint="等长窗口，用于对比趋势"
        />
        <StatCard label="聚类数" value={formatNumber(totals.groups)} hint="按 Bad Case 分类聚合" />
        <StatCard label="影响运行" value={formatNumber(totals.affected_runs)} hint="被这些问题波及的 run" />
        <StatCard
          label="未处理总数"
          value={formatNumber(totals.open_total)}
          tone={totals.open_total > 0 ? "warning" : "muted"}
          hint={`其中待分析 ${formatNumber(totals.pending)} 条`}
        />
      </div>
    </div>
  );
}

function GroupCard({
  group,
  onOpenCategory,
}: {
  group: AdminBadcaseGroup;
  onOpenCategory: (category: string) => void;
}) {
  const label = badcaseCategoryLabel(group.category);
  const sampleRuns = group.sample_runs.slice(0, SAMPLE_RUN_LIMIT);

  return (
    <article className="flex min-w-0 flex-col gap-3 rounded-xl bg-card p-3.5 ring-1 ring-foreground/10">
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <StatusBadge status={group.severity} />
          <h2 className="min-w-0 text-sm font-medium text-foreground" title={group.category}>
            {label}
          </h2>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <TrendIndicator trendPct={group.trend_pct} previousCount={group.previous_count} />
          <Sparkline
            variant="bar"
            tone={severityTone(group.severity)}
            points={group.sparkline.map((point) => point.value)}
            ariaLabel={`${label} 在窗口内的出现趋势`}
            className="w-24"
          />
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3 lg:grid-cols-6">
        <GroupMetric label="出现次数" value={formatNumber(group.occurrence_count)} />
        <GroupMetric label="影响运行" value={formatNumber(group.affected_runs)} />
        <GroupMetric label="待分析" value={formatNumber(group.pending_analysis)} />
        <GroupMetric label="未修复" value={formatNumber(group.unfixed)} />
        <GroupMetric
          label="关联模块"
          value={group.related_module ?? "未登记模块"}
          mono={Boolean(group.related_module)}
        />
        <GroupMetric
          label="最近一次"
          value={formatAgo(group.last_seen_ago_seconds)}
          title={group.last_seen ? formatDateTime(group.last_seen) : undefined}
        />
      </dl>

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] text-muted-foreground">关联 Provider</span>
        {group.related_providers.length > 0 ? (
          group.related_providers.map((provider) => (
            <ToneBadge key={provider} tone="muted" className="text-[0.6875rem]">
              <span className="font-mono" title={provider}>
                {provider}
              </span>
            </ToneBadge>
          ))
        ) : (
          <span className="text-[11px] text-muted-foreground">—</span>
        )}
      </div>

      {group.top_symptoms.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-[11px] text-muted-foreground">主要症状</span>
          <ul className="flex min-w-0 flex-col gap-1">
            {group.top_symptoms.map((symptom, index) => (
              <li
                key={`${index}-${symptom.text}`}
                className="flex min-w-0 items-baseline justify-between gap-3 text-xs leading-5 text-foreground"
              >
                <span className="min-w-0 break-words" title={symptom.text}>
                  {cleanBadcaseSymptom(symptom.text, group.category)}
                </span>
                <span className="tabular shrink-0 text-[11px] text-muted-foreground">
                  ×{formatNumber(symptom.count)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-[11px] text-muted-foreground">后端没有返回这一组的主要症状。</p>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-border/60 pt-2.5">
        <span className="text-[11px] text-muted-foreground">样例运行</span>
        {sampleRuns.length > 0 ? (
          sampleRuns.map((runId) => (
            <Link
              key={runId}
              href={`/admin/runs/${encodeURIComponent(runId)}`}
              title={runId}
              className="max-w-[11rem] truncate font-mono text-[11px] text-primary underline-offset-4 hover:underline"
            >
              {runId}
            </Link>
          ))
        ) : (
          <span className="text-[11px] text-muted-foreground">—</span>
        )}
        {group.sample_runs.length > sampleRuns.length ? (
          <span className="text-[11px] text-muted-foreground">
            等 {formatNumber(group.sample_runs.length)} 个运行
          </span>
        ) : null}
        <Link
          href={`/admin/badcases?category=${encodeURIComponent(group.category)}`}
          onClick={() => onOpenCategory(group.category)}
          className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] font-medium text-primary underline-offset-4 hover:underline"
        >
          在「全部案例」中查看该分类
          <ArrowRight className="size-3" aria-hidden />
        </Link>
      </div>
    </article>
  );
}

/**
 * 趋势：`trend_pct` 为 null 表示上一周期一条都没有 —— 这时候只写「上一周期 0」，
 * 绝不换算成一个百分比（0 → X 的变化率没有定义）。
 */
function TrendIndicator({
  trendPct,
  previousCount,
}: {
  trendPct: number | null;
  previousCount: number;
}) {
  if (trendPct === null || !Number.isFinite(trendPct)) {
    return (
      <span className="text-[11px] whitespace-nowrap text-muted-foreground" title="上一周期没有同类问题">
        上一周期 0
      </span>
    );
  }

  const percent = Math.round(trendPct * 100);
  const up = percent > 0;
  const Icon = percent === 0 ? Minus : up ? TrendingUp : TrendingDown;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 text-[11px] font-medium whitespace-nowrap",
        percent === 0
          ? "text-muted-foreground"
          : up
            ? "text-danger-subtle-foreground"
            : "text-success-subtle-foreground",
      )}
      title={`上一周期 ${formatNumber(previousCount)} 次`}
    >
      <Icon className="size-3.5" aria-hidden />
      {percent === 0 ? "与上一周期持平" : `较上一周期 ${up ? "↑" : "↓"}${Math.abs(percent)}%`}
    </span>
  );
}

function GroupMetric({
  label,
  value,
  title,
  mono = false,
}: {
  label: string;
  value: string;
  title?: string;
  mono?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 border-b border-border/60 pb-1">
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd
        className={cn("min-w-0 truncate text-xs font-medium text-foreground", mono && "font-mono")}
        title={title ?? value}
      >
        {value}
      </dd>
    </div>
  );
}

/** 相对时间只用于「最近一次」这类读数；后端没给秒数时给「—」。 */
function formatAgo(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.round(seconds));
  if (value < 60) return `${value} 秒前`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 严重度只影响趋势柱的颜色：越严重越红，未登记的严重度用中性色。 */
function severityTone(severity: string): "info" | "warning" | "danger" | "muted" {
  switch (severity) {
    case "critical":
    case "severe":
    case "high":
      return "danger";
    case "medium":
    case "moderate":
      return "warning";
    case "low":
    case "minor":
      return "muted";
    default:
      return "info";
  }
}

/** 口径与数据缺口：与漏斗 / 质量页同一块造型，缺什么口径必须写在这儿。 */
function NotesBlock({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <section className="rounded-xl bg-muted/40 px-3.5 py-3 ring-1 ring-foreground/10">
      <h2 className="text-xs font-medium text-foreground">口径与数据缺口</h2>
      <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[11px] leading-4 text-muted-foreground">
        {notes.map((note, index) => (
          <li key={index}>{note}</li>
        ))}
      </ul>
    </section>
  );
}

/* ------------------------------ 案例详情 ------------------------------ */

function BadcaseDetail({
  badcase,
  onUpdated,
}: {
  badcase: AdminBadcase;
  onUpdated: (badcase: AdminBadcase) => void;
}) {
  const [analysisStatus, setAnalysisStatus] = useState(badcase.analysis_status);
  const [fixedStatus, setFixedStatus] = useState(badcase.fixed_status);
  const [rootCauseStatus, setRootCauseStatus] = useState(badcase.root_cause_status);
  const [rootCauseText, setRootCauseText] = useState(badcase.suspected_root_cause ?? "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<AdminApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const dirty =
    analysisStatus !== badcase.analysis_status ||
    fixedStatus !== badcase.fixed_status ||
    rootCauseStatus !== badcase.root_cause_status ||
    rootCauseText !== (badcase.suspected_root_cause ?? "");

  async function handleSubmit() {
    if (pending) return;
    const patch: AdminBadcasePatch = {};
    if (analysisStatus !== badcase.analysis_status && isAnalysisStatus(analysisStatus)) {
      patch.analysis_status = analysisStatus;
    }
    if (fixedStatus !== badcase.fixed_status && isFixedStatus(fixedStatus)) {
      patch.fixed_status = fixedStatus;
    }
    if (rootCauseStatus !== badcase.root_cause_status && isRootCauseStatus(rootCauseStatus)) {
      patch.root_cause_status = rootCauseStatus;
    }
    if (rootCauseText !== (badcase.suspected_root_cause ?? "")) {
      patch.suspected_root_cause = rootCauseText;
    }
    if (Object.keys(patch).length === 0) {
      setNotice("没有需要提交的改动。");
      return;
    }
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await patchAdminBadcase(badcase.badcase_id, patch);
      onUpdated(updated);
      setNotice("已保存，列表已刷新。");
    } catch (cause) {
      setError(toAdminApiError(cause));
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      id="badcase-patch-form"
      className="flex flex-col gap-5"
      onSubmit={(event) => {
        event.preventDefault();
        void handleSubmit();
      }}
    >
      <PanelSection title="问题描述">
        <DescriptionList
          items={[
            { label: "分类", value: badcaseCategoryLabel(badcase.category) },
            { label: "症状", value: cleanBadcaseSymptom(badcase.symptom, badcase.category) },
            { label: "期望", value: badcase.expected || "未返回" },
            { label: "实际", value: badcase.actual || "未返回" },
          ]}
        />
      </PanelSection>

      <Separator />

      <PanelSection title="定位信息">
        <DescriptionList
          items={[
            { label: "疑似根因", value: badcase.suspected_root_cause || "未返回" },
            { label: "根因状态", value: <StatusBadge status={badcase.root_cause_status} /> },
            { label: "发现方式", value: <StatusBadge status={badcase.detected_by} /> },
            { label: "分析状态", value: <StatusBadge status={badcase.analysis_status} /> },
            { label: "修复状态", value: <StatusBadge status={badcase.fixed_status} /> },
            { label: "引入版本", value: badcase.introduced_in || "未返回" },
            { label: "修复版本", value: badcase.fixed_in || "未返回" },
            {
              label: "Trace 引用",
              value:
                badcase.trace_refs.length > 0 ? (
                  <span className="flex flex-wrap gap-x-2 gap-y-1">
                    {badcase.trace_refs.map((ref) => (
                      <Link
                        key={ref}
                        href={`/admin/runs/${encodeURIComponent(badcase.run_id)}`}
                        title={`在运行详情里查看 ${ref}`}
                        className="font-mono text-[11px] break-all text-primary underline-offset-4 hover:underline"
                      >
                        {ref}
                      </Link>
                    ))}
                  </span>
                ) : (
                  "未返回"
                ),
            },
            {
              label: "关联 Run",
              value: (
                <Link
                  href={`/admin/runs/${encodeURIComponent(badcase.run_id)}`}
                  className="font-mono text-primary underline-offset-4 hover:underline"
                >
                  {badcase.run_id}
                </Link>
              ),
            },
            { label: "登记时间", value: formatDateTime(badcase.created_at) },
          ]}
        />
      </PanelSection>

      <Separator />

      <PanelSection
        title="更新结论"
        description="只提交真正改过的字段；后端返回更新后的完整案例对象。"
      >
        <div className="grid gap-3 sm:grid-cols-3">
          <FilterSelect
            label="分析状态"
            value={analysisStatus}
            options={mergeFacetOptions(null, ANALYSIS_FALLBACK, "未选择")}
            onChange={setAnalysisStatus}
          />
          <FilterSelect
            label="修复状态"
            value={fixedStatus}
            options={mergeFacetOptions(null, FIXED_FALLBACK, "未选择")}
            onChange={setFixedStatus}
          />
          <FilterSelect
            label="根因状态"
            value={rootCauseStatus}
            options={mergeFacetOptions(null, ROOT_CAUSE_FALLBACK, "未选择")}
            onChange={setRootCauseStatus}
          />
        </div>
        <label className="mt-1 flex flex-col gap-1 text-[11px] text-muted-foreground" htmlFor="badcase-root-cause">
          疑似根因
        </label>
        <Textarea
          id="badcase-root-cause"
          value={rootCauseText}
          onChange={(event) => setRootCauseText(event.target.value)}
          placeholder="写清楚失败模式与受影响模块，便于后续 Evolution 复用"
          rows={4}
        />
      </PanelSection>

      {error ? (
        <ActionError message="保存失败" detail={error.detail} />
      ) : notice ? (
        <p className="text-[11px] text-muted-foreground">{notice}</p>
      ) : pending ? (
        <p className="text-[11px] text-muted-foreground">正在提交修改…</p>
      ) : dirty ? (
        <p className="text-[11px] text-muted-foreground">有未保存的修改。</p>
      ) : (
        <SectionEmpty
          title="没有待保存的修改"
          description="改动三个状态下拉或根因文本后，底部「保存修改」按钮会把差异 PATCH 到后端。"
        />
      )}
    </form>
  );
}
