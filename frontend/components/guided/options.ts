/**
 * Guided 向导的选项定义与展示标签。
 *
 * 这里是「中文按钮 ↔ 后端枚举」的唯一映射表：
 * 每个选项的 value 都是后端契约里的真实取值，不存在只写在前端的占位符。
 */

import type { LucideIcon } from "lucide-react";
import {
  Baby,
  Camera,
  Coffee,
  Compass,
  Footprints,
  Landmark,
  MapPin,
  Moon,
  ShoppingBag,
  Sparkles,
  Trees,
  Utensils,
} from "lucide-react";
import type {
  HotelPriority,
  Pace,
  PreferenceSentinel,
  PlaceCandidate,
  SessionView,
  TransportConstraint,
  TransportMode,
  TransportPriority,
} from "@/types/session";

export interface Option<T> {
  value: T;
  label: string;
  /** 一行说明：让用户不用猜这个选项会被怎么用。 */
  hint?: string;
}

export const TRANSPORT_MODE_OPTIONS: Option<TransportMode>[] = [
  { value: "train", label: "高铁 / 火车", hint: "只看 12306 车次" },
  { value: "flight", label: "飞机", hint: "只看航班" },
  { value: "any", label: "都可以", hint: "价格和时间合适就行" },
  { value: "auto", label: "随便，帮我选", hint: "怎么去完全交给系统" },
];

export const TRANSPORT_PRIORITY_OPTIONS: Option<TransportPriority>[] = [
  { value: "value", label: "性价比优先", hint: "价格与门到门时间综合最好" },
  { value: "fastest", label: "时间最短", hint: "优先门到门更快" },
  { value: "cheapest", label: "价格最低", hint: "优先更便宜" },
  { value: "comfort", label: "舒适优先", hint: "优先少折腾、座椅更舒服" },
  { value: "auto", label: "随便，帮我选", hint: "由系统稳定兜底判断" },
];

export const TRANSPORT_CONSTRAINT_OPTIONS: Option<TransportConstraint>[] = [
  { value: "few_transfers", label: "少换乘" },
  { value: "no_early", label: "不要太早" },
  { value: "no_red_eye", label: "不要红眼" },
  { value: "any_time", label: "时间都可以" },
];

export const HOTEL_PRIORITY_OPTIONS: Option<HotelPriority>[] = [
  { value: "value", label: "性价比优先" },
  { value: "location", label: "位置优先" },
  { value: "rating", label: "评分优先" },
  { value: "comfort", label: "舒适优先" },
  { value: "transit", label: "交通方便" },
  { value: "auto", label: "随便，帮我选" },
];

export const ROOM_TYPE_OPTIONS: Option<string>[] = [
  { value: "大床房", label: "大床房" },
  { value: "双床房", label: "双床房" },
  { value: "家庭房", label: "家庭房 / 三人间" },
];

export const PACE_OPTIONS: Option<Pace>[] = [
  { value: "relaxed", label: "轻松一点", hint: "每天少安排一些，留出休息时间" },
  { value: "balanced", label: "正常", hint: "经典节奏，兼顾体验和休息" },
  { value: "packed", label: "多玩一些", hint: "尽量覆盖更多值得去的地方" },
  { value: "auto", label: "随便，帮我安排", hint: "按天数与地点数量自动定" },
];

/** 「不限 / 不确定 / 帮我选」：数值与布尔型补充项共用的三个显式取值。 */
export const SENTINEL_OPTIONS: Option<PreferenceSentinel>[] = [
  { value: "unlimited", label: "不限" },
  { value: "undecided", label: "不确定" },
  { value: "auto", label: "帮我选" },
];

export const MAX_PRICE_PRESETS = [300, 500, 800, 1200];

export const BUDGET_MODE_LABELS = {
  amount: "填写预算",
  undecided: "还没想好",
  auto: "随便，帮我控制",
} as const;

export function sentinelLabel(value: string): string | null {
  const found = SENTINEL_OPTIONS.find((item) => item.value === value);
  return found ? found.label : null;
}

export function optionLabel<T>(options: Option<T>[], value: T | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const found = options.find((item) => item.value === value);
  return found ? found.label : null;
}

/** 时长文案：5 天 → 5天4晚。 */
export function formatTripLength(days: number | null | undefined): string {
  if (!days || days <= 0) return "天数待定";
  if (days === 1) return "1 天";
  return `${days}天${days - 1}晚`;
}

export const CATEGORY_LABELS: Record<string, string> = {
  attraction: "热门景点",
  sight: "热门景点",
  scenic: "热门景点",
  spot: "热门景点",
  food: "美食",
  restaurant: "美食",
  snack: "美食",
  nightview: "夜景",
  night: "夜景",
  shopping: "商圈",
  business: "商圈",
  mall: "商圈",
  experience: "特色体验",
  activity: "特色体验",
  photo: "适合拍照",
  nature: "自然",
  history: "历史",
  culture: "人文",
  family: "亲子",
  kids: "亲子",
  other: "其他",
};

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  attraction: Landmark,
  sight: Landmark,
  food: Utensils,
  snack: Coffee,
  restaurant: Utensils,
  nightview: Moon,
  night: Moon,
  shopping: ShoppingBag,
  business: ShoppingBag,
  experience: Footprints,
  activity: Footprints,
  photo: Camera,
  nature: Trees,
  history: Landmark,
  culture: Landmark,
  family: Baby,
  other: MapPin,
};

