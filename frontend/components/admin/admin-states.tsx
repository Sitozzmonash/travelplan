"use client";

import type { ReactNode } from "react";
import {
  LoaderCircle,
  RefreshCw,
  ServerCog,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { describeAdminError, type AdminApiError } from "@/lib/admin-api";
import { EmptyState, PartialNotice } from "@/components/state-views";
import type { AdminResource } from "@/components/admin/use-admin-resource";

/**
 * 管理台的状态视图集合。
 * 每个页面都必须能回答：现在有没有数据、失败的是哪一部分、下一步能做什么。
 * 401 与 503 单独成视图，因为它们的处理方式完全不同（一个是本地 Token，一个是服务端部署）。
 */

/* ------------------------------ Loading ------------------------------ */

export function SectionSkeleton({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2", className)} aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className={cn("h-9 w-full", index === 0 && "h-6 w-1/3")} />
      ))}
      <span className="sr-only">正在加载管理数据</span>
    </div>
  );
}

export function PageSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-4" aria-busy="true" aria-live="polite">
      <Skeleton className="h-8 w-48" />
      <Skeleton className="h-4 w-72" />
      <SectionSkeleton rows={rows} />
      <span className="sr-only">正在加载管理数据</span>
    </div>
  );
}

export function InlineSpinner({ label = "加载中" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
      {label}
    </span>
  );
}

/* ------------------------------ Empty ------------------------------ */

export function SectionEmpty({
  title,
  description,
  hint,
  action,
  className,
}: {
  title: string;
  description: string;
  hint?: string;
  action?: ReactNode;
  className?: string;
}) {
  return <EmptyState title={title} description={description} hint={hint} action={action} className={className} />;
}

/* ------------------------------ Partial ------------------------------ */

export function PartialBanner({
  title,
  description,
  detail,
  className,
}: {
  title: string;
  description: string;
  detail?: string;
  className?: string;
}) {
  return <PartialNotice title={title} description={description} detail={detail} className={className} />;
}

/* ------------------------------ Error ------------------------------ */

/** 行内错误：某个区块失败，但页面其余部分仍然可用。 */
export function InlineError({
  title,
  description,
  detail,
  onRetry,
  className,
}: {
  title: string;
  description: string;
  detail?: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-danger/25 bg-danger-subtle px-3.5 py-3 text-xs leading-5",
        className,
      )}
      role="alert"
    >
      <p className="flex items-center gap-1.5 font-medium text-danger-subtle-foreground">
        <TriangleAlert className="size-3.5 shrink-0" aria-hidden />
        {title}
      </p>
      <p className="mt-1 break-words text-danger-subtle-foreground/90">{description}</p>
      {detail ? (
        <p className="mt-1 break-words text-[11px] text-danger-subtle-foreground/70">{detail}</p>
      ) : null}
      {onRetry ? (
        <Button variant="outline" size="xs" className="mt-2" onClick={onRetry}>
          <RefreshCw />
          重试
        </Button>
      ) : null}
    </div>
  );
}

/**
 * 统一的错误渲染：401 / 503 走各自的话术，其余给「重试」。
 * onClearToken 只在 401 时使用，由外壳传入。
 */
export function SectionError({
  error,
  onRetry,
  onClearToken,
  className,
}: {
  error: AdminApiError;
  onRetry?: () => void;
  onClearToken?: () => void;
  className?: string;
}) {
  if (error.kind === "unauthorized") {
    return (
      <div
        className={cn(
          "rounded-lg border border-warning/30 bg-warning-subtle px-3.5 py-3 text-xs leading-5",
          className,
        )}
        role="alert"
      >
        <p className="flex items-center gap-1.5 font-medium text-warning-subtle-foreground">
          <ShieldAlert className="size-3.5 shrink-0" aria-hidden />
          管理 Token 无效或已过期
        </p>
        <p className="mt-1 text-warning-subtle-foreground/90">{describeAdminError(error)}</p>
        <p className="mt-1 break-words text-[11px] text-warning-subtle-foreground/70">{error.detail}</p>
        {onClearToken ? (
          <Button variant="outline" size="xs" className="mt-2" onClick={onClearToken}>
            清除本地 Token 并重新输入
          </Button>
        ) : null}
      </div>
    );
  }

  if (error.kind === "unconfigured") {
    return (
      <div
        className={cn(
          "rounded-lg border border-danger/25 bg-danger-subtle px-3.5 py-3 text-xs leading-5",
          className,
        )}
        role="alert"
      >
        <p className="flex items-center gap-1.5 font-medium text-danger-subtle-foreground">
          <ServerCog className="size-3.5 shrink-0" aria-hidden />
          服务端未配置管理 Token
        </p>
        <p className="mt-1 text-danger-subtle-foreground/90">{describeAdminError(error)}</p>
        <p className="mt-1 break-words text-[11px] text-danger-subtle-foreground/70">{error.detail}</p>
      </div>
    );
  }

  return (
    <InlineError
      title="这一部分没能加载"
      description={describeAdminError(error)}
      detail={error.detail}
      onRetry={onRetry}
      className={className}
    />
  );
}

