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

import { API_BASE_URL, ApiError, apiErrorKind, USE_MOCK_API } from "@/lib/api";
import {
  DISCOVERY_STATUSES,
  PLANNING_SESSION_STATUSES,
  POI_SELECTIONS,
  type BasicIntent,
  type CreateSessionInput,
  type DiscoveryStatus,
  type DiscoveryStage,
  type SessionRecommendation,
  type EvidenceSummary,
  type DiscoveryProfile,
  type DiscoverySource,
  type HotelArea,
  type HotelCandidate,
  type PlaceCandidate,
  type PlaceCategory,
  type PlanningSessionStatus,
  type PoiSelection,
  type PoiPools,
  type PreferenceSentinel,
  type SessionCapabilities,
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

const SESSION_ERROR_MESSAGES: Record<string, string> = {
  recommendation_not_ready: "推荐还在生成中。可以先填写偏好，返回探索页等待或重试后再开始规划。",
  recommendation_unavailable: "本次没有可用推荐。请返回探索页重新生成推荐后再开始规划。",
  no_selected_places: "没有可安排的地点。请返回探索页保留至少一个推荐地点，或使用这份推荐。",
  invalid_place_selection: "所选地点已不在当前推荐中。请返回探索页重新选择，或使用这份推荐。",
};

class SessionRequestError extends ApiError {
  constructor(readonly code: string) {
    super(SESSION_ERROR_MESSAGES[code], "server");
  }
}

function sessionErrorCode(text: string): string | null {
  try {
    const payload: unknown = JSON.parse(text);
    const detail = isRecord(payload) ? payload.detail ?? payload : payload;
    const code = typeof detail === "string" ? detail : isRecord(detail) ? detail.code ?? detail.error : null;
    return typeof code === "string" && Object.hasOwn(SESSION_ERROR_MESSAGES, code) ? code : null;
  } catch {
    return Object.hasOwn(SESSION_ERROR_MESSAGES, text.trim()) ? text.trim() : null;
  }
}

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
      const code = sessionErrorCode(text);
      if (code) throw new SessionRequestError(code);
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
  if (error instanceof SessionRequestError) return error.message;
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

function stableIds(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string" && id.trim().length > 0) : [];
}

function normalizeRecommendation(value: unknown): SessionRecommendation | undefined {
  if (value === undefined || value === null) return undefined;
  const raw = isRecord(value) ? value : {};
  const status = asString(raw.status)?.toUpperCase();
  return {
    // 字段已出现但形状未知时不能回退到原始池，也不能放行正式规划。
    status: status === "READY" || status === "PARTIAL" || status === "FAILED" || status === "RUNNING" ? status : "PENDING",
    source: raw.source === "llm" || raw.source === "evidence_fallback" ? raw.source : null,
    version: asNumber(raw.version),
    place_ids: stableIds(raw.place_ids),
  };
}

function normalizeDiscovery(value: unknown): Record<string, DiscoveryStage> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([, stage]) => isRecord(stage)).map(([key, stage]) => {
    const info = stage as Record<string, unknown>;
    return [key, { status: asString(info.status)?.toUpperCase() ?? null, result_count: asNumber(info.result_count) }];
  }));
}

