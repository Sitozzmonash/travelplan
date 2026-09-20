"use client";

import { ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ProviderStatus, SourceRef } from "@/types/plan";
import { formatDateTime } from "@/lib/format";

const PROVIDER_LABELS: Record<string, string> = {
  途牛: "途牛",
  "12306": "12306",
  高德: "高德",
  小红书: "小红书",
  抖音: "抖音",
  Tavily: "Tavily",
  amap: "高德",
  tuniu: "途牛",
  railway_12306: "12306",
  tikhub: "TikHub",
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

interface SourceBadgeProps {
  provider: string;
  status?: ProviderStatus | string;
  onClick?: () => void;
  className?: string;
}

/** 来源 Badge（FRONTEND_DESIGN §20）。点击查看 Provider / Fetched At / Source URL / 查询条件。 */
export function SourceBadge({ provider, status, onClick, className }: SourceBadgeProps) {
  const degraded = status && status !== "OK";
  const content = (
    <>
      <span className="size-1.5 rounded-full bg-current opacity-45" aria-hidden />
      {providerLabel(provider)}
      {degraded ? <span className="opacity-70">·降级</span> : null}
    </>
  );

  const base = cn(
    "inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/50 px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground",
    onClick && "transition-colors hover:bg-muted hover:text-foreground",
    className,
  );

  if (!onClick) {
    return (
      <span className={base} title={degraded ? `${providerLabel(provider)}：本次调用降级返回` : undefined}>
        {content}
      </span>
    );
  }

  return (
    <button type="button" onClick={onClick} className={base} title="查看来源与查询条件">
      {content}
    </button>
  );
}

interface SourceListProps {
  source: SourceRef;
  className?: string;
}

/** 来源详情卡片：只展示查询条件与时间，永不展示任何 API Token。 */
export function SourceDetail({ source, className }: SourceListProps) {
  const degraded = source.status !== "OK";
  return (
    <div
      className={cn("rounded-lg border border-border/80 bg-card px-3.5 py-3", className)}
      data-testid="source-detail"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <SourceBadge provider={source.provider} status={source.status} />
            <span className="truncate text-xs font-medium text-foreground">{source.title}</span>
          </div>
          {degraded ? (
            <p className="mt-1.5 text-[11px] leading-5 text-warning-subtle-foreground">
              本次调用为降级返回，结果来自缓存或部分可用数据。
            </p>
          ) : null}
        </div>
      </div>

      <dl className="mt-2.5 grid gap-1.5 text-[11px] leading-5">
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-muted-foreground">Provider</dt>
          <dd className="min-w-0 break-words text-foreground">{providerLabel(source.provider)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-muted-foreground">Fetched At</dt>
          <dd className="tabular text-foreground">{formatDateTime(source.fetched_at)}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-muted-foreground">查询条件</dt>
          <dd className="min-w-0 break-words text-foreground">
            {Object.entries(source.query)
              .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(" / ") : String(value)}`)
              .join("，") || "—"}
          </dd>
        </div>
        {source.source_url ? (
          <div className="flex gap-2">
            <dt className="w-20 shrink-0 text-muted-foreground">Source URL</dt>
            <dd className="min-w-0">
              <a
                href={source.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 break-all text-primary hover:underline"
              >
                {source.source_url}
                <ExternalLink className="size-3 shrink-0" aria-hidden />
              </a>
            </dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}
