"use client";

import { CircleAlert, ListChecks, Map as MapIcon, Route } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import type { ReviseRequest, ReviseResult } from "@/types/api";
import type { Evidence, ItineraryItem, MapPoint, TripPlan } from "@/types/plan";
import { getEvidence, revisePlan } from "@/lib/api";
import { formatDuration } from "@/lib/format";
import { AuditDrawer } from "@/components/audit-drawer";
import { BudgetCard } from "@/components/budget-card";
import { DayTabs } from "@/components/day-tabs";
import { EvidenceDrawer } from "@/components/evidence-drawer";
import { FeasibilityAlert } from "@/components/feasibility-alert";
import { HotelCompare, SelectedHotelCard } from "@/components/hotel-compare";
import { ItineraryTimeline } from "@/components/itinerary-timeline";
import { MobileNav } from "@/components/mobile-nav";
import { PlaceDetailDrawer } from "@/components/place-detail-drawer";
import { ReviseDialog, type ReviseTarget } from "@/components/revise-dialog";
import { SectionCard } from "@/components/section-card";
import { SourcesDrawer } from "@/components/sources-drawer";
import { PartialNotice } from "@/components/state-views";
import { SummaryCards } from "@/components/summary-cards";
import { TransportCompare } from "@/components/transport-compare";
import { TravelMap } from "@/components/travel-map";
import { TripSummary } from "@/components/trip-summary";
import { providerLabel } from "@/components/source-badge";

interface PlanWorkspaceProps {
  plan: TripPlan;
}

/**
 * 行程工作区（FRONTEND_DESIGN §7–§21）。
 * 只负责把后端返回的 plan 组织成各区块并管理交互状态，
 * 不计算 Trust / Ad Risk / 可行性 / 预算，也不生成任何行程内容。
 */
