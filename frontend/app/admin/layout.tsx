"use client";

import type { ReactNode } from "react";
import { AdminShell } from "@/components/admin/admin-shell";

/**
 * 管理台布局。
 *
 * 这里必须是 client component：Token 存在 localStorage，服务端渲染时读不到，
 * 只能由浏览器先读完再决定"显示门禁表单还是一屏数据"。
 * 因此本布局无法导出 metadata（Next.js 不允许 client layout 导出 metadata）。
 */
export default function AdminLayout({ children }: { children: ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
