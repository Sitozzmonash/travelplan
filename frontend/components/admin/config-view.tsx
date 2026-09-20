"use client";

import { useMemo, useState } from "react";
import { Info, RefreshCw, RotateCcw, Save, Settings2, ShieldCheck, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageHeader } from "@/components/admin/page-header";
import { ActionError, PartialBanner, ResourceView } from "@/components/admin/admin-states";
import { DescriptionList, MetricList } from "@/components/admin/metric-list";
import { StatCard } from "@/components/admin/stat-card";
import { SecretBadge, ToneBadge } from "@/components/admin/status-badge";
import {
  formatLatency,
  formatMetricValue,
  formatNumber,
  statusLabel,
} from "@/components/admin/format";
import { useAdminResource } from "@/components/admin/use-admin-resource";
import { formatDateTime } from "@/lib/format";
import { getAdminConfig, getAdminJevHealth, patchAdminConfig, toAdminApiError, type AdminApiError } from "@/lib/admin-api";
import { ADMIN_CONFIG_GROUPS, type AdminConfig, type AdminConfigOverride, type AdminJevHealth, type AdminRecord } from "@/types/admin";

/** Secret 只读展示：管理台永远只回答「配没配」，不回显取值。 */
const SECRET_LABELS: Record<string, string> = {
  jev: "Jev 密钥",
  admin: "管理 Token",
  amap: "高德密钥",
  tikhub: "TikHub Token",
  tuniu: "途牛密钥",
};

const GROUP_ORDER = [...ADMIN_CONFIG_GROUPS.map((group) => group.key), "other"] as const;
type ConfigGroupKey = (typeof GROUP_ORDER)[number];

