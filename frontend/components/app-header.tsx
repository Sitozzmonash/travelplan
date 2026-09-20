import Link from "next/link";
import { Plus, Route } from "lucide-react";
import { Button } from "@/components/ui/button";

export function TravelPlanMark({ className }: { className?: string }) {
  return (
    <span
      className={`inline-flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground ${className ?? ""}`}
      aria-hidden
    >
      <Route className="size-4" />
    </span>
  );
}

export function AppHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-border/70 bg-background/85 backdrop-blur-sm">
      <div className="mx-auto flex h-14 w-full max-w-[1440px] items-center justify-between gap-4 px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5">
          <TravelPlanMark />
          <span className="flex flex-col leading-none">
            <span className="text-sm font-semibold tracking-tight text-foreground">TravelPlan</span>
            <span className="mt-0.5 hidden text-[11px] text-muted-foreground sm:inline">
              用真实信息，规划真正能走的行程
            </span>
          </span>
        </Link>

        <nav className="flex items-center gap-1">
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/" />}>
            <Plus />
            <span className="hidden sm:inline">新建行程</span>
          </Button>
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/#about" />}>
            关于项目
          </Button>
          <Button
            variant="ghost"
            size="sm"
            nativeButton={false}
            render={<a href="https://github.com/" target="_blank" rel="noreferrer" />}
          >
            GitHub
          </Button>
        </nav>
      </div>
    </header>
  );
}
