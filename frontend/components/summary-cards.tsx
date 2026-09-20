"use client";

import { ArrowRight, Hotel, Plane, ShieldCheck, Wallet } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { TransportOption, TripPlan } from "@/types/plan";
import { cn } from "@/lib/utils";
import { formatCNY, formatClock } from "@/lib/format";

interface SummaryCardsProps {
  plan: TripPlan;
  onOpenTransport: () => void;
  onOpenHotel: () => void;
  onOpenBudget: () => void;
  onOpenTrust: () => void;
}

/** 四张核心卡（FRONTEND_DESIGN §9）。所有数字都直接来自后端返回的 plan。 */
export function SummaryCards({
  plan,
  onOpenTransport,
  onOpenHotel,
  onOpenBudget,
  onOpenTrust,
}: SummaryCardsProps) {
  const outbound = plan.transport?.selected ?? null;
  const inbound = plan.transport?.inbound_selected ?? null;
  const transportCount =
    (plan.transport?.alternatives.length ?? 0) +
    (plan.transport?.inbound_alternatives.length ?? 0) +
    (plan.transport?.selected ? 1 : 0) +
    (plan.transport?.inbound_selected ? 1 : 0);

  const hotel = plan.hotel?.selected ?? null;
  const summary = plan.evidence_summary;
  const sourcesUsed = summary?.sources_used ?? plan.sources.length;
  const placesVerified =
    summary?.places_verified ??
    new Set(plan.days.flatMap((day) => day.items.map((item) => item.place_id)).filter(Boolean)).size;
  const filtered = summary?.low_trust_filtered ?? plan.decisions.filter((d) => d.status === "REJECT").length;

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="summary-cards">
      <SummaryCard
        icon={Plane}
        title="交通"
        onClick={onOpenTransport}
        actionLabel={transportCount ? `查看 ${transportCount} 个候选方案` : "查看交通方案"}
      >
        {outbound ? (
          <>
            <p className="truncate text-sm font-medium text-foreground">
              去程 {optionLabel(outbound)} · {formatCNY(outbound.price)} / 人
            </p>
            <p className="tabular mt-1 text-xs text-muted-foreground">
              {stationLabel(outbound, "departure")} {formatClock(outbound.departure_at)} →{" "}
              {stationLabel(outbound, "arrival")} {formatClock(outbound.arrival_at)}
            </p>
            {inbound ? (
              <p className="mt-1.5 truncate text-xs text-muted-foreground">
                回程 {optionLabel(inbound)} · {formatClock(inbound.departure_at)} →{" "}
                {formatClock(inbound.arrival_at)} · {formatCNY(inbound.price)} / 人
              </p>
            ) : (
              <p className="mt-1.5 text-xs text-muted-foreground">回程方案未返回，可稍后重新查询。</p>
            )}
          </>
        ) : (
          <p className="text-xs leading-5 text-muted-foreground">
            本次没有查到可用的大交通方案，因此行程中未包含城际交通安排。
          </p>
        )}
      </SummaryCard>

      <SummaryCard
        icon={Hotel}
        title="酒店"
        onClick={onOpenHotel}
        actionLabel={plan.hotel?.alternatives.length ? `查看其他 ${plan.hotel.alternatives.length} 个候选` : "查看酒店"}
      >
        {hotel ? (
          <>
            <p className="truncate text-sm font-medium text-foreground">{hotel.name}</p>
            <p className="tabular mt-1 text-xs text-muted-foreground">
              {hotel.nights ?? 0} 晚 · {formatCNY(hotel.price_per_night)} / 晚 · 合计 {formatCNY(hotel.total_price)}
            </p>
            <p className="mt-1.5 truncate text-xs text-muted-foreground">
              {hotel.business_area ?? "区域未标注"}
              {hotel.fetched_at ? ` · 查询于 ${formatClock(hotel.fetched_at)}` : ""}
            </p>
          </>
        ) : (
          <p className="text-xs leading-5 text-muted-foreground">
            暂时没有找到符合条件的酒店。已按预算与区域放宽过一次筛选，仍无结果。
          </p>
        )}
      </SummaryCard>

      <SummaryCard icon={Wallet} title="预算" onClick={onOpenBudget} actionLabel="查看预算明细">
        <p className="tabular text-sm font-medium text-foreground">
          预计总计 {formatCNY(plan.budget.projected_total)}
        </p>
        <p className="tabular mt-1 text-xs text-muted-foreground">
          预算 {formatCNY(plan.budget.budget_total)} · 剩余 {formatCNY(plan.budget.remaining)}
        </p>
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span className="tabular">实时价格 {formatCNY(plan.budget.known_real_cost)}</span>
          <span className="tabular">估算费用 {formatCNY(plan.budget.estimated_cost)}</span>
        </div>
        {plan.budget.status === "over_budget" ? (
          <p className="tabular mt-1.5 text-xs text-danger-subtle-foreground">
            预计超出 {formatCNY(Math.abs(plan.budget.remaining ?? 0))}
          </p>
        ) : null}
      </SummaryCard>

      <SummaryCard icon={ShieldCheck} title="可信度" onClick={onOpenTrust} actionLabel="查看来源与依据">
        <ul className="grid gap-1 text-xs text-muted-foreground">
          <li className="tabular">使用 {sourcesUsed} 条来源</li>
          <li className="tabular">{placesVerified} 个地点已验证</li>
          <li className="tabular">{filtered} 个低可信候选被过滤</li>
        </ul>
        {plan.sources.some((source) => source.status !== "OK") ? (
          <p className="mt-2 text-[11px] leading-5 text-warning-subtle-foreground">
            有 {plan.sources.filter((source) => source.status !== "OK").length} 个数据源本次为降级返回，已在来源中标注。
          </p>
        ) : null}
      </SummaryCard>
    </div>
  );
}

interface SummaryCardProps {
  icon: LucideIcon;
  title: string;
  actionLabel: string;
  onClick: () => void;
  children: React.ReactNode;
}

function SummaryCard({ icon: Icon, title, actionLabel, onClick, children }: SummaryCardProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group flex h-full flex-col rounded-xl border border-border bg-card p-4 text-left shadow-sm transition-colors",
        "hover:border-primary/35 focus-visible:border-primary/50 focus-visible:outline-none",
      )}
    >
      <span className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Icon className="size-3.5" aria-hidden />
        {title}
      </span>
      <div className="mt-2.5 min-w-0 flex-1">{children}</div>
      <span className="mt-3 inline-flex items-center gap-1 text-[11px] text-muted-foreground transition-colors group-hover:text-foreground">
        {actionLabel}
        <ArrowRight className="size-3" aria-hidden />
      </span>
    </button>
  );
}

function optionLabel(option: TransportOption | null): string {
  if (!option) return "—";
  return option.kind === "flight" ? option.flight_no : option.train_no;
}

function stationLabel(option: TransportOption | null, which: "departure" | "arrival"): string {
  if (!option) return "—";
  if (option.kind === "flight") {
    const value = which === "departure" ? option.departure_airport : option.arrival_airport;
    return value ?? "";
  }
  const value = which === "departure" ? option.origin_station : option.destination_station;
  return value ?? "";
}
