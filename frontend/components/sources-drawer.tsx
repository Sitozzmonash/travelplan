"use client";

import type { SourceRef } from "@/types/plan";
import { SourceDetail } from "@/components/source-badge";
import { EmptyState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";

interface SourcesDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sources: SourceRef[];
  generatedAt: string | null;
  highlightSourceId?: string | null;
}

/**
 * 来源与查询条件（FRONTEND_DESIGN §20）。
 * 只展示 Provider、查询条件、返回时间与链接；任何 API Token 都不会出现在这里。
 */
export function SourcesDrawer({
  open,
  onOpenChange,
  sources,
  generatedAt,
  highlightSourceId,
}: SourcesDrawerProps) {
  const degraded = sources.filter((source) => source.status !== "OK");

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title="来源与查询条件"
      description={`本次规划共调用 ${sources.length} 个数据源${
        degraded.length ? `，其中 ${degraded.length} 个为降级返回` : ""
      }。`}
      widthClassName="sm:max-w-2xl"
    >
      <div className="space-y-3" data-testid="sources-drawer">
        <div className="rounded-lg border border-border/80 bg-muted/25 px-3.5 py-3 text-[11px] leading-5 text-muted-foreground">
          <p>查询时间：{generatedAt ?? "—"}</p>
          <p className="mt-1">
            所有数据通过后端 Provider 统一获取并记录来源；API Key 只保存在服务端，页面不会接触任何密钥。
          </p>
        </div>

        {sources.length ? (
          <div className="space-y-2.5">
            {sources.map((source) => (
              <div
                key={source.source_id}
                className={
                  highlightSourceId === source.source_id
                    ? "rounded-lg ring-1 ring-primary/40"
                    : undefined
                }
              >
                <SourceDetail source={source} />
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            title="本次没有可追溯的来源记录"
            description="规划结果可能来自缓存或历史数据，因此没有对应的 Provider 调用记录。"
            hint="需要完整来源时，请重新生成一次行程。"
          />
        )}
      </div>
    </SidePanel>
  );
}
