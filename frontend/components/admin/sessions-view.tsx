"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowUpRight, ChevronLeft, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { AdminDetailDialog, JsonBlock } from "@/components/admin/detail-dialog";
import { DescriptionList, PanelSection, type DetailItem } from "@/components/admin/metric-list";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { formatNumber } from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { formatDateTime } from "@/lib/format";
import { getAdminPlanningSession, getAdminPlanningSessions } from "@/lib/admin-api";
import type {
  AdminPlanningEvent,
  AdminPlanningSessionDetail,
  AdminPlanningSessionList,
  AdminPlanningSessionSummary,
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

/* -------------------- 详情视图：按后端的分块信封逐块渲染 -------------------- */

/** Discovery 的四条线固定顺序：交通 / 酒店 / 攻略 / 地点。 */
const DISCOVERY_STAGE_ORDER = ["transport", "hotels", "social", "places"] as const;

const DISCOVERY_STAGE_LABELS: Record<string, string> = {
  transport: "交通",
  hotels: "酒店",
  social: "攻略检索",
  places: "地点抽取",
};

/** 单条线的 status → 中文。契约只保证 OK/EMPTY/FAILED/REUSED，其余原样显示。 */
const DISCOVERY_STAGE_STATUS_LABELS: Record<string, string> = {
  OK: "成功",
  EMPTY: "无数据",
  FAILED: "失败",
  REUSED: "复用",
  PENDING: "待处理",
  RUNNING: "进行中",
};

/** Discovery 整体状态 → 中文。 */
const DISCOVERY_OVERALL_LABELS: Record<string, string> = {
  PENDING: "待开始",
  RUNNING: "进行中",
  READY: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
};

const POI_STATE_LABELS: Record<string, string> = {
  MUST: "必去",
  WANT: "想去",
  REJECT: "不感兴趣",
};

/** 后端结构化偏好里的哨兵字符串（数值 / 文本 / 布尔补充项共用）。 */
const SENTINEL_LABELS: Record<string, string> = {
  auto: "帮我选",
  unlimited: "不限",
  undecided: "不确定",
};

/** 事件名 → 中文。未登记的取值原样显示，保证新事件不会消失。 */
const EVENT_LABELS: Record<string, string> = {
  session_created: "会话创建",
  transport_prefetch_started: "交通预取开始",
  transport_prefetch_finished: "交通预取完成",
  hotel_prefetch_started: "酒店预取开始",
  hotel_prefetch_finished: "酒店预取完成",
  social_discovery_started: "攻略检索开始",
  social_discovery_finished: "攻略检索完成",
  place_extraction_started: "地点抽取开始",
  place_extraction_finished: "地点抽取完成",
  user_preferences_updated: "偏好更新",
  session_confirmed: "用户确认",
  run_started: "正式规划开始",
  session_cancelled: "用户取消",
  session_expired: "会话过期",
  discovery_finished: "Discovery 完成",
  discovery_failed: "Discovery 失败",
  discovery_queued: "Discovery 已排队",
};

function stageLabel(key: string): string {
  return DISCOVERY_STAGE_LABELS[key] ?? key;
}

function stageStatusLabel(status: string | null | undefined): string {
  if (!status) return "无数据";
  return DISCOVERY_STAGE_STATUS_LABELS[status] ?? status;
}

function overallLabel(overall: string | null | undefined): string {
  if (!overall) return "未知";
  return DISCOVERY_OVERALL_LABELS[overall] ?? overall;
}

function poiStateLabel(state: string | null | undefined): string {
  if (!state) return "未表态";
  return POI_STATE_LABELS[state] ?? state;
}

/** 哨兵字符串 → 中文；其余（含数字以外的原始枚举）原样显示。 */
function sentinelOrRaw(value: string): string {
  return SENTINEL_LABELS[value] ?? value;
}

/** 数值 / 哨兵 / null 的统一展示口径：null 表示「没表态，走后端默认」。 */
function scalarOrRaw(value: number | string | null | undefined, fallback = "未指定（走默认）"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "number") return formatNumber(value);
  return sentinelOrRaw(value);
}

function eventLabel(event: string | null | undefined): string {
  if (!event) return "未命名事件";
  return EVENT_LABELS[event] ?? event;
}

/** 分阶段耗时：毫秒 → 「1.2 秒」/「850 毫秒」。 */
function formatStageDuration(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value < 1000) return `${Math.round(value)} 毫秒`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

function eventTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function SessionDetailBody({ data }: { data: AdminPlanningSessionDetail }) {
  return (
    <div className="flex flex-col gap-5">
      <BasicInfoSection info={data.basic_info} session={data.session} />
      <PreferenceSection
        preferences={data.preferences}
        labels={data.preference_labels}
        capabilities={data.session.capabilities}
      />
      <DiscoverySection discovery={data.discovery} />
      <PrefetchSection summary={data.prefetch_summary} raw={data} />
      <PoiSelectionsSection selections={data.poi_selections} />
      <EventsSection events={data.events} />
      <RunLinkSection runLink={data.run_link} />
    </div>
  );
}

function BasicInfoSection({
  info,
  session,
}: {
  info: AdminPlanningSessionDetail["basic_info"];
  session: AdminPlanningSessionDetail["session"];
}) {
  const statusItems: DetailItem[] = [
    { label: "会话状态", value: <StatusBadge status={session.status ?? null} /> },
    {
      label: "Discovery",
      value: (
        <StatusBadge
          status={session.discovery_status ?? null}
          label={overallLabel(session.discovery_status)}
        />
      ),
    },
    {
      label: "错误",
      value: session.error ? (
        <span className="text-danger-subtle-foreground">{session.error}</span>
      ) : (
        "无"
      ),
    },
  ];

  const infoItems: DetailItem[] = info
    ? [
        { label: "出发地", value: info.origin || "未填" },
        { label: "目的地", value: info.destination || "未填" },
        { label: "出发日期", value: info.start_date || "未填" },
        {
          label: "返程日期 / 天数",
          value: `${info.end_date || "—"} / ${info.days ? `${formatNumber(info.days)} 天` : "—"}`,
        },
        {
          label: "人数",
          value: info.travelers ? `${formatNumber(info.travelers)} 人` : "未填",
        },
        { label: "预算", value: scalarOrRaw(info.budget_total, "未填") },
        { label: "创建时间", value: formatDateTime(info.created_at) },
        { label: "更新时间", value: formatDateTime(info.updated_at) },
        { label: "过期时间", value: formatDateTime(info.expires_at) },
      ]
    : [];

  return (
    <PanelSection title="基础信息" description="行程范围、会话状态与时间戳。">
      <DescriptionList items={[...statusItems, ...infoItems]} />
      {info ? null : (
        <SectionEmpty
          className="mt-1"
          title="没有返回基础信息"
          description="basic_info 块缺失或为空，行程范围与时间戳无法展示；上方的会话状态仍然可用。"
        />
      )}
    </PanelSection>
  );
}

