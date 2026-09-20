"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { formatMetricKey, formatMetricValue } from "@/components/admin/format";
import type { AdminRecord } from "@/types/admin";

/**
 * 指标列表：把后端返回的扁平对象渲染成「中文标签 + 值」。
 * 后端加的字段不需要前端改代码就能显示出来（标签未登记时原样展示 key）。
 */
export function MetricList({
  metrics,
  emptyText = "这一维度没有返回指标。",
  className,
}: {
  metrics: AdminRecord | null | undefined;
  emptyText?: string;
  className?: string;
}) {
  const entries = Object.entries(metrics ?? {});
  if (entries.length === 0) {
    return <p className="text-xs leading-5 text-muted-foreground">{emptyText}</p>;
  }
  return (
    <dl className={cn("grid gap-x-5 gap-y-2 sm:grid-cols-2 lg:grid-cols-3", className)}>
      {entries.map(([key, value]) => (
        <div
          key={key}
          className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
        >
          <dt className="min-w-0 truncate text-xs text-muted-foreground" title={key}>
            {formatMetricKey(key)}
          </dt>
          <dd className="tabular min-w-0 text-right text-xs font-medium break-words text-foreground">
            {formatMetricValue(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export interface DetailItem {
  label: string;
  value: ReactNode;
}

/** 详情用的「标签 / 值」清单，纵向排列，适合侧边面板里阅读。 */
export function DescriptionList({ items, className }: { items: DetailItem[]; className?: string }) {
  return (
    <dl className={cn("flex flex-col gap-2.5", className)}>
      {items.map((item) => (
        <div key={item.label} className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
          <dt className="shrink-0 text-[11px] text-muted-foreground sm:w-24 sm:pt-0.5">{item.label}</dt>
          <dd className="min-w-0 text-xs leading-5 break-words whitespace-pre-wrap text-foreground">
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** 侧边面板里的分组小标题。 */
export function PanelSection({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("flex flex-col gap-2", className)}>
      <div>
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {description ? (
          <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {children}
    </section>
  );
}
