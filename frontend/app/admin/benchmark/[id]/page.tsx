"use client";

import { use } from "react";
import { BenchmarkDetailView } from "@/components/admin/benchmark-detail-view";

export default function AdminBenchmarkDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <BenchmarkDetailView benchmarkRunId={id} />;
}
