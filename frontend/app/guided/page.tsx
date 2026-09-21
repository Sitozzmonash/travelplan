import type { Metadata } from "next";
import { GuidedWizard } from "@/components/guided/guided-wizard";

export const metadata: Metadata = {
  title: "逐项选择旅行偏好",
  description:
    "三步引导式旅行偏好：基础信息、探索确认（住哪一带 / 想去什么 / 想吃什么）与交通酒店节奏，选完就开始正式规划。",
};

/**
 * /guided —— 引导式规划向导（Guided Pre-Planning）。
 *
 * 这里只做一件事：让用户在正式规划之前用最少的思考表达关键偏好，
 * 同时让后端在用户选择的过程中就把交通、酒店与攻略 Prefetch 出来。
 * 正式的 12 步 Workflow 仍然由 /plan/{run_id} 承担，本页不重复实现规划逻辑。
 */
export default async function GuidedPage({ searchParams }: PageProps<"/guided">) {
  const query = await searchParams;
  const value = (key: string) => {
    const raw = query[key];
    return typeof raw === "string" ? raw : "";
  };

  return (
    <GuidedWizard
      initialBasic={{
        origin: value("origin"),
        destination: value("destination"),
        startDate: value("start_date"),
        days: value("days"),
      }}
    />
  );
}
