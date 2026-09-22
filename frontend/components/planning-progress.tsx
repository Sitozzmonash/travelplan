"use client";

import { useEffect, useRef } from "react";
import { BrainCircuit, Circle, CircleCheck, CircleX, LoaderCircle, Wrench, TriangleAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { PlanningStepState, RunStatus, StepStatus } from "@/types/api";
import { cn } from "@/lib/utils";

/**
 * 规划过程（FRONTEND_DESIGN §6）—— **Agent 实际走过的路径**，不是一份写死的流程清单。
 *
 * 主规划已经改成 SuperHarness Agent Loop：调哪些工具、先查机票还是先查酒店，由 Agent
 * 自己在循环里决定。所以这个组件只做两件事：
 *   1. 把 ``progress.current_stage`` 作为当前正在做的事（大字标题，随轮询变化）；
 *   2. 按真实发生顺序列出 ``stages`` 里已经发生过的步骤（含工具耗时）。
 * 步骤名与顺序全部来自后端，前端不预置任何节点；后端多接一个工具，这里自动多一行。
 */

const STATUS_META: Record<StepStatus, { icon: LucideIcon; className: string; spin?: boolean }> = {
  waiting: { icon: Circle, className: "text-border" },
  running: { icon: LoaderCircle, className: "text-primary", spin: true },
  success: { icon: CircleCheck, className: "text-success" },
  warning: { icon: TriangleAlert, className: "text-warning" },
  error: { icon: CircleX, className: "text-danger" },
};

/** run 终态时标题区怎么说：必须如实区分「完成」「完成但有降级」「中断」「取消」。 */
const HEADLINE: Record<RunStatus, { title: string; hint: string }> = {
  RUNNING: { title: "", hint: "Agent 会自己决定先查什么、要不要回头再查；下面是它已经实际走过的步骤。" },
  SUCCESS: { title: "规划已完成", hint: "本次规划已经结束，正在打开行程。" },
  DEGRADED: { title: "规划已完成", hint: "有步骤没有正常返回，行程仍然可用，涉及的信息会单独标注。" },
  FAILED: { title: "规划在中断前做到这里", hint: "本次没有产出可用行程；上面几步是中断前实际完成的工作。" },
  CANCELLED: { title: "规划已被取消", hint: "任务在完成前被终止；上面几步是终止前实际完成的工作。" },
};

interface PlanningProgressProps {
  steps: PlanningStepState[];
  /** progress.current_stage：当前步骤的中文展示名，比步骤列表更权威。 */
  currentStage?: string | null;
  /** 整个 run 的状态。默认 RUNNING（生成中的轮询页面）。 */
  status?: RunStatus;
  className?: string;
}

export function PlanningProgress({ steps, currentStage, status = "RUNNING", className }: PlanningProgressProps) {
  const listRef = useRef<HTMLOListElement | null>(null);
  const running = steps.find((step) => step.status === "running");
  const live = status === "RUNNING";
  const headline = live ? currentStage || running?.label || "正在准备规划" : HEADLINE[status].title;

  // 步骤是随时间往下追加的：新增一步时把列表跟到底部，否则用户看不到最新进度。
  // 只在用户本来就在底部附近时才跟随，避免打断他往上翻看历史。
  useEffect(() => {
    const node = listRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
    if (nearBottom) node.scrollTop = node.scrollHeight;
  }, [steps.length]);

  return (
    <div className={cn("rounded-xl border border-border bg-card shadow-sm", className)} data-testid="planning-progress">
      <div className="border-b border-border/70 px-4 py-3.5 sm:px-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2">
            {live ? (
              <LoaderCircle className="mt-1 size-4 shrink-0 animate-spin text-primary" aria-hidden />
            ) : null}
            <div className="min-w-0">
              <p
                className="truncate text-sm font-medium text-foreground"
                aria-live="polite"
                data-testid="current-stage"
              >
                {headline}
              </p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {HEADLINE[status].hint}
                {steps.length ? ` 已走过 ${steps.length} 个步骤。` : ""}
              </p>
            </div>
          </div>
          {live ? (
            <span className="tabular shrink-0 rounded-md bg-info-subtle px-2 py-0.5 text-[11px] font-medium text-info-subtle-foreground">
              进行中
            </span>
          ) : null}
        </div>
        {/* 步骤总数由 Agent 自己决定，前端给不出准确百分比：用一条推进中的扫描条表示「还在跑」。 */}
        {live ? (
          <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div className="h-full w-2/5 animate-pulse rounded-full bg-primary/70" />
          </div>
        ) : null}
      </div>

      {steps.length ? (
        <ol
          ref={listRef}
          className="max-h-[22rem] divide-y divide-border/60 overflow-y-auto"
          data-testid="planning-steps"
        >
          {steps.map((step) => (
            <StepRow key={step.id} step={step} />
          ))}
        </ol>
      ) : (
        <p className="px-4 py-4 text-xs leading-5 text-muted-foreground sm:px-5">
          正在等待 Agent 的第一步。它通常先读一遍需求，再决定去查交通还是住宿。
        </p>
      )}
    </div>
  );
}

function StepRow({ step }: { step: PlanningStepState }) {
  const meta = STATUS_META[step.status];
  const Icon = meta.icon;
  const duration = formatStepDuration(step.durationMs);
  return (
    <li className={cn("px-4 py-2.5 sm:px-5", step.status === "waiting" && "opacity-60")}>
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
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span
              className={cn(
                "text-sm",
                step.status === "running" ? "font-medium text-foreground" : "text-foreground/90",
              )}
            >
              {step.label}
            </span>
            {step.model && !step.tool ? (
              <BrainCircuit className="size-3 shrink-0 self-center text-muted-foreground" aria-hidden />
            ) : null}
            {step.tool ? (
              <Wrench
                className="size-3 shrink-0 self-center text-muted-foreground/70"
                aria-hidden
              />
            ) : null}
            {duration ? (
              <span className="tabular text-[11px] text-muted-foreground">耗时 {duration}</span>
            ) : null}
          </div>
          {step.facts?.length ? (
            <ul className="mt-1 space-y-0.5">
              {step.facts.map((fact) => (
                <li key={fact} className="break-words text-[11px] leading-5 text-warning-subtle-foreground">
                  {fact}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        <StatusLabel status={step.status} />
      </div>
    </li>
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

/** 单步耗时：Agent 的每一步都是工具/模型调用，毫秒级到分钟级都可能，按量级换单位。 */
function formatStepDuration(ms: number | undefined): string | null {
  if (ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} 秒`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`;
}
