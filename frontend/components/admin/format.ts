/**
 * 管理台的展示格式化。
 *
 * 为什么单独一份、不复用 `lib/format.ts`：
 * 那边是旅行商品语义（价格、步行时长、交通方式），这里全部是观测语义
 * （毫秒、Token、调用次数、比例）。混在一起会让两边都变得难以维护。
 */

import type { AdminRecord } from "@/types/admin";

/** 计数：后端理应给数字；真给了 null 时显示占位符，而不是 "NaN"。 */
export function formatNumber(value: number | null | undefined, fraction = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: fraction,
    maximumFractionDigits: fraction,
  });
}

export function formatTokens(value: number | null | undefined): string {
  return formatNumber(value);
}

/** 成本：后端没有定价表时返回 null，此时明确写「未知」而不是 0。 */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
  if (value === 0) return "0";
  return `$${value.toFixed(value < 0.01 ? 5 : 4)}`;
}

export function formatDurationMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

/** 置信度：0~1 与 0~100 都支持，避免因为后端口径不同而把 0.86 显示成 1%。 */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value >= 0 && value <= 1) return `${(value * 100).toFixed(1)}%`;
  return `${value.toFixed(1)}%`;
}

/** 秒 / 毫秒混合的时延字段：大于 1000 视为毫秒。 */
export function formatLatency(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value >= 1000) return formatDurationMs(value);
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`;
}

export function formatPercentNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

/**
 * 通过率：契约只写了 latest_pass_rate，没有说明是 0~1 还是 0~100，
 * 因此两种都渲染，避免出现「0.86%」这种明显错误的读数。
 */
export function formatPassRate(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "暂无";
  return formatRatio(value);
}

const METRIC_LABELS: Record<string, string> = {
  total_tokens: "总 Token",
  input_tokens: "输入 Token",
  output_tokens: "输出 Token",
  cached_tokens: "命中缓存 Token",
  prompt_tokens: "输入 Token",
  completion_tokens: "输出 Token",
  llm_calls: "LLM 调用",
  jev_calls: "Jev 调用",
  tool_calls: "工具调用",
  provider_failures: "Provider 失败",
  badcase_count: "Bad Case 数",
  duration_ms: "总耗时",
  total_duration_ms: "总耗时",
  p50_ms: "P50 耗时",
  p95_ms: "P95 耗时",
  avg_ms: "平均耗时",
  latency_ms: "时延",
  cost: "成本",
  cost_usd: "成本（USD）",
  total_cost: "总成本",
  case_count: "用例数",
  passed: "通过",
  failed: "失败",
  pass_rate: "通过率",
  pass_count: "通过数",
  fail_count: "失败数",
  score: "得分",
  avg_score: "平均分",
  hard_constraint_pass_rate: "硬性约束通过率",
  budget_violations: "预算超限",
  time_window_violations: "时间窗冲突",
  must_visit_violations: "必去点缺失",
  opening_hours_violations: "开放时间冲突",
  transport_legs: "交通段数",
  day_count: "天数",
  quality_score: "质量分",
  coherence: "连贯性",
  pacing: "节奏",
  actionability: "可执行性",
  source_count: "来源数",
  sources: "来源数",
  evidence_coverage: "证据覆盖率",
  price_freshness_rate: "价格新鲜度",
  ad_risk_avg: "平均广告风险",
  citations: "引用数",
  provider_success_rate: "Provider 成功率",
  provider_calls: "Provider 调用",
  fallback_count: "fallback 次数",
  fallback_rate: "fallback 比例",
  timeout_count: "超时次数",
  low_confidence_rate: "低置信度比例",
  low_confidence: "低置信度",
  invalid_response: "非法响应",
  decision_count: "决策数",
  accept_rate: "采纳率",
  reject_rate: "拒绝率",
  quota: "配额",
  quota_source: "配额来源",
  fallback: "fallback 次数",
  timeout: "超时次数",
  calls: "调用次数",
  enabled: "是否启用",
  configured: "是否已配置",
  budget: "预算",
  budget_total: "预算上限",
  budget_used: "已用预算",
  model: "模型",
  version: "版本",
  note: "说明",
  status: "状态",
};

/** 指标名尽量给中文；遇到未登记的新指标就原样显示，方便直接对照后端日志。 */
export function formatMetricKey(key: string): string {
  return METRIC_LABELS[key] ?? key;
}

const STATUS_LABELS: Record<string, string> = {
  SUCCESS: "成功",
  FAILED: "失败",
  DEGRADED: "降级完成",
  RUNNING: "运行中",
  QUEUED: "排队中",
  PENDING: "待处理",
  WAITING: "等待中",
  CANCELLED: "已取消",
  CANCELED: "已取消",
  TIMEOUT: "超时",
  OK: "正常",
  HEALTHY: "正常",
  ERROR: "异常",
  UNKNOWN: "未知",
  unknown: "未知",
  critical: "严重",
  severe: "严重",
  high: "高",
  medium: "中",
  moderate: "中",
  low: "低",
  minor: "低",
  completed: "已完成",
  running: "运行中",
  pass: "通过",
  fail: "不通过",
  passed: "通过",
  failed_case: "不通过",
  pending: "待分析",
  analyzed: "已分析",
  unfixed: "未修复",
  fixed: "已修复",
  wontfix: "不修复",
  suspected: "疑似根因",
  verified: "根因已验证",
  rejected: "根因已排除",
  ACCEPT: "采纳",
  REJECT: "拒绝",
  ROLLBACK: "回滚",
  rule: "规则判定",
  human: "人工标注",
  benchmark: "评测发现",
  llm_judge: "模型评审",
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "未知";
  return STATUS_LABELS[status] ?? status;
}

/** 数值、布尔、数组、对象都能安全转成一行文本；对象做紧凑 JSON 预览。 */
export function formatMetricValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return "—";
    return Number.isInteger(value)
      ? value.toLocaleString("zh-CN")
      : value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
  }
  if (typeof value === "string") return value.length > 0 ? value : "—";
  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    return value.map((item) => formatMetricValue(item)).join("、");
  }
  return jsonPreview(value);
}

/** 对象压缩成一行；超长时截断，避免撑破移动端布局。 */
export function jsonPreview(value: unknown, maxLength = 200): string {
  let text: string;
  try {
    text = JSON.stringify(value);
  } catch {
    text = String(value);
  }
  if (!text) return "—";
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

/** 输入摘要：可能是对象（打分、criteria），也可能是长字符串。 */
export function formatUnknown(value: unknown): string {
  return formatMetricValue(value);
}

/** 过滤掉值为空的对象，用于「有内容才渲染」的判断。 */
export function countEntries(record: AdminRecord | null | undefined): number {
  return record ? Object.keys(record).length : 0;
}
