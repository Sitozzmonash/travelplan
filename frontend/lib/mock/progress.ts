import type { PlanningStep, PlanningStepState } from "@/types/api";

/**
 * 规划步骤清单（FRONTEND_DESIGN §6）。
 *
 * 顺序与 stage_id 必须与后端 Workflow 的 12 个真实节点一致：进度事件按 stage_id 更新状态，
 * 前端不再自己编造步骤。mock 与真实后端共用这同一份定义。
 */
export const PLANNING_STEPS: PlanningStep[] = [
  {
    id: "parse_intent",
    label: "理解旅行需求",
    description: "解析出发地、日期、天数、人数、预算与偏好。",
  },
  {
    id: "search_intercity_transport",
    label: "查询城际交通",
    description: "查询机票与高铁车次及实时票价。",
  },
  {
    id: "search_hotels",
    label: "查询酒店",
    description: "按区域、价格与评分查询住宿候选。",
  },
  {
    id: "search_social_guides",
    label: "搜索攻略内容",
    description: "检索小红书与抖音内容。",
  },
  {
    id: "extract_and_normalize_places",
    label: "提取并归并地点",
    description: "合并同义写法，得到唯一地点列表。",
  },
  {
    id: "verify_poi_and_routes",
    label: "校验地点与路线",
    description: "用高德核验 POI 坐标与通勤路线。",
  },
  {
    id: "score_candidates",
    label: "评估候选可信度",
    description: "按可信度与广告风险排序候选。",
  },
  {
    id: "build_initial_plan",
    label: "生成初版行程",
    description: "把交通、住宿与地点排进每天的时段。",
  },
  {
    id: "check_budget",
    label: "检查预算",
    description: "区分实时价格与估算费用并核对总额。",
  },
  {
    id: "check_feasibility",
    label: "检查时间可行性",
    description: "校验通勤与停留时间是否走得完。",
  },
  {
    id: "critic_and_revise",
    label: "复核并修正",
    description: "对时间冲突与折返做一轮修正。",
  },
  {
    id: "finalize",
    label: "输出最终行程",
    description: "产出 plan.json / plan.md / audit_report.json。",
  },
];

/**
 * 后端新增了前端清单里还没有的 stage 时，用它兜底出一个可读标题：
 * 宁可多展示一行原始 stage_id，也不能让用户看到一行停在「等待」的假进度。
 */
export function stageLabel(stageId: string): string {
  const known = PLANNING_STEPS.find((step) => step.id === stageId);
  return known?.label ?? `未识别阶段（${stageId}）`;
}

export function initialStepStates(): PlanningStepState[] {
  return PLANNING_STEPS.map((step) => ({ ...step, status: "waiting" }));
}

/** mock 版：每一步的事实与状态，接后端后由真实进度事件替换。 */
export const MOCK_STEP_RESULTS: Record<
  string,
  { status: PlanningStepState["status"]; facts: string[] }
> = {
  parse_intent: {
    status: "success",
    facts: ["北京 → 成都 · 5 天 4 晚", "2 人 · 预算 ¥6,000", "偏好 美食 / 拍照 / 历史"],
  },
  search_intercity_transport: {
    status: "success",
    facts: ["找到 14 个交通方案", "途牛 8 个航班 · 12306 6 个车次"],
  },
  search_hotels: {
    status: "success",
    facts: ["找到 26 家酒店", "春熙路周边 9 家符合价格区间"],
  },
  search_social_guides: {
    status: "success",
    facts: ["读取 38 条攻略内容", "小红书 20 条 · 抖音 18 条"],
  },
  extract_and_normalize_places: {
    status: "success",
    facts: ["提取 23 个地点", "合并同义写法后剩 19 个唯一地点"],
  },
  verify_poi_and_routes: {
    status: "warning",
    facts: ["高德验证 17 个地点", "24 段路线中 1 段未返回，已按同区域均值估算"],
  },
  score_candidates: {
    status: "success",
    facts: ["按可信度与广告风险排序", "4 个低可信候选被过滤"],
  },
  build_initial_plan: {
    status: "success",
    facts: ["生成 5 天初版行程，共 25 个安排"],
  },
  check_budget: {
    status: "success",
    facts: ["已知实时费用 ¥4,894", "预计总计 ¥5,668 / 预算 ¥6,000"],
  },
  check_feasibility: {
    status: "success",
    facts: ["检测到 1 个时间冲突", "已自动修正 14:10 → 14:40"],
  },
  critic_and_revise: {
    status: "warning",
    facts: ["修正 Day 3 折返路线", "保留 1 段估算通勤时间"],
  },
  finalize: {
    status: "success",
    facts: ["输出 plan.json / plan.md / audit_report.json"],
  },
};

/** 每步的模拟耗时（毫秒），只影响演示节奏。 */
export const MOCK_STEP_DELAY_MS: Record<string, number> = {
  parse_intent: 620,
  search_intercity_transport: 900,
  search_hotels: 820,
  search_social_guides: 760,
  extract_and_normalize_places: 700,
  verify_poi_and_routes: 980,
  score_candidates: 780,
  build_initial_plan: 860,
  check_budget: 640,
  check_feasibility: 720,
  critic_and_revise: 820,
  finalize: 680,
};