export function PlanWorkspace({ plan }: PlanWorkspaceProps) {
  // 存的是后端的 day_index 值，不是数组下标：后端不保证 day_index 从 0 开始，
  // 按下标取会读到错误的一天，而 warning 的 day_index 也需要按值对齐。
  const [activeDayIndex, setActiveDayIndex] = useState<number>(() => plan.days[0]?.day_index ?? 0);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [lockedItemIds, setLockedItemIds] = useState<string[]>([]);

  const [evidenceItem, setEvidenceItem] = useState<ItineraryItem | null>(null);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [placeItem, setPlaceItem] = useState<ItineraryItem | null>(null);
  const [placeOpen, setPlaceOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [highlightSourceId, setHighlightSourceId] = useState<string | null>(null);
  const [auditOpen, setAuditOpen] = useState(false);
  const [transportOpen, setTransportOpen] = useState(false);
  const [hotelOpen, setHotelOpen] = useState(false);

  const [reviseTarget, setReviseTarget] = useState<ReviseTarget | null>(null);
  const [revising, setRevising] = useState(false);
  const [reviseMessage, setReviseMessage] = useState<string | null>(null);

  // intent / budget / warnings 在降级路径下可能缺失：这里按空值兜底，避免整页崩溃。
  // 用 useMemo 固定引用，否则 `?? []` 每次渲染都会生成新数组，连带下游 memo 失效。
  const days = useMemo(() => plan.days ?? [], [plan.days]);
  const warnings = useMemo(() => plan.warnings ?? [], [plan.warnings]);
  const sources = useMemo(() => plan.sources ?? [], [plan.sources]);
  const day = days.find((entry) => entry.day_index === activeDayIndex) ?? days[0] ?? null;
  const activeDayPosition = day ? Math.max(days.findIndex((entry) => entry.day_index === day.day_index), 0) : 0;
  const fetchedAt = useMemo(
    () => sources.find((source) => source.fetched_at)?.fetched_at ?? plan.generated_at,
    [sources, plan.generated_at],
  );

  const mapPoints: MapPoint[] = useMemo(
    () =>
      (day?.items ?? [])
        .filter((item): item is ItineraryItem & { lat: number; lng: number } =>
          typeof item.lat === "number" && typeof item.lng === "number",
        )
        .map((item, index) => ({ id: item.id, name: item.name, lat: item.lat, lng: item.lng, order: index + 1 })),
    [day],
  );

  const mapTotals = useMemo(() => {
    const legs = (day?.items ?? [])
      .map((item) => item.travel_from_previous)
      .filter((leg): leg is NonNullable<ItineraryItem["travel_from_previous"]> => Boolean(leg));
    return {
      distance: legs.reduce((sum, leg) => sum + (leg.distance_meters ?? 0), 0),
      minutes: legs.reduce((sum, leg) => sum + Math.ceil((leg.duration_seconds ?? 0) / 60), 0),
    };
  }, [day]);

  const degradedProviders = useMemo(
    () =>
      Object.entries(plan.transport?.provider_status ?? {})
        .filter(([, status]) => status !== "OK")
        .map(([provider, status]) => `${providerLabel(provider)}（${status}）`),
    [plan.transport],
  );

  const evidence = useMemo<Evidence[]>(
    () => (evidenceItem ? getEvidence(plan, evidenceItem.evidence_ids) : []),
    [plan, evidenceItem],
  );

  const sourcesOfItem = useCallback(
    (item: ItineraryItem | null) =>
      item ? sources.filter((source) => item.source_ids.includes(source.source_id)) : [],
    [sources],
  );

  function openEvidence(item: ItineraryItem) {
    setEvidenceItem(item);
    setEvidenceOpen(true);
  }

  function openDetail(item: ItineraryItem) {
    setPlaceItem(item);
    setPlaceOpen(true);
  }

  function openSources(sourceId?: string) {
    setHighlightSourceId(sourceId ?? null);
    setSourcesOpen(true);
  }

  function scrollTo(id: string) {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function requestRevise(item: ItineraryItem, action: ReviseTarget["action"]) {
    setReviseMessage(null);
    setReviseTarget({ action, itemId: item.id, dayIndex: day?.day_index ?? activeDayIndex });
  }

  const confirmRevise = useCallback(
    async (request: ReviseRequest) => {
      setRevising(true);
      try {
        const result: ReviseResult = await revisePlan(plan.run_id, request);
        setReviseMessage(result.message);
        if (request.action === "lock" && request.item_id) {
          setLockedItemIds((current) =>
            current.includes(request.item_id!)
              ? current.filter((id) => id !== request.item_id)
              : [...current, request.item_id!],
          );
        }
      } catch {
        setReviseMessage("修改请求没有提交成功。请稍后重试，本次行程内容未发生改变。");
      } finally {
        setRevising(false);
      }
    },
    [plan.run_id],
  );

  const dayLabel = day ? `Day ${activeDayPosition + 1}` : "";

  return (
    <div className="space-y-5 pb-24 lg:pb-6">
      <TripSummary plan={plan} onOpenAudit={() => setAuditOpen(true)} onOpenSources={() => openSources()} />

      {degradedProviders.length ? (
        <PartialNotice
          title={`${degradedProviders.join("、")} 本次为降级返回`}
          description="已使用其他可用来源完成规划，涉及的部分会在下方单独标注。"
          detail="降级不影响行程生成，但相关价格或通勤时间以出行前再次确认为准。"
        />
      ) : null}

      <SummaryCards
        plan={plan}
        onOpenTransport={() => setTransportOpen(true)}
        onOpenHotel={() => setHotelOpen(true)}
        onOpenBudget={() => scrollTo("budget")}
        onOpenTrust={() => openSources()}
      />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
        <div className="space-y-5" id="itinerary">
          <SectionCard
            title={`${dayLabel} 行程`}
            icon={ListChecks}
            description="按时间顺序排列，段间交通与停留时长都来自后端计算结果。"
          >
            <DayTabs days={days} activeDayIndex={day?.day_index ?? activeDayIndex} onChange={setActiveDayIndex} />
            <div className="mt-4">
              <ItineraryTimeline
                day={day}
                fetchedAt={fetchedAt}
                selectedItemId={selectedItemId}
                lockedItemIds={lockedItemIds}
                onSelectItem={(item) => {
                  setSelectedItemId(item.id);
                  if (typeof item.lat === "number" && typeof item.lng === "number") {
                    document.getElementById("map")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
                  }
                }}
                onOpenEvidence={openEvidence}
                onOpenDetail={openDetail}
                onOpenSources={(item) => openSources(item.source_ids[0])}
                onRevise={requestRevise}
              />
            </div>
          </SectionCard>

          <div id="feasibility">
            <FeasibilityAlert
              warnings={warnings}
              days={days}
              onJumpToItem={(dayIndex, itemId) => {
                // dayIndex 是 warning.day_index（后端值），必须按值选中，
                // 不能当成数组下标，否则 day_index 不从 0 开始时跳到别的一天。
                setActiveDayIndex(dayIndex);
                setSelectedItemId(itemId);
                scrollTo("itinerary");
              }}
            />
          </div>

          <div id="map">
            <SectionCard
              title={`${dayLabel} 路线示意`}
              icon={MapIcon}
              description="示意地图：按后端返回的经纬度绘制，用于核对当天动线，不代表真实底图。"
            >
              <TravelMap
                points={mapPoints}
                dayLabel={dayLabel}
                areaLabel={day?.area ?? ""}
                selectedId={selectedItemId}
                onSelect={setSelectedItemId}
                totalDistanceMeters={mapTotals.distance}
                totalMinutes={mapTotals.minutes}
              />
            </SectionCard>
          </div>
        </div>

        <div className="space-y-5">
          <SelectedHotelCard
            hotel={plan.hotel}
            sources={sources}
            onOpenCompare={() => setHotelOpen(true)}
            onOpenSource={openSources}
          />

          <SectionCard
            title="交通方案"
            icon={Route}
            description="去程与回程的选定方案，完整对比在交通比较中。"
            action={
              <button
                type="button"
                onClick={() => setTransportOpen(true)}
                className="text-xs text-primary hover:underline"
              >
                比较 {plan.transport ? plan.transport.alternatives.length + plan.transport.inbound_alternatives.length + 2 : 0} 个方案
              </button>
            }
          >
            <TransportSummary plan={plan} />
          </SectionCard>

          {warnings.filter((warning) => !warning.resolved).length ? (
            <div className="flex items-start gap-2 rounded-xl border border-warning/25 bg-warning-subtle/70 px-3.5 py-3 text-xs leading-5">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
              <span className="text-warning-subtle-foreground">
                本次有 {warnings.filter((warning) => !warning.resolved).length} 项未解决的问题，
                已在「时间与可行性检查」中逐条说明，并给出原计划与调整建议。
              </span>
            </div>
          ) : null}
        </div>
      </div>

      {plan.budget ? (
        <BudgetCard budget={plan.budget} scopeLabel="2 人合计" />
      ) : (
        <PartialNotice
          title="这次没有返回预算汇总"
          description="交通、住宿与门票的候选仍然可见，但无法给出总额与预算余量。可以重新规划一次以获取完整预算。"
        />
      )}

      <TransportCompare
        open={transportOpen}
        onOpenChange={setTransportOpen}
        transport={plan.transport}
        travelers={plan.intent?.travelers ?? 0}
        sources={sources}
        onOpenSource={openSources}
      />

      <HotelCompare
        open={hotelOpen}
        onOpenChange={setHotelOpen}
        hotel={plan.hotel}
        sources={sources}
        onOpenSource={openSources}
        onRequestChange={(hotel) => {
          setHotelOpen(false);
          setReviseMessage(null);
          setReviseTarget({ action: "replace", dayIndex: 0, itemId: hotel.hotel_id ?? undefined });
        }}
      />

      <EvidenceDrawer
        open={evidenceOpen}
        onOpenChange={setEvidenceOpen}
        item={evidenceItem}
        evidence={evidence}
      />

      <PlaceDetailDrawer
        open={placeOpen}
        onOpenChange={setPlaceOpen}
        item={placeItem}
        day={day}
        dayLabel={dayLabel}
        sources={sourcesOfItem(placeItem)}
        evidence={placeItem ? getEvidence(plan, placeItem.evidence_ids) : []}
        onOpenEvidence={() => {
          setPlaceOpen(false);
          if (placeItem) {
            setEvidenceItem(placeItem);
            setEvidenceOpen(true);
          }
        }}
      />

      <SourcesDrawer
        open={sourcesOpen}
        onOpenChange={setSourcesOpen}
        sources={sources}
        generatedAt={plan.generated_at}
        highlightSourceId={highlightSourceId}
      />

      <AuditDrawer open={auditOpen} onOpenChange={setAuditOpen} runId={plan.run_id} />

      <ReviseDialog
        open={reviseTarget !== null}
        onOpenChange={(next) => {
          if (!next) {
            setReviseTarget(null);
            setReviseMessage(null);
          }
        }}
        target={reviseTarget}
        lockedCount={lockedItemIds.length}
        submitting={revising}
        resultMessage={reviseMessage}
        onConfirm={confirmRevise}
      />

      <MobileNav onOpenAudit={() => setAuditOpen(true)} />
    </div>
  );
}

function TransportSummary({ plan }: { plan: TripPlan }) {
  const transport = plan.transport;
  if (!transport?.selected) {
    return (
      <p className="text-xs leading-5 text-muted-foreground">
        本次没有返回选定的大交通方案，通常是因为出行日期或路线暂无可售结果。可以调整日期后重新规划。
      </p>
    );
  }

  const rows = [
    { label: "去程", option: transport.selected },
    { label: "回程", option: transport.inbound_selected },
  ].filter((row): row is { label: string; option: NonNullable<typeof transport.selected> } => Boolean(row.option));

  return (
    <div className="space-y-3">
      {rows.map(({ label, option }) => {
        const title =
          option.kind === "flight"
            ? `${option.flight_no}${option.airline ? ` · ${option.airline}` : ""}`
            : `${option.train_no}${option.train_type ? ` · ${option.train_type}` : ""}`;
        return (
          <div key={label} className="rounded-lg border border-border/80 bg-card px-3.5 py-3">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-[11px] text-muted-foreground">{label}</span>
              <span className="text-xs font-medium text-foreground">{title}</span>
            </div>
            <p className="tabular mt-1.5 text-xs text-muted-foreground">
              {option.departure_at?.slice(11, 16) ?? "--:--"} – {option.arrival_at?.slice(11, 16) ?? "--:--"}
              {option.duration_minutes ? ` · ${formatDuration(option.duration_minutes)}` : ""}
            </p>
            {option.selection_reason ? (
              <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{option.selection_reason}</p>
            ) : null}
          </div>
        );
      })}
      <p className="text-xs leading-5 text-muted-foreground">{transport.selection_reason}</p>
    </div>
  );
}
