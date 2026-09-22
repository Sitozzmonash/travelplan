"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { RefreshCw, Route, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { PageHeader } from "@/components/admin/page-header";
import { PartialBanner, ResourceView } from "@/components/admin/admin-states";
import { StatCard } from "@/components/admin/stat-card";
import { StatusBadge, ToneBadge, type AdminTone } from "@/components/admin/status-badge";
import { Sparkline } from "@/components/admin/trend-sparkline";
import { WindowSelect, windowLabel } from "@/components/admin/window-select";
import {
  formatCost,
  formatDurationMs,
  formatMetricValue,
  formatNumber,
  formatPercentNumber,
  formatRatio,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { cn } from "@/lib/utils";
import { getAdminTravelQuality } from "@/lib/admin-api";
import {
  ADMIN_GRADE_LABELS,
  ADMIN_WINDOW_OPTIONS,
  type AdminGrade,
  type AdminQualityDestination,
  type AdminQualityDimension,
  type AdminQualityMetric,
  type AdminQualityRun,
  type AdminRecord,
  type AdminTravelQuality,
  type AdminTrendSeries,
  type AdminWindowKey,
} from "@/types/admin";

/**
 * 行程质量：回答"系统虽然成功生成了行程，但生成得好不好"。
 *
 * 两条不可退让的展示规则（与 Provider 页同一套纪律）：
 * 1) `score / score_percent` 为 null 时显示「无数据」，绝不落成 0 分；
 * 2) 六个维度按后端返回原样渲染（不写死分值），未知维度追加在末尾，不丢数据。
 *
 * 口径（feedback/TravelPlan_Admin_Feedback_01.md §2）：质量分不是「系统统一认为
 * 这趟旅行好不好」，而是「这趟旅行是否适合这个用户」——软维度按本次动态偏好画像
 * 调权。**加权数值由后端算**（A16），前端只负责解释与展示。
 */

/** 页面的默认窗口是 7d：质量问题的样本量本来就小，24h 经常只剩个位数。 */
const DEFAULT_WINDOW: AdminWindowKey = "7d";

/** 维度顺序：与 doc §4 的阅读顺序一致；不在这张表里的 key 追加到最后。 */
const DIMENSION_ORDER = [
  "preference_match",
  "hotel_quality",
  "food_quality",
  "itinerary_quality",
  "route_quality",
  "hard_validation",
];

/** 后端会带中文 label；这里做兜底，避免 label 缺失时只剩机器 key。 */
const DIMENSION_LABELS: Record<string, string> = {
  preference_match: "偏好匹配",
  hotel_quality: "住宿质量",
  food_quality: "美食质量",
  itinerary_quality: "行程质量",
  route_quality: "路线质量",
  hard_validation: "硬校验",
};

/** 一句话说明这一维度到底在查什么，避免只给一个分数。 */
const DIMENSION_HINTS: Record<string, string> = {
  preference_match: "用户声明的偏好 → 动态偏好画像 → 最终计划有没有体现出来",
  hotel_quality: "住宿区域是否符合偏好，酒店的评分、价格与选择理由",
  food_quality: "餐饮槽位、真实门店、证据覆盖与平均绕路",
  itinerary_quality: "每天停留点数量、类型多样性、区域标注与负荷均衡",
  route_quality: "路段是否经高德核实、有没有折返与超长单段",
  hard_validation: "只算离开系统时仍然存在的硬问题，已解决的不算",
};

/** 总分权重是单 run 软指标的权重（后端 QUALITY_WEIGHTS），标签在这里补中文。 */
const WEIGHT_LABELS: Record<string, string> = {
  preference_coverage: "偏好覆盖",
  pace_match: "节奏",
  category_diversity: "类型多样性",
  backtracking_score: "无折返",
  daily_load_balance: "负荷均衡",
  meal_time_quality: "用餐时段",
  route_efficiency: "路线效率",
};

const GRADE_TONE: Record<string, AdminTone> = {
  GOOD: "success",
  FAIR: "warning",
  POOR: "danger",
  UNKNOWN: "muted",
};

/**
 * 动态偏好画像来源 → 文案。
 * `llm` = 模型真的按用户生成了画像（分数才算「适合这个用户」）；
 * `fallback` = 模型没给出、用了均衡基线（仍能出计划，但个性化有限）。
 * 未登记的取值原样显示，便于对照后端。
 */
const PROFILE_SOURCE_LABELS: Record<string, string> = {
  llm: "已个性化",
  fallback: "均衡基线",
};

export function TravelQualityView() {
  // 深链参数只在首次挂载读一次：Dashboard 的「需要关注」会带 `?focus=<dimension_key>`，
  // 也接受 `?window=<1h|24h|7d|30d>`。
  // 用惰性初始化而不是 useSearchParams：后者要求整页包一层 Suspense（build 期约束）。
  // 也不用「在 effect 里 setState 回填」的写法：仓库的 react-hooks/set-state-in-effect
  // 规则会直接报错，而且管理台外壳在 hydration 前不渲染子树（见 admin-shell），
  // 这里首次求值时 window 一定可用（与 sessions-view 读 `?q=` 的写法一致）。
  const [urlParams] = useState(() => {
    if (typeof window === "undefined") return { focus: null, window: null };
    const params = new URLSearchParams(window.location.search);
    const nextWindow = params.get("window");
    return {
      focus: params.get("focus") || null,
      window:
        nextWindow && ADMIN_WINDOW_OPTIONS.some((option) => option.key === nextWindow)
          ? (nextWindow as AdminWindowKey)
          : null,
    };
  });
  const [windowKey, setWindowKey] = useState<AdminWindowKey>(urlParams.window ?? DEFAULT_WINDOW);
  const focusKey = urlParams.focus;
  const scrolledRef = useRef(false);

  const resource = useAdminResource<AdminTravelQuality>(
    `admin-travel-quality:${windowKey}`,
    () => getAdminTravelQuality({ window: windowKey }),
  );

  // 数据到位后滚动到目标维度。高亮不在这里 setState（规则禁止），而是由 focusKey 直接决定
  // className —— 保持高亮比"闪一下"更有用：操作员点进来后能一直看清是哪一维触发的跳转。
  useEffect(() => {
    if (!focusKey || !resource.data || scrolledRef.current) return;
    scrolledRef.current = true;
    document
      .getElementById(dimensionAnchorId(focusKey))
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [focusKey, resource.data]);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="行程质量"
        description="回答「系统虽然成功生成了行程，但生成得好不好」。所有分值都来自后端对已落库计划的评估，管理台不做二次打分。"
        actions={
          <>
            <WindowSelect value={windowKey} onChange={setWindowKey} className="w-36" />
            <Button variant="outline" size="sm" onClick={resource.reload}>
              <RefreshCw />
              重算 / 刷新
            </Button>
          </>
        }
      />

      <ResourceView resource={resource} loadingRows={6}>
        {(data) => <QualityBody data={data} focusKey={focusKey} />}
      </ResourceView>
    </div>
  );
}

function dimensionAnchorId(key: string): string {
  return `quality-dim-${key}`;
}

/* --------------- 运行时容错读取（画像/加权字段名后端未定稿） --------------- */

/**
 * 后端 A16（质量分动态化）的字段名尚未定稿，因此不绑定单一键名：
 * 这里按 `AdminRecord` 做运行时读取，取不到就返回 null，由展示层落成
 * 「—（后端未返回）」。与 run-detail-view / sessions-view 的 readString 同一套写法。
 * 前端**绝不**自己按固定权重重算分数 —— 那会和后端口径打架，也会把
 * 「后端还没算」伪装成一个看起来很确定的数。
 */
function readString(record: AdminRecord, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function readNumber(record: AdminRecord, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readBoolean(record: AdminRecord, key: string): boolean | null {
  const value = record[key];
  return typeof value === "boolean" ? value : null;
}

function readRecord(record: AdminRecord, key: string): AdminRecord | null {
  const value = record[key];
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as AdminRecord)
    : null;
}

/** 后端未返回时的占位：显式写「后端未返回」，避免空值被读成 0 或「无问题」。 */
const NOT_RETURNED = "—（后端未返回）";

function QualityBody({ data, focusKey }: { data: AdminTravelQuality; focusKey: string | null }) {
  const dimensions = orderDimensions(data.dimensions);

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="窗口内运行"
          value={formatNumber(data.scan.runs_in_window)}
          hint={windowLabel(data.window, data.window_label || null)}
        />
        <StatCard
          label="已完成"
          value={formatNumber(data.scan.finished)}
          hint="只有已完成运行才参与评估"
        />
        <StatCard
          label="已评估"
          value={formatNumber(data.scan.scored)}
          tone="info"
          hint="有计划且能解析出来的运行"
        />
        <StatCard
          label="无计划（未评估）"
          value={formatNumber(data.scan.unscored)}
          tone={data.scan.unscored > 0 ? "warning" : "muted"}
          hint="历史 run 可能没落 plan.json"
        />
      </div>

      {data.scan.scored === 0 ? (
        <PartialBanner
          title="窗口内没有可评估的计划"
          description="下面六个维度都会显示「无数据」——这不是 0 分，而是没有计划可算。原因见页尾「口径与数据缺口」。"
        />
      ) : null}

      <OverallScore data={data} />

      <PreferenceWeightingCard data={data} />

      {dimensions.map((dimension) => (
        <DimensionSection
          key={dimension.key}
          dimension={dimension}
          focused={focusKey === dimension.key}
        />
      ))}

      <RunsTable runs={data.runs} />
      <DestinationsTable destinations={data.destinations} />
      <QualityTrendCard trend={data.quality_trend} />
      <NotesBlock notes={data.notes} />
    </div>
  );
}

/** 固定顺序渲染；payload 里没见过的 key 追加在末尾，保证后端新增维度不会消失。 */
function orderDimensions(dimensions: AdminQualityDimension[]): AdminQualityDimension[] {
  return [...dimensions].sort((a, b) => index(a.key) - index(b.key));

  function index(key: string): number {
    const found = DIMENSION_ORDER.indexOf(key);
    return found === -1 ? DIMENSION_ORDER.length : found;
  }
}

function gradeText(grade: AdminGrade, label?: string | null): string {
  return label || ADMIN_GRADE_LABELS[grade] || grade;
}

function GradeBadge({ grade, label }: { grade: AdminGrade; label?: string | null }) {
  return (
    <ToneBadge tone={GRADE_TONE[grade] ?? "muted"} className="text-[0.6875rem]">
      <span title={grade}>{gradeText(grade, label)}</span>
    </ToneBadge>
  );
}

function OverallScore({ data }: { data: AdminTravelQuality }) {
  const overall = data.overall;
  const weightEntries = Object.entries(overall.weights);

  return (
    <Card>
      <CardHeader>
        <CardTitle>总分</CardTitle>
        <CardDescription>{overall.note || "总分由后端按维度得分合成。"}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-3">
          {overall.score === null ? (
            <span className="text-2xl font-semibold text-muted-foreground" title="窗口内没有可评估的计划">
              无数据
            </span>
          ) : (
            <span className="tabular text-3xl leading-none font-semibold tracking-tight text-foreground">
              {formatRatio(overall.score)}
            </span>
          )}
          <GradeBadge grade={overall.grade} label={overall.grade_label} />
        </div>

        {weightEntries.length > 0 ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] text-muted-foreground">
              单 run 质量分权重（画像缺失时的均衡基线；画像可用时后端按用户类型调整）
            </span>
            <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
              {weightEntries.map(([key, value]) => (
                <li key={key} className="inline-flex items-baseline gap-1">
                  <span>{WEIGHT_LABELS[key] ?? key}</span>
                  <span className="tabular font-medium text-foreground">
                    {typeof value === "number" ? formatPercentNumber(value * 100) : formatMetricValue(value)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * 「质量分为什么因用户类型而不同」的口径卡。
 *
 * 后端 A16（质量分动态化）的字段名尚未定稿，因此不绑定单一键名：按候选键做运行时
 * 容错读取，取不到就显示「—（后端未返回）」。前端绝不用固定权重自己重算分数。
 *
 * 假设 / 尝试的字段名（待与后端对齐，见汇报）：
 *   overall.profile_source | overall.source
 *   overall.profile_style | overall.travel_style
 *   overall.weighting_mode | overall.weight_mode
 *   overall.profile_weighted | overall.dynamic_weighted
 *   overall.profile_weights | overall.dynamic_weights | overall.weights_by_profile
 *   overall.profile_note | overall.weight_note
 * 顶层同名键一并尝试（信封可能把画像放在 overall 之外）。
 */
function PreferenceWeightingCard({ data }: { data: AdminTravelQuality }) {
  const overall = data.overall as unknown as AdminRecord;
  const payload = data as unknown as AdminRecord;

  const source =
    readString(overall, "profile_source") ??
    readString(overall, "source") ??
    readString(payload, "profile_source");
  const style =
    readString(overall, "profile_style") ??
    readString(overall, "travel_style") ??
    readString(payload, "profile_style") ??
    readString(payload, "travel_style");
  const mode =
    readString(overall, "weighting_mode") ??
    readString(overall, "weight_mode") ??
    readString(payload, "weighting_mode") ??
    readString(payload, "weight_mode");
  const weighted =
    readBoolean(overall, "profile_weighted") ??
    readBoolean(overall, "dynamic_weighted") ??
    readBoolean(payload, "profile_weighted") ??
    readBoolean(payload, "dynamic_weighted");
  const weights =
    readRecord(overall, "profile_weights") ??
    readRecord(overall, "dynamic_weights") ??
    readRecord(overall, "weights_by_profile") ??
    readRecord(payload, "profile_weights") ??
    readRecord(payload, "dynamic_weights");
  const note = readString(overall, "profile_note") ?? readString(overall, "weight_note");

  const modeText =
    mode ??
    (weighted === true ? "已按画像动态加权" : weighted === false ? "固定权重（未动态化）" : null);
  const weightEntries = Object.entries(weights ?? {});
  const fields: { label: string; value: string }[] = [
    { label: "本次画像来源", value: source ? (PROFILE_SOURCE_LABELS[source] ?? source) : NOT_RETURNED },
    { label: "画像风格", value: style ?? NOT_RETURNED },
    { label: "加权模式", value: modeText ?? NOT_RETURNED },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>质量分口径：按本次偏好画像加权</CardTitle>
        <CardDescription>
          质量分不是「系统统一认为这趟旅行好不好」，而是「这趟旅行是否适合这个用户」。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-[11px] leading-5 text-muted-foreground">
          软维度（偏好匹配、美食、节奏、路线等）按本次行程的动态偏好画像调权：美食型抬高美食与
          商圈 / 夜间便利；老人轻松型抬高路线距离、交通便利与节奏舒适，并压低「每天景点数量」；
          景点型抬高兴趣匹配、MUST 覆盖与区域规划。硬性约束（时间窗、营业时间、预算）不参与调权，
          仍由规则判定。画像缺失（Quick 或 fallback）时回退到均衡基线。加权由后端计算，前端不自行算分。
        </p>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 sm:grid-cols-3">
          {fields.map((field) => (
            <div key={field.label} className="flex flex-col gap-0.5">
              <dt className="text-[11px] text-muted-foreground">{field.label}</dt>
              <dd className="text-xs font-medium text-foreground">{field.value}</dd>
            </div>
          ))}
        </dl>
        {weightEntries.length > 0 ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] text-muted-foreground">本次画像加权（后端返回）</span>
            <ul className="flex flex-wrap gap-1.5">
              {weightEntries.map(([key, value]) => (
                <li
                  key={key}
                  className="inline-flex items-baseline gap-1 rounded bg-muted px-1.5 py-0.5 text-[11px]"
                >
                  <span className="text-muted-foreground">{WEIGHT_LABELS[key] ?? key}</span>
                  <span className="tabular font-medium text-foreground">{formatMetricValue(value)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="text-[11px] leading-4 text-muted-foreground">
            后端未返回「本次画像加权」明细。上面几项显示「后端未返回」表示该字段尚未提供，
            并不代表这趟旅行没有画像：单次 run 的画像可在运行详情页查看。
          </p>
        )}
        {note ? <p className="text-[11px] leading-4 text-muted-foreground">口径说明：{note}</p> : null}
      </CardContent>
    </Card>
  );
}

function DimensionSection({
  dimension,
  focused,
}: {
  dimension: AdminQualityDimension;
  focused: boolean;
}) {
  const label = dimension.label || DIMENSION_LABELS[dimension.key] || dimension.key;
  // 单个维度的画像权重（后端动态化后才会返回）；取不到就不显示，不猜。
  const dimensionRecord = dimension as unknown as AdminRecord;
  const profileWeight =
    readNumber(dimensionRecord, "profile_weight") ?? readNumber(dimensionRecord, "weight");

  return (
    <Card
      id={dimensionAnchorId(dimension.key)}
      className={cn("scroll-mt-16", focused && "ring-2 ring-info/50")}
    >
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex flex-wrap items-center gap-2">
            {label}
            <GradeBadge grade={dimension.grade} label={dimension.grade_label} />
          </CardTitle>
          {dimension.score_percent === null ? (
            <span className="flex flex-wrap items-center gap-2">
              {profileWeight !== null ? (
                <span
                  className="text-[11px] text-muted-foreground"
                  title="按本次动态偏好画像调整后的维度权重（后端返回）"
                >
                  画像权重 {formatRatio(profileWeight)}
                </span>
              ) : null}
              <span className="text-sm text-muted-foreground" title="这一维度没有足够数据打分">
                无数据
              </span>
            </span>
          ) : (
            <span className="flex flex-wrap items-baseline gap-2">
              {profileWeight !== null ? (
                <span
                  className="text-[11px] text-muted-foreground"
                  title="按本次动态偏好画像调整后的维度权重（后端返回）"
                >
                  画像权重 {formatRatio(profileWeight)}
                </span>
              ) : null}
              <span className="tabular text-sm font-semibold text-foreground">
                {formatPercentNumber(dimension.score_percent)}
              </span>
            </span>
          )}
        </div>
        {DIMENSION_HINTS[dimension.key] ? (
          <CardDescription className="text-xs">{DIMENSION_HINTS[dimension.key]}</CardDescription>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <QualityMetrics metrics={dimension.metrics} />

        {dimension.findings.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {dimension.findings.map((finding, index) => (
              <li key={index} className="flex items-start gap-2 text-xs">
                <ToneBadge tone={levelTone(finding.level)} className="mt-0.5 shrink-0">
                  {levelLabel(finding.level)}
                </ToneBadge>
                <span className="min-w-0 leading-5 text-foreground">{finding.text}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-muted-foreground">这一维度没有产生告警条目。</p>
        )}

        {dimension.offenders.length > 0 ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] text-muted-foreground">最差运行</span>
            <ul className="flex flex-col gap-1.5">
              {dimension.offenders.map((offender) => (
                <li key={offender.run_id} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <Link
                    href={`/admin/runs/${encodeURIComponent(offender.run_id)}`}
                    className="font-mono text-[11px] break-all text-primary underline-offset-4 hover:underline"
                  >
                    {offender.run_id}
                  </Link>
                  <span className="min-w-0 text-[11px] leading-4 text-muted-foreground">
                    {offender.detail}
                  </span>
                  {offender.value !== null ? (
                    <span className="tabular ml-auto text-[11px] font-medium text-foreground" title="该 run 的质量分">
                      {formatRatio(offender.value)}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {dimension.note ? (
          <p className="text-[11px] leading-4 text-muted-foreground">口径说明：{dimension.note}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function QualityMetrics({ metrics }: { metrics: AdminQualityMetric[] }) {
  if (metrics.length === 0) {
    return <p className="text-xs leading-5 text-muted-foreground">这一维度没有返回指标。</p>;
  }
  return (
    <dl className="grid gap-x-5 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
      {metrics.map((metric) => (
        <div
          key={metric.key}
          className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
        >
          <dt
            className="min-w-0 truncate text-xs text-muted-foreground"
            title={metric.hint ?? metric.key}
          >
            {metric.label || metric.key}
          </dt>
          <dd className="tabular min-w-0 text-right text-xs font-medium break-words text-foreground">
            <MetricValue metric={metric} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function MetricValue({ metric }: { metric: AdminQualityMetric }) {
  const value = metric.value;
  if (value === null || value === undefined) {
    return <span title={metric.hint ?? "后端未返回该指标"}>—</span>;
  }

  // 告警码分布这类对象：渲染成 `CODE: 次数` 的紧凑对，而不是一坨 JSON。
  if (typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <>—</>;
    return (
      <span className="inline-flex flex-wrap justify-end gap-x-2 gap-y-0.5">
        {entries.map(([code, count]) => (
          <span key={code} className="whitespace-nowrap">
            <span className="font-mono text-[11px] text-muted-foreground" title={code}>
              {code}
            </span>
            <span className="ml-1">{formatMetricValue(count)}</span>
          </span>
        ))}
      </span>
    );
  }

  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <>{formatMetricValue(value)}</>;
  }

  return (
    <>
      {formatQualityNumber(value, metric.unit, metric.key, metric.label)}
    </>
  );
}

/**
 * 指标数值的口径判断。
 *
 * 比率（0~1）必须显示成百分比：后端给了 `unit=ratio` 就直接用；
 * unit 缺失时退回关键词判断（「率 / 比例 / 覆盖率 / 得分」…）——
 * 否则 0.83 的覆盖率会被读成「0.83 次」，这是最容易被误读的一类数字。
 * 判定不出比率的数值（评分 4.7、距离 620、计数）一律按普通数字渲染。
 */
function formatQualityNumber(
  value: number,
  unit: string | null,
  key: string,
  label: string,
): string {
  if (unit === "ratio") return formatRatio(value);
  if (unit === "cny") return `¥${formatNumber(value)}`;
  if (unit === "ms") return formatDurationMs(value);
  if (unit === "m") return `${formatNumber(value)} m`;
  const looksLikeRate = /(率|比例|占比|覆盖率|得分|score|ratio|rate|coverage)/i.test(`${key} ${label}`);
  if (looksLikeRate && value >= 0 && value <= 1) return formatRatio(value);
  return formatMetricValue(value);
}

function levelTone(level: string): AdminTone {
  switch (level) {
    case "high":
      return "danger";
    case "medium":
      return "warning";
    default:
      return "muted";
  }
}

function levelLabel(level: string): string {
  switch (level) {
    case "high":
      return "高";
    case "medium":
      return "中";
    case "low":
      return "低";
    default:
      return level;
  }
}

function RunsTable({ runs }: { runs: AdminQualityRun[] }) {
  const columns: AdminColumn<AdminQualityRun>[] = [
    {
      key: "run_id",
      header: "运行",
      primary: true,
      className: "max-w-[12rem]",
      cell: (run) => (
        <Link
          href={`/admin/runs/${encodeURIComponent(run.run_id)}`}
          title={run.run_id}
          className="block max-w-[12rem] truncate font-mono text-xs text-primary underline-offset-4 hover:underline"
        >
          {run.run_id}
        </Link>
      ),
    },
    {
      key: "destination",
      header: "目的地",
      cell: (run) => (
        <span className="whitespace-nowrap text-xs text-foreground">{run.destination ?? "—"}</span>
      ),
    },
    {
      // 画像来源决定了这一行的分数是「按这个用户加权」还是「均衡基线」——
      // 同一分数在不同来源下含义不同，所以必须和分数并排出现。
      key: "profile",
      header: "偏好画像",
      cell: (run) =>
        run.profile_source ? (
          <ToneBadge tone={run.profile_source === "llm" ? "info" : "muted"}>
            {PROFILE_SOURCE_LABELS[run.profile_source] ?? run.profile_source}
          </ToneBadge>
        ) : (
          <span className="text-[11px] text-muted-foreground" title="后端未返回这次 run 的画像来源">
            —
          </span>
        ),
    },
    {
      key: "hotel_area",
      header: "住宿区域",
      mobileHidden: true,
      cell: (run) => (
        <span className="whitespace-nowrap text-xs text-foreground">{run.hotel_area ?? "—"}</span>
      ),
    },
    {
      key: "status",
      header: "状态",
      cell: (run) => <StatusBadge status={run.status} />,
    },
    {
      key: "score",
      header: "质量分",
      align: "right",
      cell: (run) => (
        <span className="inline-flex items-baseline gap-1.5">
          <span className="tabular text-xs">{formatRatio(run.score)}</span>
          <GradeBadge grade={run.grade} />
        </span>
      ),
    },
    {
      key: "preference_coverage",
      header: "偏好覆盖",
      align: "right",
      cell: (run) => <span className="tabular text-xs">{formatRatio(run.preference_coverage)}</span>,
    },
    {
      key: "issues",
      header: "告警 / 硬错误",
      align: "right",
      cell: (run) => (
        <span className="tabular whitespace-nowrap text-xs">
          <span title="并行告警数">{formatNumber(run.warnings)}</span>
          <span className="text-muted-foreground"> / </span>
          <span title="硬错误数" className={run.errors ? "text-danger" : undefined}>
            {formatNumber(run.errors)}
          </span>
        </span>
      ),
    },
    {
      key: "route_verified_rate",
      header: "路线核实率",
      align: "right",
      mobileHidden: true,
      cell: (run) => <span className="tabular text-xs">{formatRatio(run.route_verified_rate)}</span>,
    },
    {
      key: "duration_ms",
      header: "耗时",
      align: "right",
      mobileHidden: true,
      cell: (run) => <span className="tabular text-xs">{formatDurationMs(run.duration_ms)}</span>,
    },
    {
      key: "cost",
      header: "成本",
      align: "right",
      mobileHidden: true,
      cell: (run) => <span className="tabular text-xs">{formatCost(run.cost)}</span>,
    },
  ];

  if (runs.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>逐条运行质量</CardTitle>
          <CardDescription>窗口内没有被评估的运行。</CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            需要已完成且已落库 plan.json 的运行才会出现在这里。
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Route className="size-4 text-muted-foreground" aria-hidden />
          逐条运行质量
        </CardTitle>
        <CardDescription>
          后端按质量分从低到高返回，最差的运行排在最上面（最该先看的那条在第一行）。
          「偏好画像」列标出这次分数是按用户画像加权（已个性化）还是走均衡基线。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <AdminTable columns={columns} rows={runs} getRowKey={(run) => run.run_id} />
      </CardContent>
    </Card>
  );
}

function DestinationsTable({ destinations }: { destinations: AdminQualityDestination[] }) {
  const columns: AdminColumn<AdminQualityDestination>[] = [
    {
      key: "name",
      header: "目的地",
      primary: true,
      cell: (item) => <span className="text-xs text-foreground">{item.name || "未填写"}</span>,
    },
    {
      key: "runs",
      header: "运行数",
      align: "right",
      cell: (item) => <span className="tabular text-xs">{formatNumber(item.runs)}</span>,
    },
    {
      key: "score",
      header: "质量分",
      align: "right",
      cell: (item) => <span className="tabular text-xs">{formatRatio(item.score)}</span>,
    },
    {
      key: "grade",
      header: "等级",
      cell: (item) => <GradeBadge grade={item.grade} />,
    },
    {
      key: "degraded",
      header: "降级",
      align: "right",
      cell: (item) => (
        <span className={cn("tabular text-xs", item.degraded > 0 && "text-warning")}>
          {formatNumber(item.degraded)}
        </span>
      ),
    },
    {
      key: "failed",
      header: "失败",
      align: "right",
      cell: (item) => (
        <span className={cn("tabular text-xs", item.failed > 0 && "text-danger")}>
          {formatNumber(item.failed)}
        </span>
      ),
    },
  ];

  if (destinations.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>目的地维度</CardTitle>
        <CardDescription>同一个目的地的质量分是已评估运行的平均分，样本少时波动会很大。</CardDescription>
      </CardHeader>
      <CardContent>
        <AdminTable
          columns={columns}
          rows={destinations}
          getRowKey={(item) => item.name || "unknown"}
        />
      </CardContent>
    </Card>
  );
}

function QualityTrendCard({ trend }: { trend: AdminTrendSeries }) {
  const points = trend.points.map((point) => point.value);
  const summary = trend.summary;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="size-4 text-muted-foreground" aria-hidden />
          {trend.label || "质量趋势"}
        </CardTitle>
        <CardDescription>
          按时间分桶取已评估运行的平均分；某桶没有样本时线是断开的，不会画成掉到 0。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Sparkline points={points} tone="info" ariaLabel="质量趋势" />
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
          <MetricItem label="最新" value={formatRatio(summary.latest)} />
          <MetricItem label="最低" value={formatRatio(summary.min)} />
          <MetricItem label="最高" value={formatRatio(summary.max)} />
          <MetricItem label="平均" value={formatRatio(summary.avg)} />
        </dl>
      </CardContent>
    </Card>
  );
}

function MetricItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-border/60 pb-1.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="tabular text-xs font-medium text-foreground">{value}</dd>
    </div>
  );
}

function NotesBlock({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <section className="rounded-xl bg-muted/40 px-3.5 py-3 ring-1 ring-foreground/10">
      <h2 className="text-xs font-medium text-foreground">口径与数据缺口</h2>
      <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[11px] leading-4 text-muted-foreground">
        {notes.map((note, index) => (
          <li key={index}>{note}</li>
        ))}
      </ul>
    </section>
  );
}
