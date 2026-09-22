"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, CircleAlert, LoaderCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionCard } from "@/components/section-card";
import { ErrorState, InlineWarning, PartialNotice } from "@/components/state-views";
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
  createEmptyDraft,
  fullDraftPatch,
  hasAnyPreference,
  preferenceReplayPatch,
  stepPatch,
  summarize,
  toCreateInput,
  type BasicDraft,
  type GuidedDraft,
  type PoiBulkMode,
} from "./draft";
import { bestValueSelections, recommendationStartIssue } from "./options";
import { STEP_META, WizardProgress } from "./wizard-progress";
import { DiscoveryResearch } from "./discovery-research";
import { StepPoi } from "./step-poi";
import { StepPreferences } from "./step-preferences";
import { SummaryPanel } from "./summary-panel";

/** 轮询间隔与上限：社交源再慢也不该让用户无限等（§17 不能因为 TikHub 慢而卡死）。 */
const POLL_INTERVAL_MS = 2500;
const POLL_MAX_ATTEMPTS = 144;
const POLL_MAX_FAILURES = 4;
const POI_SYNC_DEBOUNCE_MS = 700;

/** 对象身份隔离每一代会话；action 在 await / React 重渲染前同步上锁。 */
interface SessionSync {
  id: string | null;
  tail: Promise<boolean>;
  action: "create" | "sync" | "finish" | null;
  confirmed: boolean;
  runId: string | null;
  view: SessionView | null;
}

function newSessionSync(): SessionSync {
  return { id: null, tail: Promise.resolve(true), action: null, confirmed: false, runId: null, view: null };
}

/**
 * Guided 向导（§17）唯一的状态机。
 *
 * 只剩两步：0「探索确认」→ 1「偏好」。「基础信息」页已经删掉 —— 它的 6 个字段里
 * 5 个与首页重复，用户刚填完又填一遍；现在基础信息只由首页收集、经 URL 带进来。
 *
 * 会话生命周期（因为不再有第 1 页，创建时机整体前移）：
 *   挂载 → POST /planning-sessions（附上 URL 带来的基础信息，后台立刻 Prefetch）
 *        → 轮询 GET 直到 READY/PARTIAL，第 0 页就是 POI 列表
 *   第 0 页「继续」→ PATCH poi_selections
 *   第 1 页「开始规划」→ POST .../start → 拿 run_id → 跳现有 /plan/{run_id} 轮询页
 *
 * 基础信息这回是一次性的：URL 缺必填项时直接给错误态并请用户回首页，绝不静默拿默认值开跑。
 * 会话过期/被取消后仍可原地重建，重建时把已经选过的偏好（不含 POI）照旧补写回去。
 */
interface GuidedWizardProps {
  /** 首页是基础信息的唯一入口：URL 上带过来的就是这次会话的全部出发信息。 */
  initialBasic?: Partial<BasicDraft>;
}

