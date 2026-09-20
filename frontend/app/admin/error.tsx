"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/state-views";

/**
 * 路由级兜底。
 *
 * 页面内部的失败都由 ResourceView / SectionError 处理；这一层只接管"渲染时抛异常"
 * 这种无法局部恢复的情况，保证操作员不会看到 Next.js 的默认错误页，
 * 而且能一键重试或退回仪表盘。
 */
export default function AdminError({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto w-full max-w-2xl space-y-4 py-8">
      <ErrorState
        title="管理台渲染出错"
        description="页面在渲染管理数据时抛出了异常，本次内容不可用。可以重试；如果反复出现，请把下面的错误信息交给开发者。"
        detail={error.message}
        onRetry={reset}
        retryLabel="重试"
      />
      <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/admin" />}>
        <ArrowLeft />
        返回仪表盘
      </Button>
    </div>
  );
}
