import type { Metadata } from "next";
import type { BasicDraft } from "@/components/guided/draft";
import { GuidedWizard } from "@/components/guided/guided-wizard";

export const metadata: Metadata = {
  title: "逐项选择旅行偏好",
  description:
    "两步引导式旅行偏好：探索确认（住哪一带 / 想去什么 / 想吃什么）与交通酒店节奏，选完就开始正式规划。",
};

/**
 * /guided —— 引导式规划向导（Guided Pre-Planning）。
 *
 * 这里只做两件事：解析首页带过来的基础信息，然后交给 GuidedWizard。
 * 向导本身不再有「基础信息」页（它的字段与首页几乎完全重复），所以这些参数
 * 是这次会话唯一的出发信息，缺一个都会被向导挡下来并请用户回首页补。
 *
 * 让用户用最少的思考表达关键偏好的同时，后端在用户选择的过程中就把交通、酒店与攻略
 * Prefetch 出来。正式的 12 步 Workflow 仍然由 /plan/{run_id} 承担，本页不重复实现规划逻辑。
 */
export default async function GuidedPage({ searchParams }: PageProps<"/guided">) {
  const query = await searchParams;
  const value = (key: string) => {
    const raw = query[key];
    return typeof raw === "string" ? raw : "";
  };

  const durationMode = value("duration_mode");
  const budgetMode = value("budget_mode");
  const initialBasic: Partial<BasicDraft> = {
    origin: value("origin"),
    destination: value("destination"),
    startDate: value("start_date"),
    days: value("days"),
    endDate: value("end_date"),
    travelers: value("travelers"),
    budgetAmount: value("budget_amount"),
    // URL 是外部输入：只把认得出的枚举放进来，其余留给 draft 的默认值。
    ...(durationMode === "days" || durationMode === "endDate" ? { durationMode } : {}),
    ...(budgetMode === "amount" || budgetMode === "undecided" || budgetMode === "auto" ? { budgetMode } : {}),
  };

  return <GuidedWizard initialBasic={initialBasic} />;
}
