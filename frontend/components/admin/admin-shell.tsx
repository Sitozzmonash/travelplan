"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useCallback, useEffect, useState, useSyncExternalStore } from "react";
import {
  Activity,
  ArrowLeft,
  Bug,
  Compass,
  FlaskConical,
  Filter,
  Gauge,
  HeartPulse,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Settings2,
  ShieldCheck,
  X,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  ADMIN_API_BASE_URL,
  clearAdminToken,
  fetchDefaultAdminToken,
  getAdminOverview,
  getAdminTokenSnapshot,
  isAdminAutofillOptedOut,
  setAdminAutofillOptOut,
  subscribeAdminToken,
  writeAdminToken,
} from "@/lib/admin-api";
import type { AdminOverview } from "@/types/admin";
import {
  PageSkeleton,
  ServerTokenMissingView,
  UnauthorizedView,
} from "@/components/admin/admin-states";
import {
  useAdminResource,
  useHydrated,
  type AdminResource,
} from "@/components/admin/use-admin-resource";

/**
 * 把外壳已经探到的 `/admin/overview` 共享给子树。
 *
 * 为什么要共享：外壳为了在页面级数据之前分辨 401 / 503，进入管理台时必然要探一次
 * overview，而页面上唯一消费这份数据的「累计用量（历史总量）」折叠卡原本会在展开时
 * **再拉一次同一端点**。`useAdminResource` 的状态是按 hook 实例存的、没有跨实例缓存，
 * 所以这里用 context 显式复用这一次请求，而不是给所有资源加一层全局缓存
 * （那会改变每个页面的刷新语义）。页面依旧保留自己的按需请求兜底，
 * 拿不到 context 时行为与改造前完全一致。
 */
export const AdminOverviewContext = createContext<AdminResource<AdminOverview> | null>(null);

/**
 * 管理台外壳。
 *
 * 职责边界：Token 门禁 + 导航 + 全局鉴权失败视图。
 * 具体页面的数据请求各自负责，这样任何一页失败都不会把整个控制台拖死。
 *
 * Token 只存在 localStorage 的 travelplan_admin_token。本地开发时它会由
 * `/admin/default-token`（服务端读非 public 的 TRAVELPLAN_ADMIN_TOKEN）**自动填入**，
 * 操作员打开 /admin 就等于已经登录；线上前端没有这个变量，端点返回空值，
 * 门禁照旧要求手输。默认值本身不参与构建（因此不能放进 NEXT_PUBLIC_*）。
 * 读取用 useSyncExternalStore：服务端快照为 null，浏览器拿到真实值后安全重渲染，
 * 不产生 hydration mismatch。
 *
 * 另外，外壳会先探测一次 /overview 来确认 Token 是否被接受 —— 401 与 503 必须
 * 在页面级数据之前就分辨出来，否则操作员会看到满屏"这一部分没能加载"而找不到原因。
 */

interface AdminNavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** 只有 /admin 自身用精确匹配，否则 /admin/runs 也会点亮「仪表盘」。 */
  exact?: boolean;
}

/**
 * 导航信息架构（docs/product/ADMIN.md §2）。
 *
 * 分组的依据是**要回答的问题**，不是页面来源：
 *   * 总览   —— 现在系统健康吗、最该修什么？
 *   * 产品质量 —— 生成出来的行程好不好、用户在哪一步流失、哪类问题在变多？
 *   * 运行观测 —— 具体某次跑成什么样？
 *   * 评测优化 —— 修完有没有变好？
 * 「产品质量」放在运行观测之前是刻意的：这个后台的首要身份是产品质量控制台，
 * 而不是 Agent 调用监视器。
 */
