"use client";

import { CircleX, Download, FileBraces } from "lucide-react";
import { useEffect, useState } from "react";
import type { AuditReport } from "@/types/api";
import type { DecisionStatus } from "@/types/plan";
import { type ApiErrorKind, apiErrorKind, describeApiError, getArtifactUrl, getAudit, USE_MOCK_API } from "@/lib/api";
import { formatProviderQuery, formatStamp } from "@/lib/format";
import { providerLabel, sourceStatusLabel, TONE_CHIP, type DisplayTone } from "@/lib/display";
import { EmptyState, ErrorState, LoadingState, UnauthorizedState } from "@/components/state-views";
import { SidePanel } from "@/components/side-panel";
import { cn } from "@/lib/utils";

interface AuditDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string | null;
}

const STATUS_STYLE: Record<string, string> = {
  SELECT: "bg-success-subtle text-success-subtle-foreground",
  USED: "bg-success-subtle text-success-subtle-foreground",
  KEEP: "bg-muted text-muted-foreground",
  PASS: "bg-muted text-muted-foreground",
  NOT_USED: "bg-muted text-muted-foreground",
  REJECT: "bg-danger-subtle text-danger-subtle-foreground",
};

const STATUS_LABELS: Record<DecisionStatus | string, string> = {
  SELECT: "选定",
  USED: "已使用",
  KEEP: "保留",
  PASS: "跳过",
  NOT_USED: "未使用",
  REJECT: "已排除",
};

const PROVIDER_STATUS_TONES: Record<string, DisplayTone> = {
  成功: "success",
  部分可用: "warning",
  失败: "danger",
  超时: "warning",
  无数据: "muted",
};

const ENTITY_PREFIX: Record<string, string> = {
  place: "地点",
  transport: "交通",
  hotel: "酒店",
  flight: "航班",
  train: "车次",
  food: "餐饮",
  attraction: "景点",
};

/** `place:武侯祠` → `地点 · 武侯祠`；没有前缀时原样返回，避免露出机器标识。 */
function entityLabel(entityId: string): string {
  const index = entityId.indexOf(":");
  if (index < 0) return entityId;
  const prefix = entityId.slice(0, index);
  const name = entityId.slice(index + 1) || prefix;
  const label = ENTITY_PREFIX[prefix];
  return label ? `${label} · ${name}` : name;
}

/**
 * 规划依据（Audit Drawer，FRONTEND_DESIGN §21）。
 * 产品化展示：链路、决策、数据源调用、时间线与产物下载入口。
 * 页面只出现中文文案，不展示 run_id、工具名、英文决策码等工程字段。
 */
