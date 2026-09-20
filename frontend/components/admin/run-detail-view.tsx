"use client";

import Link from "next/link";
import { useState } from "react";
import { ArrowLeft, Bug, Coins, Cpu, GitBranch, RefreshCw, Share2, Sparkles, Wrench } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AdminTable, type AdminColumn } from "@/components/admin/admin-table";
import { CollapsibleSection } from "@/components/admin/collapsible-section";
import { PageHeader } from "@/components/admin/page-header";
import { ResourceView, SectionEmpty } from "@/components/admin/admin-states";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import { StatCard } from "@/components/admin/stat-card";
import { StatusBadge, ToneBadge } from "@/components/admin/status-badge";
import { TraceTree } from "@/components/admin/trace-tree";
import {
  formatCost,
  formatDurationMs,
  formatLatency,
  formatMetricValue,
  formatNumber,
  formatRatio,
  formatTokens,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminRun } from "@/lib/admin-api";
import { formatProviderQuery } from "@/lib/format";
import type {
  AdminBadcase,
  AdminDecision,
  AdminJevCall,
  AdminProviderCall,
  AdminRunDetail,
  AdminTraceSpan,
} from "@/types/admin";

/**
 * 运行详情。
 *
 * 默认展开的只有「Trace」和「阶段」：它们是回答"这次运行到底卡在哪"的最短路径。
 * LLM / Jev / Provider / Token / 决策链 / Bad Case 都是可选的深入材料，默认折叠，
 * 避免一屏塞进几十行表格让人找不到重点。
 */
export function RunDetailView({ runId }: { runId: string }) {
  const resource = useAdminResource<AdminRunDetail>(`admin-run:${runId}`, () => getAdminRun(runId));

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="运行详情"
        description="Trace 按 span 的父子关系展开；阶段来自后端 workflow 的落库进度。"
        badge={
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] break-all text-muted-foreground">
            {runId}
          </span>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <Button variant="outline" size="sm" onClick={resource.reload}>
              <RefreshCw />
              刷新
            </Button>
            <Button variant="ghost" size="sm" nativeButton={false} render={<Link href="/admin/runs" />}>
              <ArrowLeft />
              运行记录
            </Button>
          </div>
        }
      />

      <ResourceView resource={resource} loadingRows={6}>
        {(data) => <RunDetailBody data={data} />}
      </ResourceView>
    </div>
  );
}

