/**
 * 展示用的分档函数（FRONTEND_DESIGN §31）。
 *
 * 注意：这里只是把后端已经算好的分值映射到颜色，不参与任何业务计算。
 * Trust / Ad Risk / 预算 / 可行性全部由 Python 后端产出（§34）。
 */

import type { Evidence, ItineraryItem, SourceRef, TripPlan } from "@/types/plan";

export type DisplayTone = "success" | "warning" | "danger" | "muted";

export function trustTone(score: number | null | undefined): DisplayTone {
  if (score === null || score === undefined) return "muted";
  if (score >= 80) return "success";
  if (score >= 60) return "muted";
  return "warning";
}

export function adRiskTone(score: number | null | undefined): DisplayTone {
  if (score === null || score === undefined) return "muted";
  if (score >= 60) return "danger";
  if (score >= 30) return "warning";
  return "muted";
}

export const TONE_TEXT: Record<DisplayTone, string> = {
  success: "text-success-subtle-foreground",
  warning: "text-warning-subtle-foreground",
  danger: "text-danger-subtle-foreground",
  muted: "text-muted-foreground",
};

export const TONE_BAR: Record<DisplayTone, string> = {
  success: "bg-success/70",
  warning: "bg-warning/70",
  danger: "bg-danger/70",
  muted: "bg-foreground/35",
};

export const TONE_CHIP: Record<DisplayTone, string> = {
  success: "bg-success-subtle text-success-subtle-foreground",
  warning: "bg-warning-subtle text-warning-subtle-foreground",
  danger: "bg-danger-subtle text-danger-subtle-foreground",
  muted: "bg-muted text-muted-foreground",
};

export function adRiskLabel(score: number | null | undefined): string {
  if (score === null || score === undefined) return "未评估";
  if (score >= 60) return "较高广告风险";
  if (score >= 30) return "存在部分营销内容";
  return "广告风险低";
}

export function trustLabel(score: number | null | undefined): string {
  if (score === null || score === undefined) return "未评估";
  if (score >= 80) return "高可信";
  if (score >= 60) return "中等可信";
  return "可信度偏低";
}

// ==================================================
// 来源展示模型（「谁、什么时候、提供了什么数据」）
// ==================================================

/**
 * Provider → 中文名。用户不认识 `tuniu` / `tikhub` 这类机器标识，
 * 页面上只允许出现这里映射出的中文来源名。
 */
export const PROVIDER_LABELS: Record<string, string> = {
  tuniu: "途牛",
  途牛: "途牛",
  amap: "高德地图",
  高德: "高德地图",
  高德地图: "高德地图",
  tikhub: "小红书 / 抖音",
  小红书: "小红书",
  抖音: "抖音",
  "12306": "铁路 12306",
  railway_12306: "铁路 12306",
  tavily: "网页搜索",
  Tavily: "网页搜索",
  mediacrawler: "本地抓取",
  MediaCrawler: "本地抓取",
};

/** 未知 Provider 不裸奔英文：降级成「其他来源」并保留原始名做兜底。 */
export function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "其他来源";
  return PROVIDER_LABELS[provider] ?? provider;
}

const SOURCE_STATUS_LABELS: Record<string, string> = {
  OK: "成功",
  SUCCESS: "成功",
  DEGRADED: "部分可用",
  FAILED: "失败",
  ERROR: "失败",
  SKIPPED: "无数据",
  TIMEOUT: "超时",
  timeout: "超时",
};

export function sourceStatusLabel(status: string | null | undefined): string {
  if (!status) return "无数据";
  return SOURCE_STATUS_LABELS[status] ?? "无数据";
}

const SOURCE_TYPE_LABELS: Record<string, string> = {
  social: "用户内容",
  poi: "地点校验",
  route: "路线数据",
  web: "公开资料",
  flight: "机票查询",
  train: "火车票查询",
  hotel: "酒店查询",
};

function sourceTypeLabel(type: string | null | undefined): string {
  if (!type) return "数据记录";
  return SOURCE_TYPE_LABELS[type] ?? "数据记录";
}

