"use client";

import { LoaderCircle, Lock } from "lucide-react";
import type { ReviseRequest } from "@/types/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export type ReviseAction = ReviseRequest["action"];

export interface ReviseTarget {
  action: ReviseAction;
  dayIndex: number;
  itemId?: string;
}

interface ReviseDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  target: ReviseTarget | null;
  lockedCount: number;
  submitting?: boolean;
  resultMessage?: string | null;
  onConfirm: (request: ReviseRequest) => void;
}

const ACTION_TITLES: Record<ReviseAction, string> = {
  replace: "换一个",
  remove: "不想去",
  lock: "更新锁定状态",
  relax_day: "这天轻松一点",
  lower_budget: "降低预算",
};

const ACTION_DESCRIPTIONS: Record<ReviseAction, string> = {
  replace: "会为这条安排寻找同类替代地点，其余安排保持不变。",
  remove: "会移除这条安排，并把当天剩余安排重新排序。",
  lock: "锁定后，后续重新规划不会再改动这条安排。",
  relax_day: "会减少当天的安排数量，并相应缩短总通勤时间。",
  lower_budget: "会在保持偏好不变的前提下，优先替换价格更高的交通与住宿候选。",
};

/**
 * 修改确认框（FRONTEND_DESIGN §22）。
 * 确认文案必须说明「会重新规划哪一天、已锁定的地点不会被改动」。
 */
export function ReviseDialog({
  open,
  onOpenChange,
  target,
  lockedCount,
  submitting = false,
  resultMessage,
  onConfirm,
}: ReviseDialogProps) {
  const dayLabel = target ? `Day ${target.dayIndex + 1}` : "";

  return (
    <Dialog open={open} onOpenChange={(next) => onOpenChange(next)}>
      <DialogContent data-testid="revise-dialog">
        <DialogHeader>
          <DialogTitle>{target ? ACTION_TITLES[target.action] : "修改计划"}</DialogTitle>
          <DialogDescription>
            {target ? ACTION_DESCRIPTIONS[target.action] : "请选择要修改的安排。"}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2 text-xs leading-5 text-muted-foreground">
          <p className="rounded-lg border border-border/80 bg-muted/30 px-3.5 py-3 text-foreground">
            将重新规划 {dayLabel}，已锁定地点不会变化。
          </p>
          <p className="flex items-center gap-1.5">
            <Lock className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            当前已锁定 {lockedCount} 个地点，本次修改不会影响它们。
          </p>
          {resultMessage ? (
            <p className="text-foreground/90">{resultMessage}</p>
          ) : (
            <p>确认后会把修改请求提交给后端，重新规划的结果以服务端返回为准。</p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button
            onClick={() =>
              target && onConfirm({ action: target.action, day_index: target.dayIndex, item_id: target.itemId })
            }
            disabled={submitting || !target}
          >
            {submitting ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
            确认修改
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
