"use client";

import type { ItineraryDay, ItineraryItem } from "@/types/plan";
import { ItineraryCard, TravelLegLine } from "@/components/itinerary-card";
import { EmptyState } from "@/components/state-views";

interface ItineraryTimelineProps {
  day: ItineraryDay | null;
  fetchedAt?: string | null;
  selectedItemId?: string | null;
  lockedItemIds?: string[];
  onSelectItem?: (item: ItineraryItem) => void;
  onOpenEvidence?: (item: ItineraryItem) => void;
  onOpenDetail?: (item: ItineraryItem) => void;
  onOpenSources?: (item: ItineraryItem) => void;
  onRevise?: (item: ItineraryItem, action: "replace" | "remove" | "lock" | "relax_day" | "lower_budget") => void;
}

/** 一天的时间线（FRONTEND_DESIGN §11）：时间 + 安排 + 段间交通。 */
export function ItineraryTimeline({
  day,
  fetchedAt,
  selectedItemId,
  lockedItemIds = [],
  onSelectItem,
  onOpenEvidence,
  onOpenDetail,
  onOpenSources,
  onRevise,
}: ItineraryTimelineProps) {
  if (!day || !day.items.length) {
    return (
      <EmptyState
        title="这一天还没有具体安排"
        description="后端返回的这一天是空行程，通常发生在当天以交通为主或刻意留白的情况。"
        hint="可以调整偏好或降低当天强度后重新规划，让这一天更符合你的节奏。"
      />
    );
  }

  return (
    <div className="space-y-3" data-testid="itinerary-timeline">
      <ol className="space-y-1">
        {day.items.map((item) => (
          <li key={item.id}>
            <TravelLegLine item={item} />
            <ItineraryCard
              item={item}
              fetchedAt={fetchedAt}
              selected={selectedItemId === item.id}
              locked={lockedItemIds.includes(item.id)}
              scopeLabel="2 人合计"
              onSelect={() => onSelectItem?.(item)}
              onOpenEvidence={() => onOpenEvidence?.(item)}
              onOpenDetail={() => onOpenDetail?.(item)}
              onOpenSources={() => onOpenSources?.(item)}
              onRevise={onRevise ? (action) => onRevise(item, action) : undefined}
            />
          </li>
        ))}
      </ol>

      {day.notes?.length ? (
        <div className="rounded-lg border border-border/80 bg-muted/25 px-3.5 py-3">
          <p className="text-xs font-medium text-muted-foreground">当天说明</p>
          <ul className="mt-1.5 space-y-1">
            {day.notes.map((note) => (
              <li key={note} className="text-xs leading-5 text-muted-foreground">
                · {note}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