export function AuditDrawer({ open, onOpenChange, runId }: AuditDrawerProps) {
  const [state, setState] = useState<{
    runId: string | null;
    report: AuditReport | null;
    error: string | null;
    errorKind: ApiErrorKind | null;
  }>({
    runId: null,
    report: null,
    error: null,
    errorKind: null,
  });

  useEffect(() => {
    if (!open || !runId) return;
    let active = true;
    getAudit(runId)
      .then((value) => {
        if (active) setState({ runId, report: value, error: null, errorKind: null });
      })
      .catch((cause) => {
        if (active) {
          setState({ runId, report: null, error: describeApiError(cause), errorKind: apiErrorKind(cause) });
        }
      });
    return () => {
      active = false;
    };
  }, [open, runId]);

  const loading = Boolean(open && runId && state.runId !== runId);
  const report = state.runId === runId ? state.report : null;
  const error = state.runId === runId ? state.error : null;
  const errorKind = state.runId === runId ? state.errorKind : null;

  return (
    <SidePanel
      open={open}
      onOpenChange={onOpenChange}
      title="规划依据"
      description={
        report
          ? `展示这次规划是怎么得出结论的 · 生成于 ${formatStamp(report.generated_at)}`
          : "展示本次规划是怎么得出结论的，不包含任何密钥或工程内部日志。"
      }
      widthClassName="sm:max-w-2xl"
      footer={
        report ? (
          <div className="flex flex-wrap items-center gap-2">
            {report.artifacts.map((artifact) => {
              const url = runId ? getArtifactUrl(runId, artifact.filename) : null;
              const disabled = !artifact.available || !url;
              return (
                <a
                  key={artifact.filename}
                  href={disabled ? undefined : (url ?? undefined)}
                  download={disabled ? undefined : artifact.filename}
                  aria-disabled={disabled}
                  title={
                    disabled
                      ? USE_MOCK_API
                        ? "演示数据未接入产物下载"
                        : "该产物本次未生成"
                      : `下载 ${artifact.filename}`
                  }
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[11px]",
                    disabled
                      ? "cursor-not-allowed text-muted-foreground/60"
                      : "text-foreground transition-colors hover:bg-muted",
                  )}
                >
                  {disabled ? <CircleX className="size-3.5" aria-hidden /> : <Download className="size-3.5" aria-hidden />}
                  下载 {artifact.label}
                </a>
              );
            })}
            <span className="text-[11px] text-muted-foreground">
              {USE_MOCK_API ? "演示数据：产物下载需接入后端" : "产物由后端生成并保留"}
            </span>
          </div>
        ) : null
      }
    >
      {loading ? (
        <LoadingState
          title="正在读取本次规划的依据"
          description="读取决策记录、数据源调用与产物清单，不会包含任何密钥。"
        />
      ) : error ? (
        errorKind === "unauthorized" ? (
          <UnauthorizedState
            title="没有权限读取规划依据"
            description={error}
            detail="行程本身仍然可用，只是这次的决策链路暂时取不到。"
          />
        ) : (
          <ErrorState
            title="无法读取规划依据"
            description={error}
            detail="行程本身仍然可用，只是这次的决策链路暂时取不到。"
          />
        )
      ) : report ? (
        <div className="space-y-6" data-testid="audit-drawer">
          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">规划链路</h3>
            <div className="space-y-2.5">
              {report.stages.map((stage) => (
                <div key={stage.id} className="rounded-lg border border-border/80 bg-card px-3.5 py-3">
                  <div className="flex items-baseline justify-between gap-3">
                    <h4 className="text-sm font-medium text-foreground">{stage.title}</h4>
                    <span className="text-[11px] text-muted-foreground">{stage.summary}</span>
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {stage.steps.map((step) => (
                      <li key={step} className="flex gap-2 text-xs leading-5 text-muted-foreground">
                        <span className="mt-2 size-1 shrink-0 rounded-full bg-border" aria-hidden />
                        <span className="min-w-0">{step}</span>
                      </li>
                    ))}
                  </ul>
                  {stage.stats?.length ? (
                    <dl className="mt-2.5 flex flex-wrap gap-x-5 gap-y-1 border-t border-border/60 pt-2.5 text-[11px]">
                      {stage.stats.map((stat) => (
                        <div key={stat.label} className="flex gap-1.5">
                          <dt className="text-muted-foreground">{stat.label}</dt>
                          <dd className="tabular font-medium text-foreground">{stat.value}</dd>
                        </div>
                      ))}
                    </dl>
                  ) : null}
                </div>
              ))}
            </div>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">关键决策</h3>
            <div className="space-y-2">
              {report.decisions.map((decision) => (
                <div
                  key={`${decision.entity_id}-${decision.status}`}
                  className="rounded-lg border border-border/80 bg-card px-3.5 py-3"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 text-[11px]",
                        STATUS_STYLE[decision.status] ?? "bg-muted text-muted-foreground",
                      )}
                    >
                      {STATUS_LABELS[decision.status] ?? decision.status}
                    </span>
                    <span className="min-w-0 break-words text-xs font-medium text-foreground">
                      {entityLabel(decision.entity_id)}
                    </span>
                  </div>
                  <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{decision.reason_text}</p>
                </div>
              ))}
            </div>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">数据源调用</h3>
            {/* 用卡片 + 定义列表而不是表格：4 列表格在 375px 下必然横向溢出。 */}
            <ul className="space-y-2">
              {report.provider_calls.map((call, index) => {
                const statusLabel = sourceStatusLabel(call.status);
                const tone = PROVIDER_STATUS_TONES[statusLabel] ?? "muted";
                return (
                  <li
                    key={`${call.provider}-${index}`}
                    className="rounded-lg border border-border/80 bg-card px-3.5 py-3"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className="text-xs font-medium text-foreground">{providerLabel(call.provider)}</span>
                        <span className={cn("rounded px-1.5 py-0.5 text-[11px]", TONE_CHIP[tone])}>{statusLabel}</span>
                      </div>
                      <span className="tabular shrink-0 text-xs text-foreground">
                        返回 {call.returned ?? "—"}
                        {call.duration_ms ? (
                          <span className="text-[11px] text-muted-foreground"> · 用时 {call.duration_ms} ms</span>
                        ) : null}
                      </span>
                    </div>

                    <dl className="mt-2 grid gap-1 text-[11px] leading-5">
                      <div className="flex gap-2">
                        <dt className="w-14 shrink-0 text-muted-foreground">查询条件</dt>
                        <dd className="min-w-0 break-all text-foreground/90">{formatProviderQuery(call.query)}</dd>
                      </div>
                      {call.note ? (
                        <div className="flex gap-2">
                          <dt className="w-14 shrink-0 text-muted-foreground">说明</dt>
                          <dd className="min-w-0 text-warning-subtle-foreground">{call.note}</dd>
                        </div>
                      ) : null}
                    </dl>
                  </li>
                );
              })}
            </ul>
            {report.provider_calls.some((call) => call.status !== "OK") ? (
              <p className="text-[11px] leading-5 text-muted-foreground">
                标记为「部分可用」的调用使用了缓存或部分数据，行程中对应部分会单独标注。
              </p>
            ) : null}
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">执行时间线</h3>
            <ol className="space-y-1.5 border-l border-border pl-4">
              {report.timeline.map((event, index) => (
                <li key={`${event.at}-${index}`} className="relative text-xs leading-5">
                  <span className="absolute -left-[19px] top-2 size-1.5 rounded-full bg-border" aria-hidden />
                  <span className="tabular text-muted-foreground">{event.at}</span>
                  <span className="mx-1.5 text-border">|</span>
                  <span className="text-muted-foreground">{event.stage}</span>
                  <span className="mx-1.5 text-border">|</span>
                  <span className="text-foreground/90">{event.text}</span>
                </li>
              ))}
            </ol>
          </section>

          <section className="space-y-2.5">
            <h3 className="text-xs font-medium text-muted-foreground">产物</h3>
            <div className="space-y-2">
              {report.artifacts.map((artifact) => (
                <div
                  key={artifact.filename}
                  className="flex items-center gap-2.5 rounded-lg border border-border/80 bg-card px-3.5 py-2.5"
                >
                  <FileBraces className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium text-foreground">{artifact.label}</p>
                    <p className="text-[11px] text-muted-foreground">
                      {artifact.note ??
                        (artifact.available ? "由后端生成，可通过下方按钮下载。" : "本次未生成该产物。")}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <p className="text-[11px] leading-5 text-muted-foreground">
            页面只读取后端保存的决策记录与产物清单，访问密钥、请求签名等敏感信息不会返回给前端。
          </p>
        </div>
      ) : (
        <EmptyState
          title="没有可展示的规划依据"
          description="这次请求没有返回决策记录，可能是规划尚未完成或结果来自缓存。"
        />
      )}
    </SidePanel>
  );
}
