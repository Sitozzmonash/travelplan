"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { CircleAlert, Database, Lock, ShieldCheck, Sparkles } from "lucide-react";
import type { PlanningStepState } from "@/types/api";
import { API_BASE_URL, API_MODE, createPlan, createPlanningSteps, describeApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { PlanningProgress } from "@/components/planning-progress";
import { SectionCard } from "@/components/section-card";
import { ErrorState } from "@/components/state-views";
import { TripSearch } from "@/components/trip-search";

type Phase = "idle" | "planning" | "error";

export default function HomePage() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("idle");
  const [steps, setSteps] = useState<PlanningStepState[]>(() => createPlanningSteps());
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  const runPlanning = useCallback(
    async (message: string) => {
      setQuery(message);
      setSteps(createPlanningSteps());
      setError(null);
      setPhase("planning");
      try {
        const result = await createPlan(message, {
          onProgress: (event) => {
            setSteps((current) =>
              current.map((step) =>
                step.id === event.step_id
                  ? { ...step, status: event.status, facts: event.facts ?? step.facts }
                  : step,
              ),
            );
          },
        });
        router.push(`/plan/${encodeURIComponent(result.run_id)}`);
      } catch (cause) {
        setError(describeApiError(cause));
        setPhase("error");
      }
    },
    [router],
  );

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
          {phase === "idle" ? <TripSearch onSubmit={runPlanning} /> : null}

          {phase === "planning" ? (
            <SectionCard
              title="正在规划"
              icon={Sparkles}
              description={query ? `需求：${query}` : undefined}
              className="shadow-sm"
            >
              <PlanningProgress steps={steps} />
              <div className="mt-4 flex items-center justify-between gap-3 border-t border-border/70 pt-3">
                <p className="text-[11px] leading-5 text-muted-foreground">
                  规划过程中会依次调用交通、酒店、攻略与路线 Provider，全部完成后自动进入行程工作台。
                </p>
                <Button variant="outline" size="sm" onClick={() => setPhase("idle")}>
                  返回修改
                </Button>
              </div>
            </SectionCard>
          ) : null}

          {phase === "error" ? (
            <div className="space-y-3">
              <ErrorState
                title="这次没有生成行程"
                description={error ?? "规划服务没有返回结果，本次没有产出可用行程。"}
                detail={`规划服务地址：${API_BASE_URL}`}
                onRetry={() => runPlanning(query)}
                retryLabel="重新规划"
              />
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
              当前为演示数据模式，行程来自内置示例；接入本地 FastAPI 后即使用真实查询结果。
            </>
          ) : (
            <>
              <Database className="size-3" aria-hidden />
              已连接规划服务：{API_BASE_URL}
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
            description="高德、途牛、TikHub 等 Provider 的 Key 全部由后端持有，前端只读取结构化结果与来源链接。"
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
