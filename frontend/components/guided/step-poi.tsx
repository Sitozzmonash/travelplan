"use client";

import { Ban, Check, ChevronDown, ChevronUp, Heart, LoaderCircle, MapPin, RefreshCw, Sparkles, Star, WandSparkles } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { EmptyState, InlineWarning, LoadingState, PartialNotice } from "@/components/state-views";
import { hasSocialDegradation } from "@/lib/sessions";
import { cn } from "@/lib/utils";
import type { PlaceCandidate, PoiSelection, SessionView } from "@/types/session";
import type { PoiBulkMode } from "./draft";
import { categoryIcon, categoryLabel, discoveryPlaces, groupPlaces, poiActionLabels } from "./options";
import { DiscoveryResearch, HotelAreaRecommendations } from "./discovery-research";

/**
 * Step 4 想去哪里（§8 / §17）。
 *
 * 硬要求：
 * - 攻略没抓完也绝不能卡住用户：显示 skeleton + 「正在整理攻略，已找到 X 个地点」，
 *   并允许「都随便，继续」。
 * - 社交源全挂时给 Partial 提示，页面照常可用（还能勾已有地点，或全部交给系统）。
 * - 地点来自攻略抽取，不在前端写死；每张卡都能标 MUST / WANT / REJECT。
 */

const VISIBLE_PER_GROUP = 5;

interface StepPoiProps {
  session: SessionView | null;
  settled: boolean;
  unusable: boolean;
  pollStalled: boolean;
  selections: Record<string, PoiSelection>;
  expanded: string[];
  bulk: PoiBulkMode | null;
  onSelect: (placeId: string, state: PoiSelection | null) => void;
  onBulk: (mode: PoiBulkMode) => void;
  onToggleExpand: (key: string) => void;
  onRetryDiscovery: () => void;
}