function RunDetailBody({ data }: { data: AdminRunDetail }) {
  const { run, metrics, progress } = data;
  const llmSpans = data.trace.filter(isLlmSpan);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <span>概览</span>
            <StatusBadge status={run.status} />
          </CardTitle>
          <CardDescription className="break-words whitespace-pre-wrap">
            {run.original_query || "后端没有记录原始请求文本。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            <StatCard label="总耗时" value={formatDurationMs(metrics.duration_ms)} />
            <StatCard label="总 Token" value={formatTokens(metrics.total_tokens)} icon={Coins} />
            <StatCard label="LLM 调用" value={formatNumber(metrics.llm_calls)} icon={Cpu} />
            <StatCard label="Jev 调用" value={formatNumber(metrics.jev_calls)} icon={Sparkles} />
            <StatCard label="工具调用" value={formatNumber(metrics.tool_calls)} icon={Wrench} />
            <StatCard
              label="Provider 失败"
              value={formatNumber(metrics.provider_failures)}
              tone={metrics.provider_failures && metrics.provider_failures > 0 ? "warning" : "muted"}
            />
            <StatCard
              label="Bad Case"
              value={formatNumber(metrics.badcase_count)}
              tone={metrics.badcase_count && metrics.badcase_count > 0 ? "warning" : "muted"}
              icon={Bug}
            />
            <StatCard
              label="成本"
              value={formatCost(metrics.cost)}
              hint={metrics.cost === null ? "后端没有给出该次运行的定价" : "按后端定价表折算"}
            />
          </div>
          <DescriptionList
            items={[
              { label: "开始", value: run.created_at ?? "—" },
              { label: "结束", value: run.finished_at ?? "—" },
              { label: "进度状态", value: progress.status ? statusLabel(progress.status) : "—" },
              { label: "进度说明", value: progress.message || "后端没有给出说明" },
            ]}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>阶段</CardTitle>
          <CardDescription>
            workflow 的 {progress.stages.length} 个阶段，按后端记录顺序展示。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {progress.stages.length === 0 ? (
            <SectionEmpty
              title="没有阶段进度"
              description="这次运行没有写入阶段记录，因此看不到分步进度。Trace 里通常仍有更细的 span。"
            />
          ) : (
            <ol className="flex flex-col gap-2.5">
              {progress.stages.map((stage) => (
                <li
                  key={stage.stage_id}
                  className="rounded-lg border border-border p-2.5"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-foreground">{stage.title || stage.stage_id}</span>
                    <StatusBadge status={stage.status} />
                    <span className="tabular ml-auto text-[11px] text-muted-foreground">
                      {formatDurationMs(durationBetween(stage.started_at, stage.finished_at))}
                    </span>
                  </div>
                  {stage.message ? (
                    <p className="mt-1.5 text-xs leading-5 break-words text-muted-foreground">
                      {stage.message}
                    </p>
                  ) : null}
                  {stage.facts && Object.keys(stage.facts).length > 0 ? (
                    <MetricList metrics={stage.facts} className="mt-2" />
                  ) : null}
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Trace</CardTitle>
          <CardDescription>
            共 {data.trace.length} 个 span；根节点是 parent_span_id 为空的 span。点击行可展开属性与错误。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <TraceTree spans={data.trace} />
        </CardContent>
      </Card>

      <CollapsibleSection
        title="LLM 调用"
        description="从 Trace 中筛出的模型调用 span（判定依据见下方说明）"
        count={llmSpans.length}
        icon={Cpu}
      >
        {llmSpans.length === 0 ? (
          <SectionEmpty
            title="Trace 里没有识别到 LLM span"
            description="管理台的 LLM 区块由 Trace 派生：component / name 含 llm、model、agent、planner、critic 等关键字，或 span 属性里带 model、*_tokens 字段。"
            hint="如果后端为此单独返回了列表，请更新契约，管理台会改成直接展示。"
          />
        ) : (
          <div className="flex flex-col gap-3">
            <AdminTable columns={LLM_COLUMNS} rows={llmSpans} getRowKey={(span) => span.span_id} />
            <p className="text-[11px] leading-4 text-muted-foreground">
              说明：LLM 调用由 Trace 派生（当前契约的 run 详情没有独立字段）。
            </p>
          </div>
        )}
      </CollapsibleSection>

      <CollapsibleSection
        title="Jev 调用"
        description="决策类型、输入摘要、选择结果、置信度与 fallback 原因"
        count={data.jev_calls.length}
        icon={Sparkles}
      >
        <JevCallList calls={data.jev_calls} />
      </CollapsibleSection>

      <CollapsibleSection
        title="Provider 调用"
        description="外部数据源的逐次请求与返回情况"
        count={data.provider_calls.length}
        icon={Share2}
      >
        {data.provider_calls.length === 0 ? (
          <SectionEmpty
            title="没有 Provider 调用记录"
            description="这次运行可能全部命中缓存，或者后端没有落库 Provider 明细。"
          />
        ) : (
          <AdminTable
            columns={PROVIDER_COLUMNS}
            rows={data.provider_calls}
            getRowKey={(call) =>
              `${call.provider}-${call.tool}-${call.fetched_at ?? ""}-${formatProviderQuery(call.query)}`
            }
          />
        )}
      </CollapsibleSection>

      <CollapsibleSection title="Token 明细" description="输入 / 输出 / 缓存的拆分" count="4 项" icon={Coins}>
        <MetricList
          metrics={{
            input_tokens: metrics.input_tokens,
            output_tokens: metrics.output_tokens,
            cached_tokens: metrics.cached_tokens,
            total_tokens: metrics.total_tokens,
          }}
        />
        <p className="mt-2 text-[11px] text-muted-foreground">
          缓存 Token 由后端统计；前端只展示，不做二次推算。
        </p>
      </CollapsibleSection>

      <CollapsibleSection
        title="决策链"
        description="每个实体的取舍结论、原因码与打分"
        count={data.decisions.length}
        icon={GitBranch}
      >
        <DecisionList decisions={data.decisions} />
      </CollapsibleSection>

      <CollapsibleSection
        title="Bad Cases"
        description="本次运行被自动或人工登记的问题案例"
        count={data.badcases.length}
        icon={Bug}
        actions={
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
            全部案例
          </Button>
        }
      >
        <BadcaseList badcases={data.badcases} />
      </CollapsibleSection>
    </div>
  );
}

/**
 * LLM span 判定。
 * 为什么用启发式：契约给的是通用 trace，没有标记 span 类型；
 * 关键字 + token 属性同时判断，误判成本很低（只是一个可折叠列表）。
 */
const LLM_HINTS = ["llm", "model", "agent", "planner", "critic", "router", "judge", "reason"];
const LLM_ATTRIBUTE_HINTS = [
  "model",
  "input_tokens",
  "output_tokens",
  "prompt_tokens",
  "completion_tokens",
  "total_tokens",
];

function isLlmSpan(span: AdminTraceSpan): boolean {
  const component = span.component.toLowerCase();
  const name = span.name.toLowerCase();
  if (LLM_HINTS.some((hint) => component.includes(hint) || name.includes(hint))) return true;
  const attributes = span.attributes ?? {};
  return LLM_ATTRIBUTE_HINTS.some((key) => key in attributes);
}

function durationBetween(startedAt: string | null, finishedAt: string | null): number | null {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

const LLM_COLUMNS: AdminColumn<AdminTraceSpan>[] = [
  {
    key: "name",
    header: "调用",
    primary: true,
    cell: (span) => (
      <div className="min-w-0">
        <p className="truncate text-xs font-medium text-foreground" title={span.name}>
          {span.name}
        </p>
        <p className="font-mono text-[10px] text-muted-foreground">{span.component}</p>
      </div>
    ),
  },
  { key: "status", header: "状态", cell: (span) => <StatusBadge status={span.status} /> },
  {
    key: "model",
    header: "模型",
    mobileHidden: true,
    cell: (span) => (
      <span className="font-mono text-[11px] break-all">{formatMetricValue(span.attributes?.model)}</span>
    ),
  },
  {
    key: "tokens",
    header: "Token",
    align: "right",
    cell: (span) => (
      <span className="tabular text-xs">
        {formatTokens(
          typeof span.attributes?.total_tokens === "number" ? span.attributes.total_tokens : null,
        )}
      </span>
    ),
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (span) => <span className="tabular text-xs">{formatDurationMs(span.duration_ms)}</span>,
  },
  {
    key: "error",
    header: "错误",
    mobileHidden: true,
    cell: (span) =>
      span.error ? (
        <span className="text-[11px] break-words text-danger-subtle-foreground">{span.error}</span>
      ) : (
        <span className="text-[11px] text-muted-foreground">—</span>
      ),
  },
];

export function JevCallList({ calls }: { calls: AdminJevCall[] }) {
  if (calls.length === 0) {
    return (
      <SectionEmpty
        title="这次运行没有 Jev 调用"
        description="可能 Jev 未启用、未配置密钥，或者本次规划没有触发需要模型判断的取舍。"
      />
    );
  }
  return (
    <ul className="flex flex-col gap-2.5">
      {calls.map((call, index) => (
        <li key={`${call.tag}-${index}`} className="rounded-lg border border-border p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              {call.tag}
            </span>
            <span className="text-xs font-medium text-foreground">{call.decision_type}</span>
            <StatusBadge status={call.status} />
            {call.fallback ? <ToneBadge tone="warning">已 fallback</ToneBadge> : null}
            <span className="tabular ml-auto text-[11px] text-muted-foreground">
              {formatLatency(call.latency_ms)}
            </span>
          </div>
          <DescriptionList
            className="mt-2"
            items={[
              { label: "模型", value: call.model ?? "—" },
              {
                label: "输出选择",
                value: call.choice ?? "—",
              },
              {
                label: "置信度",
                value:
                  call.confidence === null || call.confidence === undefined
                    ? "—"
                    : formatRatio(call.confidence),
              },
              { label: "输入摘要", value: call.input_summary ?? "—" },
              { label: "判定标准", value: formatMetricValue(call.criteria) },
              { label: "配额", value: formatMetricValue(call.quota) },
              {
                label: "fallback 原因",
                value: call.fallback ? call.fallback_reason ?? "后端未给出原因" : "未触发",
              },
            ]}
          />
        </li>
      ))}
    </ul>
  );
}

const PROVIDER_COLUMNS: AdminColumn<AdminProviderCall>[] = [
  {
    key: "provider",
    header: "Provider",
    primary: true,
    cell: (call) => (
      <div className="min-w-0">
        <p className="text-xs font-medium text-foreground">{call.provider}</p>
        <p className="font-mono text-[10px] text-muted-foreground">{call.tool}</p>
      </div>
    ),
  },
  {
    key: "query",
    header: "查询",
    mobileHidden: true,
    cell: (call) => (
      <span className="line-clamp-2 block max-w-[22rem] text-xs text-muted-foreground">
        {formatProviderQuery(call.query)}
      </span>
    ),
  },
  { key: "status", header: "状态", cell: (call) => <StatusBadge status={call.status} /> },
  {
    key: "returned",
    header: "返回",
    align: "right",
    cell: (call) => (
      <span className="tabular text-xs break-words">{formatMetricValue(call.returned)}</span>
    ),
  },
  {
    key: "duration",
    header: "耗时",
    align: "right",
    cell: (call) => <span className="tabular text-xs">{formatDurationMs(call.duration_ms)}</span>,
  },
  {
    key: "fetched_at",
    header: "抓取时间",
    mobileHidden: true,
    cell: (call) => (
      <span className="font-mono text-[11px] whitespace-nowrap text-muted-foreground">
        {call.fetched_at ?? "—"}
      </span>
    ),
  },
  {
    key: "source",
    header: "来源",
    mobileHidden: true,
    cell: (call) => (
      <span className="font-mono text-[11px] break-all text-muted-foreground">{call.source_id ?? "—"}</span>
    ),
  },
  {
    key: "note",
    header: "备注",
    mobileHidden: true,
    cell: (call) => (
      <span className="text-[11px] break-words text-muted-foreground">{call.note ?? "—"}</span>
    ),
  },
];

function DecisionList({ decisions }: { decisions: AdminDecision[] }) {
  if (decisions.length === 0) {
    return (
      <SectionEmpty
        title="没有决策记录"
        description="这次运行没有把取舍结论落库，因此看不到决策链。"
      />
    );
  }
  return (
    <ul className="flex flex-col gap-2.5">
      {decisions.map((decision) => (
        <li key={decision.entity_id} className="rounded-lg border border-border p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[11px] break-all text-muted-foreground">
              {decision.entity_id}
            </span>
            <StatusBadge status={decision.status} />
            <span className="text-xs text-muted-foreground">{decision.agent_or_stage}</span>
          </div>
          {decision.reason_text ? (
            <p className="mt-1.5 text-xs leading-5 break-words text-foreground">{decision.reason_text}</p>
          ) : null}
          {decision.reason_codes.length > 0 ? (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {decision.reason_codes.map((code) => (
                <ToneBadge key={code} tone="muted">
                  {code}
                </ToneBadge>
              ))}
            </div>
          ) : null}
          {decision.scores && Object.keys(decision.scores).length > 0 ? (
            <MetricList metrics={decision.scores} className="mt-2" />
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function BadcaseList({ badcases }: { badcases: AdminBadcase[] }) {
  const [openId, setOpenId] = useState<string | null>(null);

  if (badcases.length === 0) {
    return (
      <SectionEmpty
        title="这次运行没有登记 Bad Case"
        description="规则判定、人工标注与模型评审都没有为本次运行登记问题。"
        action={
          <Button variant="outline" size="xs" nativeButton={false} render={<Link href="/admin/badcases" />}>
            打开问题案例页
          </Button>
        }
      />
    );
  }
  return (
    <ul className="flex flex-col gap-2.5">
      {badcases.map((badcase) => {
        const expanded = openId === badcase.badcase_id;
        return (
          <li key={badcase.badcase_id} className="rounded-lg border border-border p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-foreground">{badcase.category}</span>
              <StatusBadge status={badcase.severity} />
              <StatusBadge status={badcase.analysis_status} />
              <StatusBadge status={badcase.fixed_status} />
              <StatusBadge status={badcase.detected_by} />
              <Button
                variant="ghost"
                size="xs"
                className="ml-auto"
                onClick={() => setOpenId(expanded ? null : badcase.badcase_id)}
              >
                {expanded ? "收起" : "展开"}
              </Button>
            </div>
            <p className="mt-1.5 text-xs leading-5 break-words text-muted-foreground">{badcase.symptom}</p>
            {expanded ? (
              <DescriptionList
                className="mt-2"
                items={[
                  { label: "期望", value: badcase.expected || "—" },
                  { label: "实际", value: badcase.actual || "—" },
                  { label: "疑似根因", value: badcase.suspected_root_cause || "—" },
                  { label: "根因状态", value: statusLabel(badcase.root_cause_status) },
                  { label: "Trace 引用", value: badcase.trace_refs.join("、") || "—" },
                  { label: "引入版本", value: badcase.introduced_in ?? "—" },
                  { label: "修复版本", value: badcase.fixed_in ?? "—" },
                ]}
              />
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
