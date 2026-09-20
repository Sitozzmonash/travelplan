"use client";

import { BedDouble, Building2, CalendarDays, MapPin, ShieldCheck, Star } from "lucide-react";
import { useMemo, useState } from "react";
import type { HotelOption, HotelPlan, SourceRef } from "@/types/plan";
import { Button } from "@/components/ui/button";
import { SidePanel } from "@/components/side-panel";
import { SectionCard } from "@/components/section-card";
import { EmptyState } from "@/components/state-views";
import { SourceBadge } from "@/components/source-badge";
import { ScoreRow } from "@/components/trust-badge";
import { cn } from "@/lib/utils";
import { formatCNY, formatClock, formatDate } from "@/lib/format";

const PRICE_FILTERS = [
  { id: "all", label: "不限价格", test: () => true },
  { id: "low", label: "¥300 以下 / 晚", test: (price: number | null) => price !== null && price < 300 },
  { id: "mid", label: "¥300 - 500 / 晚", test: (price: number | null) => price !== null && price >= 300 && price <= 500 },
  { id: "high", label: "¥500 以上 / 晚", test: (price: number | null) => price !== null && price > 500 },
] as const;

interface HotelCompareProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  hotel: HotelPlan | null;
  sources: SourceRef[];
  onOpenSource?: (sourceId: string) => void;
  onRequestChange?: (hotel: HotelOption) => void;
}

