import { ArrowRight, Sparkles, WandSparkles } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SummaryModel } from "./draft";

/**
 * 桌面端右侧常驻「你的旅行偏好摘要」（§15）。
 * 内容直接来自本地草稿 + 已归一化的 Session，随选择实时更新；
 * 与确认页共用 summarize()，保证两处说法一致。
 */

interface SummaryPanelProps {
  model: SummaryModel;
  className?: string;
}

export function SummaryPanel({ model, className }: SummaryPanelProps) {
  return (
    <div className={cn("rounded-xl border border-border bg-card px-4 py-4 shadow-sm", className)}>
      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Sparkles className="size-3" aria-hidden />
        你的旅行偏好摘要
      </p>

      <p className="mt-2 flex items-center gap-1.5 text-sm font-medium text-foreground">
        <span className="truncate">{model.origin}</span>
        <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="truncate">{model.destination}</span>
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {model.lengthLabel} · {model.travelersLabel} · {model.dateLabel}
      </p>

      <dl className="mt-3 grid gap-1.5 border-t border-border/70 pt-3 text-xs">
        <SummaryRow label="预算" value={model.budgetLabel} />
        <SummaryRow label="交通" value={`${model.transportMode} · ${model.transportPriority}`} />
        <SummaryRow label="酒店" value={model.hotelPriority} />
        <SummaryRow label="节奏" value={model.paceLabel} />
      </dl>

      <div className="mt-3 grid gap-1.5 border-t border-border/70 pt-3 text-xs">
        <PoiRow label="必去" group={model.must} />
        <PoiRow label="想去" group={model.want} />
        <PoiRow label="想吃" group={model.food} />
        {model.reject.count > 0 ? <SummaryRow label="不感兴趣" value={`${model.reject.count} 个`} /> : null}
      </div>

      <p className="mt-3 flex items-start gap-1.5 rounded-md bg-muted/50 px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
        <WandSparkles className="mt-0.5 size-3 shrink-0" aria-hidden />
        <span>
          {model.poiModeLabel
            ? `地点：${model.poiModeLabel}`
            : model.must.count + model.want.count + model.food.count === 0
              ? "没有勾选时使用这份推荐，并排除不感兴趣的地点。"
              : "优先安排你选中的推荐地点，并排除不感兴趣的地点。"}
        </span>
      </p>
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right text-foreground/90">{value}</dd>
    </div>
  );
}

function PoiRow({
  label,
  group,
}: {
  label: string;
  group: { count: number; names: string[] };
}) {
  const value =
    group.count === 0
      ? "—"
      : group.count <= 2
        ? group.names.join("、")
        : `${group.names.slice(0, 2).join("、")} 等 ${group.count} 个`;
  return <SummaryRow label={`${label}（${group.count}）`} value={value} />;
}
