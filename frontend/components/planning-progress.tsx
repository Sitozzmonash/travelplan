"use client";

import { Circle, CircleCheck, CircleX, LoaderCircle, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { PlanningStepState, StepStatus } from "@/types/api";
import { cn } from "@/lib/utils";

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
  const total = steps.length || 1;
  const finished = steps.filter((step) => step.status === "success" || step.status === "warning" || step.status === "error")
    .length;
  const running = steps.find((step) => step.status === "running");
  const percent = Math.round((finished / total) * 100);

  return (
    <div className={cn("rounded-xl border border-border bg-card shadow-sm", className)} data-testid="planning-progress">
      <div className="border-b border-border/70 px-4 py-3.5 sm:px-5">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm font-medium text-foreground">
              {running ? `正在${running.label}…` : finished >= total ? "规划已完成" : "准备开始规划"}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              已进行 {finished} / {total} 步。每一步都会真实查询数据源，不做推测。
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
                    {step.label}
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
                  ) : step.description ? (
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{step.description}</p>
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
