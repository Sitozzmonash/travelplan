"use client";

import { use } from "react";
import { RunDetailView } from "@/components/admin/run-detail-view";

/**
 * run_id 是客户端读取的：管理台不预渲染任何后端数据（Token 在浏览器里）。
 * Next 15+ 的 params 是 Promise，client component 用 use() 解包。
 */
export default function AdminRunDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <RunDetailView runId={id} />;
}
