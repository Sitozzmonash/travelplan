/**
 * TravelPlan API Adapter（FRONTEND_DESIGN §23 / §34）。
 *
 * 这是前端唯一的数据出口：页面组件不直接 fetch，也不内置 mock 数据。
 *   mockData → lib/api.ts → UI
 *
 * 切换到真实 FastAPI 的方式：
 *   1) 设置 NEXT_PUBLIC_API_BASE_URL（非 secret，默认 http://localhost:8000）
 *   2) 设置 NEXT_PUBLIC_USE_MOCK_API=false
 * 组件不需要任何改动。
 *
 * 这里不保存、不引用任何 Provider Secret（AMAP / TikHub / 途牛等一律由后端持有）。
 */

import type {
  AuditReport,
  CreatePlanResult,
  PlanProgressEvent,
  PlanningStepState,
  ReviseRequest,
  ReviseResult,
  RunProgress,
  RunStageProgress,
  RunStatus,
  StepStatus,
} from "@/types/api";
import type { Evidence, TripPlan } from "@/types/plan";
import { MOCK_STEP_DELAY_MS, MOCK_STEP_RESULTS, initialStepStates, stageLabel } from "./mock/progress";
import { mockAudit } from "./mock/audit";
import { mockPlan } from "./mock/plan";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** true = 使用内置演示数据；false = 全部请求发往 FastAPI。 */
export const USE_MOCK_API = (process.env.NEXT_PUBLIC_USE_MOCK_API ?? "false") !== "false";

export const API_MODE: "mock" | "live" = USE_MOCK_API ? "mock" : "live";

export type ApiErrorKind =
  | "network"
  | "not_found"
  | "endpoint_missing"
  | "unauthorized"
  | "server"
  | "invalid"
  | "timeout";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly detail?: string;

  constructor(message: string, kind: ApiErrorKind, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.detail = detail;
  }
}

const TIMEOUT_MS = 120_000;
/**
 * 创建计划的超时。这个接口的耗时由后端决定，不由前端决定：实测一次 5 天行程要 7~9 分钟
 * （12306 MCP 冷启动十几秒、多个 Provider 串行、Critic 还要再走一轮模型）。沿用 120s
 * 会让前端**每一次**都在服务端还在跑的时候就放弃，用户看到"超时"而后端其实会成功。
 */
const PLAN_TIMEOUT_MS = 20 * 60_000;

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type HttpMethod = "GET" | "POST";

interface RequestOptions<T> {
  /** 真实 HTTP 方法：错误详情必须说明「实际请求了什么」，不能一律写成 GET。 */
  method: HttpMethod;
  body?: string;
  /** 2xx 响应体的结构校验；不通过时按 invalid 抛出，而不是把残缺数据当成结果使用。 */
  validate?: (payload: unknown) => payload is T;
  /**
   * 覆盖默认的 404 语义。默认把 404 解释为「按 run_id 查询的结果不存在」；
   * createPlan 会传入自己的语义，因为 POST /api/v1/plans 返回 404 表示接口本身不存在，
   * 而不是「你要的行程不存在」。
   */
  notFound?: { kind: ApiErrorKind; message: string; note: string };
  /** 该接口自己的超时预算；不传则用默认的 TIMEOUT_MS。 */
  timeoutMs?: number;
}

async function request<T>(path: string, options: RequestOptions<T>): Promise<T> {
  const { method, body, validate, notFound } = options;
  const timeoutMs = options.timeoutMs ?? TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      body,
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
    if (response.status === 404) {
      throw new ApiError(
        notFound?.message ?? "未找到对应的规划结果",
        notFound?.kind ?? "not_found",
        notFound ? `${method} ${path} 返回 404：${notFound.note}` : `${method} ${path} 返回 404`,
      );
    }
    // 401 / 403 是权限问题，不是服务端故障：必须单独分类，否则会对用户说成「服务出错」。
    if (response.status === 401 || response.status === 403) {
      const text = await response.text().catch(() => "");
      throw new ApiError(
        "没有访问规划服务的权限",
        "unauthorized",
        `${method} ${path} 返回 ${response.status} ${text.slice(0, 200)}`,
      );
    }
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new ApiError("规划服务返回异常", "server", `${method} ${path} 返回 ${response.status} ${text.slice(0, 200)}`);
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ApiError("规划服务返回异常", "invalid", `${method} ${path} 返回 2xx，但响应体不是合法 JSON`);
    }
    if (validate && !validate(payload)) {
      throw new ApiError("规划服务返回异常", "invalid", `${method} ${path} 返回 2xx，但响应体结构不符合预期`);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("规划请求超时", "timeout", `${method} ${path} 超过 ${timeoutMs / 1000} 秒未返回`);
    }
    throw new ApiError("无法连接规划服务", "network", error instanceof Error ? error.message : String(error));
  } finally {
    clearTimeout(timer);
  }
}

