import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * 向导顶部进度条（§17）：6 步，已完成的步骤可点回去改。
 * 移动端压缩成「第 N 步 / 6 · 标题」，桌面端展开成 6 个可点步骤。
 */

export const STEP_META = [
  { key: "basic", title: "基础信息", subtitle: "去哪、几天、几个人" },
  { key: "transport", title: "交通", subtitle: "怎么去" },
  { key: "hotel", title: "酒店", subtitle: "住哪一类" },
  { key: "poi", title: "想去哪里", subtitle: "必去 / 想去 / 不感兴趣" },
  { key: "pace", title: "旅行节奏", subtitle: "每天怎么玩" },
  { key: "confirm", title: "确认", subtitle: "开始规划前再看一眼" },
] as const;

export type StepKey = (typeof STEP_META)[number]["key"];

interface WizardProgressProps {
  current: number;
  onJump: (index: number) => void;
  className?: string;
}

export function WizardProgress({ current, onJump, className }: WizardProgressProps) {
  const active = STEP_META[current] ?? STEP_META[0];
  return (
    <div className={className}>
      <div className="mb-2 flex items-baseline gap-2 lg:hidden">
        <span className="text-[11px] text-muted-foreground">
          第 {current + 1} 步 / {STEP_META.length}
        </span>
        <span className="text-sm font-medium text-foreground">{active.title}</span>
      </div>

      <ol className="flex flex-wrap items-center gap-x-1 gap-y-2 sm:gap-x-2" aria-label="向导进度">
        {STEP_META.map((step, index) => {
          const done = index < current;
          const isCurrent = index === current;
          const clickable = done;
          return (
            <li key={step.key} className="flex items-center gap-1 sm:gap-2">
              <button
                type="button"
                aria-current={isCurrent ? "step" : undefined}
                onClick={() => {
                  if (clickable) onJump(index);
                }}
                disabled={!clickable}
                title={clickable ? `返回「${step.title}」修改` : step.title}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] transition-colors sm:text-xs",
                  isCurrent && "border-primary/50 bg-accent font-medium text-accent-foreground",
                  done && "border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground",
                  !done && !isCurrent && "cursor-default border-dashed border-border text-muted-foreground/70",
                )}
              >
                <span
                  className={cn(
                    "inline-flex size-4 items-center justify-center rounded-full text-[10px]",
                    isCurrent && "bg-primary text-primary-foreground",
                    done && "bg-success-subtle text-success-subtle-foreground",
                    !done && !isCurrent && "bg-muted text-muted-foreground",
                  )}
                  aria-hidden
                >
                  {done ? <Check className="size-2.5" /> : index + 1}
                </span>
                <span className="whitespace-nowrap">{step.title}</span>
              </button>
              {index < STEP_META.length - 1 ? (
                <span
                  className={cn("hidden h-px w-4 sm:block", done ? "bg-success/50" : "bg-border")}
                  aria-hidden
                />
              ) : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
