"use client";

import { ArrowRight, CalendarDays, CircleSlash, WandSparkles } from "lucide-react";
import { ErrorState, InlineWarning, PartialNotice } from "@/components/state-views";
import { SectionCard } from "@/components/section-card";
import { cn } from "@/lib/utils";
import type { SessionView } from "@/types/session";
import type { PoiNameGroup, SummaryModel } from "./draft";

/**
 * Step 6 确认（§11）。
 * 这是「开始规划」的唯一触发点：只有点下去才会 POST .../start，
 * 之前的每一步都只写回偏好，不会创建正式 Run。
 */

interface StepConfirmProps {
  model: SummaryModel;
  session: SessionView | null;
  settled: boolean;
  socialFailed: boolean;
  unusable: boolean;
  startError: string | null;
  onRetryStart: () => void;
}

export function StepConfirm({
  model,
  session,
  settled,
  socialFailed,
  unusable,
  startError,
  onRetryStart,
}: StepConfirmProps) {
  const poiTotal = model.must.count + model.want.count + model.food.count;

  return (
    <div className="grid gap-4">
      <SectionCard title="这趟行程" icon={CalendarDays} className="shadow-sm">
        <div className="grid gap-3">
          <p className="flex flex-wrap items-center gap-2 text-base font-medium text-foreground">
            <span>{model.origin}</span>
            <ArrowRight className="size-4 text-muted-foreground" aria-hidden />
            <span>{model.destination}</span>
          </p>
          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <Stat label="出发日期" value={model.dateLabel} />
            <Stat label="行程长度" value={model.lengthLabel} />
            <Stat label="出行人数" value={model.travelersLabel} />
            <Stat label="预算" value={model.budgetLabel} />
          </dl>
        </div>
      </SectionCard>

      <SectionCard title="你的偏好" icon={WandSparkles} className="shadow-sm">
        <dl className="grid gap-2.5 text-xs">
          <Row
            label="交通"
            value={[model.transportMode, model.transportPriority, ...model.transportConstraints].join(" · ")}
          />
          <Row label="酒店" value={[model.hotelPriority, ...model.hotelExtras].join(" · ")} />
          <Row label="旅行节奏" value={model.paceLabel} />
        </dl>
      </SectionCard>

      <SectionCard title="想去的和想吃的" className="shadow-sm">
        <div className="grid gap-3">
          <NameGroup label="必去" group={model.must} tone="must" />
          <NameGroup label="想去" group={model.want} tone="want" />
          <NameGroup label="想吃" group={model.food} tone="food" />
          {model.reject.count > 0 ? (
            <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <CircleSlash className="size-3" aria-hidden />
              已排除 {model.reject.count} 个不感兴趣的地点：{model.reject.names.join("、")}
            </p>
          ) : null}
          <p className="text-[11px] leading-5 text-muted-foreground">
            {model.poiModeLabel
              ? `地点由系统安排：${model.poiModeLabel}。`
              : poiTotal === 0
                ? "没有勾选地点：系统会按攻略证据、路线与预算自己挑。"
                : "没勾选的地点由系统按证据、路线与预算决定是否安排。"}
          </p>
        </div>
      </SectionCard>

      <SectionCard title="其他" className="shadow-sm">
        <p className="text-xs leading-5 text-muted-foreground">
          系统自动安排：市内交通与接驳、每天的先后顺序与出发时间、门票与营业时间核实、
          预算与时间可行性检查。
          {model.evidenceLabel ? ` 目前 ${model.evidenceLabel}。` : ""}
        </p>
      </SectionCard>

      {!settled && !unusable ? (
        <InlineWarning
          title="攻略还在整理中。"
          description="现在开始规划也可以：系统会把已经找到的地点带进正式规划，剩下的在正式流程里继续补。"
        />
      ) : null}

      {socialFailed ? (
        <PartialNotice
          title="社交攻略缺失，仍然可以继续"
          description="本次没有拿到小红书 / 抖音内容，正式规划会以高德与网页数据为准。"
          detail="结果里会标注哪部分信息没有社交来源印证。"
        />
      ) : null}

      {unusable ? (
        <InlineWarning
          title={session?.status === "EXPIRED" ? "这次会话已过期。" : "这次会话已被取消。"}
          description="请点「返回修改」重新开始，系统会按当前的基础信息新建一次会话。"
        />
      ) : null}

      {startError ? (
        <ErrorState
          title="没能开始规划"
          description={startError}
          detail={session ? `session_id：${session.session_id}` : undefined}
          onRetry={onRetryStart}
          retryLabel="再试一次"
        />
      ) : null}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-xs font-medium text-foreground">{value}</dd>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border/60 pb-2 last:border-0 last:pb-0">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right text-foreground/90">{value}</dd>
    </div>
  );
}

function NameGroup({
  label,
  group,
  tone,
}: {
  label: string;
  group: PoiNameGroup;
  tone: "must" | "want" | "food";
}) {
  if (group.count === 0) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="shrink-0">{label}（0）</span>
        <span>—</span>
      </div>
    );
  }
  return (
    <div className="grid gap-1.5">
      <p className="text-[11px] text-muted-foreground">
        {label}（{group.count}）
      </p>
      <div className="flex flex-wrap gap-1.5">
        {group.names.map((name, index) => (
          <span
            key={`${name}-${index}`}
            className={cn(
              "inline-flex items-center rounded-md px-2 py-0.5 text-xs",
              tone === "must" && "bg-accent text-accent-foreground",
              tone === "want" && "bg-info-subtle text-info-subtle-foreground",
              tone === "food" && "bg-warning-subtle text-warning-subtle-foreground",
            )}
          >
            {name}
          </span>
        ))}
      </div>
    </div>
  );
}
