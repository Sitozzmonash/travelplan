import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Card, CardContent, CardHeader } from "@/components/ui/card";

interface SectionCardProps {
  id?: string;
  title: string;
  icon?: LucideIcon;
  action?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
}

/** 白色内容卡片：浅色背景上的信息分区（FRONTEND_DESIGN §3、§31）。 */
export function SectionCard({
  id,
  title,
  icon: Icon,
  action,
  description,
  children,
  className,
  contentClassName,
}: SectionCardProps) {
  return (
    <Card id={id} className={cn("gap-0 overflow-hidden border-border/80 py-0 shadow-none", className)}>
      <CardHeader className="flex flex-row items-center justify-between gap-3 border-b border-border/70 px-4 py-3 sm:px-5">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-sm font-medium text-foreground">
            {Icon ? <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden /> : null}
            <span className="truncate">{title}</span>
          </h2>
          {description ? (
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {action ? <div className="shrink-0">{action}</div> : null}
      </CardHeader>
      <CardContent className={cn("px-4 py-4 sm:px-5", contentClassName)}>{children}</CardContent>
    </Card>
  );
}
