"use client";

import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/** 环比：`better` 由后端给（比率类指标降低才是好事），前端不自己判断好坏。 */
export interface StatCardTrend {
  direction: "up" | "down" | "flat";
  label: string;
  better: boolean | null;
}

/**
 * 概览数字卡。
 * value 允许传 "—"：后端某项缺失时显示占位符，而不是把 undefined 渲染成空白。
 *
 * 关于 trend 的配色：颜色跟的是 **better（这是不是好事）**，不是箭头方向。
 * 失败率下降画成绿色、上升画成红色 —— 跟箭头方向配色会正好给出相反的信号。
 */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "muted",
  trend,
  className,
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: LucideIcon;
  tone?: "muted" | "success" | "warning" | "danger" | "info";
  trend?: StatCardTrend | null;
  className?: string;
}) {
  const toneClass: Record<string, string> = {
    muted: "bg-muted text-muted-foreground",
    success: "bg-success-subtle text-success-subtle-foreground",
    warning: "bg-warning-subtle text-warning-subtle-foreground",
    danger: "bg-danger-subtle text-danger-subtle-foreground",
    info: "bg-info-subtle text-info-subtle-foreground",
  };

  const TrendIcon =
    trend?.direction === "up" ? ArrowUpRight : trend?.direction === "down" ? ArrowDownRight : Minus;
  const trendClass =
    trend && trend.better === true
      ? "text-success-subtle-foreground"
      : trend && trend.better === false
        ? "text-danger-subtle-foreground"
        : "text-muted-foreground";

  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-xl bg-card p-3.5 ring-1 ring-foreground/10",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        {Icon ? (
          <span
            className={cn("flex size-7 shrink-0 items-center justify-center rounded-lg", toneClass[tone])}
            aria-hidden
          >
            <Icon className="size-3.5" />
          </span>
        ) : null}
      </div>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="tabular text-xl leading-none font-semibold tracking-tight text-foreground">
          {value}
        </span>
        {trend ? (
          <span className={cn("inline-flex items-center gap-0.5 text-[11px] font-medium", trendClass)}>
            <TrendIcon className="size-3.5" aria-hidden />
            {trend.label}
          </span>
        ) : null}
      </div>
      {hint ? <span className="text-[11px] leading-4 text-muted-foreground">{hint}</span> : null}
    </div>
  );
}
