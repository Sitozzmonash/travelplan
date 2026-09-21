import type { Metadata } from "next";
import { GuidedWizard } from "@/components/guided/guided-wizard";

export const metadata: Metadata = {
  title: "逐项选择旅行偏好",
  description:
    "六步引导式旅行偏好：基础信息、交通、酒店、想去哪里、旅行节奏与确认。每一步都可以「随便 / 帮我选 / 不确定」，选完才开始正式规划。",
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
