"use client";

import { ArrowRight, Clock3, Plane, Route, TrainFront } from "lucide-react";
import type { SourceRef, TransportOption, TransportPlan } from "@/types/plan";
import { Tabs, TabsList, TabsContent, TabsTrigger } from "@/components/ui/tabs";
import { SidePanel } from "@/components/side-panel";
import { EmptyState } from "@/components/state-views";
import { SourceBadge } from "@/components/source-badge";
import { cn } from "@/lib/utils";
import { formatCNY, formatClock, formatDateTime, formatDuration } from "@/lib/format";

interface TransportCompareProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  transport: TransportPlan | null;
  travelers: number;
  sources: SourceRef[];
  onOpenSource?: (sourceId: string) => void;
}

/** 交通比较（FRONTEND_DESIGN §15）：分「推荐 / 飞机 / 高铁」，并给出为什么推荐。 */
export function TransportCompare({
  open,
  onOpenChange,
  transport,
  travelers,
  sources,
  onOpenSource,
}: TransportCompareProps) {
  const outbound = transport?.selected ? [transport.selected, ...transport.alternatives] : [];
  const inbound = transport?.inbound_selected
    ? [transport.inbound_selected, ...transport.inbound_alternatives]
    : [];
  const flights = [...outbound, ...inbound].filter((option) => option.kind === "flight");
  const trains = [...outbound, ...inbound].filter((option) => option.kind === "train");

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title="交通方案比较"
      description={`共 ${outbound.length + inbound.length} 个候选方案（去程 + 回程）。价格与时间来自 Provider 实时查询，可能变化。`}
      widthClassName="sm:max-w-2xl"
    >
      {!transport || (!transport.selected && !transport.inbound_selected) ? (
        <EmptyState
          title="暂时没有可比较的交通方案"
          description="本次没有从机票与高铁 Provider 取得可用结果，因此没有可比较的方案。行程中不会包含城际交通安排，你可以稍后重新规划。"
        />
      ) : (
        <Tabs defaultValue="recommended" className="gap-4">
          <TabsList className="w-full">
            <TabsTrigger value="recommended">推荐</TabsTrigger>
            <TabsTrigger value="flight">飞机（{flights.length}）</TabsTrigger>
            <TabsTrigger value="train">高铁（{trains.length}）</TabsTrigger>
          </TabsList>

          <TabsContent value="recommended" className="grid gap-4">
            {transport.selection_reason ? (
              <div className="rounded-lg border border-primary/25 bg-accent/40 px-3.5 py-3">
                <p className="text-xs font-medium text-accent-foreground">为什么推荐当前方案</p>
                <p className="mt-1.5 text-xs leading-5 text-accent-foreground/90">{transport.selection_reason}</p>
              </div>
            ) : null}
            <OptionGroup
              title="去程"
              options={transport.selected ? [transport.selected] : []}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
              recommended
            />
            <OptionGroup
              title="回程"
              options={transport.inbound_selected ? [transport.inbound_selected] : []}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
              recommended
            />
          </TabsContent>

          <TabsContent value="flight" className="grid gap-4">
            <OptionGroup
              title="去程航班"
              options={outbound.filter((option) => option.kind === "flight")}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
              selectedId={transport.selected?.kind === "flight" ? transport.selected.flight_no : null}
            />
            <OptionGroup
              title="回程航班"
              options={inbound.filter((option) => option.kind === "flight")}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
            />
          </TabsContent>

          <TabsContent value="train" className="grid gap-4">
            <OptionGroup
              title="去程车次"
              options={outbound.filter((option) => option.kind === "train")}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
            />
            <OptionGroup
              title="回程车次"
              options={inbound.filter((option) => option.kind === "train")}
              travelers={travelers}
              sources={sources}
              onOpenSource={onOpenSource}
              selectedId={
                transport.inbound_selected?.kind === "train" ? transport.inbound_selected.train_no : null
              }
            />
          </TabsContent>
        </Tabs>
      )}
    </SidePanel>
  );
}

interface OptionGroupProps {
  title: string;
  options: TransportOption[];
  travelers: number;
  sources: SourceRef[];
  onOpenSource?: (sourceId: string) => void;
  selectedId?: string | null;
  recommended?: boolean;
}

function OptionGroup({
  title,
  options,
  travelers,
  sources,
  onOpenSource,
  selectedId,
  recommended = false,
}: OptionGroupProps) {
  if (!options.length) {
    return (
      <div>
        <GroupTitle>{title}</GroupTitle>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          这个方向上没有取得可用的候选方案。
        </p>
      </div>
    );
  }

  return (
    <div>
      <GroupTitle>{title}</GroupTitle>
      <div className="mt-2 grid gap-2.5">
        {options.map((option) => (
          <TransportOptionCard
            key={`${title}-${option.kind}-${option.kind === "flight" ? option.flight_no : option.train_no}`}
            option={option}
            travelers={travelers}
            sources={sources}
            onOpenSource={onOpenSource}
            highlight={recommended || selectedId === optionIdentifier(option)}
          />
        ))}
      </div>
    </div>
  );
}

function GroupTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-xs font-medium text-muted-foreground">{children}</h3>;
}

