"use client";

import { Circle, CircleCheck, CircleX, LoaderCircle, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { PlanningStepState, StepStatus } from "@/types/api";
import { cn } from "@/lib/utils";

/**
 * 规划器的决策型阶段。它们来自 Workflow，而不是前端的计时动画；这里仅把机器码翻成
 * 用户能立即理解的叙事。保留在本组件中，避免为了文案改变共享 API 类型。
 */
const DECISION_STAGE_COPY: Record<string, { active: string; complete: string; detail: string }> = {
  hotel_area_selected: {
    active: "正在确定最适合落脚的住宿区域",
    complete: "已确定住宿区域",
    detail: "综合每天的活动范围、通勤和晚间便利度挑选住宿中心。",
  },
  hotels_compared: {
    active: "正在比较附近酒店",
    complete: "附近酒店已比较完成",
    detail: "从位置、价格、评分和入住条件中筛出更合适的选项。",
  },
  days_arranged: {
    active: "正在按区域安排每天行程",
    complete: "每天的活动区域已安排",
    detail: "尽量把同一区域的景点排在同一天，减少折返。",
  },
  meals_matched: {
    active: "正在匹配午餐和晚餐",
    complete: "午餐和晚餐已匹配",
    detail: "结合当天位置、营业时间与推荐内容安排用餐。",
  },
  routes_checked: {
    active: "正在用高德检查每日路线",
    complete: "每日路线已检查",
    detail: "核对地点、段间通勤和当天动线是否顺路。",
  },
  budget_checked: {
    active: "正在检查预算和返程时间",
    complete: "预算和返程时间已检查",
    detail: "把交通、住宿和活动费用放回总预算中复核。",
  },
};

const STATUS_META: Record<StepStatus, { icon: LucideIcon; className: string; spin?: boolean }> = {
  waiting: { icon: Circle, className: "text-border" },
  running: { icon: LoaderCircle, className: "text-primary", spin: true },
  success: { icon: CircleCheck, className: "text-success" },
  warning: { icon: TriangleAlert, className: "text-warning" },
  error: { icon: CircleX, className: "text-danger" },
};

interface PlanningProgressProps {
  steps: PlanningStepState[];
  /** 已经完成或正在执行的步骤数，用于进度条。 */
  className?: string;
}

/**
 * 规划过程（FRONTEND_DESIGN §6）。
 * 展示的是 Agent 的真实工作步骤，而不是一个转圈动画；
 * 步骤状态与轻量事实都由后端进度驱动，mock 版为合理模拟。
 */
export function PlanningProgress({ steps, className }: PlanningProgressProps) {
  // 决策阶段会和底层工作流阶段并存；它们用于解释“正在作什么决定”，而不应把旧的
  // 12 个工作流进度稀释成一条不准确的 18 步进度条。
  const workflowSteps = steps.filter((step) => !DECISION_STAGE_COPY[step.id]);
  const progressSteps = workflowSteps.length ? workflowSteps : steps;
  const total = progressSteps.length || 1;
  const finished = progressSteps.filter((step) => step.status === "success" || step.status === "warning" || step.status === "error")
    .length;
  const running = steps.find((step) => step.status === "running");
  const runningCopy = running ? DECISION_STAGE_COPY[running.id] : null;
  const percent = Math.round((finished / total) * 100);

  return (
    <div className={cn("rounded-xl border border-border bg-card shadow-sm", className)} data-testid="planning-progress">
      <div className="border-b border-border/70 px-4 py-3.5 sm:px-5">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">
              {runningCopy?.active ?? (running ? `正在${running.label}…` : finished >= total ? "规划已完成" : "准备开始规划")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              已完成 {finished} / {total} 个工作流步骤。行程会把区域、酒店、餐饮、路线与预算逐项核对。
            </p>
          </div>
          <span className="tabular shrink-0 text-xs text-muted-foreground">{percent}%</span>
        </div>
        <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-primary transition-[width] duration-500 ease-out"
            style={{ width: `${Math.max(percent, 3)}%` }}
          />
        </div>
      </div>

      <ol className="divide-y divide-border/60">
        {steps.map((step) => {
          const meta = STATUS_META[step.status];
          const Icon = meta.icon;
          const dim = step.status === "waiting";
          const decisionCopy = DECISION_STAGE_COPY[step.id];
          const readableLabel = decisionCopy
            ? step.status === "running"
              ? decisionCopy.active
              : step.status === "success" || step.status === "warning"
                ? decisionCopy.complete
                : step.label
            : step.label;
          return (
            <li key={step.id} className={cn("px-4 py-3 sm:px-5", dim && "opacity-60")}>
              <div className="flex items-start gap-3">
                <span
                  className={cn(
                    "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border",
                    step.status === "waiting" ? "border-border" : "border-transparent",
                    step.status === "success" && "bg-success-subtle",
                    step.status === "warning" && "bg-warning-subtle",
                    step.status === "error" && "bg-danger-subtle",
                  )}
                >
                  <Icon className={cn("size-3.5", meta.className, meta.spin && "animate-spin")} aria-hidden />
                </span>
                <div className="min-w-0 flex-1">
                  <p
                    className={cn(
                      "text-sm",
                      step.status === "running" ? "font-medium text-foreground" : "text-foreground/90",
                    )}
                  >
                    {readableLabel}
                  </p>
                  {step.unknown ? (
                    <p className="mt-0.5 text-[11px] leading-5 text-warning-subtle-foreground">
                      后端本次新增的阶段，前端清单尚未收录
                    </p>
                  ) : null}
                  {step.facts?.length ? (
                    <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                      {step.facts.map((fact) => (
                        <li key={fact} className="tabular text-xs text-muted-foreground">
                          {fact}
                        </li>
                      ))}
                    </ul>
                  ) : step.description || decisionCopy ? (
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{step.description ?? decisionCopy.detail}</p>
                  ) : null}
                </div>
                <StatusLabel status={step.status} />
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function StatusLabel({ status }: { status: StepStatus }) {
  const labels: Record<StepStatus, string> = {
    waiting: "等待",
    running: "进行中",
    success: "完成",
    warning: "完成（有提示）",
    error: "失败",
  };
  const tone: Record<StepStatus, string> = {
    waiting: "text-muted-foreground",
    running: "text-primary",
    success: "text-success-subtle-foreground",
    warning: "text-warning-subtle-foreground",
    error: "text-danger-subtle-foreground",
  };
  return <span className={cn("shrink-0 text-[11px]", tone[status])}>{labels[status]}</span>;
}
