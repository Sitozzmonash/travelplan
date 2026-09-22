"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ArrowRight, CalendarDays, CircleAlert, Compass, Database, Lock, MapPin, ShieldCheck, Sparkles, Users, Wallet } from "lucide-react";
import type { LucideIcon } from "lucide-react";
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
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { CityCombobox } from "@/components/city-combobox";
import { PlanningProgress } from "@/components/planning-progress";
import { SectionCard } from "@/components/section-card";
import { ErrorState, PartialNotice, RunStatusBadge, UnauthorizedState } from "@/components/state-views";
import { TripSearch } from "@/components/trip-search";
import { BUDGET_MODE_LABELS, formatTripLength } from "@/components/guided/options";
import { basicIssues, createEmptyDraft, resolvedDays, type BasicDraft, type BudgetMode } from "@/components/guided/draft";

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
            先把旅行研究明白
            <br />
            再出发
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-sm leading-6 text-muted-foreground sm:text-[15px] sm:leading-7">
            从交通、住宿策略到想吃想去的地方，先由你确认；TravelPlan 再把攻略、路线和预算组合成可执行的行程。
          </p>
        </div>

        <div className="mt-8">
          {phase === "idle" ? (
            <HomeLaunchpad onQuickSubmit={runPlanning} />
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

const BUDGET_MODES: BudgetMode[] = ["amount", "undecided", "auto"];

/**
 * 首页是基础信息的唯一入口：向导里已经没有「基础信息」页了，这里填的东西会经 URL
 * 原样变成创建 Planning Session 的输入。所以它直接复用向导的 BasicDraft 字段、
 * basicIssues() 校验与 CityCombobox，而不是另立一套字段名和排版语言 ——
 * 同一件事只保留一份规则，改口径时不会两处跑偏。
 */
function HomeLaunchpad({ onQuickSubmit }: { onQuickSubmit: (message: string) => void }) {
  const router = useRouter();
  const [basic, setBasic] = useState<BasicDraft>(() => createEmptyDraft().basic);
  const [issues, setIssues] = useState<string[]>([]);

  function patchBasic(patch: Partial<BasicDraft>) {
    setBasic((current) => ({ ...current, ...patch }));
    // 用户已经动过手，旧报错就不再对应当前状态了：先收起来，提交时整套重新校验。
    setIssues((current) => (current.length > 0 ? [] : current));
  }

  function startGuided(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // 校验不过就地报错，不跳转：带着半份基础信息进向导只会让人白走一趟。
    const found = basicIssues(basic);
    if (found.length > 0) {
      setIssues(found);
      return;
    }
    setIssues([]);
    const params = new URLSearchParams({
      origin: basic.origin.trim(),
      destination: basic.destination.trim(),
      start_date: basic.startDate,
      duration_mode: basic.durationMode,
      travelers: basic.travelers.trim(),
      budget_mode: basic.budgetMode,
    });
    if (basic.durationMode === "days") params.set("days", basic.days.trim());
    else params.set("end_date", basic.endDate);
    if (basic.budgetMode === "amount" && basic.budgetAmount.trim()) {
      params.set("budget_amount", basic.budgetAmount.trim());
    }
    router.push(`/guided?${params.toString()}`);
  }

  const days = resolvedDays(basic);
  const destinations = ["成都", "重庆", "杭州", "西安"];
  const styles = ["美食旅行", "亲子出行", "拍照漫游", "轻松度假"];

  return (
    <div className="space-y-4">
      <form onSubmit={startGuided} className="overflow-hidden rounded-2xl border border-border bg-card shadow-[0_24px_60px_-36px_color-mix(in_oklab,var(--color-primary),transparent_35%)]">
        <div className="border-b border-border/70 bg-[linear-gradient(120deg,color-mix(in_oklab,var(--color-accent),transparent_12%),transparent_68%)] px-4 py-4 sm:px-5">
          <p className="flex items-center gap-2 text-sm font-medium text-foreground">
            <Compass className="size-4 text-primary" aria-hidden />
            你想去哪里？
          </p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            先在这里把出发信息填全，接下来两步只确认你真正想要的旅行方式。
          </p>
        </div>
        <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
          <LabelField label="出发地" icon={MapPin}>
            <CityCombobox
              value={basic.origin}
              onChange={(next) => patchBasic({ origin: next })}
              placeholder="北京"
              ariaLabel="出发地"
              className={HOME_INPUT_CLASS}
            />
          </LabelField>
          <LabelField label="目的地" icon={MapPin}>
            <CityCombobox
              value={basic.destination}
              onChange={(next) => patchBasic({ destination: next })}
              placeholder="成都"
              ariaLabel="目的地"
              className={HOME_INPUT_CLASS}
            />
          </LabelField>

          <LabelField label="出发日期" icon={CalendarDays}>
            <input
              type="date"
              value={basic.startDate}
              aria-label="出发日期"
              onChange={(event) => patchBasic({ startDate: event.target.value })}
              className={HOME_INPUT_CLASS}
            />
          </LabelField>

          <LabelField label="行程长度" icon={CalendarDays}>
            <div className="grid gap-2">
              <div className="flex gap-2">
                <HomeSegmentedButton
                  selected={basic.durationMode === "days"}
                  onClick={() => patchBasic({ durationMode: "days" })}
                >
                  按天数
                </HomeSegmentedButton>
                <HomeSegmentedButton
                  selected={basic.durationMode === "endDate"}
                  onClick={() => patchBasic({ durationMode: "endDate" })}
                >
                  按返程日期
                </HomeSegmentedButton>
              </div>
              {basic.durationMode === "days" ? (
                <input
                  value={basic.days}
                  inputMode="numeric"
                  aria-label="行程天数"
                  placeholder="5"
                  onChange={(event) => patchBasic({ days: event.target.value })}
                  className={HOME_INPUT_CLASS}
                />
              ) : (
                <input
                  type="date"
                  value={basic.endDate}
                  aria-label="返程日期"
                  onChange={(event) => patchBasic({ endDate: event.target.value })}
                  className={HOME_INPUT_CLASS}
                />
              )}
              <p className="text-[11px] text-muted-foreground">
                {days ? `共 ${formatTripLength(days)}` : "填好日期后这里会显示总天数"}
              </p>
            </div>
          </LabelField>

          <LabelField label="几个人" icon={Users}>
            <input
              value={basic.travelers}
              inputMode="numeric"
              aria-label="出行人数"
              placeholder="2"
              onChange={(event) => patchBasic({ travelers: event.target.value })}
              className={HOME_INPUT_CLASS}
            />
          </LabelField>

          <LabelField label="预算" icon={Wallet}>
            <div className="grid gap-2">
              <div className="flex flex-wrap gap-2">
                {BUDGET_MODES.map((mode) => (
                  <HomeSegmentedButton
                    key={mode}
                    selected={basic.budgetMode === mode}
                    onClick={() => patchBasic({ budgetMode: mode })}
                  >
                    {BUDGET_MODE_LABELS[mode]}
                  </HomeSegmentedButton>
                ))}
              </div>
              {basic.budgetMode === "amount" ? (
                <input
                  value={basic.budgetAmount}
                  inputMode="numeric"
                  aria-label="总预算"
                  placeholder="6000"
                  onChange={(event) => patchBasic({ budgetAmount: event.target.value })}
                  className={HOME_INPUT_CLASS}
                />
              ) : (
                <p className="text-[11px] leading-5 text-muted-foreground">
                  {basic.budgetMode === "undecided"
                    ? "系统不会用预算卡你，只会在结果里给出花费明细。"
                    : "系统会按性价比控制总花费，并在结果里说明取舍。"}
                </p>
              )}
            </div>
          </LabelField>
        </div>

        {issues.length > 0 ? (
          <ul className="grid gap-1 border-t border-border/70 bg-danger-subtle px-4 py-3 text-xs text-danger-subtle-foreground sm:px-5">
            {issues.map((issue) => (
              <li key={issue} className="flex items-start gap-1.5">
                <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>{issue}</span>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="flex flex-col gap-3 border-t border-border/70 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <p className="text-[11px] leading-5 text-muted-foreground">
            出发地、目的地、出发日期与人数是必填的；预算可以填一个数，也可以交给系统控制。
          </p>
          <Button type="submit" className="min-h-10 shrink-0">
            开始逐步规划
            <ArrowRight className="size-4" aria-hidden />
          </Button>
        </div>
      </form>

      <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-center">
        <div className="flex flex-wrap gap-2" aria-label="热门目的地">
          {destinations.map((place) => (
            <button
              key={place}
              type="button"
              onClick={() => patchBasic({ destination: place })}
              className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/35 hover:text-foreground"
            >
              {place}
            </button>
          ))}
          {styles.map((style) => (
            <span key={style} className="rounded-full bg-muted px-3 py-1.5 text-xs text-muted-foreground">{style}</span>
          ))}
        </div>
        <details className="group text-left sm:text-right">
          <summary className="cursor-pointer list-none text-xs text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
            我已经想好了，直接一句话规划 <ArrowRight className="ml-1 inline size-3" aria-hidden />
          </summary>
          <div className="mt-3 text-left sm:w-[560px]">
            <TripSearch onSubmit={onQuickSubmit} />
          </div>
        </details>
      </div>
    </div>
  );
}

/** 首页的分段按钮：高度与 HOME_INPUT_CLASS 对齐，一行里的控件不再三种高度。 */
function HomeSegmentedButton({
  selected,
  onClick,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "inline-flex h-10 shrink-0 items-center rounded-lg border px-3 text-xs transition-colors",
        selected
          ? "border-primary/50 bg-accent font-medium text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function LabelField({
  label,
  icon: Icon,
  children,
}: {
  label: string;
  icon: LucideIcon;
  children: ReactNode;
}) {
  return (
    <label className="grid gap-1.5">
      <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </span>
      {children}
    </label>
  );
}

const HOME_INPUT_CLASS = "h-10 w-full rounded-lg border border-border bg-background px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus:border-primary/50 focus:ring-2 focus:ring-primary/15";
