"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { PlanningStepState } from "@/types/api";
import { createPlanningSteps, describeApiError, mergeProgressEvent, waitForRun } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PlanningProgress } from "@/components/planning-progress";
import { ErrorState, LoadingState, RunStatusBadge } from "@/components/state-views";

interface PlanPendingProps {
  runId: string;
}

/**
 * 「仍在生成中」视图（FRONTEND_DESIGN §6）。
 *
 * run 还是 RUNNING 时它不是错误：这里复用 lib/api.ts 的 waitForRun 轮询 /status，
 * 到终态后再 router.refresh()，由服务端组件读取并渲染真实行程。
 * 不能在 RUNNING 阶段渲染 ErrorState，否则每次长耗时行程都会先给用户一个「失败」。
 *
 * 重新查询用 key 重新挂载轮询组件来实现：这样进度与错误状态天然重置，
 * 不用在 effect 里同步 setState（那会触发一次多余的渲染）。
 */
export function PlanPending({ runId }: PlanPendingProps) {
  const [attempt, setAttempt] = useState(0);
  return (
    <PlanPoll
      key={attempt}
      runId={runId}
      onRetry={() => setAttempt((value) => value + 1)}
    />
  );
}

function PlanPoll({ runId, onRetry }: { runId: string; onRetry: () => void }) {
  const router = useRouter();
  const [steps, setSteps] = useState<PlanningStepState[]>(() => createPlanningSteps());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    waitForRun(runId, {
      shouldContinue: () => active,
      onProgress: (event) => {
        setSteps((current) => mergeProgressEvent(current, event));
      },
    })
      .then((result) => {
        if (!active) return;
        if (result.status === "FAILED" || result.status === "CANCELLED") {
          setError(result.message ?? "这次规划没有完成，可以重新发起一次规划。");
          return;
        }
        router.refresh();
      })
      .catch((cause) => {
        if (!active) return;
        setError(describeApiError(cause));
      });
    return () => {
      active = false;
    };
  }, [runId, router]);

  if (error) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <ErrorState
          title="这次规划没有完成"
          description={error}
          detail={`行程编号：${runId}`}
          onRetry={onRetry}
          retryLabel="继续查询"
        />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <LoadingState
        title="行程仍在生成中"
        description="这次规划还没有结束，页面会每几秒查询一次状态，完成后自动展示行程。"
        detail="长行程需要依次查询交通、酒店、攻略、地点与路线，请保持页面打开。"
      />
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">规划进度（来自真实规划流程的节点）</p>
        <RunStatusBadge status="RUNNING" />
      </div>
      <PlanningProgress steps={steps} />
      <div className="flex justify-end">
        <Button variant="ghost" size="sm" onClick={onRetry}>
          重新查询状态
        </Button>
      </div>
    </div>
  );
}