/** 后端 2xx 响应的运行时结构校验：缺字段/类型不符都属于 invalid（服务端答了，但答得不合法）。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCreatePlanResult(value: unknown): value is CreatePlanResult {
  if (!isRecord(value)) return false;
  if (typeof value.run_id !== "string" || value.run_id.length === 0) return false;
  if (typeof value.status !== "string") return false;
  return value.plan === null || isRecord(value.plan);
}

function isRunProgress(value: unknown): value is RunProgress {
  if (!isRecord(value) || typeof value.run_id !== "string" || typeof value.status !== "string") return false;
  return Array.isArray(value.stages);
}

/**
 * 只校验「这份结果能不能渲染」：run_id / query / days 缺一不可。
 * intent、budget、warnings、decisions 等属于可选产出：降级路径下后端可能整块省略，
 * 若把它们也算作 invalid，用户会连已有的行程都看不到。缺失值由使用方按空值处理。
 */
function isTripPlan(value: unknown): value is TripPlan {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    value.run_id.length > 0 &&
    typeof value.query === "string" &&
    Array.isArray(value.days)
  );
}

function isAuditReport(value: unknown): value is AuditReport {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    typeof value.query === "string" &&
    typeof value.generated_at === "string" &&
    Array.isArray(value.stages) &&
    Array.isArray(value.decisions) &&
    Array.isArray(value.provider_calls) &&
    Array.isArray(value.timeline) &&
    Array.isArray(value.artifacts)
  );
}

function isReviseResult(value: unknown): value is ReviseResult {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    typeof value.status === "string" &&
    typeof value.message === "string"
  );
}

/** 把异常翻译成用户能看懂的解释，不暴露内部堆栈、接口地址或英文技术字段。 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.kind) {
      case "not_found":
        return "这个行程编号没有对应的规划结果。它可能已经过期，或者链接不完整。请返回首页重新生成一次。";
      case "endpoint_missing":
        return "找不到创建规划的接口。请确认后端已部署最新版本，或联系管理员检查服务地址。";
      case "unauthorized":
        return "规划服务拒绝了这次请求。请确认当前使用的是有权限的访问地址或凭据，然后重试。";
      case "network":
        return "暂时无法连接规划服务。请确认网络正常，或稍后重试。";
      case "timeout":
        return "规划服务超时未返回结果。行程数据量较大时可能发生，请稍后重试。";
      case "invalid":
        return "规划服务返回的数据不完整，因此本次无法展示行程。";
      case "server":
      default:
        return "规划服务在生成过程中出错，本次没有产出可用行程。请稍后重试。";
    }
  }
  return "发生了未预期的错误，本次无法展示行程。";
}

/** 供页面选择状态视图用：401/403 应渲染 Unauthorized，而不是通用的服务错误。 */
export function apiErrorKind(error: unknown): ApiErrorKind | null {
  return error instanceof ApiError ? error.kind : null;
}

export interface CreatePlanOptions {
  /** 规划过程回调。接 FastAPI 后由真实进度驱动，当前 mock 版为合理模拟。 */
  onProgress?: (event: PlanProgressEvent) => void;
  /**
   * 轮询期间的继续条件。页面卸载后返回 false 即可停止轮询，
   * 避免用户在离开「仍在生成中」页面后仍在后台反复请求 /status。
   */
  shouldContinue?: () => boolean;
}

