/**
 * Planning Session 数据出口（唯一）。
 *
 * 页面组件不直接 fetch：Guided 向导的全部网络请求都经过这里，
 * 保证「同一份契约只解析一次、同一个错误只说一种话」。
 *
 * 契约（与后端同批实现）：
 *   POST   /api/v1/planning-sessions          创建会话 + 启动后台 Prefetch
 *   GET    /api/v1/planning-sessions/{id}     轮询 Discovery 状态
 *   PATCH  /api/v1/planning-sessions/{id}     更新结构化偏好（改基础信息请重新 POST）
 *   POST   /api/v1/planning-sessions/{id}/start  正式开跑，返回 run_id
 *   DELETE /api/v1/planning-sessions/{id}     用户主动取消（best-effort）
 */

import { API_BASE_URL, ApiError, apiErrorKind } from "@/lib/api";
import {
  DISCOVERY_STATUSES,
  PLANNING_SESSION_STATUSES,
  POI_SELECTIONS,
  type BasicIntent,
  type CreateSessionInput,
  type DiscoveryStatus,
  type EvidenceSummary,
  type HotelCandidate,
  type PlaceCandidate,
  type PlaceCategory,
  type PlanningSessionStatus,
  type PoiSelection,
  type PreferenceSentinel,
  type SessionEvent,
  type SessionPatchInput,
  type SessionPreferences,
  type SessionView,
  type StartSessionResult,
  type TransportCandidate,
} from "@/types/session";

const CREATE_TIMEOUT_MS = 45_000;
const READ_TIMEOUT_MS = 30_000;
const PATCH_TIMEOUT_MS = 30_000;
const START_TIMEOUT_MS = 90_000;

export { API_BASE_URL };

type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

interface RequestOptions {
  method: HttpMethod;
  body?: unknown;
  timeoutMs?: number;
  /** 404 的语义由调用方决定：创建接口的 404 = 后端没这个路由，查询接口的 404 = 会话不存在或已过期。 */
  notFound: { kind: "endpoint_missing" | "not_found"; message: string; note: string };
}

async function request(path: string, options: RequestOptions): Promise<unknown> {
  const { method, body, notFound } = options;
  const timeoutMs = options.timeoutMs ?? READ_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
    if (response.status === 404) {
      throw new ApiError(notFound.message, notFound.kind, `${method} ${path} 返回 404：${notFound.note}`);
    }
    if (response.status === 401 || response.status === 403) {
      const text = await response.text().catch(() => "");
      throw new ApiError(
        "没有访问会话服务的权限",
        "unauthorized",
        `${method} ${path} 返回 ${response.status} ${text.slice(0, 200)}`,
      );
    }
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new ApiError(
        "会话服务返回异常",
        "server",
        `${method} ${path} 返回 ${response.status} ${text.slice(0, 200)}`,
      );
    }
    if (response.status === 204) return null;
    const text = await response.text();
    if (!text.trim()) return null;
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new ApiError("会话服务返回异常", "invalid", `${method} ${path} 返回 2xx，但响应体不是合法 JSON`);
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("会话请求超时", "timeout", `${method} ${path} 超过 ${timeoutMs / 1000} 秒未返回`);
    }
    throw new ApiError("无法连接会话服务", "network", error instanceof Error ? error.message : String(error));
  } finally {
    clearTimeout(timer);
  }
}

/** 把异常翻译成向导能直接用的一句话，不暴露内部堆栈，也不把「过期」说成「失败」。 */
export function describeSessionError(error: unknown): string {
  const kind = apiErrorKind(error);
  switch (kind) {
    case "endpoint_missing":
      return `找不到会话接口。请确认后端已部署 Guided Planning Session（${API_BASE_URL}）。`;
    case "not_found":
      return "这个会话已经不存在了。它可能已过期，请重新开始一次偏好选择。";
    case "unauthorized":
      return "会话服务拒绝了这次请求（401 / 403）。请确认当前环境使用的是有权限的访问地址。";
    case "network":
      return `暂时无法连接会话服务（${API_BASE_URL}）。请确认后端已启动，或稍后重试。`;
    case "timeout":
      return "会话服务超时未返回。攻略与交通抓取较慢时可能发生，可以稍后重试。";
    case "invalid":
      return "会话服务返回的数据不完整，本次无法展示这一步的内容。可以重试或先按「随便」继续。";
    default:
      return "会话服务出错了，这一步的内容暂时没有加载出来。可以重试，或直接按「随便，帮我选」继续。";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord);
}

function normalizeSessionStatus(value: unknown): PlanningSessionStatus {
  const raw = typeof value === "string" ? value.toUpperCase() : "";
  const found = PLANNING_SESSION_STATUSES.find((item) => item === raw);
  // 认不出的状态按「还在收集」处理：保留轮询能力，由前端超时兜底，而不是直接告诉用户失败。
  return found ?? "COLLECTING";
}

function normalizeDiscoveryStatus(value: unknown): DiscoveryStatus {
  const raw = typeof value === "string" ? value.toUpperCase() : "";
  const found = DISCOVERY_STATUSES.find((item) => item === raw);
  return found ?? "DISCOVERING";
}

