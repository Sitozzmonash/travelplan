"use client";

import { Plane, TrainFront, WandSparkles } from "lucide-react";
import type { TransportConstraint } from "@/types/session";
import type { GuidedDraft } from "./draft";
import { transportConstraintsLabel } from "./draft";
import { ChoiceGrid, ChipToggle, StepSection } from "./option-cards";
import {
  TRANSPORT_CONSTRAINT_OPTIONS,
  TRANSPORT_MODE_OPTIONS,
  TRANSPORT_PRIORITY_OPTIONS,
  optionLabel,
} from "./options";

/**
 * Step 2 交通偏好（§4 / §6）。
 * 方式 → transport_mode；排序 → transport_priority；附加 → transport_constraints（多选）。
 * 什么都不选时，提交的是文档默认值「都可以 + 帮我选 + 时间都可以」，不是空。
 */

interface StepTransportProps {
  draft: GuidedDraft;
  onChange: (patch: Partial<GuidedDraft["transport"]>) => void;
}

export function StepTransport({ draft, onChange }: StepTransportProps) {
  const { mode, priority, constraints } = draft.transport;

  function toggleConstraint(value: TransportConstraint) {
    if (value === "any_time") {
      onChange({ constraints: constraints.includes("any_time") ? [] : ["any_time"] });
      return;
    }
    const without = constraints.filter((item) => item !== "any_time");
    onChange({
      constraints: without.includes(value) ? without.filter((item) => item !== value) : [...without, value],
    });
  }

  return (
    <div className="grid gap-6">
      <StepSection title="你更在意怎么去？" hint="选「随便，帮我选」时，系统会按真实价格、门到门时间与接驳稳定判断，不会随机挑。">
        <ChoiceGrid
          ariaLabel="交通方式"
          options={TRANSPORT_MODE_OPTIONS}
          value={mode}
          onChange={(next) => onChange({ mode: next })}
        />
      </StepSection>

      <StepSection title="排序上更看重什么？" hint="这一项会直接进入交通候选的评分，不是只写进备注。">
        <ChoiceGrid
          ariaLabel="交通排序偏好"
          options={TRANSPORT_PRIORITY_OPTIONS}
          value={priority}
          onChange={(next) => onChange({ priority: next })}
          columns={3}
          dense
        />
      </StepSection>

      <StepSection title="有没有要避开的？" hint="可以多选。「时间都可以」表示没有额外限制。">
        <ChipToggle
          ariaLabel="交通附加约束"
          options={TRANSPORT_CONSTRAINT_OPTIONS}
          values={constraints}
          onToggle={toggleConstraint}
        />
      </StepSection>

      <p className="flex items-start gap-1.5 rounded-lg bg-muted/50 px-3 py-2.5 text-[11px] leading-5 text-muted-foreground">
        <WandSparkles className="mt-0.5 size-3 shrink-0" aria-hidden />
        <span>
          将提交：
          {optionLabel(TRANSPORT_MODE_OPTIONS, mode) ?? "都可以（默认）"} ·{" "}
          {optionLabel(TRANSPORT_PRIORITY_OPTIONS, priority) ?? "帮我选（默认）"} ·{" "}
          {transportConstraintsLabel(constraints).join(" / ")}
          {mode === "train" ? (
            <>
              {" "}
              <TrainFront className="inline size-3 align-[-2px]" aria-hidden />
            </>
          ) : null}
          {mode === "flight" ? (
            <>
              {" "}
              <Plane className="inline size-3 align-[-2px]" aria-hidden />
            </>
          ) : null}
        </span>
      </p>
    </div>
  );
}
