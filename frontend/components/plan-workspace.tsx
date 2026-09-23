"use client";

import { CircleAlert, Coffee, ListChecks, Map as MapIcon, Sparkles, Wallet } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import type { ReviseRequest, ReviseResult } from "@/types/api";
import type { Evidence, ItineraryDay, ItineraryItem, MapPoint, TripPlan } from "@/types/plan";
import { getEvidence, revisePlan } from "@/lib/api";
import { buildSourceRecords, providerLabel, recordsForItem, sourceStatusLabel } from "@/lib/display";
import { formatCNY, formatDistance, formatDuration, formatStamp } from "@/lib/format";
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
import { Button } from "@/components/ui/button";

interface PlanWorkspaceProps {
  plan: TripPlan;
}

/**
 * 行程工作区（FRONTEND_DESIGN §7–§21 / feedback §2–§12）。
 * 只负责把后端返回的 plan 组织成各区块并管理交互状态，
 * 不计算 Trust / Ad Risk / 可行性 / 预算，也不生成任何行程内容。
 *
 * 布局原则（feedback）：第一屏先回答「怎么玩」（目的地 / 天数 → 一行摘要 →
 * Day Tabs → 地图 + 时间线），再回答「为什么这么安排」（底部「为什么这样安排？」入口）。
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

  const mapHotel = useMemo(() => {
    const hotel = plan.hotel?.selected;
    return hotel && typeof hotel.lat === "number" && typeof hotel.lng === "number"
      ? { name: hotel.name, lat: hotel.lat, lng: hotel.lng }
      : null;
  }, [plan.hotel]);

  const foodStops = useMemo(() => (day?.items ?? []).filter((item) => item.type === "food"), [day]);

  const degradedProviders = useMemo(
    () =>
      Object.entries(plan.transport?.provider_status ?? {})
        .filter(([, status]) => status !== "OK")
        .map(([provider, status]) => `${providerLabel(provider)}（${sourceStatusLabel(status)}）`),
    [plan.transport],
  );

  const evidence = useMemo<Evidence[]>(
    () => (evidenceItem ? getEvidence(plan, evidenceItem.evidence_ids) : []),
    [plan, evidenceItem],
  );

  /** 「谁、什么时候、提供了什么数据」的来源记录：摘要区与抽屉共用同一份。 */
  const sourceRecords = useMemo(() => buildSourceRecords(plan), [plan]);
  const placeRecords = useMemo(() => recordsForItem(sourceRecords, placeItem), [sourceRecords, placeItem]);

  // P0-4：地图选中地点的旅行信息（feedback §11），从选中 itinerary item 计算，
  // 传给 TravelMap 的新可选 prop `selectedDetail`（order 与地图 marker 编号一致）。
  const selectedDetail = useMemo(() => {
    if (!selectedItemId) return null;
    const item = (day?.items ?? []).find((entry) => entry.id === selectedItemId);
    if (!item) return null;
    const order = mapPoints.findIndex((point) => point.id === item.id);
    return {
      name: item.name,
      order: order >= 0 ? order + 1 : 0,
      startTime: item.start_time ?? null,
      stayMinutes: item.duration_minutes ?? null,
      distanceFromPreviousMeters: item.travel_from_previous?.distance_meters ?? null,
      area: item.area ?? null,
    };
  }, [selectedItemId, day, mapPoints]);

  // P1-6：可信度 / 来源统计收进「为什么这样安排？」，不再占第一屏。
  const summary = plan.evidence_summary;
  const sourcesUsed = summary?.sources_used ?? plan.sources.length;
  const placesVerified =
    summary?.places_verified ??
    new Set(plan.days.flatMap((d) => d.items.map((item) => item.place_id)).filter(Boolean)).size;
  const filtered = summary?.low_trust_filtered ?? plan.decisions?.filter((d) => d.status === "REJECT").length ?? 0;
  const degradedSources = plan.sources.filter((source) => source.status !== "OK").length;

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

  // P2-8：点时间线项 → 地图 marker 高亮；若地图已滚出视野则把地图带回来。
  function revealMap() {
    const element = document.getElementById("map");
    if (!element) return;
    const rect = element.getBoundingClientRect();
    if (rect.top < 0 || rect.bottom > window.innerHeight) {
      element.scrollIntoView({ behavior: "smooth", block: "start" });
    }
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
  const unresolvedCount = warnings.filter((warning) => !warning.resolved).length;

  return (
    <div className="space-y-5 pb-24 lg:pb-6">
      {degradedProviders.length ? (
        <PartialNotice
          title={`${degradedProviders.join("、")} 本次为降级返回`}
          description="已使用其他可用来源完成规划，涉及的部分会在下方单独标注。"
          detail="降级不影响行程生成，但相关价格或通勤时间以出行前再次确认为准。"
        />
      ) : null}

      <TripSummary plan={plan} />

      <SummaryCards
        plan={plan}
        onOpenTransport={() => setTransportOpen(true)}
        onOpenHotel={() => setHotelOpen(true)}
        onOpenBudget={() => scrollTo("budget")}
      />

      {/* P0-1：Day Tabs 放到地图与时间线共同的最上层；切换同时控制地图 / 时间线 / 今日美食 / 当天可行性。 */}
      <DayTabs days={days} activeDayIndex={day?.day_index ?? activeDayIndex} onChange={setActiveDayIndex} />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
        <div className="space-y-5" id="itinerary">
          <div id="map">
            <SectionCard
              title={`${dayLabel} 路线总览`}
              icon={MapIcon}
              titleClassName="text-base"
              description="酒店、景点和餐饮都在同一张路线图里；点击编号可高亮对应安排。"
            >
              <TravelMap
                points={mapPoints}
                hotel={mapHotel}
                dayLabel={dayLabel}
                areaLabel={day?.area ?? ""}
                selectedId={selectedItemId}
                onSelect={setSelectedItemId}
                totalDistanceMeters={mapTotals.distance}
                totalMinutes={mapTotals.minutes}
                selectedDetail={selectedDetail}
              />
            </SectionCard>
          </div>

          <SectionCard
            title={`${dayLabel} 行程`}
            icon={ListChecks}
            titleClassName="text-base"
            description="按时间顺序走完这一天；点击地点可查看完整说明或调整安排。"
          >
            {/* P0-2：合并「今天的动线」与 Day Intent 为一行 Day Header，重复信息收敛。 */}
            <DayHeader day={day} dayLabel={dayLabel} totalMinutes={mapTotals.minutes} />
            <div className="mt-4">
              <ItineraryTimeline
                day={day}
                fetchedAt={fetchedAt}
                selectedItemId={selectedItemId}
                lockedItemIds={lockedItemIds}
                onSelectItem={(item) => {
                  // 点开一条安排 = 查看它的完整详情（推荐理由 / 停留 / 路线 / 价格 / 证据 / 风险），
                  // 同时让地图 marker 高亮；地图不在视野内时自动滚回地图。
                  setSelectedItemId(item.id);
                  openDetail(item);
                  revealMap();
                }}
                onOpenEvidence={openEvidence}
                onOpenDetail={openDetail}
                onOpenSources={(item) => openSources(item.source_ids[0])}
                onRevise={requestRevise}
              />
            </div>
          </SectionCard>
        </div>

        {/* P1-5：右列 Sticky，只保留 住宿 / 今日美食 / 预算摘要 / 关键 Warning；
            交通完整详情收进 TransportCompare，右栏不再放完整交通卡与来源卡。 */}
        <aside className="space-y-5 lg:sticky lg:top-4 lg:self-start">
          <SelectedHotelCard
            hotel={plan.hotel}
            sources={sources}
            onOpenCompare={() => setHotelOpen(true)}
            onOpenSource={openSources}
          />

          <FoodStops items={foodStops} onSelect={(item) => { setSelectedItemId(item.id); openDetail(item); }} />

          {plan.budget ? (
            <BudgetBrief budget={plan.budget} onOpenDetail={() => scrollTo("budget")} />
          ) : null}

          {unresolvedCount ? (
            <div className="flex items-start gap-2 rounded-xl border border-warning/25 bg-warning-subtle/70 px-3.5 py-3 text-xs leading-5">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
              <span className="text-warning-subtle-foreground">
                本次有 {unresolvedCount} 项未解决的问题，
                已在「时间与可行性检查」中逐条说明，并给出原计划与调整建议。
              </span>
            </div>
          ) : null}
        </aside>
      </div>

      <div id="feasibility">
        <FeasibilityAlert
          warnings={warnings}
          days={days}
          onJumpToItem={(dayIndex, itemId) => {
            // dayIndex 是 warning.day_index（后端值），必须按值选中，
            // 不能当成数组下标，否则 day_index 不从 0 开始时跳到别的一天。
            setActiveDayIndex(dayIndex);
            setSelectedItemId(itemId);
            scrollTo("map");
          }}
        />
      </div>

      {plan.budget ? (
        <BudgetCard budget={plan.budget} scopeLabel="2 人合计" />
      ) : (
        <PartialNotice
          title="这次没有返回预算汇总"
          description="交通、住宿与门票的候选仍然可见，但无法给出总额与预算余量。可以重新规划一次以获取完整预算。"
        />
      )}

      {/* P1-6：来源 / 可信度 / 规划依据 / Run ID 下沉到单个「为什么这样安排？」入口。 */}
      <SectionCard
        title="为什么这样安排？"
        icon={Sparkles}
        titleClassName="text-base"
        description="数据来源、可信度与规划依据都在这里，默认不占主界面。"
        action={
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={() => openSources()}>
              来源与可信度
            </Button>
            <Button variant="outline" size="sm" onClick={() => setAuditOpen(true)}>
              规划依据
            </Button>
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="tabular">使用 {sourcesUsed} 条来源</span>
          <span className="tabular">{placesVerified} 个地点已验证</span>
          <span className="tabular">{filtered} 个低可信候选被过滤</span>
          {degradedSources ? (
            <span className="text-warning-subtle-foreground">{degradedSources} 个数据源本次降级返回</span>
          ) : null}
        </div>
        <p className="mt-2 text-[11px] leading-5 text-muted-foreground/80">
          查询时间 {formatStamp(fetchedAt)} · 行程编号 {plan.run_id}
        </p>
      </SectionCard>

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
        evidenceRecords={placeRecords.evidence}
        sourceRecords={placeRecords.sources}
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
        records={sourceRecords}
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

/** P0-2：Day Header —— 合并「今天的动线」与 Day Intent，一行说清当天怎么玩。 */
function DayHeader({
  day,
  dayLabel,
  totalMinutes,
}: {
  day: ItineraryDay | null;
  dayLabel: string;
  totalMinutes: number;
}) {
  if (!day) return null;
  const places = day.items.filter((item) => item.type === "attraction" || item.type === "activity").length;
  const meals = day.items.filter((item) => item.type === "food").length;
  const parts = [
    places ? `${places} 个地点` : null,
    meals ? `${meals} 顿饭` : null,
    totalMinutes > 0 ? `移动约 ${formatDuration(totalMinutes)}` : null,
  ].filter(Boolean);

  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-primary/20 bg-accent/30 px-3.5 py-2.5">
      <p className="text-base font-semibold text-foreground">
        {dayLabel}
        {day.area ? ` · ${day.area}` : ""}
      </p>
      {parts.length ? <p className="text-xs text-muted-foreground">{parts.join(" · ")}</p> : null}
    </div>
  );
}

/** P1-5：右栏预算摘要 —— 只给一行核心数字，完整明细在下方 BudgetCard。 */
function BudgetBrief({
  budget,
  onOpenDetail,
}: {
  budget: NonNullable<TripPlan["budget"]>;
  onOpenDetail: () => void;
}) {
  const overBudget = budget.status === "over_budget";
  return (
    <SectionCard
      title="预算摘要"
      icon={Wallet}
      titleClassName="text-base"
      action={
        <button
          type="button"
          onClick={onOpenDetail}
          className="text-xs text-primary transition-colors hover:underline"
        >
          查看明细
        </button>
      }
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <span className="tabular font-medium text-foreground">{formatCNY(budget.projected_total)}</span>
        <span className="text-xs text-muted-foreground">
          预算 {formatCNY(budget.budget_total)} · 剩余 {formatCNY(budget.remaining)}
        </span>
        {overBudget ? (
          <span className="rounded bg-danger-subtle px-1.5 py-0.5 text-[11px] font-medium text-danger-subtle-foreground">
            超出预算
          </span>
        ) : null}
      </div>
    </SectionCard>
  );
}

function FoodStops({ items, onSelect }: { items: ItineraryItem[]; onSelect: (item: ItineraryItem) => void }) {
  if (!items.length) return null;

  return (
    <SectionCard
      title="今日美食"
      icon={Coffee}
      titleClassName="text-base"
      description="按当天位置和营业时间匹配；点开可查看推荐内容与来源。"
    >
      <div className="space-y-2.5">
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onSelect(item)}
            className="w-full rounded-lg border border-border/80 bg-card px-3 py-3 text-left transition-colors hover:border-primary/35 hover:bg-accent/25"
          >
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 truncate text-sm font-medium text-foreground">{item.name}</span>
              <span className="tabular shrink-0 text-xs text-muted-foreground">{item.start_time ?? "用餐时间待定"}</span>
            </div>
            <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-muted-foreground">{item.reason || "已根据当天动线匹配。"}</p>
            <div className="mt-2 flex flex-wrap gap-x-2.5 gap-y-1 text-xs text-muted-foreground">
              {item.travel_from_previous?.distance_meters ? <span>距上一站 {formatDistance(item.travel_from_previous.distance_meters)}</span> : null}
              {item.opening_hours ? <span>{item.opening_hours}</span> : null}
              {typeof item.price === "number" ? <span>人均约 {formatCNY(item.price)}</span> : null}
            </div>
          </button>
        ))}
      </div>
    </SectionCard>
  );
}
