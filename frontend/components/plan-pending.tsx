"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { RunProgress, RunStatus } from "@/types/api";
import { describeApiError, planningStepsFromProgress, runDegradations, waitForRun } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PlanningProgress } from "@/components/planning-progress";
import { ErrorState, PartialNotice, RunStatusBadge } from "@/components/state-views";

interface PlanPendingProps {
  runId: string;
  /**
   * 服务端已经拿到的那份进度快照。传进来是为了**首屏就能显示真实进度**：
   * 不传的话，页面先渲染「正在等待 Agent 的第一步」，要等客户端第一次轮询回来才更新，
   * 而服务端其实早就知道 Agent 现在在查酒店。
   */
  initialProgress?: RunProgress | null;
}

/**
 * 「仍在生成中」视图（FRONTEND_DESIGN §6）。
 *
 * run 还是 RUNNING 时它不是错误：这里复用 lib/api.ts 的 waitForRun 轮询 /status，
 * 到终态后再 router.refresh()，由服务端组件读取并渲染真实行程。
 * 不能在 RUNNING 阶段渲染 ErrorState，否则每次长耗时行程都会先给用户一个「失败」。
 *
 * 展示的是 **Agent 实际在做什么**：标题是 progress.current_stage（「在查酒店」），
 * 下面是 stages 里已经真实发生过的步骤（含工具耗时）。前端不预置步骤清单：
 * Agent 换顺序、后端加工具，这里都自动跟着变。
 *
 * 重新查询用 key 重新挂载轮询组件来实现：这样进度与错误状态天然重置，
 * 不用在 effect 里同步 setState（那会触发一次多余的渲染）。
 */
export function PlanPending({ runId, initialProgress = null }: PlanPendingProps) {
  const [attempt, setAttempt] = useState(0);
  return (
    <PlanPoll
      key={attempt}
      runId={runId}
      initialProgress={initialProgress}
      onRetry={() => setAttempt((value) => value + 1)}
    />
  );
}

function PlanPoll({
  runId,
  initialProgress,
  onRetry,
}: {
  runId: string;
  initialProgress: RunProgress | null;
  onRetry: () => void;
}) {
  const router = useRouter();
  const [progress, setProgress] = useState<RunProgress | null>(initialProgress);
  /** 轮询终止（接口错误，或后端给出 FAILED / CANCELLED 终态）时记录原因。 */
  const [stopped, setStopped] = useState<{ status: RunStatus | null; message: string } | null>(null);

  useEffect(() => {
    let active = true;
    waitForRun(runId, {
      shouldContinue: () => active,
      onProgress: (next) => {
        if (active) setProgress(next);
      },
    })
      .then((result) => {
        if (!active) return;
        if (result.status === "FAILED" || result.status === "CANCELLED") {
          setStopped({
            status: result.status,
            message: result.message ?? "这次规划没有完成，可以重新发起一次规划。",
          });
          return;
        }
        // SUCCESS / DEGRADED：行程已经写好，交回服务端组件渲染（含降级提示）。
        router.refresh();
      })
      .catch((cause) => {
        if (!active) return;
        setStopped({ status: null, message: describeApiError(cause) });
      });
    return () => {
      active = false;
    };
  }, [runId, router]);

  const steps = planningStepsFromProgress(progress);
  const degradations = runDegradations(progress);

  if (stopped) {
    const cancelled = stopped.status === "CANCELLED";
    // 后端把失败原因放在 error 上（message 只是「规划已结束」这类占位文案），
    // 两者都在时优先展示 error，避免用户看到一句没有信息量的结束语。
    const backendReason = progress?.error ?? progress?.message ?? null;
    const detail = backendReason && backendReason !== stopped.message ? `服务端说明：${backendReason}` : undefined;
    return (
      <div className="mx-auto max-w-3xl space-y-4">
        <ErrorState
          title={cancelled ? "这次规划已被取消" : "这次规划没有完成"}
          description={stopped.message}
          detail={detail}
          onRetry={onRetry}
          retryLabel="继续查询"
        />
        {progress && steps.length ? (
          <PlanningProgress
            steps={steps}
            currentStage={progress.current_stage}
            status={cancelled ? "CANCELLED" : "FAILED"}
          />
        ) : null}
        {degradations.length ? (
          <PartialNotice
            title="这些步骤本次没有正常返回"
            description={degradations.join("；")}
            detail="它们可能是本次中断的原因；重新规划时会再次尝试。"
          />
        ) : null}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">规划进度（Agent 实际执行步骤）</p>
        <RunStatusBadge status={progress?.status ?? "RUNNING"} />
      </div>
      <PlanningProgress
        steps={steps}
        currentStage={progress?.current_stage}
        status={progress?.status ?? "RUNNING"}
      />
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={onRetry}>
          重新查询状态
        </Button>
      </div>
    </div>
  );
}
