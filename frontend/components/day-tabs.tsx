"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useRef } from "react";
import type { ItineraryDay } from "@/types/plan";
import { cn } from "@/lib/utils";
import { formatDateShort, formatWeekday } from "@/lib/format";

interface DayTabsProps {
  days: ItineraryDay[];
  activeDay: number;
  onChange: (index: number) => void;
}

/** Day 导航（FRONTEND_DESIGN §10）：Tab + 横向滚动，移动端同样横向滚动。 */
export function DayTabs({ days, activeDay, onChange }: DayTabsProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const current = days[activeDay];

  function scrollBy(delta: number) {
    scrollerRef.current?.scrollBy({ left: delta, behavior: "smooth" });
  }

  return (
    <div data-testid="day-tabs">
      <div className="relative">
        <div
          ref={scrollerRef}
          className="flex gap-1.5 overflow-x-auto scroll-smooth pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {days.map((day, index) => {
            const active = index === activeDay;
            return (
              <button
                key={day.day_index}
                type="button"
                onClick={() => onChange(index)}
                aria-current={active}
                className={cn(
                  "flex shrink-0 flex-col items-start rounded-lg border px-3 py-2 text-left transition-colors",
                  active
                    ? "border-primary/40 bg-accent text-accent-foreground"
                    : "border-border bg-card text-muted-foreground hover:text-foreground",
                )}
              >
                <span className={cn("text-xs font-medium", active && "text-foreground")}>Day {index + 1}</span>
                <span className="tabular mt-0.5 text-[11px]">
                  {formatDateShort(day.date)} {formatWeekday(day.date)}
                </span>
              </button>
            );
          })}
        </div>
        {days.length > 4 ? (
          <div className="pointer-events-none absolute inset-y-0 right-0 hidden items-center bg-gradient-to-l from-background to-transparent pl-6 lg:flex">
            <div className="pointer-events-auto flex gap-1">
              <button
                type="button"
                onClick={() => scrollBy(-240)}
                aria-label="向右滚动日期"
                className="rounded-md border border-border bg-card p-1 text-muted-foreground hover:text-foreground"
              >
                <ChevronLeft className="size-3.5" aria-hidden />
              </button>
              <button
                type="button"
                onClick={() => scrollBy(240)}
                aria-label="向左滚动日期"
                className="rounded-md border border-border bg-card p-1 text-muted-foreground hover:text-foreground"
              >
                <ChevronRight className="size-3.5" aria-hidden />
              </button>
            </div>
          </div>
        ) : null}
      </div>

      {current?.area ? (
        <p className="mt-2 truncate text-xs text-muted-foreground">
          Day {activeDay + 1} · {current.area}
        </p>
      ) : null}
    </div>
  );
}