/** 酒店比较（FRONTEND_DESIGN §16）。筛选只作用于已返回的候选列表，不触发重新规划。 */
export function HotelCompare({
  open,
  onOpenChange,
  hotel,
  sources,
  onOpenSource,
  onRequestChange,
}: HotelCompareProps) {
  const [priceFilter, setPriceFilter] = useState<(typeof PRICE_FILTERS)[number]["id"]>("all");

  const candidates = useMemo(() => {
    const filter = PRICE_FILTERS.find((entry) => entry.id === priceFilter) ?? PRICE_FILTERS[0];
    return (hotel?.alternatives ?? []).filter((option) => filter.test(option.price_per_night ?? null));
  }, [hotel, priceFilter]);

  const selected = hotel?.selected ?? null;

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title="酒店候选比较"
      description={
        selected
          ? `最终选择「${selected.name}」。以下是本次查询到的其他候选，筛选只改变展示，不会重新规划。`
          : "本次没有取得可用的酒店候选。"
      }
      widthClassName="sm:max-w-2xl"
    >
      {!selected ? (
        <EmptyState
          title="暂时没有找到符合条件的酒店"
          description="已按入住日期与 2 人入住条件查询，但没有返回可用结果。你可以放宽价格或区域条件后重新规划。"
        />
      ) : (
        <div className="grid gap-4">
          <div>
            <p className="text-xs font-medium text-muted-foreground">最终选择</p>
            <div className="mt-2">
              <HotelOptionCard
                option={selected}
                sources={sources}
                onOpenSource={onOpenSource}
                highlighted
                reason={hotel?.selection_reason}
              />
            </div>
          </div>

          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs font-medium text-muted-foreground">
                其他候选（{hotel?.alternatives.length ?? 0}）
              </p>
              <div className="flex flex-wrap gap-1.5">
                {PRICE_FILTERS.map((filter) => (
                  <button
                    key={filter.id}
                    type="button"
                    onClick={() => setPriceFilter(filter.id)}
                    className={cn(
                      "rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                      priceFilter === filter.id
                        ? "border-primary/40 bg-accent text-accent-foreground"
                        : "border-border text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>
            </div>
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              筛选维度预留：价格 · 区域 · 评分 · 距主要行程。V1 只做展示筛选，修改需求会提交后端重新规划。
            </p>

            <div className="mt-2.5 grid gap-2.5">
              {candidates.length ? (
                candidates.map((option) => (
                  <HotelOptionCard
                    key={option.hotel_id ?? option.name}
                    option={option}
                    sources={sources}
                    onOpenSource={onOpenSource}
                    onChange={onRequestChange ? () => onRequestChange(option) : undefined}
                  />
                ))
              ) : (
                <EmptyState
                  title="当前筛选条件下没有候选酒店"
                  description="本次返回的候选都落在其他价格区间。把筛选调回「不限价格」即可看到全部候选，或提交修改需求让后端重新查询。"
                  action={
                    <Button variant="outline" size="sm" onClick={() => setPriceFilter("all")}>
                      清除筛选
                    </Button>
                  }
                />
              )}
            </div>
          </div>
        </div>
      )}
    </SidePanel>
  );
}

function HotelOptionCard({
  option,
  sources,
  onOpenSource,
  onChange,
  highlighted = false,
  reason,
}: {
  option: HotelOption;
  sources: SourceRef[];
  onOpenSource?: (sourceId: string) => void;
  onChange?: () => void;
  highlighted?: boolean;
  reason?: string;
}) {
  const source = sources.find((entry) => entry.source_id === option.source_id);

  return (
    <div
      className={cn(
        "rounded-lg border px-3.5 py-3",
        highlighted ? "border-primary/35 bg-accent/25" : "border-border",
      )}
      data-testid="hotel-option"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{option.name}</p>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            {option.business_area ? (
              <span className="inline-flex items-center gap-1">
                <Building2 className="size-3" aria-hidden />
                {option.business_area}
              </span>
            ) : null}
            {option.rating ? (
              <span className="tabular inline-flex items-center gap-1">
                <Star className="size-3" aria-hidden />
                {option.rating.toFixed(1)}
              </span>
            ) : null}
            {option.room_type ? <span>{option.room_type}</span> : null}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <p className="tabular text-sm font-medium text-foreground">{formatCNY(option.price_per_night)} / 晚</p>
          <p className="tabular text-[11px] text-muted-foreground">
            {option.nights ?? 0} 晚合计 {formatCNY(option.total_price)}
          </p>
        </div>
      </div>

      {option.distance_to_area ? (
        <p className="mt-2 flex items-start gap-1.5 text-[11px] leading-5 text-muted-foreground">
          <MapPin className="mt-0.5 size-3 shrink-0" aria-hidden />
          {option.distance_to_area}
        </p>
      ) : null}

      {option.cancellation_policy ? (
        <p className="mt-1 flex items-start gap-1.5 text-[11px] leading-5 text-muted-foreground">
          <CalendarDays className="mt-0.5 size-3 shrink-0" aria-hidden />
          {option.cancellation_policy}
        </p>
      ) : null}

      {typeof option.trust_score === "number" ? (
        <div className="mt-2.5 max-w-xs">
          <ScoreRow
            label="可信度（平台评分与点评综合）"
            score={option.trust_score}
            caption="由后端根据平台评分、点评数量与内容一致性计算。"
            tone="trust"
          />
        </div>
      ) : null}

      {reason ? <p className="mt-2 text-xs leading-5 text-muted-foreground">{reason}</p> : null}

      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <SourceBadge
            provider={source?.provider ?? option.provider}
            status={source?.status}
            onClick={onOpenSource && source ? () => onOpenSource(source.source_id) : undefined}
          />
          {option.fetched_at ? (
            <span className="tabular text-[11px] text-muted-foreground">
              查询于 {formatClock(option.fetched_at)}
            </span>
          ) : null}
        </div>
        {onChange ? (
          <Button variant="outline" size="sm" onClick={onChange}>
            换这家
          </Button>
        ) : null}
      </div>
    </div>
  );
}

interface SelectedHotelCardProps {
  hotel: HotelPlan | null;
  sources: SourceRef[];
  onOpenCompare: () => void;
  onOpenSource?: (sourceId: string) => void;
}

/** 最终选中酒店卡（FRONTEND_DESIGN §19）。不提供「立即预订」。 */
export function SelectedHotelCard({ hotel, sources, onOpenCompare, onOpenSource }: SelectedHotelCardProps) {
  const selected = hotel?.selected ?? null;
  const source = sources.find((entry) => entry.source_id === selected?.source_id);

  return (
    <SectionCard
      id="hotel"
      title="酒店"
      icon={BedDouble}
      action={
        <Button variant="outline" size="sm" onClick={onOpenCompare}>
          查看其他候选
        </Button>
      }
    >
      {!selected ? (
        <EmptyState
          title="暂时没有找到符合条件的酒店"
          description="已按入住日期、人数与区域条件查询，没有返回可用结果。本次行程未包含住宿安排，你可以调整区域或价格后重新规划。"
        />
      ) : (
        <div className="grid gap-3">
          <div>
            <p className="text-sm font-medium text-foreground">{selected.name}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {selected.address ?? selected.business_area ?? "地址未返回"}
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 text-xs sm:grid-cols-4">
            <Field label="入住 / 离店">
              <span className="tabular">
                {formatDate(selected.check_in)} - {formatDate(selected.check_out)}
              </span>
            </Field>
            <Field label="晚数">
              <span className="tabular">{selected.nights ?? 0} 晚</span>
            </Field>
            <Field label="价格">
              <span className="tabular">
                {formatCNY(selected.price_per_night)} / 晚 · 合计 {formatCNY(selected.total_price)}
              </span>
            </Field>
            <Field label="查询时间">
              <span className="tabular">{formatClock(selected.fetched_at) || "—"}</span>
            </Field>
          </dl>

          {selected.selection_reason ? (
            <p className="text-xs leading-5 text-muted-foreground">{selected.selection_reason}</p>
          ) : null}

          {selected.cancellation_policy ? (
            <p className="inline-flex items-start gap-1.5 text-[11px] leading-5 text-muted-foreground">
              <ShieldCheck className="mt-0.5 size-3 shrink-0" aria-hidden />
              {selected.cancellation_policy}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-muted-foreground">价格可能变化</span>
            {source ? (
              <SourceBadge
                provider={source.provider}
                status={source.status}
                onClick={onOpenSource ? () => onOpenSource(source.source_id) : undefined}
              />
            ) : null}
          </div>
        </div>
      )}
    </SectionCard>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-foreground">{children}</dd>
    </div>
  );
}
