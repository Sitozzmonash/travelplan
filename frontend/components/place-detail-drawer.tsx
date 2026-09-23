"use client";

import { CalendarClock, Info, MapPin, Route, ShieldCheck, Timer } from "lucide-react";
import type { ItineraryDay, ItineraryItem } from "@/types/plan";
import type { SourceRecord } from "@/lib/display";
import { formatDate, formatDistance, formatDuration, formatMode, legMinutes } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ItemTypeIcon, ITEM_TYPE_LABELS } from "@/components/item-icon";
import { PriceBadge } from "@/components/price-badge";
import { ScoreRow } from "@/components/trust-badge";
import { EmptyState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";
import { SourceRecordCard } from "@/components/sources-drawer";

interface PlaceDetailDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: ItineraryItem | null;
  day: ItineraryDay | null;
  /** 当天在工作台里的显示名（Day N），由调用方给出，避免从 day_index 反推序号。 */
  dayLabel?: string;
  /** 这条安排直接引用的证据（精确匹配 evidence_ids）。 */
  evidenceRecords: SourceRecord[];
  /** 这条安排引用但没有正文的来源（例如机票、路线查询）。 */
  sourceRecords: SourceRecord[];
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

/**
 * 安排详情（FRONTEND_DESIGN §4 IA 中的 place drawer）。
 *
 * 一条安排点开后要能回答全部疑问：推荐理由、预计停留、营业时间、从上一站怎么走、
 * 价格与价格类型、可信度与广告风险、证据、风险提示。缺失的字段降级为不显示该段，
 * 而不是留一片空白或崩溃。
 */
export function PlaceDetailDrawer({
  open,
  onOpenChange,
  item,
  day,
  dayLabel,
  evidenceRecords,
  sourceRecords,
  onOpenEvidence,
}: PlaceDetailDrawerProps) {
  const leg = item?.travel_from_previous ?? null;
  const legMins = legMinutes(leg);
  const reasonPoints = item?.reason_points?.length ? item.reason_points : [];

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
            <span>以上数值均来自后端返回的行程数据，前端不做二次计算。</span>
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
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span className="inline-flex items-center gap-1">
                    <CalendarClock className="size-3" aria-hidden />
                    <span className="tabular">
                      {item.start_time ?? "--:--"}
                      {item.end_time ? ` – ${item.end_time}` : ""}
                    </span>
                  </span>
                  {item.area ? (
                    <span className="inline-flex items-center gap-1">
                      <MapPin className="size-3" aria-hidden />
                      {item.area}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>

            {/* 预计停留 / 营业时间：没有就整行不显示，而不是留空。 */}
            <dl className="grid gap-2 rounded-lg border border-border/80 bg-card px-3.5 py-3 text-xs leading-5">
              <div className="flex gap-2">
                <dt className="flex w-20 shrink-0 items-center gap-1 text-muted-foreground">
                  <Timer className="size-3" aria-hidden />
                  预计停留
                </dt>
                <dd className="text-foreground">
                  {item.duration_minutes ? formatDuration(item.duration_minutes) : "后端未给出停留时长"}
                </dd>
              </div>
              {item.opening_hours ? (
                <div className="flex gap-2">
                  <dt className="flex w-20 shrink-0 items-center gap-1 text-muted-foreground">
                    <CalendarClock className="size-3" aria-hidden />
                    营业时间
                  </dt>
                  <dd className="text-foreground">{item.opening_hours}</dd>
                </div>
              ) : null}
            </dl>

            <PriceBadge
              price={item.price}
              priceType={item.price_type}
              fetchedAt={sourceRecords[0]?.fetchedAt ?? evidenceRecords[0]?.fetchedAt ?? null}
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
                <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
                  <div className="flex gap-1.5">
                    <dt>距离</dt>
                    <dd className="tabular text-foreground/90">{formatDistance(leg.distance_meters)}</dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt>票价</dt>
                    <dd className="tabular text-foreground/90">
                      {leg.estimated_cost !== null && leg.estimated_cost !== undefined
                        ? `¥${leg.estimated_cost}`
                        : "—"}
                    </dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt>是否已核实</dt>
                    <dd
                      className={cn(
                        leg.verified ? "text-success-subtle-foreground" : "text-warning-subtle-foreground",
                      )}
                    >
                      {leg.verified ? "已核实（高德路线）" : "未核实，按估算处理"}
                    </dd>
                  </div>
                </dl>
                {leg.note ? (
                  <p className="mt-2 flex gap-1.5 text-xs leading-5 text-warning-subtle-foreground">
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
            <h3 className="text-xs font-medium text-muted-foreground">推荐理由</h3>
            <p className="text-xs leading-5 text-foreground">{item.reason}</p>
            {reasonPoints.length ? (
              <ul className="space-y-1.5">
                {reasonPoints.map((point) => (
                  <li key={point} className="flex gap-2 text-xs leading-5 text-muted-foreground">
                    <span className="mt-2 size-1 shrink-0 rounded-full bg-border" aria-hidden />
                    <span className="min-w-0">{point}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {item.booking_note ? (
              <p className="flex gap-1.5 text-xs leading-5 text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                {item.booking_note}
              </p>
            ) : null}
          </section>

          <section className="space-y-3">
            <h3 className="text-xs font-medium text-muted-foreground">可信度与广告风险</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              <ScoreRow
                label="可信度"
                score={item.trust_score}
                caption="后端根据来源质量、独立复述与地点校验结果计算。"
                tone="trust"
              />
              <ScoreRow
                label="广告风险"
                score={item.ad_risk}
                caption="后端根据推广标识、发布密度与措辞雷同度计算。"
                tone="adRisk"
              />
            </div>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">风险提示</h3>
            {item.risk_note ? (
              <div className="flex gap-2 rounded-lg border border-warning/25 bg-warning-subtle/70 px-3.5 py-3">
                <Info className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
                <p className="min-w-0 text-xs leading-5 text-warning-subtle-foreground">{item.risk_note}</p>
              </div>
            ) : (
              <p className="text-xs leading-5 text-muted-foreground">
                本次未发现需要注意的风险项。价格为查询时点结果，出行前可能变化。
              </p>
            )}
          </section>

          <section className="space-y-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-xs font-medium text-muted-foreground">证据条目</h3>
              <span className="text-[11px] text-muted-foreground">{evidenceRecords.length} 条</span>
            </div>
            {evidenceRecords.length ? (
              <div className="space-y-2.5">
                {evidenceRecords.map((record) => (
                  <SourceRecordCard key={record.key} record={record} />
                ))}
              </div>
            ) : (
              <EmptyState
                title="这条安排没有对应的攻略证据"
                description="它的依据是交通时刻表、票价与路线数据，没有小红书 / 抖音内容，因此没有可展示的正文。"
                hint="时刻表与票价为实时查询结果，出行前请以官方渠道为准。"
              />
            )}
          </section>

          {sourceRecords.length ? (
            <section className="space-y-2.5">
              <div className="flex items-baseline justify-between gap-2">
                <h3 className="text-xs font-medium text-muted-foreground">数据来源</h3>
                <span className="text-[11px] text-muted-foreground">{sourceRecords.length} 条</span>
              </div>
              <div className="space-y-2.5">
                {sourceRecords.map((record) => (
                  <SourceRecordCard key={record.key} record={record} />
                ))}
              </div>
            </section>
          ) : null}
        </div>
      ) : null}
    </SidePanel>
  );
}