export function GuidedWizard({ initialBasic }: GuidedWizardProps) {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<GuidedDraft>(() => {
    const empty = createEmptyDraft();
    // URL 上没带的参数是空字符串，不能直接 spread —— 那会把默认值（如"今天出发"）覆盖成空。
    const fromUrl = initialBasic ?? {};
    const basic = { ...empty.basic };
    if (fromUrl.origin?.trim()) basic.origin = fromUrl.origin;
    if (fromUrl.destination?.trim()) basic.destination = fromUrl.destination;
    if (fromUrl.startDate?.trim()) basic.startDate = fromUrl.startDate;
    if (fromUrl.days?.trim()) basic.days = fromUrl.days;
    if (fromUrl.endDate?.trim()) basic.endDate = fromUrl.endDate;
    if (fromUrl.travelers?.trim()) basic.travelers = fromUrl.travelers;
    if (fromUrl.budgetAmount?.trim()) basic.budgetAmount = fromUrl.budgetAmount;
    if (fromUrl.durationMode) basic.durationMode = fromUrl.durationMode;
    if (fromUrl.budgetMode) basic.budgetMode = fromUrl.budgetMode;
    return { ...empty, basic };
  });
  const draftRef = useRef(draft);
  const sessionSync = useRef<SessionSync>(newSessionSync());
  const [session, setSession] = useState<SessionView | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [pollStalled, setPollStalled] = useState(false);
  const [pollNonce, setPollNonce] = useState(0);
  const poiTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bootstrapped = useRef(false);

  const sessionId = session?.session_id ?? null;
  const settled = isDiscoverySettled(session);
  const unusable = isSessionUnusable(session);
  const socialFailed = hasSocialDegradation(session?.degradations ?? []);
  const startIssue = recommendationStartIssue(session, draft.poi.selections);
  const model = summarize(draft, session);
  const isLast = step === STEP_META.length - 1;
  // draft.basic 在向导里不会再被编辑（基础信息只由首页决定），所以每次渲染重算就够了。
  const missingBasic = basicIssues(draft.basic);

  useEffect(() => {
    if (!sessionId || settled || unusable || pollStalled || starting) return;
    const scope = sessionSync.current;
    if (scope.id !== sessionId) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let failures = 0;

    async function tick() {
      if (!active || scope !== sessionSync.current || scope.confirmed || !sessionId) return;
      try {
        const view = await getPlanningSession(sessionId);
        if (!active || scope !== sessionSync.current || scope.confirmed) return;
        scope.view = view;
        setSession(view);
        failures = 0;
        if (isDiscoverySettled(view) || isSessionUnusable(view)) return;
      } catch {
        if (!active || scope !== sessionSync.current || scope.confirmed) return;
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
  }, [sessionId, settled, unusable, pollStalled, pollNonce, starting]);

  useEffect(() => {
    return () => {
      if (poiTimer.current) clearTimeout(poiTimer.current);
    };
  }, []);

  /**
   * 挂载即建会话：第 0 页已经是 POI 列表，后台 Prefetch 不能再等用户点「继续」。
   *
   * 两处必须小心：
   * 1. StrictMode 在开发模式下会把 effect 跑两遍，用 ref 兜住，绝不重复 POST
   *    （重复创建不只是一个多余请求，还会把上一个会话晾在那里等 TTL）。
   * 2. URL 缺必填项时不建会话、不轮询，直接走下面的错误态 —— 拿默认值静默开跑
   *    会规划出一趟用户没说过要去的旅行。
   */
  useEffect(() => {
    if (bootstrapped.current) return;
    if (missingBasic.length > 0) return;
    bootstrapped.current = true;
    void rebuildSession();
    // 只在挂载时跑一次：rebuildSession 读的是此刻的 draft，之后的会话生命周期由用户操作驱动。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function updateDraft(update: (current: GuidedDraft) => GuidedDraft): void {
    // 最终确认锁定控件；ref guard 同时挡住重渲染前已经排入的点击。
    if (sessionSync.current.action === "finish" || sessionSync.current.confirmed) return;
    const next = update(draftRef.current);
    draftRef.current = next;
    setDraft(next);
  }

  function patchTransport(patch: Partial<GuidedDraft["transport"]>) {
    updateDraft((current) => ({ ...current, transport: { ...current.transport, ...patch } }));
  }

  function patchHotel(patch: Partial<GuidedDraft["hotel"]>) {
    updateDraft((current) => ({ ...current, hotel: { ...current.hotel, ...patch } }));
  }

  function patchPace(next: Pace) {
    updateDraft((current) => ({ ...current, pace: next }));
  }

  function patchPoiExpanded(key: string) {
    updateDraft((current) => ({
      ...current,
      poi: {
        ...current.poi,
        expanded: current.poi.expanded.includes(key)
          ? current.poi.expanded.filter((item) => item !== key)
          : [...current.poi.expanded, key],
      },
    }));
  }

  /** 重建开始时清空旧 POI，不能等新会话异步补写完后再清掉新选择。 */
  function clearPoi(): void {
    updateDraft((current) => ({ ...current, poi: { selections: {}, bulk: null, expanded: [] } }));
  }

  function cancelPoiSync(): void {
    if (poiTimer.current) clearTimeout(poiTimer.current);
    poiTimer.current = null;
  }

  function schedulePoiSync(): void {
    const scope = sessionSync.current;
    if (!scope.id || scope.action === "finish" || scope.confirmed) return;
    cancelPoiSync();
    poiTimer.current = setTimeout(() => {
      poiTimer.current = null;
      if (scope !== sessionSync.current || scope.action === "finish" || scope.confirmed) return;
      void syncNow(() => stepPatch(0, draftRef.current), scope);
    }, POI_SYNC_DEBOUNCE_MS);
  }

  function selectPoi(placeId: string, state: PoiSelection | null): void {
    updateDraft((current) => {
      const selections = { ...current.poi.selections };
      if (state) selections[placeId] = state;
      else delete selections[placeId];
      return { ...current, poi: { ...current.poi, selections, bulk: null } };
    });
    schedulePoiSync();
  }

  /**
   * 两个总开关都映射成真实的 poi_selections：
   *   使用这份推荐 → 空映射（后端使用已筛选的推荐，不是原始候选池）
   *   只安排最值得去的 → 每个类别按证据/可信度取前 3 个标成 WANT
   */
  function selectBulk(mode: PoiBulkMode): void {
    const selections: Record<string, PoiSelection> = mode === "auto" ? {} : bestValueSelections(session);
    updateDraft((current) => ({ ...current, poi: { ...current.poi, selections, bulk: mode } }));
    schedulePoiSync();
  }

  /** 创建（或重建）会话：先换代，使旧 PATCH / 轮询响应立刻失效。 */
  async function rebuildSession(): Promise<boolean> {
    const previous = sessionSync.current;
    if (previous.action === "create" || previous.action === "finish" || previous.confirmed) return false;
    const scope = newSessionSync();
    scope.action = "create";
    sessionSync.current = scope;
    cancelPoiSync();
    if (previous.id) void cancelPlanningSession(previous.id);
    clearPoi();
    setSession(null);
    setCreating(true);
    setSyncing(false);
    setCreateError(null);
    setSyncError(null);
    setStartError(null);
    try {
      const created = await createPlanningSession(toCreateInput(draftRef.current.basic));
      if (scope !== sessionSync.current) return false;
      scope.id = created.session_id;
      scope.view = created;
      setSession(created);
      setPollStalled(false);
      if (hasAnyPreference(draftRef.current)) {
        // 补写也走同一队列；失败不阻塞浏览，但最终确认必须重新提交完整草稿。
        await syncNow(() => preferenceReplayPatch(draftRef.current), scope);
      }
      return scope === sessionSync.current;
    } catch (cause) {
      if (scope === sessionSync.current) setCreateError(describeSessionError(cause));
      return false;
    } finally {
      if (scope === sessionSync.current) {
        scope.action = null;
        setCreating(false);
      }
    }
  }

  /** 所有 PATCH 的唯一出口：串行执行，真正发出时才读取草稿，失败显式返回 false。 */
  function syncNow(patch: () => SessionPatchInput, scope: SessionSync): Promise<boolean> {
    const id = scope.id;
    if (!id || scope !== sessionSync.current || scope.confirmed) return Promise.resolve(false);
    const result = scope.tail.then(async () => {
      if (scope !== sessionSync.current || scope.confirmed) return false;
      try {
        const view = await patchPlanningSession(id, patch());
        if (scope !== sessionSync.current) return false;
        scope.view = view;
        setSession(view);
        if (view.status === "STARTING" && view.run_id) scope.runId = view.run_id;
        if (isSessionUnusable(view) || view.status === "STARTING") {
          setSyncError("会话已关闭或已经开始规划，无法写回偏好。请检查会话状态。");
          return false;
        }
        setSyncError(null);
        return true;
      } catch (cause) {
        if (scope === sessionSync.current) setSyncError(describeSessionError(cause));
        return false;
      }
    });
    // 失败也让链正常结算，下一次重试不会被旧 rejection 阻断。
    scope.tail = result;
    return result;
  }

  async function syncStep(advanceTo: number, full = false): Promise<void> {
    const scope = sessionSync.current;
    if (!scope.id || scope.action || scope.confirmed) return;
    scope.action = "sync";
    cancelPoiSync();
    setSyncing(true);
    try {
      await syncNow(() => full ? fullDraftPatch(draftRef.current) : stepPatch(step, draftRef.current), scope);
    } finally {
      if (scope === sessionSync.current) {
        scope.action = null;
        setSyncing(false);
        // 中途同步失败仍可浏览下一步；只有最终确认必须成功才允许 START。
        setStep(advanceTo);
      }
    }
  }

  async function handleContinue(): Promise<void> {
    const scope = sessionSync.current;
    if (scope.action || scope.confirmed) return;
    if (!scope.id) {
      // 创建失败时不静默进入下一屏，先给这一页的选择建立会话。
      await rebuildSession();
      return;
    }
    await syncStep(step + 1);
  }

  /** 锁定选择 → 排空已发 / 已排 PATCH → 最新完整草稿 → 成功后才 START。 */
  async function handleFinish(): Promise<void> {
    const scope = sessionSync.current;
    if (scope.action || scope.confirmed) return;
    if (!scope.id) {
      setStartError("会话还没创建成功，无法开始规划。请先重新创建会话。");
      return;
    }
    const issue = recommendationStartIssue(scope.view, draftRef.current.poi.selections);
    if (issue) {
      setStartError(issue);
      return;
    }
    scope.action = "finish";
    cancelPoiSync();
    setStarting(true);
    setStartError(null);
    try {
      const ok = await syncNow(() => fullDraftPatch(draftRef.current), scope);
      if (scope !== sessionSync.current) return;
      if (!ok) {
        // 上次 START 可能已被服务器接收，只是响应丢失。只恢复已确认的 Run，
        // 不重复启动，也不把被冻结的偏好冒充成本次修改已成功。
        if (scope.runId) {
          scope.confirmed = true;
          router.push(`/plan/${encodeURIComponent(scope.runId)}`);
          return;
        }
        setStartError("部分偏好尚未写回会话，请先重试同步后再开始规划。");
        return;
      }
      const latestIssue = recommendationStartIssue(scope.view, draftRef.current.poi.selections);
      if (latestIssue) {
        setStartError(latestIssue);
        return;
      }
      // 此后禁止任何 POI / 重试 PATCH 插入，直到 START 失败并由用户重新确认。
      scope.confirmed = true;
      const result = await startPlanningSession(scope.id);
      if (scope !== sessionSync.current) return;
      router.push(`/plan/${encodeURIComponent(result.run_id)}`);
    } catch (cause) {
      if (scope === sessionSync.current) {
        scope.confirmed = false;
        setStartError(describeSessionError(cause));
      }
    } finally {
      if (scope === sessionSync.current && !scope.confirmed) {
        scope.action = null;
        setStarting(false);
      }
    }
  }

  /** 会话过期 / 被取消后的重建入口：按 URL 带来的基础信息重开，偏好原样补写回去。 */
  async function handleRestart(): Promise<void> {
    const scope = sessionSync.current;
    if (scope.action === "create" || scope.action === "finish" || scope.confirmed) return;
    const ok = await rebuildSession();
    if (!ok) {
      setStep(0);
      return;
    }
    setPollNonce((value) => value + 1);
  }

  function retrySync(): void {
    // 不保存失败 PATCH：每次重试覆盖为最新完整草稿，包含已取消的选择。
    void syncStep(step, true);
  }

  function retryDiscovery(): void {
    const scope = sessionSync.current;
    if (scope.action || scope.confirmed) return;
    if (isDiscoverySettled(scope.view) || isSessionUnusable(scope.view)) {
      // 终态仅重新 GET 不会重跑推荐；显式重建，保留偏好但清理旧地点选择。
      void handleRestart();
      return;
    }
    setPollStalled(false);
    setPollNonce((value) => value + 1);
  }

  const discoveryPending = step === 0 && !settled && !unusable;
  const primaryLabel = isLast
    ? "开始规划"
    : creating && !session
      ? "正在创建会话…"
      : !session && createError
        ? "重新创建会话"
        : discoveryPending
          ? "先填写偏好"
          : "继续";
  const busy = creating || syncing || starting;

  if (missingBasic.length > 0) {
    return (
      <div className="mx-auto w-full max-w-2xl px-4 py-8 pb-20 sm:px-6">
        <Link
          href="/"
          className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" aria-hidden />
          返回首页
        </Link>
        <div className="mt-4">
          <ErrorState
            title="这次向导缺少出发信息"
            description="向导现在直接从「探索确认」开始，出发地、目的地、日期和人数由首页带过来；这份参数不完整，所以没有替你创建会话。"
            detail={`还缺少：${missingBasic.join("；")}`}
            onRetry={() => router.push("/")}
            retryLabel="回首页填写"
          />
        </div>
      </div>
    );
  }

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
            两步就够：先确认探索出来的地点，再定交通、酒店与节奏。任何一项都能「随便 / 帮我选 / 不确定」，
            这些选择会原样提交给规划服务，不存在只做样子的按钮。
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
                onRetry={() => void rebuildSession()}
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
                  <Button variant="outline" size="sm" onClick={retrySync} disabled={busy}>
                    <RefreshCw />
                    重试同步
                  </Button>
                </div>
              </div>
            ) : null}

            {unusable ? (
              <SessionUnusableNotice
                status={session?.status ?? "EXPIRED"}
                busy={creating}
                onRestart={() => void handleRestart()}
              />
            ) : null}

            {session?.error ? (
              <InlineWarning title="会话报告了一个问题" description={session.error} />
            ) : null}

            {isLast && socialFailed ? (
              <PartialNotice
                title="部分攻略来源不可用"
                description="可以继续填写偏好；正式开始前请确认已有可用推荐。"
                detail="推荐卡片会如实显示关联攻略数量。"
              />
            ) : null}

            {startError ? (
              <ErrorState
                title="没能开始规划"
                description={startError}
                detail={session ? `session_id：${session.session_id}` : undefined}
                onRetry={() => startIssue ? setStep(0) : void handleFinish()}
                retryLabel={startIssue ? "返回探索页" : "再试一次"}
              />
            ) : null}

            {isLast && startIssue && !startError ? (
              <InlineWarning title="暂时不能开始规划" description={startIssue} />
            ) : null}
            {session && isLast ? <DiscoveryResearch session={session} settled={settled} /> : null}
          </div>

          <SectionCard
            title={STEP_META[step].title}
            description={STEP_META[step].subtitle}
            action={session ? <SessionStatusChip session={session} poiStep={step === 0} /> : null}
            className="mt-3 shadow-sm"
          >
            <fieldset disabled={starting} className="m-0 min-w-0 border-0 p-0">
              {step === 0 ? (
                <StepPoi
                  session={session}
                  settled={settled}
                  unusable={unusable}
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
              {isLast ? (
                <StepPreferences
                  draft={draft}
                  onTransport={patchTransport}
                  onHotel={patchHotel}
                  onPace={patchPace}
                />
              ) : null}
            </fieldset>
          </SectionCard>

          <div className="sticky bottom-0 z-20 mt-4 flex items-center justify-between gap-3 border-t border-border/70 bg-background/95 py-3 backdrop-blur-sm sm:static sm:bg-transparent sm:py-0 sm:pt-4">
            {step === 0 ? (
              // 不写死「会话已经建好」：创建失败时上面已经有错误卡，这里再宣称建好了就是自相矛盾。
              <span className="hidden text-[11px] text-muted-foreground sm:block">
                {session
                  ? "先确认攻略推荐，也可以先填写偏好；交通班次与酒店产品将在正式规划时查询。"
                  : "会话还没建好，这页勾选的地点暂时没有落点；先按上面的提示重新创建会话。"}
              </span>
            ) : (
              <Button
                variant="outline"
                size="lg"
                className="h-11 sm:h-9"
                disabled={busy}
                onClick={() => setStep((current) => Math.max(current - 1, 0))}
              >
                上一步
              </Button>
            )}

            <Button
              size="lg"
              className="h-11 min-w-[150px] sm:h-9 sm:min-w-[120px]"
              disabled={busy || unusable || (isLast && Boolean(startIssue))}
              onClick={() => {
                if (isLast) void handleFinish();
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
  const recommendationIssue = recommendationStartIssue(session, {});
  const label = isSessionUnusable(session)
    ? SESSION_STATUS_LABELS[session.status]
    : recommendationIssue
      ? isDiscoverySettled(session) ? "推荐暂不可用" : "等待推荐就绪"
      : showDiscovery
        ? DISCOVERY_STATUS_LABELS[session.discovery_status]
        : SESSION_STATUS_LABELS[session.status];
  const tone =
    isSessionUnusable(session) || recommendationIssue
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
        会话只在一段时间内有效，过期不会产生失败记录。点下面的按钮会按这次出发信息重新创建一次，
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
