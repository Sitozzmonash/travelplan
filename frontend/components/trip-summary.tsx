"use client";

import { BadgeCheck, CalendarDays, CircleAlert, Clock3, MapPin, Users, Wallet } from "lucide-react";
import type { TripPlan } from "@/types/plan";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { formatCNY, formatDate, formatWeekday } from "@/lib/format";

interface TripSummaryProps {
  plan: TripPlan;
  onOpenAudit: () => void;
  onOpenSources: () => void;
}

/**
 * 顶部 Summary（FRONTEND_DESIGN §8）。
 * 状态 Badge 只汇总后端已经给出的结论（预算状态、问题列表、路线是否验证），
 * 前端不重新计算可行性。
 *
 * intent / budget / warnings 在后端降级时可能整块缺失，因此这里一律按空值处理：
 * 顶部摘要挂掉会让整页不可用，代价远高于少显示一行。
 */
export function TripSummary({ plan, onOpenAudit, onOpenSources }: TripSummaryProps) {
  const { intent, budget, warnings, days } = plan;
  const dayList = days ?? [];
  const warningList = warnings ?? [];
  const preferences = intent?.preferences ?? [];
  const dayCount = intent?.days ?? dayList.length;

  const destination = (intent?.destination ?? []).join(" / ") || "未指定目的地";
  const nights = Math.max(dayList.length - 1, 0);

  const unresolvedErrors = warningList.filter((warning) => !warning.resolved && warning.severity === "error");
  const resolvedIssues = warningList.filter((warning) => warning.resolved);
  const unverifiedLegs = dayList
    .flatMap((day) => day.items)
    .filter((item) => item.travel_from_previous && !item.travel_from_previous.verified);

  const budgetWithin = budget?.status === "within_budget";
  const budgetKnown = budget !== undefined && budget.status !== "unknown";
  const remaining = budget?.remaining ?? null;
  const feasible = unresolvedErrors.length === 0;
  const routesVerified = unverifiedLegs.length === 0;

  const startDate = intent?.start_date ?? null;
  const endDate = intent?.end_date ?? null;
  const dateRange =
    startDate && endDate
      ? `${formatDate(startDate)} - ${formatDate(endDate)}`
      : startDate
        ? formatDate(startDate)
        : "日期待确认";

  return (
    <section className="rounded-xl border border-border bg-card shadow-sm" data-testid="trip-summary">
      <div className="flex flex-col gap-4 p-4 sm:p-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
            <h1 className="truncate text-lg font-semibold tracking-tight text-foreground sm:text-xl">
              {destination} · {dayCount} 天 {nights} 晚
            </h1>
            <div className="flex flex-wrap items-center gap-1.5">
              <StatusBadge
                tone={feasible ? "success" : "danger"}
                label={feasible ? "时间可行" : `${unresolvedErrors.length} 个时间问题未解决`}
              />
              <StatusBadge
                tone={!budgetKnown ? "muted" : budgetWithin ? "success" : "danger"}
                label={!budgetKnown ? "预算未设定" : budgetWithin ? "预算内" : "超出预算"}
              />
              <StatusBadge
                tone={routesVerified ? "success" : "warning"}
                label={routesVerified ? "路线已验证" : `${unverifiedLegs.length} 段路线为估算`}
              />
            </div>
          </div>

          <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <CalendarDays className="size-3.5" aria-hidden />
              <span className="tabular">{dateRange}</span>
              {startDate ? <span className="tabular">{formatWeekday(startDate)}起</span> : null}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <MapPin className="size-3.5" aria-hidden />
              {intent?.origin ? `${intent.origin}出发` : "出发地未指定"}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Users className="size-3.5" aria-hidden />
              {intent?.travelers ?? 0} 人
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Wallet className="size-3.5" aria-hidden />
              {intent?.budget_total ? `预算 ${formatCNY(intent.budget_total)}` : "未设定预算"}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Clock3 className="size-3.5" aria-hidden />
              {paceLabel(intent?.pace ?? "")}
            </span>
          </div>

          {preferences.length ? (
            <p className="mt-2 text-xs text-muted-foreground">偏好：{preferences.join(" · ")}</p>
          ) : null}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={onOpenSources}>
            来源与查询条件
          </Button>
          <Button variant="outline" size="sm" onClick={onOpenAudit}>
            规划依据
          </Button>
        </div>
      </div>

      {warningList.length ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-border/70 bg-muted/30 px-4 py-2.5 text-xs text-muted-foreground sm:px-5">
          {resolvedIssues.length ? (
            <span className="inline-flex items-center gap-1.5">
              <BadgeCheck className="size-3.5 text-success" aria-hidden />
              <span className="text-foreground/80">{resolvedIssues.length} 个时间冲突已自动修正</span>
            </span>
          ) : null}
          {unverifiedLegs.length ? (
            <span className="inline-flex items-center gap-1.5">
              <CircleAlert className="size-3.5 text-warning" aria-hidden />
              <span>{unverifiedLegs.length} 段路线未取得真实通勤时间，按估算计算</span>
            </span>
          ) : null}
          <span className="inline-flex items-center gap-1.5">
            <CircleAlert className="size-3.5 text-warning" aria-hidden />
            部分实时价格可能变化
          </span>
          <span className="inline-flex items-center gap-1.5">
            <CircleAlert className="size-3.5 text-warning" aria-hidden />
            预算余量 {formatCNY(remaining)}
          </span>
        </div>
      ) : null}
    </section>
  );
}

function StatusBadge({ tone, label }: { tone: "success" | "warning" | "danger" | "muted"; label: string }) {
  const tones = {
    success: "bg-success-subtle text-success-subtle-foreground",
    warning: "bg-warning-subtle text-warning-subtle-foreground",
    danger: "bg-danger-subtle text-danger-subtle-foreground",
    muted: "bg-muted text-muted-foreground",
  } as const;
  return (
    <span className={cn("rounded-md px-2 py-0.5 text-[11px] font-medium", tones[tone])}>{label}</span>
  );
}

function paceLabel(pace: string): string {
  if (pace === "relaxed" || pace === "轻松") return "节奏 轻松";
  if (pace === "intensive" || pace === "特种兵") return "节奏 特种兵";
  return "节奏 平衡";
}