function normalizePoiSelection(value: unknown): PoiSelection | null {
  const raw = typeof value === "string" ? value.toUpperCase() : "";
  return POI_SELECTIONS.find((item) => item === raw) ?? null;
}

function normalizeBudget(value: unknown): number | PreferenceSentinel | null {
  const numeric = asNumber(value);
  if (numeric !== null) return numeric;
  if (value === "auto" || value === "undecided" || value === "unlimited") return value;
  return null;
}

function normalizeBasicIntent(value: unknown): BasicIntent | null {
  if (!isRecord(value)) return null;
  const origin = asString(value.origin);
  const destination = asString(value.destination);
  if (!origin || !destination) return null;
  return {
    origin,
    destination,
    start_date: asString(value.start_date),
    end_date: asString(value.end_date),
    days: asNumber(value.days),
    travelers: asNumber(value.travelers),
    budget_total: normalizeBudget(value.budget_total),
  };
}

function normalizePreferences(value: unknown): SessionPreferences {
  if (!isRecord(value)) return {};
  return value as SessionPreferences;
}

function normalizeTransportCandidates(value: unknown): TransportCandidate[] {
  return asRecordArray(value).map((item) => ({
    label: asString(item.label) ?? "未命名班次",
    mode: asString(item.mode),
    provider: asString(item.provider),
    price: asNumber(item.price),
    currency: asString(item.currency),
    departure_at: asString(item.departure_at),
    arrival_at: asString(item.arrival_at),
    duration_minutes: asNumber(item.duration_minutes),
    door_to_door_minutes: asNumber(item.door_to_door_minutes),
    score: asNumber(item.score),
    reason: asString(item.reason),
  }));
}

function normalizeHotelCandidates(value: unknown): HotelCandidate[] {
  return asRecordArray(value).map((item) => ({
    hotel_id: asString(item.hotel_id),
    name: asString(item.name) ?? "未命名酒店",
    price_per_night: asNumber(item.price_per_night),
    rating: asNumber(item.rating),
    star: asNumber(item.star),
    business_area: asString(item.business_area),
    score: asNumber(item.score),
    reason: asString(item.reason),
  }));
}

function normalizePlaceCandidates(value: unknown): PlaceCandidate[] {
  return asRecordArray(value)
    .map((item): PlaceCandidate | null => {
      const place_id = asString(item.place_id);
      const name = asString(item.name);
      if (!place_id || !name) return null;
      return {
        place_id,
        name,
        category: asString(item.category),
        category_label: asString(item.category_label),
        area: asString(item.area),
        district: asString(item.district),
        reason: asString(item.reason),
        evidence_count: asNumber(item.evidence_count),
        trust_score: asNumber(item.trust_score),
        ad_risk: typeof item.ad_risk === "string" ? item.ad_risk : asNumber(item.ad_risk),
        amap_verified: typeof item.amap_verified === "boolean" ? item.amap_verified : null,
        selected: normalizePoiSelection(item.selected),
      };
    })
    .filter((item): item is PlaceCandidate => item !== null);
}

function normalizePlaceCategories(value: unknown): PlaceCategory[] {
  return asRecordArray(value)
    .map((item): PlaceCategory | null => {
      const category = asString(item.category);
      if (!category) return null;
      return {
        category,
        label: asString(item.label),
        count: asNumber(item.count),
      };
    })
    .filter((item): item is PlaceCategory => item !== null);
}

function normalizeEvidenceSummary(value: unknown): EvidenceSummary | null {
  if (!isRecord(value)) return null;
  return {
    sources_used: asNumber(value.sources_used),
    places_verified: asNumber(value.places_verified),
    total_candidates: asNumber(value.total_candidates),
  };
}

function normalizeEvents(value: unknown): SessionEvent[] {
  return asRecordArray(value).map((item) => ({
    at: asString(item.at),
    event: asString(item.event),
    detail: asString(item.detail),
  }));
}

/** 任意返回体 → 一定能渲染的 SessionView（缺字段降级为空态，而不是抛给错误边界）。 */
export function normalizeSession(payload: unknown): SessionView {
  const raw = isRecord(payload) ? payload : {};
  const sessionId = asString(raw.session_id);
  if (!sessionId) {
    throw new ApiError("会话服务返回异常", "invalid", "返回体缺少 session_id，无法继续向导");
  }
  const poiSelections: Record<string, PoiSelection> = {};
  if (isRecord(raw.poi_selections)) {
    for (const [key, value] of Object.entries(raw.poi_selections)) {
      const state = normalizePoiSelection(value);
      if (state) poiSelections[key] = state;
    }
  }
  return {
    session_id: sessionId,
    status: normalizeSessionStatus(raw.status),
    discovery_status: normalizeDiscoveryStatus(raw.discovery_status),
    expires_at: asString(raw.expires_at),
    run_id: asString(raw.run_id),
    error: typeof raw.error === "string" ? raw.error : null,
    basic_intent: normalizeBasicIntent(raw.basic_intent),
    preferences: normalizePreferences(raw.preferences),
    poi_selections: poiSelections,
    transport_candidates: normalizeTransportCandidates(raw.transport_candidates),
    hotel_candidates: normalizeHotelCandidates(raw.hotel_candidates),
    place_candidates: normalizePlaceCandidates(raw.place_candidates),
    place_categories: normalizePlaceCategories(raw.place_categories),
    evidence_summary: normalizeEvidenceSummary(raw.evidence_summary),
    degradations: Array.isArray(raw.degradations)
      ? raw.degradations.filter((item): item is string => typeof item === "string")
      : [],
    events: normalizeEvents(raw.events),
  };
}

