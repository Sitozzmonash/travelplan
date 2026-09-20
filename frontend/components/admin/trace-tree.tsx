"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { AdminDetailDialog, JsonBlock, KeyValueList } from "@/components/admin/detail-dialog";
import {
  formatDurationMs,
  formatTraceAttributeKey,
  formatTraceAttributeValue,
} from "@/components/admin/format";
import { StatusBadge } from "@/components/admin/status-badge";
import type { AdminTraceSpan } from "@/types/admin";

export interface AdminTraceNode extends AdminTraceSpan {
  children: AdminTraceNode[];
}

/**
 * 把扁平 span 列表按 parent_span_id 组装成树。
 *
 * 两个必须处理的脏数据场景：
 * 1) parent_span_id 指向一个不存在的 span（采集丢包）→ 当作根节点，否则这段调用会凭空消失。
 * 2) 父子关系成环（采集中断导致的半截数据）→ 用带步数上限的向上遍历判定，
 *    成环的节点提升为根，避免渲染时无限递归。
 */
export function buildTraceTree(spans: AdminTraceSpan[]): AdminTraceNode[] {
  const nodes = new Map<string, AdminTraceNode>();
  for (const span of spans) {
    nodes.set(span.span_id, { ...span, children: [] });
  }

  const looping = new Set<string>();
  for (const [spanId, node] of nodes) {
    const seen = new Set<string>([spanId]);
    let cursor = node.parent_span_id;
    let steps = 0;
    while (cursor && nodes.has(cursor) && steps <= nodes.size) {
      if (seen.has(cursor)) {
        looping.add(spanId);
        break;
      }
      seen.add(cursor);
      cursor = nodes.get(cursor)?.parent_span_id ?? null;
      steps += 1;
    }
  }

  const roots: AdminTraceNode[] = [];
  for (const [spanId, node] of nodes) {
    const parent = node.parent_span_id && !looping.has(spanId) ? nodes.get(node.parent_span_id) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

/**
 * Trace 树。
 *
 * 页面默认只给摘要：每一行是「组件 + 名称 + 状态 + 耗时」，
 * 点击任意一行打开完整属性弹窗（含友好标签与原始 JSON）。
 * 这样既不会在主页面里摊开几十行内部属性，也保证每个 span 都点得开。
 */
export function TraceTree({ spans, className }: { spans: AdminTraceSpan[]; className?: string }) {
  const [selected, setSelected] = useState<AdminTraceSpan | null>(null);
  const roots = buildTraceTree(spans);

  if (roots.length === 0) {
    return <p className="text-xs text-muted-foreground">本次运行没有采集到 Span。</p>;
  }

  return (
    <div className={cn("flex min-w-0 flex-col", className)}>
      <ul className="flex min-w-0 flex-col">
        {roots.map((root) => (
          <TraceRow key={root.span_id} node={root} depth={0} onSelect={setSelected} />
        ))}
      </ul>

      <SpanDetailDialog span={selected} onOpenChange={(open) => (open ? null : setSelected(null))} />
    </div>
  );
}

function TraceRow({
  node,
  depth,
  onSelect,
}: {
  node: AdminTraceNode;
  depth: number;
  onSelect: (span: AdminTraceSpan) => void;
}) {
  const indent = Math.min(depth, 8) * 12;

  return (
    <li className="min-w-0 list-none">
      <button
        type="button"
        onClick={() => onSelect(node)}
        className="flex w-full min-w-0 items-center gap-2 rounded-md py-1.5 pr-2 pl-1 text-left hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        style={{ paddingLeft: indent + 4 }}
      >
        <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
          {node.component}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground" title={node.name}>
          {node.name}
        </span>
        <StatusBadge status={node.status} />
        <span className="tabular shrink-0 text-[11px] text-muted-foreground">
          {formatDurationMs(node.duration_ms)}
        </span>
        {node.error ? (
          <span className="size-1.5 shrink-0 rounded-full bg-danger" aria-label="有错误" />
        ) : null}
      </button>

      {node.children.length > 0 ? (
        <ul className="flex min-w-0 flex-col">
          {node.children.map((child) => (
            <TraceRow key={child.span_id} node={child} depth={depth + 1} onSelect={onSelect} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function SpanDetailDialog({
  span,
  onOpenChange,
}: {
  span: AdminTraceSpan | null;
  onOpenChange: (open: boolean) => void;
}) {
  const attributes = span?.attributes ?? {};
  const entries = Object.entries(attributes);

  return (
    <AdminDetailDialog
      open={span !== null}
      onOpenChange={onOpenChange}
      title={span ? span.name : "Span 详情"}
      description={span ? `${span.component} · ${span.span_id}` : undefined}
      badge={span ? <StatusBadge status={span.status} /> : undefined}
    >
      {span ? (
        <>
          <KeyValueList
            entries={[
              { key: "component", label: "组件", value: span.component },
              { key: "span_id", label: "Span ID", value: <span className="font-mono">{span.span_id}</span> },
              {
                key: "parent",
                label: "父 Span",
                value: <span className="font-mono">{span.parent_span_id ?? "无（根节点）"}</span>,
              },
              { key: "started", label: "开始", value: <span className="font-mono">{span.started_at ?? "未返回"}</span> },
              { key: "finished", label: "结束", value: <span className="font-mono">{span.finished_at ?? "未返回"}</span> },
              { key: "duration", label: "耗时", value: formatDurationMs(span.duration_ms) },
            ]}
          />

          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-foreground">属性</h3>
            {entries.length === 0 ? (
              <p className="text-xs text-muted-foreground">这个 span 没有属性。</p>
            ) : (
              <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
                {entries.map(([key, value]) => (
                  <div
                    key={key}
                    className="flex items-baseline justify-between gap-3 border-b border-border/60 pb-1.5"
                  >
                    <dt className="shrink-0 text-[11px] text-muted-foreground" title={key}>
                      {formatTraceAttributeKey(key)}
                    </dt>
                    <dd className="min-w-0 text-right text-xs break-words text-foreground">
                      {formatTraceAttributeValue(key, value)}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-foreground">完整属性（JSON）</h3>
            <JsonBlock value={attributes} />
          </section>

          {span.error ? (
            <section className="flex flex-col gap-2">
              <h3 className="text-xs font-medium text-danger-subtle-foreground">错误</h3>
              <pre className="max-h-60 overflow-auto rounded-md border border-danger/25 bg-danger-subtle/60 p-2 font-mono text-[10px] leading-4 break-all whitespace-pre-wrap text-danger-subtle-foreground">
                {span.error}
              </pre>
            </section>
          ) : null}
        </>
      ) : null}
    </AdminDetailDialog>
  );
}
