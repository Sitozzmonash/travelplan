"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Activity, Play, RefreshCw, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import {
  ActionError,
  InlineSpinner,
  PartialBanner,
  ResourceView,
  SectionEmpty,
} from "@/components/admin/admin-states";
import { DescriptionList, MetricList, PanelSection } from "@/components/admin/metric-list";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { StatCard } from "@/components/admin/stat-card";
import { formatNumber, statusLabel } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { SidePanel } from "@/components/side-panel";
import {
  getAdminEvolution,
  getAdminEvolutionRun,
  launchAdminEvolution,
  toAdminApiError,
  type AdminApiError,
} from "@/lib/admin-api";
import type {
  AdminEvolutionOverview,
  AdminEvolutionRunDetail,
  AdminEvolutionRunSummary,
  AdminExperience,
} from "@/types/admin";

const POLL_INTERVAL_MS = 3000;

export function EvolutionView() {
  const overview = useAdminResource<AdminEvolutionOverview>("admin-evolution", getAdminEvolution);
  const reloadOverview = overview.reload;
  const [dialogOpen, setDialogOpen] = useState(false);
  const [pollingId, setPollingId] = useState<string | null>(null);
  const [pollError, setPollError] = useState<AdminApiError | null>(null);
  const [pollNotice, setPollNotice] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    if (!pollingId) return;
    let cancelled = false;
    const timer = setInterval(() => {
      getAdminEvolutionRun(pollingId).then(
        (detail) => {
          if (cancelled) return;
          setPollError(null);
          if (detail.run.status !== "RUNNING") {
            setPollingId(null);
            setPollNotice(
              `Evolution ${pollingId} 已结束：${statusLabel(detail.run.status)}，决定 ${statusLabel(
                detail.run.decision,
              )}。`,
            );
            reloadOverview();
          }
        },
        (cause: unknown) => {
          if (!cancelled) setPollError(toAdminApiError(cause));
        },
      );
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pollingId, reloadOverview]);

  const columns: AdminColumn<AdminEvolutionRunSummary>[] = [
    {
      key: "id",
      header: "Evolution Run",
      primary: true,
      cell: (run) => <span className="font-mono text-xs break-all">{run.evolution_run_id}</span>,
    },
    { key: "status", header: "状态", cell: (run) => <StatusBadge status={run.status} /> },
    {
      key: "badcases",
      header: "Bad Case",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.badcase_count)}</span>,
    },
    {
      key: "experiences",
      header: "经验",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatNumber(run.experience_count)}</span>,
    },
    {
      key: "decision",
      header: "决定",
      cell: (run) =>
        run.decision ? <StatusBadge status={run.decision} /> : <span className="text-xs text-muted-foreground">—</span>,
    },
    {
      key: "created_at",
      header: "创建时间",
      mobileHidden: true,
      cell: (run) => (
        <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
          {run.created_at ?? "—"}
        </span>
      ),
    },
    {
      key: "summary",
      header: "摘要",
      mobileHidden: true,
      cell: (run) => (
        <span className="line-clamp-2 block max-w-[22rem] text-[11px] text-muted-foreground">
          {run.summary ?? "—"}
        </span>
      ),
    },
    {
      key: "action",
      header: "操作",
      align: "right",
      cell: (run) => (
        <Button variant="outline" size="xs" onClick={() => setSelectedId(run.evolution_run_id)}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Evolution"
        description="把已登记的 Bad Case 沉淀为可复用的经验，并给出采纳 / 拒绝 / 回滚的决定。"
        badge={<Activity className="size-4 text-muted-foreground" aria-hidden />}
        actions={
          <div className="flex items-center gap-1.5">
            <Button variant="outline" size="sm" onClick={reloadOverview}>
              <RefreshCw />
              刷新
            </Button>
            <Button size="sm" onClick={() => setDialogOpen(true)}>
              <Play />
              触发 Evolution
            </Button>
          </div>
        }
      />

      {pollingId ? (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-info/30 bg-info-subtle px-3.5 py-3">
          <InlineSpinner
            label={`Evolution ${pollingId} 运行中，每 ${POLL_INTERVAL_MS / 1000} 秒轮询一次`}
          />
          <Button variant="outline" size="xs" className="ml-auto" onClick={() => setPollingId(null)}>
            停止轮询
          </Button>
        </div>
      ) : null}

      {pollError ? (
        <PartialBanner
          title="轮询暂时失败"
          description="下一次轮询会继续尝试，不需要重新触发。"
          detail={pollError.detail}
        />
      ) : null}

      {pollNotice ? (
        <p className="rounded-lg border border-success/30 bg-success-subtle px-3.5 py-2.5 text-[11px] text-success-subtle-foreground">
          {pollNotice}
        </p>
      ) : null}

      <ResourceView resource={overview} loadingRows={4}>
        {(data) => (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              <StatCard
                label="待处理 Bad Case"
                value={formatNumber(data.pending_badcases)}
                tone={data.pending_badcases > 0 ? "warning" : "success"}
                icon={Activity}
                hint="还没有被 Evolution 消化的案例"
              />
              <StatCard
                label="Evolution 开关"
                value={data.enabled ? "已启用" : "未启用"}
                tone={data.enabled ? "success" : "muted"}
              />
              <StatCard label="历史运行" value={formatNumber(data.runs.length)} icon={Undo2} />
              <StatCard
                label="最近一次决定"
                value={
                  data.runs[0]?.decision ? statusLabel(data.runs[0].decision) : "暂无"
                }
                hint={data.runs[0]?.evolution_run_id ?? "还没有运行过"}
              />
            </div>

            {!data.enabled ? (
              <PartialBanner
                title="Evolution 当前未启用"
                description="后端会把触发请求直接拒绝（HTTP 409），不会产生运行记录。先设置 EVOLUTION_ENABLED=true 并重启服务，再来触发。"
              />
            ) : null}

            {data.note ? (
              <p className="text-[11px] leading-5 text-muted-foreground">后端说明：{data.note}</p>
            ) : null}

            <Card>
              <CardHeader>
                <CardTitle>运行历史</CardTitle>
                <CardDescription>每次 Evolution 消费的 Bad Case 数量、产出经验数与最终决定。</CardDescription>
              </CardHeader>
              <CardContent>
                {data.runs.length === 0 ? (
                  <SectionEmpty
                    title="还没有 Evolution 运行记录"
                    description="点右上角「触发 Evolution」让后端消费待处理的 Bad Case。"
                  />
                ) : (
                  <AdminTable columns={columns} rows={data.runs} getRowKey={(run) => run.evolution_run_id} />
                )}
              </CardContent>
            </Card>
          </div>
        )}
      </ResourceView>

      <LaunchDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onLaunched={(id) => {
          setPollNotice(null);
          setPollError(null);
          setPollingId(id);
          reloadOverview();
        }}
      />

      <SidePanel
        open={selectedId !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedId(null);
        }}
        title="Evolution 详情"
        description={selectedId ?? undefined}
      >
        {selectedId ? <EvolutionDetail evolutionRunId={selectedId} /> : null}
      </SidePanel>
    </div>
  );
}