export function categoryLabel(category: string | null | undefined, fallback?: string | null): string {
  if (fallback) return fallback;
  if (!category) return "其他";
  return CATEGORY_LABELS[category.toLowerCase()] ?? category;
}

export function categoryIcon(category: string | null | undefined): LucideIcon {
  if (!category) return Compass;
  return CATEGORY_ICONS[category.toLowerCase()] ?? Sparkles;
}

const FOOD_KEYS = ["food", "restaurant", "snack", "美食", "小吃", "餐厅", "吃"];

/** 美食类的三个按钮换成「想吃 / 可以安排 / 不要」。 */
export function isFoodCategory(category: string | null | undefined): boolean {
  if (!category) return false;
  const lower = category.toLowerCase();
  return FOOD_KEYS.some((key) => lower.includes(key));
}

export interface PoiActionLabels {
  must: string;
  want: string;
  reject: string;
}

export function poiActionLabels(category: string | null | undefined): PoiActionLabels {
  if (isFoodCategory(category)) {
    return { must: "想吃", want: "可以安排", reject: "不要" };
  }
  return { must: "必去", want: "想去", reject: "不感兴趣" };
}

function trust(place: PlaceCandidate): number {
  return typeof place.trust_score === "number" ? place.trust_score : -1;
}

function evidence(place: PlaceCandidate): number {
  return typeof place.evidence_count === "number" ? place.evidence_count : -1;
}

function adRisk(place: PlaceCandidate): number {
  if (typeof place.ad_risk === "number") return place.ad_risk;
  if (place.ad_risk === "low") return 0;
  if (place.ad_risk === "medium") return 1;
  if (place.ad_risk === "high") return 2;
  return 0;
}

/**
 * POI 排序（§8）：先看证据与可信度，再看到底被多少篇攻略提到，最后把广告风险高的排后面。
 * Discovery 阶段还没有路线信息，所以这里不能按路线排。
 */
export function sortPlaces(places: PlaceCandidate[]): PlaceCandidate[] {
  return places.slice().sort((a, b) => {
    if (trust(b) !== trust(a)) return trust(b) - trust(a);
    if (evidence(b) !== evidence(a)) return evidence(b) - evidence(a);
    if (adRisk(a) !== adRisk(b)) return adRisk(a) - adRisk(b);
    return a.name.localeCompare(b.name, "zh-Hans-CN");
  });
}

export interface PlaceGroup {
  key: string;
  label: string;
  places: PlaceCandidate[];
}

/** 契约 2 优先使用分池候选；旧后端只回 place_candidates 时无缝回退。 */
export function discoveryPlaces(session: SessionView | null): PlaceCandidate[] {
  if (!session) return [];
  const pooled = [...session.poi_pools.attraction, ...session.poi_pools.food, ...session.poi_pools.experience];
  const source = pooled.length ? pooled : session.place_candidates;
  const unique = new Map<string, PlaceCandidate>();
  for (const place of source) {
    if (!unique.has(place.place_id)) unique.set(place.place_id, place);
  }
  return Array.from(unique.values());
}

/**
 * 按类别分组：优先用后端给的 place_categories 顺序与文案，
 * 后端没给（或给了新类别）时按 place_candidates 自己归类，绝不丢数据。
 */
export function groupPlaces(session: SessionView | null): PlaceGroup[] {
  if (!session) return [];
  const groups = new Map<string, PlaceGroup>();
  for (const item of session.place_categories) {
    if (groups.has(item.category)) continue;
    groups.set(item.category, {
      key: item.category,
      label: categoryLabel(item.category, item.label),
      places: [],
    });
  }
  for (const place of discoveryPlaces(session)) {
    const key = place.category ?? "other";
    const existing = groups.get(key);
    if (existing) {
      existing.places.push(place);
      continue;
    }
    groups.set(key, {
      key,
      label: categoryLabel(key, place.category_label),
      places: [place],
    });
  }
  return Array.from(groups.values())
    .map((group) => ({ ...group, places: sortPlaces(group.places) }))
    .filter((group) => group.places.length > 0);
}

/**
 * 「只安排最值得去的」的后端映射：每个类别按证据/可信度取前 N 个标成 WANT。
 * 这不是本地开关，而是一份真实的 poi_selections 提交给后端。
 */
export function bestValueSelections(session: SessionView | null, perCategory = 3): Record<string, "WANT"> {
  const selections: Record<string, "WANT"> = {};
  for (const group of groupPlaces(session)) {
    for (const place of group.places.slice(0, perCategory)) {
      selections[place.place_id] = "WANT";
    }
  }
  return selections;
}