export function StepPoi({
  session,
  settled,
  unusable,
  pollStalled,
  selections,
  expanded,
  bulk,
  onSelect,
  onBulk,
  onToggleExpand,
  onRetryDiscovery,
}: StepPoiProps) {
  const places = discoveryPlaces(session);
  const groups = groupPlaces(session);
  const evidence = session?.evidence_summary;
  const candidateCount = evidence?.total_candidates ?? places.length;

  const degradations = session?.degradations ?? [];
  const socialFailed = hasSocialDegradation(degradations);
  const partialDetail =
    !socialFailed &&
    session !== null &&
    (session.discovery_status === "PARTIAL" ||
      session.discovery_status === "FAILED" ||
      degradations.length > 0)
      ? (degradations[0] ?? "部分数据源本次没有返回内容。")
      : null;

  const mustCount = countState(selections, "MUST");
  const wantCount = countState(selections, "WANT");
  const rejectCount = countState(selections, "REJECT");

  return (
    <div className="grid gap-5">
      <DiscoveryResearch session={session} settled={settled} />

      {settled ? <HotelAreaRecommendations areas={session?.hotel_areas ?? []} /> : null}

      <div className="grid gap-2.5 rounded-xl border border-border bg-muted/30 p-3 sm:p-4">
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <WandSparkles className="size-3.5 shrink-0" aria-hidden />
          一条也不想选也可以：下面两个开关会直接把决定交给系统。
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <BulkButton
            label="都随便，帮我安排"
            hint="全部交给系统按证据与路线挑"
            selected={bulk === "auto"}
            disabled={unusable}
            onClick={() => onBulk("auto")}
          />
          <BulkButton
            label="只安排最值得去的"
            hint="每个类别取证据最充分的几个"
            selected={bulk === "best"}
            disabled={unusable || places.length === 0}
            onClick={() => onBulk("best")}
          />
        </div>
        <p className="text-[11px] text-muted-foreground">
          已勾选：必去 {mustCount} · 想去 {wantCount} · 不感兴趣 {rejectCount}
          {bulk === "auto" ? "（当前为「都随便，帮我安排」）" : ""}
          {bulk === "best" ? "（当前为「只安排最值得去的」）" : ""}
        </p>
      </div>

      {pollStalled ? (
        <div className="grid gap-2">
          <InlineWarning
            title="整理攻略的时间比预期长。"
            description="可以继续往下走：正式规划时会再次尝试抓取，你也可以在这里重试。"
          />
          <div className="flex justify-end">
            <Button variant="outline" size="sm" onClick={onRetryDiscovery}>
              <RefreshCw />
              重试整理攻略
            </Button>
          </div>
        </div>
      ) : null}

      {partialDetail ? (
        <PartialNotice
          title="攻略只拿到了一部分"
          description={partialDetail}
          detail="页面照常可用：已整理出的地点可以先勾选，也可以直接「都随便，继续」，正式规划会继续补齐。"
        />
      ) : null}

      {socialFailed ? (
        <PartialNotice
          title="没有拿到社交攻略"
          description="小红书 / 抖音这次没有返回可用内容，已经改用高德与网页数据继续整理，页面照常可用。"
          detail="可以先勾选已有的地点，或直接点「都随便，继续」，交给系统在正式规划时再试一次。"
        />
      ) : null}

      {!settled && !unusable ? (
        <div className="grid gap-3">
          <LoadingState
            title={`正在整理攻略，已找到 ${places.length} 个地点`}
            description="系统正在把小红书 / 抖音 / 网页攻略里的地点去重、核实并归类。"
            detail="不用等它跑完：现在就可以点「都随便，继续」，也可以先在已出现的卡片上勾选。"
          />
          <SkeletonGroups />
        </div>
      ) : null}

      {settled && places.length === 0 ? (
        <EmptyState
          title="这次没有整理出可选的地点"
          description={
            session?.degradations.length
              ? `攻略源本次大多不可用：${session.degradations[0]}`
              : "攻略里没有抽取到可核实的地点，可能是内容过少或全部被判定为推广。"
          }
          hint="不影响继续：正式规划会用高德与网页数据自己找地方，你也可以点「都随便，继续」。"
          action={
            <Button variant="outline" size="sm" onClick={onRetryDiscovery}>
              <RefreshCw />
              重新整理攻略
            </Button>
          }
        />
      ) : null}

      {places.length > 0 ? (
        <div className="grid gap-5">
          {groups.map((group) => {
            const isExpanded = expanded.includes(group.key);
            const visible = isExpanded ? group.places : group.places.slice(0, VISIBLE_PER_GROUP);
            const Icon = categoryIcon(group.key);
            return (
              <section key={group.key} className="grid gap-2.5">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="flex items-center gap-1.5 text-sm font-medium text-foreground">
                    <Icon className="size-3.5 text-muted-foreground" aria-hidden />
                    {group.label}
                    <span className="text-[11px] font-normal text-muted-foreground">{group.places.length} 个</span>
                  </h3>
                  {group.places.length > VISIBLE_PER_GROUP ? (
                    <button
                      type="button"
                      onClick={() => onToggleExpand(group.key)}
                      className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
                    >
                      {isExpanded ? (
                        <>
                          收起
                          <ChevronUp className="size-3" aria-hidden />
                        </>
                      ) : (
                        <>
                          查看更多（共 {group.places.length} 个）
                          <ChevronDown className="size-3" aria-hidden />
                        </>
                      )}
                    </button>
                  ) : null}
                </div>

                <div className="grid gap-2.5 sm:grid-cols-2">
                  {visible.map((place) => (
                    <PlaceCard
                      key={place.place_id}
                      place={place}
                      state={selections[place.place_id] ?? null}
                      disabled={unusable}
                      onSelect={onSelect}
                    />
                  ))}
                </div>
              </section>
            );
          })}

          {candidateCount > places.length ? (
            <p className="text-[11px] text-muted-foreground">
              本次共取得 {candidateCount} 个候选中转结果，已筛选出 {places.length} 个可核实地点展示。
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function countState(selections: Record<string, PoiSelection>, state: PoiSelection): number {
  return Object.values(selections).filter((item) => item === state).length;
}

function BulkButton({
  label,
  hint,
  selected,
  disabled,
  onClick,
}: {
  label: string;
  hint: string;
  selected: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected ? "border-primary/50 bg-accent text-accent-foreground" : "border-border bg-card hover:border-primary/30",
      )}
    >
      <WandSparkles className={cn("size-3.5 shrink-0", selected ? "text-primary" : "text-muted-foreground")} aria-hidden />
      <span className="min-w-0">
        <span className="block text-xs font-medium">{label}</span>
        <span className="mt-0.5 block text-[11px] text-muted-foreground">{hint}</span>
      </span>
    </button>
  );
}

function PlaceCard({
  place,
  state,
  disabled,
  onSelect,
}: {
  place: PlaceCandidate;
  state: PoiSelection | null;
  disabled: boolean;
  onSelect: (placeId: string, state: PoiSelection | null) => void;
}) {
  const labels = poiActionLabels(place.category);
  const cat = categoryLabel(place.category, place.category_label);
  const area = place.area ?? place.district;
  const risk = adRiskLabel(place.ad_risk);
  const trust = trustLabel(place.trust_score);

  return (
    <article
      className={cn(
        "flex flex-col gap-2 rounded-xl border px-3.5 py-3 transition-colors",
        state === "MUST" && "border-primary/50 bg-accent/60",
        state === "WANT" && "border-info/40 bg-info-subtle/60",
        state === "REJECT" && "border-dashed border-border bg-muted/30 opacity-70",
        !state && "border-border bg-card",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <h4 className="min-w-0 text-sm font-medium text-foreground">{place.name}</h4>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {typeof place.evidence_count === "number" ? `来自 ${place.evidence_count} 篇攻略` : "攻略提及"}
        </span>
      </div>

      {place.reason ? <p className="text-xs leading-5 text-muted-foreground">{place.reason}</p> : null}

      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
        <Tag>{cat}</Tag>
        {area ? (
          <Tag>
            <MapPin className="size-3" aria-hidden />
            {area}
          </Tag>
        ) : null}
        {trust ? (
          <Tag>
            <Star className="size-3" aria-hidden />
            {trust}
          </Tag>
        ) : null}
        {place.amap_verified ? <Tag>地点已核实</Tag> : null}
        {risk ? <Tag className="text-warning-subtle-foreground">{risk}</Tag> : null}
      </div>

      <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
        <PoiButton
          label={labels.must}
          icon={Check}
          selected={state === "MUST"}
          disabled={disabled}
          onClick={() => onSelect(place.place_id, state === "MUST" ? null : "MUST")}
        />
        <PoiButton
          label={labels.want}
          icon={Heart}
          selected={state === "WANT"}
          disabled={disabled}
          onClick={() => onSelect(place.place_id, state === "WANT" ? null : "WANT")}
        />
        <PoiButton
          label={labels.reject}
          icon={Ban}
          selected={state === "REJECT"}
          disabled={disabled}
          onClick={() => onSelect(place.place_id, state === "REJECT" ? null : "REJECT")}
        />
      </div>
    </article>
  );
}

function PoiButton({
  label,
  icon: Icon,
  selected,
  disabled,
  onClick,
}: {
  label: string;
  icon: typeof Check;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex min-h-[34px] items-center gap-1 rounded-full border px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        selected
          ? "border-primary/50 bg-primary/10 font-medium text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className="size-3" aria-hidden />
      {label}
    </button>
  );
}

function Tag({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground",
        className,
      )}
    >
      {children}
    </span>
  );
}

function trustLabel(score: number | null | undefined): string | null {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  const normalized = score > 1 ? score / 100 : score;
  return `可信度 ${Math.round(normalized * 100)}%`;
}

function adRiskLabel(value: number | string | null | undefined): string | null {
  if (typeof value === "number") {
    if (value >= 0.66) return "广告风险高";
    if (value >= 0.33) return "广告风险中";
    return null;
  }
  if (typeof value === "string") {
    const lower = value.toLowerCase();
    if (lower === "high" || lower === "高") return "广告风险高";
    if (lower === "medium" || lower === "中") return "广告风险中";
  }
  return null;
}

function SkeletonGroups() {
  return (
    <div className="grid gap-5">
      {[0, 1].map((groupIndex) => (
        <div key={groupIndex} className="grid gap-2.5">
          <div className="flex items-center gap-2">
            <Skeleton className="size-3.5 rounded-full" />
            <Skeleton className="h-3.5 w-24" />
            <Sparkles className="size-3 text-muted-foreground/40" aria-hidden />
            <LoaderCircle className="size-3 animate-spin text-muted-foreground/60" aria-hidden />
          </div>
          <div className="grid gap-2.5 sm:grid-cols-2">
            {[0, 1, 2, 3].map((cardIndex) => (
              <div key={cardIndex} className="grid gap-2 rounded-xl border border-border bg-card px-3.5 py-3">
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-2/3" />
                <div className="flex gap-1.5 pt-1">
                  <Skeleton className="h-7 w-16 rounded-full" />
                  <Skeleton className="h-7 w-16 rounded-full" />
                  <Skeleton className="h-7 w-20 rounded-full" />
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
