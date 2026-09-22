/**
 * TravelPlan 数据模型（对齐 docs/product/PRD.md §28 plan.json 与 app/models.py）。
 *
 * 前端只消费这些结构，不做任何业务判断：Trust / Ad Risk / Feasibility /
 * 预算与最终方案都由后端计算（FRONTEND_DESIGN §34）。
 */

export type ItemType =
  | "attraction"
  | "food"
  | "hotel"
  | "transport"
  | "activity"
  | "free_time";

export type PriceType = "realtime" | "estimated" | "unknown";

export type BudgetStatus = "within_budget" | "over_budget" | "unknown";

export type Severity = "error" | "warning";

/** Provider 调用状态（PRD §32）：DEGRADED 表示降级但可用。 */
export type ProviderStatus = "OK" | "DEGRADED" | "FAILED" | "SKIPPED";

export interface TripIntent {
  origin: string | null;
  destination: string[];
  start_date: string | null;
  end_date: string | null;
  days: number;
  travelers: number;
  budget_total: number | null;
  preferences: string[];
  pace: string;
  transport_preferences?: string[];
  hotel_preferences?: string[];
  constraints?: string[];
}

/** 任何实时数据都必须能回答「谁、什么时候、通过哪个链接给的」。 */
export interface Provenance {
  provider: string;
  source_id?: string | null;
  source_url?: string | null;
  fetched_at?: string | null;
}

export interface FlightOption extends Provenance {
  kind: "flight";
  flight_no: string;
  airline?: string | null;
  origin?: string | null;
  destination?: string | null;
  departure_airport?: string | null;
  arrival_airport?: string | null;
  departure_at?: string | null;
  arrival_at?: string | null;
  duration_minutes?: number | null;
  price?: number | null;
  currency?: string;
  is_direct?: boolean | null;
  remaining_seats?: number | null;
  price_note?: string | null;
  /** 后端补充字段：机场往返所需分钟数（用于门到门对比）。 */
  airport_transfer_minutes?: number | null;
  door_to_door_minutes?: number | null;
  selection_reason?: string | null;
}

export interface TrainOption extends Provenance {
  kind: "train";
  train_no: string;
  origin_station?: string | null;
  destination_station?: string | null;
  departure_at?: string | null;
  arrival_at?: string | null;
  duration_minutes?: number | null;
  /** 席别 → 余票描述或数量 */
  seats?: Record<string, number | string | null>;
  /** 席别 → 票价 */
  price_range?: Record<string, number | null>;
  currency?: string;
  train_type?: string | null;
  price?: number | null;
  price_note?: string | null;
  airport_transfer_minutes?: number | null;
  door_to_door_minutes?: number | null;
  selection_reason?: string | null;
}

export type TransportOption = FlightOption | TrainOption;

export interface TransportPlan {
  selected: TransportOption | null;
  alternatives: TransportOption[];
  inbound_selected: TransportOption | null;
  inbound_alternatives: TransportOption[];
  selection_reason: string;
  door_to_door_minutes?: number | null;
  provider_status?: Record<string, ProviderStatus | string>;
}

export interface HotelOption extends Provenance {
  hotel_id?: string | null;
  name: string;
  address?: string | null;
  lat?: number | null;
  lng?: number | null;
  check_in?: string | null;
  check_out?: string | null;
  room_type?: string | null;
  price_per_night?: number | null;
  total_price?: number | null;
  rating?: number | null;
  cancellation_policy?: string | null;
  price_note?: string | null;
  city?: string | null;
  business_area?: string | null;
  /** 距当天主要区域的文字描述，例如「步行 12 分钟」。 */
  distance_to_area?: string | null;
  trust_score?: number | null;
  nights?: number | null;
  selection_reason?: string | null;
}

export interface HotelPlan {
  selected: HotelOption | null;
  alternatives: HotelOption[];
  selection_reason: string;
}

export interface TravelLeg {
  mode: string;
  distance_meters?: number | null;
  duration_seconds?: number | null;
  estimated_cost?: number | null;
  verified: boolean;
  estimated?: boolean;
  from_place_id?: string | null;
  to_place_id?: string | null;
  provider?: string | null;
  /** 未取得路线时的说明，用于展示降级原因。 */
  note?: string | null;
}

