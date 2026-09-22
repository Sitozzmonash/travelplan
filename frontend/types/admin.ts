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
  total_tool_calls: number;
  provider_failures: number;
  badcase_open: number;
  badcase_total: number;
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
  tool_calls?: number | null;
  provider_failures?: number | null;
  badcase_count?: number | null;
  cost?: number | null;
  /** 后端新增：来源（quick / guided / cli / benchmark）与来源会话。 */
  source?: string | null;
  source_session_id?: string | null;
  /** 详情补齐的字段（列表不一定有）：开始时间。 */
  started_at?: string | null;
  /**
   * 列表补齐的行程质量（后端按 run 现算 plan_quality 合成）。
   * 这些字段是**可选**的：老后端不返回时页面显示「—」，不会因为缺一个字段就整页失败。
   */
  quality_score?: number | null;
  quality_grade?: AdminGrade | null;
  /** 现算质量失败的原因（如「无计划」）；有值时页面显示「—」并把原因放进 title。 */
  quality_error?: string | null;
  /** 结构化目的地（来自 plan.intent.destination，多个目的地用「、」连接）。 */
  destination?: string | null;
  /** 未解决的行程告警条数。 */
  warnings?: number | null;
  /** Bad Case 条数（= badcase_count 的别名，列表列用它语义更清楚）。 */
  issue_count?: number | null;
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
  tool_calls?: number | null;
  provider_failures?: number | null;
  badcase_count?: number | null;
  /** 成本：后端没有定价表（cost_source 为 null）时为 null。 */
  cost?: number | null;
  cost_currency?: string | null;
  /** `user_price` 表示按用户配置的每百万 token 单价算出；null 表示未配置。 */
  cost_source?: AdminCostSource | null;
  cost_breakdown?: AdminCostBreakdown | null;
  /**
   * 后端写入的性能摘要（Part M）。形状不固定：可能是
   * `{ total_duration_ms, transport_ms, …, provider_calls, cache_hits, prefetch_reused, fallback_count }`，
   * 也可能同名字段直接平铺在 `metrics.*` 上。展示层按「有就用、没有就按阶段推导」处理。
   */
  performance_summary?: AdminRecord | null;
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

export interface AdminLlmCall {
  tag: string;
  model?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  chars?: number | null;
  error?: string | null;
  started_at?: string | null;
  /** 逐次调用的 token 用量（后端把 `usage` 带进审计记录后才会有）；缺失时 UI 写「后端未返回 usage」。 */
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
}

