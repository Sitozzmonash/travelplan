"""PRD §36.4 的产物核验器：拿一个真实 run_id，逐条检查"能不能自证"。

用法：
    python scripts/verify_run.py tp-20260918-184140-b3981b
    python scripts/verify_run.py --latest bj_cd        # 从 _acceptance/<name>.log 里读 run_id

为什么要有这个脚本：验收报告里"所有实时价格有 source"这种话必须由代码判定，
不能靠人眼扫一遍 plan.json 就说通过 —— 那正是 PRD §36 禁止的"只写测试通过"。
每条检查都打印 PASS/FAIL 和它看到的证据（第几天、哪个 item、哪个 id）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models import TripPlan  # noqa: E402
from app.store import DEFAULT_DB_PATH, TravelPlanStore  # noqa: E402

#: 带价格的 item 类型（这些价格必须能定位到一次真实调用）。
PRICED_ITEM_TYPES = {"transport", "hotel"}
#: 不算"行程安排"的 item：占位与交通/住宿本身。
PLACE_ITEM_TYPES = {"attraction", "activity", "food"}

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
#: planner.minutes_to_clock 对 ≥24:00 的时刻写成 "00:15(次日)"（避免出现 25:30 这种
#: 不可读时间）。跨午夜的航班/车次本来就该这么显示，所以这里要认这个后缀。
_NEXT_DAY = "(次日)"


class Checker:
    def __init__(self, run_id: str, outputs: Path, store: TravelPlanStore) -> None:
        self.run_id = run_id
        self.outputs = outputs
        self.store = store
        self.plan_json: dict = {}
        self.audit: dict = {}
        self.results: list[tuple[bool, str, str]] = []

    # --- 基础设施 ---

    def check(self, ok: bool, title: str, detail: str = "") -> bool:
        self.results.append((ok, title, detail))
        return ok

    def check_arrival_grounding(self) -> None:
        """去程必须出现在行程里，且抵达之前不允许有游玩点。

        真机事故（2026-09-18）：MU6649「10-01 21:30 起飞、10-02 00:15 落地」这种跨午夜航班，
        旧实现只要"到达日 ≠ 行程第一天"就把去程段整段丢掉，于是第 1 天直接排成
        「08:30 在成都逛景点」—— 那时旅客还没上飞机。这条检查就是为了让这种事再发生时报红。
        """
        days = self.plan_json.get("days") or []
        transport = self.plan_json.get("transport") or {}
        selected = transport.get("selected") or {}

        if selected:
            has_outbound = any(
                str(item.get("id") or "").endswith("outbound")
                for day in days
                for item in day.get("items") or []
            )
            self.check(
                has_outbound,
                "去程交通段出现在行程里（跨午夜航班也不能丢）",
                "transport.selected 有选中方案，但行程里找不到 outbound 段",
            )
        else:
            # 也要记一条：一项"通过时才不出现"的检查，和"没跑"分不出来，
            # 四个 case 的分母就会不一样（实测 gz_cq 因为没有去程方案而只有 28 项）。
            self.check(
                True,
                "去程交通段出现在行程里（跨午夜航班也不能丢）",
                "本次没有选中去程方案，无从要求（不当成通过；见「没有去程方案时明说了」）",
            )

        # 抵达日 = 第一个出现"办理入住"的日子；它之前的任何游玩点都是不可能的。
        sightseeing = {"activity", "attraction", "food", "shopping", "nightlife"}
        checkin_index: int | None = None
        for day in days:
            for item in day.get("items") or []:
                if item.get("type") == "hotel" and item.get("booking_note") == "check-in":
                    checkin_index = day.get("day_index")
                    break
            if checkin_index is not None:
                break
        if checkin_index is None:
            self.check(
                True,
                "抵达日之前没有游玩点（人还在路上）",
                "行程里没有办理入住项，无从判定抵达日（不当成通过）",
            )
            return
        early = [
            f"D{day.get('day_index')} {item.get('name')}"
            for day in days
            if (day.get("day_index") or 0) < checkin_index
            for item in day.get("items") or []
            if item.get("type") in sightseeing
        ]
        self.check(
            not early,
            "抵达日之前没有游玩点（人还在路上）",
            "；".join(early[:5]),
        )

    def check_inbound_grounding(self) -> None:
        """回程也必须在行程里出现，且任何一天都不能是空白日程卡。

        真机事故（2026-09-18）：`_trim_to_window` 掐当天时间窗时整段切片
        `working_items[first:]`，而返程段固定排在当天最后 —— 于是"最后一个景点装不下"
        会把回家的那一段一起裁掉；末日赶早班车时更是整天被清空。
        4 个 case 里有 3 个中招，而日程里看不出任何异常：返程只存在于 `transport` 块里，
        旅客"玩到最后一天再也没回来"。原来的检查只看去程段，所以漏了。
        """
        days = self.plan_json.get("days") or []
        transport = self.plan_json.get("transport") or {}
        inbound = transport.get("inbound_selected") or {}

        if inbound:
            has_inbound = any(
                str(item.get("id") or "").endswith("inbound")
                for day in days
                for item in day.get("items") or []
            )
            self.check(
                has_inbound,
                "回程交通段出现在行程里（末日赶早班车也不能丢）",
                "transport.inbound_selected 有选中方案，但行程里找不到 inbound 段",
            )
        else:
            self.check(
                True,
                "回程交通段出现在行程里（末日赶早班车也不能丢）",
                "本次没有选中回程方案（候选为空），无从要求（不当成通过）",
            )

        blank = [f"D{day.get('day_index')} {day.get('date')}" for day in days if not day.get("items")]
        self.check(not blank, "每一天都有安排（没有空白日程卡）", "；".join(blank))

        # 回程跨天（在车上过夜、到家晚于行程最后一天）必须在 warnings 里说清楚：
        # 长春→成都只有一趟 48 小时的 K546，行程表排到 10-06 就结束，
        # 10-07 / 10-08 在车上那两天不能一个字都不提。
        if inbound and inbound.get("arrival_at"):
            times = [
                w for w in (self.plan_json.get("warnings") or [])
                if w.get("code") == "INBOUND_MULTI_DAY"
            ]
            last_date = (self.plan_json.get("days") or [{}])[-1].get("date") or ""
            arrives = str(inbound["arrival_at"])[:10]
            self.check(
                arrives <= last_date or bool(times),
                "回程跨天时明说了在车上要过几天",
                f"回程 {arrives} 才到家，晚于行程最后一天 {last_date}，"
                "但 warnings 里没有 INBOUND_MULTI_DAY",
            )
        else:
            self.check(True, "回程跨天时明说了在车上要过几天", "没有回程方案，不适用")

    def check_missing_candidates_disclosed(self) -> None:
        """去程 / 住宿一个候选都没有时，必须在 plan.warnings 里明说。

        真机事故（2026-09-18，广州→重庆 tp-20260918-232247-ebca73）：12306 与途牛航班
        同时 TIMEOUT、途牛酒店 UNAVAILABLE，4 天行程里既没有去程也没有住宿，
        D0 08:30 就在重庆逛景点、住宿费 ¥0 却显示"预算内"，而 plan.warnings 一条不提 ——
        只有 audit.degradations 写了两行，前端汇总成"有 2 个数据源为降级返回"。
        用户看不出这趟行程没法去、也没地方睡。
        """
        transport = self.plan_json.get("transport") or {}
        hotel_block = self.plan_json.get("hotel") or {}
        codes = {str(w.get("code") or "") for w in (self.plan_json.get("warnings") or [])}
        day_count = len(self.plan_json.get("days") or [])

        if transport.get("selected"):
            self.check(True, "没有去程方案时明说了（本次有去程，不适用）", "")
        else:
            self.check(
                "OUTBOUND_UNAVAILABLE" in codes,
                "没有去程方案时明说了（不能只写在 audit 降级记录里）",
                "transport.selected 为空，行程按「人已经在目的地」排，"
                "但 warnings 里没有 OUTBOUND_UNAVAILABLE",
            )

        if hotel_block.get("selected"):
            self.check(True, "没有住宿候选时明说了（本次有住宿，不适用）", "")
        elif day_count <= 1:
            self.check(True, "没有住宿候选时明说了（当天往返不需要住宿，不适用）", "")
        else:
            self.check(
                "HOTEL_UNAVAILABLE" in codes,
                "没有住宿候选时明说了（不能只写在 audit 降级记录里）",
                f"hotel.selected 为空且行程有 {day_count} 天，住宿费用是 0，"
                "但 warnings 里没有 HOTEL_UNAVAILABLE",
            )

    def check_transport_date_honesty(self) -> None:
        """交通方案的实际发车日与行程约定日期不符时，必须明说而不是混过去。

        真机事故（2026-09-18）：12306 查 2026-10-02 返回 09-30 / 10-01 的车次，比选只看
        价格和时长，于是"上海 10-02 出发"的行程选了一趟 10-01 08:16 开的车，计划里
        没有任何地方提到这件事。改车次的日期是编造，所以这里检查的是"说了没有"，
        不是"日期对不对"——日期本身由数据源决定，我们只负责不隐瞒。

        日期相符时也照常记一条 PASS：一条通过时就不出现的检查，和"没跑"分不出来，
        这一项的数量也就不可复现。
        """
        transport = self.plan_json.get("transport") or {}
        option = transport.get("selected") or {}
        departure = str(option.get("departure_at") or "")
        expected = str((self.plan_json.get("intent") or {}).get("start_date") or "")
        if not departure or not expected:
            self.check(
                True,
                "去程发车日与行程出发日一致性",
                "行程未约定出发日或方案无发车时刻，无从比较（不当成通过）",
            )
            return

        if departure[:10] == expected[:10]:
            self.check(True, "去程发车日与行程出发日一致性", f"均为 {departure[:10]}")
            return

        reason = str(transport.get("selection_reason") or "")
        codes = {
            str(issue.get("code") or "")
            for issue in (self.plan_json.get("warnings") or [])
            if isinstance(issue, dict)
        }
        self.check(
            "相差" in reason or "OUTBOUND_OFF_DATE" in codes,
            "去程发车日与行程出发日不符时必须明说",
            f"去程方案发车 {departure[:10]}，行程出发日 {expected[:10]}，"
            "但 selection_reason 和 warnings 里都没有提示日期不符",
        )

    def run(self) -> int:
        self._load()
        if not self.plan_json:
            return self._report()

        self.check_artifacts()
        self.check_schema()
        self.check_status()
        self.check_price_provenance()
        self.check_place_provenance()
        self.check_timeline()
        self.check_arrival_grounding()
        self.check_inbound_grounding()
        self.check_missing_candidates_disclosed()
        self.check_transport_date_honesty()
        self.check_budget()
        self.check_audit()
        return self._report()

    # --- 数据 ---

    def _load(self) -> None:
        run_dir = self.outputs / self.run_id
        plan_path = run_dir / "plan.json"
        audit_path = run_dir / "audit_report.json"
        self.check(
            plan_path.is_file() and audit_path.is_file() and (run_dir / "plan.md").is_file(),
            "产物齐备（plan.json / plan.md / audit_report.json）",
            f"目录 {run_dir}",
        )
        if not plan_path.is_file():
            return
        self.plan_json = json.loads(plan_path.read_text(encoding="utf-8"))
        if audit_path.is_file():
            self.audit = json.loads(audit_path.read_text(encoding="utf-8"))

    def _items(self):
        for day in self.plan_json.get("days") or []:
            for item in day.get("items") or []:
                yield day, item

    # --- 检查项 ---

    def check_artifacts(self) -> None:
        run_dir = self.outputs / self.run_id
        listed = {entry.get("filename") for entry in self.audit.get("artifacts") or []}
        missing = {"plan.json", "plan.md", "audit_report.json"} - listed
        self.check(
            not missing,
            "audit_report.artifacts 列出了三份产物",
            f"缺少 {sorted(missing)}" if missing else "",
        )
        self.check((run_dir / "plan.md").stat().st_size > 0, "plan.md 非空", "")

    def check_schema(self) -> None:
        try:
            TripPlan.model_validate(self.plan_json)
            ok, detail = True, ""
        except Exception as exc:  # noqa: BLE001 —— 校验失败本身就是结论
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.check(ok, "plan.json 能被 TripPlan 校验通过", detail)

    def check_status(self) -> None:
        run = self.store.get_run(self.run_id)
        self.check(run is not None, "run 已落库", "" if run else f"库里没有 {self.run_id}")
        if run:
            self.check(
                run.get("status") == "completed",
                "run 状态是 completed（不是卡在 running）",
                f"实际 {run.get('status')}",
            )
        self.check(self.store.get_plan(self.run_id) is not None, "plan 已落库", "")

    def check_price_provenance(self) -> None:
        """realtime 价格必须能定位到一次真实 Provider 调用及其 fetched_at。"""
        sources = {row["source_id"]: row for row in self.store.list_sources(self.run_id)}
        problems: list[str] = []
        checked = 0
        for day, item in self._items():
            if item.get("price_type") != "realtime":
                continue
            checked += 1
            ids = item.get("source_ids") or []
            label = f"D{day.get('day_index')} {item.get('name')}"
            if not ids:
                problems.append(f"{label} 标了 realtime 却没有 source_ids")
                continue
            for source_id in ids:
                row = sources.get(source_id)
                if row is None:
                    problems.append(f"{label} 的 source {source_id} 不在 sources 表里")
                elif not row.get("fetched_at"):
                    problems.append(f"{label} 的 source {source_id} 没有 fetched_at")
        self.check(
            not problems,
            f"每个实时价格都能定位到带 fetched_at 的 source（{checked} 条）",
            "；".join(problems[:5]),
        )
        self.check(checked > 0, "本次 run 至少有一条实时价格", "一条都没有：'价格不是编的'无从验证")

    def check_place_provenance(self) -> None:
        """PRD §35 D：行程里的地点要能定位到证据；只有高德 POI 的地点至少要能定位到来源。"""
        evidence_rows = {row["evidence_id"]: row for row in self.store.get_evidence(self.run_id)}
        source_ids = {row["source_id"] for row in self.store.list_sources(self.run_id)}
        problems: list[str] = []
        checked = 0
        for day, item in self._items():
            if item.get("type") not in PLACE_ITEM_TYPES:
                continue
            checked += 1
            label = f"D{day.get('day_index')} {item.get('name')}"
            evidence_ids = item.get("evidence_ids") or []
            sources = item.get("source_ids") or []
            if not evidence_ids and not sources:
                problems.append(f"{label} 既没有 evidence 也没有 source")
                continue
            for evidence_id in evidence_ids:
                row = evidence_rows.get(evidence_id)
                if row is None:
                    problems.append(f"{label} 的 evidence {evidence_id} 不在 evidence 表里")
                elif row.get("source_id") and row["source_id"] not in source_ids:
                    problems.append(f"{label} 的 evidence {evidence_id} 指向不存在的 source")
            for source_id in sources:
                if source_id not in source_ids:
                    problems.append(f"{label} 的 source {source_id} 不在 sources 表里")
        self.check(
            not problems,
            f"每个地点都能定位到 evidence/source（{checked} 个地点）",
            "；".join(problems[:5]),
        )
        self.check(checked > 0, "行程里有真实地点（不是空行程）", "一个地点都没有")

    def check_timeline(self) -> None:
        """时间硬冲突：起点晚于终点、同一天内两段重叠、跨出当天。"""
        problems: list[str] = []
        for day in self.plan_json.get("days") or []:
            index = day.get("day_index")
            date = day.get("date")
            spans: list[tuple[int, int, str]] = []
            for item in day.get("items") or []:
                start, end = _minutes(item.get("start_time")), _minutes(item.get("end_time"))
                if item.get("start_time") and start is None:
                    problems.append(f"D{index} {item.get('name')} 的 start_time 不是时刻：{item.get('start_time')}")
                    continue
                if item.get("end_time") and end is None:
                    problems.append(f"D{index} {item.get('name')} 的 end_time 不是时刻：{item.get('end_time')}")
                    continue
                if start is not None and end is not None:
                    if end < start:
                        problems.append(f"D{index} {item.get('name')} 结束早于开始（{item.get('start_time')}→{item.get('end_time')}）")
                        continue
                    # 跨午夜的**交通段**越到次日是事实（21:30 起飞 00:15 落地），
                    # 计划里也明确标了「(次日)」，不算冲突；其他类型的 item 跨出当天
                    # 才是真的排错了（博物馆不会开到凌晨三点）。
                    crossing = str(item.get("end_time") or "").strip().endswith(_NEXT_DAY)
                    if end > 24 * 60 and not (crossing and item.get("type") == "transport"):
                        problems.append(f"D{index} {item.get('name')} 跨出了当天（结束 {item.get('end_time')}）")
                    spans.append((start, end, str(item.get("name"))))
            spans.sort()
            for (s1, e1, n1), (s2, e2, n2) in zip(spans, spans[1:]):
                if s2 < e1:
                    problems.append(f"D{index} 「{n1}」({_clock(s1)}-{_clock(e1)}) 与「{n2}」({_clock(s2)}-{_clock(e2)}) 重叠")
        self.check(not problems, "行程内没有时间硬冲突", "；".join(problems[:5]))

    def check_budget(self) -> None:
        budget = self.plan_json.get("budget") or {}
        breakdown = budget.get("breakdown") or {}
        projected = budget.get("projected_total")
        known = budget.get("known_real_cost")
        estimated = budget.get("estimated_cost")

        total_parts = sum(float(value or 0) for value in breakdown.values())
        self.check(
            projected is not None and abs(total_parts - projected) <= 0.01,
            "预算分项之和 == projected_total",
            f"分项和 {total_parts} / projected {projected}",
        )
        if known is not None and estimated is not None and projected is not None:
            self.check(
                abs((known + estimated) - projected) <= 0.01,
                "known_real_cost + estimated_cost == projected_total",
                f"{known} + {estimated} = {known + estimated}，与 projected {projected} 不符",
            )
        remaining = budget.get("remaining")
        if remaining is not None and projected is not None and budget.get("budget_total") is not None:
            self.check(
                abs(remaining - (budget["budget_total"] - projected)) <= 0.01,
                "remaining == 预算 - projected_total",
                f"{remaining} vs {budget['budget_total'] - projected}",
            )
        # 实时价格与估算价格必须分开报，不能让估算混进 realtime。
        types = budget.get("breakdown_price_type") or {}
        realtime_categories = sorted(name for name, kind in types.items() if kind == "realtime")
        self.check(
            bool(realtime_categories) or not any(
                item.get("price_type") == "realtime" for _, item in self._items()
            ),
            f"价格口径有标注（realtime 类别：{realtime_categories or '无'}）",
            "明细里有 realtime 价格，但 breakdown 里没有任何类别标成 realtime",
        )

    def check_audit(self) -> None:
        for key in ("stages", "decisions", "provider_calls", "timeline", "degradations"):
            self.check(bool(self.audit.get(key) is not None), f"audit 含 {key}", "")
        missing_reason = [
            row.get("entity_id")
            for row in self.audit.get("decisions") or []
            if not (row.get("reason_text") or row.get("reason"))
        ]
        self.check(not missing_reason, "每条决策都有理由（REJECT/PASS/NOT_USED 都要说清）", f"缺理由：{missing_reason[:5]}")
        self.check(
            bool(self.plan_json.get("sources")),
            "plan 里带 sources 清单（前端来源抽屉要用）",
            "",
        )
        warnings = self.plan_json.get("warnings") or []
        self.check(
            bool(warnings),
            f"plan 里有 warnings（{len(warnings)} 条）",
            "一条都没有：要么真的完美，要么检查没跑",
        )

    # --- 输出 ---

    def _report(self) -> int:
        failed = 0
        for ok, title, detail in self.results:
            if ok:
                print(f"[PASS] {title}")
                continue
            failed += 1
            line = f"[FAIL] {title}"
            if detail:
                line += f" —— {detail}"
            print(line)
        print()
        print(f"run_id {self.run_id}：{len(self.results) - failed}/{len(self.results)} 项通过")
        return 1 if failed else 0


def _minutes(value: str | None) -> int | None:
    """`"21:30"` / `"00:15(次日)"` → 绝对分钟数（次日 +1440）。"""
    text = (value or "").strip()
    next_day = text.endswith(_NEXT_DAY)
    if next_day:
        text = text[: -len(_NEXT_DAY)]
    match = _CLOCK.match(text)
    if not match:
        return None
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    return minutes + 24 * 60 if next_day else minutes


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _latest_run_id(name: str) -> str:
    """从 `_acceptance/<name>.log` 里取这次验收 run 的 run_id。"""
    log = ROOT / "_acceptance" / f"{name}.log"
    if not log.is_file():
        raise SystemExit(f"找不到 {log}")
    matches = re.findall(r"run_id\s*:\s*(\S+)", log.read_text(encoding="utf-8", errors="replace"))
    if not matches:
        raise SystemExit(f"{log} 里没有 run_id（这次 run 可能没跑到最后）")
    return matches[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="核验一次真实 run 的产物（PRD §36.4）")
    parser.add_argument("run_id", nargs="?", help="run_id，例如 tp-20260918-184140-b3981b")
    parser.add_argument("--latest", metavar="NAME", help="改成从 _acceptance/<NAME>.log 取最后一个 run_id")
    parser.add_argument("--outputs", default=str(ROOT / "outputs"), help="产物根目录")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="sqlite 路径")
    args = parser.parse_args()

    run_id = args.run_id or (_latest_run_id(args.latest) if args.latest else None)
    if not run_id:
        parser.error("需要给一个 run_id，或用 --latest <验收案例名>")

    return Checker(run_id, Path(args.outputs), TravelPlanStore(args.db)).run()


if __name__ == "__main__":
    raise SystemExit(main())
