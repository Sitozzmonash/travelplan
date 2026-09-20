/**
 * 展示用的分档函数（FRONTEND_DESIGN §31）。
 *
 * 注意：这里只是把后端已经算好的分值映射到颜色，不参与任何业务计算。
 * Trust / Ad Risk / 预算 / 可行性全部由 Python 后端产出（§34）。
 */

export type DisplayTone = "success" | "warning" | "danger" | "muted";

export function trustTone(score: number | null | undefined): DisplayTone {
  if (score === null || score === undefined) return "muted";
  if (score >= 80) return "success";
  if (score >= 60) return "muted";
  return "warning";
}

export function adRiskTone(score: number | null | undefined): DisplayTone {
  if (score === null || score === undefined) return "muted";
  if (score >= 60) return "danger";
  if (score >= 30) return "warning";
  return "muted";
}

export const TONE_TEXT: Record<DisplayTone, string> = {
  success: "text-success-subtle-foreground",
  warning: "text-warning-subtle-foreground",
  danger: "text-danger-subtle-foreground",
  muted: "text-muted-foreground",
};

export const TONE_BAR: Record<DisplayTone, string> = {
  success: "bg-success/70",
  warning: "bg-warning/70",
  danger: "bg-danger/70",
  muted: "bg-foreground/35",
};

export const TONE_CHIP: Record<DisplayTone, string> = {
  success: "bg-success-subtle text-success-subtle-foreground",
  warning: "bg-warning-subtle text-warning-subtle-foreground",
  danger: "bg-danger-subtle text-danger-subtle-foreground",
  muted: "bg-muted text-muted-foreground",
};

export function adRiskLabel(score: number | null | undefined): string {
  if (score === null || score === undefined) return "未评估";
  if (score >= 60) return "较高广告风险";
  if (score >= 30) return "存在部分营销内容";
  return "广告风险低";
}

export function trustLabel(score: number | null | undefined): string {
  if (score === null || score === undefined) return "未评估";
  if (score >= 80) return "高可信";
  if (score >= 60) return "中等可信";
  return "可信度偏低";
}