function normalizePlaceCandidates(value: unknown): PlaceCandidate[] {
  return asRecordArray(value)
    .map((item): PlaceCandidate | null => {
      const place_id = asString(item.place_id);
      const name = asString(item.name);
      if (!place_id?.trim() || !name) return null;
      return {
        place_id,
        name,
        display_name: asString(item.display_name),
        canonical_place_id: asString(item.canonical_place_id)?.trim() || null,
        merged_from: stableIds(item.merged_from),
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

function normalizeHotelAreas(value: unknown): HotelArea[] {
  return asRecordArray(value)
    .map((item): HotelArea | null => {
      const key = asString(item.key);
      const name = asString(item.name);
      if (!key || !name) return null;
      return {
        key,
        name,
        reason: asString(item.reason),
        tags: Array.isArray(item.tags) ? item.tags.filter((tag): tag is string => typeof tag === "string") : [],
        fit_score: asNumber(item.fit_score),
      };
    })
    .filter((item): item is HotelArea => item !== null);
}

function normalizePoiPools(value: unknown): PoiPools {
  const raw = isRecord(value) ? value : {};
  return {
    attraction: normalizePlaceCandidates(raw.attraction),
    food: normalizePlaceCandidates(raw.food),
    experience: normalizePlaceCandidates(raw.experience),
  };
}

function normalizeProfile(value: unknown): DiscoveryProfile | null {
  if (!isRecord(value)) return null;
  const dimension = (key: string): Record<string, unknown> | undefined => (isRecord(value[key]) ? value[key] : undefined);
  return {
    travel_style: asString(value.travel_style),
    hotel_area: dimension("hotel_area"),
    hotel: dimension("hotel"),
    attraction: dimension("attraction"),
    food: dimension("food"),
    pace: dimension("pace"),
  };
}

function normalizeDiscoverySource(value: unknown): DiscoverySource | null {
  return value === "live" || value === "city_cache" ? value : null;
}

// ---------------------------------------------------------------------------
// Guided 演示会话。正式 Run 的 mock 已在 lib/api.ts，这里补齐会话部分，
// 让 `NEXT_PUBLIC_USE_MOCK_API=true` 能实际走完首页 → Guided → 确认 → 结果页。
// 不会在 live 模式使用，也不会向真实服务发送模拟字段。
// ---------------------------------------------------------------------------

let mockSession: SessionView | null = null;
let mockDiscoveryReads = 0;

function cloneSession<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function mockSessionId(sessionId: string): SessionView {
  if (!mockSession || mockSession.session_id !== sessionId) {
    throw new ApiError("这个会话已经不存在了", "not_found", `mock 中没有 session_id=${sessionId}`);
  }
  return mockSession;
}

function createMockSession(input: CreateSessionInput): SessionView {
  mockDiscoveryReads = 0;
  const updatedAt = new Date(Date.now() - 3 * 86_400_000).toISOString();
  mockSession = normalizeSession({
    session_id: "guided-demo-session",
    status: "DISCOVERING",
    discovery_status: "DISCOVERING",
    expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
    basic_intent: input,
    preferences: {},
    poi_selections: {},
    transport_candidates: [
      { label: "G89 北京西 → 成都东", mode: "train", price: 864, duration_minutes: 471, reason: "时间合适且少换乘" },
      { label: "CA4102 北京首都 → 成都双流", mode: "flight", price: 720, duration_minutes: 185, reason: "早到可多玩半天" },
    ],
    hotel_candidates: [
      { name: "春熙路逸扉酒店", business_area: "春熙路", price_per_night: 468, rating: 4.7, reason: "步行可到商圈和地铁" },
      { name: "天府广场亚朵酒店", business_area: "天府广场", price_per_night: 428, rating: 4.6, reason: "去主要景点较均衡" },
    ],
    place_candidates: [
      { place_id: "panda-base", name: "成都大熊猫繁育研究基地", category: "attraction", area: "成华区", reason: "第一次来成都很值得安排在上午", evidence_count: 18, trust_score: 0.93, amap_verified: true },
      { place_id: "wuhou-shrine", name: "武侯祠", category: "attraction", area: "武侯区", reason: "历史文化和锦里可顺路安排", evidence_count: 14, trust_score: 0.91, amap_verified: true },
      { place_id: "kuanzhai", name: "宽窄巷子", category: "attraction", area: "青羊区", reason: "适合慢逛和拍照", evidence_count: 11, trust_score: 0.86, amap_verified: true },
      { place_id: "hotpot", name: "蜀大侠火锅", category: "food", area: "春熙路", reason: "多篇攻略提及毛肚和黄喉", evidence_count: 9, trust_score: 0.88, amap_verified: true },
      { place_id: "chuanchuan", name: "钢管厂五区小郡肝串串香", category: "food", area: "玉林", reason: "适合晚间安排的本地串串", evidence_count: 8, trust_score: 0.84, amap_verified: true },
      { place_id: "tea-house", name: "鹤鸣茶社", category: "experience", area: "人民公园", reason: "在公园里喝茶，适合留白", evidence_count: 7, trust_score: 0.82, amap_verified: true },
    ],
    place_categories: [
      { category: "attraction", label: "景点", count: 3 },
      { category: "food", label: "美食", count: 2 },
      { category: "experience", label: "体验", count: 1 },
    ],
    hotel_areas: [
      { key: "chunxi", name: "春熙路 / 太古里", reason: "晚间方便、餐饮密集、地铁连接好", tags: ["美食多", "商圈", "夜生活"], fit_score: 0.94 },
      { key: "tianfu", name: "天府广场", reason: "位置居中，去主要景点更均衡", tags: ["景点居中", "地铁方便"], fit_score: 0.86 },
      { key: "kuanzhai", name: "宽窄巷子", reason: "适合慢逛和偏安静的旅行节奏", tags: ["人文", "慢旅行"], fit_score: 0.77 },
    ],
    poi_pools: {
      attraction: [
        { place_id: "panda-base", name: "成都大熊猫繁育研究基地", category: "attraction", area: "成华区", reason: "第一次来成都很值得安排在上午", evidence_count: 18, trust_score: 0.93, amap_verified: true },
        { place_id: "wuhou-shrine", name: "武侯祠", category: "attraction", area: "武侯区", reason: "历史文化和锦里可顺路安排", evidence_count: 14, trust_score: 0.91, amap_verified: true },
      ],
      food: [
        { place_id: "hotpot", name: "蜀大侠火锅", category: "food", area: "春熙路", reason: "多篇攻略提及毛肚和黄喉", evidence_count: 9, trust_score: 0.88, amap_verified: true },
        { place_id: "chuanchuan", name: "钢管厂五区小郡肝串串香", category: "food", area: "玉林", reason: "适合晚间安排的本地串串", evidence_count: 8, trust_score: 0.84, amap_verified: true },
      ],
      experience: [
        { place_id: "tea-house", name: "鹤鸣茶社", category: "experience", area: "人民公园", reason: "在公园里喝茶，适合留白", evidence_count: 7, trust_score: 0.82, amap_verified: true },
      ],
    },
    profile: { travel_style: "food_commercial_centered", hotel_area: {}, hotel: {}, attraction: {}, food: {}, pace: {} },
    updated_at: updatedAt,
    source: "city_cache",
    evidence_summary: { sources_used: 26, places_verified: 6, total_candidates: 34 },
    events: [
      { event: "transport", detail: "找到 2 个交通候选" },
      { event: "hotels", detail: "找到 2 家酒店候选" },
      { event: "social", detail: "已阅读 26 篇旅行攻略" },
      { event: "places", detail: "已归类 6 个地点与美食候选" },
      { event: "hotel_areas", detail: "找到 3 个常被推荐的住宿区域" },
    ],
    capabilities: {},
  });
  return cloneSession(mockSession);
}

async function readMockSession(sessionId: string): Promise<SessionView> {
  const session = readMockSessionSync(sessionId);
  mockDiscoveryReads += 1;
  if (mockDiscoveryReads >= 2 && session.discovery_status === "DISCOVERING") {
    mockSession = { ...session, status: "READY", discovery_status: "READY" };
  }
  return cloneSession(mockSession ?? session);
}

function readMockSessionSync(sessionId: string): SessionView {
  return mockSessionId(sessionId);
}

async function patchMockSession(sessionId: string, patch: SessionPatchInput): Promise<SessionView> {
  const session = readMockSessionSync(sessionId);
  const preferences = { ...session.preferences };
  for (const key of [
    "transport_mode",
    "transport_priority",
    "transport_constraints",
    "hotel_priority",
    "hotel_max_price_per_night",
    "hotel_min_star",
    "hotel_room_type",
    "hotel_allow_change",
    "pace",
  ] as const) {
    if (key in patch) Object.assign(preferences, { [key]: patch[key] });
  }
  mockSession = {
    ...session,
    preferences,
    poi_selections: patch.poi_selections ? { ...patch.poi_selections } : session.poi_selections,
  };
  return cloneSession(mockSession);
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

/**
 * 能力开关归一：只认真正的布尔值，其余（缺失 / 字符串 / 对象）都当「未声明」= 不限制。
 * 缺字段时按「可用」处理，这样旧后端或创建接口的即时返回也不会把功能误关掉。
 *
 * 注意：`hotel_star_filter` / `hotel_allow_change` 仍然原样解析并保留 —— 它们是后端契约声明，
 * 前端界面已不再有对应的星级与换酒店选项，但字段本身仍描述服务端行为，不在这一层收口。
 */
function normalizeCapabilities(value: unknown): SessionCapabilities {
  if (!isRecord(value)) return {};
  return {
    hotel_star_filter:
      typeof value.hotel_star_filter === "boolean" ? value.hotel_star_filter : null,
    hotel_max_price_filter:
      typeof value.hotel_max_price_filter === "boolean" ? value.hotel_max_price_filter : null,
    // 白名单式归一化：新增能力字段必须在这里放行，否则会被静默丢掉，
    // 于是"后端说不可用"变成"前端以为可用"，又长出一个点了没用的按钮。
    hotel_allow_change:
      typeof value.hotel_allow_change === "boolean" ? value.hotel_allow_change : null,
    hotel_star_note: typeof value.hotel_star_note === "string" ? value.hotel_star_note : null,
    hotel_allow_change_note:
      typeof value.hotel_allow_change_note === "string" ? value.hotel_allow_change_note : null,
  };
}

/* --------------------- 数值型偏好的哨兵字符串收敛 --------------------- */

/** 引导式向导在数值字段上使用的三个显式取值。 */
const NUMERIC_SENTINELS = new Set(["unlimited", "undecided", "auto"]);

/**
 * 把数值型偏好的哨兵字符串收敛成 null。
 *
 * 后端 Pydantic 对 `budget_total` / `hotel_max_price_per_night` 声明的是 `float | None`，
 * 直接提交 "unlimited" / "undecided" / "auto" 会被判 422。这里统一收敛：
 *   - 数字原样保留；
 *   - 三个哨兵字符串 → null（等价于让后端走默认策略）；
 *   - null / undefined 原样保留（PATCH 时不改动该字段）。
 *
 * 收敛放在这个唯一的数据出口，而不是分散在各步组件里 —— 只要请求经过 sessions.ts，
 * 就不可能有哨兵字符串漏到后端。界面上的中文标签（不限 / 不确定 / 帮我选）来自本地草稿，
 * 因此不受影响。
 */
function coerceNumericPreference(value: unknown): number | null | undefined {
  if (value === null || value === undefined) return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && NUMERIC_SENTINELS.has(value)) return null;
  return null;
}

function sanitizeCreateInput(input: CreateSessionInput): CreateSessionInput {
  if (!("budget_total" in input)) return input;
  return { ...input, budget_total: coerceNumericPreference(input.budget_total) };
}

function sanitizePatchInput(patch: SessionPatchInput): SessionPatchInput {
  const next: SessionPatchInput = { ...patch };
  if ("budget_total" in next) {
    next.budget_total = coerceNumericPreference(next.budget_total);
  }
  if ("hotel_max_price_per_night" in next) {
    next.hotel_max_price_per_night = coerceNumericPreference(next.hotel_max_price_per_night);
  }
  return next;
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
    recommendation: normalizeRecommendation(raw.recommendation),
    discovery: normalizeDiscovery(raw.discovery),
    place_categories: normalizePlaceCategories(raw.place_categories),
    hotel_areas: normalizeHotelAreas(raw.hotel_areas),
    poi_pools: normalizePoiPools(raw.poi_pools),
    profile: normalizeProfile(raw.profile),
    updated_at: asString(raw.updated_at),
    source: normalizeDiscoverySource(raw.source),
    evidence_summary: normalizeEvidenceSummary(raw.evidence_summary),
    degradations: Array.isArray(raw.degradations)
      ? raw.degradations.filter((item): item is string => typeof item === "string")
      : [],
    events: normalizeEvents(raw.events),
    capabilities: normalizeCapabilities(raw.capabilities),
  };
}

/** 创建会话：这一步之后后端就开始后台 Prefetch，前端不必等用户选完偏好。 */
export async function createPlanningSession(input: CreateSessionInput): Promise<SessionView> {
  if (USE_MOCK_API) return createMockSession(input);
  const payload = await request("/api/v1/planning-sessions", {
    method: "POST",
    body: sanitizeCreateInput(input),
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
  if (USE_MOCK_API) return readMockSession(sessionId);
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
  if (USE_MOCK_API) return patchMockSession(sessionId, patch);
  const payload = await request(`/api/v1/planning-sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: sanitizePatchInput(patch),
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
  if (USE_MOCK_API) {
    const session = readMockSessionSync(sessionId);
    mockSession = { ...session, status: "STARTING" };
    return { run_id: "tp-guided-demo", status: "RUNNING" };
  }
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
  if (USE_MOCK_API) {
    const session = readMockSessionSync(sessionId);
    mockSession = { ...session, status: "CANCELLED" };
    return;
  }
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
  if (session.discovery_status === "FAILED" || session.recommendation?.status === "FAILED") return true;
  if (session.recommendation) {
    if (session.recommendation.status === "PENDING" || session.recommendation.status === "RUNNING") return false;
    if (["PENDING", "RUNNING", "DISCOVERING"].includes(session.discovery_status)) return false;
  }
  if (session.discovery_status === "READY" || session.discovery_status === "PARTIAL") {
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
  PENDING: "等待探索",
  RUNNING: "正在探索",
  DISCOVERING: "正在整理攻略",
  READY: "攻略已就绪",
  PARTIAL: "攻略部分就绪",
  FAILED: "攻略暂不可用",
};
