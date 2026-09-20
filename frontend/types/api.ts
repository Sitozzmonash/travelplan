import type { Decision, ProviderStatus } from "./plan";

/** 规划过程步骤状态（FRONTEND_DESIGN §6）。 */
export type StepStatus = "waiting" | "running" | "success" | "warning" | "error";

export interface PlanningStep {
  id: string;
  label: string;
  /** 该步骤旁展示的轻量事实，来自后端返回的进度事件。 */
  facts?: string[];
}

export interface PlanningStepState extends PlanningStep {
  status: StepStatus;
}

/** createPlan 的进度事件：mock 由模拟驱动，接 FastAPI 后由真实状态驱动。 */
export interface PlanProgressEvent {
  step_id: string;
  status: StepStatus;
  facts?: string[];
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
  status: "completed" | "running" | "failed";
  plan: import("./plan").TripPlan | null;
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
