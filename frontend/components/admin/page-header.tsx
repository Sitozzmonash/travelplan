"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** 管理台页面标题区：标题 + 一句话说明 + 右侧操作。 */
export function PageHeader({
  title,
  description,
  badge,
  actions,
  className,
}: {
  title: string;
  description?: string;
  badge?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between", className)}>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold tracking-tight text-foreground">{title}</h1>
          {badge}
        </div>
        {description ? (
          <p className="mt-1 max-w-prose text-xs leading-5 text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
