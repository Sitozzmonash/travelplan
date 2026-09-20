import type { Decision, ProviderStatus } from "./plan";

/** 规划过程步骤状态（FRONTEND_DESIGN §6）。 */
export type StepStatus = "waiting" | "running" | "success" | "warning" | "error";

export interface PlanningStep {
  id: string;
  label: string;
  /** 该步骤做什么的一行说明，用于规划过程列表。 */
  description?: string;
  /** 该步骤旁展示的轻量事实，来自后端返回的进度事件。 */
  facts?: string[];
}

export interface PlanningStepState extends PlanningStep {
  status: StepStatus;
  /** 后端返回了前端清单之外的 stage_id 时置为 true，仍作为额外一行展示。 */
  unknown?: boolean;
}

/** createPlan 的进度事件：mock 由模拟驱动，接 FastAPI 后由真实状态驱动。 */
export interface PlanProgressEvent {
  step_id: string;
  status: StepStatus;
  facts?: string[];
  /** 步骤标题。已知 stage 由前端清单给出，未知 stage 由后端事件带来，避免多出一行无名记录。 */
  label?: string;
  description?: string;
}

/** 后端持久化的标准 run 状态；RUNNING 以外都是终态。 */
export type RunStatus = "RUNNING" | "SUCCESS" | "FAILED" | "DEGRADED" | "CANCELLED";

export interface RunStageProgress {
  stage_id: string;
  status: "WAITING" | "RUNNING" | "SUCCESS" | "WARNING" | "FAILED";
  message: string;
  started_at: string | null;
  finished_at: string | null;
  facts: Record<string, unknown>;
}

/** GET /api/v1/plans/{id}/status：真实 Workflow 节点状态，不是 UI 计时模拟。 */
export interface RunProgress {
  run_id: string;
  status: RunStatus;
  current_stage: string | null;
  message: string | null;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
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