/** 创建会话：这一步之后后端就开始后台 Prefetch，前端不必等用户选完偏好。 */
export async function createPlanningSession(input: CreateSessionInput): Promise<SessionView> {
  const payload = await request("/api/v1/planning-sessions", {
    method: "POST",
    body: input,
    timeoutMs: CREATE_TIMEOUT_MS,
    notFound: {
      kind: "endpoint_missing",
      message: "找不到创建会话的接口",
      note: "POST /api/v1/planning-sessions 不存在，通常是后端未部署 Guided Planning Session，或 API Base URL 配置错误。",
    },
  });
  return normalizeSession(payload);
}

export async function getPlanningSession(sessionId: string): Promise<SessionView> {
  const payload = await request(`/api/v1/planning-sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
    timeoutMs: READ_TIMEOUT_MS,
    notFound: {
      kind: "not_found",
      message: "这个会话已经不存在了",
      note: "会话可能已过期（TTL 到期）或被取消。",
    },
  });
  return normalizeSession(payload);
}

export async function patchPlanningSession(
  sessionId: string,
  patch: SessionPatchInput,
): Promise<SessionView> {
  const payload = await request(`/api/v1/planning-sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: patch,
    timeoutMs: PATCH_TIMEOUT_MS,
    notFound: {
      kind: "not_found",
      message: "这个会话已经不存在了",
      note: "偏好没能写回后端：会话可能已过期（TTL 到期）或被取消。",
    },
  });
  return normalizeSession(payload);
}

export async function startPlanningSession(sessionId: string): Promise<StartSessionResult> {
  const payload = await request(`/api/v1/planning-sessions/${encodeURIComponent(sessionId)}/start`, {
    method: "POST",
    body: {},
    timeoutMs: START_TIMEOUT_MS,
    notFound: {
      kind: "not_found",
      message: "这个会话已经不存在了",
      note: "无法开始规划：会话可能已过期（TTL 到期）或被取消。",
    },
  });
  const record = isRecord(payload) ? payload : {};
  const runId = asString(record.run_id);
  if (!runId) {
    throw new ApiError("会话服务返回异常", "invalid", "start 返回体缺少 run_id");
  }
  return { run_id: runId, status: asString(record.status) ?? "RUNNING" };
}

/** 用户主动取消 / 重建前清理旧会话。取消失败不影响前端继续（旧会话会自己 TTL 过期）。 */
export async function cancelPlanningSession(sessionId: string): Promise<void> {
  try {
    await request(`/api/v1/planning-sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
      timeoutMs: READ_TIMEOUT_MS,
      notFound: { kind: "not_found", message: "会话已不存在", note: "无需再取消。" },
    });
  } catch {
    // best-effort：不把取消失败暴露成用户问题。
  }
}

/** Discovery 是否已经「说到能看的程度」：READY / PARTIAL / FAILED 都可以停轮询并展示 POI 区。 */
export function isDiscoverySettled(session: SessionView | null): boolean {
  if (!session) return false;
  if (
    session.discovery_status === "READY" ||
    session.discovery_status === "PARTIAL" ||
    session.discovery_status === "FAILED"
  ) {
    return true;
  }
  return session.status === "READY" || session.status === "STARTING";
}

/** 会话不能再用了：过期 / 被取消。 */
export function isSessionUnusable(session: SessionView | null): boolean {
  return session?.status === "EXPIRED" || session?.status === "CANCELLED";
}

/**
 * 社交源降级判定（§17 Partial）。
 * 文案只做提示，不阻塞：POI 页依旧可用。
 */
export function hasSocialDegradation(degradations: string[]): boolean {
  return degradations.some((item) => /social|xiaohongshu|小红书|抖音|tikhub|mediacrawler|攻略|笔记/i.test(item));
}

export const SESSION_STATUS_LABELS: Record<PlanningSessionStatus, string> = {
  COLLECTING: "正在收集偏好",
  DISCOVERING: "正在整理攻略",
  READY: "可以开始规划",
  STARTING: "正在启动规划",
  EXPIRED: "会话已过期",
  CANCELLED: "会话已取消",
};

export const DISCOVERY_STATUS_LABELS: Record<DiscoveryStatus, string> = {
  DISCOVERING: "正在整理攻略",
  READY: "攻略已就绪",
  PARTIAL: "攻略部分就绪",
  FAILED: "攻略暂不可用",
};
