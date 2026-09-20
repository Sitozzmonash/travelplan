"use client";

import { ChevronDown, Database, Quote } from "lucide-react";
import { useState } from "react";
import type { SourceRecord } from "@/lib/display";
import { TONE_CHIP, type DisplayTone } from "@/lib/display";
import { formatStamp } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/state-views";
import { SectionCard } from "@/components/section-card";
import { SidePanel } from "@/components/side-panel";

interface SourcesDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  records: SourceRecord[];
  generatedAt: string | null;
  highlightSourceId?: string | null;
}

/**
 * 「来源」抽屉。
 *
 * 用户只想知道三件事：**谁**（来源中文名）、**什么时候**（抓取时间）、**提供了什么数据**
 * （正文前 200 字，可展开）。因此这里：
 *   - 不出现任何 URL（`source_url` / `evidence[].source_url` 一律不读不渲染）；
 *   - 不出现 provider / tool / status 这类英文技术字段，状态一律中文；
 *   - 用卡片堆叠而不是大表格，移动端单列不横向溢出。
 */
export function SourcesDrawer({
  open,
  onOpenChange,
  records,
  generatedAt,
  highlightSourceId,
}: SourcesDrawerProps) {
  const degraded = records.filter((record) => record.status !== "OK");

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title="来源"
      description={
        records.length
          ? `共 ${records.length} 条来源${degraded.length ? `，其中 ${degraded.length} 条为部分可用` : ""}。每条都标注了来源、抓取时间与内容。`
          : "本次规划没有可展示的来源记录。"
      }
      widthClassName="sm:max-w-2xl"
    >
      <div className="space-y-3" data-testid="sources-drawer">
        <div className="rounded-lg border border-border/80 bg-muted/25 px-3.5 py-3 text-[11px] leading-5 text-muted-foreground">
          <p>
            查询时间：<span className="tabular">{formatStamp(generatedAt)}</span>
          </p>
          <p className="mt-1">
            数据由后端统一获取，页面只读取结构化结果与来源名称，不展示链接，也不会接触任何密钥。
          </p>
        </div>

        {records.length ? (
          <div className="space-y-2.5">
            {records.map((record) => (
              <SourceRecordCard
                key={record.key}
                record={record}
                highlighted={Boolean(
                  highlightSourceId && record.sourceIds.includes(highlightSourceId),
                )}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            title="本次没有可追溯的来源记录"
            description="规划结果可能来自缓存或历史数据，因此没有对应的来源记录。"
            hint="需要完整来源时，请重新生成一次行程。"
          />
        )}
      </div>
    </SidePanel>
  );
}

const STATUS_TONES: Record<string, DisplayTone> = {
  成功: "success",
  部分可用: "warning",
  失败: "danger",
  超时: "warning",
  无数据: "muted",
};

interface SourceRecordCardProps {
  record: SourceRecord;
  highlighted?: boolean;
  defaultExpanded?: boolean;
}

/** 单条来源卡片：来源 + 时间 + 内容（前 200 字，可展开全文）。 */
export function SourceRecordCard({ record, highlighted = false, defaultExpanded = false }: SourceRecordCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const tone = STATUS_TONES[record.statusLabel] ?? "muted";

  return (
    <article
      className={cn(
        "rounded-lg border border-border/80 bg-card px-3.5 py-3",
        highlighted && "border-primary/45 ring-1 ring-primary/30",
      )}
      data-testid="source-card"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="inline-flex items-center gap-1.5 rounded-md bg-muted px-1.5 py-0.5 text-xs font-medium text-foreground">
          <Quote className="size-3 shrink-0 text-muted-foreground" aria-hidden />
          {record.providerName}
        </span>
        <span className="text-[11px] text-muted-foreground">{record.kindLabel}</span>
        <span className={cn("rounded px-1.5 py-0.5 text-[11px]", TONE_CHIP[tone])}>{record.statusLabel}</span>
        <span className="tabular ml-auto shrink-0 text-[11px] text-muted-foreground">
          {formatStamp(record.fetchedAt)}
        </span>
      </div>

      <p className="mt-2 whitespace-pre-line break-words text-xs leading-5 text-foreground/90">
        {expanded ? record.fullText : record.preview}
      </p>

      {record.truncated ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="mt-2 inline-flex items-center gap-1 text-[11px] text-primary transition-colors hover:underline"
          aria-expanded={expanded}
        >
          <ChevronDown className={cn("size-3 transition-transform", expanded && "rotate-180")} aria-hidden />
          {expanded ? "收起" : "展开全文"}
        </button>
      ) : null}
    </article>
  );
}

interface SourcesSummaryProps {
  records: SourceRecord[];
  onOpenAll: () => void;
  /** 一屏先给几条，其余进抽屉。 */
  limit?: number;
}

/** 「来源」摘要区：共 N 条 + 前几条，其余进抽屉（避免一屏堆满）。 */
export function SourcesSummary({ records, onOpenAll, limit = 3 }: SourcesSummaryProps) {
  const preview = records.slice(0, limit);
  const degraded = records.filter((record) => record.status !== "OK").length;

  return (
    <SectionCard
      title="来源"
      icon={Database}
      description={
        records.length
          ? `共 ${records.length} 条来源${degraded ? `，${degraded} 条为部分可用` : ""}。这里先显示 ${preview.length} 条。`
          : "本次规划没有可展示的来源记录。"
      }
      action={
        records.length ? (
          <Button variant="outline" size="sm" onClick={onOpenAll}>
            查看全部 {records.length} 条
          </Button>
        ) : undefined
      }
      contentClassName="space-y-2.5"
    >
      {preview.length ? (
        preview.map((record) => <SourceRecordCard key={record.key} record={record} />)
      ) : (
        <p className="text-xs leading-5 text-muted-foreground">
          这份行程没有附带来源记录，可能来自缓存或历史数据。需要完整来源时，可以重新生成一次行程。
        </p>
      )}
    </SectionCard>
  );
}
