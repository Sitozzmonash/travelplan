"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { FlaskConical, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import {
  ActionError,
  InlineSpinner,
  PartialBanner,
  ResourceView,
} from "@/components/admin/admin-states";
import { BooleanBadge, StatusBadge } from "@/components/admin/status-badge";
import { formatDurationMs, formatNumber } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import {
  getAdminBenchmarkRun,
  getAdminBenchmarkRuns,
  launchAdminBenchmark,
  toAdminApiError,
  type AdminApiError,
} from "@/lib/admin-api";
import { BENCHMARK_SUITES, type AdminBenchmarkRun, type AdminBenchmarkRunList } from "@/types/admin";

const SUITE_LABELS: Record<string, string> = {
  basic: "基础用例",
  hard: "困难用例",
  badcase_regression: "Bad Case 回归",
  provider_failure: "Provider 故障",
  jev_decision: "Jev 决策",
  live_smoke: "线上冒烟",
};

const POLL_INTERVAL_MS = 2500;

export function BenchmarkView() {
  const router = useRouter();
  const list = useAdminResource<AdminBenchmarkRunList>("admin-benchmark-runs", () =>
    getAdminBenchmarkRuns(20),
  );
  const [dialogOpen, setDialogOpen] = useState(false);
  const [pollingId, setPollingId] = useState<string | null>(null);
  const [pollError, setPollError] = useState<AdminApiError | null>(null);
  const reloadList = list.reload;

  // 轮询到 RUNNING 变成其它状态为止，然后跳到详情页 —— 结果本身比列表更有信息量。
  useEffect(() => {
    if (!pollingId) return;
    let cancelled = false;
    const timer = setInterval(() => {
      getAdminBenchmarkRun(pollingId).then(
        (detail) => {
          if (cancelled) return;
          setPollError(null);
          if (detail.run.status !== "RUNNING") {
            setPollingId(null);
            reloadList();
            router.push(`/admin/benchmark/${encodeURIComponent(pollingId)}`);
          }
        },
        (cause: unknown) => {
          // 轮询失败不终止：下一次 tick 继续尝试，避免一次网络抖动就丢掉结果。
          if (!cancelled) setPollError(toAdminApiError(cause));
        },
      );
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pollingId, reloadList, router]);

  const columns: AdminColumn<AdminBenchmarkRun>[] = [
    {
      key: "id",
      header: "Benchmark",
      primary: true,
      cell: (run) => (
        <Link
          href={`/admin/benchmark/${encodeURIComponent(run.benchmark_run_id)}`}
          className="font-mono text-xs text-primary underline-offset-4 hover:underline"
        >
          {run.benchmark_run_id}
        </Link>
      ),
    },
    { key: "status", header: "状态", cell: (run) => <StatusBadge status={run.status} /> },
    {
      key: "suite",
      header: "Suite",
      cell: (run) => (
        <span className="text-xs text-muted-foreground">
          {run.suite ? SUITE_LABELS[run.suite] ?? run.suite : "全部"}
        </span>
      ),
    },
    {
      key: "jev",
      header: "Jev",
      cell: (run) => (
        <BooleanBadge value={run.jev_enabled} onLabel="ON" offLabel="OFF" onTone="info" />
      ),
    },
    {
      key: "cases",
      header: "通过 / 总数",
      align: "right",
      cell: (run) => (
        <span className="tabular text-xs whitespace-nowrap">
          {formatNumber(run.passed)} / {formatNumber(run.case_count)}
          {run.failed !== null && run.failed > 0 ? (
            <span className="ml-1 text-danger-subtle-foreground">（失败 {run.failed}）</span>
          ) : null}
        </span>
      ),
    },
    {
      key: "model",
      header: "模型",
      mobileHidden: true,
      cell: (run) => (
        <span className="font-mono text-[11px] break-all text-muted-foreground">
          {run.model ?? "—"}
        </span>
      ),
    },
    {
      key: "version",
      header: "版本",
      mobileHidden: true,
      cell: (run) => (
        <span className="text-[11px] text-muted-foreground">
          {run.version}
          {run.travelplan_commit ? ` · ${run.travelplan_commit.slice(0, 8)}` : ""}
        </span>
      ),
    },
    {
      key: "started_at",
      header: "开始时间",
      mobileHidden: true,
      cell: (run) => (
        <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
          {run.started_at ?? "—"}
        </span>
      ),
    },
    {
      key: "duration",
      header: "耗时",
      align: "right",
      mobileHidden: true,
      cell: (run) => (
        <span className="tabular text-xs">
          {formatDurationMs(durationBetween(run.started_at, run.finished_at))}
        </span>
      ),
    },
    {
      key: "notes",
      header: "备注",
      mobileHidden: true,
      cell: (run) => (
        <span className="line-clamp-2 block max-w-[18rem] text-[11px] text-muted-foreground">
          {run.notes ?? "—"}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Benchmark"
        description="在固定用例集上跑回归：六个维度分别看硬性约束、行程质量、证据、Provider、性能与 Jev 决策。"
        badge={<FlaskConical className="size-4 text-muted-foreground" aria-hidden />}
        actions={
          <div className="flex items-center gap-1.5">
            <Button variant="outline" size="sm" onClick={list.reload}>
              <RefreshCw />
              刷新
            </Button>
            <Button size="sm" onClick={() => setDialogOpen(true)}>
              <Play />
              运行 Benchmark
            </Button>
          </div>
        }
      />

      {pollingId ? (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-info/30 bg-info-subtle px-3.5 py-3">
          <InlineSpinner label={`正在运行 ${pollingId}，每 ${POLL_INTERVAL_MS / 1000} 秒轮询一次`} />
          <span className="text-[11px] text-info-subtle-foreground">
            完成后会自动跳转到详情页；也可以继续留在这一页做别的事。
          </span>
          <Button variant="outline" size="xs" className="ml-auto" onClick={() => setPollingId(null)}>
            停止轮询
          </Button>
        </div>
      ) : null}

      {pollError ? (
        <PartialBanner
          title="轮询暂时失败"
          description="上一次读取运行状态失败，下一次轮询会继续尝试。"
          detail={pollError.detail}
        />
      ) : null}

      <ResourceView
        resource={list}
        loadingRows={5}
        isEmpty={(data) => data.items.length === 0}
        emptyTitle="还没有 Benchmark 记录"
        emptyDescription="点右上角「运行 Benchmark」，选择 suite 后即可得到第一条基线。"
      >
        {(data) => <AdminTable columns={columns} rows={data.items} getRowKey={(run) => run.benchmark_run_id} />}
      </ResourceView>

      <LaunchDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onLaunched={(id) => {
          setPollError(null);
          setPollingId(id);
          list.reload();
        }}
      />
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
  onLaunched: (benchmarkRunId: string) => void;
}) {
  const [suites, setSuites] = useState<string[]>([]);
  const [jevEnabled, setJevEnabled] = useState(true);
  const [live, setLive] = useState(false);
  const [limit, setLimit] = useState("20");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<AdminApiError | null>(null);

  async function handleLaunch() {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      const parsedLimit = Number.parseInt(limit, 10);
      const result = await launchAdminBenchmark({
        suites: suites.length > 0 ? suites : undefined,
        jev_enabled: jevEnabled,
        live,
        limit: Number.isFinite(parsedLimit) && parsedLimit > 0 ? parsedLimit : undefined,
      });
      onOpenChange(false);
      onLaunched(result.benchmark_run_id);
    } catch (cause) {
      setError(toAdminApiError(cause));
    } finally {
      setPending(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>运行 Benchmark</DialogTitle>
          <DialogDescription>
            提交后后端立即返回 RUNNING，管理台每 {POLL_INTERVAL_MS / 1000} 秒轮询一次运行状态。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-foreground">Suite</span>
            <div className="grid gap-2 sm:grid-cols-2">
              {BENCHMARK_SUITES.map((suite) => (
                <label key={suite} className="flex items-center gap-2 text-xs text-foreground">
                  <input
                    type="checkbox"
                    className="size-4 accent-primary"
                    checked={suites.includes(suite)}
                    onChange={(event) => {
                      setSuites((previous) =>
                        event.target.checked
                          ? [...previous, suite]
                          : previous.filter((item) => item !== suite),
                      );
                    }}
                  />
                  <span>{SUITE_LABELS[suite] ?? suite}</span>
                  <span className="font-mono text-[10px] text-muted-foreground">{suite}</span>
                </label>
              ))}
            </div>
            <p className="text-[11px] leading-4 text-muted-foreground">
              一个都不勾选时，后端会用它自己的默认 suite 集合。
            </p>
          </div>

          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-xs text-foreground">
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={jevEnabled}
                onChange={(event) => setJevEnabled(event.target.checked)}
              />
              <span>启用 Jev（可与历史 OFF 结果对比）</span>
            </label>
            <label className="flex items-center gap-2 text-xs text-foreground">
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={live}
                onChange={(event) => setLive(event.target.checked)}
              />
              <span>使用真实网络（live）：会实际请求外部数据源，耗时更长</span>
            </label>
          </div>

          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-muted-foreground" htmlFor="benchmark-limit">
              每个 suite 的用例上限
            </label>
            <Input
              id="benchmark-limit"
              inputMode="numeric"
              value={limit}
              onChange={(event) => setLimit(event.target.value)}
              className="max-w-32"
            />
          </div>

          {error ? <ActionError message="启动失败" detail={error.detail} /> : null}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button size="sm" onClick={() => void handleLaunch()} disabled={pending}>
            <Play />
            {pending ? "正在提交…" : "开始运行"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function durationBetween(startedAt: string | null, finishedAt: string | null): number | null {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}
