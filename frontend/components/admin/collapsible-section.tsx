"use client";

import { useState, type ReactNode } from "react";
import { ChevronRight, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * 可折叠区块（用原生 <details>）。
 *
 * 为什么不用基础组件库的 Collapsible：这里需要「首屏一定折叠」这个硬性行为，
 * 原生 details 在没有 open 属性时天然折叠，不依赖任何 hydration 顺序；
 * 同时在移动端自带键盘/读屏支持。
 *
 * defaultOpen 通过受控模式实现：否则父组件每次轮询重渲染都会把用户手动折叠的
 * 区块重新弹开。
 */
export function CollapsibleSection({
  title,
  description,
  count,
  icon: Icon,
  defaultOpen = false,
  actions,
  onOpenChange,
  children,
  className,
}: {
  title: string;
  description?: string;
  count?: number | string;
  icon?: LucideIcon;
  defaultOpen?: boolean;
  actions?: ReactNode;
  /** 展开/折叠通知。给「展开后才去请求」的区块用，避免首屏为折叠内容白等一次请求。 */
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <details
      open={open}
      onToggle={(event) => {
        const next = event.currentTarget.open;
        setOpen(next);
        onOpenChange?.(next);
      }}
      className={cn("group/section rounded-xl bg-card ring-1 ring-foreground/10", className)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3.5 py-2.5 [&::-webkit-details-marker]:hidden">
        <ChevronRight
          className="size-4 shrink-0 text-muted-foreground transition-transform group-open/section:rotate-90"
          aria-hidden
        />
        {Icon ? <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden /> : null}
        <span className="truncate text-sm font-medium text-foreground">{title}</span>
        {count !== undefined ? (
          <Badge variant="secondary" className="tabular shrink-0 text-[0.6875rem]">
            {count}
          </Badge>
        ) : null}
        {description ? (
          <span className="hidden min-w-0 truncate text-xs text-muted-foreground sm:inline">
            {description}
          </span>
        ) : null}
        {actions ? (
          // 避免点击按钮时把整个区块折叠起来。
          <span
            className="ml-auto flex shrink-0 items-center gap-1.5"
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
          >
            {actions}
          </span>
        ) : null}
      </summary>
      <div className="border-t border-border/70 px-3.5 py-3">{children}</div>
    </details>
  );
}
