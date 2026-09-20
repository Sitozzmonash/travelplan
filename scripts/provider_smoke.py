"""PRD §36.3 的 Provider Smoke：每个 Provider 真打一次，输出可直接贴进验收报告的表格。

用法：
    python scripts/provider_smoke.py                # 全部
    python scripts/provider_smoke.py --only tuniu   # 只跑名字里含 tuniu 的行
    python scripts/provider_smoke.py --with-fallback   # 额外跑 §36.5 的降级验证

口径：状态与条数一律取 ProviderCall 里记录的值（和真实 run 完全同一条代码路径），
不是另写一套探活逻辑 —— 探活能过而主流程不过，是最没有意义的"通过"。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers import ProviderHub, default_mcp_servers  # noqa: E402

DEPART = date(2026, 10, 1)


@dataclass
class Row:
    provider: str
    tool: str
    call: Callable[[ProviderHub], Any]


def _train_12306(hub: ProviderHub) -> Any:
    """12306 是主源；返回结果里 provider 字段会说明最终是谁答的。"""
    return hub.search_trains("北京", "成都", DEPART, limit=5)


def _plugin(plugin_tool: str, args: dict[str, Any]) -> Callable[[ProviderHub], Any]:
    """走插件层直接调一个工具（邮轮/跟团游没有 Hub 包装，只能这样打）。"""

    def _call(hub: ProviderHub) -> Any:
        entry = hub.plugins.get(plugin_tool)
        if entry is None:
            raise RuntimeError(f"插件工具 {plugin_tool} 不存在")
        _, tool = entry
        return json.loads(tool.invoke(args))

    return _call


ROWS: list[Row] = [
    Row("12306", "train", _train_12306),
    Row("tuniu", "flight", lambda hub: hub.search_flights("北京", "成都", DEPART)),
    Row("tuniu", "hotel", lambda hub: hub.search_hotels("成都", DEPART, date(2026, 10, 3), travelers=2)),
    Row("tuniu", "ticket", lambda hub: hub.search_scenic_tickets("宽窄巷子")),
    Row(
        "tuniu",
        "cruise",
        _plugin("tuniu_search_cruises", {"departs_date_begin": "2026-10-17", "departs_date_end": "2026-10-20"}),
    ),
    Row(
        "tuniu",
        "holiday",
        _plugin(
            "tuniu_search_holiday_packages",
            {"destination": "三亚", "departs_date_begin": "2026-10-10", "departs_date_end": "2026-10-15"},
        ),
    ),
    Row("amap", "poi", lambda hub: hub.search_poi("宽窄巷子", "成都", page_size=5, type_hint="attraction")),
    Row("amap", "route", lambda hub: hub.route((104.0555, 30.6691), (104.0400, 30.6400), "transit", city="成都")),
    Row("amap", "geocode", lambda hub: hub.geocode("成都宽窄巷子", "成都")),
    Row("tikhub", "quota", lambda hub: hub.tikhub_quota()),
    Row("tikhub", "xhs", lambda hub: hub.search_xiaohongshu("成都 美食")),
    Row("mediacrawler", "xhs", _plugin("search_xhs_via_mediacrawler", {"keyword": "成都 美食"})),
    Row("tavily", "web", lambda hub: hub.web_search("成都 必去景点", max_results=3)),
]


def _describe(result: Any) -> tuple[str, int, str]:
    """从 ProviderResult / 插件信封里取出 (状态, 条数, 备注)。"""
    if isinstance(result, dict):  # 插件信封
        items = result.get("items") or []
        status = str(result.get("status") or "UNKNOWN")
        note = str(result.get("error") or result.get("note") or "")
        return status, len(items), note

    calls = list(getattr(result, "calls", []) or [])
    status = str(getattr(result, "status", "UNKNOWN"))
    items = list(getattr(result, "items", []) or [])
    notes: list[str] = []
    provider = getattr(result, "provider", "")
    if calls:
        first = calls[0]
        notes.extend(str(note) for note in (first.notes or []) if note)
        if first.error:
            notes.append(str(first.error))
        if len(calls) > 1:
            # 火车这类"主源失败回退备源"的调用，两条都要留在备注里。
            notes.append("回退链：" + " → ".join(f"{c.provider}/{c.tool}={c.status}" for c in calls))
    if provider and calls and calls[0].provider != provider:
        notes.append(f"provider={provider}")
    return status, len(items), "；".join(notes)[:180]


def run_smoke(only: str | None) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    healthy = True
    hub = ProviderHub(run_id="smoke", store=None, mcp_servers=default_mcp_servers())
    try:
        for row in ROWS:
            if only and only not in f"{row.provider}/{row.tool}":
                continue
            started = time.perf_counter()
            try:
                result = row.call(hub)
                status, count, note = _describe(result)
            except Exception as exc:  # noqa: BLE001 —— 探活失败本身就是要报告的结果
                status, count, note = "EXCEPTION", 0, f"{type(exc).__name__}: {exc}"
            elapsed = time.perf_counter() - started
            if status not in ("OK", "EMPTY"):
                healthy = False
            rows.append([row.provider, row.tool, status, str(count), f"{elapsed:.1f}s", note])
            print(f"{row.provider:<12} {row.tool:<10} {status:<24} {count:>3} 条 {elapsed:6.1f}s  {note}")
    finally:
        hub.close()
    return rows, healthy


def print_markdown(rows: list[list[str]]) -> None:
    print()
    print("| Provider | Tool | 状态 | 返回条数 | 延迟 | 备注 |")
    print("|---|---|---:|---:|---:|---|")
    for row in rows:
        print("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")


def check_fallbacks() -> None:
    """PRD §36.5：三个降级路径必须真的走通，而不是写在文档里。"""
    print()
    print("=== §36.5 Fallback ===")

    # 1) 不给 12306 spec → 火车必须落到途牛，且审计里两条调用都在。
    without_rail = ProviderHub(run_id="smoke-fb", store=None, mcp_servers=[])
    try:
        result = without_rail.search_trains("北京", "成都", DEPART, limit=5)
        providers = [f"{call.provider}/{call.tool}={call.status}" for call in result.calls]
        print(f"12306 不可用时：整体 status={result.status}，调用链 {providers or '无'}")
        fell_back = any(call.provider == "tuniu" for call in result.calls)
        print(f"  → {'PASS' if fell_back else 'FAIL'}：回退到了途牛")
    finally:
        without_rail.close()

    hub = ProviderHub(run_id="smoke-fb2", store=None, mcp_servers=default_mcp_servers())
    try:
        # 2) TikHub 免费额度：额度为 0 时小红书必须落到 MediaCrawler（未安装则如实报 UNAVAILABLE）。
        quota = hub.tikhub_quota()
        print(f"TikHub 额度：status={quota.status}，{json.dumps(list(quota.items)[:1], ensure_ascii=False)[:160]}")
        social = hub.search_xiaohongshu("成都 美食")
        chain = [f"{call.provider}/{call.tool}={call.status}" for call in social.calls]
        print(f"小红书：status={social.status}，调用链 {chain or '无'}")
        print("  → 无免费额度时必须走 MediaCrawler；MediaCrawler 未安装时如实记 UNAVAILABLE，不编造内容")

        # 3) 高德不可用：路线必须为空，由 planner 标 ROUTE_UNVERIFIED，而不是模型编一个时长。
        broken = ProviderHub(run_id="smoke-fb3", store=None)
        original = broken._plugin_call

        def _fail_amap(**kwargs: Any) -> Any:
            if kwargs.get("provider") == "amap":
                from app.models import utcnow
                from app.providers import ProviderCall

                return ProviderCall(
                    source_id=broken._next_source_id(),
                    provider="amap",
                    source_type=str(kwargs.get("source_type") or ""),
                    tool=str(kwargs.get("tool_name") or ""),
                    query=dict(kwargs.get("args") or {}),
                    status="UNAVAILABLE",
                    payload={},
                    items=[],
                    error="smoke：模拟高德不可用",
                    fetched_at=utcnow(),
                )
            return original(**kwargs)

        broken._plugin_call = _fail_amap  # type: ignore[method-assign]
        route = broken.route((104.0555, 30.6691), (104.0400, 30.6400), "transit", city="成都")
        print(f"高德不可用时路线：status={route.status}、items={len(route.items)}")
        print(f"  → {'PASS' if route.status != 'OK' and not route.items else 'FAIL'}：没有编造路线，交由 planner 记 ROUTE_UNVERIFIED")
        broken.close()
    finally:
        hub.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Provider Smoke（PRD §36.3）")
    parser.add_argument("--only", help="只跑名字里含该子串的行，例如 tuniu / amap")
    parser.add_argument("--with-fallback", action="store_true", help="额外跑 §36.5 的降级验证")
    parser.add_argument("--no-markdown", action="store_true", help="不打印 markdown 表格")
    args = parser.parse_args()

    rows, healthy = run_smoke(args.only)
    if not args.no_markdown:
        print_markdown(rows)
    if args.with_fallback:
        check_fallbacks()
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