/* ------------------------------ 整页状态 ------------------------------ */

export function UnauthorizedView({
  detail,
  onClearToken,
  onRetry,
}: {
  detail?: string;
  onClearToken: () => void;
  onRetry?: () => void;
}) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-3 rounded-xl border border-warning/30 bg-warning-subtle px-5 py-6">
      <div className="flex items-center gap-2 text-sm font-medium text-warning-subtle-foreground">
        <ShieldAlert className="size-4 shrink-0" aria-hidden />
        管理 Token 未被接受（401）
      </div>
      <p className="text-xs leading-5 text-warning-subtle-foreground/90">
        浏览器里保存的 Token 与后端 <code className="font-mono">TRAVELPLAN_ADMIN_TOKEN</code> 不一致，
        或者已经被轮换。这里不会自动重试：先清除本地 Token，再粘贴当前生效的那一个。
      </p>
      <ul className="list-disc space-y-1 pl-5 text-xs leading-5 text-warning-subtle-foreground/90">
        <li>Token 只保存在本机 localStorage（travelplan_admin_token），不会上传到任何第三方。</li>
        <li>请向部署者确认后端环境变量，不要凭印象重输。</li>
      </ul>
      {detail ? (
        <p className="break-words text-[11px] text-warning-subtle-foreground/70">{detail}</p>
      ) : null}
      <div className="flex flex-wrap gap-2 pt-1">
        <Button size="sm" onClick={onClearToken}>
          清除本地 Token
        </Button>
        {onRetry ? (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw />
            重新探测
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function ServerTokenMissingView({ detail, onRetry }: { detail?: string; onRetry?: () => void }) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-3 rounded-xl border border-danger/25 bg-danger-subtle px-5 py-6">
      <div className="flex items-center gap-2 text-sm font-medium text-danger-subtle-foreground">
        <ServerCog className="size-4 shrink-0" aria-hidden />
        服务端未配置管理 Token（503）
      </div>
      <p className="text-xs leading-5 text-danger-subtle-foreground/90">
        后端没有设置 <code className="font-mono">TRAVELPLAN_ADMIN_TOKEN</code>，
        所有 /api/v1/admin/* 接口都被拒绝。这是部署配置问题，输入任何 Token 都不会通过。
      </p>
      <p className="text-xs leading-5 text-danger-subtle-foreground/90">
        请让部署者在后端配置该环境变量后重启服务；前端这边无需改动。
      </p>
      {detail ? (
        <p className="break-words text-[11px] text-danger-subtle-foreground/70">{detail}</p>
      ) : null}
      {onRetry ? (
        <div className="pt-1">
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw />
            配置好后重新探测
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------ 组合视图 ------------------------------ */

/**
 * 资源区块的统一渲染：Loading → Error → Empty → 内容。
 * 有旧数据时错误降级成顶部提示（Partial），页面其余部分继续可用。
 */
export function ResourceView<T>({
  resource,
  isEmpty,
  emptyTitle = "这里还没有数据",
  emptyDescription = "后端返回了空集合。等有记录产生后，这个区块会自动出现内容。",
  emptyAction,
  loadingRows = 3,
  onClearToken,
  children,
}: {
  resource: AdminResource<T>;
  isEmpty?: (data: T) => boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  loadingRows?: number;
  onClearToken?: () => void;
  children: (data: T) => ReactNode;
}) {
  const { data, error, loading, reload } = resource;

  if (loading && !data) return <SectionSkeleton rows={loadingRows} />;
  if (error && !data) return <SectionError error={error} onRetry={reload} onClearToken={onClearToken} />;
  if (!data) return <SectionSkeleton rows={loadingRows} />;

  const empty = isEmpty ? isEmpty(data) : false;

  return (
    <div className="flex flex-col gap-3">
      {error ? <SectionError error={error} onRetry={reload} onClearToken={onClearToken} /> : null}
      {empty ? (
        <SectionEmpty title={emptyTitle} description={emptyDescription} action={emptyAction} />
      ) : (
        children(data)
      )}
    </div>
  );
}

/** 操作（POST/PATCH）失败时的提示条。 */
export function ActionError({ message, detail }: { message: string; detail?: string }) {
  return (
    <div className="flex gap-2 rounded-md border border-danger/25 bg-danger-subtle px-3 py-2 text-xs leading-5">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-danger-subtle-foreground" aria-hidden />
      <div className="min-w-0">
        <p className="font-medium text-danger-subtle-foreground">{message}</p>
        {detail ? (
          <p className="mt-0.5 break-words text-[11px] text-danger-subtle-foreground/80">{detail}</p>
        ) : null}
      </div>
    </div>
  );
}
