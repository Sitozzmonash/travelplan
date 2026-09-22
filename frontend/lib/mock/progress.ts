import type { RunProgress, RunStageProgress, RunStatus } from "@/types/api";

/**
 * 演示模式（``NEXT_PUBLIC_USE_MOCK_API=true``）用的模拟进度。
 *
 * 这里**不再有「固定 12 步」的清单**：主规划已经改成 SuperHarness Agent Loop，
 * 步骤顺序由 Agent 自己决定。所以演示数据也必须同构地表达「Agent 依次调了哪些工具」，
 * 否则演示模式下前端会渲染出一份真实后端永远不会产生的进度。
 *
 * 与真实后端对齐的三条约定（见 ``app/agent_trace.py``）：
 *   1. ``stage_id`` 就是中文展示名（「在查机票」），不是机器码；
 *   2. 同一个 stage_id 只占一行，重复调用覆盖同一行的 facts（写库是 ON CONFLICT DO UPDATE）；
 *   3. facts 里带 ``tool / status / duration_ms / started / finished / seq / stage_key``。
 */
export interface MockAgentStep {
  /** 步骤展示名，同时也是后端的 stage_id。 */
  stageId: string;
  /** 工具名；模型思考步骤为 null（对应 facts.model）。 */
  tool: string | null;
  model?: string;
  /** 这一步做了什么（写进 facts.note，用于演示「事实」区）。 */
  note: string;
  /** 收口状态：正常 SUCCESS，拿到缓存或部分数据时 WARNING。 */
  status: "SUCCESS" | "WARNING";
  /** 该步耗时，仅用于演示展示与排序。 */
  durationMs: number;
  /** 模拟节奏：这一步「跑」多久。 */
  delayMs: number;
}

/**
 * 一次演示 run 的步骤序列。
 * 顺序刻意不是「先交通后酒店」的教科书顺序：Agent 会先想一轮、再按需查，
 * 中途还可能停下来再想一次（同一个「在思考行程」出现两次，覆盖同一行）。
 */
export const MOCK_AGENT_STEPS: MockAgentStep[] = [
  {
    stageId: "在思考行程",
    tool: null,
    model: "claude-sonnet",
    note: "拆解需求，决定先确认城际交通",
    status: "SUCCESS",
    durationMs: 2400,
    delayMs: 700,
  },
  {
    stageId: "在查火车票",
    tool: "search_trains",
    note: "北京 → 成都 · 6 个车次",
    status: "SUCCESS",
    durationMs: 3100,
    delayMs: 800,
  },
  {
    stageId: "在查机票",
    tool: "search_flights",
    note: "北京 → 成都 · 8 个航班",
    status: "SUCCESS",
    durationMs: 4200,
    delayMs: 850,
  },
  {
    stageId: "在查酒店",
    tool: "search_hotels",
    note: "春熙路周边 9 家符合价格区间",
    status: "SUCCESS",
    durationMs: 5200,
    delayMs: 900,
  },
  {
    stageId: "在翻小红书攻略",
    tool: "search_xiaohongshu",
    note: "读到 20 条攻略内容",
    status: "SUCCESS",
    durationMs: 2600,
    delayMs: 750,
  },
  {
    stageId: "在搜周边地点",
    tool: "search_poi",
    note: "提取 19 个唯一地点",
    status: "SUCCESS",
    durationMs: 3300,
    delayMs: 780,
  },
  {
    stageId: "在算路上时间",
    tool: "route",
    note: "24 段路线，1 段未返回已按同区域均值估算",
    status: "WARNING",
    durationMs: 6100,
    delayMs: 900,
  },
  {
    stageId: "在思考行程",
    tool: null,
    model: "claude-sonnet",
    note: "把地点排进每天的时段并复核预算",
    status: "SUCCESS",
    durationMs: 5400,
    delayMs: 900,
  },
  {
    stageId: "在查景区门票",
    tool: "search_scenic_tickets",
    note: "返回缓存价格，出行前需再次确认",
    status: "WARNING",
    durationMs: 2100,
    delayMs: 700,
  },
];

/** 演示 run 的起始时刻（相对 now 往前推，让时间线看起来是刚发生的）。 */
function stepTimestamps(): { started: string; finished: string }[] {
  const total = MOCK_AGENT_STEPS.reduce((sum, step) => sum + step.durationMs, 0);
  let clock = Date.now() - total;
  return MOCK_AGENT_STEPS.map((step) => {
    const started = new Date(clock).toISOString();
    clock += step.durationMs;
    return { started, finished: new Date(clock).toISOString() };
  });
}

const TIMESTAMPS = stepTimestamps();

/**
 * 生成某一时刻的 ``RunProgress`` 快照：``cursor`` 之前的步骤已完成，``cursor`` 这一步正在跑。
 * ``cursor`` 超出范围时按「全部完成」处理。
 */
export function mockRunProgress(runId: string, cursor: number, status: RunStatus = "RUNNING"): RunProgress {
  const order: string[] = [];
  const rows = new Map<string, RunStageProgress>();
  const last = Math.max(0, Math.min(cursor, MOCK_AGENT_STEPS.length - 1));

  MOCK_AGENT_STEPS.forEach((step, index) => {
    if (index > last) return;
    const running = status === "RUNNING" && index === last;
    const { started, finished } = TIMESTAMPS[index];
    const row: RunStageProgress = {
      stage_id: step.stageId,
      status: running ? "RUNNING" : step.status,
      message: step.stageId,
      started_at: started,
      finished_at: running ? null : finished,
      facts: {
        ...(step.tool ? { tool: step.tool } : { model: step.model ?? "llm" }),
        status: running ? "RUNNING" : step.status,
        duration_ms: running ? null : step.durationMs,
        started,
        finished: running ? null : finished,
        seq: index + 1,
        stage_key: step.tool ? `tool:${step.tool}` : `llm:${step.model ?? "llm"}`,
        note: step.note,
        error: null,
      },
    };
    // 同一 stage_id 只占一行：后来的调用覆盖 facts，但插入位置不变（与后端主键语义一致）。
    if (!rows.has(step.stageId)) order.push(step.stageId);
    rows.set(step.stageId, row);
  });

  const currentStage = order.length ? rows.get(order[order.length - 1])!.stage_id : null;
  return {
    run_id: runId,
    status,
    current_stage: status === "RUNNING" ? currentStage : null,
    message: currentStage,
    started_at: TIMESTAMPS[0].started,
    updated_at: new Date().toISOString(),
    finished_at: status === "RUNNING" ? null : new Date().toISOString(),
    error: null,
    stages: order.map((id) => rows.get(id)!),
  };
}

/** 演示 run 全部完成后的快照（含终态）。 */
export function mockCompletedProgress(runId: string, status: RunStatus = "SUCCESS"): RunProgress {
  const progress = mockRunProgress(runId, MOCK_AGENT_STEPS.length, status);
  return { ...progress, current_stage: null, message: "规划已结束" };
}
