"use client";

import type { ReactNode } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { PreferenceSentinel } from "@/types/session";
import type { Option } from "./options";

/**
 * 向导的三种基础选择形态：
 *   ChoiceGrid  单选卡片（方式 / 策略 / 节奏）——移动端大按钮，每步一屏
 *   ChipToggle  多选标签（附加约束）
 *   SentinelRow 「不限 / 不确定 / 帮我选」——数值与布尔补充项共用
 *
 * 三者都把 value 原样交回调用方，由 draft.ts 映射成后端枚举，页面不做二次翻译。
 */

interface ChoiceGridProps<T extends string | number | boolean> {
  options: Option<T>[];
  value: T | null;
  onChange: (value: T) => void;
  ariaLabel: string;
  columns?: 1 | 2 | 3;
  /** 移动端触摸目标高度：默认大按钮。 */
  dense?: boolean;
}

export function ChoiceGrid<T extends string | number | boolean>({
  options,
  value,
  onChange,
  ariaLabel,
  columns = 2,
  dense = false,
}: ChoiceGridProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        "grid gap-2",
        columns === 1 && "grid-cols-1",
        columns === 2 && "grid-cols-1 sm:grid-cols-2",
        columns === 3 && "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
      )}
    >
      {options.map((option) => {
        const selected = value === option.value;
        return (
          <button
            key={String(option.value)}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "flex w-full items-start gap-2.5 rounded-xl border px-3.5 text-left transition-colors",
              dense ? "py-2.5" : "py-3 sm:min-h-[62px]",
              "min-h-[52px]",
              selected
                ? "border-primary/50 bg-accent text-accent-foreground shadow-sm"
                : "border-border bg-card text-foreground hover:border-primary/30 hover:bg-muted/40",
            )}
          >
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">{option.label}</span>
              {option.hint ? (
                <span
                  className={cn(
                    "mt-0.5 block text-[11px] leading-4",
                    selected ? "text-accent-foreground/80" : "text-muted-foreground",
                  )}
                >
                  {option.hint}
                </span>
              ) : null}
            </span>
            <span
              className={cn(
                "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border",
                selected ? "border-primary bg-primary text-primary-foreground" : "border-border",
              )}
              aria-hidden
            >
              {selected ? <Check className="size-3" /> : null}
            </span>
          </button>
        );
      })}
    </div>
  );
}

interface ChipToggleProps<T extends string | number> {
  options: Option<T>[];
  values: T[];
  onToggle: (value: T) => void;
  ariaLabel: string;
  /** 后端声明这条过滤不可用时整组置灰，避免用户选了一个永远不生效的条件。 */
  disabled?: boolean;
}

export function ChipToggle<T extends string | number>({
  options,
  values,
  onToggle,
  ariaLabel,
  disabled = false,
}: ChipToggleProps<T>) {
  return (
    <div role="group" aria-label={ariaLabel} className="flex flex-wrap gap-2">
      {options.map((option) => {
        const selected = values.includes(option.value);
        return (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => onToggle(option.value)}
            className={cn(
              "inline-flex min-h-[40px] items-center gap-1.5 rounded-full border px-3.5 text-xs transition-colors",
              selected
                ? "border-primary/50 bg-accent text-accent-foreground"
                : "border-border bg-card text-muted-foreground hover:text-foreground",
              disabled && "cursor-not-allowed opacity-50 hover:border-border hover:text-muted-foreground",
            )}
          >
            {selected ? <Check className="size-3" aria-hidden /> : null}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

interface SentinelRowProps {
  /** 当前是否选中了某个哨兵值；null = 用户没表态。 */
  value: PreferenceSentinel | null;
  onChange: (value: PreferenceSentinel | null) => void;
  ariaLabel: string;
  options: Option<PreferenceSentinel>[];
  /** 允许再点一次取消（回到「没表态」）。 */
  clearable?: boolean;
  /** 同 ChipToggle：整组不可用（用于「最低星级」）。 */
  disabled?: boolean;
}

export function SentinelRow({
  value,
  onChange,
  ariaLabel,
  options,
  clearable = true,
  disabled = false,
}: SentinelRowProps) {
  return (
    <div role="group" aria-label={ariaLabel} className="flex flex-wrap gap-2">
      {options.map((option) => {
        const selected = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => onChange(selected && clearable ? null : option.value)}
            className={cn(
              "inline-flex min-h-[36px] items-center rounded-full border px-3 text-xs transition-colors",
              selected
                ? "border-primary/50 bg-accent text-accent-foreground"
                : "border-dashed border-border bg-card text-muted-foreground hover:text-foreground",
              disabled && "cursor-not-allowed opacity-50 hover:border-border hover:text-muted-foreground",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function StepSection({
  title,
  hint,
  children,
  className,
}: {
  title: string;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("grid gap-2.5", className)}>
      <div>
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        {hint ? <p className="mt-1 text-[11px] leading-5 text-muted-foreground">{hint}</p> : null}
      </div>
      {children}
    </section>
  );
}