/** 阶段详情里的模型调用摘要。 */
export interface AdminStageLlmCall {
  tag?: string | null;
  model?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  chars?: number | null;
  error?: string | null;
  /** 同 AdminLlmCall：逐次调用的 token 用量，后端未返回时为 null。 */
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
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

/**
 * 引导式会话里保存的结构化偏好。
 * 两种口径都要能接：旧接口返回 POI 名称数组，新接口按 MUST/WANT/REJECT 返回计数。
 */
export interface AdminPlaceSelections {
  must: string[] | number;
  want: string[] | number;
  reject: string[] | number;
}

/**
 * Discovery → 正式 Run 的单条线交接结论（`user_journey.discovery` 的一项）。
 * `handoff` 取 `reused` / `fallback_query` / `unavailable`（未登记取值原样展示）。
 */
export interface AdminJourneyHandoff {
  /** 后端给的中文标签（交通 / 酒店 / 攻略 / 地点）。 */
  label?: string | null;
  handoff?: string | null;
  prefetch_available?: boolean | null;
  queried_in_run?: boolean | null;
  resolved?: boolean | null;
  /** 该线的原始 Discovery 状态（OK / EMPTY / DEGRADED…）。 */
  status?: string | null;
  result_count?: number | null;
  duration_ms?: number | null;
  degraded?: boolean | null;
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
  /** 旧口径是布尔（这次 run 有没有复用 prefetch），新口径是每条线的布尔。 */
  prefetch_reused?: boolean | Record<string, boolean> | null;
  prefetch_reused_any?: boolean | null;
  /** Discovery 已经查到的线（transport / hotels / social / places）。 */
  prefetch_available?: string[] | null;
  prefetch_status?: string | null;
  discovery_status?: string | null;
  /** 每条线的交接结论；键为 transport / hotels / social / places。 */
  discovery?: Record<string, AdminJourneyHandoff> | null;
  /** 分阶段原始 Discovery 数据（status / duration_ms / result_count…）。 */
  prefetch_stages?: Record<string, AdminDiscoveryStage> | null;
  /** 开始规划时为等待 Discovery 实际等待的毫秒数（0 = 无需等待）。 */
  grace_waited_ms?: number | null;
  /** 本次 run 的动态偏好画像（与会话详情同源，run 侧由审计文件带回）。 */
  profile?: AdminDecisionProfile | null;
  /** 规划器选中的住宿区域；候选来源见 session 详情的 decision.hotel_areas。 */
  hotel_area_selected?: string | null;
  /** 住宿区域候选名列表。 */
  hotel_areas?: string[] | null;
  /** 用户标了 MUST 但最终没进计划的地点。 */
  must_missing?: string[] | null;
  /** 用户标了 REJECT 但最终进了计划的地点。 */
  rejected_in_plan?: string[] | null;
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
  /** 五个维度允许为 null 或整段缺失：缺失时页面显示 Partial 提示，而不是整页报错。 */
  hard_constraints: AdminRecord | null | undefined;
  plan_quality: AdminRecord | null | undefined;
  evidence: AdminRecord | null | undefined;
  provider: AdminRecord | null | undefined;
  performance: AdminRecord | null | undefined;
}

/** 五个评测维度：契约固定，顺序也固定，前端只负责贴标签。 */
export const BENCHMARK_DIMENSIONS = [
  { key: "hard_constraints", label: "硬性约束", description: "预算、时间窗、必去点等不可违背的条件" },
  { key: "plan_quality", label: "行程质量", description: "节奏、连贯性、可执行性" },
  { key: "evidence", label: "证据与来源", description: "来源覆盖、价格新鲜度、广告风险" },
  { key: "provider", label: "Provider 表现", description: "各数据源成功率与降级情况" },
  { key: "performance", label: "性能", description: "端到端耗时与调用次数" },
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
 * OFF / ON 基线对比。
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
  limit?: number;
  live?: boolean;
}

export interface AdminBenchmarkLaunchResult {
  benchmark_run_id: string;
  status: string;
}

/* ------------------------------ 配置 ------------------------------ */

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

/** 配置分组：预算 / Planner 阈值 / 模型单价；未命中的落到「其他」。 */
export const ADMIN_CONFIG_GROUPS = [
  { key: "budget", label: "预算", description: "预算上限与超限处理策略" },
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

/* --------------- 会话详情（GET /admin/planning-sessions/{id} 的分块信封） --------------- */

/**
 * 会话详情是一份「分块信封」：session / basic_info / preferences / preference_labels /
 * discovery / prefetch_summary / poi_selections / events / run_link。
 *
 * 为什么不是一个扁平大对象：后端任何一块没算出来（例如 Discovery 没跑完）时，
 * 前端只需让那一块降级成 Empty/Partial，其余块照常渲染，而不是整页 invalid。
 * 因此这里的每块都允许为 null，数组字段用空数组（[]）兜底。
 */

/** basic_intent：下游的原始意图（可能只有数字/字符串/哨兵值）。 */
export interface AdminPlanningSessionBasicIntent {
  origin?: string | null;
  destination?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  days?: number | null;
  travelers?: number | null;
  /** 可能是数字、哨兵字符串（auto/undecided/unlimited）或 null。 */
  budget_total?: number | string | null;
}

/**
 * 结构化偏好（preferences 块）。
 * 数值型字段保留哨兵字符串：`auto`=帮我选、`undecided`=不确定、`unlimited`=不限。
 */
export interface AdminPlanningSessionPreferences {
  transport_mode?: string | null;
  transport_priority?: string | null;
  transport_constraints?: string[];
  hotel_priority?: string | null;
  hotel_max_price_per_night?: number | string | null;
  hotel_min_rating?: number | string | null;
  hotel_min_star?: number | string | null;
  hotel_room_type?: string | null;
  hotel_allow_change?: boolean | string | null;
  pace?: string | null;
}

/** 后端给出的中文标签（preference_labels 块），前端优先用它而不是自己再翻译一遍。 */
export interface AdminPlanningSessionPreferenceLabels {
  transport_mode?: string | null;
  transport_priority?: string | null;
  transport_constraints?: string[];
  hotel_priority?: string | null;
  pace?: string | null;
}
/** 基础信息块：行程范围 + 会话的三个时间戳。 */
export interface AdminPlanningSessionBasicInfo {
  origin?: string | null;
  destination?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  days?: number | null;
  travelers?: number | null;
  budget_total?: number | string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
}

/**
 * 能力声明。`hotel_star_filter === false` 表示数据源不返回星级，
 * `hotel_star_note` 解释原因，展示层必须把它一起呈现，否则「星级不可用」会显得莫名其妙。
 */
export interface AdminPlanningSessionCapabilities {
  hotel_star_filter?: boolean | null;
  hotel_star_note?: string | null;
  hotel_max_price_filter?: boolean | null;
  hotel_rating_filter?: boolean | null;
}

export interface AdminPlanningSessionEvidenceSummary {
  sources_used?: number | null;
  places_verified?: number | null;
  total_candidates?: number | null;
  transport_candidates?: number | null;
  hotel_candidates?: number | null;
  evidence_count?: number | null;
}

export interface AdminPlanningSessionPlaceCategory {
  category: string;
  label?: string | null;
  count?: number | null;
}

/** session 块：列表行 + session_view 的全部字段（详情视图的基础数据来源）。 */
export interface AdminPlanningSessionBlock {
  session_id: string;
  status?: string | null;
  discovery_status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
  run_id?: string | null;
  error?: string | null;
  basic_intent?: AdminPlanningSessionBasicIntent | null;
  preferences?: AdminPlanningSessionPreferences | null;
  preference_labels?: AdminPlanningSessionPreferenceLabels | null;
  /** place_id → MUST | WANT | REJECT */
  poi_selections?: Record<string, string> | null;
  transport_candidates?: AdminPlanningCandidate[];
  hotel_candidates?: AdminPlanningCandidate[];
  place_candidates?: AdminPlanningCandidate[];
  place_categories?: AdminPlanningSessionPlaceCategory[];
  evidence_summary?: AdminPlanningSessionEvidenceSummary | null;
  degradations?: string[];
  events?: AdminPlanningEvent[];
  capabilities?: AdminPlanningSessionCapabilities | null;
}

/** Discovery 的单条线（transport / hotels / social / places）状态。 */
export interface AdminDiscoveryStage {
  status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  result_count?: number | null;
  degraded?: boolean | null;
  error?: string | null;
}

export interface AdminDiscoveryBlock {
  /** PENDING | RUNNING | READY | PARTIAL | FAILED（未登记取值原样显示） */
  overall?: string | null;
  stages?: Record<string, AdminDiscoveryStage>;
  degradations?: string[];
}

/** Prefetch 摘要：五个数字。 */
export interface AdminPrefetchSummary {
  transport_candidates?: number | null;
  hotel_candidates?: number | null;
  evidence_count?: number | null;
  place_candidates?: number | null;
  provider_calls?: number | null;
}

export interface AdminPoiSelectionCounts {
  must?: number | null;
  want?: number | null;
  reject?: number | null;
  neutral?: number | null;
}

export interface AdminPoiSelectionItem {
  place_id: string;
  name?: string | null;
  /** MUST | WANT | REJECT */
  state?: string | null;
}

export interface AdminPlanningSessionPoiSelections {
  counts?: AdminPoiSelectionCounts | null;
  items?: AdminPoiSelectionItem[];
}

/** Session → Run 的关联摘要。 */
export interface AdminRunLink {
  has_run: boolean;
  run_id?: string | null;
  run_status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

/**
 * 动态偏好画像（`decision.profile`）。
 * `source` 是关键：`llm` 表示模型真的生成了一份带信号的画像，`fallback` 表示模型没给出、
 * 用的是均衡基线 —— 后者仍然能出计划，但个性化程度有限，管理端必须能看出来。
 */
export interface AdminDecisionProfile {
  source?: string | null;
  travel_style?: string | null;
  hotel_area?: string | null;
  hotel?: AdminRecord | null;
  attraction?: AdminRecord | null;
  food?: AdminRecord | null;
  pace?: AdminRecord | null;
  reason?: string | null;
}

/** 住宿区域候选：规划器就是从这组候选里选一家，所以它决定了「住哪一片」。 */
export interface AdminHotelAreaCandidate {
  key: string | null;
  name: string | null;
  reason: string | null;
  tags: string[];
  fit_score: number | null;
}

export interface AdminSessionDecision {
  profile: AdminDecisionProfile | null;
  hotel_areas: AdminHotelAreaCandidate[];
  poi_pools: AdminRecord;
}

export interface AdminPlanningSessionDetail {
  session: AdminPlanningSessionBlock;
  basic_info: AdminPlanningSessionBasicInfo | null;
  preferences: AdminPlanningSessionPreferences | null;
  preference_labels: AdminPlanningSessionPreferenceLabels | null;
  discovery: AdminDiscoveryBlock | null;
  prefetch_summary: AdminPrefetchSummary | null;
  poi_selections: AdminPlanningSessionPoiSelections | null;
  events: AdminPlanningEvent[];
  run_link: AdminRunLink;
  /** 引导式决策块；旧部署可能整块缺失，此时为 null（页面显示「后端未返回」）。 */
  decision: AdminSessionDecision | null;
}

/* ------------------------------ Provider 健康 ------------------------------ */

/**
 * Provider 健康状态五态里的四态。
 *
 * `UNKNOWN` 的语义是「这段时间没有调用记录」，**不是**故障：
 * 它必须是中性灰，不能因为拿不到成功率就画成红色。
 */
export const PROVIDER_STATUSES = ["HEALTHY", "DEGRADED", "UNAVAILABLE", "UNKNOWN"] as const;

export type AdminProviderStatus = (typeof PROVIDER_STATUSES)[number];

export const PROVIDER_STATUS_LABELS: Record<AdminProviderStatus, string> = {
  HEALTHY: "健康",
  DEGRADED: "降级",
  UNAVAILABLE: "不可用",
  UNKNOWN: "未知",
};

/**
 * Provider 状态的中文标签。
 *
 * `UNKNOWN` 有两种来源：完全没被调用过（calls === 0），或最近样本太少、后端不下结论。
 * 两者都不是故障，但必须说清是哪一种 —— 否则会把「刚被调用过几次」误读成「从没调用过」。
 */
export function providerStatusLabel(
  status: AdminProviderStatus,
  calls: number | null | undefined,
): string {
  if (status !== "UNKNOWN") return PROVIDER_STATUS_LABELS[status];
  return (calls ?? 0) === 0 ? "无调用记录" : "样本不足";
}

/** 调用来源（source_type）。后端将来新增取值时原样显示，不做白名单丢弃。 */
export const PROVIDER_SOURCE_LABELS: Record<string, string> = {
  discovery: "Discovery 预取",
  run: "正式 Run",
  benchmark: "Benchmark",
};

export function providerSourceLabel(source: string | null | undefined): string {
  if (!source) return "未知来源";
  return PROVIDER_SOURCE_LABELS[source] ?? source;
}

/**
 * 单个 Provider 的聚合统计（最近 N 次真实调用）。
 * 所有字段都可能缺失：展示层统一降级成「—」，不放大成整页错误。
 */
export interface AdminProviderStats {
  provider?: string | null;
  calls?: number | null;
  successes?: number | null;
  failures?: number | null;
  timeouts?: number | null;
  auth_errors?: number | null;
  rate_limited?: number | null;
  empty?: number | null;
  fallback_count?: number | null;
  last_call_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  avg_latency_ms?: number | null;
  p95_latency_ms?: number | null;
  tools?: string[] | null;
  sources?: string[] | null;
  last_error?: string | null;
  last_status?: string | null;
  /** 0~1；后端在没有样本时返回 null —— 页面必须显示「—」而不是 0%。 */
  success_rate?: number | null;
  failure_rate?: number | null;
}

/** 总览卡片行：统计字段 + 展示字段（label / configured / status）。 */
export interface AdminProviderSummary extends AdminProviderStats {
  provider: string;
  label: string;
  /** 后端不一定认识这个 Provider，认不出时为 null。 */
  configured: boolean | null;
  status: AdminProviderStatus;
}

export interface AdminProviderListSummary {
  providers: number | null;
  healthy: number | null;
  degraded: number | null;
  unavailable: number | null;
  unknown: number | null;
  /** 处于 DEGRADED / UNAVAILABLE 的 Provider id。 */
  needs_attention: string[];
}

export interface AdminProviderList {
  items: AdminProviderSummary[];
  /** 后端没返回 summary 时为 null：页面显示 Partial，而不是整页失败。 */
  summary: AdminProviderListSummary | null;
  /** 统计口径说明；后端没返回时为空串。 */
  note: string;
}

/**
 * 单次 Provider 调用明细（Provider Health 详情）。
 * 注意与上面 AdminRunDetail 里的 `AdminProviderCall` 区分：那个来自 `sources` 表，
 * 这个来自 provider_calls 账本，字段口径不同。
 */
export interface AdminProviderHealthCall {
  call_id: string | null;
  provider: string | null;
  tool: string | null;
  status: string | null;
  source_type: string | null;
  source_id: string | null;
  run_id: string | null;
  session_id: string | null;
  fetched_at: string | null;
  duration_ms: number | null;
  /** 后端未固定形状：可能是条数、布尔或空。 */
  returned: unknown;
  error: string | null;
  fallback: boolean;
  /** 后端已脱敏；页面默认折叠，不把原始参数平铺在主视图。 */
  query: AdminRecord;
}

export interface AdminProviderDetail {
  provider: string;
  label: string;
  configured: boolean | null;
  status: AdminProviderStatus;
  stats: AdminProviderStats | null;
  calls: AdminProviderHealthCall[];
  note: string;
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

/* ==================================================================
 * 管理端聚合视图（对应 docs/product/ADMIN.md）
 *
 * 这五组契约服务四件事：首屏该看什么（Dashboard）、行程生成得好不好
 * （Travel Quality）、用户在哪一步流失（Funnel）、一堆 Bad Case 里
 * 哪几个才是真问题（聚类），以及一次 run 到底发生了什么（Timeline）。
 *
 * 与后端一致的两条约定：
 * - 后端拿不到的数一律 `| null`，前端显示「—」，**不显示 0**；
 * - 所有指标都按同一个时间窗算，环比字段由后端给，前端不做减法。
 * ================================================================== */

/* ------------------------------ 时间窗 ------------------------------ */

export type AdminWindowKey = "1h" | "24h" | "7d" | "30d";

export const ADMIN_WINDOW_OPTIONS: { key: AdminWindowKey; label: string }[] = [
  { key: "1h", label: "最近 1 小时" },
  { key: "24h", label: "最近 24 小时" },
  { key: "7d", label: "最近 7 天" },
  { key: "30d", label: "最近 30 天" },
];

export const DEFAULT_ADMIN_WINDOW: AdminWindowKey = "24h";

/** 质量等级：与后端 `_grade()` 同一套词表，避免两边各有一套「多少分算好」。 */
export type AdminGrade = "GOOD" | "FAIR" | "POOR" | "UNKNOWN";

export const ADMIN_GRADE_LABELS: Record<string, string> = {
  GOOD: "良好",
  FAIR: "一般",
  POOR: "差",
  UNKNOWN: "无数据",
};

/* ------------------------------ Dashboard ------------------------------ */

export type AdminKpiUnit = "ratio" | "ms" | "cny" | "count" | "score" | string;

export interface AdminKpi {
  key: string;
  label: string;
  value: number | null;
  unit: AdminKpiUnit | null;
  /** 等长上一周期的同一个指标；后端没有对照数据时为 null。 */
  previous: number | null;
  /** 与 value 同单位（比率指标就是百分点差）。 */
  delta: number | null;
  direction: "up" | "down" | "flat";
  higher_is_better: boolean;
  /** 这个方向是不是好事；方向为平或没有对照时为 null。 */
  better: boolean | null;
  hint: string | null;
  detail: string | null;
}

export interface AdminAttentionItem {
  id: string;
  level: "high" | "medium" | "low" | string;
  title: string;
  detail: string;
  /** 跳转目标：`{ kind, label, status?, category?, id?, focus? }`，形状由后端给、前端按 kind 渲染。 */
  action: AdminRecord;
  metric: AdminRecord;
}

export interface AdminTrendPoint {
  t: string;
  /** 该分桶没有样本时为 null（不是 0）—— 0 会被读成「真的是 0 次」。 */
  value: number | null;
}

export interface AdminTrendSeries {
  key: string;
  label: string;
  unit: AdminKpiUnit | null;
  points: AdminTrendPoint[];
  summary: {
    latest: number | null;
    min: number | null;
    max: number | null;
    avg: number | null;
  };
}

export interface AdminDashboardProviderRow extends AdminProviderStats {
  provider: string;
  calls: number;
  failures: number;
  timeouts: number;
  fallback_count: number;
  /** OK / DEGRADED / UNAVAILABLE / IDLE —— 阈值由后端统一定义。 */
  state: string;
  needs_attention: boolean;
}

export interface AdminDashboard {
  window: string;
  window_label: string;
  generated_at: string;
  kpis: AdminKpi[];
  statuses: AdminRecord;
  volume: {
    runs: number;
    previous_runs: number;
    finished: number;
    previous_finished: number;
    running: number;
  };
  trends: AdminTrendSeries[];
  attention: AdminAttentionItem[];
  cost: {
    total: number | null;
    avg_per_run: number | null;
    previous_avg_per_run: number | null;
    currency: string;
    price_configured: boolean;
  };
  quality: {
    score: number | null;
    grade: AdminGrade;
    sampled_runs: number;
    /** true 表示窗口内运行比抽样数更多，分数只代表抽样。 */
    sampled: boolean;
  };
  /**
   * 决策健康：一组回答"软决策到底有没有生效"的指标，全部来自抽样运行的既有事实，
   * 不做任何推断。
   */
  decision_health: {
    runs: number;
    guided_runs: number;
    profile_llm: number;
    profile_fallback: number;
    /** 既不是 llm 也不是 fallback：Quick 模式本来就没有画像。 */
    profile_missing: number;
    /** 分母是引导式运行；没有引导式运行时为 null。 */
    profile_success_rate: number | null;
    runs_with_hard_errors: number;
    runs_with_rejected_in_plan: number;
    runs_missing_must: number;
    note: string;
  };
  badcases: {
    open: number;
    window: number;
    previous: number;
    by_severity: AdminRecord;
  };
  providers: {
    total: number;
    unhealthy: number;
    /** 成功率的统计口径说明（按每个 Provider 最近 N 次调用，不是时间窗）。 */
    window_note: string;
    items: AdminDashboardProviderRow[];
  };
  benchmark: { latest_run_id: string | null; latest_pass_rate: number | null };
  evolution: { enabled: boolean; last_run_id: string | null; last_decision: string | null };
  /** 口径/数据缺口说明，页面必须原样展示（例如「未配置模型单价」）。 */
  notes: string[];
}

/* ------------------------------ Travel Quality ------------------------------ */

export interface AdminQualityMetric {
  key: string;
  label: string;
  /** 可能是数字、比率、对象（如告警码分布）或 null。 */
  value: unknown;
  unit: string | null;
  hint: string | null;
}

export interface AdminQualityFinding {
  level: "high" | "medium" | "low" | string;
  text: string;
}

export interface AdminQualityOffender {
  run_id: string;
  detail: string;
  value: number | null;
}

export interface AdminQualityDimension {
  key: string;
  label: string;
  /** 0~1；数据不足时为 null（显示「无数据」而不是 0 分）。 */
  score: number | null;
  score_percent: number | null;
  grade: AdminGrade;
  grade_label: string;
  metrics: AdminQualityMetric[];
  findings: AdminQualityFinding[];
  offenders: AdminQualityOffender[];
  note: string | null;
}

export interface AdminQualityRun {
  run_id: string;
  created_at: string | null;
  status: string;
  destination: string | null;
  days: number | null;
  score: number | null;
  grade: AdminGrade;
  profile_source: string | null;
  hotel_area: string | null;
  preference_coverage: number | null;
  warnings: number | null;
  errors: number | null;
  route_verified_rate: number | null;
  cost: number | null;
  duration_ms: number | null;
}

export interface AdminQualityDestination {
  name: string;
  runs: number;
  score: number | null;
  grade: AdminGrade;
  degraded: number;
  failed: number;
}

export interface AdminTravelQuality {
  window: string;
  window_label: string;
  generated_at: string;
  scan: { runs_in_window: number; finished: number; scored: number; unscored: number };
  overall: {
    score: number | null;
    grade: AdminGrade;
    grade_label: string;
    weights: AdminRecord;
    note: string;
  };
  dimensions: AdminQualityDimension[];
  runs: AdminQualityRun[];
  destinations: AdminQualityDestination[];
  quality_trend: AdminTrendSeries;
  notes: string[];
}

export function findQualityDimension(
  payload: AdminTravelQuality | null,
  key: string,
): AdminQualityDimension | null {
  if (!payload) return null;
  return payload.dimensions.find((dimension) => dimension.key === key) ?? null;
}

/* ------------------------------ Planning Funnel ------------------------------ */

export interface AdminFunnelStage {
  key: string;
  label: string;
  count: number;
  /** 占第一步的比例（0~1）。 */
  rate_of_total: number | null;
  dropped: number;
  /** 相对上一步的流失率（0~1）；第一步为 null。 */
  drop_rate: number | null;
  previous_count: number;
  previous_dropped: number;
}

export interface AdminFunnelBreakdownRow {
  key: string;
  sessions: number;
  /** 与 stages 一一对应的各阶段人数（保证单调不增）。 */
  stages: number[];
  plan_success: number;
  conversion: number | null;
}

export interface AdminFunnel {
  window: string;
  window_label: string;
  generated_at: string;
  stages: AdminFunnelStage[];
  sessions: number;
  previous_sessions: number;
  overall_conversion: number | null;
  biggest_drop: {
    from: string;
    to: string;
    from_key: string;
    to_key: string;
    drop_rate: number;
    dropped: number;
  } | null;
  /** 当前拆解用的维度（与 dimension_options 的 key 对应）。 */
  destination_dimension: string;
  breakdown: Record<string, AdminFunnelBreakdownRow[]>;
  dimension_options: { key: string; label: string }[];
  notes: string[];
}

/* ------------------------------ Bad Case 聚类 ------------------------------ */

export interface AdminBadcaseGroup {
  key: string;
  category: string;
  severity: string;
  occurrence_count: number;
  previous_count: number;
  /** 与上一周期相比的变化率；上一周期为 0 时为 null（不是 100%）。 */
  trend_pct: number | null;
  affected_runs: number;
  first_seen: string | null;
  last_seen: string | null;
  last_seen_ago_seconds: number | null;
  /** 该找哪个模块修；后端没登记这个 category 时为 null。 */
  related_module: string | null;
  related_providers: string[];
  pending_analysis: number;
  unfixed: number;
  top_symptoms: { text: string; count: number }[];
  sample_ids: string[];
  sample_runs: string[];
  sparkline: AdminTrendPoint[];
}

export interface AdminBadcaseGroups {
  window: string;
  window_label: string;
  generated_at: string;
  totals: {
    badcases: number;
    previous_badcases: number;
    groups: number;
    affected_runs: number;
    pending: number;
    open_total: number;
  };
  groups: AdminBadcaseGroup[];
  facets: AdminBadcaseFacets;
  notes: string[];
}

/* ------------------------------ Run Timeline ------------------------------ */

export type AdminTraceEventType =
  | "SYSTEM"
  | "USER"
  | "CONTEXT"
  | "ASSISTANT"
  | "TOOL"
  | "PROVIDER"
  | "VALIDATION"
  | "ERROR";

export interface AdminTraceBadcaseBrief {
  badcase_id: string;
  category: string | null;
  severity: string | null;
  symptom: string | null;
  analysis_status: string | null;
}

export interface AdminTraceEvent {
  event_id: string;
  run_id: string;
  /** 所属 workflow 阶段的序号；挂不上阶段时为 null。 */
  round_index: number | null;
  event_type: AdminTraceEventType | string;
  event_type_label: string;
  stage: string | null;
  title: string;
  summary: string | null;
  input_preview: string | null;
  output_preview: string | null;
  status: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  parent_event_id: string | null;
  model: string | null;
  provider: string | null;
  tool: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost: number | null;
  cache_hit: boolean | null;
  prefetch_reused: boolean | null;
  fallback: boolean | null;
  metadata: AdminRecord;
  /** 口径说明（例如「模型输入正文未持久化」），展开详情时展示。 */
  notes: string[];
  badcases: AdminTraceBadcaseBrief[];

  // ---- 模型交互预览（后端 A13：**已脱敏 + 已截断**，前端只负责不做二次泄露）----
  // 为什么单独一组而不是复用 input_preview / output_preview：一次 ASSISTANT 调用是
  // 「模型收到了什么（system + user + context）→ 模型返回了什么（assistant）」两件事，
  // 现有 input/output_preview 回答不了「Dynamic Preference 有没有进 prompt」
  // 「Tool Result 有没有进下一轮上下文」这两个坏 Case 排查里最常问的问题。
  // 全部可选：后端契约分批落地，缺字段时前端写「—」并说明「后端未返回」，绝不前端假造正文。
  /** 脱敏截断后的系统提示预览（SYSTEM 事件与 ASSISTANT 调用上都有意义）。 */
  system_preview?: string | null;
  /** 脱敏截断后的用户输入预览（USER 事件与 ASSISTANT 调用）。 */
  user_preview?: string | null;
  /** 脱敏截断后注入的上下文预览（CONTEXT 事件与 ASSISTANT 调用）；动态偏好是否传给模型看这里。 */
  context_preview?: string | null;
  /** 脱敏截断后的模型返回预览（ASSISTANT 事件）。 */
  assistant_preview?: string | null;
  /** Prompt 模板版本 / 内容哈希：用来回答「Prompt 给错了吗」。 */
  prompt_version?: string | null;
  prompt_hash?: string | null;
  /** 这一次调用自己的 token 用量（后端 A12 把 `usage` 带进审计记录后才有）。 */
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_tokens?: number | null;
}

export interface AdminTimelineTrackBlock {
  event_id: string;
  title: string;
  status: string | null;
  stage: string | null;
  /** 相对 run 开始时刻的偏移；没有起点时为 null。 */
  start_ms: number | null;
  duration_ms: number | null;
  badcase_count: number;
}

export interface AdminTimelineTrack {
  key: string;
  label: string;
  count: number;
  blocks: AdminTimelineTrackBlock[];
}

export interface AdminTimelineStage {
  stage_id: string;
  title: string;
  status: string | null;
  ordinal: number;
  started_at: string | null;
  finished_at: string | null;
}

/* ------------------------------ Agent Loop 步骤流 ------------------------------ */
/*
 * 主规划已从「固定 12 步工作流」换成 SuperHarness Agent Loop（`app/agent_runner.py`）：
 * Agent 在循环里自己调工具、最后交卷出计划。这一路的事实形状与固定流程不同，
 * 所以后端额外给了一份 `agent` 块 —— 后端只读聚合（`app/admin_timeline.py::_agent_block`），
 * 前端不重算、不拼接，缺字段一律按「后端没给」渲染。
 */

/** 一次模型调用的 token 四项；取不到的那项是 null（不是 0）。 */
export interface AdminAgentStepTokens {
  input: number | null;
  output: number | null;
  cached: number | null;
  total: number | null;
}

/** Agent 的一步：一次真实调用（工具 span 或模型 span），不是 run_stages 的一行。 */
export interface AdminAgentStep {
  /** 展示序号，1 开始，按真实发生顺序（started_at，同一时刻用 seq 兜底）。 */
  order: number;
  kind: "tool" | "model" | string;
  /** span 上记的序号：工具是「该工具的第几次调用」，模型是全局第几次模型调用。 */
  seq: number | null;
  span_id: string;
  /** 与时间轴事件的 id 相同（TOOL / ASSISTANT 事件的 event_id 就是 span_id），可跳转。 */
  event_id: string;
  /** 中文步骤名（= run_stages.stage_id，如「在查酒店」）；挂不上时为 null。 */
  stage: string | null;
  stage_title: string | null;
  /** 机器可读的步骤身份（`tool:search_hotels:2` / `llm:gpt-4o`）。 */
  step_key: string | null;
  tool: string | null;
  model: string | null;
  provider: string | null;
  status: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  query: string | null;
  note: string | null;
  /** 工具返回条数（对齐上的 Provider 账本行或 span 属性）；取不到为 null。 */
  returned: unknown;
  error: string | null;
  /** 只有模型步有 token；工具步是 null（它不消耗模型 token）。 */
  tokens: AdminAgentStepTokens | null;
  /** 到这一步为止的累计 token（模型 span 上带的 cumulative_total_tokens）。 */
  cumulative_total_tokens: number | null;
}

/** Agent 运行元信息 + 步骤流 + 整 run token 汇总。 */
export interface AdminAgentInfo {
  /** 这次 run 是不是 Agent Loop 出的计划；false 时走固定 12 步的旧展示口径。 */
  is_agent_run: boolean;
  prompt_version: string | null;
  system_prompt_chars: number | null;
  /** 这次 run 里对模型可见的工具名（trace 的 prompt_version span 上记着）。 */
  tools: string[];
  /** `submit_final_plan`（交卷）或 `message_json`（兜底解析）；两者都取不到时为 null。 */
  plan_origin: string | null;
  /** plan_origin 的中文一句话；后端没给标签时为 null。 */
  plan_origin_label: string | null;
  /** plan_origin 的来源：audit_report.json（权威）/ trace_spans（反推）。 */
  plan_origin_source: string | null;
  /** 循环为什么被截断（超时 / 超步数 / 成本护栏）；没有或不详时为 null。 */
  truncation: string | null;
  limits: {
    max_steps: number | null;
    timeout_seconds: number | null;
    /** 上限是否来自本次 run 的审计快照；false 表示取自「当前配置」，未必是当次的值。 */
    from_audit: boolean;
    note: string;
  };
  steps: AdminAgentStep[];
  tokens: {
    input: number | null;
    output: number | null;
    cached: number | null;
    total: number | null;
    llm_calls: number | null;
    tool_calls: number | null;
    duration_ms: number | null;
    /** 汇总口径来源（当前只有 run_metrics）。 */
    source: string;
    /** 逐条 span 相加的自算值：用来核对「汇总 vs 明细」。 */
    step_total: number | null;
    steps_llm: number;
    steps_tool: number;
  };
  notes: string[];
}

export interface AdminTimeline {
  run_id: string;
  generated_at: string;
  run: {
    run_id: string;
    status: string | null;
    source: string | null;
    original_query: string | null;
    created_at: string | null;
    started_at: string | null;
    finished_at: string | null;
    duration_ms: number | null;
    cost: number | null;
    total_tokens: number | null;
    badcase_count: number | null;
    error: string | null;
  };
  summary: {
    events: number;
    by_type: AdminRecord;
    errors: number;
    fallbacks: number;
    badcases: number;
    stages: number;
    /** Agent 步骤数（工具 span + 模型 span）。 */
    agent_steps?: number;
  };
  stages: AdminTimelineStage[];
  tracks: AdminTimelineTrack[];
  events: AdminTraceEvent[];
  badcases: AdminTraceBadcaseBrief[];
  /**
   * Agent Loop 的步骤流 / 每步 token / 总 token / 元信息。
   * 可选：后端契约是分批落地的，旧后端不返回这个键时页面必须能优雅降级。
   */
  agent?: AdminAgentInfo | null;
  /** 本次 run 的行程质量画像（与质量页同一份口径/同一份缓存）。 */
  quality: {
    score: number | null;
    grade: AdminGrade;
    /** `plan_quality()` 的 14 个原始字段；没有可解析的计划时为 null。 */
    metrics: AdminRecord | null;
    /** 现算失败的原因（如「无计划」）；有值时 score 一定为 null。 */
    error: string | null;
  };
  type_options: { key: string; label: string }[];
  notes: string[];
}