function LaunchDialog({
  open,
  onOpenChange,
  onLaunched,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onLaunched: (evolutionRunId: string) => void;
}) {
  const [limit, setLimit] = useState("10");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<AdminApiError | null>(null);

  async function handleLaunch() {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      const parsed = Number.parseInt(limit, 10);
      const result = await launchAdminEvolution(
        Number.isFinite(parsed) && parsed > 0 ? parsed : undefined,
      );
      onOpenChange(false);
      onLaunched(result.evolution_run_id);
    } catch (cause) {
      setError(toAdminApiError(cause));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>触发 Evolution</DialogTitle>
          <DialogDescription>
            后端会立刻返回 202 与 evolution_run_id，管理台随后每 {POLL_INTERVAL_MS / 1000} 秒轮询一次运行状态。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <label className="text-[11px] text-muted-foreground" htmlFor="evolution-limit">
            本次最多消费多少条 Bad Case
          </label>
          <Input
            id="evolution-limit"
            inputMode="numeric"
            value={limit}
            onChange={(event) => setLimit(event.target.value)}
            className="max-w-32"
          />
          <p className="text-[11px] leading-4 text-muted-foreground">
            留空或填 0 时只提交空 body，由后端决定默认批量大小。
          </p>
        </div>

        {error ? <ActionError message="触发失败" detail={error.detail} /> : null}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button size="sm" onClick={() => void handleLaunch()} disabled={pending}>
            <Play />
            {pending ? "正在提交…" : "开始"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EvolutionDetail({ evolutionRunId }: { evolutionRunId: string }) {
  const resource = useAdminResource<AdminEvolutionRunDetail>(
    `admin-evolution-run:${evolutionRunId}`,
    () => getAdminEvolutionRun(evolutionRunId),
  );

  return (
    <ResourceView resource={resource} loadingRows={5}>
      {(data) => (
        <div className="flex flex-col gap-5">
          <PanelSection title="运行摘要">
            <DescriptionList
              items={[
                { label: "编号", value: evolutionRunId },
                { label: "状态", value: <StatusBadge status={data.run.status} /> },
                {
                  label: "决定",
                  value: data.run.decision ? (
                    <StatusBadge status={data.run.decision} />
                  ) : (
                    "后端未给出决定"
                  ),
                },
                { label: "消费 Bad Case", value: formatNumber(data.run.badcase_count) },
                { label: "产出经验", value: formatNumber(data.run.experience_count) },
                { label: "开始", value: data.run.created_at ?? "—" },
                { label: "结束", value: data.run.finished_at ?? "—" },
                { label: "摘要", value: data.run.summary ?? "—" },
              ]}
            />
          </PanelSection>

          <Separator />

          <PanelSection
            title="执行步骤"
            description="后端记录的处理步骤，按顺序展示。"
          >
            {data.run.steps.length === 0 ? (
              <p className="text-xs text-muted-foreground">这次运行没有记录步骤。</p>
            ) : (
              <ol className="flex flex-col gap-2">
                {data.run.steps.map((step, index) => (
                  <li key={`${step.step}-${index}`} className="rounded-lg border border-border p-2.5">
                    <p className="text-xs font-medium text-foreground">{step.step}</p>
                    <p className="mt-1 text-xs leading-5 break-words whitespace-pre-wrap text-muted-foreground">
                      {step.detail}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </PanelSection>

          <Separator />

          <PanelSection
            title="前后指标"
            description="Evolution 前后的对比。两个分组都直接来自后端，不做二次计算。"
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-border p-2.5">
                <p className="mb-2 text-xs font-medium text-muted-foreground">运行前</p>
                <MetricList metrics={data.run.before_metrics} emptyText="没有返回运行前指标。" />
              </div>
              <div className="rounded-lg border border-border p-2.5">
                <p className="mb-2 text-xs font-medium text-muted-foreground">运行后</p>
                <MetricList metrics={data.run.after_metrics} emptyText="没有返回运行后指标。" />
              </div>
            </div>
          </PanelSection>

          <Separator />

          <PanelSection
            title="经验"
            description="每条经验都必须能追溯到它来自哪些 Bad Case。"
          >
            {data.experiences.length === 0 ? (
              <SectionEmpty
                title="这次运行没有产出经验"
                description="可能没有可复用的失败模式，或者运行被拒绝。"
              />
            ) : (
              <ul className="flex flex-col gap-3">
                {data.experiences.map((experience) => (
                  <ExperienceCard key={experience.experience_id} experience={experience} />
                ))}
              </ul>
            )}
          </PanelSection>

          <Separator />

          <PanelSection title="关联 Bad Case" description="本次消费的原始案例。">
            {data.badcases.length === 0 ? (
              <p className="text-xs text-muted-foreground">没有关联案例。</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {data.badcases.map((badcase) => (
                  <li key={badcase.badcase_id} className="rounded-lg border border-border p-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-medium text-foreground">{badcase.category}</span>
                      <StatusBadge status={badcase.severity} />
                      <StatusBadge status={badcase.fixed_status} />
                      <Link
                        href={`/admin/runs/${encodeURIComponent(badcase.run_id)}`}
                        className="ml-auto font-mono text-[11px] text-primary underline-offset-4 hover:underline"
                      >
                        {badcase.run_id}
                      </Link>
                    </div>
                    <p className="mt-1.5 text-xs leading-5 break-words text-muted-foreground">
                      {badcase.symptom}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </PanelSection>
        </div>
      )}
    </ResourceView>
  );
}

function ExperienceCard({ experience }: { experience: AdminExperience }) {
  return (
    <li className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={experience.decision} />
        <span className="text-xs font-medium text-foreground">{experience.affected_module}</span>
        <span className="font-mono text-[10px] text-muted-foreground">{experience.experience_id}</span>
      </div>
      <DescriptionList
        className="mt-2"
        items={[
          { label: "失败模式", value: experience.failure_pattern },
          { label: "已验证根因", value: experience.verified_root_cause },
          { label: "建议改动", value: experience.recommended_change },
          {
            label: "来源案例",
            value:
              experience.source_badcases.length > 0 ? (
                <span className="font-mono text-[11px]">{experience.source_badcases.join("、")}</span>
              ) : (
                "—"
              ),
          },
        ]}
      />
      <div className="mt-2 grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border p-2.5">
          <p className="mb-2 text-xs font-medium text-muted-foreground">改动前</p>
          <MetricList metrics={experience.before_metrics} emptyText="没有指标。" />
        </div>
        <div className="rounded-lg border border-border p-2.5">
          <p className="mb-2 text-xs font-medium text-muted-foreground">改动后</p>
          <MetricList metrics={experience.after_metrics} emptyText="没有指标。" />
        </div>
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        创建时间：{experience.created_at ?? "—"}
      </p>
      <div className="mt-2">
        <ToneBadge tone="muted">{experience.evolution_run_id}</ToneBadge>
      </div>
    </li>
  );
}
