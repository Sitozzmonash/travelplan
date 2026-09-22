import type { Decision, ProviderStatus } from "./plan";

/** 规划过程步骤状态（FRONTEND_DESIGN §6）。 */
export type StepStatus = "waiting" | "running" | "success" | "warning" | "error";

export interface PlanningStep {
  id: string;
  label: string;
  /** 该步骤做什么的一行说明，用于规划过程列表。 */
  description?: string;
  /** 该步骤旁展示的轻量事实（错误说明等），来自后端返回的进度。 */
  facts?: string[];
}

/**
 * 运行中展示的一行步骤 = 后端 ``run_stages`` 里真实发生过的一步。
 *
 * Agent Loop 之后步骤不再来自前端的固定清单：``stage_id`` 本身就是
 * 「在查机票」这类中文展示名（后端 ``app/agent_trace.py`` 把展示名当 stage_id 写库），
 * 所以这里只做「后端事实 → 可渲染行」的翻译，不含任何写死的步骤名与顺序。
 */
export interface PlanningStepState extends PlanningStep {
  status: StepStatus;
  /** 工具步骤的机器名（fact.tool），例如 ``search_flights``；模型思考步骤没有它。 */
  tool?: string;
  /** 模型思考步骤的模型名（fact.model）。有它就说明这步在「想」而不是在查。 */
  model?: string;
  /** 这一步实际耗时（毫秒）。进行中或后端未记录时为 undefined。 */
  durationMs?: number;
  /** 后端给这一步的说明（``run_stages.message``）；与展示名相同则不再重复显示。 */
  message?: string;
  /** 真实开始时刻（fact.started 优先，回落 started_at），用于排序与展示。 */
  startedAt?: string | null;
  finishedAt?: string | null;
}

/** 后端持久化的标准 run 状态；RUNNING 以外都是终态。 */
export type RunStatus = "RUNNING" | "SUCCESS" | "FAILED" | "DEGRADED" | "CANCELLED";

export interface RunStageProgress {
  /**
   * 步骤标识。Agent Loop 下**它就是中文展示名**（「在查机票」「在思考行程」），
   * 由后端 ``app/agent_trace.py`` 写入；同一工具多次调用共用同一行（主键 run_id + stage_id）。
   */
  stage_id: string;
  status: "WAITING" | "RUNNING" | "SUCCESS" | "WARNING" | "FAILED";
  /** 人类可读说明，正常路径等于 stage_id。 */
  message: string;
  started_at: string | null;
  finished_at: string | null;
  /** 工具事实：tool / status / duration_ms / started / finished / seq / stage_key / query / note / error。 */
  facts: Record<string, unknown>;
}

/**
 * GET /api/v1/plans/{id}/status：Agent 实际做到哪一步的真实状态，不是 UI 计时模拟。
 * 步骤顺序由 Agent 自己决定，因此这里给的是「已经发生的事实」，不是「待办清单」。
 */
export interface RunProgress {
  run_id: string;
  status: RunStatus;
  /** 当前步骤的中文展示名（如「在查酒店」）；run 刚创建、还没开始任何步骤时为 null。 */
  current_stage: string | null;
  message: string | null;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  /** 终态为 FAILED / CANCELLED 时，这里是后端给出的失败原因。 */
  error: string | null;
  stages: RunStageProgress[];
}

export interface AuditStage {
  id: string;
  title: string;
  /** 一行式链路，例如「查到 14 个候选 → 过滤 6 个 → 比较 8 个 → 选择 CA4102」 */
  summary: string;
  steps: string[];
  stats?: { label: string; value: string }[];
}

export interface ProviderCall {
  provider: string;
  tool: string;
  query: Record<string, unknown>;
  status: ProviderStatus | string;
  returned: number | null;
  duration_ms?: number | null;
  fetched_at?: string | null;
  source_id?: string | null;
  note?: string | null;
}

export interface AuditEvent {
  at: string;
  stage: string;
  text: string;
}

export interface ArtifactRef {
  label: string;
  filename: string;
  format: string;
  available: boolean;
  note?: string;
}

/** GET /api/v1/plans/{id}/audit 的返回结构（产品化版本，非工程 trace 全量）。 */
export interface AuditReport {
  run_id: string;
  query: string;
  generated_at: string;
  stages: AuditStage[];
  decisions: Decision[];
  provider_calls: ProviderCall[];
  timeline: AuditEvent[];
  artifacts: ArtifactRef[];
}

export interface CreatePlanResult {
  run_id: string;
  status: RunStatus;
  plan: import("./plan").TripPlan | null;
  message?: string | null;
  /** 运行中的降级项（stages 里 WARNING / FAILED 的说明），DEGRADED 时用于向用户解释。 */
  degradations?: string[];
}

export interface ReviseRequest {
  action: "replace" | "remove" | "lock" | "relax_day" | "lower_budget";
  day_index?: number;
  item_id?: string;
}

export interface ReviseResult {
  run_id: string;
  status: "queued" | "completed";
  message: string;
}
