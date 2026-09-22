/**
 * 管理台的展示格式化。
 *
 * 为什么单独一份、不复用 `lib/format.ts`：
 * 那边是旅行商品语义（价格、步行时长、交通方式），这里全部是观测语义
 * （毫秒、Token、调用次数、比例）。混在一起会让两边都变得难以维护。
 */

import { BADCASE_CATEGORY_LABELS, RUN_SOURCE_LABELS, type AdminRecord } from "@/types/admin";

/** 计数：后端理应给数字；真给了 null 时显示占位符，而不是 "NaN"。 */
export function formatNumber(value: number | null | undefined, fraction = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: fraction,
    maximumFractionDigits: fraction,
  });
}

/**
 * Token 数量（KPI 行 / 环比口径）。
 *
 * 为什么不能只用 formatNumber：token 会到千万、亿这个量级，纯千分位在窄卡片里
 * 会挤成一长串数字，读不出数量级。10^4 以上收成「万」、10^8 以上收成「亿」，
 * 一眼能看出「多大」；万以下仍保留千分位，精确值不丢。
 *
 * 注意与 formatTokenCount 的区别：那个是明细里的精确计数（null 显示「未知」），
 * 这个专供聚合指标的展示，null 必须落成「—」（缺失 ≠ 0 个 token）。
 */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(1)} 亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
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
 * 采样口径的比例（Provider 成功率 / 失败率）。
 * 后端在没有样本时给 null，此时必须显示「—（无样本）」—— 落成 0% 会被读成「全部失败」，
 * 恰好把「这段时间没被调用」误判成故障。
 */
export function formatSampleRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—（无样本）";
  return formatRatio(value);
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
  // Provider 状态与调用状态（Provider Health 页）。
  UNAVAILABLE: "不可用",
  AUTH_ERROR: "鉴权失败",
  RATE_LIMIT: "被限流",
  EMPTY: "无结果",
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

/* ------------------------------ 成本 ------------------------------ */

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: "¥",
  RMB: "¥",
  USD: "$",
  EUR: "€",
  JPY: "¥",
};

/** 成本：未知货币时把代码缀在后面，不用猜测符号。 */
export function formatCostWithCurrency(
  value: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
  if (value === 0) return "0";
  const symbol = currency ? CURRENCY_SYMBOLS[currency.toUpperCase()] : undefined;
  const amount = value.toFixed(value < 0.01 ? 5 : 4);
  if (symbol) return `${symbol}${amount}`;
  return currency ? `${amount} ${currency}` : amount;
}

/** 每百万 token 单价：数值精度到 4 位，未配置时明确写「未配置」。 */
export function formatUnitPrice(
  value: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未配置";
  return `${formatCostWithCurrency(value, currency)} / 百万 token`;
}

/** Token 计数：后端未返回 usage 时显示「未知」，而不是一个孤零零的「—」。 */
export function formatTokenCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "未知";
  return formatNumber(value);
}

/** BadCase 分类的中文标签；未登记的分类原样返回，便于对照后端规则名。 */
export function badcaseCategoryLabel(category: string | null | undefined): string {
  if (!category) return "未分类";
  return BADCASE_CATEGORY_LABELS[category] ?? category;
}

/**
 * BadCase 症状清洗。
 * 后端 symptom 常带机器前缀（如 `route_unverified:15 段路线未核实`），
 * 直接展示会让人读不懂。这里剥掉 ASCII 标识符前缀，保留人话部分；
 * 剥完为空时回退到分类中文标签。
 */
export function cleanBadcaseSymptom(
  symptom: string | null | undefined,
  category?: string | null,
): string {
  const text = (symptom ?? "").trim();
  if (!text) return badcaseCategoryLabel(category);
  const stripped = text.replace(/^[A-Za-z][\w./-]*\s*[:：]\s*/, "").trim();
  return stripped.length > 0 ? stripped : badcaseCategoryLabel(category);
}

/** run 来源的中文标签。 */
export function runSourceLabel(source: string | null | undefined): string {
  if (!source) return "未知来源";
  return RUN_SOURCE_LABELS[source] ?? source;
}

/* ------------------------------ Trace 属性 ------------------------------ */

interface TraceAttributeSpec {
  label: string;
  unit?: string;
  kind?: "count" | "percent" | "money" | "text" | "duration";
}

/**
 * Trace span attributes 的内部中文键 → 友好标签 + 单位。
 * 为什么需要它：后端把「入选」「候选数」这类内部统计名直接写进 span 属性，
 * 操作员看到的是一串没有单位、没有说明的机器字段。
 * 未登记的键原样透传（见 formatTraceAttributeKey），保证新字段不会消失。
 */
