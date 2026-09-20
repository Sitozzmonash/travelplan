import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Info, RefreshCw, SearchX, ServerCrash, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/**
 * 页面状态视图（FRONTEND_DESIGN §26）。
 * 原则：每一种状态都要向用户解释「发生了什么、当前结果是否可用」，
 * 不使用「暂无数据」这类没有解释的文案。
 */

interface EmptyStateProps {
  title: string;
  description: string;
  hint?: string;
  icon?: LucideIcon;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({
  title,
  description,
  hint,
  icon: Icon = SearchX,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-start gap-2 rounded-lg border border-dashed border-border bg-muted/25 px-4 py-5",
        className,
      )}
      data-testid="state-empty"
    >
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        {title}
      </div>
      <p className="max-w-prose text-xs leading-5 text-muted-foreground">{description}</p>
      {hint ? <p className="max-w-prose text-xs leading-5 text-muted-foreground/80">{hint}</p> : null}
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  );
}

interface PartialNoticeProps {
  title: string;
  description: string;
  detail?: string;
  className?: string;
}

/** Partial：部分数据源不可用，但规划仍然完成。 */
export function PartialNotice({ title, description, detail, className }: PartialNoticeProps) {
  return (
    <div
      className={cn(
        "flex gap-2.5 rounded-lg border border-warning/30 bg-warning-subtle px-3.5 py-3",
        className,
      )}
      data-testid="state-partial"
    >
      <Info className="mt-0.5 size-4 shrink-0 text-warning-subtle-foreground" aria-hidden />
      <div className="min-w-0 text-xs leading-5">
        <p className="font-medium text-warning-subtle-foreground">{title}</p>
        <p className="mt-0.5 text-warning-subtle-foreground/90">{description}</p>
        {detail ? <p className="mt-1 text-warning-subtle-foreground/75">{detail}</p> : null}
      </div>
    </div>
  );
}

interface ErrorStateProps {
  title: string;
  description: string;
  detail?: string;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}

/** Error：本次结果不可用，并说明下一步可以做什么。 */
export function ErrorState({
  title,
  description,
  detail,
  onRetry,
  retryLabel = "重新加载",
  className,
}: ErrorStateProps) {
  return (
    <div
      className={cn("rounded-lg border border-danger/25 bg-danger-subtle px-4 py-5", className)}
      data-testid="state-error"
    >
      <div className="flex items-center gap-2 text-sm font-medium text-danger-subtle-foreground">
        <ServerCrash className="size-4 shrink-0" aria-hidden />
        {title}
      </div>
      <p className="mt-2 max-w-prose text-xs leading-5 text-danger-subtle-foreground/90">{description}</p>
      {detail ? (
        <p className="mt-1.5 max-w-prose break-words text-[11px] leading-5 text-danger-subtle-foreground/70">
          {detail}
        </p>
      ) : null}
      {onRetry ? (
        <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
          <RefreshCw />
          {retryLabel}
        </Button>
      ) : null}
    </div>
  );
}

interface InlineWarningProps {
  title: string;
  description: string;
  className?: string;
}

/** 行内提示：数据本身可用，但有需要注意的地方。 */
export function InlineWarning({ title, description, className }: InlineWarningProps) {
  return (
    <div
      className={cn(
        "flex gap-2 rounded-md border border-warning/25 bg-warning-subtle/70 px-3 py-2 text-xs leading-5",
        className,
      )}
    >
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
      <div className="min-w-0">
        <span className="font-medium text-warning-subtle-foreground">{title}</span>
        <span className="text-warning-subtle-foreground/90"> {description}</span>
      </div>
    </div>
  );
}
