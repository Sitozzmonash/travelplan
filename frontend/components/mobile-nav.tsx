"use client";

import { CalendarDays, Map, ScrollText, Wallet } from "lucide-react";
import { cn } from "@/lib/utils";

interface MobileNavProps {
  onOpenAudit: () => void;
  className?: string;
}

const ANCHORS = [
  { id: "itinerary", label: "行程", icon: CalendarDays },
  { id: "map", label: "地图", icon: Map },
  { id: "budget", label: "预算", icon: Wallet },
] as const;

/** 移动端底部导航（FRONTEND_DESIGN §27）：只做锚点跳转与抽屉入口，不新增业务。 */
export function MobileNav({ onOpenAudit, className }: MobileNavProps) {
  return (
    <nav
      className={cn(
        "fixed inset-x-0 bottom-0 z-30 border-t border-border bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden",
        className,
      )}
      aria-label="页面导航"
    >
      <ul className="grid grid-cols-4">
        {ANCHORS.map((anchor) => (
          <li key={anchor.id}>
            <a
              href={`#${anchor.id}`}
              className="flex flex-col items-center gap-1 py-2.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <anchor.icon className="size-4" aria-hidden />
              {anchor.label}
            </a>
          </li>
        ))}
        <li>
          <button
            type="button"
            onClick={onOpenAudit}
            className="flex w-full flex-col items-center gap-1 py-2.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            <ScrollText className="size-4" aria-hidden />
            依据
          </button>
        </li>
      </ul>
    </nav>
  );
}