function optionIdentifier(option: TransportOption): string {
  return option.kind === "flight" ? option.flight_no : option.train_no;
}

interface TransportOptionCardProps {
  option: TransportOption;
  travelers: number;
  sources: SourceRef[];
  onOpenSource?: (sourceId: string) => void;
  highlight?: boolean;
}

export function TransportOptionCard({
  option,
  travelers,
  sources,
  onOpenSource,
  highlight = false,
}: TransportOptionCardProps) {
  const isFlight = option.kind === "flight";
  const Icon = isFlight ? Plane : TrainFront;

  const transferMinutes = option.airport_transfer_minutes ?? null;
  const doorToDoor =
    option.door_to_door_minutes ??
    (option.duration_minutes !== null && option.duration_minutes !== undefined && transferMinutes !== null
      ? option.duration_minutes + transferMinutes
      : null);

  const source = sources.find((entry) => entry.source_id === option.source_id);
  const perPerson = option.price ?? null;

  return (
    <div
      className={cn(
        "rounded-lg border px-3.5 py-3",
        highlight ? "border-primary/35 bg-accent/25" : "border-border",
      )}
      data-testid="transport-option"
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <Icon className="size-3.5" aria-hidden />
        </span>
        <span className="text-sm font-medium text-foreground">{optionIdentifier(option)}</span>
        {option.kind === "flight" && option.airline ? (
          <span className="text-xs text-muted-foreground">{option.airline}</span>
        ) : null}
        {option.kind === "train" && option.train_type ? (
          <span className="text-xs text-muted-foreground">{option.train_type}</span>
        ) : null}
        {highlight ? (
          <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">当前选择</span>
        ) : null}
        {option.kind === "flight" && option.is_direct ? (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">直飞</span>
        ) : null}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-foreground">
        <span className="tabular">{stationLabel(option, "from")}</span>
        <span className="tabular font-medium">{formatClock(option.departure_at)}</span>
        <ArrowRight className="size-3.5 text-muted-foreground" aria-hidden />
        <span className="tabular">{stationLabel(option, "to")}</span>
        <span className="tabular font-medium">{formatClock(option.arrival_at)}</span>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Clock3 className="size-3" aria-hidden />
          {isFlight ? "飞行" : "运行"} {formatDuration(option.duration_minutes)}
        </span>
        {transferMinutes !== null ? (
          <span className="tabular">{isFlight ? "机场" : "车站"}往返预计 +{transferMinutes} 分钟</span>
        ) : null}
        {doorToDoor !== null ? (
          <span className="tabular inline-flex items-center gap-1 text-foreground/80">
            <Route className="size-3" aria-hidden />
            门到门预计 {formatDuration(doorToDoor)}
          </span>
        ) : null}
      </div>

      <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {perPerson !== null ? (
          <>
            <span className="tabular text-sm font-medium text-foreground">{formatCNY(perPerson)} / 人</span>
            {travelers > 1 ? (
              <span className="tabular text-[11px] text-muted-foreground">
                {travelers} 人合计 {formatCNY(perPerson * travelers)}
              </span>
            ) : null}
          </>
        ) : (
          <span className="text-xs text-muted-foreground">未取得票价</span>
        )}
        {option.kind === "train" && option.price_range ? (
          <span className="tabular text-[11px] text-muted-foreground">
            {Object.entries(option.price_range)
              .filter(([, value]) => typeof value === "number")
              .map(([seat, value]) => `${seat} ${formatCNY(value as number)}`)
              .join(" · ")}
          </span>
        ) : null}
        {option.kind === "flight" && option.remaining_seats !== null && option.remaining_seats !== undefined ? (
          <span className="tabular text-[11px] text-muted-foreground">余票 {option.remaining_seats} 张</span>
        ) : null}
      </div>

      {option.kind === "train" && option.seats ? (
        <p className="tabular mt-1 text-[11px] text-muted-foreground">
          余票：
          {Object.entries(option.seats)
            .map(([seat, value]) => `${seat} ${value ?? "未知"}`)
            .join(" · ")}
        </p>
      ) : null}

      {option.price_note ? (
        <p className="mt-1.5 text-[11px] leading-5 text-warning-subtle-foreground">{option.price_note}</p>
      ) : null}

      {option.selection_reason ? (
        <p className="mt-2 text-xs leading-5 text-muted-foreground">{option.selection_reason}</p>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        {source ? (
          <SourceBadge
            provider={source.provider}
            status={source.status}
            onClick={onOpenSource ? () => onOpenSource(source.source_id) : undefined}
          />
        ) : (
          <SourceBadge provider={option.provider} onClick={undefined} />
        )}
        {option.fetched_at ? (
          <span className="tabular text-[11px] text-muted-foreground">查询于 {formatDateTime(option.fetched_at)}</span>
        ) : null}
      </div>
    </div>
  );
}

function stationLabel(option: TransportOption, which: "from" | "to"): string {
  if (option.kind === "flight") {
    const value = which === "from" ? option.departure_airport : option.arrival_airport;
    return value ?? (which === "from" ? (option.origin ?? "") : (option.destination ?? ""));
  }
  const value = which === "from" ? option.origin_station : option.destination_station;
  return value ?? "";
}