function PreferenceSection({
  preferences,
  labels,
  capabilities,
}: {
  preferences: AdminPlanningSessionDetail["preferences"];
  labels: AdminPlanningSessionDetail["preference_labels"];
  capabilities: AdminPlanningSessionDetail["session"]["capabilities"];
}) {
  const constraints = labels?.transport_constraints?.length
    ? labels.transport_constraints
    : (preferences?.transport_constraints ?? []).map(sentinelOrRaw);

  const chips: { label: string; value: string }[] = [
    {
      label: "交通方式",
      value: labels?.transport_mode ?? scalarOrRaw(preferences?.transport_mode, "未返回"),
    },
    {
      label: "交通排序",
      value: labels?.transport_priority ?? scalarOrRaw(preferences?.transport_priority, "未返回"),
    },
    { label: "附加约束", value: constraints.length > 0 ? constraints.join("、") : "无附加约束" },
    {
      label: "酒店策略",
      value: labels?.hotel_priority ?? scalarOrRaw(preferences?.hotel_priority, "未返回"),
    },
    { label: "节奏", value: labels?.pace ?? scalarOrRaw(preferences?.pace, "未返回") },
  ];

  const starDisabled = capabilities?.hotel_star_filter === false;
  const starNote =
    capabilities?.hotel_star_note ??
    (starDisabled ? "数据源当前不返回星级字段，暂不生效。" : null);
  const minStar = preferences?.hotel_min_star;

  const hotelItems: DetailItem[] = preferences
    ? [
        {
          label: "每晚价格上限",
          value:
            typeof preferences.hotel_max_price_per_night === "number"
              ? `≤ ${formatNumber(preferences.hotel_max_price_per_night)} 元 / 晚`
              : scalarOrRaw(preferences.hotel_max_price_per_night),
        },
        {
          label: "评分下限",
          value:
            typeof preferences.hotel_min_rating === "number"
              ? `≥ ${formatNumber(preferences.hotel_min_rating)} 分`
              : scalarOrRaw(preferences.hotel_min_rating),
        },
        { label: "房型", value: scalarOrRaw(preferences.hotel_room_type) },
        {
          label: "是否接受换酒店",
          value:
            typeof preferences.hotel_allow_change === "boolean"
              ? preferences.hotel_allow_change
                ? "可以换"
                : "不想换"
              : scalarOrRaw(preferences.hotel_allow_change),
        },
        {
          label: "最低星级",
          value:
            minStar === null || minStar === undefined || minStar === ""
              ? "未指定"
              : `${scalarOrRaw(minStar)}${starDisabled ? "（数据源无星级字段，暂不生效）" : ""}`,
        },
      ]
    : [];

  return (
    <PanelSection title="用户偏好" description="交通、住宿与节奏；中文标签由后端 preference_labels 提供。">
      <div className="flex flex-wrap items-center gap-2">
        {chips.map((chip) => (
          <ValueChip key={chip.label} label={chip.label} value={chip.value} />
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">酒店补充条件</p>
      {preferences ? (
        <DescriptionList items={hotelItems} />
      ) : (
        <SectionEmpty
          title="没有返回结构化偏好"
          description="preferences 块缺失，交通 / 住宿 / 节奏的原始取值无法展示。"
        />
      )}
      {starNote ? (
        <p className="mt-1 rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
          {starNote}
        </p>
      ) : null}
    </PanelSection>
  );
}

function DiscoverySection({ discovery }: { discovery: AdminPlanningSessionDetail["discovery"] }) {
  if (!discovery) {
    return (
      <PanelSection title="Discovery 分阶段" description="四条预取线的状态与耗时。">
        <SectionEmpty
          title="没有返回 Discovery 进度"
          description="discovery 块缺失，无法判断 Discovery 卡在哪一步。"
        />
      </PanelSection>
    );
  }

  const stages = discovery.stages ?? {};
  const known = DISCOVERY_STAGE_ORDER.map((key) => [key, stages[key]] as const);
  // 后端将来新增一条线时也要显示出来，而不是因为没有登记就丢掉。
  const extraKeys = Object.keys(stages).filter(
    (key) => !(DISCOVERY_STAGE_ORDER as readonly string[]).includes(key),
  );
  const rows = [...known, ...extraKeys.map((key) => [key, stages[key]] as const)];

  return (
    <PanelSection title="Discovery 分阶段" description="四条预取线的状态、耗时、结果数与降级。">
      <div className="flex items-center gap-2 text-xs">
        <span className="text-muted-foreground">整体</span>
        <StatusBadge
          status={discovery.overall ?? null}
          label={overallLabel(discovery.overall)}
        />
      </div>
      <ul className="flex flex-col">
        {rows.map(([key, stage]) => (
          <li
            key={key}
            className="flex flex-col gap-1 border-b border-border/60 py-2 text-xs last:border-b-0"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium text-foreground">{stageLabel(key)}</span>
              <StatusBadge
                status={stage?.status ?? null}
                label={stageStatusLabel(stage?.status)}
              />
            </div>
            {stage ? (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                <span>耗时 {formatStageDuration(stage.duration_ms)}</span>
                <span>结果 {formatNumber(stage.result_count)} 条</span>
                <span>{stage.degraded ? "已降级" : "未降级"}</span>
                <span>开始 {formatDateTime(stage.started_at)}</span>
                <span>结束 {formatDateTime(stage.finished_at)}</span>
              </div>
            ) : (
              <p className="text-[11px] text-muted-foreground">后端未返回这一条线的状态。</p>
            )}
            {stage?.error ? (
              <p className="text-[11px] leading-5 break-words text-danger-subtle-foreground">
                错误：{stage.error}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
      {discovery.degradations && discovery.degradations.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {discovery.degradations.map((item, index) => (
            <li
              key={`${index}-${item}`}
              className="rounded-md border border-warning/25 bg-warning-subtle/60 px-2.5 py-1.5 text-xs leading-5 break-words text-warning-subtle-foreground"
            >
              {item}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-muted-foreground">本次 Discovery 没有降级。</p>
      )}
    </PanelSection>
  );
}

function PrefetchSection({
  summary,
  raw,
}: {
  summary: AdminPlanningSessionDetail["prefetch_summary"];
  raw: AdminPlanningSessionDetail;
}) {
  return (
    <PanelSection title="Prefetch 摘要" description="后台预取到的候选数量与 Provider 调用量。">
      {summary ? (
        <DescriptionList
          items={[
            { label: "交通候选", value: formatNumber(summary.transport_candidates) },
            { label: "酒店候选", value: formatNumber(summary.hotel_candidates) },
            { label: "攻略证据", value: formatNumber(summary.evidence_count) },
            { label: "地点候选", value: formatNumber(summary.place_candidates) },
            { label: "Provider 调用", value: formatNumber(summary.provider_calls) },
          ]}
        />
      ) : (
        <SectionEmpty
          title="没有返回 Prefetch 摘要"
          description="prefetch_summary 块缺失，候选数量无法展示。"
        />
      )}
      <details className="rounded-lg border border-border p-2.5">
        <summary className="cursor-pointer text-xs font-medium text-foreground">
          原始 JSON（默认折叠）
        </summary>
        <JsonBlock value={raw} maxHeight="18rem" className="mt-2" />
      </details>
    </PanelSection>
  );
}

function PoiSelectionsSection({
  selections,
}: {
  selections: AdminPlanningSessionDetail["poi_selections"];
}) {
  if (!selections) {
    return (
      <PanelSection title="用户选择" description="POI 的取舍统计与逐条明细。">
        <SectionEmpty
          title="没有返回用户选择"
          description="poi_selections 块缺失，无法展示 POI 取舍。"
        />
      </PanelSection>
    );
  }

  const counts = selections.counts ?? {};
  const items = selections.items ?? [];
  return (
    <PanelSection title="用户选择" description="POI 的取舍统计与逐条明细。">
      <DescriptionList
        items={[
          { label: "必去", value: formatNumber(counts.must) },
          { label: "想去", value: formatNumber(counts.want) },
          { label: "不感兴趣", value: formatNumber(counts.reject) },
          { label: "未表态", value: formatNumber(counts.neutral) },
        ]}
      />
      {items.length > 0 ? (
        <ul className="mt-1 flex flex-col gap-1.5">
          {items.map((item) => (
            <li
              key={item.place_id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs"
            >
              <span className="min-w-0 truncate text-foreground">{item.name || item.place_id}</span>
              <ToneBadge tone="muted">{poiStateLabel(item.state)}</ToneBadge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-xs text-muted-foreground">没有逐条 POI 选择记录。</p>
      )}
    </PanelSection>
  );
}

function EventsSection({ events }: { events: AdminPlanningEvent[] }) {
  // 按时间升序：把「会话创建 → … → 正式规划开始」排成可读的时间线。
  const sorted = events
    .map((event, index) => ({ event, index }))
    .sort((a, b) => {
      const left = eventTime(a.event.at);
      const right = eventTime(b.event.at);
      if (left === null && right === null) return a.index - b.index;
      if (left === null) return 1;
      if (right === null) return -1;
      return left - right || a.index - b.index;
    })
    .map((item) => item.event);

  return (
    <PanelSection title="事件时间线" description="会话过程中的关键节点，按时间升序。">
      {sorted.length > 0 ? (
        <ol className="flex flex-col gap-2">
          {sorted.map((event, index) => (
            <li
              key={`${event.at ?? ""}-${event.event ?? ""}-${index}`}
              className="flex flex-col gap-0.5 rounded-lg border border-border p-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-medium text-foreground">{eventLabel(event.event)}</span>
                <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
                  {formatDateTime(event.at)}
                </span>
              </div>
              {event.detail ? (
                <p className="text-[11px] leading-5 break-words text-muted-foreground">
                  {event.detail}
                </p>
              ) : null}
            </li>
          ))}
        </ol>
      ) : (
        <SectionEmpty title="没有事件" description="后端没有返回该会话的事件列表。" />
      )}
    </PanelSection>
  );
}

function RunLinkSection({ runLink }: { runLink: AdminPlanningSessionDetail["run_link"] }) {
  if (!runLink.has_run || !runLink.run_id) {
    return (
      <PanelSection title="Session → Run" description="这份前置选择最终进入了哪次正式规划。">
        <p className="text-xs text-muted-foreground">尚未创建正式 Run。</p>
      </PanelSection>
    );
  }

  return (
    <PanelSection title="Session → Run" description="这份前置选择最终进入了哪次正式规划。">
      <DescriptionList
        items={[
          {
            label: "Run ID",
            value: (
              <Link
                href={`/admin/runs/${encodeURIComponent(runLink.run_id)}`}
                title={runLink.run_id}
                className="inline-flex items-center gap-1 font-mono text-primary underline-offset-4 hover:underline"
              >
                {runLink.run_id}
                <ArrowUpRight className="size-3" aria-hidden />
              </Link>
            ),
          },
          { label: "Run 状态", value: <StatusBadge status={runLink.run_status ?? null} /> },
          { label: "开始时间", value: formatDateTime(runLink.started_at) },
          { label: "结束时间", value: formatDateTime(runLink.finished_at) },
        ]}
      />
      <div className="mt-1">
        <Button
          variant="outline"
          size="xs"
          nativeButton={false}
          render={<Link href={`/admin/runs/${encodeURIComponent(runLink.run_id)}`} />}
        >
          打开 Run 详情
          <ArrowUpRight />
        </Button>
      </div>
    </PanelSection>
  );
}

function ValueChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <ToneBadge tone="muted">{value}</ToneBadge>
    </span>
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
