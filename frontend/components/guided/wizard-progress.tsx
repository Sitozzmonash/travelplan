import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * 向导顶部进度条（§17）：2 步，已完成的步骤可点回去改。
 * 移动端压缩成「第 N 步 / 2 · 标题」，桌面端展开成 2 个可点步骤。
 *
 * 为什么只剩 2 步：交通 / 酒店 / 节奏原来是三页，各自只问一两件事，
 * 用户要点三次「继续」才走到开始，体感很磨。它们都是"选个偏好"而不是"填信息"，
 * 合成一页不增加任何新的必填项，只少了两次跳转；末尾独立那页「确认」也去掉了 ——
 * 右侧摘要卡（桌面）与顶部一行摘要（移动）本来就在实时复述，再让用户确认一遍是多余的。
 * 真正的「基础信息」页随后也删掉了：它的 6 个字段里有 5 个与首页完全重复，
 * 用户刚在首页填完又要填一遍。基础信息现在只由首页收集、经 URL 带进来，
 * 向导直接开场就是「探索确认」，所以这里连第 1 步都不需要了。
 */

export const STEP_META = [
  { key: "explore", title: "探索确认", subtitle: "住哪一带、想去什么、想吃什么" },
  { key: "preferences", title: "偏好", subtitle: "交通、酒店、节奏，一页选完" },
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
