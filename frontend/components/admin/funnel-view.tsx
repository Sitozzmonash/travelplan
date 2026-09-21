"use client";

import { useState } from "react";
import { RefreshCw, TrendingDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { FilterSelect, type FilterOption } from "@/components/admin/filter-select";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { StatCard } from "@/components/admin/stat-card";
import { WindowSelect, windowLabel } from "@/components/admin/window-select";
import { formatNumber, formatRatio } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { cn } from "@/lib/utils";
import { getAdminFunnel } from "@/lib/admin-api";
import type {
  AdminFunnel,
  AdminFunnelBreakdownRow,
  AdminFunnelStage,
  AdminWindowKey,
} from "@/types/admin";

/**
 * 规划漏斗：回答"用户在哪一步流失"。
 *
 * 页面只做两件事：把每个阶段的绝对人数摆出来（柱宽 = 占首步比例），
 * 以及把"相对上一步流失最多"的那一段标成最显眼的元素。
 * 阶段判定全部由后端按会话事件流给出，前端不做任何估算。
 */

/** 默认 7d：引导式会话一天内的样本还不够看流失。 */
const DEFAULT_WINDOW: AdminWindowKey = "7d";

/**
 * 拆解维度的兜底选项。
 * 与后端 `funnel()` 的 `dimension_options` 同一张表：首次加载（payload 还没到）时
 * 下拉也要能用，否则操作员会以为"没得选"。后端返回后以返回值为准。
 */
const FALLBACK_DIMENSIONS: FilterOption[] = [
  { value: "destination", label: "目的地" },
  { value: "date", label: "日期" },
  { value: "pace", label: "节奏偏好" },
  { value: "transport_priority", label: "交通偏好" },
  { value: "hotel_priority", label: "住宿偏好" },
  { value: "session_status", label: "会话状态" },
];

export function FunnelView() {
  const [windowKey, setWindowKey] = useState<AdminWindowKey>(DEFAULT_WINDOW);
  const [dimension, setDimension] = useState("destination");

  const resource = useAdminResource<AdminFunnel>(
    `admin-funnel:${windowKey}:${dimension}`,
    () => getAdminFunnel({ window: windowKey, dimension }),
  );

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="规划漏斗"
        description="回答「用户在哪一步流失」。按会话事件流推出每个会话走到了最远的哪一步，再反推各阶段人数，因此各阶段天然单调不增。"
        actions={
          <>
            <WindowSelect value={windowKey} onChange={setWindowKey} className="w-36" />
            <Button variant="outline" size="sm" onClick={resource.reload}>
              <RefreshCw />
              刷新
            </Button>
          </>
        }
      />

      <ResourceView resource={resource} loadingRows={6}>
        {(data) => (
          <FunnelBody
            data={data}
            dimension={dimension}
            onDimensionChange={setDimension}
          />
        )}
      </ResourceView>
    </div>
  );
}