const TRACE_ATTRIBUTE_SPECS: Record<string, TraceAttributeSpec> = {
  入选: { label: "入选地点", unit: " 个", kind: "count" },
  剔除: { label: "剔除地点", unit: " 个", kind: "count" },
  候选数: { label: "候选方案数", unit: " 个", kind: "count" },
  数据源调用: { label: "外部调用次数", unit: " 次", kind: "count" },
  模型调用: { label: "模型调用次数", unit: " 次", kind: "count" },
  决策数: { label: "决策条数", unit: " 条", kind: "count" },
  天数: { label: "天数", unit: " 天", kind: "count" },
  安排数: { label: "安排数", unit: " 项", kind: "count" },
  问题数: { label: "问题数", unit: " 个", kind: "count" },
  已修订: { label: "已修订", unit: " 个", kind: "count" },
  未解决: { label: "未解决", unit: " 个", kind: "count" },
  门票价格数: { label: "门票价格数", unit: " 条", kind: "count" },
  规则决策: { label: "规则决策", unit: " 条", kind: "count" },
  模型决策: { label: "模型决策", unit: " 条", kind: "count" },
  BadCase数: { label: "Bad Case 数", unit: " 条", kind: "count" },
  置信度: { label: "置信度", kind: "percent" },
  选用: { label: "选用", kind: "text" },
  选定: { label: "选定", kind: "text" },
  路线成功: { label: "路线成功", unit: " 段", kind: "count" },
  路线失败: { label: "路线失败", unit: " 段", kind: "count" },
  证据条数: { label: "证据条数", unit: " 条", kind: "count" },
  航班候选: { label: "航班候选", unit: " 个", kind: "count" },
  火车候选: { label: "火车候选", unit: " 个", kind: "count" },
  地图候选: { label: "地图候选", unit: " 个", kind: "count" },
  平台数: { label: "平台数", unit: " 个", kind: "count" },
  去重后: { label: "去重后", unit: " 个", kind: "count" },
  有证据: { label: "有证据", unit: " 项", kind: "count" },
  真实花费: { label: "真实花费", kind: "money" },
  估算花费: { label: "估算花费", kind: "money" },
  高德状态: { label: "高德状态", kind: "text" },
  数据源状态: { label: "数据源状态", kind: "text" },
  状态: { label: "状态", kind: "text" },
  决策: { label: "决策", kind: "text" },
  // Discovery → 正式 Run 的交接 span（component=planner，name=discovery_handoff）。
  // 四条线（transport / hotels / social / places）的值是交接结论，见 HANDOFF_VALUE_LABELS。
  grace_waited_ms: { label: "Discovery 等待", kind: "duration" },
};

/**
 * Discovery 交接结论 → 中文。
 *
 * 为什么按「值」而不是按「键」映射：同一个 `discovery_handoff` span 里，四条线用的是
 * `transport` / `hotels` / `social` / `places` 做键，而 `transport` 这个键在 provider span 上
 * 表示传输方式（http / mcp stdio）。按值翻译只命中这三个专用字符串，不会误伤。
 */
const HANDOFF_VALUE_LABELS: Record<string, string> = {
  reused: "复用预取",
  fallback_query: "本次补查",
  unavailable: "无可用结果",
};

/** 未登记的键原样返回；登记过的换成友好标签。 */
export function formatTraceAttributeKey(key: string): string {
  return TRACE_ATTRIBUTE_SPECS[key]?.label ?? key;
}

const TRACE_VALUE_MAX = 160;

function truncateText(text: string): string {
  return text.length > TRACE_VALUE_MAX ? `${text.slice(0, TRACE_VALUE_MAX)}…` : text;
}

/**
 * Trace 属性值：计数加千分位与单位，置信度按比例，花费带货币符号，
 * 长文本 / 长数组截断。未知键走 formatMetricValue（也带千分位）。
 */
export function formatTraceAttributeValue(key: string, value: unknown): string {
  const spec = TRACE_ATTRIBUTE_SPECS[key];
  if (!spec) {
    // 未登记的键：先把 Discovery 交接结论翻成中文，其余走通用格式化。
    if (typeof value === "string" && HANDOFF_VALUE_LABELS[value]) return HANDOFF_VALUE_LABELS[value];
    return formatMetricValue(value);
  }
  if (value === null || value === undefined) return "未返回";

  if (spec.kind === "count" || spec.kind === "money") {
    if (typeof value === "number" && Number.isFinite(value)) {
      if (spec.kind === "money") return `¥${formatNumber(value, 2)}`;
      return `${formatNumber(value)}${spec.unit ?? ""}`;
    }
    return truncateText(formatMetricValue(value));
  }

  if (spec.kind === "percent") {
    if (typeof value === "number" && Number.isFinite(value)) return formatRatio(value);
    return truncateText(formatMetricValue(value));
  }

  if (spec.kind === "duration") {
    if (typeof value === "number" && Number.isFinite(value)) return formatDurationMs(value);
    return truncateText(formatMetricValue(value));
  }

  return truncateText(formatMetricValue(value));
}

/** 完整属性的 pretty JSON，用于 Trace / 明细弹窗。 */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
