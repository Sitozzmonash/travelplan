"use client";

import { CalendarDays, Clock3, MapPin, Users, Wallet } from "lucide-react";
import type { TripPlan } from "@/types/plan";
import { API_MODE } from "@/lib/api";
import { formatCNY, formatDate, formatWeekday } from "@/lib/format";

interface TripSummaryProps {
  plan: TripPlan;
}

/**
 * 顶部 Summary（FRONTEND_DESIGN §8）。
 * 只回答「去哪、几天、什么时候、几个人、预算多少、什么节奏」，
 * 来源 / 可信度 / 规划依据一律不占这里 —— 它们收进底部的「为什么这样安排？」入口。
 *
 * intent / budget / warnings 在后端降级时可能整块缺失，因此这里一律按空值处理：
 * 顶部摘要挂掉会让整页不可用，代价远高于少显示一行。
 */
export function TripSummary({ plan }: TripSummaryProps) {
  const { intent, days } = plan;
  const dayList = days ?? [];
  const preferences = intent?.preferences ?? [];
  const dayCount = intent?.days ?? dayList.length;

  const destination = (intent?.destination ?? []).join(" / ") || "未指定目的地";
  const nights = Math.max(dayList.length - 1, 0);

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
      <div className="flex flex-col gap-2.5 p-4 sm:p-5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          <h1 className="truncate text-xl font-semibold tracking-tight text-foreground sm:text-2xl">
            {destination} · {dayCount} 天 {nights} 晚
          </h1>
          <span className="rounded-md bg-success-subtle px-2 py-0.5 text-xs font-medium text-success-subtle-foreground">
            {API_MODE === "mock" ? "演示数据规划" : "真实数据规划"} ✓
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
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
          <p className="text-xs text-muted-foreground">偏好：{preferences.join(" · ")}</p>
        ) : null}
      </div>
    </section>
  );
}

function paceLabel(pace: string): string {
  if (pace === "relaxed" || pace === "轻松") return "节奏 轻松";
  if (pace === "intensive" || pace === "特种兵") return "节奏 特种兵";
  return "节奏 平衡";
}
