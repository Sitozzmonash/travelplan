"use client";

import { CalendarDays, CircleAlert, Users, Wallet } from "lucide-react";
import { Input } from "@/components/ui/input";
import { CityCombobox } from "@/components/city-combobox";
import { cn } from "@/lib/utils";
import type { BudgetMode, BasicDraft } from "./draft";
import { resolvedDays } from "./draft";
import { StepSection } from "./option-cards";
import { BUDGET_MODE_LABELS, formatTripLength } from "./options";

/**
 * Step 1 基础信息（§3）。
 * 这是唯一必填的一步，也是唯一会触发「创建 Session / 后台 Prefetch」的一步。
 * 预算可填数字，也可以「还没想好」或「随便，帮我控制」——三者都会原样传给后端。
 */

const BUDGET_MODES: BudgetMode[] = ["amount", "undecided", "auto"];

interface StepBasicProps {
  value: BasicDraft;
  onChange: (patch: Partial<BasicDraft>) => void;
  issues: string[];
  showIssues: boolean;
  busy: boolean;
}

export function StepBasic({ value, onChange, issues, showIssues, busy }: StepBasicProps) {
  const days = resolvedDays(value);
  return (
    <div className="grid gap-6">
      <StepSection
        title="这趟去哪里？"
        hint="出发地与目的地会决定后台去查哪条线路的交通、酒店和攻略。"
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="出发地">
            <CityCombobox
              value={value.origin}
              onChange={(next) => onChange({ origin: next })}
              placeholder="北京"
              ariaLabel="出发地"
            />
          </Field>
          <Field label="目的地">
            <CityCombobox
              value={value.destination}
              onChange={(next) => onChange({ destination: next })}
              placeholder="成都"
              ariaLabel="目的地"
            />
          </Field>
        </div>
      </StepSection>

      <StepSection title="什么时候出发，玩多久？" hint="改成别的目的地或日期时，系统会重新整理攻略地点，不会沿用旧城市的点。">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="出发日期">
            <Input
              type="date"
              value={value.startDate}
              aria-label="出发日期"
              onChange={(event) => onChange({ startDate: event.target.value })}
            />
          </Field>

          <div className="grid gap-2">
            <span className="text-[11px] text-muted-foreground">行程长度</span>
            <div className="flex gap-2">
              <SegmentedButton
                selected={value.durationMode === "days"}
                onClick={() => onChange({ durationMode: "days" })}
              >
                按天数
              </SegmentedButton>
              <SegmentedButton
                selected={value.durationMode === "endDate"}
                onClick={() => onChange({ durationMode: "endDate" })}
              >
                按返程日期
              </SegmentedButton>
            </div>
            {value.durationMode === "days" ? (
              <Input
                value={value.days}
                inputMode="numeric"
                aria-label="行程天数"
                placeholder="5"
                onChange={(event) => onChange({ days: event.target.value })}
              />
            ) : (
              <Input
                type="date"
                value={value.endDate}
                aria-label="返程日期"
                onChange={(event) => onChange({ endDate: event.target.value })}
              />
            )}
          </div>
        </div>
        <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <CalendarDays className="size-3" aria-hidden />
          {days ? `共 ${formatTripLength(days)}` : "填好日期后这里会显示总天数"}
        </p>
      </StepSection>

      <StepSection title="几个人去？" hint="人数会影响酒店房型与每天用车的建议。">
        <div className="grid gap-3 sm:max-w-[220px]">
          <Field label="出行人数">
            <Input
              value={value.travelers}
              inputMode="numeric"
              aria-label="出行人数"
              placeholder="2"
              onChange={(event) => onChange({ travelers: event.target.value })}
            />
          </Field>
        </div>
      </StepSection>

      <StepSection
        title="预算（可选）"
        hint="不确定就先空着：可以填一个数，也可以直接交给系统控制。预算会参与交通、酒店与总花费的取舍。"
      >
        <div className="flex flex-wrap gap-2">
          {BUDGET_MODES.map((mode) => (
            <SegmentedButton
              key={mode}
              selected={value.budgetMode === mode}
              onClick={() => onChange({ budgetMode: mode })}
            >
              {BUDGET_MODE_LABELS[mode]}
            </SegmentedButton>
          ))}
        </div>
        {value.budgetMode === "amount" ? (
          <div className="grid gap-3 sm:max-w-[220px]">
            <Field label="总预算（元）">
              <Input
                value={value.budgetAmount}
                inputMode="numeric"
                aria-label="总预算"
                placeholder="6000"
                onChange={(event) => onChange({ budgetAmount: event.target.value })}
              />
            </Field>
          </div>
        ) : (
          <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <Wallet className="size-3" aria-hidden />
            {value.budgetMode === "undecided"
              ? "记下了「还没想好」：系统不会用预算卡你，只会在结果里给出花费明细。"
              : "记下了「随便，帮我控制」：系统会按性价比控制总花费，并在结果里说明取舍。"}
          </p>
        )}
      </StepSection>

      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Users className="size-3" aria-hidden />
        {busy ? "正在按这份基础信息创建会话并开始后台找交通、酒店与攻略…" : "点「继续」后，系统会立刻开始后台查询，你不用等它跑完。"}
      </p>

      {showIssues && issues.length > 0 ? (
        <ul className="grid gap-1 rounded-lg border border-danger/25 bg-danger-subtle px-3.5 py-3 text-xs text-danger-subtle-foreground">
          {issues.map((issue) => (
            <li key={issue} className="flex items-start gap-1.5">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <span>{issue}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function SegmentedButton({
  selected,
  onClick,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "inline-flex min-h-[40px] items-center rounded-lg border px-3 text-xs transition-colors sm:min-h-[36px]",
        selected
          ? "border-primary/50 bg-accent font-medium text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}
