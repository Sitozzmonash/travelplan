"use client";

import Link from "next/link";
import { useState } from "react";
import { Bug, ChevronLeft, ChevronRight, Filter, RefreshCw, Save, Search } from "lucide-react";
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
import { StatusBadge } from "@/components/admin/status-badge";
import { badcaseCategoryLabel, cleanBadcaseSymptom, formatNumber, statusLabel } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { formatDateTime } from "@/lib/format";
import {
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
  type AdminBadcaseList,
  type AdminBadcasePatch,
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

function isAnalysisStatus(value: string): value is BadcaseAnalysisStatus {
  return (BADCASE_ANALYSIS_STATUSES as readonly string[]).includes(value);
}

function isFixedStatus(value: string): value is BadcaseFixedStatus {
  return (BADCASE_FIXED_STATUSES as readonly string[]).includes(value);
}

function isRootCauseStatus(value: string): value is BadcaseRootCauseStatus {
  return (BADCASE_ROOT_CAUSE_STATUSES as readonly string[]).includes(value);
}

export function BadcasesView() {
  const [analysisStatus, setAnalysisStatus] = useState(FILTER_ALL);
  const [category, setCategory] = useState(FILTER_ALL);
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

  const resource = useAdminResource<AdminBadcaseList>(
    `admin-badcases:${query.analysisStatus ?? ""}:${query.category ?? ""}:${query.severity ?? ""}:${query.runId ?? ""}:${limit}:${offset}`,
    () => getAdminBadcases(query),
  );

  const facets = resource.data?.facets;
  const items = resource.data?.items ?? [];

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
        title="问题案例"
        description="规则、人工、Benchmark 与模型评审登记的问题。可以在这里更新分析状态、修复状态与根因结论。"
        badge={<Bug className="size-4 text-muted-foreground" aria-hidden />}
        actions={
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw />
            刷新
          </Button>
        }
      />

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
