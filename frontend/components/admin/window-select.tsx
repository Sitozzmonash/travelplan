"use client";

import { ADMIN_WINDOW_OPTIONS, type AdminWindowKey } from "@/types/admin";
import { FilterSelect } from "@/components/admin/filter-select";

/**
 * 时间范围选择器。
 *
 * 四档窗口的定义在 `types/admin.ts` 里只写一次，这里只负责渲染 ——
 * Dashboard / 质量页 / 漏斗页如果各自写一份选项，加一档就会漏改一两处。
 */
export function WindowSelect({
  value,
  onChange,
  label = "时间范围",
  className,
}: {
  value: AdminWindowKey;
  onChange: (value: AdminWindowKey) => void;
  label?: string;
  className?: string;
}) {
  return (
    <FilterSelect
      label={label}
      value={value}
      options={ADMIN_WINDOW_OPTIONS.map((option) => ({ value: option.key, label: option.label }))}
      onChange={(next) => onChange(next as AdminWindowKey)}
      placeholder="最近 24 小时"
      className={className}
    />
  );
}

/** 窗口标签：后端返回的中文标签优先，没有就退回本地表，避免出现裸的 `24h`。 */
export function windowLabel(key: string, fallback?: string | null): string {
  if (fallback) return fallback;
  return ADMIN_WINDOW_OPTIONS.find((option) => option.key === key)?.label ?? key;
}
