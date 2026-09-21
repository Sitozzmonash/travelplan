"use client";

import { cn } from "@/lib/utils";

/**
 * 迷你趋势线 / 柱状图。
 *
 * 为什么手写 SVG 而不引图表库：这里只用得到"一条 30 点以内的折线/一组柱子"，
 * 而 recharts 会把管理台的 bundle 推高几百 KB，还要为它单独写一套主题适配。
 * 需要坐标轴、图例、交互的图表不在这里做 —— 那种需求应该在 Benchmark 页单独设计。
 *
 * 空值语义：点值为 **null 表示该分桶没有样本**（不是 0）。null 段直接断开，
 * 不再画线 —— 把"没有数据"画成"掉到 0"是最容易误导人的一种画法。
 */
export function Sparkline({
  points,
  variant = "line",
  className,
  ariaLabel,
  tone = "info",
}: {
  points: (number | null)[];
  variant?: "line" | "bar";
  className?: string;
  ariaLabel?: string;
  tone?: "info" | "success" | "warning" | "danger" | "muted";
}) {
  const values = points.filter((value): value is number => typeof value === "number");
  if (values.length < 2) {
    return (
      <span className={cn("text-[11px] text-muted-foreground", className)}>
        {values.length === 0 ? "无数据" : "样本不足"}
      </span>
    );
  }

  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const width = 100;
  const height = 28;
  const stroke = {
    info: "var(--color-info, #38bdf8)",
    success: "var(--color-success, #34d399)",
    warning: "var(--color-warning, #fbbf24)",
    danger: "var(--color-danger, #f87171)",
    muted: "var(--color-muted-foreground, #94a3b8)",
  }[tone];

  if (variant === "bar") {
    const slot = width / points.length;
    const barWidth = Math.max(1, slot * 0.6);
    return (
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={ariaLabel ?? "趋势"}
        className={cn("h-7 w-full", className)}
      >
        {points.map((value, index) => {
          if (typeof value !== "number") return null;
          const ratio = (value - min) / span;
          const barHeight = Math.max(1.5, ratio * (height - 4) + 1.5);
          return (
            <rect
              key={index}
              x={index * slot + (slot - barWidth) / 2}
              y={height - barHeight}
              width={barWidth}
              height={barHeight}
              fill={stroke}
              opacity={0.75}
            />
          );
        })}
      </svg>
    );
  }

  // 折线：null 处断开，用多段 polyline 表达"这里真的没有数据"。
  const segments: string[][] = [];
  let current: string[] = [];
  points.forEach((value, index) => {
    if (typeof value !== "number") {
      if (current.length > 1) segments.push(current);
      current = [];
      return;
    }
    const x = (index / Math.max(1, points.length - 1)) * width;
    const y = height - ((value - min) / span) * (height - 4) - 2;
    current.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  });
  if (current.length > 1) segments.push(current);

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={ariaLabel ?? "趋势"}
      className={cn("h-7 w-full", className)}
    >
      {segments.map((segment, index) => (
        <polyline
          key={index}
          points={segment.join(" ")}
          fill="none"
          stroke={stroke}
          strokeWidth={1.6}
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  );
}
