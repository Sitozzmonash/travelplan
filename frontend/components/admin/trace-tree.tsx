"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatDurationMs } from "@/components/admin/format";
import { MetricList } from "@/components/admin/metric-list";
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

export function TraceTree({ spans, className }: { spans: AdminTraceSpan[]; className?: string }) {
  const roots = buildTraceTree(spans);
  if (roots.length === 0) {
    return <p className="text-xs text-muted-foreground">本次运行没有采集到 Span。</p>;
  }
  return (
    <ul className={cn("flex min-w-0 flex-col", className)}>
      {roots.map((root) => (
        <TraceNode key={root.span_id} node={root} depth={0} defaultOpen={roots.length <= 3} />
      ))}
    </ul>
  );
}

function TraceNode({
  node,
  depth,
  defaultOpen = false,
}: {
  node: AdminTraceNode;
  depth: number;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const attributeCount = node.attributes ? Object.keys(node.attributes).length : 0;
  const expandable = attributeCount > 0 || Boolean(node.error);
  const indent = Math.min(depth, 8) * 12;

  return (
    <li className="min-w-0 list-none">
      <details
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
        className="group/trace min-w-0"
      >
        <summary
          className="flex min-w-0 cursor-pointer list-none items-center gap-2 rounded-md py-1.5 pr-2 pl-1 hover:bg-muted/50 [&::-webkit-details-marker]:hidden"
          style={{ marginLeft: indent }}
        >
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 transition-transform group-open/trace:rotate-90",
              expandable ? "text-muted-foreground" : "text-transparent",
            )}
            aria-hidden
          />
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
        </summary>

        <div className="flex flex-col gap-2 pb-2" style={{ marginLeft: indent + 24 }}>
          {node.started_at || node.finished_at ? (
            <p className="font-mono text-[10px] break-all text-muted-foreground">
              {node.started_at ?? "—"} → {node.finished_at ?? "—"}
            </p>
          ) : null}
          {attributeCount > 0 ? <MetricList metrics={node.attributes} /> : null}
          {node.error ? (
            <pre className="max-h-40 overflow-auto rounded-md border border-danger/25 bg-danger-subtle/60 p-2 font-mono text-[10px] leading-4 whitespace-pre-wrap text-danger-subtle-foreground">
              {node.error}
            </pre>
          ) : null}
        </div>

        {node.children.length > 0 ? (
          <ul className="flex min-w-0 flex-col">
            {node.children.map((child) => (
              <TraceNode key={child.span_id} node={child} depth={depth + 1} />
            ))}
          </ul>
        ) : null}
      </details>
    </li>
  );
}
