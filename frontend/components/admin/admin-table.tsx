"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface AdminColumn<T> {
  key: string;
  header: string;
  cell: (row: T) => ReactNode;
  className?: string;
  align?: "left" | "right";
  /** 移动端卡片的主标题（不传则用第一列）。 */
  primary?: boolean;
  /** 移动端卡片里隐藏（长文本列，手机上读起来更累）。 */
  mobileHidden?: boolean;
}

/**
 * 表格：md 以上是真表格（外层 overflow-x-auto，保证页面本身不横向滚动），
 * md 以下自动切换成卡片，避免用户在小屏上左右拖表格。
 */
export function AdminTable<T>({
  columns,
  rows,
  getRowKey,
  className,
}: {
  columns: AdminColumn<T>[];
  rows: T[];
  getRowKey: (row: T) => string;
  className?: string;
}) {
  const primary = columns.find((column) => column.primary) ?? columns[0];
  const rest = columns.filter((column) => column !== primary && !column.mobileHidden);

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[680px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-border">
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={cn(
                    "px-2.5 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground",
                    column.align === "right" ? "text-right" : "text-left",
                    column.className,
                  )}
                >
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={getRowKey(row)}
                className="border-b border-border/60 last:border-0 hover:bg-muted/40"
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={cn(
                      "px-2.5 py-2 align-top",
                      column.align === "right" ? "text-right" : "text-left",
                      column.className,
                    )}
                  >
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="flex flex-col gap-2.5 md:hidden">
        {rows.map((row) => (
          <li key={getRowKey(row)} className="rounded-xl border border-border bg-card p-3">
            {primary ? <div className="text-sm font-medium text-foreground">{primary.cell(row)}</div> : null}
            {rest.length > 0 ? (
              <dl className="mt-2 flex flex-col gap-1.5">
                {rest.map((column) => (
                  <div key={column.key} className="flex items-start justify-between gap-3">
                    <dt className="shrink-0 text-[11px] text-muted-foreground">{column.header}</dt>
                    <dd className="min-w-0 text-right text-xs text-foreground">{column.cell(row)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
