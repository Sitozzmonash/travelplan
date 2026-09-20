"use client";

import { CircleCheck, ExternalLink, Quote, TriangleAlert } from "lucide-react";
import type { Evidence, ItineraryItem } from "@/types/plan";
import { formatDateTime } from "@/lib/format";
import { providerLabel } from "@/components/source-badge";
import { ScoreRow } from "@/components/trust-badge";
import { ItemTypeIcon, ITEM_TYPE_LABELS } from "@/components/item-icon";
import { EmptyState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";

interface EvidenceDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  item: ItineraryItem | null;
  evidence: Evidence[];
}

/**
 * Evidence Drawer（FRONTEND_DESIGN §13）。
 * 回答「为什么推荐这里」：综合判断 → 推荐原因 → 风险说明 → 来源卡片。
 * 只展示来源摘要，从不展示抓取到的完整长原文。
 */
export function EvidenceDrawer({ open, onOpenChange, item, evidence }: EvidenceDrawerProps) {
  const reasonPoints = item?.reason_points?.length ? item.reason_points : item ? [item.reason] : [];

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
                label="Trust（可信度）"
                score={item.trust_score}
                caption="来自后端对来源质量、独立复述与 POI 校验结果的综合评分。"
                tone="trust"
              />
              <ScoreRow
                label="Ad Risk（广告风险）"
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
            <h3 className="text-xs font-medium text-muted-foreground">风险说明</h3>
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
              <span className="text-[11px] text-muted-foreground">仅显示摘要，不展示完整原文</span>
            </div>
            {evidence.length ? (
              <div className="space-y-2.5">
                {evidence.map((entry) => (
                  <EvidenceCard key={entry.id} evidence={entry} />
                ))}
              </div>
            ) : (
              <EmptyState
                title="这条安排没有社媒来源"
                description="它的依据是交通时刻表、票价与路线数据，没有对应的小红书或抖音内容，因此这里没有可展示的摘要。"
                hint="时刻表与票价为实时查询结果，出行前请以官方渠道为准。"
              />
            )}
          </section>
        </div>
      ) : null}
    </SidePanel>
  );
}

function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const metrics = Object.entries(evidence.raw_metrics ?? {});
  return (
    <article className="rounded-lg border border-border/80 bg-card px-3.5 py-3" data-testid="evidence-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Quote className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <span className="text-xs font-medium text-foreground">{providerLabel(evidence.provider)}</span>
          <span className="truncate text-xs text-muted-foreground">{evidence.source_type === "social" ? "用户内容" : "公开数据"}</span>
        </div>
        {evidence.source_url ? (
          <a
            href={evidence.source_url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex shrink-0 items-center gap-1 text-[11px] text-primary hover:underline"
          >
            查看来源
            <ExternalLink className="size-3" aria-hidden />
          </a>
        ) : null}
      </div>

      <p className="mt-2 text-xs font-medium leading-5 text-foreground">{evidence.title}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{evidence.text}</p>

      <dl className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <div className="flex gap-1.5">
          <dt>作者</dt>
          <dd className="text-foreground/90">{evidence.author ?? "—"}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>发布时间</dt>
          <dd className="tabular text-foreground/90">{formatDateTime(evidence.published_at)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>查询时间</dt>
          <dd className="tabular text-foreground/90">{formatDateTime(evidence.fetched_at)}</dd>
        </div>
        {metrics.length ? (
          <div className="flex gap-1.5">
            <dt>热度</dt>
            <dd className="tabular text-foreground/90">
              {metrics.map(([key, value]) => `${metricLabel(key)} ${String(value ?? "—")}`).join(" · ")}
            </dd>
          </div>
        ) : null}
      </dl>

      {evidence.specific_dishes.length ? (
        <p className="mt-2 text-[11px] text-muted-foreground">
          提到菜品：<span className="text-foreground/90">{evidence.specific_dishes.join("、")}</span>
        </p>
      ) : null}
    </article>
  );
}

const METRIC_LABELS: Record<string, string> = {
  likes: "点赞",
  collects: "收藏",
  comments: "评论",
  cooperation_tag: "合作标识",
  poi_count: "POI 数",
  matched: "匹配",
  results: "检索结果",
  trusted_domains: "可信域名",
  distance_meters: "距离(m)",
  duration_minutes: "耗时(分)",
  fare: "票价",
};

function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key;
}
