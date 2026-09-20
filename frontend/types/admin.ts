/**
 * 管理控制台的数据契约（对应 FastAPI `/api/v1/admin/*`）。
 *
 * 这里刻意保留后端的 snake_case 字段名：前端不做字段重命名，
 * 这样后端一旦改名，下面的运行时类型守卫会立刻把问题报出来，
 * 而不是在某个渲染分支里悄悄变成 undefined。
 *
 * 约定：
 * - 计数类字段后端一定返回数字；展示层用 formatNumber 兜底 null。
 * - 允许为空的字段（例如 cost / finished_at）显式标注 `| null`。
 * - 未在契约中固定形状的嵌套结构用 AdminRecord 承载，由展示层决定怎么读。
 */

export type AdminRecord = Record<string, unknown>;

/* ------------------------------ 通用枚举 ------------------------------ */

/**
 * run 的来源。
 * `quick` 一句话、`guided` 引导式、`cli` 命令行、`benchmark` Benchmark 用例。
 * 旧数据没有该字段时按 `undefined` 处理，展示层回退成「未知来源」。
 */
export type AdminRunSource = "quick" | "guided" | "cli" | "benchmark";

export const ADMIN_RUN_SOURCES = ["quick", "guided", "cli", "benchmark"] as const;

export const RUN_SOURCE_LABELS: Record<string, string> = {
  quick: "一句话",
  guided: "引导式",
  cli: "命令行",
  benchmark: "Benchmark",
};

/**
 * BadCase 分类 → 中文标签。
 * 为什么在前端维护映射：后端 category 是稳定的机器标识（规则名），
 * 而这一页是给人看的。新增未登记分类时原样显示，不会因为漏配而变成空白。
 */
export const BADCASE_CATEGORY_LABELS: Record<string, string> = {
  route_unverified: "路线未核实",
  provider_failure: "数据源调用失败",
  hard_constraint: "硬约束冲突",
  budget: "超预算",
  budget_math: "预算算术错误",
  evidence_gap: "缺少攻略证据",
  missing_disclosure: "未披露交通/住宿",
  hallucinated_price: "无来源的实时价",
  degradation: "能力降级",
  jev_timeout: "Jev 超时",
  jev_quota: "Jev 额度不足",
  jev_invalid_response: "Jev 返回不合法",
  jev_low_confidence: "Jev 置信度过低",
  jev_wrong_choice: "Jev 选错方案",
  jev_unnecessary_replan: "Jev 多余重排",
  jev_missed_replan: "Jev 漏判重排",
};

/** 成本来源：`user_price` 表示按用户配置的每百万 token 单价算出，其余为后端定价表。 */
export type AdminCostSource = "user_price" | "backend" | string;

export interface AdminCostBreakdown {
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  input_price_per_million: number | null;
  output_price_per_million: number | null;
  cached_price_per_million: number | null;
}

/* ------------------------------ 仪表盘 ------------------------------ */

export interface AdminJevSummary {
  enabled: boolean;
  configured: boolean;
  calls: number;
  fallback: number;
  timeout: number;
  low_confidence: number;
}

export interface AdminBenchmarkSummary {
  latest_run_id: string | null;
  latest_suites: string[];
  /** 0~1 或 0~100 由后端决定，展示层两种都能渲染。 */
  latest_pass_rate: number | null;
}

export interface AdminEvolutionSummary {
  enabled: boolean;
  pending_badcases: number;
  last_run_id: string | null;
  last_decision: string | null;
}

export interface AdminOverview {
  run_count: number;
  /** 形如 { SUCCESS: 12, FAILED: 1 }，key 是后端的状态枚举。 */
  statuses: AdminRecord;
  degraded_count: number;
  failed_count: number;
  running_count: number;
  total_tokens: number;
  total_llm_calls: number;
  total_jev_calls: number;
  total_tool_calls: number;
  provider_failures: number;
  badcase_open: number;
  badcase_total: number;
  jev: AdminJevSummary;
  benchmark: AdminBenchmarkSummary;
  evolution: AdminEvolutionSummary;
}

