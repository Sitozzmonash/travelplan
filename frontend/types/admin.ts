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
  original_query: string;
  created_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  total_tokens: number | null;
  llm_calls: number | null;
  jev_calls: number | null;
  tool_calls: number | null;
  provider_failures: number | null;
  badcase_count: number | null;
  cost: number | null;
}

export interface AdminRunList {
  items: AdminRunSummary[];
  limit: number;
  offset: number;
  total: number;
}

export interface AdminRunMetrics {
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  total_tokens: number | null;
  llm_calls: number | null;
  jev_calls: number | null;
  tool_calls: number | null;
  provider_failures: number | null;
  badcase_count: number | null;
  cost: number | null;
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
  decision_type: string;
  status: string;
  choice: string | null;
  confidence: number | null;
  latency_ms: number | null;
  model: string | null;
  fallback: boolean;
  fallback_reason: string | null;
  quota: unknown;
  input_summary: string | null;
  criteria: unknown;
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
  badcases: AdminBadcase[];
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

export interface AdminBenchmarkBaseline {
  off: AdminRecord;
  on: AdminRecord;
  delta_pct: AdminRecord;
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
  secret_configured: { jev: boolean; admin: boolean };
  planner_tuning: AdminRecord;
  note: string;
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
