"use client";

import type { Pace } from "@/types/session";
import { ChoiceGrid, StepSection } from "./option-cards";
import { PACE_OPTIONS, optionLabel } from "./options";

/**
 * Step 5 旅行节奏（§10）。
 * 选「随便，帮我安排」会提交 pace=auto，由 Planner 按天数、候选地点数量、
 * 首末日交通与每日可用时间算出稳定节奏。
 */

interface StepPaceProps {
  pace: Pace | null;
  onChange: (pace: Pace) => void;
}

export function StepPace({ pace, onChange }: StepPaceProps) {
  return (
    <div className="grid gap-5">
      <StepSection title="你希望每天怎么玩？" hint="不选也没关系：默认交给系统按天数与地点数量安排。">
        <ChoiceGrid
          ariaLabel="旅行节奏"
          options={PACE_OPTIONS}
          value={pace}
          onChange={onChange}
          columns={1}
        />
      </StepSection>
      <p className="rounded-lg bg-muted/50 px-3 py-2.5 text-[11px] leading-5 text-muted-foreground">
        将提交：{optionLabel(PACE_OPTIONS, pace) ?? "帮我安排（默认）"}
        {pace === "auto" ? " · Planner 会按天数、地点数量与到达/返程时间自动定节奏。" : ""}
      </p>
    </div>
  );
}