/* ------------------------------ 运行记录 ------------------------------ */

export interface AdminRunSummary {
  run_id: string;
  status: string;
  /** 允许缺失：后端由 `COALESCE(original_query, '')` 给出，但旧数据可能整段没有。 */
  original_query?: string | null;
  /** 列表行保留创建时间；详情里以 started_at / finished_at 为准。 */
  created_at?: string | null;
  /** 结束时间：老记录（没有 run_progress 行）可能没有终态时间。 */
  finished_at?: string | null;
  duration_ms?: number | null;
  /** benchmark 用例运行使用假模型，usage 可能整段为 null。 */
  total_tokens?: number | null;
  llm_calls?: number | null;
  jev_calls?: number | null;
  tool_calls?: number | null;
  provider_failures?: number | null;
  badcase_count?: number | null;
  cost?: number | null;
  /** 后端新增：来源（quick / guided / cli / benchmark）与来源会话。 */
  source?: string | null;
  source_session_id?: string | null;
  /** 详情补齐的字段（列表不一定有）：开始时间。 */
  started_at?: string | null;
}

export interface AdminRunList {
  items: AdminRunSummary[];
  limit: number;
  offset: number;
  total: number;
}

export interface AdminRunMetrics {
  duration_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
  total_tokens?: number | null;
  llm_calls?: number | null;
  jev_calls?: number | null;
  tool_calls?: number | null;
  provider_failures?: number | null;
  badcase_count?: number | null;
  /** 成本：后端没有定价表（cost_source 为 null）时为 null。 */
  cost?: number | null;
  cost_currency?: string | null;
  /** `user_price` 表示按用户配置的每百万 token 单价算出；null 表示未配置。 */
  cost_source?: AdminCostSource | null;
  cost_breakdown?: AdminCostBreakdown | null;
}

