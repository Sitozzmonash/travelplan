"use client";

import { CalendarClock, Info, MapPin, Route, ShieldCheck, Timer } from "lucide-react";
import type { Evidence, ItineraryDay, ItineraryItem, SourceRef } from "@/types/plan";
import { formatCNY, formatDate, formatDistance, formatDuration, formatMode, legMinutes } from "@/lib/format";
import { ItemTypeIcon, ITEM_TYPE_LABELS } from "@/components/item-icon";
import { PriceBadge } from "@/components/price-badge";
import { ScoreRow } from "@/components/trust-badge";
import { SourceDetail } from "@/components/source-badge";
import { EmptyState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";

interface PlaceDetailDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: ItineraryItem | null;
  day: ItineraryDay | null;
  /** 当天在工作台里的显示名（Day N），由调用方给出，避免从 day_index 反推序号。 */
  dayLabel?: string;
  sources: SourceRef[];
  evidence: Evidence[];
  onOpenEvidence?: () => void;
}

const PRICE_TITLES: Record<ItineraryItem["type"], string> = {
  attraction: "门票",
  food: "餐饮",
  hotel: "住宿",
  transport: "票价",
  activity: "消费",
  free_time: "消费",
};

/** 地点 / 安排详情（FRONTEND_DESIGN §4 IA 中的 place drawer）。只展示后端返回的数据。 */
export function PlaceDetailDrawer({
  open,
  onOpenChange,
  item,
  day,
  dayLabel,
  sources,
  evidence,
  onOpenEvidence,
}: PlaceDetailDrawerProps) {
  const leg = item?.travel_from_previous ?? null;
  const legMins = legMinutes(leg);

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title={item ? item.name : "安排详情"}
      description={
        item
          ? [dayLabel, day?.date ? formatDate(day.date) : null, ITEM_TYPE_LABELS[item.type]]
              .filter((part): part is string => Boolean(part))
              .join(" · ")
          : undefined
      }
      footer={
        item ? (
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span>该条安排的数值均来自后端返回的行程数据，前端不做二次计算。</span>
            {item.evidence_ids.length ? (
              <button
                type="button"
                onClick={onOpenEvidence}
                className="inline-flex items-center gap-1 text-primary hover:underline"
              >
                <ShieldCheck className="size-3" aria-hidden />
                查看推荐依据
              </button>
            ) : null}
          </div>
        ) : null
      }
    >
      {item ? (
        <div className="space-y-6" data-testid="place-detail-drawer">
          <section className="space-y-3">
            <div className="flex items-start gap-3 rounded-lg border border-border/80 bg-muted/25 px-3.5 py-3">
              <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-card text-muted-foreground">
                <ItemTypeIcon type={item.type} transportMode={item.transport_mode} />
              </span>
              <div className="min-w-0 space-y-1.5">
                <p className="text-sm font-medium text-foreground">{item.name}</p>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                  <span className="inline-flex items-center gap-1">
                    <CalendarClock className="size-3" aria-hidden />
                    <span className="tabular">
                      {item.start_time ?? "--:--"}
                      {item.end_time ? ` – ${item.end_time}` : ""}
                    </span>
                  </span>
                  {item.duration_minutes ? (
                    <span className="inline-flex items-center gap-1">
                      <Timer className="size-3" aria-hidden />
                      建议停留 {formatDuration(item.duration_minutes)}
                    </span>
                  ) : null}
                  {item.area ? (
                    <span className="inline-flex items-center gap-1">
                      <MapPin className="size-3" aria-hidden />
                      {item.area}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>

            <PriceBadge
              price={item.price}
              priceType={item.price_type}
              fetchedAt={sources[0]?.fetched_at ?? null}
              scope={PRICE_TITLES[item.type]}
              size="md"
            />
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">从上一站到这里</h3>
            {leg ? (
              <div className="rounded-lg border border-border/80 bg-card px-3.5 py-3 text-xs leading-5">
                <p className="flex items-center gap-1.5 font-medium text-foreground">
                  <Route className="size-3.5 text-muted-foreground" aria-hidden />
                  {formatMode(leg.mode)} {legMins !== null ? formatDuration(legMins) : "时长未知"}
                </p>
                <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-muted-foreground">
                  <div className="flex gap-1.5">
                    <dt>距离</dt>
                    <dd className="tabular text-foreground/90">{formatDistance(leg.distance_meters)}</dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt>票价</dt>
                    <dd className="tabular text-foreground/90">
                      {leg.estimated_cost ? formatCNY(leg.estimated_cost) : "—"}
                    </dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt>路线来源</dt>
                    <dd className="text-foreground/90">{leg.verified ? "高德路线" : "估算"}</dd>
                  </div>
                </dl>
                {leg.note ? (
                  <p className="mt-2 flex gap-1.5 text-[11px] leading-5 text-warning-subtle-foreground">
                    <Info className="mt-0.5 size-3 shrink-0" aria-hidden />
                    {leg.note}
                  </p>
                ) : null}
              </div>
            ) : (
              <p className="rounded-lg border border-dashed border-border bg-muted/25 px-3.5 py-3 text-xs leading-5 text-muted-foreground">
                这条安排是当天的第一站，没有上一段交通记录。
              </p>
            )}
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">推荐说明</h3>
            <p className="text-xs leading-5 text-foreground">{item.reason}</p>
            {item.booking_note ? (
              <p className="flex gap-1.5 text-xs leading-5 text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                {item.booking_note}
              </p>
            ) : null}
          </section>

          <section className="space-y-3">
            <h3 className="text-xs font-medium text-muted-foreground">可信度评估</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              <ScoreRow
                label="Trust（可信度）"
                score={item.trust_score}
                caption="后端根据来源质量、独立复述与 POI 校验结果计算。"
                tone="trust"
              />
              <ScoreRow
                label="Ad Risk（广告风险）"
                score={item.ad_risk}
                caption="后端根据推广标识、发布密度与措辞雷同度计算。"
                tone="adRisk"
              />
            </div>
          </section>

          <section className="space-y-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-xs font-medium text-muted-foreground">数据来源</h3>
              <span className="text-[11px] text-muted-foreground">
                {sources.length} 个来源 · {evidence.length} 条证据
              </span>
            </div>
            {sources.length ? (
              <div className="space-y-2.5">
                {sources.map((source) => (
                  <SourceDetail key={source.source_id} source={source} />
                ))}
              </div>
            ) : (
              <EmptyState
                title="该条安排没有来源记录"
                description="它来自行程编排时的固定安排，没有对应的 Provider 查询记录，因此无法追溯查询条件。"
              />
            )}
          </section>
        </div>
      ) : null}
    </SidePanel>
  );
}
