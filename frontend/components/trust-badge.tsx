"use client";

import { ChevronRight, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { TONE_BAR, TONE_CHIP, TONE_TEXT, adRiskLabel, adRiskTone, trustLabel, trustTone } from "@/lib/display";

interface EvidenceBadgeProps {
  evidenceCount: number;
  trust?: number | null;
  adRisk?: number | null;
  onClick?: () => void;
  className?: string;
}

/** 行程卡片上的 Evidence compact badge（FRONTEND_DESIGN §12），点击打开 Evidence Drawer。 */
export function EvidenceBadge({ evidenceCount, trust, adRisk, onClick, className }: EvidenceBadgeProps) {
  const trustValue = typeof trust === "number" ? trust : null;
  const riskValue = typeof adRisk === "number" ? adRisk : null;

  const body = (
    <>
      <ShieldCheck className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      <span className="tabular">{evidenceCount} 条证据</span>
      <span className="text-border">|</span>
      <span className={cn("tabular", TONE_TEXT[trustTone(trustValue)])}>
        可信度 {trustValue ?? "—"}
      </span>
      <span className="text-border">|</span>
      <span className={cn("tabular", TONE_TEXT[adRiskTone(riskValue)])}>
        广告风险 {riskValue ?? "—"}
      </span>
      {onClick ? <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden /> : null}
    </>
  );

  const base = cn(
    "inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground",
    onClick && "transition-colors hover:bg-muted hover:text-foreground",
    className,
  );

  if (!onClick) {
    return <span className={base}>{body}</span>;
  }

  return (
    <button type="button" onClick={onClick} className={base} aria-label="查看推荐依据">
      {body}
    </button>
  );
}

interface ScoreRowProps {
  label: string;
  score: number | null | undefined;
  caption: string;
  tone: "trust" | "adRisk";
  className?: string;
}

/** 分值条：只做展示，分值由后端给出。 */
export function ScoreRow({ label, score, caption, tone, className }: ScoreRowProps) {
  const value = typeof score === "number" ? score : null;
  const displayTone = tone === "trust" ? trustTone(value) : adRiskTone(value);
  const width = value === null ? 0 : Math.max(2, Math.min(100, value));

  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className={cn("tabular text-sm font-medium", TONE_TEXT[displayTone])}>
          {value === null ? "未评估" : `${value} / 100`}
        </span>
      </div>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted" role="presentation">
        <div className={cn("h-full rounded-full", TONE_BAR[displayTone])} style={{ width: `${width}%` }} />
      </div>
      <p className="mt-1 text-[11px] leading-5 text-muted-foreground">{caption}</p>
    </div>
  );
}

interface TrustChipProps {
  trust?: number | null;
  adRisk?: number | null;
  className?: string;
}

export function TrustChip({ trust, adRisk, className }: TrustChipProps) {
  const trustValue = typeof trust === "number" ? trust : null;
  const riskValue = typeof adRisk === "number" ? adRisk : null;
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      <span className={cn("rounded px-1.5 py-0.5 text-[11px]", TONE_CHIP[trustTone(trustValue)])}>
        {trustLabel(trustValue)}
        {trustValue !== null ? ` · 可信度 ${trustValue}` : ""}
      </span>
      <span className={cn("rounded px-1.5 py-0.5 text-[11px]", TONE_CHIP[adRiskTone(riskValue)])}>
        {adRiskLabel(riskValue)}
        {riskValue !== null ? ` · ${riskValue}` : ""}
      </span>
    </div>
  );
}
