"use client";

import { ArrowRight, Hotel, Plane, Wallet } from "lucide-react";
import type { TransportOption, TripPlan } from "@/types/plan";
import { formatCNY, formatClock } from "@/lib/format";

interface SummaryCardsProps {
  plan: TripPlan;
  onOpenTransport: () => void;
  onOpenHotel: () => void;
  onOpenBudget: () => void;
}

/**
 * 顶部一行摘要（feedback §3 / §5）：酒店 / 交通 / 预计花费 各一行，
 * 点击进对应抽屉或预算明细。详细内容只完整出现一次（抽屉 / 下方完整区块），
 * 这里永远只放短摘要，可信度 / 来源这类技术信息不占主视觉。
 */
export function SummaryCards({ plan, onOpenTransport, onOpenHotel, onOpenBudget }: SummaryCardsProps) {
  const outbound = plan.transport?.selected ?? null;
  const hotel = plan.hotel?.selected ?? null;
  const budget = plan.budget ?? null;

  return (
    <div className="grid gap-2 sm:grid-cols-3" data-testid="summary-cards">
      <SummarySegment icon={Hotel} label="酒店" onClick={onOpenHotel}>
        {hotel ? (
          <>
            <span className="truncate font-medium text-foreground">{hotel.name}</span>
            <span className="tabular shrink-0 text-muted-foreground">
              {hotel.business_area ?? ""}
              {hotel.price_per_night ? ` · ${formatCNY(hotel.price_per_night)}/晚` : ""}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">暂无可用酒店</span>
        )}
      </SummarySegment>

      <SummarySegment icon={Plane} label="交通" onClick={onOpenTransport}>
        {outbound ? (
          <>
            <span className="truncate font-medium text-foreground">
              {optionLabel(outbound)} · {formatCNY(outbound.price)} / 人
            </span>
            <span className="tabular shrink-0 text-muted-foreground">
              {formatClock(outbound.departure_at)} → {formatClock(outbound.arrival_at)}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">暂无可用大交通</span>
        )}
      </SummarySegment>

      <SummarySegment icon={Wallet} label="预计花费" onClick={onOpenBudget}>
        {budget ? (
          <>
            <span className="tabular font-medium text-foreground">{formatCNY(budget.projected_total)}</span>
            <span className="tabular shrink-0 text-muted-foreground">
              预算 {formatCNY(budget.budget_total)} · 剩余 {formatCNY(budget.remaining)}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">本次未返回预算汇总</span>
        )}
      </SummarySegment>
    </div>
  );
}

function SummarySegment({
  icon: Icon,
  label,
  onClick,
  children,
}: {
  icon: typeof Hotel;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex min-w-0 items-center gap-3 rounded-xl border border-border bg-card px-3.5 py-3 text-left shadow-sm transition-colors hover:border-primary/35 focus-visible:border-primary/50 focus-visible:outline-none"
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
        <Icon className="size-4" aria-hidden />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[11px] text-muted-foreground">{label}</span>
        <span className="flex min-w-0 items-baseline gap-2 text-sm leading-5">{children}</span>
      </span>
      <ArrowRight
        className="size-3.5 shrink-0 text-muted-foreground/60 transition-transform group-hover:translate-x-0.5 group-hover:text-foreground"
        aria-hidden
      />
    </button>
  );
}

function optionLabel(option: TransportOption | null): string {
  if (!option) return "—";
  return option.kind === "flight" ? option.flight_no : option.train_no;
}
