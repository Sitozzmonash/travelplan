import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, CircleSlash } from "lucide-react";
import type { TripPlan } from "@/types/plan";
import type { RunProgress } from "@/types/api";
import {
  API_MODE,
  USE_MOCK_API,
  apiErrorKind,
  describeApiError,
  getPlan,
  getPlanStatus,
} from "@/lib/api";
import { ErrorState, PartialNotice, RunStatusBadge, UnauthorizedState } from "@/components/state-views";
import { PlanPending } from "@/components/plan-pending";
import { PlanWorkspace } from "@/components/plan-workspace";

export const dynamic = "force-dynamic";

/**
 * 这里**故意没有 `loading.tsx`**。
 *
 * 曾经有一个骨架屏，但它让这个页面永远停在"正在读取行程数据"：`loading.tsx` 会为
 * `page.tsx` 建立 Suspense 边界，服务端虽然把完整内容（含 plan_md）写进了 RSC 流，
 * 客户端却始终没有把 fallback 替换掉 —— 打开任何已完成的行程都是一片骨架。
 * 实测（同一份 run，其余不变）：有 loading.tsx 时可见文本只有骨架；删掉后同一 URL
 * 立刻渲染出完整行程与降级提示。
 *
 * 这个页面的等待时间本来就不长（取一次状态 + 一次 plan），而且"运行中"已经有
 * `PlanPending` 这个真正的页面内状态，所以骨架屏是多余的 —— 不要再加回来。
 */
export default async function PlanPage({ params }: PageProps<"/plan/[id]">) {
  const { id } = await params;

  let plan: TripPlan | null = null;
  let progress: RunProgress | null = null;
  let error: string | null = null;
  let errorKind: ReturnType<typeof apiErrorKind> = null;

  try {
    // 先看 run 状态：RUNNING 时没有 plan.json，直接取 plan 会把「还在跑」显示成「失败」。
    // 演示模式没有状态接口，getPlanStatus 会抛 invalid，下面的 catch 会退回直接取 plan。
    if (!USE_MOCK_API) {
      progress = await getPlanStatus(id);
    }
    if (!progress || progress.status === "SUCCESS" || progress.status === "DEGRADED") {
      plan = await getPlan(id);
    }
  } catch (cause) {
    if (apiErrorKind(cause) === "not_found") notFound();
    error = describeApiError(cause);
    errorKind = apiErrorKind(cause);
  }

  return (
    <div className="mx-auto w-full max-w-[1440px] px-4 py-6 pb-24 sm:px-6 lg:pb-8">
      <div className="mb-4 flex items-center justify-between gap-3">
        <Link
          href="/"
          className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          新建行程
        </Link>
        <span className="flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
          {progress ? <RunStatusBadge status={progress.status} className="shrink-0" /> : null}
          <span className="truncate">
            {API_MODE === "mock" ? "演示数据模式" : "真实查询结果"} · 行程编号 {id}
          </span>
        </span>
      </div>

      {progress?.status === "RUNNING" ? <PlanPending runId={id} /> : null}

      {progress?.status === "FAILED" ? (
        <div className="mx-auto max-w-2xl space-y-4">
          <ErrorState
            title="这次没有生成行程"
            description="规划在生成过程中中断，本次没有产出可用行程。可以重新发起一次规划。"
            detail={progress.message ?? undefined}
          />
          <BackHomeLink />
        </div>
      ) : null}

      {progress?.status === "CANCELLED" ? (
        <div className="mx-auto max-w-2xl space-y-4">
          <div className="rounded-lg border border-border bg-muted/25 px-4 py-5" data-testid="state-cancelled">
            <div className="flex items-center gap-2 text-sm font-medium text-foreground">
              <CircleSlash className="size-4 shrink-0 text-muted-foreground" aria-hidden />
              这次规划已被取消
            </div>
            <p className="mt-2 max-w-prose text-xs leading-5 text-muted-foreground">
              {progress.message ?? "任务在完成前被终止，通常是服务重启或手动停止。本次没有产出可用行程。"}
            </p>
            <p className="mt-1.5 text-[11px] leading-5 text-muted-foreground/80">行程编号：{id}</p>
          </div>
          <BackHomeLink />
        </div>
      ) : null}

      {plan ? (
        <div className="space-y-5">
          {progress?.status === "DEGRADED" ? (
            <PartialNotice
              title="行程已生成，部分信息暂未验证"
              description={progress.message ?? "部分数据源本次只返回了缓存或部分数据，行程仍然可用。"}
              detail="降级会影响个别价格或通勤时间的准确性，出行前请再次确认。"
            />
          ) : null}
          <PlanWorkspace plan={plan} />
        </div>
      ) : null}

      {!plan && (error !== null || progress === null) ? (
        <div className="mx-auto max-w-2xl space-y-4">
          {errorKind === "unauthorized" ? (
            <UnauthorizedState
              title="没有权限打开这个行程"
              description={error ?? "规划服务拒绝了这次请求。"}
              detail={`行程编号：${id}`}
            />
          ) : (
            <ErrorState
              title="无法打开这个行程"
              description={error ?? "这个行程编号没有对应的规划结果。"}
              detail={`行程编号：${id}`}
            />
          )}
          <BackHomeLink />
        </div>
      ) : null}
    </div>
  );
}

function BackHomeLink() {
  return (
    <Link
      href="/"
      className="inline-flex items-center gap-1.5 text-xs text-primary transition-colors hover:underline"
    >
      <ArrowLeft className="size-3.5" aria-hidden />
      返回首页重新生成
    </Link>
  );
}
