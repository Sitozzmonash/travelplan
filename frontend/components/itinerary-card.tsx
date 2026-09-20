"use client";

import { ArrowDown, Heart, Info, Lock, MapPin, RefreshCw, Timer } from "lucide-react";
import type { ItineraryItem } from "@/types/plan";
import { cn } from "@/lib/utils";
import { formatCNY, formatDistance, formatDuration, formatMode, legMinutes } from "@/lib/format";
import { ITEM_TYPE_LABELS, ItemTypeIcon } from "@/components/item-icon";
import { PriceBadge } from "@/components/price-badge";
import { EvidenceBadge } from "@/components/trust-badge";

const PRICE_LABELS: Record<ItineraryItem["type"], string> = {
  attraction: "门票",
  food: "餐饮",
  hotel: "住宿",
  transport: "票价",
  activity: "消费",
  free_time: "消费",
};

interface TravelLegLineProps {
  item: ItineraryItem;
}

/** 段间交通（FRONTEND_DESIGN §11「↓ 步行 8 分钟」）。 */
export function TravelLegLine({ item }: TravelLegLineProps) {
  const leg = item.travel_from_previous;
  if (!leg) return null;

  const minutes = legMinutes(leg);
  const mode = formatMode(leg.mode);
  const degraded = !leg.verified;

  return (
    <div className="flex items-start gap-2 pl-1 sm:pl-2">
      <ArrowDown className="mt-0.5 size-3.5 shrink-0 text-border" aria-hidden />
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] leading-5">
        <span className={cn("font-medium", degraded ? "text-warning-subtle-foreground" : "text-muted-foreground")}>
          {mode} {minutes !== null ? formatDuration(minutes) : "时长未知"}
        </span>
        {leg.distance_meters ? (
          <span className="tabular text-muted-foreground/80">距上一地点 {formatDistance(leg.distance_meters)}</span>
        ) : null}
        {leg.estimated_cost ? (
          <span className="tabular text-muted-foreground/80">票价 {formatCNY(leg.estimated_cost)}</span>
        ) : null}
        {degraded ? <span className="text-warning-subtle-foreground">该段为估算</span> : null}
      </div>
    </div>
  );
}

interface ItineraryCardProps {
  item: ItineraryItem;
  selected?: boolean;
  fetchedAt?: string | null;
  scopeLabel?: string;
  onSelect?: () => void;
  onOpenEvidence?: () => void;
  onOpenSources?: () => void;
  onOpenDetail?: () => void;
  onRevise?: (action: "replace" | "remove" | "lock" | "relax_day" | "lower_budget") => void;
  locked?: boolean;
}

/** 行程 Item 卡片（FRONTEND_DESIGN §11、§12）。 */
export function ItineraryCard({
  item,
  selected = false,
  fetchedAt,
  scopeLabel,
  onSelect,
  onOpenEvidence,
  onOpenSources,
  onOpenDetail,
  onRevise,
  locked = false,
}: ItineraryCardProps) {
  const evidenceCount = item.evidence_ids.length;

  return (
    <article
      className={cn(
        "rounded-xl border bg-card px-3.5 py-3 transition-colors sm:px-4",
        selected ? "border-primary/45 bg-accent/35" : "border-border",
      )}
      data-testid="itinerary-card"
      onClick={onSelect}
    >
      <div className="flex items-start gap-3">
        <div className="flex w-12 shrink-0 flex-col pt-0.5">
          <span className="tabular text-sm font-medium text-foreground">{item.start_time ?? "--:--"}</span>
          {item.end_time ? (
            <span className="tabular mt-0.5 text-[11px] text-muted-foreground">{item.end_time}</span>
          ) : null}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="flex size-5 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
              <ItemTypeIcon type={item.type} transportMode={item.transport_mode} className="size-3.5" />
            </span>
            <h3 className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">{item.name}</h3>
            {locked ? (
              <span className="inline-flex items-center gap-1 rounded bg-accent px-1.5 py-0.5 text-[11px] text-accent-foreground">
                <Lock className="size-3" aria-hidden />
                已锁定
              </span>
            ) : null}
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span>{ITEM_TYPE_LABELS[item.type]}</span>
            {item.duration_minutes ? (
              <span className="inline-flex items-center gap-1">
                <Timer className="size-3" aria-hidden />
                建议 {formatDuration(item.duration_minutes)}
              </span>
            ) : null}
            {item.area ? (
              <span className="inline-flex items-center gap-1">
                <MapPin className="size-3" aria-hidden />
                {item.area}
              </span>
            ) : null}
            {onOpenDetail ? (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenDetail();
                }}
                className="text-primary transition-colors hover:underline"
              >
                地点详情
              </button>
            ) : null}
          </div>

          <div className="mt-2.5">
            <PriceBadge
              price={item.price}
              priceType={item.price_type}
              fetchedAt={fetchedAt}
              scope={item.price === null ? undefined : `${PRICE_LABELS[item.type]}${scopeLabel ? ` · ${scopeLabel}` : ""}`}
            />
          </div>

          {item.reason ? (
            <p className="mt-2.5 text-xs leading-5 text-muted-foreground">
              <span className="text-foreground/70">为什么推荐：</span>
              {item.reason}
            </p>
          ) : null}

          {item.booking_note ? (
            <p className="mt-1.5 flex items-start gap-1.5 text-[11px] leading-5 text-muted-foreground">
              <Info className="mt-0.5 size-3 shrink-0" aria-hidden />
              {item.booking_note}
            </p>
          ) : null}

          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {evidenceCount > 0 ? (
              <EvidenceBadge
                evidenceCount={evidenceCount}
                trust={item.trust_score}
                adRisk={item.ad_risk}
                onClick={onOpenEvidence}
              />
            ) : (
              <span className="rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground">
                无攻略证据 · 数据来自实时查询
              </span>
            )}
            {item.source_ids.length ? (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  if (onOpenSources) onOpenSources();
                  else onOpenEvidence?.();
                }}
                className="rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
              >
                查看 {item.source_ids.length} 个来源
              </button>
            ) : null}
          </div>

          {onRevise ? (
            <div className="mt-2.5 flex flex-wrap gap-1.5 border-t border-border/60 pt-2.5">
              <ReviseButton icon={RefreshCw} label="换一个" onClick={() => onRevise("replace")} />
              <ReviseButton icon={Heart} label="不想去" onClick={() => onRevise("remove")} />
              <ReviseButton
                icon={Lock}
                label={locked ? "取消锁定" : "锁定这个地点"}
                onClick={() => onRevise("lock")}
              />
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function ReviseButton({
  icon: Icon,
  label,
  onClick,
}: {
  icon: typeof RefreshCw;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/35 hover:text-foreground"
    >
      <Icon className="size-3" aria-hidden />
      {label}
    </button>
  );
}
