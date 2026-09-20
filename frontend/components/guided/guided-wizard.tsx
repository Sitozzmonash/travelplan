"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, CircleAlert, LoaderCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/section-card";
import { ErrorState, InlineWarning } from "@/components/state-views";
import {
  API_BASE_URL,
  DISCOVERY_STATUS_LABELS,
  SESSION_STATUS_LABELS,
  cancelPlanningSession,
  createPlanningSession,
  describeSessionError,
  getPlanningSession,
  hasSocialDegradation,
  isDiscoverySettled,
  isSessionUnusable,
  patchPlanningSession,
  startPlanningSession,
} from "@/lib/sessions";
import { cn } from "@/lib/utils";
import type { Pace, PoiSelection, SessionPatchInput, SessionView } from "@/types/session";
import {
  basicIssues,
  budgetPatch,
  createEmptyDraft,
  hasAnyPreference,
  prefetchKey,
  preferenceReplayPatch,
  stepPatch,
  summarize,
  toCreateInput,
  type GuidedDraft,
  type PoiBulkMode,
} from "./draft";
import { bestValueSelections } from "./options";
import { STEP_META, WizardProgress } from "./wizard-progress";
import { StepBasic } from "./step-basic";
import { StepConfirm } from "./step-confirm";
import { StepHotel } from "./step-hotel";
import { StepPace } from "./step-pace";
import { StepPoi } from "./step-poi";
import { StepTransport } from "./step-transport";
import { SummaryPanel } from "./summary-panel";

/** 轮询间隔与上限：社交源再慢也不该让用户无限等（§17 不能因为 TikHub 慢而卡死）。 */
const POLL_INTERVAL_MS = 2500;
const POLL_MAX_ATTEMPTS = 144;
const POLL_MAX_FAILURES = 4;
const POI_SYNC_DEBOUNCE_MS = 700;

/**
 * Guided 向导（§17）唯一的状态机。
 *
 * 会话生命周期：
 *   Step 1「继续」→ POST /planning-sessions（后台立刻 Prefetch）→ 轮询 GET 直到 READY/PARTIAL
 *   Step 2~5「继续」→ PATCH 结构化偏好
 *   Step 6「开始规划」→ POST .../start → 拿 run_id → 跳现有 /plan/{run_id} 轮询页
 *
 * 基础信息（出发地 / 目的地 / 日期 / 天数）一变：旧会话取消、POI 清空、重新创建会话，
 * 绝不沿用旧城市的地点（§16）。
 */
