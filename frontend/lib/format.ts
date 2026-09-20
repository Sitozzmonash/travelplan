import type { PriceType, TravelLeg } from "@/types/plan";

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

export function formatCNY(value: number | null | undefined, fraction = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `¥${value.toLocaleString("zh-CN", {
    minimumFractionDigits: fraction,
    maximumFractionDigits: fraction,
  })}`;
}

/** "2026-10-01" → "2026/10/01" */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const [y, m, d] = value.slice(0, 10).split("-");
  if (!y || !m || !d) return value;
  return `${y}/${m}/${d}`;
}

/** "2026-10-01" → "10月1日" */
export function formatDateShort(value: string | null | undefined): string {
  if (!value) return "—";
  const [, m, d] = value.slice(0, 10).split("-");
  if (!m || !d) return value;
  return `${Number(m)}月${Number(d)}日`;
}

export function formatWeekday(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(`${value.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(date.getTime())) return "";
  return WEEKDAYS[date.getDay()] ?? "";
}

/** ISO → "14:32"，用于「查询于 14:32」。 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return "";
  const match = value.match(/T(\d{2}):(\d{2})/);
  if (match) return `${match[1]}:${match[2]}`;
  const bare = value.match(/^(\d{2}):(\d{2})/);
  return bare ? `${bare[1]}:${bare[2]}` : "";
}

/** ISO → "2026/10/01 14:32" */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const clock = formatClock(value);
  return `${formatDate(value)}${clock ? ` ${clock}` : ""}`;
}

export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

export function formatDistance(meters: number | null | undefined): string {
  if (meters === null || meters === undefined) return "—";
  if (meters < 1000) return `${Math.round(meters)} m`;
  return `${(meters / 1000).toFixed(1)} km`;
}

export function legMinutes(leg: TravelLeg | null | undefined): number | null {
  if (!leg?.duration_seconds) return null;
  return Math.ceil(leg.duration_seconds / 60);
}

const MODE_LABELS: Record<string, string> = {
  walk: "步行",
  walking: "步行",
  transit: "地铁 / 公交",
  subway: "地铁",
  bus: "公交",
  driving: "打车 / 自驾",
  taxi: "打车",
  flight: "飞机",
  train: "高铁 / 火车",
  bicycle: "骑行",
};

export function formatMode(mode: string | null | undefined): string {
  if (!mode) return "市内交通";
  return MODE_LABELS[mode] ?? mode;
}

export const PRICE_TYPE_LABELS: Record<PriceType, string> = {
  realtime: "实时价格",
  estimated: "估算费用",
  unknown: "价格未知",
};

export function formatPriceType(type: PriceType | string | undefined): string {
  if (type === "realtime" || type === "estimated" || type === "unknown") {
    return PRICE_TYPE_LABELS[type];
  }
  return "估算费用";
}

export function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/**
 * Provider 查询参数压成一行。
 *
 * 后端把 `sources.query_json` 原样给出来（结构保真），所以这里拿到的是对象而不是字符串；
 * 直接把它当一个 React 子节点渲染会抛 "Objects are not valid as a React child" 并让整页
 * 进入错误边界 —— 用户端与管理端都必须经过这个函数。
 */
export function formatProviderQuery(query: Record<string, unknown> | null | undefined): string {
  if (!query) return "—";
  const entries = Object.entries(query);
  if (entries.length === 0) return "—";
  return entries
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(" / ") : String(value)}`)
    .join("，");
}
