/**
 * 管理台 API 客户端。
 *
 * 与用户端 `lib/api.ts` 分开的原因：
 * 1) 管理接口全部要求 `Authorization: Bearer <token>`，Token 由操作员输入并只存在
 *    localStorage（key: travelplan_admin_token）。它**不能**放在 NEXT_PUBLIC_* 环境变量里，
 *    那会被打进浏览器 bundle。
 * 2) 管理台的失败语义更细：401 表示 Token 不对，503 表示服务端根本没配
 *    TRAVELPLAN_ADMIN_TOKEN。这两种必须能区分，否则操作员会去改一个改不对的地方。
 *
 * 所有响应都做运行时结构校验：后端答了但答得不合法时，页面显示真实错误，
 * 而不是渲染出一堆 undefined。
 */

import type {
  AdminBadcase,
  AdminBadcaseList,
  AdminBadcasePatch,
  AdminBenchmarkDetail,
  AdminBenchmarkLaunchRequest,
  AdminBenchmarkLaunchResult,
  AdminBenchmarkRunList,
  AdminConfig,
  AdminConfigOverride,
  AdminConfigPatch,
  AdminCostBreakdown,
  AdminEvolutionLaunchResult,
  AdminEvolutionOverview,
  AdminEvolutionRunDetail,
  AdminJevHealth,
  AdminLlmCall,
  AdminOverview,
  AdminPlanningEvent,
  AdminPlanningSessionDetail,
  AdminPlanningSessionList,
  AdminPlanningSessionSummary,
  AdminRecord,
  AdminRunDetail,
  AdminRunList,
  AdminRunMetrics,
  AdminRunSummary,
  AdminStageDetail,
  AdminStageProgress,
  AdminStageTokens,
  AdminTraceSpan,
} from "@/types/admin";

export const ADMIN_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** localStorage 键名：只放操作员手输的 Token，不放任何服务端密钥。 */
export const ADMIN_TOKEN_STORAGE_KEY = "travelplan_admin_token";

const ADMIN_TIMEOUT_MS = 30_000;
/** Benchmark / Evolution 是长任务，但 POST 只负责入队，所以给一个稍宽松的预算即可。 */
const ADMIN_LAUNCH_TIMEOUT_MS = 60_000;

/* ------------------------------ Token 存取 ------------------------------ */

/** 读取本地 Token；隐私模式下 localStorage 可能抛异常，此时按「未配置」处理。 */
export function readAdminToken(): string | null {
  try {
    const value = window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    return value && value.trim().length > 0 ? value.trim() : null;
  } catch {
    return null;
  }
}

export function writeAdminToken(token: string): void {
  try {
    window.localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token.trim());
  } catch {
    // 存不进去时页面会在下一次读取发现「未配置」，比抛异常更可控。
  }
  emitAdminTokenChange();
}

export function clearAdminToken(): void {
  try {
    window.localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
  } catch {
    // 同上：清理失败不应让整个管理台崩掉。
  }
  emitAdminTokenChange();
}

const tokenListeners = new Set<() => void>();

/**
 * 订阅本地 Token 变化。
 *
 * 配合 useSyncExternalStore 使用：服务端快照固定返回 null，浏览器读到真实值后
 * React 会安全地重渲染，既不会 hydration mismatch，也不需要「在 effect 里 setState」。
 */
export function subscribeAdminToken(listener: () => void): () => void {
  tokenListeners.add(listener);
  return () => {
    tokenListeners.delete(listener);
  };
}

export function getAdminTokenSnapshot(): string | null {
  return readAdminToken();
}

function emitAdminTokenChange(): void {
  for (const listener of tokenListeners) listener();
}

/* ------------------------------ 错误模型 ------------------------------ */

export type AdminErrorKind =
  | "unauthorized"
  | "unconfigured"
  | "not_found"
  | "network"
  | "timeout"
  | "server"
  | "invalid";

export class AdminApiError extends Error {
  readonly kind: AdminErrorKind;
  readonly status: number | null;
  readonly detail: string;

