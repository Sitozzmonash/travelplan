"use client";

import { cn } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/** 「全部」哨兵值：真实枚举里不会出现它，用它把"不筛选"和"筛选某个空值"区分开。 */
export const FILTER_ALL = "__all__";

export interface FilterOption {
  value: string;
  label: string;
}

/**
 * 管理台筛选下拉。
 * 用哨兵值而不是空字符串：基础组件对空字符串有"未选中"的语义，
 * 用了会导致「全部」这一项选不回去。
 */
export function FilterSelect({
  label,
  value,
  options,
  onChange,
  placeholder = "全部",
  className,
}: {
  label?: string;
  value: string;
  options: FilterOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}) {
  const current = options.find((option) => option.value === value);

  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      {label ? <span className="text-[11px] text-muted-foreground">{label}</span> : null}
      <Select
        value={value}
        onValueChange={(next) => onChange(typeof next === "string" ? next : FILTER_ALL)}
      >
        <SelectTrigger size="sm" className="w-full min-w-0" aria-label={label ?? placeholder}>
          <SelectValue placeholder={placeholder}>
            {() => current?.label ?? placeholder}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** 把 facets（{k: count}）转成下拉选项，附带出现次数，让操作员先知道能筛出什么。 */
export function facetOptions(
  facets: Record<string, unknown> | null | undefined,
  allLabel: string,
): FilterOption[] {
  const entries = Object.entries(facets ?? {});
  return [
    { value: FILTER_ALL, label: allLabel },
    ...entries.map(([value, count]) => ({
      value,
      label: typeof count === "number" ? `${value}（${count}）` : value,
    })),
  ];
}

/**
 * facets + 静态枚举合并。
 * facets 是空对象时（后端还没统计）筛选器仍然要能用，否则操作员会以为"没有可筛的选项"。
 */
export function mergeFacetOptions(
  facets: Record<string, unknown> | null | undefined,
  fallback: readonly FilterOption[],
  allLabel: string,
): FilterOption[] {
  const options = facetOptions(facets, allLabel);
  const known = new Set(options.map((option) => option.value));
  for (const item of fallback) {
    if (!known.has(item.value)) options.push({ value: item.value, label: item.label });
  }
  return options;
}