const NAV_GROUPS: { label: string; items: AdminNavItem[] }[] = [
  {
    label: "总览",
    items: [{ href: "/admin", label: "仪表盘", icon: LayoutDashboard, exact: true }],
  },
  {
    label: "产品质量",
    items: [
      { href: "/admin/travel-quality", label: "行程质量", icon: Gauge },
      { href: "/admin/funnel", label: "规划漏斗", icon: Filter },
      { href: "/admin/badcases", label: "问题中心", icon: Bug },
    ],
  },
  {
    label: "运行观测",
    items: [
      { href: "/admin/runs", label: "运行记录", icon: ListChecks },
      { href: "/admin/sessions", label: "引导式会话", icon: Compass },
      { href: "/admin/providers", label: "Provider 健康", icon: HeartPulse },
    ],
  },
  {
    label: "评测优化",
    items: [
      { href: "/admin/benchmark", label: "Benchmark", icon: FlaskConical },
      { href: "/admin/evolution", label: "Evolution", icon: Activity },
    ],
  },
  {
    label: "系统",
    items: [{ href: "/admin/config", label: "系统配置", icon: Settings2 }],
  },
];

function isActive(pathname: string, item: AdminNavItem): boolean {
  if (item.exact) return pathname === item.href;
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

export function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const hydrated = useHydrated();
  const token = useSyncExternalStore(subscribeAdminToken, getAdminTokenSnapshot, () => null);
  const [navOpen, setNavOpen] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  /** 服务端下发的默认 Token；null 表示这台部署没配（线上就是这种）。 */
  const [defaultToken, setDefaultToken] = useState<string | null>(null);
  /** 默认值是否已经问过一次：没问完之前不渲染门禁表单，避免表单闪一下。 */
  const [defaultChecked, setDefaultChecked] = useState(false);
  /** 操作员主动清除过 Token：此时不再自动填回，否则「清除」等于空操作。 */
  const [autofillOptedOut, setAutofillOptedOut] = useState(false);

  const probe = useAdminResource<AdminOverview>(
    "admin-auth-probe",
    getAdminOverview,
    Boolean(token),
  );

  // 问一次服务端有没有默认 Token。只在浏览器里跑：服务端渲染时没有这条请求，
  // 也就不存在「服务端和客户端拿到不同 Token」的 hydration 问题。
  // 「不再自动填入」的标记位在同一个回调里一起落到 state，因此自动填入那一步
  // 一定读到的是已经决定好的标记位，不会先把 Token 填回去。
  useEffect(() => {
    if (!hydrated) return;
    let cancelled = false;
    fetchDefaultAdminToken().then((value) => {
      if (cancelled) return;
      setDefaultToken(value);
      setAutofillOptedOut(isAdminAutofillOptedOut());
      setDefaultChecked(true);
    });
    return () => {
      cancelled = true;
    };
  }, [hydrated]);

  // 本地没有 Token 且操作员没拒绝自动填入时，把默认值写进 localStorage，
  // 之后的每次请求都走 useAdminResource，和手输 Token 完全没有区别。
  useEffect(() => {
    if (!hydrated || token || !defaultChecked || autofillOptedOut) return;
    if (!defaultToken) return;
    writeAdminToken(defaultToken);
  }, [hydrated, token, defaultChecked, autofillOptedOut, defaultToken]);

  const applyToken = useCallback((next: string) => {
    const trimmed = next.trim();
    if (!trimmed) return;
    setAdminAutofillOptOut(false);
    setAutofillOptedOut(false);
    writeAdminToken(trimmed);
    setTokenInput("");
    setNavOpen(false);
  }, []);

  /** 主动启用本地默认 Token：撤销「不再自动填入」，等价于自动登录。 */
  const handleUseDefaultToken = useCallback(() => {
    if (!defaultToken) return;
    setAdminAutofillOptOut(false);
    setAutofillOptedOut(false);
    writeAdminToken(defaultToken);
    setNavOpen(false);
  }, [defaultToken]);

  const handleClearToken = useCallback(() => {
    // 清除 = 「别再自动填回来」。不置位的话下一次渲染就把默认值写回去了，
    // 操作员会以为按钮坏了，401 的人也换不成别的 Token。
    setAdminAutofillOptOut(true);
    setAutofillOptedOut(true);
    clearAdminToken();
    setTokenInput("");
    setNavOpen(false);
  }, []);

  // 默认值还在路上、或马上要被自动填入时先给骨架：不要先闪一下门禁表单再跳进控制台。
  const autofillPending =
    !token && (!defaultChecked || (!autofillOptedOut && defaultToken !== null));

  if (!hydrated || autofillPending) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-4 py-6 sm:px-6">
        <PageSkeleton rows={4} />
      </div>
    );
  }

  if (!token) {
    return (
      <TokenGate
        value={tokenInput}
        onChange={setTokenInput}
        onSubmit={applyToken}
        defaultToken={defaultToken}
        onUseDefaultToken={handleUseDefaultToken}
      />
    );
  }

  // 只有鉴权类失败才接管整屏；其它错误由各页面自己说明，避免在无关页面上叠加杂音。
  const authIssue = probe.error?.isAuthIssue ? probe.error : null;

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-4 px-3 py-4 sm:px-6">
      <header className="sticky top-14 z-30 -mx-3 flex h-12 items-center gap-2 border-b border-border/70 bg-background/85 px-3 backdrop-blur-sm sm:-mx-6 sm:px-6">
        <Button
          variant="ghost"
          size="icon-sm"
          className="lg:hidden"
          aria-label="打开管理导航"
          onClick={() => setNavOpen(true)}
        >
          <Menu />
        </Button>
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-medium text-foreground">TravelPlan 管理后台</span>
          <Badge variant="outline" className="hidden text-[0.6875rem] sm:inline-flex">
            v0.1
          </Badge>
        </span>
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/" />}>
            <ArrowLeft />
            <span className="hidden sm:inline">返回用户端</span>
          </Button>
          <Button variant="outline" size="sm" onClick={handleClearToken}>
            <LogOut />
            <span className="hidden sm:inline">清除 Token</span>
          </Button>
        </div>
      </header>

      <div className="flex min-w-0 flex-col gap-4 lg:flex-row lg:gap-6">
        <aside className="hidden lg:block lg:w-52 lg:shrink-0">
          <div className="sticky top-28">
            <AdminNav pathname={pathname} />
          </div>
        </aside>

        <main className="min-w-0 flex-1 pb-16 lg:pb-4">
          <AdminOverviewContext.Provider value={probe}>
            {authIssue?.kind === "unauthorized" ? (
              <UnauthorizedView
                detail={authIssue.detail}
                onClearToken={handleClearToken}
                onRetry={probe.reload}
              />
            ) : authIssue?.kind === "unconfigured" ? (
              <ServerTokenMissingView detail={authIssue.detail} onRetry={probe.reload} />
            ) : (
              children
            )}
          </AdminOverviewContext.Provider>
        </main>
      </div>

      <Drawer open={navOpen} onOpenChange={setNavOpen} swipeDirection="left" showSwipeHandle>
        <DrawerContent>
          <DrawerHeader className="flex-row items-center justify-between gap-2 border-b px-4 py-3 text-left">
            <div className="min-w-0">
              <DrawerTitle className="text-sm">TravelPlan 管理后台</DrawerTitle>
              <DrawerDescription className="mt-0.5 text-[11px]">
                观测、评估、诊断与持续优化
              </DrawerDescription>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="关闭导航"
              onClick={() => setNavOpen(false)}
            >
              <X />
            </Button>
          </DrawerHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-2 py-3">
            <AdminNav pathname={pathname} onNavigate={() => setNavOpen(false)} />
          </div>
          <div className="border-t px-4 py-3">
            <Button variant="outline" size="sm" className="w-full" onClick={handleClearToken}>
              <LogOut />
              清除 Token
            </Button>
          </div>
        </DrawerContent>
      </Drawer>
    </div>
  );
}