  constructor(message: string, kind: AdminErrorKind, detail: string, status: number | null = null) {
    super(message);
    this.name = "AdminApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }

  /** 这个错误是否说明「当前凭据/部署不可用」，页面应换成整页说明而不是「重试」。 */
  get isAuthIssue(): boolean {
    return this.kind === "unauthorized" || this.kind === "unconfigured";
  }
}

export function toAdminApiError(cause: unknown): AdminApiError {
  if (cause instanceof AdminApiError) return cause;
  return new AdminApiError(
    "管理接口调用失败",
    "network",
    cause instanceof Error ? cause.message : String(cause),
  );
}

/** 顺序与可读性优先：把错误翻译成操作员看得懂的一句话。 */
export function describeAdminError(error: AdminApiError): string {
  switch (error.kind) {
    case "unauthorized":
      return "管理 Token 无效或已过期。请确认后端 TRAVELPLAN_ADMIN_TOKEN 的取值，然后清除并重新输入。";
    case "unconfigured":
      return "服务端没有配置 TRAVELPLAN_ADMIN_TOKEN，因此管理接口整体不可用。这是后端部署问题，重试不会解决。";
    case "not_found":
      return "这个编号在服务端没有对应的记录，可能是数据已被清理。";
    case "timeout":
      return "管理接口超时未返回。后端可能正在处理长任务，可以稍后重试。";
    case "invalid":
      return "管理接口返回了不符合契约的数据结构，因此本次不渲染。";
    case "network":
      return `无法连接管理服务（${ADMIN_API_BASE_URL}）。请确认后端已启动，且 NEXT_PUBLIC_API_BASE_URL 指向它。`;
    case "server":
    default:
      return "管理服务返回了异常状态，本次请求没有结果。";
  }
}

/* ------------------------------ 请求执行 ------------------------------ */

type AdminMethod = "GET" | "POST" | "PATCH";

interface AdminRequestOptions<T> {
  method: AdminMethod;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** 2xx 响应体的运行时校验；不通过按 invalid 抛出。 */
  validate: (payload: unknown) => payload is T;
  timeoutMs?: number;
}

function buildUrl(path: string, query?: AdminRequestOptions<unknown>["query"]): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const suffix = search.toString();
  return `${ADMIN_API_BASE_URL}${path}${suffix ? `?${suffix}` : ""}`;
}

