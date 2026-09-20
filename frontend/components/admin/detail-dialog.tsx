"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * 管理台统一的详情弹窗。
 *
 * 约定（任务 §10）：所有详情/明细都走弹窗或抽屉，主页面只给摘要。
 * 这个壳只负责三件事：
 * 1) 桌面端限宽限高并内部滚动，移动端沿用 ui/dialog 的全屏行为；
 * 2) 标题栏固定，正文可滚动；
 * 3) 可选底栏（保存 / 关闭）。
 */
export function AdminDetailDialog({
  open,
  onOpenChange,
  title,
  description,
  badge,
  footer,
  children,
  contentClassName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  badge?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  contentClassName?: string;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn("sm:max-h-[85vh] sm:max-w-3xl sm:overflow-y-auto", contentClassName)}
      >
        <DialogHeader className="pr-8">
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <span className="min-w-0 break-words">{title}</span>
            {badge}
          </DialogTitle>
          {description ? (
            <DialogDescription className="break-words">{description}</DialogDescription>
          ) : (
            <DialogDescription className="sr-only">详情</DialogDescription>
          )}
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-4">{children}</div>
        {footer ? (
          <div className="-mx-4 -mb-4 border-t bg-muted/50 p-4 sm:sticky sm:bottom-0">{footer}</div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/** 弹窗里的 pretty JSON 块：限高、可滚动、长行不撑破布局。 */
export function JsonBlock({
  value,
  className,
  maxHeight = "22rem",
}: {
  value: unknown;
  className?: string;
  maxHeight?: string;
}) {
  return (
    <pre
      className={cn(
        "max-w-full overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-[11px] leading-4 break-all whitespace-pre-wrap text-foreground",
        className,
      )}
      style={{ maxHeight }}
    >
      {stringify(value)}
    </pre>
  );
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 弹窗里的「键 / 值」两列小表。 */
export function KeyValueList({
  entries,
  className,
}: {
  entries: { key: string; label: string; value: ReactNode }[];
  className?: string;
}) {
  return (
    <dl className={cn("grid gap-x-6 gap-y-2 sm:grid-cols-2", className)}>
      {entries.map((entry) => (
        <div
          key={entry.key}
          className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
        >
          <dt className="shrink-0 text-[11px] text-muted-foreground">{entry.label}</dt>
          <dd className="min-w-0 text-right text-xs break-words text-foreground">{entry.value}</dd>
        </div>
      ))}
    </dl>
  );
}
