"use client";

import { Info, RefreshCw, Settings2, ShieldCheck, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/admin/page-header";
import { PartialBanner, ResourceView } from "@/components/admin/admin-states";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import { SecretBadge } from "@/components/admin/status-badge";
import { StatCard } from "@/components/admin/stat-card";
import {
  formatLatency,
  formatMetricValue,
  formatNumber,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { getAdminConfig, getAdminJevHealth } from "@/lib/admin-api";
import type { AdminConfig, AdminJevHealth, AdminRecord } from "@/types/admin";

/** 配置分组：前缀决定归属，未匹配的落到「其他」，新字段不会因为没登记而消失。 */
const CONFIG_GROUPS = [
  { key: "budget", label: "预算", description: "预算上限与超限处理策略" },
  { key: "jev", label: "Jev 决策", description: "模型决策层的开关、配额与阈值" },
  { key: "evolution", label: "Evolution", description: "自进化流程的开关与触发条件" },
] as const;

export function ConfigView() {
  const config = useAdminResource<AdminConfig>("admin-config", getAdminConfig);
  const health = useAdminResource<AdminJevHealth>("admin-jev-health", getAdminJevHealth);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="系统配置"
        description="后端当前生效的配置快照。管理台只读展示：改配置要改部署环境变量并重启，避免线上被随手改坏。"
        badge={<Settings2 className="size-4 text-muted-foreground" aria-hidden />}
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              config.reload();
              health.reload();
            }}
          >
            <RefreshCw />
            刷新
          </Button>
        }
      />

      <ResourceView resource={config} loadingRows={5}>
        {(data) => <ConfigBody data={data} />}
      </ResourceView>

      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <Sparkles className="size-4 text-muted-foreground" aria-hidden />
            Jev 健康
          </CardTitle>
          <CardDescription>
            来自 GET /api/v1/admin/jev/health 的实时探测，与上面的静态配置分开读取。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ResourceView resource={health} loadingRows={3}>
            {(data) => <JevHealthBody data={data} />}
          </ResourceView>
        </CardContent>
      </Card>
    </div>
  );
}

function ConfigBody({ data }: { data: AdminConfig }) {
  const grouped = groupConfig(data.config);
  const secretValues = data.secret_configured ?? { jev: false, admin: false };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <ShieldCheck className="size-4 text-muted-foreground" aria-hidden />
            密钥配置状态
          </CardTitle>
          <CardDescription>
            这里只回答「配没配」。管理台永远不回显任何密钥取值，也不提供读取接口。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <span className="flex items-center gap-2 text-xs text-foreground">
              Jev 密钥
              <SecretBadge configured={Boolean(secretValues.jev)} />
            </span>
            <span className="flex items-center gap-2 text-xs text-foreground">
              管理 Token
              <SecretBadge configured={Boolean(secretValues.admin)} />
            </span>
          </div>
          <p className="flex gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
            <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            <span>
              「管理 Token 未配置」说明后端缺少 TRAVELPLAN_ADMIN_TOKEN：那种情况下整个管理接口都会返回 503，
              连这一页也打不开。
            </span>
          </p>
          {data.note ? (
            <p className="text-[11px] leading-5 text-muted-foreground">后端说明：{data.note}</p>
          ) : null}
        </CardContent>
      </Card>

      {grouped.map((group) => (
        <Card key={group.key}>
          <CardHeader>
            <CardTitle>{group.label}</CardTitle>
            <CardDescription>{group.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <MetricList
              metrics={group.entries}
              emptyText={`后端配置里没有以「${group.key}」开头的字段。`}
            />
          </CardContent>
        </Card>
      ))}

      <Card>
        <CardHeader>
          <CardTitle>Planner 调参</CardTitle>
          <CardDescription>规划器的可调参数，通常只在实验时改动。</CardDescription>
        </CardHeader>
        <CardContent>
          <MetricList
            metrics={data.planner_tuning}
            emptyText="后端没有返回 planner_tuning 字段内容。"
          />
        </CardContent>
      </Card>
    </div>
  );
}

function JevHealthBody({ data }: { data: AdminJevHealth }) {
  const quotaKnown = data.quota_source === "reported" && data.quota !== null && data.quota !== undefined;

  return (
    <div className="flex flex-col gap-3">
      {!data.configured ? (
        <PartialBanner
          title="Jev 未配置密钥"
          description="配置里缺少 Jev 所需的密钥，因此所有决策都会走 fallback。这不会让规划失败，但会降低取舍质量。"
        />
      ) : null}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatCard
          label="状态"
          value={statusLabel(data.status)}
          tone={
            data.status === "OK" || data.status === "HEALTHY"
              ? "success"
              : data.status === "ERROR"
                ? "danger"
                : "muted"
          }
          hint={data.enabled ? "Jev 已启用" : "Jev 未启用"}
        />
        <StatCard label="探测时延" value={formatLatency(data.latency_ms)} />
        <StatCard
          label="配额"
          value={quotaKnown ? formatMetricValue(data.quota) : "unknown"}
          hint={
            data.quota_source === "reported"
              ? "由后端上报"
              : "后端未上报配额，因此不可知"
          }
        />
        <StatCard
          label="fallback 次数"
          value={formatNumber(data.fallback_count)}
          tone={data.fallback_count && data.fallback_count > 0 ? "warning" : "muted"}
        />
      </div>

      <DescriptionList
        items={[
          { label: "累计调用", value: formatNumber(data.calls) },
          { label: "低置信度", value: formatNumber(data.low_confidence) },
          { label: "超时", value: formatNumber(data.timeout) },
          { label: "非法响应", value: formatNumber(data.invalid_response) },
          {
            label: "最后错误",
            value: data.last_error ? (
              <span className="font-mono text-[11px] break-words text-danger-subtle-foreground">
                {data.last_error}
              </span>
            ) : (
              "无"
            ),
          },
          {
            label: "密钥",
            value: <SecretBadge configured={Boolean(data.configured)} />,
          },
          { label: "配额来源", value: data.quota_source === "reported" ? "后端上报" : "unknown" },
        ]}
      />
    </div>
  );
}

interface ConfigGroup {
  key: string;
  label: string;
  description: string;
  entries: AdminRecord;
}

function groupConfig(config: AdminRecord): ConfigGroup[] {
  const buckets = new Map<string, AdminRecord>();
  for (const group of CONFIG_GROUPS) buckets.set(group.key, {});
  const rest: AdminRecord = {};

  for (const [key, value] of Object.entries(config)) {
    const group = CONFIG_GROUPS.find((candidate) => key.startsWith(candidate.key));
    if (group) {
      const bucket = buckets.get(group.key);
      if (bucket) bucket[key] = value;
    } else {
      rest[key] = value;
    }
  }

  const groups: ConfigGroup[] = CONFIG_GROUPS.map((group) => ({
    key: group.key,
    label: group.label,
    description: group.description,
    entries: buckets.get(group.key) ?? {},
  }));
  groups.push({
    key: "other",
    label: "其他配置",
    description: "不属于预算 / Jev / Evolution 前缀的字段。",
    entries: rest,
  });
  return groups;
}
