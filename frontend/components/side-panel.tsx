"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";

interface SidePanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  widthClassName?: string;
}

/** 统一右侧抽屉：桌面为右侧面板，移动端为全宽 Sheet（FRONTEND_DESIGN §27）。 */
export function SidePanel({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  widthClassName,
}: SidePanelProps) {
  return (
    <Sheet open={open} onOpenChange={(next) => onOpenChange(next)}>
      <SheetContent
        side="right"
        className={cn("w-full gap-0 p-0 sm:max-w-xl", widthClassName)}
        data-testid="side-panel"
      >
        <SheetHeader className="shrink-0 border-b px-5 py-4 pr-12">
          <SheetTitle className="text-base leading-6">{title}</SheetTitle>
          {description ? (
            <SheetDescription className="text-xs leading-5">{description}</SheetDescription>
          ) : (
            <SheetDescription className="sr-only">详情面板</SheetDescription>
          )}
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4">{children}</div>
        {footer ? <div className="shrink-0 border-t bg-muted/40 px-5 py-3">{footer}</div> : null}
      </SheetContent>
    </Sheet>
  );
}
