"use client";

import { CircleCheck, TriangleAlert } from "lucide-react";
import type { Evidence, ItineraryItem } from "@/types/plan";
import { buildEvidenceRecords, providerLabel } from "@/lib/display";
import { ScoreRow } from "@/components/trust-badge";
import { ItemTypeIcon, ITEM_TYPE_LABELS } from "@/components/item-icon";
import { EmptyState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";
import { SourceRecordCard } from "@/components/sources-drawer";

interface EvidenceDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: ItineraryItem | null;
  evidence: Evidence[];
}

/**
 * Evidence Drawer（FRONTEND_DESIGN §13）。
 * 回答「为什么推荐这里」：综合判断 → 推荐原因 → 风险说明 → 来源（来源 / 时间 / 内容）。
 * 来源一律用中文来源名 + 抓取时间 + 正文前 200 字，不出现任何 URL。
 */
export function EvidenceDrawer({ open, onOpenChange, item, evidence }: EvidenceDrawerProps) {
  const reasonPoints = item?.reason_points?.length ? item.reason_points : item ? [item.reason] : [];
  const records = buildEvidenceRecords(evidence);

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title={item ? `为什么推荐「${item.name}」` : "推荐依据"}
      description={
        item
          ? `${ITEM_TYPE_LABELS[item.type]}${item.area ? ` · ${item.area}` : ""} · 依据 ${evidence.length} 条来源`
          : undefined
      }
    >
      {item ? (
        <div className="space-y-6" data-testid="evidence-drawer">
          <section className="space-y-3">
            <h3 className="text-xs font-medium text-muted-foreground">综合判断</h3>
            <div className="flex items-start gap-2.5 rounded-lg border border-border/80 bg-muted/25 px-3.5 py-3">
              <ItemTypeIcon type={item.type} transportMode={item.transport_mode} className="mt-0.5 text-muted-foreground" />
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">{item.name}</p>
                <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{item.reason}</p>
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <ScoreRow
                label="可信度"
                score={item.trust_score}
                caption="来自后端对来源质量、独立复述与地点校验结果的综合评分。"
                tone="trust"
              />
              <ScoreRow
                label="广告风险"
                score={item.ad_risk}
                caption="来自后端对推广标识、发布密度与措辞雷同度的综合评分。"
                tone="adRisk"
              />
            </div>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">推荐原因</h3>
            <ul className="space-y-2">
              {reasonPoints.map((point) => (
                <li key={point} className="flex gap-2 text-xs leading-5 text-foreground">
                  <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-success-subtle-foreground" aria-hidden />
                  <span className="min-w-0">{point}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">风险提示</h3>
            {item.risk_note ? (
              <div className="flex gap-2 rounded-lg border border-warning/25 bg-warning-subtle/70 px-3.5 py-3">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning-subtle-foreground" aria-hidden />
                <p className="min-w-0 text-xs leading-5 text-warning-subtle-foreground">{item.risk_note}</p>
              </div>
            ) : (
              <p className="text-xs leading-5 text-muted-foreground">
                本次未发现需要注意的风险项。价格为查询时点结果，出行前可能变化。
              </p>
            )}
          </section>

          <section className="space-y-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-xs font-medium text-muted-foreground">来源</h3>
              <span className="text-[11px] text-muted-foreground">每条显示来源、抓取时间与内容</span>
            </div>
            {records.length ? (
              <div className="space-y-2.5">
                {records.map((record, index) => (
                  <EvidenceCard key={record.key} record={record} evidence={evidence[index]} />
                ))}
              </div>
            ) : (
              <EmptyState
                title="这条安排没有社媒来源"
                description="它的依据是交通时刻表、票价与路线数据，没有对应的攻略内容，因此这里没有可展示的摘要。"
                hint="时刻表与票价为实时查询结果，出行前请以官方渠道为准。"
              />
            )}
          </section>
        </div>
      ) : null}
    </SidePanel>
  );
}

function EvidenceCard({
  record,
  evidence,
}: {
  record: ReturnType<typeof buildEvidenceRecords>[number];
  evidence: Evidence | undefined;
}) {
  const dishes = evidence?.specific_dishes ?? [];
  const author = evidence?.author ?? null;
  if (!evidence) return <SourceRecordCard record={record} />;

  return (
    <div className="space-y-1.5">
      <SourceRecordCard record={record} />
      <p className="flex flex-wrap gap-x-4 gap-y-1 px-1 text-[11px] text-muted-foreground">
        <span>
          作者：<span className="text-foreground/90">{author ?? `${providerLabel(evidence.provider)} 内容`}</span>
        </span>
        {dishes.length ? (
          <span>
            提到菜品：<span className="text-foreground/90">{dishes.join("、")}</span>
          </span>
        ) : null}
      </p>
    </div>
  );
}