export interface ItineraryItem {
  id: string;
  type: ItemType;
  place_id?: string | null;
  name: string;
  lat?: number | null;
  lng?: number | null;
  start_time?: string | null;
  end_time?: string | null;
  duration_minutes?: number | null;
  travel_from_previous?: TravelLeg | null;
  price?: number | null;
  price_type: PriceType;
  trust_score?: number | null;
  ad_risk?: number | null;
  reason: string;
  /** 推荐原因拆解（Evidence Drawer 的 ✓ 列表），由后端给出。 */
  reason_points?: string[];
  /** 营业时间（若有），由后端在 POI 校验时带回；缺失时前端不展示该行。 */
  opening_hours?: string | null;
  /** 风险说明（Evidence Drawer 的风险段），由后端给出。 */
  risk_note?: string;
  evidence_ids: string[];
  source_ids: string[];
  transport_mode?: string | null;
  area?: string | null;
  booking_note?: string | null;
}

export interface ItineraryDay {
  day_index: number;
  date: string | null;
  area?: string | null;
  items: ItineraryItem[];
  notes?: string[];
}

export interface BudgetSummary {
  budget_total: number | null;
  known_real_cost: number;
  estimated_cost: number;
  projected_total: number;
  remaining: number | null;
  status: BudgetStatus;
  breakdown: Record<string, number>;
  breakdown_price_type: Record<string, PriceType | string>;
  /** 优化建议由后端给出，前端只展示。 */
  optimization_suggestions: string[];
  price_notes: string[];
}

/** 可行性 / 数据完整性问题（PRD §22、FRONTEND_DESIGN §18）。 */
export interface Warning {
  day_index: number;
  item_id?: string | null;
  severity: Severity;
  code: string;
  reason: string;
  original_start?: string | null;
  revised_start?: string | null;
  resolved: boolean;
}

export type FeasibilityIssue = Warning;

export interface Evidence extends Provenance {
  id: string;
  source_type: string;
  title: string;
  author?: string | null;
  published_at?: string | null;
  text: string;
  place_mentions: string[];
  specific_dishes: string[];
  raw_metrics?: Record<string, number | string | null>;
}

export interface SourceRef {
  source_id: string;
  provider: string;
  source_type: string;
  source_url?: string | null;
  title: string;
  query: Record<string, unknown>;
  fetched_at?: string | null;
  status: ProviderStatus | string;
}

export type DecisionStatus =
  | "KEEP"
  | "REJECT"
  | "SELECT"
  | "PASS"
  | "USED"
  | "NOT_USED";

export interface Decision {
  entity_id: string;
  status: DecisionStatus;
  agent_or_stage: string;
  reason_codes: string[];
  reason_text: string;
  scores?: Record<string, number | string | null>;
  timestamp?: string | null;
}

export interface TripPlan {
  run_id: string;
  query: string;
  /**
   * 以下四项在降级路径下可能整体缺失（后端尚未产出或产出失败），
   * 因此它们是可选的：使用方必须按空值处理，而不是假定一定存在。
   */
  intent?: TripIntent;
  transport: TransportPlan | null;
  hotel: HotelPlan | null;
  days: ItineraryDay[];
  budget?: BudgetSummary;
  warnings?: Warning[];
  sources: SourceRef[];
  generated_at: string;
  decisions?: Decision[];
  /**
   * 证据明细（Evidence Store）。后端可随 plan.json 一起返回，
   * 前端只读取用于 Evidence Drawer，不做任何可信度计算。
   */
  evidence?: Evidence[];
  /** 可信度汇总（FRONTEND_DESIGN §9），由后端统计，前端只展示。 */
  evidence_summary?: EvidenceSummary;
}

export interface EvidenceSummary {
  /** 本次规划实际使用的来源条数 */
  sources_used: number;
  /** 通过高德校验的地点数量 */
  places_verified: number;
  /** 因可信度不足被过滤的候选数量 */
  low_trust_filtered: number;
}

/** FRONTEND_DESIGN §14：地图组件接口，未来接高德 JS SDK 时无需改页面。 */
export interface MapPoint {
  id: string;
  name: string;
  lat: number;
  lng: number;
  order: number;
}
