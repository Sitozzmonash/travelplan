import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import type { TripPlan } from "@/types/plan";
import { API_MODE, describeApiError, getPlan } from "@/lib/api";
import { ErrorState } from "@/components/state-views";
import { PlanWorkspace } from "@/components/plan-workspace";

export const dynamic = "force-dynamic";

export default async function PlanPage({ params }: PageProps<"/plan/[id]">) {
  const { id } = await params;
  let plan: TripPlan | null = null;
  let error: string | null = null;

  try {
    plan = await getPlan(id);
  } catch (cause) {
    error = describeApiError(cause);
  }

  return (
    <div className="mx-auto w-full max-w-[1440px] px-4 py-6 pb-24 sm:px-6 lg:pb-8">
      <div className="mb-4 flex items-center justify-between gap-3">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          新建行程
        </Link>
        <span className="text-[11px] text-muted-foreground">
          {API_MODE === "mock" ? "演示数据模式" : "已连接规划服务"} · run_id {id}
        </span>
      </div>

      {plan ? (
        <PlanWorkspace plan={plan} />
      ) : (
        <div className="mx-auto max-w-2xl space-y-4">
          <ErrorState
            title="无法打开这个行程"
            description={error ?? "这个行程编号没有对应的规划结果。"}
            detail={`run_id：${id}`}
          />
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 text-xs text-primary transition-colors hover:underline"
          >
            <ArrowLeft className="size-3.5" aria-hidden />
            返回首页重新生成
          </Link>
        </div>
      )}
    </div>
  );
}
