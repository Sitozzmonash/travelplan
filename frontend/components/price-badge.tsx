import { Clock3, TrendingUp } from "lucide-react";
import { cn } from "@/lib/utils";
import type { PriceType } from "@/types/plan";
import { formatCNY, formatClock } from "@/lib/format";

interface PriceBadgeProps {
  price: number | null | undefined;
  priceType: PriceType;
  /** Provider 查询时间，用于「查询于 14:32」。 */
  fetchedAt?: string | null;
  /** 价格口径说明，例如「2 人合计」。 */
  scope?: string;
  size?: "sm" | "md";
  className?: string;
}

/**
 * 价格 Badge（FRONTEND_DESIGN §12、§17）。
 * 实时价格必须同时给出查询时间，并明确提示「价格可能变化」。
 */
export function PriceBadge({ price, priceType, fetchedAt, scope, size = "sm", className }: PriceBadgeProps) {
  const clock = formatClock(fetchedAt);

  if (priceType === "unknown" || price === null || price === undefined) {
    return (
      <div className={cn("text-xs leading-5 text-muted-foreground", className)}>
        <span className="inline-flex items-center gap-1.5">
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px]">未取得价格</span>
          <span>该地点未返回实时票价，行程中按免费或现场购票处理。</span>
        </span>
      </div>
    );
  }

  const isRealtime = priceType === "realtime";

  return (
    <div className={cn("flex flex-wrap items-baseline gap-x-2 gap-y-1", className)} data-testid="price-badge">
      <span className={cn("tabular font-medium text-foreground", size === "md" ? "text-lg" : "text-sm")}>
        {formatCNY(price)}
      </span>
      {scope ? <span className="text-[11px] text-muted-foreground">{scope}</span> : null}
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px]",
          isRealtime
            ? "bg-success-subtle text-success-subtle-foreground"
            : "bg-muted text-muted-foreground",
        )}
      >
        {isRealtime ? <Clock3 className="size-3" aria-hidden /> : <TrendingUp className="size-3" aria-hidden />}
        {isRealtime ? "实时价格" : "估算费用"}
      </span>
      {isRealtime && clock ? <span className="tabular text-[11px] text-muted-foreground">查询于 {clock}</span> : null}
      <span className="text-[11px] text-muted-foreground/80">
        {isRealtime ? "价格可能变化" : "按行程口径估算"}
      </span>
    </div>
  );
}