function FunnelBody({
  data,
  dimension,
  onDimensionChange,
}: {
  data: AdminFunnel;
  dimension: string;
  onDimensionChange: (next: string) => void;
}) {
  const lastStage = data.stages.length > 0 ? data.stages[data.stages.length - 1] : null;
  const dimensionOptions: FilterOption[] =
    data.dimension_options.length > 0
      ? data.dimension_options.map((option) => ({ value: option.key, label: option.label }))
      : FALLBACK_DIMENSIONS;
  const rows = data.breakdown[data.destination_dimension] ?? data.breakdown[dimension] ?? [];

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <StatCard
          label="会话数"
          value={formatNumber(data.sessions)}
          hint={`${windowLabel(data.window, data.window_label || null)}；上一等长窗口 ${formatNumber(
            data.previous_sessions,
          )} 个`}
        />
        <StatCard
          label="成功生成"
          value={formatNumber(lastStage?.count)}
          tone="success"
          hint={lastStage ? `走到「${lastStage.label}」的会话数` : "没有阶段数据"}
        />
        <StatCard
          label="整体转化率"
          value={data.overall_conversion === null ? "—" : formatRatio(data.overall_conversion)}
          hint="成功生成 / 会话创建；无会话时为「—」"
        />
      </div>

      {data.biggest_drop ? (
        <BiggestDrop data={data} />
      ) : (
        <p className="text-[11px] leading-4 text-muted-foreground">
          窗口内还没有可比较的相邻阶段，因此算不出最大流失。
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>阶段漏斗</CardTitle>
          <CardDescription>
            柱宽 = 该阶段人数占首步的比例。每个阶段下面给出相对上一步的流失人数与比例。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {data.stages.length === 0 ? (
            <SectionEmpty
              title="没有阶段数据"
              description="后端没有返回 stages。可能是窗口内没有任何会话。"
              hint="漏斗按 planning_sessions.created_at 过滤窗口。"
            />
          ) : (
            <ol className="flex flex-col gap-3.5">
              {data.stages.map((stage, index) => (
                <StageRow key={stage.key} stage={stage} previous={data.stages[index - 1] ?? null} />
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>维度拆解</CardTitle>
              <CardDescription>
                按选中的维度把会话分组，看同一阶段在哪里差异最大。维度值只显示窗口内实际出现的取值。
              </CardDescription>
            </div>
            <FilterSelect
              label="拆解维度"
              value={dimension}
              options={dimensionOptions}
              onChange={onDimensionChange}
              className="w-40"
            />
          </div>
        </CardHeader>
        <CardContent>
          <BreakdownTable rows={rows} stages={data.stages} />
        </CardContent>
      </Card>

      <NotesBlock notes={data.notes} />
    </div>
  );
}

function BiggestDrop({ data }: { data: AdminFunnel }) {
  const drop = data.biggest_drop;
  if (!drop) return null;
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-warning/40 bg-warning-subtle px-4 py-3.5">
      <span className="flex items-center gap-1.5 text-[11px] font-medium text-warning-subtle-foreground">
        <TrendingDown className="size-3.5 shrink-0" aria-hidden />
        最大流失
      </span>
      <span className="text-base leading-snug font-semibold text-warning-subtle-foreground">
        {drop.from} → {drop.to}
      </span>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="tabular text-2xl leading-none font-semibold text-warning-subtle-foreground">
          -{formatRatio(drop.drop_rate)}
        </span>
        <span className="text-[11px] text-warning-subtle-foreground/90">
          流失 {formatNumber(drop.dropped)} 个会话（相对上一步人数）
        </span>
      </div>
    </div>
  );
}

function StageRow({ stage, previous }: { stage: AdminFunnelStage; previous: AdminFunnelStage | null }) {
  const rate = stage.rate_of_total === null ? null : Math.max(0, Math.min(1, stage.rate_of_total));
  const diff = previous ? stage.count - previous.count : 0;

  return (
    <li className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-foreground">{stage.label}</span>
        <span className="tabular text-sm font-semibold text-foreground">
          {formatNumber(stage.count)} 人
        </span>
      </div>

      <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
        {rate === null ? (
          <div className="h-full w-1 rounded-full bg-muted-foreground/40" />
        ) : (
          <div className="h-full rounded-full bg-info" style={{ width: `${rate * 100}%` }} />
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
        <span title="占第一步（会话创建）的比例">
          占首步 {stage.rate_of_total === null ? "—" : formatRatio(stage.rate_of_total)}
        </span>
        {stage.drop_rate === null ? (
          <span>起点</span>
        ) : (
          <span>
            较上一步流失 {formatNumber(stage.dropped)} 人（{formatRatio(stage.drop_rate)}）
          </span>
        )}
        <span title="上一等长窗口的同一步人数">
          上一窗口 {formatNumber(stage.previous_count)} 人
          {diff !== 0 ? (
            <span className={cn("ml-1", diff > 0 ? "text-success" : "text-warning")}>
              ({diff > 0 ? "+" : ""}
              {formatNumber(diff)})
            </span>
          ) : null}
        </span>
      </div>
    </li>
  );
}

function BreakdownTable({
  rows,
  stages,
}: {
  rows: AdminFunnelBreakdownRow[];
  stages: AdminFunnelStage[];
}) {
  const columns: AdminColumn<AdminFunnelBreakdownRow>[] = [
    {
      key: "key",
      header: "维度值",
      primary: true,
      className: "max-w-[12rem]",
      cell: (row) => (
        <span className="block max-w-[12rem] truncate text-xs text-foreground" title={row.key || "未填写"}>
          {row.key || "未填写"}
        </span>
      ),
    },
    {
      key: "sessions",
      header: "会话数",
      align: "right",
      cell: (row) => <span className="tabular text-xs">{formatNumber(row.sessions)}</span>,
    },
    {
      key: "stages",
      header: "各阶段人数",
      cell: (row) => <StageSequence values={row.stages} stages={stages} />,
    },
    {
      key: "plan_success",
      header: "成功生成",
      align: "right",
      mobileHidden: true,
      cell: (row) => <span className="tabular text-xs">{formatNumber(row.plan_success)}</span>,
    },
    {
      key: "conversion",
      header: "转化率",
      align: "right",
      cell: (row) => (
        <span className="tabular text-xs">
          {row.conversion === null ? "—" : formatRatio(row.conversion)}
        </span>
      ),
    },
  ];

  if (rows.length === 0) {
    return (
      <SectionEmpty
        title="这个维度没有可拆解的会话"
        description="后端返回了空集合。可能是该字段在窗口内没有非空取值，或该维度暂不支持。"
      />
    );
  }

  return <AdminTable columns={columns} rows={rows} getRowKey={(row) => row.key} />;
}

/**
 * 各阶段人数的紧凑序列。
 * 阶段多（默认 8 步）时把中间省略掉，只留首尾两段 —— 表格里塞 8 个数字会读不出重点；
 * 完整序列放在 title 里，鼠标悬停即可看到，不隐藏任何数据。
 */
function StageSequence({ values, stages }: { values: number[]; stages: AdminFunnelStage[] }) {
  if (values.length === 0) return <span className="text-xs text-muted-foreground">—</span>;
  const full = values
    .map((value, index) => `${stages[index]?.label ?? `第 ${index + 1} 步`} ${formatNumber(value)}`)
    .join(" → ");
  const numbers = values.map((value) => formatNumber(value));
  const compact =
    numbers.length > 6
      ? [...numbers.slice(0, 2), "…", ...numbers.slice(-2)].join(" → ")
      : numbers.join(" → ");
  return (
    <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground" title={full}>
      {compact}
    </span>
  );
}

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
