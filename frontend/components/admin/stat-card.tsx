"use client";

import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * 概览数字卡。
 * value 允许传 "—"：后端某项缺失时显示占位符，而不是把 undefined 渲染成空白。
 */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "muted",
  className,
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: LucideIcon;
  tone?: "muted" | "success" | "warning" | "danger" | "info";
  className?: string;
}) {
  const toneClass: Record<string, string> = {
    muted: "bg-muted text-muted-foreground",
    success: "bg-success-subtle text-success-subtle-foreground",
    warning: "bg-warning-subtle text-warning-subtle-foreground",
    danger: "bg-danger-subtle text-danger-subtle-foreground",
    info: "bg-info-subtle text-info-subtle-foreground",
  };

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
      <span className="tabular text-xl leading-none font-semibold tracking-tight text-foreground">
        {value}
      </span>
      {hint ? <span className="text-[11px] leading-4 text-muted-foreground">{hint}</span> : null}
    </div>
  );
}