export interface AdminStageProgress {
  stage_id: string;
  title: string;
  status: string;
  message: string | null;
  facts: AdminRecord | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface AdminRunProgress {
  run_id: string;
  status: string;
  message: string | null;
  stages: AdminStageProgress[];
}

export interface AdminTraceSpan {
  span_id: string;
  parent_span_id: string | null;
  component: string;
  name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  attributes: AdminRecord | null;
  error: string | null;
}

export interface AdminDecision {
  entity_id: string;
  status: string;
  agent_or_stage: string;
  reason_codes: string[];
  reason_text: string;
  scores: AdminRecord | null;
}

export interface AdminProviderCall {
  provider: string;
  tool: string;
  /** Provider 调用的查询参数（后端原样给出 sources.query_json）。 */
  query: Record<string, unknown>;
  status: string;
  /** 后端未固定形状：可能是条数、布尔或空。 */
  returned: unknown;
  duration_ms: number | null;
  fetched_at: string | null;
  source_id: string | null;
  note: string | null;
}

export interface AdminJevCall {
  tag: string;
  decision_type?: string | null;
  status?: string | null;
  choice?: string | null;
  confidence?: number | null;
  latency_ms?: number | null;
  model?: string | null;
  fallback?: boolean | null;
  fallback_reason?: string | null;
  quota?: unknown;
  input_summary?: string | null;
  criteria?: unknown;
  error?: string | null;
}

/** 一次模型调用。benchmark 用例跑的是假模型，因此多数字段可能为 null。 */
export interface AdminLlmCall {
  tag: string;
  model?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  chars?: number | null;
  error?: string | null;
  started_at?: string | null;
}

/** 阶段详情里的模型调用摘要。 */
export interface AdminStageLlmCall {
  tag?: string | null;
  model?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  chars?: number | null;
  error?: string | null;
}

/** 阶段详情里的工具 / 数据源调用摘要。 */
export interface AdminStageToolCall {
  provider?: string | null;
  tool?: string | null;
  status?: string | null;
  returned?: unknown;
  duration_ms?: number | null;
}

export interface AdminStageTokens {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
  total_tokens?: number | null;
}

/**
 * 单个阶段的完整明细（`GET /admin/runs/{id}/stages`）。
 * 所有嵌套字段都按「可能缺失」处理：后端契约是分批补的，缺一段只应降级成空表。
 */
export interface AdminStageDetail {
  stage_id: string;
  title?: string | null;
  status?: string | null;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  steps?: string[] | null;
  facts?: AdminRecord | null;
  spans?: AdminTraceSpan[] | null;
  llm_calls?: AdminStageLlmCall[] | null;
  tool_calls?: AdminStageToolCall[] | null;
  tokens?: AdminStageTokens | null;
}

/** 引导式会话里保存的结构化偏好。 */
export interface AdminPlaceSelections {
  must: string[];
  want: string[];
  reject: string[];
}

/**
 * 这次 run 对应的用户前置选择（引导式旅程）。
 * 后端在没有引导式会话时返回 null —— 页面必须能显示"这次不是引导式创建的"。
 */
export interface AdminUserJourney {
  source?: string | null;
  source_session_id?: string | null;
  transport_mode?: string | null;
  transport_priority?: string | null;
  hotel_priority?: string | null;
  pace?: string | null;
  place_selections?: AdminPlaceSelections | null;
  prefetch_reused?: boolean | null;
  discovery_status?: string | null;
}

export const BADCASE_ROOT_CAUSE_STATUSES = ["suspected", "verified", "rejected"] as const;
export const BADCASE_ANALYSIS_STATUSES = ["pending", "analyzed"] as const;
export const BADCASE_FIXED_STATUSES = ["unfixed", "fixed", "wontfix"] as const;
export const BADCASE_DETECTED_BY = ["rule", "human", "benchmark", "llm_judge"] as const;

export type BadcaseRootCauseStatus = (typeof BADCASE_ROOT_CAUSE_STATUSES)[number];
export type BadcaseAnalysisStatus = (typeof BADCASE_ANALYSIS_STATUSES)[number];
export type BadcaseFixedStatus = (typeof BADCASE_FIXED_STATUSES)[number];

export interface AdminBadcase {
  badcase_id: string;
  run_id: string;
  category: string;
  severity: string;
  symptom: string;
  expected: string;
  actual: string;
  suspected_root_cause: string | null;
  root_cause_status: string;
  detected_by: string;
  trace_refs: string[];
  analysis_status: string;
  fixed_status: string;
  introduced_in: string | null;
  fixed_in: string | null;
  created_at: string | null;
}

/** PATCH /admin/badcases/{id} 的请求体：只提交操作员真正改动的字段。 */
export interface AdminBadcasePatch {
  analysis_status?: BadcaseAnalysisStatus;
  fixed_status?: BadcaseFixedStatus;
  root_cause_status?: BadcaseRootCauseStatus;
  suspected_root_cause?: string;
}

export interface AdminBadcaseFacets {
  category: AdminRecord;
  severity: AdminRecord;
  analysis_status: AdminRecord;
}

export interface AdminBadcaseList {
  items: AdminBadcase[];
  total: number;
  limit: number;
  offset: number;
  facets: AdminBadcaseFacets;
}

export interface AdminRunDetail {
  run: AdminRunSummary & AdminRecord;
  metrics: AdminRunMetrics;
  progress: AdminRunProgress;
  trace: AdminTraceSpan[];
  decisions: AdminDecision[];
  provider_calls: AdminProviderCall[];
  jev_calls: AdminJevCall[];
  llm_calls: AdminLlmCall[];
  badcases: AdminBadcase[];
  /** 没有引导式来源时后端返回 null（要能优雅显示"这次不是引导式创建的"）。 */
  user_journey: AdminUserJourney | null;
}

/* ------------------------------ Benchmark ------------------------------ */

export const BENCHMARK_SUITES = [
  "basic",
  "hard",
  "badcase_regression",
  "provider_failure",
  "jev_decision",
  "live_smoke",
] as const;

export interface AdminBenchmarkRun {
  benchmark_run_id: string;
  suite: string;
  version: string;
  travelplan_commit: string | null;
  superharness_commit: string | null;
  model: string | null;
  fixture_version: string | null;
  jev_enabled: boolean;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  case_count: number | null;
  passed: number | null;
  failed: number | null;
  metrics: AdminRecord;
  notes: string | null;
}

export interface AdminBenchmarkRunList {
  items: AdminBenchmarkRun[];
  limit: number;
}

export interface AdminBenchmarkMetrics {
  /** 六个维度允许为 null 或整段缺失：缺失时页面显示 Partial 提示，而不是整页报错。 */
  hard_constraints: AdminRecord | null | undefined;
  plan_quality: AdminRecord | null | undefined;
  evidence: AdminRecord | null | undefined;
  provider: AdminRecord | null | undefined;
  performance: AdminRecord | null | undefined;
  jev: AdminRecord | null | undefined;
}

/** 六个评测维度：契约固定，顺序也固定，前端只负责贴标签。 */
export const BENCHMARK_DIMENSIONS = [
  { key: "hard_constraints", label: "硬性约束", description: "预算、时间窗、必去点等不可违背的条件" },
  { key: "plan_quality", label: "行程质量", description: "节奏、连贯性、可执行性" },
  { key: "evidence", label: "证据与来源", description: "来源覆盖、价格新鲜度、广告风险" },
  { key: "provider", label: "Provider 表现", description: "各数据源成功率与降级情况" },
  { key: "performance", label: "性能", description: "端到端耗时与调用次数" },
  { key: "jev", label: "Jev 决策", description: "决策质量、fallback 与低置信度比例" },
] as const;

export type BenchmarkDimensionKey = (typeof BENCHMARK_DIMENSIONS)[number]["key"];

export interface AdminBenchmarkCaseResult {
  case_id: string;
  suite: string;
  title: string;
  status: string;
  metrics: AdminRecord;
  detail: AdminRecord;
}

/**
 * Jev OFF / ON 基线对比。
 *
 * 历史后端返回的是扁平对象（没有 off / on / delta_pct 三个分组），
 * 因此三个分组都标成可选；展示层用 `Object.keys(x ?? {})` 兜底，缺一段就退化成空表。
 */
export interface AdminBenchmarkBaseline {
  off?: AdminRecord | null;
  on?: AdminRecord | null;
  delta_pct?: AdminRecord | null;
  off_generated_at?: string | null;
  on_generated_at?: string | null;
}

export interface AdminBenchmarkDetail {
  run: AdminBenchmarkRun;
  metrics: AdminBenchmarkMetrics;
  case_results: AdminBenchmarkCaseResult[];
  /** 没有对照时后端返回 null（也可能整段不带该字段）。 */
  baseline_compare: AdminBenchmarkBaseline | null | undefined;
}

export interface AdminBenchmarkLaunchRequest {
  suites?: string[];
  jev_enabled?: boolean;
  limit?: number;
  live?: boolean;
}

export interface AdminBenchmarkLaunchResult {
  benchmark_run_id: string;
  status: string;
}

/* ------------------------------ 配置与 Jev ------------------------------ */

export interface AdminConfig {
  config: AdminRecord;
  secret_configured: Record<string, boolean>;
  planner_tuning: AdminRecord;
  note: string;
  /** 允许编辑（非 Secret）的键名。后端没有时为空数组，页面只展示只读快照。 */
  editable_keys?: string[];
  /** 运行时覆盖：key → 覆盖值 / 更新时间 / 覆盖人。 */
  runtime_overrides?: Record<string, AdminConfigOverride>;
  /** 当前生效的取值（含运行时覆盖）。 */
  values?: AdminRecord;
}

export interface AdminConfigOverride {
  value: unknown;
  updated_at?: string | null;
  updated_by?: string | null;
}

/** PATCH /admin/config 的请求体：只提交被修改的键。 */
export interface AdminConfigPatch {
  values: Record<string, unknown>;
}

/** 配置分组：预算 / Jev / Planner 阈值 / 模型单价；未命中的落到「其他」。 */
export const ADMIN_CONFIG_GROUPS = [
  { key: "budget", label: "预算", description: "预算上限与超限处理策略" },
  { key: "jev", label: "Jev", description: "模型决策层的开关、配额与阈值" },
  { key: "planner", label: "Planner 阈值", description: "规划器的候选门槛与节奏限制" },
  { key: "price", label: "模型单价", description: "按每百万 token 计的成本单价" },
] as const;

/* --------------------------- 引导式会话 --------------------------- */

export interface AdminPlanningSessionSummary {
  session_id: string;
  status?: string | null;
  discovery_status?: string | null;
  origin?: string | null;
  destination?: string | null;
  start_date?: string | null;
  days?: number | null;
  travelers?: number | null;
  budget_total?: number | null;
  pace?: string | null;
  transport_priority?: string | null;
  hotel_priority?: string | null;
  must_count?: number | null;
  want_count?: number | null;
  reject_count?: number | null;
  run_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
}

export interface AdminPlanningSessionList {
  items: AdminPlanningSessionSummary[];
  limit: number;
  offset: number;
  total: number;
}

export interface AdminPlanningEvent {
  at?: string | null;
  event?: string | null;
  detail?: string | null;
}

/** 候选摘要形状由后端决定（可能是对象数组），展示层按 AdminRecord 兜底。 */
export type AdminPlanningCandidate = AdminRecord;

export interface AdminPlanningSessionDetail extends AdminPlanningSessionSummary {
  place_candidates?: AdminPlanningCandidate[] | null;
  transport_candidates?: AdminPlanningCandidate[] | null;
  hotel_candidates?: AdminPlanningCandidate[] | null;
  events?: AdminPlanningEvent[] | null;
  degradations?: string[] | null;
}

export interface AdminJevHealth {
  enabled: boolean;
  configured: boolean;
  status: string;
  latency_ms: number | null;
  quota: unknown;
  quota_source: string;
  fallback_count: number | null;
  calls: number | null;
  low_confidence: number | null;
  timeout: number | null;
  invalid_response: number | null;
  last_error: string | null;
}

/* ------------------------------ Evolution ------------------------------ */

export interface AdminEvolutionRunSummary {
  evolution_run_id: string;
  status: string;
  badcase_count: number | null;
  experience_count: number | null;
  decision: string | null;
  summary: string | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface AdminEvolutionOverview {
  enabled: boolean;
  pending_badcases: number;
  runs: AdminEvolutionRunSummary[];
  note: string;
}

export interface AdminEvolutionStep {
  step: string;
  detail: string;
}

export interface AdminExperience {
  experience_id: string;
  evolution_run_id: string;
  source_badcases: string[];
  failure_pattern: string;
  verified_root_cause: string;
  affected_module: string;
  recommended_change: string;
  before_metrics: AdminRecord | null;
  after_metrics: AdminRecord | null;
  decision: string;
  created_at: string | null;
}

export interface AdminEvolutionRunDetail {
  run: AdminEvolutionRunSummary & {
    steps: AdminEvolutionStep[];
    before_metrics: AdminRecord | null;
    after_metrics: AdminRecord | null;
  };
  experiences: AdminExperience[];
  badcases: AdminBadcase[];
}

export interface AdminEvolutionLaunchResult {
  evolution_run_id: string;
  status: string;
}
