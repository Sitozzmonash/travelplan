"use client";

import { Lightbulb, Wallet } from "lucide-react";
import type { BudgetSummary } from "@/types/plan";
import { SectionCard } from "@/components/section-card";
import { InlineWarning } from "@/components/state-views";
import { cn } from "@/lib/utils";
import { formatCNY, formatPercent } from "@/lib/format";

/** 分类配色：实时价格用主色系，估算费用用低饱和中性色，避免颜色过多。 */
const CATEGORY_STYLE: Record<string, { bar: string; dot: string }> = {
  大交通: { bar: "bg-primary/85", dot: "bg-primary/85" },
  住宿: { bar: "bg-primary/60", dot: "bg-primary/60" },
  门票: { bar: "bg-primary/40", dot: "bg-primary/40" },
  餐饮估算: { bar: "bg-warning/45", dot: "bg-warning/45" },
  市内交通估算: { bar: "bg-warning/30", dot: "bg-warning/30" },
  其他: { bar: "bg-muted-foreground/25", dot: "bg-muted-foreground/25" },
};

const FALLBACK_STYLE = { bar: "bg-foreground/25", dot: "bg-foreground/25" };

interface BudgetCardProps {
  budget: BudgetSummary;
  /** 换算比例：单独占一行展示的标注，例如「2 人合计」。 */
  scopeLabel?: string;
  className?: string;
}

/** 预算区块（FRONTEND_DESIGN §17）。优化建议必须来自后端，前端只展示。 */
export function BudgetCard({ budget, scopeLabel, className }: BudgetCardProps) {
  const categories = Object.entries(budget.breakdown).filter(([, value]) => value > 0);
  const total = categories.reduce((sum, [, value]) => sum + value, 0) || 1;
  const overBudget = budget.status === "over_budget";
  const overflow = budget.remaining !== null && budget.remaining < 0 ? Math.abs(budget.remaining) : 0;

  return (
    <SectionCard
      id="budget"
      title="预算"
      icon={Wallet}
      description={`实时价格与估算费用分开计算。${scopeLabel ?? ""}`}
      className={className}
      action={
        <span
          className={cn(
            "rounded-md px-2 py-0.5 text-[11px] font-medium",
            overBudget ? "bg-danger-subtle text-danger-subtle-foreground" : "bg-success-subtle text-success-subtle-foreground",
          )}
        >
          {budget.status === "unknown" ? "预算未设定" : overBudget ? "超出预算" : "预算内"}
        </span>
      }
    >
      <div className="grid gap-4">
        <div className="flex h-3 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label="预算构成">
          {categories.map(([category, value]) => (
            <span
              key={category}
              className={cn("h-full", (CATEGORY_STYLE[category] ?? FALLBACK_STYLE).bar)}
              style={{ width: `${(value / total) * 100}%` }}
              title={`${category} ${formatCNY(value)}`}
            />
          ))}
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-5">
          <Metric label="预算" value={formatCNY(budget.budget_total)} />
          <Metric label="已知实时费用" value={formatCNY(budget.known_real_cost)} tone="realtime" />
          <Metric label="估算费用" value={formatCNY(budget.estimated_cost)} tone="estimated" />
          <Metric label="预计总计" value={formatCNY(budget.projected_total)} emphasis />
          <Metric
            label="剩余"
            value={formatCNY(budget.remaining)}
            tone={overBudget ? "danger" : undefined}
            emphasis
          />
        </dl>

        {budget.budget_total ? (
          <div>
            <div className="flex items-center justify-between text-[11px] text-muted-foreground">
              <span>预算使用</span>
              <span className="tabular">
                {formatPercent(Math.min(budget.projected_total / budget.budget_total, 1.5))}
              </span>
            </div>
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className={cn("h-full rounded-full", overBudget ? "bg-danger/70" : "bg-primary/70")}
                style={{ width: `${Math.min((budget.projected_total / budget.budget_total) * 100, 100)}%` }}
              />
            </div>
          </div>
        ) : null}

        <ul className="grid gap-1.5">
          {categories.map(([category, value]) => {
            const priceType = budget.breakdown_price_type[category] ?? "estimated";
            const style = CATEGORY_STYLE[category] ?? FALLBACK_STYLE;
            return (
              <li key={category} className="flex items-center justify-between gap-3 text-xs">
                <span className="flex min-w-0 items-center gap-2 text-muted-foreground">
                  <span className={cn("size-2 shrink-0 rounded-full", style.dot)} aria-hidden />
                  <span className="truncate">{category}</span>
                  <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px]">
                    {priceType === "realtime" ? "实时价格" : "估算费用"}
                  </span>
                </span>
                <span className="tabular shrink-0 text-foreground">{formatCNY(value)}</span>
              </li>
            );
          })}
        </ul>

        {overBudget ? (
          <div className="rounded-lg border border-danger/25 bg-danger-subtle px-3.5 py-3">
            <p className="tabular text-sm font-medium text-danger-subtle-foreground">
              预计超出 {formatCNY(overflow)}
            </p>
            <p className="mt-1 text-xs leading-5 text-danger-subtle-foreground/90">
              预计总计已高于设定预算。下方的可优化项来自规划引擎，列出每一项调整能省下的金额。
            </p>
          </div>
        ) : null}

        {budget.optimization_suggestions.length ? (
          <div className="rounded-lg border border-border bg-muted/25 px-3.5 py-3">
            <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <Lightbulb className="size-3.5 text-muted-foreground" aria-hidden />
              可优化项（由规划引擎给出）
            </p>
            <ul className="mt-2 grid gap-1.5">
              {budget.optimization_suggestions.map((suggestion) => (
                <li key={suggestion} className="flex gap-2 text-xs leading-5 text-muted-foreground">
                  <span className="text-border">·</span>
                  {suggestion}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {budget.price_notes.length ? (
          <ul className="grid gap-1.5">
            {budget.price_notes.map((note) => (
              <li key={note} className="text-[11px] leading-5 text-muted-foreground">
                {note}
              </li>
            ))}
          </ul>
        ) : null}

        {!overBudget && budget.remaining !== null && budget.budget_total ? (
          budget.remaining / budget.budget_total < 0.1 ? (
            <InlineWarning
              title="预算余量偏紧"
              description={`剩余 ${formatCNY(budget.remaining)}，约占预算 ${formatPercent(
                budget.remaining / budget.budget_total,
              )}。若餐饮或市内交通超出估算，预计会超出预算。`}
            />
          ) : null
        ) : null}
      </div>
    </SectionCard>
  );
}

function Metric({
  label,
  value,
  tone,
  emphasis = false,
}: {
  label: string;
  value: string;
  tone?: "realtime" | "estimated" | "danger";
  emphasis?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "tabular mt-0.5",
          emphasis ? "text-base font-medium" : "text-sm",
          tone === "realtime" && "text-foreground",
          tone === "estimated" && "text-warning-subtle-foreground",
          tone === "danger" && "text-danger-subtle-foreground",
          !tone && "text-foreground",
        )}
      >
        {value}
      </dd>
    </div>
  );
}