export function GuidedWizard() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<GuidedDraft>(createEmptyDraft);
  const [session, setSession] = useState<SessionView | null>(null);
  const [sessionKey, setSessionKey] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [pendingPatch, setPendingPatch] = useState<SessionPatchInput | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [showIssues, setShowIssues] = useState(false);
  const [pollStalled, setPollStalled] = useState(false);
  const [pollNonce, setPollNonce] = useState(0);
  const [poiReset, setPoiReset] = useState(false);
  const poiTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const sessionId = session?.session_id ?? null;
  const settled = isDiscoverySettled(session);
  const unusable = isSessionUnusable(session);
  const socialFailed = hasSocialDegradation(session?.degradations ?? []);
  const model = summarize(draft, session);
  const isConfirm = step === STEP_META.length - 1;

  useEffect(() => {
    if (!sessionId || settled || unusable || pollStalled) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let failures = 0;

    async function tick() {
      if (!active || !sessionId) return;
      try {
        const view = await getPlanningSession(sessionId);
        if (!active) return;
        setSession(view);
        failures = 0;
        if (isDiscoverySettled(view) || isSessionUnusable(view)) return;
      } catch {
        if (!active) return;
        failures += 1;
        attempts += 1;
        if (failures >= POLL_MAX_FAILURES || attempts >= POLL_MAX_ATTEMPTS) {
          setPollStalled(true);
          return;
        }
        timer = setTimeout(() => {
          void tick();
        }, POLL_INTERVAL_MS);
        return;
      }
      attempts += 1;
      if (attempts >= POLL_MAX_ATTEMPTS) {
        setPollStalled(true);
        return;
      }
      timer = setTimeout(() => {
        void tick();
      }, POLL_INTERVAL_MS);
    }

    void tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, settled, unusable, pollStalled, pollNonce]);

  useEffect(() => {
    return () => {
      if (poiTimer.current) clearTimeout(poiTimer.current);
    };
  }, []);

  function patchBasic(patch: Partial<GuidedDraft["basic"]>) {
    setDraft((current) => ({ ...current, basic: { ...current.basic, ...patch } }));
  }

  function patchTransport(patch: Partial<GuidedDraft["transport"]>) {
    setDraft((current) => ({ ...current, transport: { ...current.transport, ...patch } }));
  }

  function patchHotel(patch: Partial<GuidedDraft["hotel"]>) {
    setDraft((current) => ({ ...current, hotel: { ...current.hotel, ...patch } }));
  }

  function patchPace(next: Pace) {
    setDraft((current) => ({ ...current, pace: next }));
  }

  function patchPoiExpanded(key: string) {
    setDraft((current) => ({
      ...current,
      poi: {
        ...current.poi,
        expanded: current.poi.expanded.includes(key)
          ? current.poi.expanded.filter((item) => item !== key)
          : [...current.poi.expanded, key],
      },
    }));
  }

  /** 清空旧城市的 POI：基础信息变了以后必须走这里。 */
  function clearPoi(): void {
    setDraft((current) => ({ ...current, poi: { selections: {}, bulk: null, expanded: [] } }));
  }

  function schedulePoiSync(selections: Record<string, PoiSelection>): void {
    const id = sessionId;
    if (!id) return;
    if (poiTimer.current) clearTimeout(poiTimer.current);
    poiTimer.current = setTimeout(() => {
      void patchPlanningSession(id, { poi_selections: selections })
        .then((view) => {
          setSession(view);
          setSyncError(null);
          setPendingPatch(null);
        })
        .catch((cause) => {
          setSyncError(describeSessionError(cause));
          setPendingPatch({ poi_selections: selections });
        });
    }, POI_SYNC_DEBOUNCE_MS);
  }

  function selectPoi(placeId: string, state: PoiSelection | null): void {
    const selections = { ...draft.poi.selections };
    if (state) selections[placeId] = state;
    else delete selections[placeId];
    setDraft((current) => ({ ...current, poi: { ...current.poi, selections, bulk: null } }));
    schedulePoiSync(selections);
  }

  /**
   * 两个总开关都映射成真实的 poi_selections：
   *   都随便，帮我安排 → 空映射（全部交给 Planner）
   *   只安排最值得去的 → 每个类别按证据/可信度取前 3 个标成 WANT
   */
  function selectBulk(mode: PoiBulkMode): void {
    const selections: Record<string, PoiSelection> = mode === "auto" ? {} : bestValueSelections(session);
    setDraft((current) => ({ ...current, poi: { ...current.poi, selections, bulk: mode } }));
    schedulePoiSync(selections);
  }

  /** 创建（或重建）会话：取消旧会话 + 取消待发送的 POI 同步，避免打到已作废的 session。 */
  async function rebuildSession(): Promise<boolean> {
    const previous = sessionId;
    if (previous) void cancelPlanningSession(previous);
    if (poiTimer.current) {
      clearTimeout(poiTimer.current);
      poiTimer.current = null;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createPlanningSession(toCreateInput(draft.basic));
      setSession(created);
      setSessionKey(prefetchKey(draft.basic));
      setPollStalled(false);
      setSyncError(null);
      setPendingPatch(null);
      setStartError(null);
      if (hasAnyPreference(draft)) {
        try {
          const replayed = await patchPlanningSession(created.session_id, preferenceReplayPatch(draft));
          setSession(replayed);
        } catch {
          // 偏好补写失败不阻塞：用户继续往下走时还会再写一次。
        }
      }
      return true;
    } catch (cause) {
      setCreateError(describeSessionError(cause));
      return false;
    } finally {
      setCreating(false);
    }
  }

  async function syncNow(patch: SessionPatchInput, advanceTo: number): Promise<void> {
    const id = sessionId;
    if (!id || Object.keys(patch).length === 0) {
      setStep(advanceTo);
      return;
    }
    // 这次要发的就是最新的 POI 选择时，取消还在等待的防抖同步，避免重复 PATCH。
    if (patch.poi_selections && poiTimer.current) {
      clearTimeout(poiTimer.current);
      poiTimer.current = null;
    }
    setSyncing(true);
    try {
      const view = await patchPlanningSession(id, patch);
      setSession(view);
      setSyncError(null);
      setPendingPatch(null);
    } catch (cause) {
      // 写不回后端也不拦住用户：明确告知 + 提供重试，而不是把人卡在这一步。
      setSyncError(describeSessionError(cause));
      setPendingPatch(patch);
    } finally {
      setSyncing(false);
      setStep(advanceTo);
    }
  }

  async function handleContinue(): Promise<void> {
    if (step === 0) {
      const issues = basicIssues(draft.basic);
      if (issues.length > 0) {
        setShowIssues(true);
        return;
      }
      setShowIssues(false);
      const key = prefetchKey(draft.basic);
      const changed = sessionKey !== null && sessionKey !== key;
      if (!session || changed || unusable) {
        const previousKey = sessionKey;
        const ok = await rebuildSession();
        if (!ok) return;
        if ((previousKey !== null && previousKey !== key) || Object.keys(draft.poi.selections).length > 0) {
          clearPoi();
          setPoiReset(previousKey !== null && previousKey !== key);
        }
        setStep(1);
        return;
      }
      await syncNow(budgetPatch(draft), 1);
      return;
    }
    if (step >= 1 && step <= 4) {
      await syncNow(stepPatch(step, draft), step + 1);
    }
  }

  async function handleStart(): Promise<void> {
    if (!sessionId) {
      setStep(0);
      setShowIssues(true);
      return;
    }
    setStarting(true);
    setStartError(null);
    try {
      const result = await startPlanningSession(sessionId);
      router.push(`/plan/${encodeURIComponent(result.run_id)}`);
    } catch (cause) {
      setStartError(describeSessionError(cause));
      setStarting(false);
    }
  }

  /** 会话过期 / 被取消后的重建入口：按当前基础信息重开，偏好原样补写回去。 */
  async function handleRestart(): Promise<void> {
    const ok = await rebuildSession();
    if (!ok) {
      setStep(0);
      return;
    }
    clearPoi();
    setPoiReset(false);
    setPollNonce((value) => value + 1);
  }

  function retrySync(): void {
    // 重试时留在当前步骤：偏好没写回去也要让用户能继续，而不是把人卡在这一步。
    const patch = pendingPatch ?? stepPatch(Math.max(step - 1, 0), draft);
    void syncNow(patch, step);
  }

  function retryDiscovery(): void {
    setPollStalled(false);
    setPollNonce((value) => value + 1);
  }

  const discoveryPending = step === 3 && !settled && !unusable;
  const primaryLabel = isConfirm
    ? "开始规划"
    : step === 0
      ? creating
        ? "正在创建会话…"
        : "继续，后台开始找攻略"
      : discoveryPending
        ? "都随便，继续"
        : "继续";
  const busy = creating || syncing || starting;

  return (
    <div className="mx-auto w-full max-w-[1440px] px-4 py-6 pb-24 sm:px-6 lg:pb-10">
      <header className="grid gap-3">
        <Link
          href="/"
          className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          返回首页
        </Link>
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground sm:text-2xl">逐项选择旅行偏好</h1>
          <p className="mt-1.5 max-w-2xl text-xs leading-5 text-muted-foreground sm:text-sm sm:leading-6">
            每一步只问一件事，任何一项都能「随便 / 帮我选 / 不确定」。这些选择会原样提交给规划服务，
            不存在只做样子的按钮。
          </p>
        </div>
        <p className="lg:hidden text-xs text-muted-foreground">
          {model.origin} → {model.destination} · {model.lengthLabel} · {model.travelersLabel} · 预算{" "}
          {model.budgetLabel}
        </p>
        <WizardProgress current={step} onJump={setStep} />
      </header>

      <div className="mt-5 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0">
          <div className="grid gap-3">
            {createError ? (
              <ErrorState
                title="没能创建这次的会话"
                description={createError}
                detail={`会话服务地址：${API_BASE_URL}`}
                onRetry={() => void handleContinue()}
                retryLabel="重新创建"
              />
            ) : null}

            {syncError ? (
              <div className="grid gap-2 rounded-lg border border-warning/30 bg-warning-subtle px-3.5 py-3">
                <p className="flex items-start gap-2 text-xs leading-5 text-warning-subtle-foreground">
                  <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>这一步的偏好没能写回后端：{syncError}</span>
                </p>
                <div className="flex justify-end">
                  <Button variant="outline" size="sm" onClick={retrySync} disabled={syncing}>
                    <RefreshCw />
                    重试同步
                  </Button>
                </div>
              </div>
            ) : null}

            {unusable && step > 0 ? (
              <SessionUnusableNotice
                status={session?.status ?? "EXPIRED"}
                busy={creating}
                onRestart={() => void handleRestart()}
              />
            ) : null}

            {session?.error ? (
              <InlineWarning title="会话报告了一个问题" description={session.error} />
            ) : null}
          </div>

          <SectionCard
            title={STEP_META[step].title}
            description={STEP_META[step].subtitle}
            action={session ? <SessionStatusChip session={session} poiStep={step === 3} /> : null}
            className="mt-3 shadow-sm"
          >
            {step === 0 ? (
              <StepBasic
                value={draft.basic}
                onChange={patchBasic}
                issues={basicIssues(draft.basic)}
                showIssues={showIssues}
                busy={creating}
              />
            ) : null}
            {step === 1 ? <StepTransport draft={draft} onChange={patchTransport} /> : null}
            {step === 2 ? <StepHotel draft={draft} onChange={patchHotel} /> : null}
            {step === 3 ? (
              <StepPoi
                session={session}
                settled={settled}
                unusable={unusable}
                resetNotice={poiReset}
                pollStalled={pollStalled}
                selections={draft.poi.selections}
                expanded={draft.poi.expanded}
                bulk={draft.poi.bulk}
                onSelect={selectPoi}
                onBulk={selectBulk}
                onToggleExpand={patchPoiExpanded}
                onRetryDiscovery={retryDiscovery}
              />
            ) : null}
            {step === 4 ? <StepPace pace={draft.pace} onChange={patchPace} /> : null}
            {step === 5 ? (
              <StepConfirm
                model={model}
                session={session}
                settled={settled}
                socialFailed={socialFailed}
                unusable={unusable}
                startError={startError}
                onRetryStart={() => void handleStart()}
              />
            ) : null}
          </SectionCard>

          <div className="sticky bottom-0 z-20 mt-4 flex items-center justify-between gap-3 border-t border-border/70 bg-background/95 py-3 backdrop-blur-sm sm:static sm:bg-transparent sm:py-0 sm:pt-4">
            {step === 0 ? (
              <span className="hidden text-[11px] text-muted-foreground sm:block">
                点「继续」后请保持页面打开，系统会在后台开始查交通、酒店与攻略。
              </span>
            ) : (
              <Button
                variant="outline"
                size="lg"
                className="h-11 sm:h-9"
                disabled={busy}
                onClick={() => {
                  if (isConfirm) setStep(0);
                  else setStep((current) => Math.max(current - 1, 0));
                }}
              >
                {isConfirm ? "返回修改" : "上一步"}
              </Button>
            )}

            <Button
              size="lg"
              className="h-11 min-w-[150px] sm:h-9 sm:min-w-[120px]"
              disabled={busy || (unusable && step > 0)}
              onClick={() => {
                if (isConfirm) void handleStart();
                else void handleContinue();
              }}
            >
              {busy ? <LoaderCircle className="animate-spin" /> : null}
              {primaryLabel}
            </Button>
          </div>

          <p className="mt-3 hidden text-[11px] text-muted-foreground lg:block">
            会话服务：{API_BASE_URL} · 只有点「开始规划」才会创建正式 Run，中途关闭页面不会留下失败记录。
          </p>
        </div>

        <aside className="hidden lg:block">
          <div className="sticky top-20">
            <SummaryPanel model={model} />
          </div>
        </aside>
      </div>
    </div>
  );
}