/**
 * 规划步骤清单（FRONTEND_DESIGN §6）。
 * mock 与真实后端共用同一份步骤定义，进度事件按 stage_id 更新状态。
 */
export function createPlanningSteps(): PlanningStepState[] {
  return initialStepStates();
}

/**
 * 把一个进度事件合并进步骤清单。
 * 后端新增前端还不认识的 stage 时追加一行，而不是把事件丢掉：
 * 丢掉的后果是这一行永远停在「等待」，用户以为规划卡住了。
 */
export function mergeProgressEvent(
  steps: PlanningStepState[],
  event: PlanProgressEvent,
): PlanningStepState[] {
  const index = steps.findIndex((step) => step.id === event.step_id);
  if (index === -1) {
    return [
      ...steps,
      {
        id: event.step_id,
        label: event.label ?? stageLabel(event.step_id),
        description: event.description,
        status: event.status,
        facts: event.facts,
        unknown: true,
      },
    ];
  }
  const next = steps.slice();
  const current = next[index];
  next[index] = { ...current, status: event.status, facts: event.facts ?? current.facts };
  return next;
}

function toStepStatus(status: RunStageProgress["status"]): StepStatus {
  const statuses: Record<RunStageProgress["status"], StepStatus> = {
    WAITING: "waiting",
    RUNNING: "running",
    SUCCESS: "success",
    WARNING: "warning",
    FAILED: "error",
  };
  return statuses[status];
}

function progressFacts(stage: RunStageProgress): string[] {
  const facts = Object.entries(stage.facts ?? {}).map(([label, value]) => `${label}：${String(value)}`);
  return stage.message ? [stage.message, ...facts] : facts;
}

function emitProgress(progress: RunProgress, onProgress?: CreatePlanOptions["onProgress"]): void {
  for (const stage of progress.stages) {
    onProgress?.({
      step_id: stage.stage_id,
      status: toStepStatus(stage.status),
      facts: progressFacts(stage),
      label: stageLabel(stage.stage_id),
    });
  }
}

/** DEGRADED 时要能说清「哪一步降级了」，只给一个状态码对用户没有意义。 */
function collectDegradations(progress: RunProgress): string[] {
  return progress.stages
    .filter((stage) => stage.status === "WARNING" || stage.status === "FAILED")
    .map((stage) => {
      const detail = stage.message || progressFacts(stage).join("；");
      return detail ? `${stageLabel(stage.stage_id)}：${detail}` : stageLabel(stage.stage_id);
    });
}

const DONE_STATUSES: RunStatus[] = ["SUCCESS", "DEGRADED"];
const STOPPED_STATUSES: RunStatus[] = ["FAILED", "CANCELLED"];

/**
 * 轮询 run 状态直到终态（createPlan 与「仍在生成中」页面共用同一份逻辑）。
 *
 * FAILED / CANCELLED 返回结果而不是抛异常：调用方要按状态分别渲染，
 * 统一抛成 ApiError 只会让用户看到一句笼统的「服务出错」，看不出是失败还是被取消。
 */
export async function waitForRun(runId: string, options: CreatePlanOptions = {}): Promise<CreatePlanResult> {
  const started = Date.now();
  while (Date.now() - started < PLAN_TIMEOUT_MS) {
    if (options.shouldContinue && !options.shouldContinue()) {
      throw new ApiError("已停止查询规划状态", "timeout", `run_id=${runId} 的轮询被调用方终止`);
    }
    const progress: RunProgress = await getPlanStatus(runId);
    emitProgress(progress, options.onProgress);
    if (DONE_STATUSES.includes(progress.status)) {
      return {
        run_id: runId,
        status: progress.status,
        plan: await getPlan(runId),
        message: progress.message,
        degradations: progress.status === "DEGRADED" ? collectDegradations(progress) : undefined,
      };
    }
    if (STOPPED_STATUSES.includes(progress.status)) {
      return {
        run_id: runId,
        status: progress.status,
        plan: null,
        message: progress.error ?? progress.message,
        degradations: collectDegradations(progress),
      };
    }
    await delay(900);
  }
  throw new ApiError(
    "规划请求超时",
    "timeout",
    `run_id=${runId} 超过 ${PLAN_TIMEOUT_MS / 1000} 秒仍未结束`,
  );
}

