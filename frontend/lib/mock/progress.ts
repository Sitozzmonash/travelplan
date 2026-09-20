import type { PlanningStep, PlanningStepState } from "@/types/api";

/** 规划步骤清单（FRONTEND_DESIGN §6）。接入 FastAPI 后由后端的进度事件驱动同一份清单。 */
export const PLANNING_STEPS: PlanningStep[] = [
  { id: "intent", label: "理解旅行需求" },
  { id: "transport_search", label: "查询机票 / 高铁" },
  { id: "transport_compare", label: "比较交通时间和价格" },
  { id: "hotel_search", label: "查询酒店" },
  { id: "social_research", label: "搜索小红书 / 抖音攻略" },
  { id: "poi_verify", label: "高德验证地点" },
  { id: "route_plan", label: "计算市内路线" },
  { id: "budget_check", label: "检查预算" },
  { id: "feasibility_check", label: "检查时间可行性" },
  { id: "compose", label: "生成最终行程" },
];

export function initialStepStates(): PlanningStepState[] {
  return PLANNING_STEPS.map((step) => ({ ...step, status: "waiting" }));
}

/** mock 版：每一步的事实与状态，接后端后由真实进度事件替换。 */
export const MOCK_STEP_RESULTS: Record<
  string,
  { status: PlanningStepState["status"]; facts: string[] }
> = {
  intent: {
    status: "success",
    facts: ["北京 → 成都 · 5 天 4 晚", "2 人 · 预算 ¥6,000", "偏好 美食 / 拍照 / 历史"],
  },
  transport_search: {
    status: "success",
    facts: ["找到 14 个交通方案", "途牛 8 个航班 · 12306 6 个车次"],
  },
  transport_compare: {
    status: "success",
    facts: ["过滤 6 个到达过晚的方案", "保留 8 个进入门到门比较"],
  },
  hotel_search: {
    status: "success",
    facts: ["找到 26 家酒店", "春熙路周边 9 家符合价格区间"],
  },
  social_research: {
    status: "success",
    facts: ["读取 38 条攻略内容", "提取 23 个地点"],
  },
  poi_verify: {
    status: "success",
    facts: ["高德验证 17 个地点", "4 个低可信候选被过滤"],
  },
  route_plan: {
    status: "warning",
    facts: ["完成 24 段路线计算", "1 段未返回，已按同区域均值估算"],
  },
  budget_check: {
    status: "success",
    facts: ["已知实时费用 ¥4,894", "预计总计 ¥5,668 / 预算 ¥6,000"],
  },
  feasibility_check: {
    status: "warning",
    facts: ["检测到 1 个时间冲突", "已自动修正 14:10 → 14:40"],
  },
  compose: {
    status: "success",
    facts: ["生成 5 天行程，共 25 个安排", "输出 plan.json / plan.md / audit_report.json"],
  },
};

/** 每步的模拟耗时（毫秒），只影响演示节奏。 */
export const MOCK_STEP_DELAY_MS: Record<string, number> = {
  intent: 620,
  transport_search: 900,
  transport_compare: 700,
  hotel_search: 820,
  social_research: 980,
  poi_verify: 780,
  route_plan: 860,
  budget_check: 640,
  feasibility_check: 720,
  compose: 820,
};
