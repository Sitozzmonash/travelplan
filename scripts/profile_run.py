"""性能 Profiling：把一次真实 run 的耗时拆到阶段与 Provider 上。

为什么要这个脚本（性能优化任务 §2）：优化前必须先知道时间花在哪。
凭感觉优化最典型的后果是"把 3 秒的环节优化了，600 秒的环节原样留着"。

数据来源是**已有的真实产物**，不改任何业务代码：
  * `trace_spans`：workflow 节点 span 给阶段耗时，tool/provider span 给单次调用耗时；
  * `run_metrics`：总量（token / 调用次数）；
  * `outputs/<run_id>/audit_report.json`：provider_calls 的 provider/tool/status。

用法：
    python scripts/profile_run.py --latest          # 取最近一次完成的 run
    python scripts/profile_run.py tp-xxxx           # 指定 run_id
    python scripts/profile_run.py --latest --json   # 机器可读，便于做 Before/After 对比
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import TravelPlanStore  # noqa: E402


def _duration_ms(span: dict[str, Any]) -> int:
    value = (span.get("attributes") or {}).get("duration_ms")
    if isinstance(value, (int, float)):
        return int(value)
    started, finished = span.get("started_at"), span.get("finished_at")
    if not (started and finished):
        return 0
    from datetime import datetime

    try:
        return int((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() * 1000)
    except ValueError:
        return 0


def profile(store: TravelPlanStore, run_id: str) -> dict[str, Any]:
    spans = store.get_trace_spans(run_id)
    metrics = store.get_run_metrics(run_id) or {}
    run = store.get_run(run_id) or {}

    stages: list[dict[str, Any]] = []
    for span in spans:
        if span.get("component") != "workflow":
            continue
        stages.append(
            {
                "stage_id": span["name"],
                "status": span["status"],
                "duration_ms": _duration_ms(span),
            }
        )
    stages.sort(key=lambda item: -item["duration_ms"])

    # Provider / tool：按 (provider, tool) 聚合，看"哪个源最慢、调了几次"。
    #
    # 只数 `tool` span：provider / mcp 是**成组** span，它的窗口是组内多次调用的
    # min(start)~max(finish)，并发下会覆盖整段墙钟。把它和 tool 一起累加，
    # 会出现"单次调用合计 293s 而整次 run 只有 78s"这种自相矛盾的数字。
    per_call: dict[str, list[int]] = defaultdict(list)
    status_counts: Counter[str] = Counter()
    for span in spans:
        if span.get("component") != "tool":
            continue
        attributes = span.get("attributes") or {}
        key = f"{attributes.get('provider') or '?'}/{attributes.get('tool') or span['name']}"
        per_call[key].append(_duration_ms(span))
        status_counts[str(attributes.get("status") or span["status"])] += 1

    providers = [
        {
            "key": key,
            "calls": len(values),
            "total_ms": sum(values),
            "avg_ms": round(statistics.mean(values)),
            "max_ms": max(values),
        }
        for key, values in per_call.items()
    ]
    providers.sort(key=lambda item: -item["total_ms"])

    llm_spans = [span for span in spans if span.get("component") == "llm"]
    jev_spans = [span for span in spans if span.get("component") == "jev"]
    route_spans = [span for span in spans if (span.get("attributes") or {}).get("tool") == "route"]
    poi_spans = [span for span in spans if (span.get("attributes") or {}).get("tool") == "search_poi"]
    reused = [span for span in spans if (span.get("attributes") or {}).get("reused_from_discovery")]
    handoff = next((span for span in spans if span["name"] == "discovery_handoff"), None)

    return {
        "run_id": run_id,
        "status": run.get("status"),
        "source": run.get("source"),
        "total_ms": metrics.get("duration_ms"),
        "stages": stages,
        "providers": providers,
        "tool_status": dict(status_counts),
        "counts": {
            "llm_calls": metrics.get("llm_calls"),
            "jev_calls": metrics.get("jev_calls"),
            "tool_calls": metrics.get("tool_calls"),
            "adopted_tool_calls": metrics.get("adopted_tool_calls"),
            "route_calls": len(route_spans),
            "poi_calls": len(poi_spans),
            "reused_calls": len(reused),
            "llm_spans": len(llm_spans),
            "jev_spans": len(jev_spans),
            "provider_failures": metrics.get("provider_failures"),
        },
        "tokens": {
            "input": metrics.get("input_tokens"),
            "output": metrics.get("output_tokens"),
            "total": metrics.get("total_tokens"),
        },
        "grace_waited_ms": (handoff or {}).get("attributes", {}).get("grace_waited_ms"),
        "handoff": {
            key: value
            for key, value in ((handoff or {}).get("attributes") or {}).items()
            if key != "grace_waited_ms"
        },
    }


def _latest_run_id(store: TravelPlanStore) -> str | None:
    for run in store.list_runs(limit=50, include_benchmark=False):
        if run.get("status") in {"SUCCESS", "DEGRADED"}:
            return str(run["run_id"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="把一次 run 的耗时拆到阶段与 Provider")
    parser.add_argument("run_id", nargs="?", help="run_id；不传则用 --latest")
    parser.add_argument("--latest", action="store_true", help="取最近一次成功的 run")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()

    store = TravelPlanStore()
    run_id = args.run_id or (_latest_run_id(store) if args.latest else None)
    if not run_id:
        print("没有找到可分析的 run；请显式传 run_id")
        return 1

    report = profile(store, run_id)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    total = report["total_ms"] or 0
    print(f"run {run_id}  状态={report['status']}  来源={report['source']}  总耗时={total/1000:.1f}s")
    print("\n[阶段耗时]（按耗时降序，占比）")
    for stage in report["stages"]:
        share = f"{stage['duration_ms'] / total * 100:5.1f}%" if total else "  n/a"
        print(f"  {stage['duration_ms']/1000:8.1f}s  {share}  {stage['stage_id']:<34s} {stage['status']}")
    print("\n[Provider / 工具调用]（按总耗时降序）")
    for item in report["providers"][:14]:
        print(
            f"  {item['total_ms']/1000:8.1f}s  次数 {item['calls']:3d}  平均 {item['avg_ms']/1000:6.1f}s  "
            f"最大 {item['max_ms']/1000:6.1f}s  {item['key']}"
        )
    counts = report["counts"]
    print(
        f"\n[调用统计] llm={counts['llm_calls']} jev={counts['jev_calls']} tool={counts['tool_calls']} "
        f"（其中复用过户 {counts['adopted_tool_calls']}）route={counts['route_calls']} poi={counts['poi_calls']} "
        f"失败={counts['provider_failures']}"
    )
    print(f"[Token] {report['tokens']}")
    if report["grace_waited_ms"] is not None:
        print(f"[Discovery 交接] 等待 {report['grace_waited_ms']}ms  {report['handoff']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
