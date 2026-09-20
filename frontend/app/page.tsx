"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { ArrowRight, CircleAlert, Database, Lock, ShieldCheck, Sparkles } from "lucide-react";
import type { CreatePlanResult, PlanningStepState } from "@/types/api";
import {
  API_MODE,
  type ApiErrorKind,
  apiErrorKind,
  createPlan,
  createPlanningSteps,
  describeApiError,
  mergeProgressEvent,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PlanningProgress } from "@/components/planning-progress";
import { SectionCard } from "@/components/section-card";
import { ErrorState, PartialNotice, RunStatusBadge, UnauthorizedState } from "@/components/state-views";
import { TripSearch } from "@/components/trip-search";

type Phase = "idle" | "planning" | "result" | "error";

export default function HomePage() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("idle");
  const [steps, setSteps] = useState<PlanningStepState[]>(() => createPlanningSteps());
  const [query, setQuery] = useState("");
  /**
   * 已经跑完但没有直接进入工作台的 run：DEGRADED 需要先把降级原因说清楚，
   * FAILED / CANCELLED 需要按各自的状态渲染，不能一律说成「服务出错」。
   */
  const [result, setResult] = useState<CreatePlanResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorKind, setErrorKind] = useState<ApiErrorKind | null>(null);

  const runPlanning = useCallback(
    async (message: string) => {
      setQuery(message);
      setSteps(createPlanningSteps());
      setResult(null);
      setError(null);
      setErrorKind(null);
      setPhase("planning");
      try {
        const created = await createPlan(message, {
          onProgress: (event) => {
            setSteps((current) => mergeProgressEvent(current, event));
          },
        });
        if (created.status === "SUCCESS" && created.plan) {
          router.push(`/plan/${encodeURIComponent(created.run_id)}`);
          return;
        }
        setResult(created);
        setPhase("result");
      } catch (cause) {
        setError(describeApiError(cause));
        setErrorKind(apiErrorKind(cause));
        setPhase("error");
      }
    },
    [router],
  );

  function openPlan(runId: string) {
    router.push(`/plan/${encodeURIComponent(runId)}`);
  }

  return (
    <div className="relative">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-[420px] bg-[radial-gradient(120%_100%_at_50%_0%,var(--color-accent)_0%,transparent_70%)]"
        aria-hidden
      />

      <section className="relative mx-auto w-full max-w-3xl px-4 pt-14 pb-10 sm:px-6 sm:pt-20">
        <div className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-[11px] text-muted-foreground">
            <Sparkles className="size-3" aria-hidden />
            先查证，再规划
          </span>
          <h1 className="mt-5 text-[28px] leading-[1.25] font-semibold tracking-tight text-foreground sm:text-[38px] sm:leading-[1.2]">
            说出你想怎么旅行
            <br />
            剩下的交给 TravelPlan
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-sm leading-6 text-muted-foreground sm:text-[15px] sm:leading-7">
            比较机票、高铁、酒店与门票，结合小红书/抖音攻略和真实路线，
            自动生成预算合理、时间可行的国内旅行计划。
          </p>
        </div>

        <div className="mt-8">
          {phase === "idle" ? (
            <div className="space-y-3">
              <TripSearch onSubmit={runPlanning} />
              <div className="flex justify-center">
                <Link
                  href="/guided"
                  className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3.5 py-2 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                >
                  不想写一句话？逐步选择偏好
                  <ArrowRight className="size-3.5" aria-hidden />
                </Link>
              </div>
            </div>
          ) : null}

          {phase === "planning" ? (
            <SectionCard
              title="正在规划"
              icon={Sparkles}
              description={query ? `需求：${query}` : undefined}
              action={<RunStatusBadge status="RUNNING" />}
              className="shadow-sm"
            >
              <PlanningProgress steps={steps} />
              <div className="mt-4 flex items-center justify-between gap-3 border-t border-border/70 pt-3">
                <p className="text-[11px] leading-5 text-muted-foreground">
                  规划过程中会依次查询交通、酒店、攻略与路线数据源，全部完成后自动进入行程工作台。
                </p>
                <Button variant="outline" size="sm" onClick={() => setPhase("idle")}>
                  返回修改
                </Button>
              </div>
            </SectionCard>
          ) : null}

          {phase === "result" && result ? (
            <SectionCard
              title={result.status === "DEGRADED" ? "行程已生成，部分信息暂未验证" : "这次没有生成行程"}
              icon={Sparkles}
              description={query ? `需求：${query}` : undefined}
              action={<RunStatusBadge status={result.status} />}
              className="shadow-sm"
            >
              {result.status === "DEGRADED" ? (
                <>
                  <PartialNotice
                    title="行程已生成，部分信息暂未验证"
                    description={result.message ?? "部分数据源本次只返回了缓存或部分数据，行程仍然可用。"}
                    detail="降级会影响个别价格或通勤时间的准确性，出行前请再次确认。"
                  />
                  {result.degradations?.length ? (
                    <ul className="mt-3 grid gap-1.5">
                      {result.degradations.map((item) => (
                        <li key={item} className="flex gap-2 text-xs leading-5 text-muted-foreground">
                          <span className="text-border">·</span>
                          <span className="min-w-0">{item}</span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </>
              ) : (
                <ErrorState
                  title={result.status === "CANCELLED" ? "这次规划被取消" : "这次没有生成行程"}
                  description={
                    result.status === "CANCELLED"
                      ? "任务在完成前被终止，通常是服务重启或手动停止。本次没有产出可用行程，可以重新规划一次。"
                      : "规划在生成过程中中断，本次没有产出可用行程。可以重新规划一次。"
                  }
                  detail={result.message ?? undefined}
                  onRetry={() => runPlanning(query)}
                  retryLabel="重新规划"
                />
              )}

              <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border/70 pt-3">
                <PlanningProgress steps={steps} className="w-full border-0 bg-transparent shadow-none" />
                <div className="flex flex-wrap items-center gap-2">
                  {result.plan ? (
                    <Button size="sm" onClick={() => openPlan(result.run_id)}>
                      打开行程
                    </Button>
                  ) : null}
                  <Button variant="ghost" size="sm" onClick={() => setPhase("idle")}>
                    返回修改需求
                  </Button>
                </div>
              </div>
            </SectionCard>
          ) : null}

          {phase === "error" ? (
            <div className="space-y-3">
              {errorKind === "unauthorized" ? (
                <UnauthorizedState
                  title="没有权限访问规划服务"
                  description={error ?? "规划服务拒绝了这次请求。"}
                  detail="请确认当前使用的是有权限的访问地址或凭据，然后重试。"
                />
              ) : (
                <ErrorState
                  title="这次没有生成行程"
                  description={error ?? "规划服务没有返回结果，本次没有产出可用行程。"}
                  onRetry={() => runPlanning(query)}
                  retryLabel="重新规划"
                />
              )}
              <Button variant="ghost" size="sm" onClick={() => setPhase("idle")}>
                返回修改需求
              </Button>
            </div>
          ) : null}
        </div>

        <p className="mt-4 flex items-center justify-center gap-1.5 text-[11px] text-muted-foreground">
          {API_MODE === "mock" ? (
            <>
              <CircleAlert className="size-3" aria-hidden />
              当前为演示数据模式，行程来自内置示例；接入真实查询服务后即使用实时结果。
            </>
          ) : (
            <>
              <Database className="size-3" aria-hidden />
              已连接真实查询服务，价格与余票为查询时点结果。
            </>
          )}
        </p>
      </section>

      <section id="about" className="mx-auto w-full max-w-5xl px-4 pb-20 sm:px-6">
        <div className="grid gap-4 sm:grid-cols-3">
          <AboutCard
            icon={ShieldCheck}
            title="每条安排都能追溯"
            description="住宿、餐饮与景点都标注来源数量、可信度与广告风险；被判定为推广内容的候选不会进入行程。"
          />
          <AboutCard
            icon={Database}
            title="价格区分实时与估算"
            description="机票、高铁、酒店与门票标注查询时间，并提示价格可能变化；估算费用单独列出，不与实时价格混算。"
          />
          <AboutCard
            icon={Lock}
            title="密钥只留在服务端"
            description="地图、机票、酒店与攻略平台的访问密钥全部由后端持有，页面只读取结构化结果，不接触任何密钥。"
          />
        </div>
        <p className="mt-6 max-w-3xl text-xs leading-6 text-muted-foreground">
          TravelPlan 不做「一句话生成奇幻旅程」。它会先把交通与住宿的真实价格查出来，再校验攻略里提到的地点是否存在，
          最后检查每天的安排能不能按时走完；遇到走不通的地方，会直接告诉你原计划哪里不合理、改成了什么。
        </p>
      </section>
    </div>
  );
}

function AboutCard({
  icon: Icon,
  title,
  description,
}: {
  icon: typeof ShieldCheck;
  title: string;
  description: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-4">
      <span className="inline-flex size-7 items-center justify-center rounded-md bg-muted text-muted-foreground">
        <Icon className="size-3.5" aria-hidden />
      </span>
      <h2 className="mt-3 text-sm font-medium text-foreground">{title}</h2>
      <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{description}</p>
    </div>
  );
}
