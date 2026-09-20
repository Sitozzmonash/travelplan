/**
 * Guided Pre-Planning（Planning Session）的接口契约。
 *
 * 这一层是「用户还在表达偏好」的临时会话，和正式 Run 的
 * RUNNING / SUCCESS / DEGRADED / FAILED / CANCELLED 是两套状态，
 * 前端在措辞与渲染上也必须分开，不能把 EXPIRED 说成「规划失败」。
 *
 * 「随便 / 帮我选 / 不确定」不是占位符：它们会被序列化成后端能识别的值
 * （`auto` / `undecided` / `unlimited` 或文档里已有的枚举），原样 PATCH 回后端。
 */

export const PLANNING_SESSION_STATUSES = [
  "COLLECTING",
  "DISCOVERING",
  "READY",
  "STARTING",
  "EXPIRED",
  "CANCELLED",
] as const;

/** Planning Session 自身的状态（与 RunStatus 无关）。 */
export type PlanningSessionStatus = (typeof PLANNING_SESSION_STATUSES)[number];

export const DISCOVERY_STATUSES = ["DISCOVERING", "READY", "PARTIAL", "FAILED"] as const;

/** Discovery 子状态：READY / PARTIAL 才算「选够了，可以给用户看」。 */
export type DiscoveryStatus = (typeof DISCOVERY_STATUSES)[number];

export const TRANSPORT_MODES = ["train", "flight", "any", "auto"] as const;
export type TransportMode = (typeof TRANSPORT_MODES)[number];

export const TRANSPORT_PRIORITIES = ["value", "fastest", "cheapest", "comfort", "auto"] as const;
export type TransportPriority = (typeof TRANSPORT_PRIORITIES)[number];

export const TRANSPORT_CONSTRAINTS = ["few_transfers", "no_early", "no_red_eye", "any_time"] as const;
export type TransportConstraint = (typeof TRANSPORT_CONSTRAINTS)[number];

export const HOTEL_PRIORITIES = ["value", "location", "rating", "comfort", "transit", "auto"] as const;
export type HotelPriority = (typeof HOTEL_PRIORITIES)[number];

export const PACES = ["relaxed", "balanced", "packed", "auto"] as const;
export type Pace = (typeof PACES)[number];

export const POI_SELECTIONS = ["MUST", "WANT", "REJECT"] as const;
export type PoiSelection = (typeof POI_SELECTIONS)[number];

/**
 * 「不限 / 不确定 / 帮我选」在数值与布尔字段上的显式取值。
 * 直接传字符串哨兵，而不是折叠成 null，否则三种语义会在后端被抹平成同一种。
 */
export type PreferenceSentinel = "auto" | "undecided" | "unlimited";

/** 数值型偏好：数字 = 明确上限；哨兵 = 不限 / 不确定 / 帮我选；null = 用户没表态（后端默认策略）。 */
export type NumericPreference = number | PreferenceSentinel | null;
/** 文本型偏好（房型等）。 */
export type StringPreference = string | PreferenceSentinel | null;
/** 布尔型偏好（是否接受换酒店）。 */
export type BooleanPreference = boolean | PreferenceSentinel | null;

export interface BasicIntent {
  origin: string;
  destination: string;
  start_date?: string | null;
  end_date?: string | null;
  days?: number | null;
  travelers?: number | null;
  budget_total?: number | PreferenceSentinel | null;
}

export interface SessionPreferences {
  transport_mode?: TransportMode | null;
  transport_priority?: TransportPriority | null;
  transport_constraints?: TransportConstraint[] | null;
  hotel_priority?: HotelPriority | null;
  hotel_max_price_per_night?: NumericPreference;
  hotel_min_star?: NumericPreference;
  hotel_room_type?: StringPreference;
  hotel_allow_change?: BooleanPreference;
  pace?: Pace | null;
}

export interface TransportCandidate {
  label: string;
  mode?: string | null;
  provider?: string | null;
  price?: number | null;
  currency?: string | null;
  departure_at?: string | null;
  arrival_at?: string | null;
  duration_minutes?: number | null;
  door_to_door_minutes?: number | null;
  score?: number | null;
  reason?: string | null;
}

export interface HotelCandidate {
  hotel_id?: string | null;
  name: string;
  price_per_night?: number | null;
  rating?: number | null;
  star?: number | null;
  business_area?: string | null;
  score?: number | null;
  reason?: string | null;
}

export interface PlaceCandidate {
  place_id: string;
  name: string;
  category?: string | null;
  category_label?: string | null;
  area?: string | null;
  district?: string | null;
  reason?: string | null;
  evidence_count?: number | null;
  trust_score?: number | null;
  ad_risk?: number | string | null;
  amap_verified?: boolean | null;
  selected?: PoiSelection | string | null;
}

export interface PlaceCategory {
  category: string;
  label?: string | null;
  count?: number | null;
}

export interface EvidenceSummary {
  sources_used?: number | null;
  places_verified?: number | null;
  total_candidates?: number | null;
}

export interface SessionEvent {
  at?: string | null;
  event?: string | null;
  detail?: string | null;
}

/** GET /api/v1/planning-sessions/{id} 的原始返回（字段可能缺失，必须能降级渲染）。 */
export interface PlanningSessionPayload {
  session_id?: string;
  status?: string;
  discovery_status?: string;
  expires_at?: string | null;
  run_id?: string | null;
  error?: string | null;
  basic_intent?: BasicIntent | null;
  preferences?: SessionPreferences | null;
  poi_selections?: Record<string, PoiSelection | string> | null;
  transport_candidates?: TransportCandidate[] | null;
  hotel_candidates?: HotelCandidate[] | null;
  place_candidates?: PlaceCandidate[] | null;
  place_categories?: PlaceCategory[] | null;
  evidence_summary?: EvidenceSummary | null;
  degradations?: string[] | null;
  events?: SessionEvent[] | null;
}

/** 归一化后的 Session：数组/对象字段一定有值，页面不需要再写防御代码。 */
export interface SessionView {
  session_id: string;
  status: PlanningSessionStatus;
  discovery_status: DiscoveryStatus;
  expires_at: string | null;
  run_id: string | null;
  error: string | null;
  basic_intent: BasicIntent | null;
  preferences: SessionPreferences;
  poi_selections: Record<string, PoiSelection>;
  transport_candidates: TransportCandidate[];
  hotel_candidates: HotelCandidate[];
  place_candidates: PlaceCandidate[];
  place_categories: PlaceCategory[];
  evidence_summary: EvidenceSummary | null;
  degradations: string[];
  events: SessionEvent[];
}

/** POST /api/v1/planning-sessions 的请求体。 */
export interface CreateSessionInput {
  origin: string;
  destination: string;
  start_date?: string | null;
  end_date?: string | null;
  days?: number | null;
  travelers?: number | null;
  budget_total?: number | PreferenceSentinel | null;
}

/** PATCH 请求体：任意子集，「改基础信息请重新 POST」由调用方保证。 */
export interface SessionPatchInput {
  transport_mode?: TransportMode;
  transport_priority?: TransportPriority;
  transport_constraints?: TransportConstraint[];
  hotel_priority?: HotelPriority;
  hotel_max_price_per_night?: NumericPreference;
  hotel_min_star?: NumericPreference;
  hotel_room_type?: StringPreference;
  hotel_allow_change?: BooleanPreference;
  pace?: Pace;
  budget_total?: number | PreferenceSentinel | null;
  poi_selections?: Record<string, PoiSelection>;
}

/** POST /api/v1/planning-sessions/{id}/start 的返回。 */
export interface StartSessionResult {
  run_id: string;
  status: string;
}
