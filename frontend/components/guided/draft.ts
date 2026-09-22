/**
 * Guided 向导的本地草稿模型与「草稿 → 后端请求体」的转换。
 *
 * 设计原则：
 * 1. 草稿保留没表态（null）与「不限 / 不确定 / 帮我选」的区别；
 *    清空补充条件时显式提交 null，不能省略 PATCH 字段而留下旧值。
 * 2. 任何一步都能用「随便 / 帮我选」走完：未表态时按文档默认（都可以 + 帮我选）提交，
 *    不阻塞、也不在前端假装后端已经知道。
 */

import { formatCNY, formatDateShort } from "@/lib/format";
import type {
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
  placeDisplayName,
  HOTEL_PRIORITY_OPTIONS,
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
  roomType: StringPreference;
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
      // 默认今天出发；用户可以改。放在这里而不是挂载后 setState：
      // `todayISO()` 钉死了时区，服务端与客户端算出的字符串一致，不会 hydration 不匹配。
      startDate: todayISO(),
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
      roomType: null,
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

function maxPriceValue(hotel: HotelDraft): NumericPreference {
  if (hotel.maxPriceSentinel) return hotel.maxPriceSentinel;
  const text = hotel.maxPriceText.trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

/** 后端按字段是否出现更新；null 才会清除旧价格 / 房型，省略意味着保留。 */
export function hotelPatch(draft: GuidedDraft): SessionPatchInput {
  return {
    hotel_priority: draft.hotel.priority ?? "auto",
    hotel_max_price_per_night: maxPriceValue(draft.hotel),
    hotel_room_type: draft.hotel.roomType,
  };
}

/** POI 选择：永远提交完整的显式映射，空对象 = 都交给系统安排。 */
export function poiPatch(draft: GuidedDraft): SessionPatchInput {
  return { poi_selections: draft.poi.selections };
}

export function pacePatch(draft: GuidedDraft): SessionPatchInput {
  return { pace: draft.pace ?? "auto" };
}

/**
 * 某一步「继续」时要写回后端的字段（索引与 `STEP_META` 对齐）。
 * 第 0 页（探索确认）写回的就是这一页勾选的 POI；
 * 最后一页（偏好）由向导先 PATCH 再开跑，两份合起来就是这一页改过的全部字段。
 * 基础信息不在这里 —— 它由首页带进 URL，只在创建会话时提交一次。
 */
export function stepPatch(stepIndex: number, draft: GuidedDraft): SessionPatchInput {
  switch (stepIndex) {
    case 0:
      return poiPatch(draft);
    case 1:
      return { ...transportPatch(draft), ...hotelPatch(draft), ...pacePatch(draft) };
    default:
      return {};
  }
}

/**
 * 出发日期的默认值：**中国时区的今天**（YYYY-MM-DD）。
 *
 * 两个约束决定了必须钉死时区，而不是用浏览器本地时间：
 *  1. 这个组件会被 SSR 一次，服务端（UTC）与客户端（东八区）用各自的 `new Date()`
 *     会在凌晨 00:00~08:00 算出**不同的日期**，造成 hydration 不一致；
 *  2. 本产品只做中国国内旅行规划，用户与目的地都在 UTC+8，
 *     用 Asia/Shanghai 既确定又符合实际。
 */
export function todayISO(): string {
  // en-CA 的短日期格式恰好是 YYYY-MM-DD。
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export function hasAnyPreference(draft: GuidedDraft): boolean {
  return (
    draft.transport.mode !== null ||
    draft.transport.priority !== null ||
    draft.transport.constraints.length > 0 ||
    draft.hotel.priority !== null ||
    draft.hotel.maxPriceSentinel !== null ||
    draft.hotel.maxPriceText.trim() !== "" ||
    draft.hotel.roomType !== null ||
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

/** 最终确认 / 错误重试提交完整现状，不重放历史失败的局部 PATCH。 */
export function fullDraftPatch(draft: GuidedDraft): SessionPatchInput {
  return { ...preferenceReplayPatch(draft), ...poiPatch(draft) };
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
  return { count: places.length, names: places.map(placeDisplayName) };
}

/**
 * 酒店补充项的中文标签（每晚价格上限 + 房型）。
 * 这两项是酒店页现在仅剩的补充条件：星级过滤与「接受换酒店」已从界面移除，
 * 摘要里自然也不会再出现一个前端根本没让用户选过的承诺。
 */
export function hotelDetailLabels(hotel: HotelDraft): string[] {
  const labels: string[] = [];
  if (hotel.maxPriceSentinel) {
    labels.push(`每晚价格上限：${sentinelLabel(hotel.maxPriceSentinel) ?? hotel.maxPriceSentinel}`);
  } else if (hotel.maxPriceText.trim()) {
    labels.push(`每晚价格上限：${formatCNY(Number(hotel.maxPriceText))}`);
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
  if (draft.poi.bulk === "auto") poiModeLabel = "使用这份推荐，帮我安排";
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
    hotelExtras: hotelDetailLabels(draft.hotel),
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
