"use client";

import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { statusLabel } from "@/components/admin/format";

/**
 * 状态色只有一个来源：颜色只表达状态，不做装饰（与用户端 FRONTEND_DESIGN §31 一致）。
 * 未知状态一律用中性色，宁可"看不出严重性"也不要把新状态误标成成功。
 */
export type AdminTone = "success" | "warning" | "danger" | "info" | "muted";

const TONE_CLASS: Record<AdminTone, string> = {
  success: "bg-success-subtle text-success-subtle-foreground",
  warning: "bg-warning-subtle text-warning-subtle-foreground",
  danger: "bg-danger-subtle text-danger-subtle-foreground",
  info: "bg-info-subtle text-info-subtle-foreground",
  muted: "bg-muted text-muted-foreground",
};

export function statusTone(status: string | null | undefined): AdminTone {
  if (!status) return "muted";
  switch (status) {
    case "SUCCESS":
    case "completed":
    case "pass":
    case "passed":
    case "fixed":
    case "verified":
    case "analyzed":
    case "ACCEPT":
    case "OK":
    case "HEALTHY":
      return "success";
    case "DEGRADED":
    case "WARNING":
    case "wontfix":
    case "suspected":
    case "pending":
    case "ROLLBACK":
    case "medium":
    case "moderate":
      return "warning";
    case "FAILED":
    case "fail":
    case "rejected":
    case "unfixed":
    case "REJECT":
    case "ERROR":
    case "TIMEOUT":
    case "critical":
    case "high":
    case "severe":
      return "danger";
    case "RUNNING":
    case "QUEUED":
    case "running":
    case "WAITING":
      return "info";
    case "low":
    case "minor":
      return "muted";
    default:
      return "muted";
  }
}

export function ToneBadge({
  tone,
  children,
  className,
}: {
  tone: AdminTone;
  children: ReactNode;
  className?: string;
}) {
  return <Badge className={cn("border-transparent", TONE_CLASS[tone], className)}>{children}</Badge>;
}

/** 状态徽标：把后端枚举翻译成中文，同时保留原始值在 title 里，便于对照日志。 */
export function StatusBadge({
  status,
  label,
  className,
}: {
  status: string | null | undefined;
  label?: string;
  className?: string;
}) {
  if (!status) {
    return (
      <Badge variant="outline" className={cn("text-[0.6875rem]", className)}>
        未知
      </Badge>
    );
  }
  return (
    <ToneBadge tone={statusTone(status)} className={cn("text-[0.6875rem]", className)}>
      <span title={status}>{label ?? statusLabel(status)}</span>
    </ToneBadge>
  );
}

/** 布尔型状态（enabled / configured / fallback …）。 */
export function BooleanBadge({
  value,
  onLabel = "是",
  offLabel = "否",
  onTone = "success",
  offTone = "muted",
  className,
}: {
  value: boolean;
  onLabel?: string;
  offLabel?: string;
  onTone?: AdminTone;
  offTone?: AdminTone;
  className?: string;
}) {
  return (
    <ToneBadge tone={value ? onTone : offTone} className={cn("text-[0.6875rem]", className)}>
      {value ? onLabel : offLabel}
    </ToneBadge>
  );
}

/** 密钥配置状态：管理台只回答「配没配」，永远不回显取值。 */
export function SecretBadge({ configured }: { configured: boolean }) {
  return (
    <BooleanBadge
      value={configured}
      onLabel="已配置"
      offLabel="未配置"
      onTone="success"
      offTone="warning"
    />
  );
}
