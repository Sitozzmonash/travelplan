"use client";

import { CircleAlert, CircleCheck, ChevronDown, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";
import type { ItineraryDay, Warning } from "@/types/plan";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/section-card";
import { SidePanel } from "@/components/side-panel";

const CODE_TITLES: Record<string, string> = {
  arrival_buffer_conflict: "到达缓冲时间冲突",
  route_unverified: "路线数据不完整",
  budget_tight: "预算偏紧",
  pace_exceeded: "当天节奏超限",
  opening_hours_conflict: "开放时间冲突",
  transport_unavailable: "交通数据不完整",
};

const GROUP_LIMIT = 3;

interface FeasibilityAlertProps {
  warnings: Warning[];
  days: ItineraryDay[];
  onJumpToItem?: (dayIndex: number, itemId: string) => void;
  className?: string;
}

/**
 * 时间与可行性检查（FRONTEND_DESIGN §18）。
 *
 * 一个真实 run 可能有十几段估算路线 + 多个时间冲突，全部平铺会把页面撑爆。
 * 因此默认只给一行摘要，点「查看详情」才进抽屉，抽屉里按问题类型分组，
 * 每组只展示前几条，其余可展开；严重（error 且未解决）与提示（warning）分开呈现。
 */
export function FeasibilityAlert({ warnings, days, onJumpToItem, className }: FeasibilityAlertProps) {
  const [open, setOpen] = useState(false);

  const unverifiedLegs = useMemo(
    () =>
      (days ?? [])
        .flatMap((day) => day.items)
        .filter((item) => item.travel_from_previous && !item.travel_from_previous.verified).length,
    [days],
  );

  const unresolved = warnings.filter((warning) => !warning.resolved);
  const resolved = warnings.filter((warning) => warning.resolved);
  const severe = unresolved.filter((warning) => warning.severity === "error");
  const notices = warnings.filter((warning) => !(warning.severity === "error" && !warning.resolved));

  const summaryParts: string[] = [];
  if (severe.length) summaryParts.push(`${severe.length} 个时间问题未解决`);
  else if (unresolved.length) summaryParts.push(`${unresolved.length} 项待确认`);
  if (unverifiedLegs) summaryParts.push(`${unverifiedLegs} 段路线为估算`);
  if (resolved.length) summaryParts.push(`${resolved.length} 项已自动修正`);
  const summary = summaryParts.length
    ? summaryParts.join(" · ")
    : "没有检测到时间冲突或路线问题，所有通勤都取自路线结果。";

  const hasDetails = warnings.length > 0;
  const Icon = unresolved.length ? CircleAlert : CircleCheck;

  return (
    <>
      <SectionCard
        id="feasibility"
        title="时间与可行性检查"
        icon={Icon}
        className={className}
        action={
          hasDetails ? (
            <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
              查看详情
            </Button>
          ) : undefined
        }
      >
        <p className="text-xs leading-5 text-muted-foreground" data-testid="feasibility-summary">
          {summary}
        </p>
      </SectionCard>

      <SidePanel
        open={open}
        onOpenChange={setOpen}
        title="时间与可行性检查"
        description={
          warnings.length
            ? `共 ${warnings.length} 项问题：${severe.length} 项严重、${Math.max(warnings.length - severe.length, 0)} 项提示。`
            : "本次没有检测到问题。"
        }
        widthClassName="sm:max-w-2xl"
      >
        <div className="space-y-5" data-testid="feasibility-drawer">
          {severe.length ? (
            <IssueSection
              title="严重问题（需要出发前确认）"
              tone="danger"
              warnings={severe}
              days={days}
              onJumpToItem={onJumpToItem}
            />
          ) : (
            <div className="flex items-start gap-2 rounded-lg border border-success/25 bg-success-subtle/60 px-3.5 py-3 text-xs leading-5 text-success-subtle-foreground">
              <CircleCheck className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              没有未解决的严重时间问题。
            </div>
          )}

          <IssueSection
            title="提示"
            tone="warning"
            warnings={notices}
            days={days}
            onJumpToItem={onJumpToItem}
            emptyText="本次没有其他提示。"
          />
        </div>
      </SidePanel>
    </>
  );
}

function IssueSection({
  title,
  tone,
  warnings,
  days,
  onJumpToItem,
  emptyText = "这里没有问题。",
}: {
  title: string;
  tone: "danger" | "warning";
  warnings: Warning[];
  days: ItineraryDay[];
  onJumpToItem?: (dayIndex: number, itemId: string) => void;
  emptyText?: string;
}) {
  const groups = useMemo(() => groupByCode(warnings), [warnings]);

  return (
    <section className="space-y-2.5">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
        <span className="tabular text-[11px] text-muted-foreground">{warnings.length} 项</span>
      </div>
      {groups.length ? (
        <div className="space-y-2.5">
          {groups.map((group) => (
            <IssueGroup
              key={group.code}
              code={group.code}
              items={group.items}
              tone={tone}
              days={days}
              onJumpToItem={onJumpToItem}
            />
          ))}
        </div>
      ) : (
        <p className="text-xs leading-5 text-muted-foreground">{emptyText}</p>
      )}
    </section>
  );
}

interface WarningGroup {
  code: string;
  items: Warning[];
}

function groupByCode(warnings: Warning[]): WarningGroup[] {
  const map = new Map<string, Warning[]>();
  for (const warning of warnings) {
    const list = map.get(warning.code) ?? [];
    list.push(warning);
    map.set(warning.code, list);
  }
  return Array.from(map.entries()).map(([code, items]) => ({ code, items }));
}

function IssueGroup({
  code,
  items,
  tone,
  days,
  onJumpToItem,
}: {
  code: string;
  items: Warning[];
  tone: "danger" | "warning";
  days: ItineraryDay[];
  onJumpToItem?: (dayIndex: number, itemId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? items : items.slice(0, GROUP_LIMIT);
  const hidden = items.length - visible.length;

  return (
    <div className="rounded-lg border border-border/80 bg-card">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/70 px-3.5 py-2.5">
        <span
          className={cn(
            "inline-flex size-5 shrink-0 items-center justify-center rounded-md",
            tone === "danger"
              ? "bg-danger-subtle text-danger-subtle-foreground"
              : "bg-warning-subtle text-warning-subtle-foreground",
          )}
        >
          {tone === "danger" ? (
            <CircleAlert className="size-3" aria-hidden />
          ) : (
            <TriangleAlert className="size-3" aria-hidden />
          )}
        </span>
        <p className="text-xs font-medium text-foreground">{CODE_TITLES[code] ?? "需要注意的问题"}</p>
        <span className="tabular rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {items.length} 条
        </span>
      </div>

      <div className="space-y-2.5 px-3.5 py-3">
        {visible.map((warning, index) => (
          <WarningItem
            key={`${warning.code}-${warning.item_id ?? index}`}
            warning={warning}
            days={days}
            onJumpToItem={onJumpToItem}
          />
        ))}
        {hidden > 0 ? (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="inline-flex items-center gap-1 text-[11px] text-primary transition-colors hover:underline"
          >
            <ChevronDown className="size-3" aria-hidden />
            展开其余 {hidden} 条
          </button>
        ) : null}
        {expanded && items.length > GROUP_LIMIT ? (
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="inline-flex items-center gap-1 text-[11px] text-primary transition-colors hover:underline"
          >
            <ChevronDown className="size-3 rotate-180" aria-hidden />
            收起
          </button>
        ) : null}
      </div>
    </div>
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
  const dayPosition = days.findIndex((entry) => entry.day_index === warning.day_index);
  const day = dayPosition >= 0 ? days[dayPosition] : undefined;
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
          {resolved ? "已自动修正" : "需要确认"}
        </p>
        <span className="text-[11px] text-muted-foreground">
          {dayPosition >= 0 ? `Day ${dayPosition + 1}` : `第 ${warning.day_index + 1} 天`}
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