async function adminRequest<T>(path: string, options: AdminRequestOptions<T>): Promise<T> {
  const token = readAdminToken();
  if (!token) {
    throw new AdminApiError(
      "缺少管理 Token",
      "unauthorized",
      `本地未设置 ${ADMIN_TOKEN_STORAGE_KEY}，请求未发出`,
    );
  }

  const { method, body, validate } = options;
  const timeoutMs = options.timeoutMs ?? ADMIN_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const url = buildUrl(path, options.query);

  try {
    const response = await fetch(url, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
    });

    if (response.status === 401) {
      throw new AdminApiError(
        "管理 Token 未被接受",
        "unauthorized",
        `${method} ${url} 返回 401：Token 缺失或与后端配置不一致`,
        401,
      );
    }
    if (response.status === 503) {
      throw new AdminApiError(
        "服务端未配置管理 Token",
        "unconfigured",
        `${method} ${url} 返回 503：后端缺少 TRAVELPLAN_ADMIN_TOKEN，管理接口不可用`,
        503,
      );
    }
    if (response.status === 404) {
      throw new AdminApiError(
        "未找到对应的记录",
        "not_found",
        `${method} ${url} 返回 404`,
        404,
      );
    }
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new AdminApiError(
        "管理服务返回异常",
        "server",
        `${method} ${url} 返回 ${response.status} ${text.slice(0, 200)}`,
        response.status,
      );
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new AdminApiError(
        "管理服务返回异常",
        "invalid",
        `${method} ${url} 返回 2xx，但响应体不是合法 JSON`,
        response.status,
      );
    }
    if (!validate(payload)) {
      throw new AdminApiError(
        "管理服务返回异常",
        "invalid",
        `${method} ${url} 返回 2xx，但响应体结构不符合契约`,
        response.status,
      );
    }
    return payload;
  } catch (error) {
    if (error instanceof AdminApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new AdminApiError(
        "管理请求超时",
        "timeout",
        `${method} ${url} 超过 ${Math.round(timeoutMs / 1000)} 秒未返回`,
      );
    }
    throw new AdminApiError(
      "无法连接管理服务",
      "network",
      error instanceof Error ? error.message : String(error),
    );
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------ 运行时类型守卫 ------------------------------ */

function isRecord(value: unknown): value is AdminRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isArray(value: unknown): value is unknown[] {
  return Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

/** 分页信封字段（limit / offset / total）：必须是真数字，否则分页算术会失真。 */
function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * 统计类字段：数字，或 null / undefined（后端没统计出来）。
 * 为什么不要求"必须是数字"：一个 null 就会把整页变成错误页，
 * 而展示层本来就能把 null 渲染成「—」，这样损失的信息少得多。
 */
function isMetricCount(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  return typeof value === "number" && Number.isFinite(value);
}

function hasMetricCounts(record: AdminRecord, keys: readonly string[]): boolean {
  return keys.every((key) => isMetricCount(record[key]));
}

function toNumberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function toArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/**
 * run 列表行由 normalizeRunList 归一：只要 run_id / status 是字符串，
 * 其余字段缺失都降级成「未知／—」，不放大成整页 invalid。
 */
function normalizeRunSummary(value: AdminRecord): AdminRunSummary {
  return {
    run_id: String(value.run_id),
    status: String(value.status),
    original_query: toStringOrNull(value.original_query),
    created_at: toStringOrNull(value.created_at),
    finished_at: toStringOrNull(value.finished_at),
    started_at: toStringOrNull(value.started_at),
    duration_ms: toNumberOrNull(value.duration_ms),
    total_tokens: toNumberOrNull(value.total_tokens),
    llm_calls: toNumberOrNull(value.llm_calls),
    jev_calls: toNumberOrNull(value.jev_calls),
    tool_calls: toNumberOrNull(value.tool_calls),
    provider_failures: toNumberOrNull(value.provider_failures),
    badcase_count: toNumberOrNull(value.badcase_count),
    cost: toNumberOrNull(value.cost),
    source: toStringOrNull(value.source),
    source_session_id: toStringOrNull(value.source_session_id),
  };
}

function normalizeCostBreakdown(value: unknown): AdminCostBreakdown | null {
  if (!isRecord(value)) return null;
  return {
    input_tokens: toNumberOrNull(value.input_tokens),
    output_tokens: toNumberOrNull(value.output_tokens),
    cached_tokens: toNumberOrNull(value.cached_tokens),
    input_price_per_million: toNumberOrNull(value.input_price_per_million),
    output_price_per_million: toNumberOrNull(value.output_price_per_million),
    cached_price_per_million: toNumberOrNull(value.cached_price_per_million),
  };
}

/** metrics 允许整段缺失（benchmark 用例运行没有 usage）→ 归一成空心对象。 */
function normalizeRunMetrics(value: unknown): AdminRunMetrics {
  const record = isRecord(value) ? value : {};
  return {
    duration_ms: toNumberOrNull(record.duration_ms),
    input_tokens: toNumberOrNull(record.input_tokens),
    output_tokens: toNumberOrNull(record.output_tokens),
    cached_tokens: toNumberOrNull(record.cached_tokens),
    total_tokens: toNumberOrNull(record.total_tokens),
    llm_calls: toNumberOrNull(record.llm_calls),
    jev_calls: toNumberOrNull(record.jev_calls),
    tool_calls: toNumberOrNull(record.tool_calls),
    provider_failures: toNumberOrNull(record.provider_failures),
    badcase_count: toNumberOrNull(record.badcase_count),
    cost: toNumberOrNull(record.cost),
    cost_currency: toStringOrNull(record.cost_currency),
    cost_source: toStringOrNull(record.cost_source),
    cost_breakdown: normalizeCostBreakdown(record.cost_breakdown),
  };
}

function normalizeStageProgress(value: AdminRecord): AdminStageProgress {
  return {
    stage_id: String(value.stage_id ?? value.id ?? ""),
    title: toStringOrNull(value.title) ?? "",
    status: toStringOrNull(value.status) ?? "",
    message: toStringOrNull(value.message),
    facts: isRecord(value.facts) ? value.facts : null,
    started_at: toStringOrNull(value.started_at),
    finished_at: toStringOrNull(value.finished_at),
  };
}

function normalizeRunList(payload: unknown): AdminRunList {
  const record = isRecord(payload) ? payload : {};
  const items = toArray(record.items).filter(isRecord).map(normalizeRunSummary);
  return {
    items,
    limit: toNumberOrNull(record.limit) ?? items.length,
    offset: toNumberOrNull(record.offset) ?? 0,
    total: toNumberOrNull(record.total) ?? items.length,
  };
}

function isRunListPayload(value: unknown): value is AdminRecord {
  return isRecord(value) && Array.isArray(value.items);
}

function normalizeRunDetail(payload: unknown): AdminRunDetail {
  const record = isRecord(payload) ? payload : {};
  const rawRun = isRecord(record.run) ? record.run : {};
  const rawProgress = isRecord(record.progress) ? record.progress : {};
  return {
    run: {
      ...rawRun,
      ...normalizeRunSummary({
        ...rawRun,
        run_id: rawRun.run_id ?? "",
        status: rawRun.status ?? "UNKNOWN",
      }),
    },
    metrics: normalizeRunMetrics(record.metrics),
    progress: {
      run_id: toStringOrNull(rawProgress.run_id) ?? toStringOrNull(rawRun.run_id) ?? "",
      status: toStringOrNull(rawProgress.status) ?? "",
      message: toStringOrNull(rawProgress.message),
      stages: toArray(rawProgress.stages).filter(isRecord).map(normalizeStageProgress),
    },
    trace: toArray(record.trace).filter(isTraceSpan),
    decisions: toArray(record.decisions).filter(isRecord) as unknown as AdminRunDetail["decisions"],
    provider_calls: toArray(record.provider_calls).filter(isRecord) as unknown as AdminRunDetail["provider_calls"],
    jev_calls: toArray(record.jev_calls).filter(isRecord) as unknown as AdminRunDetail["jev_calls"],
    llm_calls: toArray(record.llm_calls).filter(isRecord) as unknown as AdminLlmCall[],
    badcases: toArray(record.badcases).filter(isBadcase),
    user_journey: isRecord(record.user_journey)
      ? (record.user_journey as AdminRunDetail["user_journey"])
      : null,
  };
}

function isRunDetailPayload(value: unknown): value is AdminRecord {
  return isRecord(value) && isRecord(value.run) && isString(value.run.run_id);
}

function normalizeStageDetail(value: AdminRecord): AdminStageDetail {
  return {
    stage_id: String(value.stage_id ?? value.id ?? ""),
    title: toStringOrNull(value.title),
    status: toStringOrNull(value.status),
    message: toStringOrNull(value.message),
    started_at: toStringOrNull(value.started_at),
    finished_at: toStringOrNull(value.finished_at),
    duration_ms: toNumberOrNull(value.duration_ms),
    steps: toArray(value.steps).filter(isString),
    facts: isRecord(value.facts) ? value.facts : null,
    spans: toArray(value.spans).filter(isTraceSpan),
    llm_calls: toArray(value.llm_calls).filter(isRecord) as unknown as AdminStageDetail["llm_calls"],
    tool_calls: toArray(value.tool_calls).filter(isRecord) as unknown as AdminStageDetail["tool_calls"],
    tokens: isRecord(value.tokens) ? (value.tokens as unknown as AdminStageTokens) : null,
  };
}

/**
 * 阶段接口的响应体兼容三种写法：裸数组、`{stages: [...]}`、单个阶段对象。
 * 契约只固定了「每个阶段」的形状，没有固定信封，因此这里全部接受。
 */
function normalizeStageList(payload: unknown): AdminStageDetail[] {
  if (Array.isArray(payload)) return payload.filter(isRecord).map(normalizeStageDetail);
  if (isRecord(payload)) {
    if (Array.isArray(payload.stages)) {
      return payload.stages.filter(isRecord).map(normalizeStageDetail);
    }
    if (isString(payload.stage_id)) return [normalizeStageDetail(payload)];
  }
  return [];
}

function isStagePayload(value: unknown): value is AdminRecord | unknown[] {
  return isRecord(value) || Array.isArray(value);
}

/* ------------------------------ 仪表盘 ------------------------------ */

function isOverview(value: unknown): value is AdminOverview {
  if (!isRecord(value)) return false;
  if (!hasMetricCounts(value, ["run_count", "degraded_count", "failed_count", "running_count"])) return false;
  if (!hasMetricCounts(value, ["total_tokens", "total_llm_calls", "total_jev_calls", "total_tool_calls"])) return false;
  if (!hasMetricCounts(value, ["provider_failures", "badcase_open", "badcase_total"])) return false;
  if (!isRecord(value.statuses)) return false;
  if (!isRecord(value.jev) || !isRecord(value.benchmark) || !isRecord(value.evolution)) return false;
  const benchmark = value.benchmark;
  return isArray(benchmark.latest_suites);
}

function isTraceSpan(value: unknown): value is AdminTraceSpan {
  if (!isRecord(value)) return false;
  return isString(value.span_id) && isString(value.name) && isString(value.component) && isString(value.status);
}

function isBadcase(value: unknown): value is AdminBadcase {
  if (!isRecord(value)) return false;
  if (!isString(value.badcase_id) || !isString(value.category) || !isString(value.severity)) return false;
  return isArray(value.trace_refs) && isString(value.analysis_status) && isString(value.fixed_status);
}

function isBadcaseList(value: unknown): value is AdminBadcaseList {
  if (!isRecord(value)) return false;
  if (!isArray(value.items) || !value.items.every(isBadcase)) return false;
  if (!isCount(value.total) || !isCount(value.limit) || !isCount(value.offset)) return false;
  return isRecord(value.facets);
}

function isBenchmarkRun(value: unknown): boolean {
  if (!isRecord(value)) return false;
  if (!isString(value.benchmark_run_id) || !isString(value.suite) || !isString(value.status)) return false;
  return typeof value.jev_enabled === "boolean" && isRecord(value.metrics);
}

function isBenchmarkRunList(value: unknown): value is AdminBenchmarkRunList {
  if (!isRecord(value)) return false;
  return isArray(value.items) && value.items.every(isBenchmarkRun) && isCount(value.limit);
}

const DIMENSION_KEYS = [
  "hard_constraints",
  "plan_quality",
  "evidence",
  "provider",
  "performance",
  "jev",
] as const;

function isBenchmarkDetail(value: unknown): value is AdminBenchmarkDetail {
  if (!isRecord(value)) return false;
  if (!isRecord(value.run) || !isString(value.run.benchmark_run_id)) return false;
  if (!isRecord(value.metrics)) return false;
  for (const key of DIMENSION_KEYS) {
    const dimension = value.metrics[key];
    // null / undefined 都算「这一维没数据」，交给页面显示 Partial，而不是整页失败。
    if (dimension !== null && dimension !== undefined && !isRecord(dimension)) return false;
  }
  if (!isArray(value.case_results)) return false;
  return (
    value.baseline_compare === null ||
    value.baseline_compare === undefined ||
    isRecord(value.baseline_compare)
  );
}

function isConfigPayload(value: unknown): value is AdminRecord {
  return isRecord(value);
}

/**
 * 配置归一：`values` / `editable_keys` / `runtime_overrides` 都是后端新加字段，
 * 缺了也照样返回一份能渲染的最小结构（旧后端返回 config/planner_tuning 时仍可用）。
 */
function normalizeConfig(payload: unknown): AdminConfig {
  const record = isRecord(payload) ? payload : {};
  const overrides: Record<string, AdminConfigOverride> = {};
  if (isRecord(record.runtime_overrides)) {
    for (const [key, raw] of Object.entries(record.runtime_overrides)) {
      if (isRecord(raw)) {
        overrides[key] = {
          value: raw.value,
          updated_at: toStringOrNull(raw.updated_at),
          updated_by: toStringOrNull(raw.updated_by),
        };
      }
    }
  }
  return {
    config: isRecord(record.config) ? record.config : {},
    planner_tuning: isRecord(record.planner_tuning) ? record.planner_tuning : {},
    secret_configured: isRecord(record.secret_configured)
      ? (record.secret_configured as Record<string, boolean>)
      : {},
    note: toStringOrNull(record.note) ?? "",
    editable_keys: toArray(record.editable_keys).filter(isString),
    runtime_overrides: overrides,
    values: isRecord(record.values) ? record.values : {},
  };
}

function isJevHealth(value: unknown): value is AdminJevHealth {
  if (!isRecord(value)) return false;
  return (
    typeof value.enabled === "boolean" &&
    typeof value.configured === "boolean" &&
    isString(value.status) &&
    isString(value.quota_source)
  );
}

/* --------------------------- 引导式会话 --------------------------- */

function normalizePlanningSessionSummary(value: AdminRecord): AdminPlanningSessionSummary {
  return {
    session_id: String(value.session_id),
    status: toStringOrNull(value.status),
    discovery_status: toStringOrNull(value.discovery_status),
    origin: toStringOrNull(value.origin),
    destination: toStringOrNull(value.destination),
    start_date: toStringOrNull(value.start_date),
    days: toNumberOrNull(value.days),
    travelers: toNumberOrNull(value.travelers),
    budget_total: toNumberOrNull(value.budget_total),
    pace: toStringOrNull(value.pace),
    transport_priority: toStringOrNull(value.transport_priority),
    hotel_priority: toStringOrNull(value.hotel_priority),
    must_count: toNumberOrNull(value.must_count),
    want_count: toNumberOrNull(value.want_count),
    reject_count: toNumberOrNull(value.reject_count),
    run_id: toStringOrNull(value.run_id),
    created_at: toStringOrNull(value.created_at),
    updated_at: toStringOrNull(value.updated_at),
    expires_at: toStringOrNull(value.expires_at),
  };
}

function normalizePlanningSessionList(payload: unknown): AdminPlanningSessionList {
  const record = isRecord(payload) ? payload : {};
  const items = toArray(record.items)
    .filter(isRecord)
    .filter((item) => isString(item.session_id))
    .map(normalizePlanningSessionSummary);
  return {
    items,
    limit: toNumberOrNull(record.limit) ?? items.length,
    offset: toNumberOrNull(record.offset) ?? 0,
    total: toNumberOrNull(record.total) ?? items.length,
  };
}

function isPlanningSessionListPayload(value: unknown): value is AdminRecord {
  return isRecord(value) && Array.isArray(value.items);
}

function normalizePlanningEvent(value: AdminRecord): AdminPlanningEvent {
  return {
    at: toStringOrNull(value.at),
    event: toStringOrNull(value.event),
    detail: toStringOrNull(value.detail),
  };
}

function normalizePlanningSessionDetail(payload: unknown): AdminPlanningSessionDetail {
  const record = isRecord(payload) ? payload : {};
  const base = normalizePlanningSessionSummary({
    ...record,
    session_id: record.session_id ?? "",
  });
  return {
    ...record,
    ...base,
    place_candidates: toArray(record.place_candidates).filter(isRecord),
    transport_candidates: toArray(record.transport_candidates).filter(isRecord),
    hotel_candidates: toArray(record.hotel_candidates).filter(isRecord),
    events: toArray(record.events).filter(isRecord).map(normalizePlanningEvent),
    degradations: toArray(record.degradations).filter(isString),
  };
}

function isPlanningSessionDetailPayload(value: unknown): value is AdminRecord {
  return isRecord(value) && isString(value.session_id);
}

function isEvolutionOverview(value: unknown): value is AdminEvolutionOverview {
  if (!isRecord(value)) return false;
  if (typeof value.enabled !== "boolean") return false;
  if (!isMetricCount(value.pending_badcases) || !isArray(value.runs)) return false;
  return value.runs.every(
    (run) => isRecord(run) && isString(run.evolution_run_id) && isString(run.status),
  );
}

function isEvolutionRunDetail(value: unknown): value is AdminEvolutionRunDetail {
  if (!isRecord(value)) return false;
  if (!isRecord(value.run) || !isString(value.run.evolution_run_id)) return false;
  if (!isArray(value.run.steps)) return false;
  if (!isArray(value.experiences) || !isArray(value.badcases)) return false;
  return value.experiences.every(
    (experience) =>
      isRecord(experience) &&
      isString(experience.experience_id) &&
      isString(experience.failure_pattern) &&
      isString(experience.decision),
  );
}

function isBenchmarkLaunchResult(value: unknown): value is AdminBenchmarkLaunchResult {
  return isRecord(value) && isString(value.benchmark_run_id) && isString(value.status);
}

function isEvolutionLaunchResult(value: unknown): value is AdminEvolutionLaunchResult {
  return isRecord(value) && isString(value.evolution_run_id) && isString(value.status);
}

/* ------------------------------ 端点 ------------------------------ */

export function getAdminOverview(): Promise<AdminOverview> {
  return adminRequest<AdminOverview>("/api/v1/admin/overview", { method: "GET", validate: isOverview });
}

export interface AdminRunsQuery {
  limit: number;
  offset: number;
  status?: string;
  /** run_id 搜索：精确或子串，后端负责匹配。 */
  q?: string;
  /** 是否包含 Benchmark 用例运行；默认 false（它们会淹没真实运行）。 */
  includeBenchmark?: boolean;
}

export function getAdminRuns(query: AdminRunsQuery): Promise<AdminRunList> {
  return adminRequest<AdminRecord>("/api/v1/admin/runs", {
    method: "GET",
    query: {
      limit: query.limit,
      offset: query.offset,
      status: query.status,
      q: query.q,
      // 只在需要时发送 true：默认 false 时省略参数，兼容不接受该参数的旧后端。
      include_benchmark: query.includeBenchmark ? "true" : undefined,
    },
    validate: isRunListPayload,
  }).then(normalizeRunList);
}

export function getAdminRun(runId: string): Promise<AdminRunDetail> {
  return adminRequest<AdminRecord>(`/api/v1/admin/runs/${encodeURIComponent(runId)}`, {
    method: "GET",
    validate: isRunDetailPayload,
  }).then(normalizeRunDetail);
}

/**
 * 阶段明细。响应体兼容裸数组 / `{stages}` / 单阶段对象，
 * 归一后始终返回数组，缺字段降级成空表。
 */
export function getAdminRunStages(runId: string): Promise<AdminStageDetail[]> {
  return adminRequest<AdminRecord | unknown[]>(
    `/api/v1/admin/runs/${encodeURIComponent(runId)}/stages`,
    { method: "GET", validate: isStagePayload },
  ).then(normalizeStageList);
}

export interface AdminPlanningSessionsQuery {
  limit: number;
  offset: number;
  /** session_id / run_id 搜索：后端支持时透传，前端再做一次兜底过滤。 */
  q?: string;
}

export function getAdminPlanningSessions(
  query: AdminPlanningSessionsQuery,
): Promise<AdminPlanningSessionList> {
  return adminRequest<AdminRecord>("/api/v1/admin/planning-sessions", {
    method: "GET",
    query: { limit: query.limit, offset: query.offset, q: query.q },
    validate: isPlanningSessionListPayload,
  }).then(normalizePlanningSessionList);
}

export function getAdminPlanningSession(sessionId: string): Promise<AdminPlanningSessionDetail> {
  return adminRequest<AdminRecord>(
    `/api/v1/admin/planning-sessions/${encodeURIComponent(sessionId)}`,
    { method: "GET", validate: isPlanningSessionDetailPayload },
  ).then(normalizePlanningSessionDetail);
}

export interface AdminBadcasesQuery {
  analysisStatus?: string;
  category?: string;
  severity?: string;
  runId?: string;
  limit: number;
  offset: number;
}

export function getAdminBadcases(query: AdminBadcasesQuery): Promise<AdminBadcaseList> {
  return adminRequest<AdminBadcaseList>("/api/v1/admin/badcases", {
    method: "GET",
    query: {
      analysis_status: query.analysisStatus,
      category: query.category,
      severity: query.severity,
      run_id: query.runId,
      limit: query.limit,
      offset: query.offset,
    },
    validate: isBadcaseList,
  });
}

export function patchAdminBadcase(badcaseId: string, patch: AdminBadcasePatch): Promise<AdminBadcase> {
  return adminRequest<AdminBadcase>(`/api/v1/admin/badcases/${encodeURIComponent(badcaseId)}`, {
    method: "PATCH",
    body: patch,
    validate: isBadcase,
  });
}

export function getAdminBenchmarkRuns(limit = 20): Promise<AdminBenchmarkRunList> {
  return adminRequest<AdminBenchmarkRunList>("/api/v1/admin/benchmark/runs", {
    method: "GET",
    query: { limit },
    validate: isBenchmarkRunList,
  });
}

export function getAdminBenchmarkRun(benchmarkRunId: string): Promise<AdminBenchmarkDetail> {
  return adminRequest<AdminBenchmarkDetail>(
    `/api/v1/admin/benchmark/runs/${encodeURIComponent(benchmarkRunId)}`,
    { method: "GET", validate: isBenchmarkDetail },
  );
}

export function launchAdminBenchmark(
  request: AdminBenchmarkLaunchRequest,
): Promise<AdminBenchmarkLaunchResult> {
  return adminRequest<AdminBenchmarkLaunchResult>("/api/v1/admin/benchmark/runs", {
    method: "POST",
    body: request,
    timeoutMs: ADMIN_LAUNCH_TIMEOUT_MS,
    validate: isBenchmarkLaunchResult,
  });
}

export function getAdminConfig(): Promise<AdminConfig> {
  return adminRequest<AdminRecord>("/api/v1/admin/config", {
    method: "GET",
    validate: isConfigPayload,
  }).then(normalizeConfig);
}

/** 保存非 Secret 的配置项；后端返回与 GET 相同的结构，前端据此刷新。 */
export function patchAdminConfig(patch: AdminConfigPatch): Promise<AdminConfig> {
  return adminRequest<AdminRecord>("/api/v1/admin/config", {
    method: "PATCH",
    body: patch,
    validate: isConfigPayload,
  }).then(normalizeConfig);
}

export function getAdminJevHealth(): Promise<AdminJevHealth> {
  return adminRequest<AdminJevHealth>("/api/v1/admin/jev/health", {
    method: "GET",
    validate: isJevHealth,
  });
}

export function getAdminEvolution(): Promise<AdminEvolutionOverview> {
  return adminRequest<AdminEvolutionOverview>("/api/v1/admin/evolution", {
    method: "GET",
    validate: isEvolutionOverview,
  });
}

export function getAdminEvolutionRun(evolutionRunId: string): Promise<AdminEvolutionRunDetail> {
  return adminRequest<AdminEvolutionRunDetail>(
    `/api/v1/admin/evolution/runs/${encodeURIComponent(evolutionRunId)}`,
    { method: "GET", validate: isEvolutionRunDetail },
  );
}

export function launchAdminEvolution(limit?: number): Promise<AdminEvolutionLaunchResult> {
  return adminRequest<AdminEvolutionLaunchResult>("/api/v1/admin/evolution/runs", {
    method: "POST",
    body: limit === undefined ? {} : { limit },
    timeoutMs: ADMIN_LAUNCH_TIMEOUT_MS,
    validate: isEvolutionLaunchResult,
  });
}
