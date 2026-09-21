/**
 * Guided 向导的本地草稿模型与「草稿 → 后端请求体」的转换。
 *
 * 设计原则：
 * 1. 用户没表态（null）≠「不限」≠「不确定」≠「帮我选」，四者在提交时必须可区分；
 *    只是没表态的字段会从 PATCH 里省略，让后端自己的默认策略生效。
 * 2. 任何一步都能用「随便 / 帮我选」走完：未表态时按文档默认（都可以 + 帮我选）提交，
 *    不阻塞、也不在前端假装后端已经知道。
 */

import { formatCNY, formatDateShort } from "@/lib/format";
import type {
  BooleanPreference,
  CreateSessionInput,
  NumericPreference,
  Pace,
  PlaceCandidate,
  PoiSelection,
  PreferenceSentinel,
  SessionPatchInput,
  SessionView,
  StringPreference,
  TransportConstraint,
  TransportMode,
  TransportPriority,
  HotelPriority,
} from "@/types/session";
import {
  BUDGET_MODE_LABELS,
  discoveryPlaces,
  HOTEL_PRIORITY_OPTIONS,
  MIN_STAR_OPTIONS,
  PACE_OPTIONS,
  ROOM_TYPE_OPTIONS,
  TRANSPORT_CONSTRAINT_OPTIONS,
  TRANSPORT_MODE_OPTIONS,
  TRANSPORT_PRIORITY_OPTIONS,
  formatTripLength,
  isFoodCategory,
  optionLabel,
  sentinelLabel,
} from "./options";

export type BudgetMode = "amount" | "undecided" | "auto";
export type DurationMode = "days" | "endDate";
export type PoiBulkMode = "auto" | "best";

export interface BasicDraft {
  origin: string;
  destination: string;
  startDate: string;
  durationMode: DurationMode;
  days: string;
  endDate: string;
  travelers: string;
  budgetMode: BudgetMode;
  budgetAmount: string;
}

export interface TransportDraft {
  mode: TransportMode | null;
  priority: TransportPriority | null;
  constraints: TransportConstraint[];
}

export interface HotelDraft {
  priority: HotelPriority | null;
  maxPriceText: string;
  maxPriceSentinel: PreferenceSentinel | null;
  minStar: NumericPreference;
  roomType: StringPreference;
  allowChange: BooleanPreference;
}

export interface PoiDraft {
  selections: Record<string, PoiSelection>;
  bulk: PoiBulkMode | null;
  /** 已经按类别展开过的分组，用于「查看更多」。 */
  expanded: string[];
}

export interface GuidedDraft {
  basic: BasicDraft;
  transport: TransportDraft;
  hotel: HotelDraft;
  poi: PoiDraft;
  pace: Pace | null;
}

export function createEmptyDraft(): GuidedDraft {
  return {
    basic: {
      origin: "",
      destination: "",
      startDate: "",
      durationMode: "days",
      days: "5",
      endDate: "",
      travelers: "2",
      budgetMode: "amount",
      budgetAmount: "",
    },
    transport: { mode: null, priority: null, constraints: [] },
    hotel: {
      priority: null,
      maxPriceText: "",
      maxPriceSentinel: null,
      minStar: null,
      roomType: null,
      allowChange: null,
    },
    poi: { selections: {}, bulk: null, expanded: [] },
    pace: null,
  };
}

function toPositiveInt(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed <= 0) return null;
  return parsed;
}