export async function createPlan(message: string, options: CreatePlanOptions = {}): Promise<CreatePlanResult> {
  if (!USE_MOCK_API) {
    const payload = await request<CreatePlanResult>("/api/v1/plans", {
      method: "POST",
      body: JSON.stringify({ message, background: true }),
      validate: isCreatePlanResult,
      timeoutMs: PLAN_TIMEOUT_MS,
      notFound: {
        kind: "endpoint_missing",
        message: "找不到创建规划的接口",
        note: "创建接口不存在，通常是 API Base URL 配置错误或后端部署过期。",
      },
    });
    return waitForRun(payload.run_id, options);
  }

  for (const [stepId, result] of Object.entries(MOCK_STEP_RESULTS)) {
    options.onProgress?.({ step_id: stepId, status: "running", label: stageLabel(stepId) });
    await delay(MOCK_STEP_DELAY_MS[stepId] ?? 600);
    options.onProgress?.({
      step_id: stepId,
      status: result.status,
      facts: result.facts,
      label: stageLabel(stepId),
    });
  }

  const plan = clone(mockPlan);
  plan.query = message;
  return { run_id: plan.run_id, status: "SUCCESS", plan };
}

export async function getPlanStatus(id: string): Promise<RunProgress> {
  if (USE_MOCK_API) {
    throw new ApiError("演示模式没有运行状态接口", "invalid");
  }
  return request<RunProgress>(`/api/v1/plans/${encodeURIComponent(id)}/status`, {
    method: "GET",
    validate: isRunProgress,
  });
}

function isKnownMockRun(id: string): boolean {
  return id.startsWith("tp-") || id === "demo";
}

export async function getPlan(id: string): Promise<TripPlan> {
  if (!USE_MOCK_API) {
    return request<TripPlan>(`/api/v1/plans/${encodeURIComponent(id)}`, {
      method: "GET",
      validate: isTripPlan,
    });
  }
  await delay(320);
  if (!isKnownMockRun(id)) {
    throw new ApiError("未找到对应的规划结果", "not_found", `mock 中没有 run_id=${id}`);
  }
  const plan = clone(mockPlan);
  plan.run_id = id;
  return plan;
}

export async function getAudit(id: string): Promise<AuditReport> {
  if (!USE_MOCK_API) {
    return request<AuditReport>(`/api/v1/plans/${encodeURIComponent(id)}/audit`, {
      method: "GET",
      validate: isAuditReport,
    });
  }
  await delay(260);
  if (!isKnownMockRun(id)) {
    throw new ApiError("未找到对应的规划依据", "not_found", `mock 中没有 run_id=${id}`);
  }
  const audit = clone(mockAudit);
  audit.run_id = id;
  return audit;
}

/** 用户修改计划（FRONTEND_DESIGN §22）。v0 只提交请求，重新规划由后端完成。 */
export async function revisePlan(id: string, request_: ReviseRequest): Promise<ReviseResult> {
  if (!USE_MOCK_API) {
    return request<ReviseResult>(`/api/v1/plans/${encodeURIComponent(id)}/revise`, {
      method: "POST",
      body: JSON.stringify(request_),
      validate: isReviseResult,
    });
  }
  await delay(480);
  return {
    run_id: id,
    status: "queued",
    message: "修改请求已提交。重新规划由后端完成，本次演示不会改动页面上的行程。",
  };
}

export function getEvidence(plan: TripPlan | null, ids: string[]): Evidence[] {
  if (!plan?.evidence) return [];
  return plan.evidence.filter((entry) => ids.includes(entry.id));
}

/** 产物下载（plan.json / plan.md / audit_report.json）。mock 模式后端未接入，返回 null。 */
export function getArtifactUrl(id: string, filename: string): string | null {
  if (USE_MOCK_API) return null;
  return `${API_BASE_URL}/api/v1/plans/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(filename)}`;
}
