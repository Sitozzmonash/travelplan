"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/state-views";

export default function PlanError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto w-full max-w-2xl space-y-4 px-4 py-14 sm:px-6">
      <ErrorState
        title="行程页面加载失败"
        description="页面在渲染行程时出错，本次没有展示可用行程。可以重试，或返回首页重新生成一次。"
        detail={error.message}
        onRetry={reset}
        retryLabel="重试"
      />
      <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/" />}>
        <ArrowLeft />
        返回首页
      </Button>
    </div>
  );
}
