"use client";

import { cn } from "@/lib/utils";
import type { ProviderStatus } from "@/types/plan";
import { providerLabel } from "@/lib/display";

export { providerLabel };

interface SourceBadgeProps {
  provider: string;
  status?: ProviderStatus | string;
  onClick?: () => void;
  className?: string;
}

/** 来源 Badge：只显示中文来源名，不展示 URL 或查询条件。 */
export function SourceBadge({ provider, status, onClick, className }: SourceBadgeProps) {
  const degraded = status && status !== "OK";
  const content = (
    <>
      <span className="size-1.5 rounded-full bg-current opacity-45" aria-hidden />
      {providerLabel(provider)}
      {degraded ? <span className="opacity-70">·部分可用</span> : null}
    </>
  );

  const base = cn(
    "inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/50 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground",
    onClick && "transition-colors hover:bg-muted hover:text-foreground",
    className,
  );

  if (!onClick) {
    return (
      <span className={base} title={degraded ? `${providerLabel(provider)}：本次为部分可用数据` : undefined}>
        {content}
      </span>
    );
  }

  return (
    <button type="button" onClick={onClick} className={base} title="查看来源">
      {content}
    </button>
  );
}