export interface SourceRecord {
  key: string;
  /** 原始 provider，仅用于内部匹配，不直接渲染。 */
  provider: string;
  /** 中文来源名。 */
  providerName: string;
  /** 数据类别的中文名，例如「用户内容」。 */
  kindLabel: string;
  fetchedAt: string | null;
  /** 正文前 200 字，超出时以「…」结尾。 */
  preview: string;
  /** 完整正文（用于「展开」）。 */
  fullText: string;
  truncated: boolean;
  /** 中文状态：成功 / 部分可用 / 失败 / 超时 / 无数据。 */
  statusLabel: string;
  status: string;
  /** 该记录对应的 plan.sources[].source_id，用于「从行程项跳到来源」时高亮。 */
  sourceIds: string[];
  /** 该记录对应的 evidence[].id。 */
  evidenceIds: string[];
}

const CONTENT_LIMIT = 200;

function previewOf(text: string): { preview: string; truncated: boolean } {
  const clean = text.trim();
  if (clean.length <= CONTENT_LIMIT) return { preview: clean, truncated: false };
  return { preview: `${clean.slice(0, CONTENT_LIMIT)}…`, truncated: true };
}

function statusOfProvider(sources: SourceRef[], provider: string, fallback: string): string {
  const matched = sources.find((source) => source.provider === provider);
  return matched?.status ?? fallback;
}

/** 把 evidence[] 转成用户可读的来源记录（正文 = 证据正文）。 */
export function buildEvidenceRecords(evidence: Evidence[], sources: SourceRef[] = []): SourceRecord[] {
  return evidence.map((entry) => {
    const text = entry.text?.trim() || entry.title || "本次没有返回可展示的正文。";
    const { preview, truncated } = previewOf(text);
    return {
      key: `ev:${entry.id}`,
      provider: entry.provider,
      providerName: providerLabel(entry.provider),
      kindLabel: sourceTypeLabel(entry.source_type),
      fetchedAt: entry.fetched_at ?? null,
      preview,
      fullText: text,
      truncated,
      status: statusOfProvider(sources, entry.provider, "OK"),
      statusLabel: sourceStatusLabel(statusOfProvider(sources, entry.provider, "OK")),
      sourceIds: sources.filter((source) => source.provider === entry.provider).map((source) => source.source_id),
      evidenceIds: [entry.id],
    };
  });
}

/**
 * 汇总本次规划的全部来源记录。
 *
 * evidence[] 是「有正文的数据」；sources[] 里还有机票 / 火车票 / 酒店这类没有正文、
 * 只有查询结果的来源。只展示 evidence 会漏掉「谁提供了机票」，只展示 sources 又拿不到
 * 正文，所以两者合并：evidence 优先，sources 里未被任何 evidence 覆盖的类别补进来
 * （内容用 source.title，它本身就是中文的数据摘要），并对同一 source_id 去重。
 */
export function buildSourceRecords(plan: TripPlan): SourceRecord[] {
  const sources = plan.sources ?? [];
  const evidence = plan.evidence ?? [];
  const records = buildEvidenceRecords(evidence, sources);

  const coveredTypes = new Set(evidence.map((entry) => entry.source_type));
  const seenSourceIds = new Set<string>();
  for (const source of sources) {
    if (coveredTypes.has(source.source_type)) continue;
    if (seenSourceIds.has(source.source_id)) continue;
    seenSourceIds.add(source.source_id);
    const text = source.title?.trim() || "本次没有返回可展示的内容。";
    const { preview, truncated } = previewOf(text);
    records.push({
      key: `src:${source.source_id}`,
      provider: source.provider,
      providerName: providerLabel(source.provider),
      kindLabel: sourceTypeLabel(source.source_type),
      fetchedAt: source.fetched_at ?? null,
      preview,
      fullText: text,
      truncated,
      status: source.status,
      statusLabel: sourceStatusLabel(source.status),
      sourceIds: [source.source_id],
      evidenceIds: [],
    });
  }

  return records;
}

/** 某条行程安排对应的来源：证据按 evidence_ids 精确匹配，其余来源按 source_ids 匹配。 */
export function recordsForItem(
  records: SourceRecord[],
  item: ItineraryItem | null,
): { evidence: SourceRecord[]; sources: SourceRecord[] } {
  if (!item) return { evidence: [], sources: [] };
  const evidenceIds = new Set(item.evidence_ids ?? []);
  const sourceIds = new Set(item.source_ids ?? []);
  const evidence: SourceRecord[] = [];
  const sources: SourceRecord[] = [];
  for (const record of records) {
    if (record.evidenceIds.some((id) => evidenceIds.has(id))) {
      evidence.push(record);
      continue;
    }
    if (record.evidenceIds.length === 0 && record.sourceIds.some((id) => sourceIds.has(id))) {
      sources.push(record);
    }
  }
  return { evidence, sources };
}
