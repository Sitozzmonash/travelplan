"use client";

import Link from "next/link";
import { CircleAlert, Info, OctagonAlert } from "lucide-react";
import type { AdminAttentionItem, AdminRecord } from "@/types/admin";
import { cn } from "@/lib/utils";

const LEVEL_META: Record<string, { icon: typeof Info; className: string; label: string }> = {
  high: {
    icon: OctagonAlert,
    className: "bg-danger-subtle text-danger-subtle-foreground",
    label: "严重",
  },
  medium: {
    icon: CircleAlert,
    className: "bg-warning-subtle text-warning-subtle-foreground",
    label: "注意",
  },
  low: {
    icon: Info,
    className: "bg-muted text-muted-foreground",
    label: "提示",
  },
};

/**
 * 把后端的 `action` 块翻译成一条真实可点的入口。
 *
 * 后端只给**语义**（kind + 参数），不拼 URL —— 前端路由是前端的事。
 * 拼不出入口时返回 null，页面据此只展示文案而不给按钮：
 * 一个点了没反应的按钮比没有按钮更糟。
 */
function actionHref(action: AdminRecord): string | null {
  const kind = String(action.kind ?? "");
  const params = new URLSearchParams();
  if (kind === "runs") {
    if (action.status) params.set("status", String(action.status));
    if (action.q) params.set("q", String(action.q));
  } else if (kind === "badcases") {
    if (action.category) params.set("category", String(action.category));
    if (action.analysis_status) params.set("analysis_status", String(action.analysis_status));
  } else if (kind === "quality") {
    if (action.focus) params.set("focus", String(action.focus));
  } else if (kind !== "provider") {
    return null;
  }
  const base: Record<string, string> = {
    runs: "/admin/runs",
    badcases: "/admin/badcases",
    provider: "/admin/providers",
    quality: "/admin/travel-quality",
  };
  const search = params.toString();
  return `${base[kind]}${search ? `?${search}` : ""}`;
}

/**
 * 「当前需要关注」。
 *
 * 这一块是仪表盘**最重要的部分**：目标是一打开就知道现在最该修什么，
 * 而不是先看一堆累计指标再自己推测。因此每条都带一个能直接跳过去的入口。
 */
export function AttentionPanel({
  items,
  className,
}: {
  items: AdminAttentionItem[];
  className?: string;
}) {
  return (
    <section
      className={cn("flex flex-col gap-2 rounded-xl bg-card p-3.5 ring-1 ring-foreground/10", className)}
      aria-label="当前需要关注"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-foreground">当前需要关注</h2>
        <span className="text-[11px] text-muted-foreground">
          {items.length > 0 ? `${items.length} 条` : "按当前时间窗统计"}
        </span>
      </div>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          这个时间窗内没有触发任何告警规则。没有告警不等于没有问题 —— 质量页的六个维度仍然值得看一眼。
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => {
            const meta = LEVEL_META[item.level] ?? LEVEL_META.low;
            const Icon = meta.icon;
            const href = actionHref(item.action);
            const label = String(item.action.label ?? "查看");
            return (
              <li
                key={item.id}
                className="flex flex-col gap-1.5 rounded-lg border border-border/70 p-2.5 sm:flex-row sm:items-start sm:gap-3"
              >
                <span
                  className={cn(
                    "inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium",
                    meta.className,
                  )}
                  title={`严重度：${meta.label}`}
                >
                  <Icon className="size-3" aria-hidden />
                  {meta.label}
                </span>
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="text-xs font-medium text-foreground">{item.title}</span>
                  <span className="text-[11px] leading-4 text-muted-foreground">{item.detail}</span>
                </div>
                {href ? (
                  <Link
                    href={href}
                    className="shrink-0 self-start text-[11px] font-medium text-primary underline-offset-4 hover:underline"
                  >
                    {label}
                  </Link>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