export function ConfigView() {
  const config = useAdminResource<AdminConfig>("admin-config", getAdminConfig);
  const health = useAdminResource<AdminJevHealth>("admin-jev-health", getAdminJevHealth);

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="系统配置"
        description="非 Secret 的配置项可以在线修改（保存后写入运行时覆盖）；Secret 只报「配没配」，永不回显。"
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
        {(data) => <ConfigBody data={data} onSaved={config.reload} />}
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

function ConfigBody({ data, onSaved }: { data: AdminConfig; onSaved: () => void }) {
  const editableKeys = useMemo(() => data.editable_keys ?? [], [data.editable_keys]);
  const overrides = data.runtime_overrides ?? {};
  const grouped = useMemo(() => groupEditableKeys(editableKeys), [editableKeys]);

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
            {Object.entries(data.secret_configured).map(([key, configured]) => (
              <span key={key} className="flex items-center gap-2 text-xs text-foreground">
                {SECRET_LABELS[key] ?? key}
                <SecretBadge configured={Boolean(configured)} />
              </span>
            ))}
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

      {editableKeys.length === 0 ? (
        <PartialBanner
          title="后端没有声明可编辑的配置项"
          description="GET /admin/config 没有返回 editable_keys，因此这一页保持只读：下面是当前生效的配置快照。"
          detail="等后端按契约回填 editable_keys / values / runtime_overrides 后，这里会自动出现可编辑表单。"
        />
      ) : (
        GROUP_ORDER.map((groupKey) => {
          const keys = grouped[groupKey];
          if (!keys || keys.length === 0) return null;
          const meta = ADMIN_CONFIG_GROUPS.find((group) => group.key === groupKey);
          return (
            <Card key={groupKey}>
              <CardHeader>
                <CardTitle>{meta?.label ?? "其他配置"}</CardTitle>
                <CardDescription>
                  {meta?.description ?? "不属于预算 / Jev / Planner 阈值 / 模型单价的字段。"}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                {keys.map((key) => (
                  <ConfigKeyEditor
                    key={key}
                    configKey={key}
                    data={data}
                    override={overrides[key]}
                    onSaved={onSaved}
                  />
                ))}
              </CardContent>
            </Card>
          );
        })
      )}

      {editableKeys.length === 0 ? (
        <>
          <Card>
            <CardHeader>
              <CardTitle>当前生效配置</CardTitle>
              <CardDescription>只读快照，包含后端返回的 config 字段。</CardDescription>
            </CardHeader>
            <CardContent>
              <MetricList metrics={data.config} emptyText="后端没有返回 config 字段。" />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Planner 调参</CardTitle>
              <CardDescription>规划器的可调参数，通常只在实验时改动。</CardDescription>
            </CardHeader>
            <CardContent>
              <MetricList metrics={data.planner_tuning} emptyText="后端没有返回 planner_tuning 字段内容。" />
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function ConfigKeyEditor({
  configKey,
  data,
  override,
  onSaved,
}: {
  configKey: string;
  data: AdminConfig;
  override: AdminConfigOverride | undefined;
  onSaved: () => void;
}) {
  const effective = resolveValue(data.values, configKey);
  const fallback = resolveValue(data.config, configKey) ?? resolveValue(data.planner_tuning, configKey);
  const initial = serializeValue(effective ?? fallback);
  const [draft, setDraft] = useState(initial);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<AdminApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const kind = valueKind(effective ?? fallback);
  const dirty = draft !== initial;

  async function handleSave() {
    if (pending || !dirty) return;
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      await patchAdminConfig({ values: { [configKey]: parseValue(draft, kind) } });
      setNotice("已保存。");
      onSaved();
    } catch (cause) {
      setError(toAdminApiError(cause));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium break-all text-foreground" title={configKey}>
          {configKey}
        </span>
        {override ? <ToneBadge tone="info">已覆盖</ToneBadge> : null}
        {override?.updated_at ? (
          <span className="text-[11px] text-muted-foreground">
            更新时间：{formatDateTime(override.updated_at)}
            {override.updated_by ? ` · ${override.updated_by}` : ""}
          </span>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-[11px] text-muted-foreground">
            默认值：{fallback === undefined || fallback === null ? "后端未提供" : serializeValue(fallback)}
          </span>
          <span className="text-[11px] text-muted-foreground">
            当前生效：{effective === undefined || effective === null ? "未返回" : formatMetricValue(effective)}
          </span>
          {kind === "boolean" ? (
            <span className="mt-1 flex items-center gap-2 text-xs text-foreground">
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={draft === "true"}
                onChange={(event) => setDraft(event.target.checked ? "true" : "false")}
              />
              启用
            </span>
          ) : (
            <Input
              value={draft}
              inputMode={kind === "number" ? "decimal" : undefined}
              spellCheck={false}
              aria-label={configKey}
              onChange={(event) => setDraft(event.target.value)}
            />
          )}
        </label>

        <div className="flex items-end gap-2">
          <Button variant="outline" size="sm" onClick={() => setDraft(initial)} disabled={!dirty || pending}>
            <RotateCcw />
            重置
          </Button>
          <Button size="sm" onClick={() => void handleSave()} disabled={!dirty || pending}>
            <Save />
            {pending ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>

      {error ? <ActionError message="保存失败" detail={error.detail} /> : null}
      {!error && notice ? <p className="text-[11px] text-muted-foreground">{notice}</p> : null}
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

/* ------------------------------ 工具函数 ------------------------------ */

type ConfigValueKind = "boolean" | "number" | "text";

function valueKind(value: unknown): ConfigValueKind {
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  return "text";
}

function serializeValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function parseValue(draft: string, kind: ConfigValueKind): unknown {
  if (kind === "boolean") return draft === "true";
  if (kind === "number") {
    const parsed = Number(draft);
    return Number.isFinite(parsed) ? parsed : draft;
  }
  return draft;
}

/**
 * 取值解析：后端 key 可能是大写环境变量名（`JEV_MAX_CALLS_PER_RUN`），
 * 而 config / planner_tuning 用的是 snake_case，因此精确匹配失败时再试小写。
 */
function resolveValue(record: AdminRecord | undefined, key: string): unknown {
  if (!record) return undefined;
  if (key in record) return record[key];
  const lower = key.toLowerCase();
  if (lower in record) return record[lower];
  return undefined;
}

function groupOf(key: string): ConfigGroupKey {
  const k = key.toLowerCase();
  if (k.includes("price") || k.includes("per_million") || k.includes("token_cost") || k.includes("unit_price")) {
    return "price";
  }
  if (k.includes("budget")) return "budget";
  if (k.includes("jev")) return "jev";
  if (
    k.startsWith("tp_") ||
    k.includes("threshold") ||
    k.includes("min_") ||
    k.includes("max_") ||
    k.includes("ad_risk") ||
    k.includes("cluster") ||
    k.includes("day_end") ||
    k.includes("planner")
  ) {
    return "planner";
  }
  return "other";
}

function groupEditableKeys(keys: string[]): Partial<Record<ConfigGroupKey, string[]>> {
  const buckets: Partial<Record<ConfigGroupKey, string[]>> = {};
  for (const key of keys) {
    const group = groupOf(key);
    const bucket = buckets[group];
    if (bucket) bucket.push(key);
    else buckets[group] = [key];
  }
  return buckets;
}
