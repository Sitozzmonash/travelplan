"""Before / After 对比：把两份 E2E 结果与 run 基线拉平成一张表。

为什么单独写一个：性能任务的验收要求"同一行程、同一流程"直接对比，
手工抄数字既容易抄错，也没法在优化后重跑时重新核对。这里统一从
`_acceptance/*.json` 读事实，只做汇总，不重新测量。

用法：
    python scripts/perf_compare.py                       # 打印已采集到的对比
    python scripts/perf_compare.py --run-id tp-xxx       # 追加某个 run 的阶段明细
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "_acceptance"

#: 三次关键耗时里，用户真正等待的是 plan_s；discovery_s 是后台并行的时间。
KEYS = ("discovery_s", "plan_s", "total_s", "discovery_states", "run_status")


def _load(name: str) -> dict[str, Any] | None:
    path = ACC / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _pair(label: str, before_name: str, after_name: str) -> list[tuple[str, str, str, str]]:
    before, after = _load(before_name), _load(after_name)
    rows: list[tuple[str, str, str, str]] = []
    for key in KEYS:
        b = before.get(key) if before else None
        a = after.get(key) if after else None
        delta = ""
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b:
            pct = (a - b) / b * 100
            delta = f"{pct:+.1f}%"
        rows.append((key, _fmt(b), _fmt(a), delta))
    rows.insert(0, ("行程", f"{before_name}", f"{after_name}", ""))
    if before:
        rows.insert(
            1,
            (
                "行程参数",
                f"{before.get('origin')}→{before.get('destination')} {before.get('days')}天",
                f"{after.get('origin')}→{after.get('destination')} {after.get('days')}天" if after else "—",
                "",
            ),
        )
    return rows


def _print_table(title: str, rows: list[tuple[str, str, str, str]]) -> None:
    print(f"\n### {title}")
    print(f"| {'指标':<14} | {'优化前':>10} | {'优化后':>10} | {'变化':>8} |")
    print(f"| {'-' * 14} | {'-' * 10} | {'-' * 10} | {'-' * 8} |")
    for key, before, after, delta in rows:
        print(f"| {key:<14} | {before:>10} | {after:>10} | {delta:>8} |")


def _stage_rows(before_json: str, after_json: str) -> list[tuple[str, str, str, str]]:
    """按阶段名对齐两份 run 的阶段耗时。"""

    def stages(name: str) -> dict[str, float]:
        payload = _load(name) or {}
        out: dict[str, float] = {}
        for stage in payload.get("stages") or []:
            out[str(stage.get("stage_id"))] = round((stage.get("duration_ms") or 0) / 1000, 1)
        return out

    before, after = stages(before_json), stages(after_json)
    rows: list[tuple[str, str, str, str]] = []
    for key in sorted(set(before) | set(after), key=lambda k: -max(before.get(k, 0), after.get(k, 0))):
        b, a = before.get(key), after.get(key)
        delta = f"{(a - b) / b * 100:+.1f}%" if b and a is not None else ""
        rows.append((key, _fmt(b), _fmt(a), delta))
    return rows


def _count_rows(before_json: str, after_json: str) -> list[tuple[str, str, str, str]]:
    """调用次数是对"是否还在重复查询"最直接的证据。"""

    before, after = _load(before_json) or {}, _load(after_json) or {}
    keys = ("llm_calls", "jev_calls", "tool_calls", "provider_calls", "route_calls", "poi_calls", "poi_detail_calls")
    rows: list[tuple[str, str, str, str]] = []
    for key in keys:
        b, a = (before.get("counts") or {}).get(key), (after.get("counts") or {}).get(key)
        delta = f"{(a - b) / b * 100:+.1f}%" if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b else ""
        rows.append((key, _fmt(b), _fmt(a), delta))
    for key in ("input", "output", "total"):
        b, a = (before.get("tokens") or {}).get(key), (after.get("tokens") or {}).get(key)
        rows.append((f"token_{key}", _fmt(b), _fmt(a), ""))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Before / After 汇总")
    parser.add_argument("--run-id", action="append", default=[], help="额外打印某个 run 的 E2E 结果")
    args = parser.parse_args()

    print("=" * 78)
    print("TravelPlan 性能 Before / After")
    print("=" * 78)

    _print_table("3 天行程（北京→西安）", _pair("3d", "e2e_3d_before.json", "e2e_3d_after.json"))
    _print_table("5 天行程（北京→成都）", _pair("5d", "e2e_5d_before.json", "e2e_5d_after.json"))

    print("\n（阶段明细与调用次数需要 --run-id 指向具体 run）")
    for run_id in args.run_id:
        print(f"\n### run {run_id}")
        print(json.dumps(_load(f"profile_{run_id}.json") or {}, ensure_ascii=False, indent=2)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