/** 会话状态徽标：措辞与正式 Run 的 RUNNING / SUCCESS 完全分开，避免用户误以为已经在生成行程。 */
function SessionStatusChip({ session, poiStep }: { session: SessionView; poiStep: boolean }) {
  const showDiscovery = poiStep && session.discovery_status !== "READY";
  const label = showDiscovery
    ? DISCOVERY_STATUS_LABELS[session.discovery_status]
    : SESSION_STATUS_LABELS[session.status];
  const tone =
    session.status === "EXPIRED" || session.status === "CANCELLED"
      ? "bg-warning-subtle text-warning-subtle-foreground"
      : session.status === "READY"
        ? "bg-success-subtle text-success-subtle-foreground"
        : "bg-info-subtle text-info-subtle-foreground";
  return (
    <span
      className={cn(
        "hidden shrink-0 items-center rounded-md px-2 py-0.5 text-[11px] font-medium sm:inline-flex",
        tone,
      )}
      data-testid="session-status"
    >
      {label}
    </span>
  );
}

/** 会话过期 / 取消：与 Run 的 FAILED 区分措辞，并给出明确的重建入口。 */
function SessionUnusableNotice({
  status,
  busy,
  onRestart,
}: {
  status: string;
  busy: boolean;
  onRestart: () => void;
}) {
  const expired = status === "EXPIRED";
  return (
    <div className="grid gap-2 rounded-lg border border-warning/30 bg-warning-subtle px-4 py-3.5">
      <p className="text-xs font-medium text-warning-subtle-foreground">
        {expired ? "这次会话已过期，请重新开始。" : "这次会话已被取消。"}
      </p>
      <p className="text-[11px] leading-5 text-warning-subtle-foreground/90">
        会话只在一段时间内有效，过期不会产生失败记录。点下面的按钮会按当前填写的出发信息重新创建一次，
        已经选过的交通 / 酒店 / 节奏会一并带过去。
      </p>
      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={onRestart} disabled={busy}>
          <RefreshCw />
          重新开始
        </Button>
      </div>
    </div>
  );
}
