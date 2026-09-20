"""PRD §36.4 的 E2E 结果摘要：把一个 run 的产物压成验收报告要填的那几个字段。

用法：
    python scripts/case_summary.py --latest bj_cd
    python scripts/case_summary.py --all            # 四个验收案例依次输出
    python scripts/case_summary.py tp-2026...       # 直接给 run_id

为什么要有这个脚本：§36.4 要求 `transport / hotel / day_count / real_cost /
estimated_cost / warnings` 这几项必须是**这次 run 的真实数字**。手抄 plan.json
既容易抄错，也没法在报告里说清数字是怎么来的 —— 这里把取数口径固定下来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 四个验收案例（PRD §34.4）与它们的日志名，顺序与 `_acceptance_runs.sh` 一致。
CASES = ("bj_cd", "sh_hz", "cc_cd", "gz_cq")


def latest_run_id(name: str) -> str:
    log = ROOT / "_acceptance" / f"{name}.log"
    if not log.is_file():
        raise SystemExit(f"找不到 {log}")
    matches = re.findall(r"run_id\s*:\s*(\S+)", log.read_text(encoding="utf-8", errors="replace"))
    if not matches:
        raise SystemExit(f"{log} 里没有 run_id（这次 run 可能没跑到最后）")
    return matches[-1]


def summarize(run_id: str) -> dict:
    plan = json.loads((ROOT / "outputs" / run_id / "plan.json").read_text(encoding="utf-8"))
    budget = plan.get("budget") or {}
    breakdown = budget.get("breakdown") or {}

    days = plan.get("days") or []
    placed = [item for day in days for item in (day.get("items") or []) if item.get("place_id")]
    free_time = [
        item
        for day in days
        for item in (day.get("items") or [])
        if item.get("type") == "free_time"
    ]

    realtime = [
        item
        for day in days
        for item in (day.get("items") or [])
        if item.get("price_type") == "realtime"
    ]
    unknown_price = [
        item
        for day in days
        for item in (day.get("items") or [])
        if item.get("place_id") and item.get("price_type") == "unknown"
    ]

    transport = plan.get("transport") or {}
    selected = transport.get("selected") or {}
    inbound_selected = transport.get("inbound_selected") or {}
    hotel_block = plan.get("hotel") or {}
    hotel_selected = hotel_block.get("selected") or {}

    warnings = plan.get("warnings") or []

    return {
        "run_id": run_id,
        "query": plan.get("query", ""),
        "status": plan.get("status") or "completed",
        "day_count": len(days),
        "placed_items": len(placed),
        "free_time_items": len(free_time),
        "transport": _label(selected, ("name", "flight_no", "train_no", "title", "provider")),
        "transport_price": _price(selected),
        "inbound": _label(inbound_selected, ("name", "flight_no", "train_no", "title", "provider")),
        "inbound_price": _price(inbound_selected),
        "inbound_arrival": inbound_selected.get("arrival_at"),
        "hotel": _label(hotel_selected, ("name", "title", "provider")),
        "hotel_price_per_night": hotel_selected.get("price_per_night"),
        "real_cost": budget.get("known_real_cost"),
        "estimated_cost": budget.get("estimated_cost"),
        "projected_total": budget.get("projected_total"),
        "budget_total": budget.get("budget_total"),
        "remaining": budget.get("remaining"),
        "budget_status": budget.get("status"),
        "breakdown": breakdown,
        "price_notes": budget.get("price_notes") or [],
        "realtime_items": len(realtime),
        "unknown_price_places": len(unknown_price),
        "warnings": len(warnings),
        "warning_codes": _codes(warnings),
        "sources": len(plan.get("sources") or []),
        "decisions": len(plan.get("decisions") or []),
    }


def _label(block: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = block.get(key)
        if value:
            return str(value)
    return "（未选中）"


def _price(block: dict) -> str:
    """火车票没有单一 `price`（按席别给 `price_range`），直接取 price 会打印出 ¥None。"""
    price = block.get("price")
    if price is not None:
        return f"¥{price}"
    span = block.get("price_range") or {}
    low, high = span.get("min"), span.get("max")
    if low is None and high is None:
        return "无报价"
    if low == high:
        return f"¥{low}"
    return f"¥{low}~{high}（分席别）"


def _codes(warnings: list) -> list[str]:
    """把告警压成 `CODE×n`，重复的只留计数 —— 27 条里 20 条是同一种，列全没意义。"""
    counts: dict[str, int] = {}
    for warning in warnings:
        code = ""
        if isinstance(warning, dict):
            code = str(warning.get("code") or warning.get("type") or "")
        else:
            code = str(warning).split(":")[0]
        code = code.strip() or "UNKNOWN"
        counts[code] = counts.get(code, 0) + 1
    return [f"{code}×{count}" if count > 1 else code for code, count in sorted(counts.items())]


def render(summary: dict) -> str:
    hotel = summary["hotel"]
    hotel_line = (
        f"{hotel}  ¥{summary['hotel_price_per_night']}/晚"
        if hotel != "（未选中）"
        else "（未选中）—— 住宿是空缺的，见 warnings 的 HOTEL_UNAVAILABLE"
    )
    outbound_line = (
        f"{summary['transport']}  {summary['transport_price']}"
        if summary["transport"] != "（未选中）"
        else "（未选中）—— 行程按「人已经在目的地」排，见 warnings 的 OUTBOUND_UNAVAILABLE"
    )
    if summary["inbound"] != "（未选中）":
        inbound_line = (
            f"{summary['inbound']}  {summary['inbound_price']}  抵达 {summary['inbound_arrival']}"
        )
    else:
        inbound_line = "（未选中）—— 行程末尾没有回家那一段，见 warnings 的 INBOUND_UNAVAILABLE"

    lines = [
        f"### {summary['run_id']}",
        "",
        "```text",
        f"输入:            {summary['query']}",
        f"run_id:          {summary['run_id']}",
        f"transport:       {outbound_line}",
        f"inbound:         {inbound_line}",
        f"hotel:           {hotel_line}",
        f"day_count:       {summary['day_count']}",
        f"itinerary:       {summary['placed_items']} 个真实地点 / {summary['free_time_items']} 段留白",
        f"real_cost:       {summary['real_cost']}",
        f"estimated_cost:  {summary['estimated_cost']}",
        f"projected_total: {summary['projected_total']}（预算 {summary['budget_total']}，结余 {summary['remaining']}，status={summary['budget_status']}）",
        f"budget:          {summary['breakdown']}",
        f"realtime 价格:   {summary['realtime_items']} 条；价格未知的地点 {summary['unknown_price_places']} 个",
        f"warnings:        {summary['warnings']} 条 {summary['warning_codes']}",
        f"sources:         {summary['sources']} 条，decisions {summary['decisions']} 条",
        "```",
    ]
    if summary["price_notes"]:
        lines += ["", "价格口径备注：", ""]
        lines += [f"- {note}" for note in summary["price_notes"]]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="输出 PRD §36.4 需要的 E2E 结果字段")
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--latest", metavar="NAME", help=f"从 _acceptance/<NAME>.log 取 run_id，可选 {CASES}")
    parser.add_argument("--all", action="store_true", help="四个验收案例全跑一遍")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    if args.all:
        run_ids = [latest_run_id(name) for name in CASES]
    elif args.latest:
        run_ids = [latest_run_id(args.latest)]
    elif args.run_id:
        run_ids = [args.run_id]
    else:
        parser.error("需要 run_id / --latest NAME / --all 三者之一")

    for index, run_id in enumerate(run_ids):
        summary = summarize(run_id)
        if index:
            print()
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
