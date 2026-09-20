"use client";

import { CircleAlert, CircleCheck, TriangleAlert } from "lucide-react";
import type { ItineraryDay, Warning } from "@/types/plan";
import { SectionCard } from "@/components/section-card";
import { cn } from "@/lib/utils";

const CODE_TITLES: Record<string, string> = {
  arrival_buffer_conflict: "已自动修正 1 个时间冲突",
  route_unverified: "路线数据不完整",
  budget_tight: "预算提示",
  pace_exceeded: "当天节奏超限",
  opening_hours_conflict: "开放时间冲突",
};

interface FeasibilityAlertProps {
  warnings: Warning[];
  days: ItineraryDay[];
  onJumpToItem?: (dayIndex: number, itemId: string) => void;
  className?: string;
}

/** 统一 Warning 区（FRONTEND_DESIGN §18）。展示「原计划 / 问题 / 调整」三要素。 */
export function FeasibilityAlert({ warnings, days, onJumpToItem, className }: FeasibilityAlertProps) {
  if (!warnings.length) {
    return (
      <SectionCard id="feasibility" title="时间与可行性检查" icon={CircleCheck} className={className}>
        <p className="text-xs leading-5 text-muted-foreground">
          本次规划没有检测到时间冲突、预算超标或路线问题。所有安排之间的通勤时间都取自高德路线结果。
        </p>
      </SectionCard>
    );
  }

  const resolvedCount = warnings.filter((warning) => warning.resolved).length;

  return (
    <SectionCard
      id="feasibility"
      title="时间与可行性检查"
      icon={CircleAlert}
      className={className}
      action={
        resolvedCount ? (
          <span className="rounded-md bg-success-subtle px-2 py-0.5 text-[11px] font-medium text-success-subtle-foreground">
            已自动修正 {resolvedCount} 项
          </span>
        ) : undefined
      }
    >
      <div className="grid gap-3">
        {warnings.map((warning, index) => (
          <WarningItem
            key={`${warning.code}-${warning.item_id ?? index}`}
            warning={warning}
            days={days}
            onJumpToItem={onJumpToItem}
          />
        ))}
      </div>
    </SectionCard>
  );
}

function WarningItem({
  warning,
  days,
  onJumpToItem,
}: {
  warning: Warning;
  days: ItineraryDay[];
  onJumpToItem?: (dayIndex: number, itemId: string) => void;
}) {
  const day = days.find((entry) => entry.day_index === warning.day_index);
  const item = warning.item_id ? day?.items.find((entry) => entry.id === warning.item_id) : undefined;
  const resolved = warning.resolved;

  return (
    <div
      className={cn(
        "rounded-lg border px-3.5 py-3",
        resolved ? "border-success/25 bg-success-subtle/60" : "border-warning/30 bg-warning-subtle/60",
      )}
      data-testid="feasibility-warning"
    >
      <div className="flex flex-wrap items-center gap-2">
        {resolved ? (
          <CircleCheck className="size-3.5 shrink-0 text-success" aria-hidden />
        ) : (
          <TriangleAlert className="size-3.5 shrink-0 text-warning" aria-hidden />
        )}
        <p className="text-xs font-medium text-foreground">
          {CODE_TITLES[warning.code] ?? (resolved ? "已自动修正 1 项问题" : "需要注意的问题")}
        </p>
        <span className="text-[11px] text-muted-foreground">
          Day {warning.day_index + 1}
          {item ? ` · ${item.name}` : ""}
        </span>
        {onJumpToItem && item ? (
          <button
            type="button"
            onClick={() => onJumpToItem(warning.day_index, item.id)}
            className="ml-auto text-[11px] text-primary transition-colors hover:underline"
          >
            定位到该安排
          </button>
        ) : null}
      </div>

      <dl className="mt-2.5 grid gap-2 text-xs leading-5">
        {warning.original_start ? (
          <div className="flex gap-2">
            <dt className="w-14 shrink-0 text-muted-foreground">原计划</dt>
            <dd className="tabular text-foreground">
              {warning.original_start}
              {item ? ` ${item.name}` : ""}
            </dd>
          </div>
        ) : null}
        <div className="flex gap-2">
          <dt className="w-14 shrink-0 text-muted-foreground">问题</dt>
          <dd className="min-w-0 text-foreground/85">{warning.reason}</dd>
        </div>
        {warning.revised_start ? (
          <div className="flex gap-2">
            <dt className="w-14 shrink-0 text-muted-foreground">调整</dt>
            <dd className="tabular text-foreground">
              {item ? `${item.name} 改为 ${warning.revised_start}` : `改为 ${warning.revised_start}`}
            </dd>
          </div>
        ) : null}
        {!warning.revised_start && !resolved ? (
          <div className="flex gap-2">
            <dt className="w-14 shrink-0 text-muted-foreground">处理</dt>
            <dd className="text-muted-foreground">该问题无法在本轮自动修正，已在行程中标注，建议出发前再次确认。</dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}
