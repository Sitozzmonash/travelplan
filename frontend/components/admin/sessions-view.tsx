"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowUpRight, ChevronLeft, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { AdminDetailDialog } from "@/components/admin/detail-dialog";
import { DescriptionList, MetricList, PanelSection } from "@/components/admin/metric-list";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { formatNumber, statusLabel } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { formatDateTime } from "@/lib/format";
import { getAdminPlanningSession, getAdminPlanningSessions } from "@/lib/admin-api";
import type {
  AdminPlanningCandidate,
  AdminPlanningSessionDetail,
  AdminPlanningSessionList,
  AdminPlanningSessionSummary,
  AdminPlanningEvent,
} from "@/types/admin";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 350;

/**
 * Guided Session 列表。
 * 这一页是「从用户前置选择追溯到最终 Run」的入口：
 * 列表按 session_id / run_id 搜索，详情弹窗展示偏好、POI 选择统计、Discovery 事件与降级。
 */
export function SessionsView() {
  // 支持从运行详情跳转过来时带上 `?q=<session_id>`。
  // 用惰性初始化而不是 effect+setState：管理台外壳在 hydration 前不渲染子树，
  // 因此这里首次求值时 window 一定可用，也避免了「effect 里同步 setState」。
  const [queryDraft, setQueryDraft] = useState(() => {
    if (typeof window === "undefined") return "";
    return new URLSearchParams(window.location.search).get("q") ?? "";
  });
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const q = useDebouncedValue(queryDraft.trim(), SEARCH_DEBOUNCE_MS);

  const resource = useAdminResource<AdminPlanningSessionList>(
    `admin-sessions:${q}:${offset}`,
    () => getAdminPlanningSessions({ limit: PAGE_SIZE, offset, q: q || undefined }),
  );

  const items = resource.data?.items ?? [];
  // 后端契约暂未声明 q 参数：这里再做一次客户端兜底过滤，保证搜索一定可用。
  const filtered = q ? items.filter((item) => matchesQuery(item, q)) : items;

  const columns: AdminColumn<AdminPlanningSessionSummary>[] = [
    {
      key: "session_id",
      header: "Session",
      primary: true,
      className: "max-w-[14rem]",
      cell: (session) => (
        <button
          type="button"
          onClick={() => setSelected(session.session_id)}
          title={session.session_id}
          className="block max-w-[14rem] truncate text-left font-mono text-xs text-primary underline-offset-4 hover:underline"
        >
          {session.session_id}
        </button>
      ),
    },
    { key: "status", header: "状态", cell: (session) => <StatusBadge status={session.status} /> },
    {
      key: "discovery",
      header: "Discovery",
      cell: (session) => <StatusBadge status={session.discovery_status} />,
    },
    {
      key: "route",
      header: "行程",
      cell: (session) => (
        <span className="whitespace-nowrap text-xs text-foreground">
          {session.origin || "未填"} → {session.destination || "未填"}
        </span>
      ),
    },
    {
      key: "start_date",
      header: "出发日期",
      mobileHidden: true,
      cell: (session) => (
        <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
          {session.start_date ?? "—"}
        </span>
      ),
    },
    {
      key: "days",
      header: "天数",
      align: "right",
      cell: (session) => <span className="tabular text-xs">{formatNumber(session.days)}</span>,
    },
    {
      key: "travelers",
      header: "人数",
      align: "right",
      mobileHidden: true,
      cell: (session) => <span className="tabular text-xs">{formatNumber(session.travelers)}</span>,
    },
    {
      key: "budget",
      header: "预算",
      align: "right",
      mobileHidden: true,
      cell: (session) => <span className="tabular text-xs">{formatNumber(session.budget_total)}</span>,
    },
    {
      key: "selections",
      header: "必去 / 想去 / 排除",
      align: "right",
      mobileHidden: true,
      cell: (session) => (
        <span className="tabular text-xs whitespace-nowrap">
          {formatNumber(session.must_count)} / {formatNumber(session.want_count)} /{" "}
          {formatNumber(session.reject_count)}
        </span>
      ),
    },
    {
      key: "run_id",
      header: "Run",
      mobileHidden: true,
      className: "max-w-[12rem]",
      cell: (session) =>
        session.run_id ? (
          <Link
            href={`/admin/runs/${encodeURIComponent(session.run_id)}`}
            title={session.run_id}
            className="block max-w-[12rem] truncate font-mono text-[11px] whitespace-nowrap text-primary underline-offset-4 hover:underline"
          >
            {session.run_id}
          </Link>
        ) : (
          <span className="text-[11px] text-muted-foreground">未生成</span>
        ),
    },
    {
      key: "updated_at",
      header: "更新时间",
      mobileHidden: true,
      cell: (session) => (
        <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
          {formatDateTime(session.updated_at)}
        </span>
      ),
    },
    {
      key: "action",
      header: "操作",
      align: "right",
      cell: (session) => (
        <Button variant="outline" size="xs" onClick={() => setSelected(session.session_id)}>
          详情
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="引导式会话"
        description="用户在向导里的前置选择：偏好、POI 取舍、Discovery 事件与降级。点详情可以追溯到最终 Run。"
        actions={
          <Button variant="outline" size="sm" onClick={resource.reload}>
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <div className="flex flex-col gap-1 rounded-xl bg-card p-3 ring-1 ring-foreground/10 sm:max-w-md">
        <label className="text-[11px] text-muted-foreground" htmlFor="sessions-search">
          session_id / run_id 搜索
        </label>
        <div className="flex items-center gap-1.5">
          <Input
            id="sessions-search"
            value={queryDraft}
            placeholder="输入完整或部分 session_id / run_id"
            spellCheck={false}
            onChange={(event) => {
              setQueryDraft(event.target.value);
              setOffset(0);
            }}
          />
          {queryDraft ? (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="清空搜索"
              onClick={() => {
                setQueryDraft("");
                setOffset(0);
              }}
            >
              <X />
            </Button>
          ) : (
            <span className="flex size-7 items-center justify-center text-muted-foreground" aria-hidden>
              <Search className="size-3.5" />
            </span>
          )}
        </div>
        {q ? (
          <p className="text-[11px] leading-4 text-muted-foreground">
            当前页内按关键字过滤；如果后端支持 q 参数，分页结果会由后端先行筛选。
          </p>
        ) : null}
      </div>

      <ResourceView
        resource={resource}
        loadingRows={5}
        isEmpty={(data) => (q ? filtered.length === 0 : data.items.length === 0)}
        emptyTitle="没有符合条件的引导式会话"
        emptyDescription={
          q
            ? `没有匹配「${q}」的 session_id 或 run_id。可以换一个片段再试。`
            : "后端返回了空集合。用户在引导式向导里创建会话后，这里会出现记录。"
        }
      >
        {(data) => (
          <div className="flex flex-col gap-3">
            <Pagination
              total={data.total}
              offset={data.offset}
              count={data.items.length}
              onPrevious={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
              onNext={() => setOffset((value) => value + PAGE_SIZE)}
            />
            <AdminTable columns={columns} rows={filtered} getRowKey={(session) => session.session_id} />
            <Pagination
              total={data.total}
              offset={data.offset}
              count={data.items.length}
              onPrevious={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
              onNext={() => setOffset((value) => value + PAGE_SIZE)}
            />
          </div>
        )}
      </ResourceView>

      {selected ? (
        <SessionDetailDialog
          sessionId={selected}
          onOpenChange={(open) => {
            if (!open) setSelected(null);
          }}
        />
      ) : null}
    </div>
  );
}

function SessionDetailDialog({
  sessionId,
  onOpenChange,
}: {
  sessionId: string;
  onOpenChange: (open: boolean) => void;
}) {
  const resource = useAdminResource<AdminPlanningSessionDetail>(
    `admin-session:${sessionId}`,
    () => getAdminPlanningSession(sessionId),
  );

  return (
    <AdminDetailDialog
      open
      onOpenChange={onOpenChange}
      title="引导式会话详情"
      description={<span className="font-mono">{sessionId}</span>}
    >
      <ResourceView resource={resource} loadingRows={5}>
        {(data) => <SessionDetailBody data={data} />}
      </ResourceView>
    </AdminDetailDialog>
  );
}

function SessionDetailBody({ data }: { data: AdminPlanningSessionDetail }) {
  const degradations = data.degradations ?? [];

  return (
    <div className="flex flex-col gap-5">
      <PanelSection title="基本信息" description="会话状态、行程范围与生成出的 Run。">
        <DescriptionList
          items={[
            { label: "状态", value: <StatusBadge status={data.status ?? null} /> },
            { label: "Discovery", value: <StatusBadge status={data.discovery_status ?? null} /> },
            { label: "出发地", value: data.origin || "未返回" },
            { label: "目的地", value: data.destination || "未返回" },
            { label: "出发日期", value: data.start_date ?? "未返回" },
            { label: "天数", value: formatNumber(data.days) },
            { label: "人数", value: formatNumber(data.travelers) },
            { label: "预算", value: formatNumber(data.budget_total) },
            { label: "创建时间", value: formatDateTime(data.created_at) },
            { label: "更新时间", value: formatDateTime(data.updated_at) },
            { label: "过期时间", value: formatDateTime(data.expires_at) },
            {
              label: "最终 Run",
              value: data.run_id ? (
                <Link
                  href={`/admin/runs/${encodeURIComponent(data.run_id)}`}
                  className="inline-flex items-center gap-1 font-mono text-primary underline-offset-4 hover:underline"
                >
                  {data.run_id}
                  <ArrowUpRight className="size-3" aria-hidden />
                </Link>
              ) : (
                "尚未生成"
              ),
            },
          ]}
        />
      </PanelSection>

      <PanelSection title="结构化偏好" description="节奏、交通与住宿优先级，以及 POI 取舍数量。">
        <div className="flex flex-wrap items-center gap-2">
          <PreferenceChip label="节奏" value={data.pace} />
          <PreferenceChip label="交通偏好" value={data.transport_priority} />
          <PreferenceChip label="住宿偏好" value={data.hotel_priority} />
        </div>
        <DescriptionList
          className="mt-2"
          items={[
            { label: "必去", value: formatNumber(data.must_count) },
            { label: "想去", value: formatNumber(data.want_count) },
            { label: "排除", value: formatNumber(data.reject_count) },
          ]}
        />
      </PanelSection>

      <PanelSection title="POI 选择统计" description="候选摘要由后端给出；字段未固定时按原样展示。">
        <div className="flex flex-col gap-3">
          <CandidateList title="地点候选" candidates={data.place_candidates ?? []} />
          <CandidateList title="交通候选" candidates={data.transport_candidates ?? []} />
          <CandidateList title="住宿候选" candidates={data.hotel_candidates ?? []} />
        </div>
      </PanelSection>

      <PanelSection title="Discovery 事件" description="会话过程中的关键节点。">
        {data.events && data.events.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {data.events.map((event, index) => (
              <EventRow key={`${event.at ?? ""}-${event.event ?? ""}-${index}`} event={event} />
            ))}
          </ul>
        ) : (
          <SectionEmpty title="没有 Discovery 事件" description="后端没有返回该会话的事件列表。" />
        )}
      </PanelSection>

      <PanelSection title="降级" description="会话期间发生的能力降级。">
        {degradations.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {degradations.map((item, index) => (
              <li
                key={`${index}-${item}`}
                className="rounded-md border border-warning/25 bg-warning-subtle/60 px-2.5 py-1.5 text-xs leading-5 break-words text-warning-subtle-foreground"
              >
                {item}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">本次会话没有降级。</p>
        )}
      </PanelSection>
    </div>
  );
}

function PreferenceChip({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <ToneBadge tone="muted">{value && value.length > 0 ? statusLabel(value) : "未返回"}</ToneBadge>
    </span>
  );
}

function CandidateList({ title, candidates }: { title: string; candidates: AdminPlanningCandidate[] }) {
  if (candidates.length === 0) {
    return (
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="text-muted-foreground">{title}</span>
        <span className="text-muted-foreground">没有候选</span>
      </div>
    );
  }
  return (
    <details className="rounded-lg border border-border p-2.5">
      <summary className="cursor-pointer text-xs font-medium text-foreground">
        {title}（{candidates.length}）
      </summary>
      <ul className="mt-2 flex flex-col gap-2">
        {candidates.map((candidate, index) => (
          <li key={`${title}-${index}`} className="rounded-md bg-muted/40 p-2">
            <MetricList metrics={candidate} emptyText="候选没有可读字段。" />
          </li>
        ))}
      </ul>
    </details>
  );
}

function EventRow({ event }: { event: AdminPlanningEvent }) {
  return (
    <li className="flex flex-col gap-1 rounded-lg border border-border p-2.5 sm:flex-row sm:items-start sm:gap-3">
      <span className="shrink-0 font-mono text-[11px] whitespace-nowrap text-muted-foreground">
        {formatDateTime(event.at)}
      </span>
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{event.event || "未命名事件"}</p>
        {event.detail ? (
          <p className="mt-0.5 text-xs leading-5 break-words text-muted-foreground">{event.detail}</p>
        ) : null}
      </div>
    </li>
  );
}

function Pagination({
  total,
  offset,
  count,
  onPrevious,
  onNext,
}: {
  total: number;
  offset: number;
  count: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const from = count === 0 ? 0 : offset + 1;
  const to = offset + count;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span className="tabular">
        第 {formatNumber(from)}–{formatNumber(to)} 条 / 共 {formatNumber(total)} 条
      </span>
      <div className="flex items-center gap-1.5">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={onPrevious}>
          <ChevronLeft />
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={offset + PAGE_SIZE >= total || count === 0}
          onClick={onNext}
        >
          下一页
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

function matchesQuery(session: AdminPlanningSessionSummary, q: string): boolean {
  const needle = q.toLowerCase();
  return (
    session.session_id.toLowerCase().includes(needle) ||
    (session.run_id ?? "").toLowerCase().includes(needle)
  );
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