/** 天数：按天数直接取，按返程日期则按「含首尾」计算。 */
export function resolvedDays(basic: BasicDraft): number | null {
  if (basic.durationMode === "days") return toPositiveInt(basic.days);
  if (!basic.startDate || !basic.endDate) return null;
  const start = new Date(`${basic.startDate}T00:00:00`);
  const end = new Date(`${basic.endDate}T00:00:00`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return null;
  const diff = Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
  return diff > 0 ? diff : null;
}

export function transportConstraintsLabel(values: TransportConstraint[]): string[] {
  if (values.length === 0) return ["时间都可以"];
  return values.map((value) => optionLabel(TRANSPORT_CONSTRAINT_OPTIONS, value) ?? value);
}

export function maxPricePresetLabel(value: number): string {
  return `≤ ${formatCNY(value)}/晚`;
}

/** 基础信息的必填校验（预算可选）。 */
export function basicIssues(basic: BasicDraft): string[] {
  const issues: string[] = [];
  if (!basic.origin.trim()) issues.push("请填写出发地");
  if (!basic.destination.trim()) issues.push("请填写目的地");
  if (!basic.startDate) issues.push("请选择出发日期");
  if (basic.durationMode === "days") {
    if (!resolvedDays(basic)) issues.push("请填写行程天数（大于 0 的整数）");
  } else if (!basic.endDate) {
    issues.push("请选择返程日期");
  } else if (!resolvedDays(basic)) {
    issues.push("返程日期不能早于出发日期");
  }
  if (!toPositiveInt(basic.travelers)) issues.push("请填写出行人数（大于 0 的整数）");
  if (basic.budgetMode === "amount" && basic.budgetAmount.trim()) {
    if (!Number.isFinite(Number(basic.budgetAmount)) || Number(basic.budgetAmount) <= 0) {
      issues.push("预算请填写大于 0 的数字，或直接选「还没想好」");
    }
  }
  return issues;
}

function budgetValue(basic: BasicDraft): number | PreferenceSentinel | null {
  if (basic.budgetMode === "undecided") return "undecided";
  if (basic.budgetMode === "auto") return "auto";
  const amount = Number(basic.budgetAmount.trim());
  if (!basic.budgetAmount.trim() || !Number.isFinite(amount) || amount <= 0) return null;
  return amount;
}

/**
 * Prefetch 失效键（§15）：出发地 / 目的地 / 日期 / 天数一变，
 * 旧 Session 的交通、酒店与攻略都必须重查，POI 也必须换掉。
 */
export function prefetchKey(basic: BasicDraft): string {
  const endDate = basic.durationMode === "endDate" ? basic.endDate : "";
  return [
    basic.origin.trim(),
    basic.destination.trim(),
    basic.startDate,
    endDate,
    String(resolvedDays(basic) ?? ""),
  ].join("|");
}

export function toCreateInput(basic: BasicDraft): CreateSessionInput {
  const days = resolvedDays(basic);
  return {
    origin: basic.origin.trim(),
    destination: basic.destination.trim(),
    start_date: basic.startDate || null,
    end_date: basic.durationMode === "endDate" ? basic.endDate || null : null,
    days,
    travelers: toPositiveInt(basic.travelers),
    budget_total: budgetValue(basic),
  };
}

/** 交通偏好：未表态时按文档默认「都可以 + 帮我选 + 时间都可以」提交。 */
export function transportPatch(draft: GuidedDraft): SessionPatchInput {
  const constraints = draft.transport.constraints.filter((item) => item !== "any_time");
  return {
    transport_mode: draft.transport.mode ?? "any",
    transport_priority: draft.transport.priority ?? "auto",
    transport_constraints: constraints.length > 0 ? constraints : ["any_time"],
  };
}

function maxPriceValue(hotel: HotelDraft): NumericPreference | undefined {
  if (hotel.maxPriceSentinel) return hotel.maxPriceSentinel;
  const text = hotel.maxPriceText.trim();
  if (!text) return undefined;
  const parsed = Number(text);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
}

/** 酒店偏好：未表态的补充项直接省略，把决定权留给后端的稳定默认策略。 */
export function hotelPatch(draft: GuidedDraft): SessionPatchInput {
  const patch: SessionPatchInput = { hotel_priority: draft.hotel.priority ?? "auto" };
  const maxPrice = maxPriceValue(draft.hotel);
  if (maxPrice !== undefined) patch.hotel_max_price_per_night = maxPrice;
  if (draft.hotel.minStar !== null) patch.hotel_min_star = draft.hotel.minStar;
  if (draft.hotel.roomType !== null) patch.hotel_room_type = draft.hotel.roomType;
  if (draft.hotel.allowChange !== null) patch.hotel_allow_change = draft.hotel.allowChange;
  return patch;
}

/** POI 选择：永远提交完整的显式映射，空对象 = 都交给系统安排。 */
export function poiPatch(draft: GuidedDraft): SessionPatchInput {
  return { poi_selections: draft.poi.selections };
}

export function pacePatch(draft: GuidedDraft): SessionPatchInput {
  return { pace: draft.pace ?? "auto" };
}

export function budgetPatch(draft: GuidedDraft): SessionPatchInput {
  return { budget_total: budgetValue(draft.basic) };
}

/** 某一步「继续」时要写回后端的字段。基础信息与确认页不在这里（前者创建会话，后者开跑）。 */
export function stepPatch(stepIndex: number, draft: GuidedDraft): SessionPatchInput {
  switch (stepIndex) {
    case 1:
      return transportPatch(draft);
    case 2:
      return hotelPatch(draft);
    case 3:
      return poiPatch(draft);
    case 4:
      return pacePatch(draft);
    default:
      return {};
  }
}

export function hasAnyPreference(draft: GuidedDraft): boolean {
  return (
    draft.transport.mode !== null ||
    draft.transport.priority !== null ||
    draft.transport.constraints.length > 0 ||
    draft.hotel.priority !== null ||
    draft.hotel.maxPriceSentinel !== null ||
    draft.hotel.maxPriceText.trim() !== "" ||
    draft.hotel.minStar !== null ||
    draft.hotel.roomType !== null ||
    draft.hotel.allowChange !== null ||
    draft.pace !== null ||
    Object.keys(draft.poi.selections).length > 0
  );
}

/**
 * 重新创建 Session 后，把用户已经做过的结构化偏好补回去。
 * 注意：**不包含 poi_selections** —— 新会话意味着出发信息变了，旧城市的地点选择必须丢弃（§16）。
 */
export function preferenceReplayPatch(draft: GuidedDraft): SessionPatchInput {
  return {
    ...transportPatch(draft),
    ...hotelPatch(draft),
    ...pacePatch(draft),
  };
}

export interface PoiNameGroup {
  count: number;
  names: string[];
}

export interface SummaryModel {
  origin: string;
  destination: string;
  dateLabel: string;
  lengthLabel: string;
  travelersLabel: string;
  budgetLabel: string;
  transportMode: string;
  transportPriority: string;
  transportConstraints: string[];
  hotelPriority: string;
  hotelExtras: string[];
  paceLabel: string;
  must: PoiNameGroup;
  want: PoiNameGroup;
  food: PoiNameGroup;
  reject: PoiNameGroup;
  poiModeLabel: string | null;
  evidenceLabel: string | null;
}

function group(places: PlaceCandidate[]): PoiNameGroup {
  return { count: places.length, names: places.map((place) => place.name) };
}

/**
 * 酒店补充项的中文标签。
 * `includeStar: false` 时不再展示「最低星级」——数据源不返回星级时这条过滤不生效，
 * 摘要里也不应该出现一个做不到的承诺。
 */
export function hotelDetailLabels(
  hotel: HotelDraft,
  options: { includeStar?: boolean; includeAllowChange?: boolean } = {},
): string[] {
  const includeStar = options.includeStar ?? true;
  // 行程全程只订一家酒店时不展示「换酒店」：后端 capabilities.hotel_allow_change=false
  // 时它不参与排程，写进摘要会让人以为这个选择起了作用。
  const includeAllowChange = options.includeAllowChange ?? true;
  const labels: string[] = [];
  if (hotel.maxPriceSentinel) {
    labels.push(`每晚价格上限：${sentinelLabel(hotel.maxPriceSentinel) ?? hotel.maxPriceSentinel}`);
  } else if (hotel.maxPriceText.trim()) {
    labels.push(`每晚价格上限：${formatCNY(Number(hotel.maxPriceText))}`);
  }
  if (includeStar) {
    if (typeof hotel.minStar === "number") {
      const matched = MIN_STAR_OPTIONS.find((item) => item.value === hotel.minStar);
      labels.push(`最低星级：${matched ? matched.label : `${hotel.minStar} 星起`}`);
    } else if (hotel.minStar) {
      labels.push(`最低星级：${sentinelLabel(hotel.minStar) ?? hotel.minStar}`);
    }
  }
  if (typeof hotel.roomType === "string") {
    // 哨兵（auto / undecided / unlimited）本身也是字符串，必须先翻译成中文；
    // 否则选项表里匹配不到，会把原始枚举值直接显示成「房型：auto」。
    const sentinel = sentinelLabel(hotel.roomType);
    if (sentinel) {
      labels.push(`房型：${sentinel}`);
    } else {
      const matched = ROOM_TYPE_OPTIONS.find((item) => item.value === hotel.roomType);
      labels.push(`房型：${matched ? matched.label : hotel.roomType}`);
    }
  }
  if (includeAllowChange) {
    if (typeof hotel.allowChange === "boolean") {
      labels.push(`换酒店：${hotel.allowChange ? "可以换" : "不想换"}`);
    } else if (hotel.allowChange) {
      labels.push(`换酒店：${sentinelLabel(hotel.allowChange) ?? hotel.allowChange}`);
    }
  }
  return labels;
}

/** 摘要模型：右侧常驻卡片与确认页共用同一份口径，避免两处说法不一致。 */
export function summarize(draft: GuidedDraft, session: SessionView | null): SummaryModel {
  const places = new Map<string, PlaceCandidate>();
  for (const place of discoveryPlaces(session)) places.set(place.place_id, place);

  const must: PlaceCandidate[] = [];
  const want: PlaceCandidate[] = [];
  const food: PlaceCandidate[] = [];
  const reject: PlaceCandidate[] = [];
  for (const [placeId, state] of Object.entries(draft.poi.selections)) {
    const place = places.get(placeId);
    if (!place) continue;
    const isFood = isFoodCategory(place.category);
    if (state === "REJECT") {
      reject.push(place);
      continue;
    }
    if (isFood) {
      food.push(place);
      continue;
    }
    if (state === "MUST") must.push(place);
    else want.push(place);
  }

  const days = resolvedDays(draft.basic);
  const dateLabel = draft.basic.startDate
    ? `${formatDateShort(draft.basic.startDate)} 出发`
    : "出发日期待定";
  const evidence = session?.evidence_summary;
  const evidenceLabel =
    evidence && (evidence.places_verified ?? evidence.total_candidates) !== null
      ? `已核对 ${evidence.places_verified ?? evidence.total_candidates} 个地点 · 来源 ${evidence.sources_used ?? "—"} 个`
      : null;

  let poiModeLabel: string | null = null;
  if (draft.poi.bulk === "auto") poiModeLabel = "都随便，帮我安排";
  else if (draft.poi.bulk === "best") poiModeLabel = "只安排最值得去的";

  return {
    origin: draft.basic.origin.trim() || "出发地待定",
    destination: draft.basic.destination.trim() || "目的地待定",
    dateLabel,
    lengthLabel: formatTripLength(days),
    travelersLabel: draft.basic.travelers.trim() ? `${draft.basic.travelers.trim()} 人` : "人数待定",
    budgetLabel:
      draft.basic.budgetMode === "amount"
        ? Number(draft.basic.budgetAmount.trim()) > 0
          ? formatCNY(Number(draft.basic.budgetAmount))
          : "未填写"
        : BUDGET_MODE_LABELS[draft.basic.budgetMode],
    transportMode: optionLabel(TRANSPORT_MODE_OPTIONS, draft.transport.mode) ?? "都可以（默认）",
    transportPriority: optionLabel(TRANSPORT_PRIORITY_OPTIONS, draft.transport.priority) ?? "帮我选（默认）",
    transportConstraints: transportConstraintsLabel(draft.transport.constraints),
    hotelPriority: optionLabel(HOTEL_PRIORITY_OPTIONS, draft.hotel.priority) ?? "帮我选（默认）",
    // 后端声明星级过滤不可用时，摘要也不展示星级（来源是 Session 的能力声明）。
    hotelExtras: hotelDetailLabels(draft.hotel, {
      includeStar: session?.capabilities.hotel_star_filter !== false,
      // 同理：「接受换酒店」不生效时不写进摘要，免得用户以为它起了作用。
      includeAllowChange: session?.capabilities.hotel_allow_change !== false,
    }),
    paceLabel: optionLabel(PACE_OPTIONS, draft.pace) ?? "帮我安排（默认）",
    must: group(must),
    want: group(want),
    food: group(food),
    reject: group(reject),
    poiModeLabel,
    evidenceLabel,
  };
}

/** 「不限 / 不确定 / 帮我选」的中文短标签，供按钮态使用。 */
export function sentinelShortLabel(value: PreferenceSentinel): string {
  return sentinelLabel(value) ?? value;
}