function AdminNav({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-4" aria-label="管理台导航">
      {NAV_GROUPS.map((group) => (
        <div key={group.label} className="flex flex-col gap-1">
          <span className="px-2 text-[11px] font-medium text-muted-foreground">{group.label}</span>
          {group.items.map((item) => {
            const active = isActive(pathname, item);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors",
                  active
                    ? "bg-accent font-medium text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                <span className="truncate">{item.label}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

/**
 * Token 门禁。
 * 这里把 401 与 503 的差别写清楚：输错 Token 的人会反复重试，
 * 而 503 是他无论怎么输都不可能通过的，必须早点告诉他去找部署者。
 *
 * 本地 `.env.local` 配了 `TRAVELPLAN_ADMIN_TOKEN` 时不会走到这里（已自动填入）；
 * 只有两种情况会看到它：这台部署没配默认值，或者操作员刚点过「清除 Token」。
 * 后一种情况下把「用本地默认 Token」的入口留在页面上，操作员随时能一键回到免手输。
 */
function TokenGate({
  value,
  onChange,
  onSubmit,
  defaultToken,
  onUseDefaultToken,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  defaultToken: string | null;
  onUseDefaultToken: () => void;
}) {
  return (
    <div className="mx-auto flex w-full max-w-xl flex-col gap-5 px-4 py-10 sm:px-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <span
            className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground"
            aria-hidden
          >
            <ShieldCheck className="size-4" />
          </span>
          <h1 className="text-base font-semibold tracking-tight">TravelPlan 管理控制台</h1>
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          管理台用于观测运行质量、查看 Bad Case、跑 Benchmark 与 Evolution。
          它不对外开放，进入前需要后端签发的管理 Token。
        </p>
      </div>

      <form
        className="flex flex-col gap-3 rounded-xl bg-card p-4 ring-1 ring-foreground/10"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit(value);
        }}
      >
        <label className="text-xs font-medium text-foreground" htmlFor="admin-token">
          管理 Token
        </label>
        <Input
          id="admin-token"
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="粘贴后端 TRAVELPLAN_ADMIN_TOKEN 的取值"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
        <p className="text-[11px] leading-4 text-muted-foreground">
          Token 只写入本机 localStorage（键名 travelplan_admin_token），每个请求以
          <code className="mx-1 font-mono">Authorization: Bearer &lt;token&gt;</code>
          头发送。它不会进入前端构建产物，也不会上传到任何第三方。
        </p>
        {defaultToken ? (
          <p className="text-[11px] leading-4 text-muted-foreground">
            本机 <code className="font-mono">.env</code> 里有默认 Token，已暂停自动填入。
            <button
              type="button"
              className="mx-1 font-medium text-primary underline-offset-4 hover:underline"
              onClick={onUseDefaultToken}
            >
              使用本地默认 Token
            </button>
            （等价于自动登录，下次打开 /admin 不再显示这一页）。
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={value.trim().length === 0}>
            进入管理台
          </Button>
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/" />}>
            <ArrowLeft />
            返回用户端
          </Button>
        </div>
      </form>

      <div className="rounded-lg border border-border bg-muted/30 p-3.5 text-[11px] leading-5 text-muted-foreground">
        <p className="text-xs font-medium text-foreground">两种失败要先分清</p>
        <ul className="mt-1.5 list-disc space-y-1 pl-4">
          <li>
            <span className="font-medium text-foreground">401</span>：Token 不对或已轮换。
            清除本地 Token，再粘贴当前生效的那一个。
          </li>
          <li>
            <span className="font-medium text-foreground">503</span>：后端没有设置
            <code className="mx-1 font-mono">TRAVELPLAN_ADMIN_TOKEN</code>，
            此时输任何 Token 都无效，需要部署者配置后重启。
          </li>
        </ul>
        <p className="mt-2 font-mono break-all">管理接口地址：{ADMIN_API_BASE_URL}</p>
      </div>
    </div>
  );
}
