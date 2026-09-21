"""管理端聚合层：把已经落库的运行事实换算成"现在最该修什么"。

为什么不写在 `api.py` 里：`/admin/overview` 报的是历史累计量（调了多少次 LLM、
烧了多少 token），它回答不了"这批行程生成得好不好、用户在哪一步流失、哪一类问题
在变多"。本模块把**同一批事实**（runs / run_metrics / plans / badcases /
planning_sessions / provider_calls）按时间窗重切一遍，产出四个视图：

  * ``/admin/dashboard``      关键指标 + 环比 + 当前需要关注
  * ``/admin/travel-quality`` 生成出来的行程到底好不好（六个维度）
  * ``/admin/funnel``         引导式会话在哪一步流失
  * ``/admin/badcase-groups`` 把一堆 Bad Case 聚成几个真正要解决的问题

外加给 ``/admin/runs`` 列表补的每-run质量分。

四条硬约定
----------
1. **不编数据**：库里没有的字段一律给 ``None``，并在 ``notes`` 里写明为什么拿不到。
   绝不用 0 或估计值冒充 —— 0 会被读成"确实一次都没有"，那是另一个意思。
2. **窗口统一**：所有指标都按 ``1h/24h/7d/30d`` 四档切，并给出**等长上一窗口**的对照，
   前端只负责画 ↑↓，不自己算差值。
3. **只读**：本模块不写任何表、不加任何埋点。所有输入都来自既有 store 读接口。
4. **口径单点**：等级阈值、质量分权重、漏斗阶段定义都只在本文件里定义一次。

关于成本：一份 ``plan_json`` 有 100–220KB，跑质量指标要解一遍 pydantic。管理端
一次列表 50 条就是 5–10MB 的读取与解析，所以 ``_PlanCache`` 按 run_id 缓存解析结果
（run 结束后计划不再变化，缓存只在进程内存里，重启即失效）。
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, Depends, Query

from . import planner
from .config import current_config
from .models import TripPlan
from .observability import now_iso, parallel_map
from .store import TravelPlanStore

# ======================================================================
# 时间窗
# ======================================================================

#: 四档窗口是**唯一**允许的时间口径。新增档位要同时想清楚分桶宽度与环比窗口。
WINDOW_LENGTHS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
WINDOW_LABELS: dict[str, str] = {
    "1h": "最近 1 小时",
    "24h": "最近 24 小时",
    "7d": "最近 7 天",
    "30d": "最近 30 天",
}
#: sparkline 的分桶宽度：1h 用 5 分钟（12 个点），其余用小时/天。
WINDOW_BUCKETS: dict[str, timedelta] = {
    "1h": timedelta(minutes=5),
    "24h": timedelta(hours=1),
    "7d": timedelta(days=1),
    "30d": timedelta(days=1),
}
DEFAULT_WINDOW = "24h"
WINDOW_PATTERN = "^(1h|24h|7d|30d)$"

#: 单次请求最多翻多久的历史。窗口内行数超过它时按"最近 N 条"截断并记进 notes。
_MAX_SCAN_ROWS = 2000
#: 列表接口的分页上限（store 层对 list_runs 有 200 的硬上限）。
_PAGE_SIZE = 200


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    since: datetime
    until: datetime
    prev_since: datetime
    bucket: timedelta

    @property
    def previous_label(self) -> str:
        return "上一周期"


def _window(key: str, now: datetime | None = None) -> Window:
    until = now or datetime.now(timezone.utc)
    length = WINDOW_LENGTHS[key]
    return Window(
        key=key,
        label=WINDOW_LABELS[key],
        since=until - length,
        until=until,
        prev_since=until - 2 * length,
        bucket=WINDOW_BUCKETS[key],
    )


def _parse_ts(value: Any) -> datetime | None:
    """把库里的时间列统一成带时区的 UTC。

    SQLite 存的是 `utcnow().isoformat()` 字符串，Postgres 经 psycopg 回来的是
    **datetime 对象**；两种都要认，否则窗口过滤在本地能用、上线全空。
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _parse_ts(value)
    return parsed.isoformat() if parsed else (str(value) if value else None)


def _scan(
    fetch: Callable[[int, int], list[dict[str, Any]]],
    *,
    since: datetime | None,
    key: str,
    max_rows: int = _MAX_SCAN_ROWS,
) -> tuple[list[dict[str, Any]], bool]:
    """按时间倒序翻页取行，遇到早于 ``since`` 的行就停。

    返回 ``(行, 是否被 max_rows 截断)``。为什么要翻页：store 的列表接口有单页上限
    （list_runs / list_planning_sessions 都是 200），一次只拿一页会让"30 天窗口"
    实际只统计到最近几十条 —— 数字看着有，但口径是错的。
    """

    rows: list[dict[str, Any]] = []
    offset = 0
    truncated = False
    while True:
        batch = fetch(_PAGE_SIZE, offset)
        if not batch:
            break
        for row in batch:
            stamp = _parse_ts(row.get(key))
            if since is not None and stamp is not None and stamp < since:
                return rows, truncated
            if len(rows) >= max_rows:
                return rows, True
            rows.append(row)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows, truncated


def _split_window(
    rows: Iterable[dict[str, Any]], *, key: str, window: Window
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把扫到的行切成 ``(当前窗口, 上一窗口)``，用于环比。"""

    current: list[dict[str, Any]] = []
    previous: list[dict[str, Any]] = []
    for row in rows:
        stamp = _parse_ts(row.get(key))
        if stamp is None:
            continue
        if stamp >= window.since:
            current.append(row)
        elif stamp >= window.prev_since:
            previous.append(row)
    return current, previous


def _bucket_key(stamp: datetime, window: Window) -> datetime:
    """把时刻归到分桶。用"窗口起点 + n×宽度"而不是对齐自然整点：
    这样 30 天窗口的最后一个桶永远包含"现在"，不会因为对齐而丢掉尾部。"""

    index = int((stamp - window.since).total_seconds() // window.bucket.total_seconds())
    return window.since + index * window.bucket


def _series(
    rows: Iterable[dict[str, Any]], *, key: str, window: Window, reduce: Callable[[list[dict]], float | None]
) -> dict[str, Any]:
    """按分桶算一条 sparkline：缺桶补 0/None，保证点数固定。"""

    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for row in rows:
        stamp = _parse_ts(row.get(key))
        if stamp is None or stamp < window.since:
            continue
        buckets.setdefault(_bucket_key(stamp, window), []).append(row)

    points: list[dict[str, Any]] = []
    cursor = window.since
    while cursor <= window.until:
        value = reduce(buckets.get(cursor, []))
        points.append({"t": cursor.isoformat(), "value": value})
        cursor += window.bucket
    values = [point["value"] for point in points if point["value"] is not None]
    return {
        "points": points,
        "summary": {
            "latest": values[-1] if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "avg": round(sum(values) / len(values), 4) if values else None,
        },
    }


# ======================================================================
# 小工具
# ======================================================================


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(float(value) for value in values)
    if len(data) == 1:
        return data[0]
    rank = max(0.0, min(1.0, q)) * (len(data) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return data[int(rank)]
    return data[low] + (data[high] - data[low]) * (rank - low)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _rate(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def grade(score: float | None) -> str:
    """统一的等级词表：GOOD / FAIR / POOR / UNKNOWN。

    阈值只在这里定义一次 —— 前端与后端如果各有一套"多少分算好"，
    同一个页面上的两处结论会互相打架。
    """

    if score is None:
        return "UNKNOWN"
    if score >= 0.8:
        return "GOOD"
    if score >= 0.6:
        return "FAIR"
    return "POOR"


GRADE_LABELS = {"GOOD": "良好", "FAIR": "一般", "POOR": "差", "UNKNOWN": "无数据"}

#: Run 状态里"已经跑完"的那些。RUNNING 不参与成功率 —— 把它算进分母会让进行中的运行
#: 拉低成功率，运维会以为系统变差了。
FINISHED_STATUSES = ("SUCCESS", "DEGRADED", "FAILED", "CANCELLED")

#: 慢 run 阈值（与文档 §3.4 的 120 秒一致）。
SLOW_RUN_MS = 120_000


def _cost_configured() -> bool:
    config = current_config()
    return bool(config.model_price_input_per_million or config.model_price_output_per_million)


#: 并发上限。**不能超过 DB 连接池**（`app/db.py` 的 `DEFAULT_MAX_CONNECTIONS = 4`）：
#: 池子只有 4 条连接，超发的任务会卡在 acquire 上等 15 秒才拿到连接，
#: 结果比串行还慢。留一条给同进程里的其它请求，所以取 3。
_ADMIN_WORKERS = 3


def _gather(tasks: Mapping[str, Callable[[], Any]]) -> tuple[dict[str, Any], list[str]]:
    """并发跑一组互不依赖的读，返回 ``(结果, 错误说明)``。

    为什么需要它：管理端的每次聚合都要读 5–15 张表，而经 Neon 的**每次往返约 1 秒**，
    串行做首屏就是十几秒。这些读之间没有依赖关系，并发是唯一不用改存储层就能拿回的收益。
    用既有的 ``parallel_map``（受控并发 + 与输入同序 + 异常变数据），不另写一套。

    某一路失败**不**降级成 0：返回的错误说明会被拼进 payload 的 notes，
    让页面显示"这一项没读到"，而不是一个看起来正常的假数字。
    """

    keys = list(tasks)
    if not keys:
        return {}, []
    outcomes = parallel_map(
        [tasks[key] for key in keys], max_workers=_ADMIN_WORKERS, thread_prefix="tp-admin"
    )
    results: dict[str, Any] = {}
    problems: list[str] = []
    for key, outcome in zip(keys, outcomes):
        if outcome.ok:
            results[key] = outcome.value
        else:
            results[key] = None
            problems.append(f"{key} 读取失败：{outcome.error}")
    return results, problems


# ======================================================================
# 计划快照缓存（质量指标的基础）
# ======================================================================


@dataclass(slots=True)
class PlanSnapshot:
    """一次 run 的计划 + 质量画像。run 结束后不可变，可以安全缓存。"""

    run_id: str
    plan: TripPlan | None
    quality: dict[str, Any] | None
    score: float | None
    error: str | None = None


class _PlanCache:
    """按 run_id 缓存解析后的计划。

    为什么必须缓存：`list_quality` 要对一页 run 逐个 `get_plan` + `model_validate`，
    而单份 plan_json 有 100–220KB。不缓存的话管理端每翻一页都要把同样的几百 KB
    重新解一遍，还会白等 Postgres 的往返。
    """

    def __init__(self, *, ttl_seconds: float = 900.0, max_entries: int = 400) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, PlanSnapshot]] = {}

    def get(self, run_id: str) -> PlanSnapshot | None:
        with self._lock:
            entry = self._items.get(run_id)
            if entry is None:
                return None
            stored_at, snapshot = entry
            if time.monotonic() - stored_at > self._ttl:
                self._items.pop(run_id, None)
                return None
            return snapshot

    def put(self, run_id: str, snapshot: PlanSnapshot) -> None:
        with self._lock:
            if len(self._items) >= self._max:
                # 简单淘汰：去掉最早写入的那一批。缓存只是加速层，淘汰策略不需要精确。
                for key in sorted(self._items, key=lambda item: self._items[item][0])[: self._max // 4]:
                    self._items.pop(key, None)
            self._items[run_id] = (time.monotonic(), snapshot)


_PLANS = _PlanCache()


@dataclass(slots=True)
class RunContext:
    """一次 run 在质量换算里需要的东西：列表行 + 计划快照 + 来源会话。"""

    run: Mapping[str, Any]
    snapshot: PlanSnapshot
    session: Mapping[str, Any] | None


def run_contexts(store: TravelPlanStore, runs: Sequence[Mapping[str, Any]]) -> list[RunContext]:
    """并发载入一批 run 的计划快照与来源会话。

    为什么必须并发：每条 run 至少要一次 `get_plan`（有些还要一次会话读），
    而经 Neon 的每次往返约 1 秒 —— 30 条 run 串行就是半分钟，页面早就转圈转到放弃了。
    `plan_snapshot` 自带进程内缓存与锁，store 也是线程安全的，所以这里可以安全并发。
    """

    def load(run: Mapping[str, Any]) -> RunContext:
        snapshot = plan_snapshot(store, str(run.get("run_id")))
        session: Mapping[str, Any] | None = None
        session_id = run.get("source_session_id")
        if session_id:
            try:
                session = store.get_planning_session(str(session_id))
            except Exception:  # noqa: BLE001 —— 会话读不到不影响计划本身的质量
                session = None
        return RunContext(run=run, snapshot=snapshot, session=session)

    if not runs:
        return []
    outcomes = parallel_map(
        [(lambda item=run: load(item)) for run in runs],
        max_workers=_ADMIN_WORKERS,
        thread_prefix="tp-admin-plan",
    )
    contexts: list[RunContext] = []
    for run, outcome in zip(runs, outcomes):
        if outcome.ok:
            contexts.append(outcome.value)
        else:
            # 失败也保留这一行：列表里少一行会让操作员以为"这次运行不存在"。
            contexts.append(
                RunContext(
                    run=run,
                    snapshot=PlanSnapshot(
                        run_id=str(run.get("run_id")),
                        plan=None,
                        quality=None,
                        score=None,
                        error="读取失败",
                    ),
                    session=None,
                )
            )
    return contexts

#: 质量分权重。为什么把这些软指标合成一个分：页面上总得有一个可排序的数，
#: 但"哪个分值多少"必须明说（payload 里带回 weights），不能让读者猜。
QUALITY_WEIGHTS: dict[str, float] = {
    "preference_coverage": 0.25,
    "pace_match": 0.2,
    "category_diversity": 0.15,
    "backtracking_score": 0.15,
    "daily_load_balance": 0.1,
    "meal_time_quality": 0.1,
    "route_efficiency": 0.05,
}


def _quality_score(quality: Mapping[str, Any]) -> float:
    total = 0.0
    weight_sum = 0.0
    for key, weight in QUALITY_WEIGHTS.items():
        value = quality.get(key)
        if isinstance(value, (int, float)):
            total += float(value) * weight
            weight_sum += weight
    return round(total / weight_sum, 4) if weight_sum else 0.0


def plan_snapshot(store: TravelPlanStore, run_id: str) -> PlanSnapshot:
    """取（或算出）一份 run 的计划与质量画像。

    ``places`` / ``evidences`` 不传：它们要按 run 再查两张表。代价是
    ``preference_coverage`` 退化成"按 item 名称与理由文本匹配"的近似口径
    （planner 自己文档里写明的降级行为），这一点在 payload 的 notes 里如实标出。
    """

    cached = _PLANS.get(run_id)
    if cached is not None:
        return cached

    snapshot = PlanSnapshot(run_id=run_id, plan=None, quality=None, score=None, error="无计划")
    try:
        stored = store.get_plan(run_id)
    except Exception as exc:  # noqa: BLE001 —— 单条计划读不出来不该让整页失败
        snapshot = PlanSnapshot(run_id=run_id, plan=None, quality=None, score=None, error=f"{type(exc).__name__}")
        _PLANS.put(run_id, snapshot)
        return snapshot

    if not stored or not isinstance(stored.get("plan"), dict):
        _PLANS.put(run_id, snapshot)
        return snapshot

    try:
        plan = TripPlan.model_validate(stored["plan"])
    except Exception:  # noqa: BLE001 —— 库里存了旧结构，如实标"解析失败"而不是猜
        snapshot = PlanSnapshot(run_id=run_id, plan=None, quality=None, score=None, error="计划结构无法解析")
        _PLANS.put(run_id, snapshot)
        return snapshot

    quality = planner.plan_quality(plan.days, plan.intent).to_dict()
    snapshot = PlanSnapshot(
        run_id=run_id, plan=plan, quality=quality, score=_quality_score(quality)
    )
    _PLANS.put(run_id, snapshot)
    return snapshot


# ======================================================================
# Dashboard：KPI + 趋势 + 当前需要关注
# ======================================================================


def _kpi(
    key: str,
    label: str,
    value: Any,
    *,
    unit: str | None = None,
    previous: Any = None,
    higher_is_better: bool = True,
    hint: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """一条关键指标。``delta`` 与本指标同单位，前端只做箭头与配色。"""

    delta = None
    if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
        delta = round(float(value) - float(previous), 4)
    direction = "flat"
    if delta is not None and abs(delta) > 1e-9:
        direction = "up" if delta > 0 else "down"
    better = None
    if direction != "flat" and delta is not None:
        better = (direction == "up") == higher_is_better
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "previous": previous,
        "delta": delta,
        "direction": direction,
        "higher_is_better": higher_is_better,
        "better": better,
        "hint": hint,
        "detail": detail,
    }


def _run_stats(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(run.get("status") or "UNKNOWN") for run in runs)
    finished = [run for run in runs if str(run.get("status")) in FINISHED_STATUSES]
    durations = [
        float(run["duration_ms"])
        for run in finished
        if isinstance(run.get("duration_ms"), (int, float)) and run["duration_ms"] > 0
    ]
    costs = [
        float(run["cost"])
        for run in finished
        if isinstance(run.get("cost"), (int, float)) and not isinstance(run.get("cost"), bool)
    ]
    return {
        "runs": len(runs),
        "finished": len(finished),
        "statuses": dict(statuses),
        "success": statuses.get("SUCCESS", 0),
        "degraded": statuses.get("DEGRADED", 0),
        "failed": statuses.get("FAILED", 0),
        "cancelled": statuses.get("CANCELLED", 0),
        "running": statuses.get("RUNNING", 0),
        "success_rate": _rate(statuses.get("SUCCESS", 0), len(finished)),
        "degrade_rate": _rate(statuses.get("DEGRADED", 0), len(finished)),
        "failure_rate": _rate(statuses.get("FAILED", 0), len(finished)),
        "p95_duration_ms": int(_percentile(durations, 0.95)) if durations else None,
        "avg_duration_ms": int(_mean(durations)) if durations else None,
        "avg_cost": _mean(costs),
        "total_cost": round(sum(costs), 4) if costs else None,
        "cost_known_runs": len(costs),
        "slow_runs": [
            run
            for run in finished
            if isinstance(run.get("duration_ms"), (int, float)) and run["duration_ms"] > SLOW_RUN_MS
        ],
    }


def _provider_rows(store: TravelPlanStore, *, limit_per_provider: int = 200) -> list[dict[str, Any]]:
    """Provider 健康行 + 一个自己的判定（成功率、是否需要关注）。

    判定口径写在这里而不是各页面：Dashboard 的"异常 Provider 数"和 Provider 页的红点
    必须来自同一个判断，否则两处数字对不上。
    """

    rows: list[dict[str, Any]] = []
    for raw in store.provider_call_stats(limit_per_provider=limit_per_provider):
        calls = int(raw.get("calls") or 0)
        failures = int(raw.get("failures") or 0)
        timeouts = int(raw.get("timeouts") or 0)
        success_rate = _rate(int(raw.get("successes") or 0), calls)
        failure_rate = _rate(failures, calls)
        state = "OK"
        if calls == 0:
            state = "IDLE"
        elif failure_rate is not None and failure_rate >= 0.5:
            state = "UNAVAILABLE"
        elif failure_rate is not None and failure_rate >= 0.2:
            state = "DEGRADED"
        rows.append(
            {
                **raw,
                "calls": calls,
                "failures": failures,
                "timeouts": timeouts,
                "fallback_count": int(raw.get("fallback_count") or 0),
                "success_rate": success_rate,
                "failure_rate": failure_rate,
                "state": state,
                "needs_attention": state in ("DEGRADED", "UNAVAILABLE"),
            }
        )
    return rows


def _attention(
    *,
    window: Window,
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
    providers: Sequence[Mapping[str, Any]],
    badcases_current: Sequence[Mapping[str, Any]],
    badcases_previous: Sequence[Mapping[str, Any]],
    badcase_open: int,
    quality_score: float | None,
    quality_scored_runs: int,
) -> list[dict[str, Any]]:
    """当前需要关注：把"该修什么"写成可点击的条目。

    每条都必须指向一个**已经能看的数据**（一个列表、一个筛选）。写不出跳转的告警
    只会让操作员在页面上再找一遍，那就等于没写。
    """

    items: list[dict[str, Any]] = []

    def add(
        key: str,
        level: str,
        title: str,
        detail: str,
        *,
        action: Mapping[str, Any] | None = None,
        metric: Mapping[str, Any] | None = None,
    ) -> None:
        items.append(
            {
                "id": key,
                "level": level,
                "title": title,
                "detail": detail,
                "action": dict(action or {}),
                "metric": dict(metric or {}),
            }
        )

    # 1. Provider 失败率
    for provider in providers:
        if not provider.get("needs_attention"):
            continue
        rate = provider.get("failure_rate") or 0.0
        level = "high" if provider.get("state") == "UNAVAILABLE" else "medium"
        add(
            f"provider:{provider['provider']}",
            level,
            f"{provider['provider']} 状态 {provider['state']}",
            f"最近 {provider['calls']} 次调用失败率 {rate * 100:.0f}%"
            f"（失败 {provider['failures']}、超时 {provider['timeouts']}、fallback {provider['fallback_count']}）",
            action={"kind": "provider", "id": str(provider["provider"]), "label": "查看 Provider"},
            metric={"failure_rate": rate, "calls": provider["calls"]},
        )

    # 2. 慢 run
    slow = list(current.get("slow_runs") or [])
    if slow:
        worst = max(slow, key=lambda run: float(run.get("duration_ms") or 0))
        add(
            "slow_runs",
            "medium" if len(slow) < 5 else "high",
            f"{len(slow)} 个运行超过 {SLOW_RUN_MS // 1000} 秒",
            f"最慢 {float(worst.get('duration_ms') or 0) / 1000:.0f}s（{worst.get('run_id')}）。"
            "点进去看轨迹里哪一段最耗时。",
            action={"kind": "runs", "label": "查看运行记录"},
            metric={"count": len(slow), "worst_run_id": worst.get("run_id")},
        )

    # 3. 失败 run
    if current.get("failed"):
        add(
            "failed_runs",
            "high",
            f"{current['failed']} 个运行失败",
            f"{window.label}内有运行以 FAILED 结束，失败率 {(current.get('failure_rate') or 0) * 100:.1f}%",
            action={"kind": "runs", "label": "查看失败运行", "status": "FAILED"},
            metric={"failed": current["failed"]},
        )

    # 4. 未处理 Bad Case（积压，不看窗口）
    pending_high = sum(
        1 for case in badcases_current if str(case.get("severity")) == "high"
    )
    if badcase_open:
        add(
            "badcase_open",
            "high" if pending_high else "medium",
            f"{badcase_open} 条 Bad Case 未处理",
            f"其中本窗口新增 {len(badcases_current)} 条"
            + (f"，HIGH {pending_high} 条" if pending_high else ""),
            action={"kind": "badcases", "label": "打开问题中心", "analysis_status": "pending"},
            metric={"open": badcase_open, "window": len(badcases_current)},
        )

    # 5. 某一类问题在变多
    for category, count in Counter(str(case.get("category")) for case in badcases_current).most_common(3):
        baseline = sum(1 for case in badcases_previous if str(case.get("category")) == category)
        if count < 3:
            continue
        if baseline and count >= baseline * 1.4:
            growth = (count - baseline) / baseline
            add(
                f"badcase_growth:{category}",
                "medium",
                f"{category} 在变多",
                f"{window.label}出现 {count} 次，上一周期 {baseline} 次（+{growth * 100:.0f}%）",
                action={"kind": "badcases", "label": "查看这一类", "category": category},
                metric={"count": count, "previous": baseline, "growth": round(growth, 4)},
            )
        elif not baseline and count >= 5:
            add(
                f"badcase_new:{category}",
                "medium",
                f"{category} 本周期反复出现",
                f"{window.label}出现 {count} 次，上一周期为 0",
                action={"kind": "badcases", "label": "查看这一类", "category": category},
                metric={"count": count, "previous": 0},
            )

    # 6. 用户偏好被忽略（文档点名的"最该修"之一）
    ignored = sum(1 for case in badcases_current if str(case.get("category")) == "user_preference_ignored")
    if ignored:
        add(
            "preference_ignored",
            "high" if ignored >= 3 else "medium",
            f"{ignored} 次忽略用户偏好",
            "用户明确表达的偏好没有体现在最终行程里（或住宿区域与动态偏好画像不一致）",
            action={"kind": "quality", "label": "打开质量页", "focus": "preference_match"},
            metric={"count": ignored},
        )

    # 7. 降级率抬头
    degrade_delta = None
    if current.get("degrade_rate") is not None and previous.get("degrade_rate") is not None:
        degrade_delta = current["degrade_rate"] - previous["degrade_rate"]
    if degrade_delta is not None and degrade_delta >= 0.1 and (current.get("degraded") or 0) >= 2:
        add(
            "degrade_rate",
            "medium",
            "降级率上升",
            f"{window.label}降级率 {(current['degrade_rate']) * 100:.1f}%，"
            f"上一周期 {(previous['degrade_rate']) * 100:.1f}%",
            action={"kind": "runs", "label": "查看降级运行", "status": "DEGRADED"},
            metric={"current": current["degrade_rate"], "previous": previous["degrade_rate"]},
        )

    # 8. 行程质量
    if quality_score is not None and quality_scored_runs >= 3 and quality_score < 0.6:
        add(
            "quality_low",
            "high",
            "行程质量偏低",
            f"最近 {quality_scored_runs} 个有计划的运行，综合质量分 {quality_score * 100:.0f}（差档）",
            action={"kind": "quality", "label": "打开质量页"},
            metric={"score": quality_score, "runs": quality_scored_runs},
        )

    level_order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda item: level_order.get(str(item["level"]), 3))
    return items


def dashboard(store: TravelPlanStore, *, window: Window, quality_sample: int = 10) -> dict[str, Any]:
    """Dashboard 首屏：关键指标 + 环比 + 趋势 + 当前需要关注。"""

    notes: list[str] = []

    def scan_runs() -> tuple[list[dict[str, Any]], bool]:
        return _scan(
            lambda limit, offset: store.list_runs(limit=limit, offset=offset, include_benchmark=False),
            since=window.prev_since,
            key="created_at",
        )

    def scan_cases() -> tuple[list[dict[str, Any]], bool]:
        return _scan(
            lambda limit, offset: store.list_badcases(limit=limit, offset=offset)[0],
            since=window.prev_since,
            key="created_at",
        )

    gathered, problems = _gather(
        {
            "runs": scan_runs,
            "cases": scan_cases,
            "badcase_open": lambda: store.count_badcases(analysis_status="pending"),
            "providers": lambda: _provider_rows(store),
            "benchmarks": lambda: store.list_benchmark_runs(limit=1),
            "evolves": lambda: store.list_evolution_runs(limit=1),
        }
    )
    notes.extend(problems)

    scanned_runs, runs_truncated = gathered["runs"] or ([], False)
    current_runs, previous_runs = _split_window(scanned_runs, key="created_at", window=window)
    if runs_truncated:
        notes.append(f"窗口内运行数超过 {_MAX_SCAN_ROWS} 条，指标按最近的 {_MAX_SCAN_ROWS} 条统计")

    stats = _run_stats(current_runs)
    previous_stats = _run_stats(previous_runs)

    scanned_cases, cases_truncated = gathered["cases"] or ([], False)
    cases_current, cases_previous = _split_window(scanned_cases, key="created_at", window=window)
    if cases_truncated:
        notes.append(f"窗口内 Bad Case 超过 {_MAX_SCAN_ROWS} 条，按最近的 {_MAX_SCAN_ROWS} 条统计")

    badcase_open = gathered["badcase_open"] or 0

    # 质量只抽最近若干个有计划的 run：一次列表 50 条 = 5–10MB 的 plan_json，
    # 全量算会让首屏白等。抽样时如实标 sampled。
    sampled_plans: list[float] = []
    sampled_facts: list[dict[str, Any]] = []
    sample_runs = current_runs[: max(0, quality_sample)]
    # 复用质量页同一份换算：抽样运行既然已经解了计划，就把决策健康一起算出来，
    # 免得为了三个计数再去查一遍会话。样本量为 0 时这一段完全不发请求。
    for context in run_contexts(store, sample_runs):
        if context.snapshot.plan is None:
            continue
        sampled_facts.append(
            _run_facts(run=context.run, snapshot=context.snapshot, session=context.session)
        )
        if context.snapshot.score is not None:
            sampled_plans.append(context.snapshot.score)
    quality_score = _mean(sampled_plans)
    quality_sampled = len(sampled_plans) < stats["finished"] and len(sampled_plans) >= quality_sample

    providers = gathered["providers"] or []
    unhealthy = [provider for provider in providers if provider["needs_attention"]]

    config = current_config()
    if not _cost_configured():
        notes.append("未配置模型单价，成本字段为空（不是 0）")

    kpis = [
        _kpi(
            "success_rate",
            "成功率",
            stats["success_rate"],
            unit="ratio",
            previous=previous_stats["success_rate"],
            hint=f"{stats['success']}/{stats['finished']} 个已完成运行",
        ),
        _kpi(
            "degrade_rate",
            "降级率",
            stats["degrade_rate"],
            unit="ratio",
            previous=previous_stats["degrade_rate"],
            higher_is_better=False,
            hint=f"{stats['degraded']} 个降级完成",
        ),
        _kpi(
            "failure_rate",
            "失败率",
            stats["failure_rate"],
            unit="ratio",
            previous=previous_stats["failure_rate"],
            higher_is_better=False,
            hint=f"{stats['failed']} 个失败",
        ),
        _kpi(
            "p95_duration_ms",
            "P95 规划耗时",
            stats["p95_duration_ms"],
            unit="ms",
            previous=previous_stats["p95_duration_ms"],
            higher_is_better=False,
            hint=f"平均 {int((stats['avg_duration_ms'] or 0) / 1000)}s"
            if stats["avg_duration_ms"]
            else None,
        ),
        _kpi(
            "avg_cost",
            "平均成本 / Run",
            stats["avg_cost"],
            unit="cny",
            previous=previous_stats["avg_cost"],
            higher_is_better=False,
            hint=f"{stats['cost_known_runs']} 个运行有成本快照" if stats["cost_known_runs"] else "未配置单价",
        ),
        _kpi(
            "badcase_open",
            "未处理 Bad Case",
            badcase_open,
            unit="count",
            previous=None,
            higher_is_better=False,
            hint=f"本窗口新增 {len(cases_current)} 条",
        ),
        _kpi(
            "unhealthy_providers",
            "异常 Provider",
            len(unhealthy),
            unit="count",
            previous=None,
            higher_is_better=False,
            hint="、".join(str(item["provider"]) for item in unhealthy[:3]) or "全部正常",
        ),
        _kpi(
            "quality_score",
            "行程质量分",
            quality_score,
            unit="ratio",
            previous=None,
            hint=f"抽样 {len(sampled_plans)} 个运行"
            + ("（窗口内运行更多，仅抽样）" if quality_sampled else ""),
        ),
    ]

    trends = [
        {
            "key": "runs",
            "label": "运行数",
            "unit": "count",
            **_series(current_runs, key="created_at", window=window, reduce=lambda rows: float(len(rows))),
        },
        {
            "key": "success_rate",
            "label": "成功率",
            "unit": "ratio",
            **_series(
                current_runs,
                key="created_at",
                window=window,
                reduce=lambda rows: (
                    _rate(
                        sum(1 for row in rows if str(row.get("status")) == "SUCCESS"),
                        sum(1 for row in rows if str(row.get("status")) in FINISHED_STATUSES),
                    )
                    if any(str(row.get("status")) in FINISHED_STATUSES for row in rows)
                    else None
                ),
            ),
        },
        {
            "key": "avg_duration_ms",
            "label": "平均耗时",
            "unit": "ms",
            **_series(
                current_runs,
                key="created_at",
                window=window,
                reduce=lambda rows: _mean(
                    [
                        float(row["duration_ms"])
                        for row in rows
                        if isinstance(row.get("duration_ms"), (int, float)) and row["duration_ms"] > 0
                    ]
                ),
            ),
        },
        {
            "key": "avg_cost",
            "label": "平均成本",
            "unit": "cny",
            **_series(
                current_runs,
                key="created_at",
                window=window,
                reduce=lambda rows: _mean(
                    [
                        float(row["cost"])
                        for row in rows
                        if isinstance(row.get("cost"), (int, float)) and not isinstance(row.get("cost"), bool)
                    ]
                ),
            ),
        },
        {
            "key": "badcases",
            "label": "新增 Bad Case",
            "unit": "count",
            **_series(cases_current, key="created_at", window=window, reduce=lambda rows: float(len(rows))),
        },
    ]

    benchmarks = gathered["benchmarks"] or []
    evolves = gathered["evolves"] or []

    return {
        "window": window.key,
        "window_label": window.label,
        "generated_at": now_iso(),
        "kpis": kpis,
        "statuses": stats["statuses"],
        "volume": {
            "runs": stats["runs"],
            "previous_runs": previous_stats["runs"],
            "finished": stats["finished"],
            "previous_finished": previous_stats["finished"],
            "running": stats["running"],
        },
        "trends": trends,
        "attention": _attention(
            window=window,
            current=stats,
            previous=previous_stats,
            providers=providers,
            badcases_current=cases_current,
            badcases_previous=cases_previous,
            badcase_open=badcase_open,
            quality_score=quality_score,
            quality_scored_runs=len(sampled_plans),
        ),
        "cost": {
            "total": stats["total_cost"],
            "avg_per_run": stats["avg_cost"],
            "previous_avg_per_run": previous_stats["avg_cost"],
            # 模型单价的计价单位（控制台里成本一律按 $ 显示）；这不是行程预算那种人民币口径。
            "currency": "USD",
            "price_configured": _cost_configured(),
        },
        "quality": {
            "score": quality_score,
            "grade": grade(quality_score),
            "sampled_runs": len(sampled_plans),
            "sampled": quality_sampled,
        },
        "decision_health": _decision_health(sampled_facts),
        "badcases": {
            "open": badcase_open,
            "window": len(cases_current),
            "previous": len(cases_previous),
            "by_severity": dict(Counter(str(case.get("severity")) for case in cases_current)),
        },
        "providers": {
            "total": len(providers),
            "read_failed": gathered["providers"] is None,
            "unhealthy": len(unhealthy),
            "window_note": "成功率/失败率按每个 Provider 最近 200 次调用统计（不是按时间窗）",
            "items": providers,
        },
        "benchmark": {
            "latest_run_id": benchmarks[0]["benchmark_run_id"] if benchmarks else None,
            "latest_pass_rate": _pass_rate(benchmarks[0]) if benchmarks else None,
        },
        "evolution": {
            "enabled": config.evolution_enabled,
            "last_run_id": evolves[0]["evolution_run_id"] if evolves else None,
            "last_decision": evolves[0]["decision"] if evolves else None,
        },
        "notes": notes,
    }


def _pass_rate(benchmark_run: Mapping[str, Any]) -> float | None:
    case_count = int(benchmark_run.get("case_count") or 0)
    passed = benchmark_run.get("passed")
    if not case_count or passed is None:
        return None
    return round(int(passed) / case_count, 4)


def _decision_health(facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """决策健康：替代"Jev 是不是活着"的那一组指标。

    文档 §10 把 Jev 降为可选增强，主指标换成**决策层自己**的事实：
    动态偏好画像有没有真的由模型生成、硬校验有没有把问题留下、REJECT 有没有进计划。
    这三件事都能从已抽样的运行上直接数出来，不需要任何新埋点。
    """

    runs = len(facts)
    profile_llm = sum(1 for item in facts if item.get("profile_source") == "llm")
    profile_fallback = sum(1 for item in facts if item.get("profile_source") == "fallback")
    # 没画像分两种情况：Quick 模式本来就没有画像是正常的，所以单独计数而不是算作失败。
    profile_missing = runs - profile_llm - profile_fallback
    hard_errors = sum(1 for item in facts if (item.get("validation") or {}).get("errors"))
    rejected = sum(1 for item in facts if item.get("rejected_in_plan"))
    must_missing = sum(1 for item in facts if item.get("must_missing"))
    guided = sum(1 for item in facts if item.get("source") == "guided")
    return {
        "runs": runs,
        "guided_runs": guided,
        "profile_llm": profile_llm,
        "profile_fallback": profile_fallback,
        "profile_missing": profile_missing,
        # 分母只用引导式运行：Quick 模式没有会话，也就没有画像可谈。
        "profile_success_rate": _rate(profile_llm, guided) if guided else None,
        "runs_with_hard_errors": hard_errors,
        "runs_with_rejected_in_plan": rejected,
        "runs_missing_must": must_missing,
        "note": "按最近抽样的运行统计；profile_success_rate 的分母是引导式运行（Quick 没有画像）",
    }


# ======================================================================
# Travel Quality：行程质量画像
# ======================================================================


def _destination_text(intent: Any) -> str | None:
    """把意图里的目的地归一成一段文本。

    ``TripIntent.destination`` 是 **list[str]**（一次需求可以带多个目的地），
    直接塞进 payload 会渲染成 "['成都']"，既难看又没法用来分组。
    """

    value = getattr(intent, "destination", None)
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(parts) if parts else None
    text = str(value or "").strip()
    return text or None


def _item_legs(plan: TripPlan) -> list[Any]:
    return [
        item.travel_from_previous
        for day in plan.days
        for item in day.items
        if item.travel_from_previous is not None
    ]


def _profile_of(session: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not session:
        return None
    prefetch = session.get("prefetch") or {}
    profile = prefetch.get("profile")
    return profile if isinstance(profile, dict) else None


def _area_text(value: Any) -> str:
    return str(value or "").strip()


def _areas_overlap(left: str, right: str) -> bool | None:
    """两个区域名是否是同一片地方。

    这里**只能是文本近似**：住宿区域的候选名来自攻略抽取（"春熙路"），而最终酒店的
    ``business_area`` 来自 OTA（"春熙路/太古里"），两者没有共同主键可对。
    因此结果标注为近似（payload 里带 ``note``），不当作硬结论。
    """

    left, right = _area_text(left), _area_text(right)
    if not left or not right:
        return None
    if left == right:
        return True
    return left in right or right in left


def _run_facts(
    *,
    run: Mapping[str, Any],
    snapshot: PlanSnapshot,
    session: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把一份计划摊成六个维度要用到的**原始事实**。

    这里只做换算，不下结论：结论在 ``_dimension_*`` 里，好处是同一批事实既能
    聚合成页面指标，也能在单个 run 上原样展示（不用两套口径）。
    """

    plan = snapshot.plan
    quality = snapshot.quality or {}
    facts: dict[str, Any] = {
        "run_id": str(run.get("run_id")),
        "status": str(run.get("status") or "UNKNOWN"),
        "created_at": _iso(run.get("created_at")),
        "duration_ms": run.get("duration_ms"),
        "cost": run.get("cost"),
        "source": str(run.get("source") or "quick"),
        "quality": quality,
        "score": snapshot.score,
        "grade": grade(snapshot.score),
        "error": snapshot.error,
        "destination": None,
        "days": 0,
    }
    if plan is None:
        return facts

    intent = plan.intent
    facts["destination"] = _destination_text(intent)
    facts["days"] = len(plan.days)
    facts["preferences"] = [str(item) for item in (getattr(intent, "preferences", None) or ())]
    facts["pace"] = getattr(intent, "pace", None)
    facts["budget_total"] = getattr(plan.budget, "budget_total", None)
    facts["budget_projected"] = getattr(plan.budget, "projected_total", None)
    facts["budget_status"] = getattr(plan.budget, "status", None)

    profile = _profile_of(session)
    facts["profile"] = profile
    facts["profile_source"] = (profile or {}).get("source")
    facts["hotel_areas_candidate"] = [
        area.get("name") for area in ((session or {}).get("prefetch") or {}).get("hotel_areas") or []
    ][:8]

    # --- 偏好匹配 ---
    selections = {}
    if session:
        selections = dict(session.get("poi_selections") or {})
    must_ids = {key for key, value in selections.items() if str(value).upper() == "MUST"}
    reject_ids = {key for key, value in selections.items() if str(value).upper() == "REJECT"}
    plan_place_ids = {
        str(item.place_id) for day in plan.days for item in day.items if item.place_id
    }
    facts["must_total"] = len(must_ids)
    facts["must_missing"] = sorted(must_ids - plan_place_ids)
    facts["rejected_in_plan"] = sorted(reject_ids & plan_place_ids)

    hotel_area_selected = _area_text(getattr(plan.hotel.selected, "business_area", None)) if plan.hotel and plan.hotel.selected else ""
    profile_area = _area_text((profile or {}).get("hotel_area"))
    facts["hotel_area_selected"] = hotel_area_selected or None
    facts["profile_hotel_area"] = profile_area or None
    facts["hotel_area_aligned"] = _areas_overlap(profile_area, hotel_area_selected)

    # --- 酒店 ---
    selected = plan.hotel.selected if plan.hotel else None
    facts["hotel"] = (
        {
            "name": selected.name,
            "rating": selected.rating,
            "price_per_night": selected.price_per_night,
            "total_price": selected.total_price,
            "business_area": selected.business_area,
            "address": selected.address,
            "provider": selected.provider,
            "verified": bool(selected.source_url),
            "has_reason": bool(selected.selection_reason),
            "cancellation_policy": bool(selected.cancellation_policy),
            "alternatives": len(plan.hotel.alternatives or []),
            "selection_reason": selected.selection_reason,
        }
        if selected is not None
        else None
    )

    # --- 美食 ---
    food_items = [item for day in plan.days for item in day.items if item.type == "food"]
    facts["food"] = {
        "count": len(food_items),
        "expected_slots": max(1, quality.get("playable_days") or len(plan.days)) * len(planner.MEAL_WINDOWS),
        "with_place": sum(1 for item in food_items if item.place_id),
        "with_evidence": sum(1 for item in food_items if item.evidence_ids),
        "with_price": sum(1 for item in food_items if item.price is not None),
        "high_ad_risk": sum(1 for item in food_items if str(item.ad_risk) == "high"),
        "duplicate_names": sum(
            count - 1 for count in Counter(item.name for item in food_items).values() if count > 1
        ),
        "avg_detour_meters": _mean(
            [
                float(item.travel_from_previous.distance_meters)
                for item in food_items
                if item.travel_from_previous is not None
                and isinstance(item.travel_from_previous.distance_meters, (int, float))
            ]
        ),
        "max_detour_meters": max(
            [
                int(item.travel_from_previous.distance_meters)
                for item in food_items
                if item.travel_from_previous is not None
                and isinstance(item.travel_from_previous.distance_meters, (int, float))
            ],
            default=None,
        ),
        "meal_time_quality": quality.get("meal_time_quality"),
    }

    # --- 行程 ---
    per_day_stays = [
        len([item for item in day.items if item.type in planner.STAY_TYPES]) for day in plan.days
    ]
    facts["itinerary"] = {
        "items": sum(len(day.items) for day in plan.days),
        "stays": sum(per_day_stays),
        "stays_per_day": _mean([float(value) for value in per_day_stays if value]),
        "max_stays_per_day": max(per_day_stays) if per_day_stays else 0,
        "free_time": sum(1 for day in plan.days for item in day.items if item.type == "free_time"),
        "areas": [day.area for day in plan.days if day.area],
        "days_without_area": sum(1 for day in plan.days if not day.area),
        "category_diversity": quality.get("category_diversity"),
        "daily_load_balance": quality.get("daily_load_balance"),
        "first_day_quality": quality.get("first_day_quality"),
        "last_day_quality": quality.get("last_day_quality"),
        "pace_match": quality.get("pace_match"),
    }

    # --- 路线 ---
    legs = _item_legs(plan)
    distances = [
        float(leg.distance_meters) for leg in legs if isinstance(leg.distance_meters, (int, float))
    ]
    durations = [
        float(leg.duration_seconds) for leg in legs if isinstance(leg.duration_seconds, (int, float))
    ]
    verified = sum(1 for leg in legs if leg.verified)
    max_leg_limit = float(current_config().route_max_leg_meters or 0)
    facts["route"] = {
        "legs": len(legs),
        "verified": verified,
        "verified_rate": _rate(verified, len(legs)),
        "avg_distance_meters": _mean(distances),
        "total_distance_meters": round(sum(distances)) if distances else None,
        "avg_leg_seconds": _mean(durations),
        "max_leg_meters": max((int(value) for value in distances), default=None),
        "over_limit_legs": (
            sum(1 for value in distances if value > max_leg_limit) if max_leg_limit else None
        ),
        "max_leg_limit_meters": max_leg_limit or None,
        "backtracking_score": quality.get("backtracking_score"),
        "active_minutes": quality.get("active_minutes"),
    }

    # --- 硬校验 ---
    warnings = list(plan.warnings or [])
    facts["validation"] = {
        "warnings": len(warnings),
        "errors": sum(1 for issue in warnings if issue.severity == "error"),
        "unresolved": sum(1 for issue in warnings if not issue.resolved),
        "codes": dict(Counter(str(issue.code) for issue in warnings)),
        "unverified_routes": sum(1 for issue in warnings if str(issue.code) == "ROUTE_UNVERIFIED"),
        "opening_hours": sum(
            1
            for issue in warnings
            if "OPENING" in str(issue.code) or "CLOSING" in str(issue.code) or "LAST_ENTRY" in str(issue.code)
        ),
        "return_conflicts": sum(1 for issue in warnings if str(issue.code) == "RETURN_DEPARTURE_CONFLICT"),
        "overloaded_days": sum(1 for issue in warnings if str(issue.code) == "DAY_OVERLOADED"),
        "transport_missing": sum(
            1 for issue in warnings if str(issue.code) in ("OUTBOUND_UNAVAILABLE", "INBOUND_UNAVAILABLE")
        ),
        "hotel_missing": sum(1 for issue in warnings if str(issue.code) == "HOTEL_UNAVAILABLE"),
    }
    return facts


def _dimension(
    key: str,
    label: str,
    *,
    score: float | None,
    metrics: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]] = (),
    offenders: Sequence[Mapping[str, Any]] = (),
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "score": score,
        "score_percent": round(score * 100) if score is not None else None,
        "grade": grade(score),
        "grade_label": GRADE_LABELS[grade(score)],
        "metrics": list(metrics),
        "findings": list(findings),
        "offenders": list(offenders),
        "note": note,
    }


def _metric(key: str, label: str, value: Any, *, unit: str | None = None, hint: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "unit": unit, "hint": hint}


def _checked_score(checks: Sequence[tuple[str, bool | None]]) -> float | None:
    """只对**判断得出来**的检查项计分（None 表示数据不足，不参与），
    并返回 ``passed / checked``。这样"没有评分数据"不会伪装成"很差"。"""

    checked = [value for _, value in checks if value is not None]
    if not checked:
        return None
    return round(sum(1 for value in checked if value) / len(checked), 4)


def _preference_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    coverages = [
        float(item["quality"]["preference_coverage"])
        for item in with_plan
        if isinstance((item.get("quality") or {}).get("preference_coverage"), (int, float))
    ]
    declared = [item for item in with_plan if item.get("preferences")]
    aligned = [item for item in with_plan if item.get("hotel_area_aligned") is not None]
    profile_llm = sum(1 for item in with_plan if item.get("profile_source") == "llm")
    profile_fallback = sum(1 for item in with_plan if item.get("profile_source") == "fallback")
    must_missing = [item for item in with_plan if item.get("must_missing")]
    rejected_in_plan = [item for item in with_plan if item.get("rejected_in_plan")]

    checks = [
        ("声明了偏好时有覆盖分", None if not declared else (len(coverages) == len(declared))),
        ("住宿区域与偏好区域一致", None if not aligned else all(bool(item["hotel_area_aligned"]) for item in aligned)),
        ("MUST 地点没有丢失", None if not with_plan else not must_missing),
        ("REJECT 地点没有进计划", None if not with_plan else not rejected_in_plan),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if must_missing:
        findings.append(
            {"level": "high", "text": f"{len(must_missing)} 个运行丢了用户标 MUST 的地点"}
        )
    if rejected_in_plan:
        findings.append(
            {"level": "high", "text": f"{len(rejected_in_plan)} 个运行的行程里出现了用户 REJECT 的地点"}
        )
    mismatched = [item for item in aligned if not item["hotel_area_aligned"]]
    if mismatched:
        findings.append(
            {
                "level": "medium",
                "text": f"{len(mismatched)} 个运行的住宿区域与动态偏好画像不一致（文本近似判断）",
            }
        )
    if profile_fallback and not profile_llm:
        findings.append(
            {
                "level": "low",
                "text": f"{profile_fallback} 个运行的动态偏好画像走了 fallback（LLM 未生成），个性化程度有限",
            }
        )

    offenders = sorted(
        [item for item in with_plan if item.get("score") is not None],
        key=lambda item: float(item["score"]),
    )[:5]

    return _dimension(
        "preference_match",
        "偏好匹配",
        score=score,
        metrics=[
            _metric("preference_coverage", "偏好覆盖率（均值）", _mean(coverages), unit="ratio",
                    hint="按 item 名称与理由文本近似匹配，未接地点分类表"),
            _metric("declared_runs", "声明了偏好的运行", len(declared), unit="count"),
            _metric("profile_llm", "画像由 LLM 生成", profile_llm, unit="count"),
            _metric("profile_fallback", "画像走 fallback", profile_fallback, unit="count"),
            _metric("must_missing_runs", "丢 MUST 的运行", len(must_missing), unit="count"),
            _metric("rejected_in_plan_runs", "REJECT 进计划的运行", len(rejected_in_plan), unit="count"),
            _metric("area_mismatch_runs", "区域疑似不一致", len(mismatched), unit="count"),
        ],
        findings=findings,
        offenders=[
            {
                "run_id": item["run_id"],
                "detail": f"偏好覆盖 {float(item['score']) * 100:.0f} 分；"
                f"声明 {len(item.get('preferences') or [])} 项，"
                f"丢 MUST {len(item.get('must_missing') or [])} 个",
                "value": item["score"],
            }
            for item in offenders
        ],
        note="住宿区域一致性是文本近似判断（候选区域名与 OTA 的 business_area 没有共同主键）",
    )


def _hotel_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    hotels = [item["hotel"] for item in with_plan if item.get("hotel")]

    def check(values: Sequence[bool | None]) -> bool | None:
        known = [value for value in values if value is not None]
        if not known:
            return None
        return all(known)

    ratings = [float(hotel["rating"]) for hotel in hotels if isinstance(hotel.get("rating"), (int, float))]
    prices = [
        float(hotel["price_per_night"])
        for hotel in hotels
        if isinstance(hotel.get("price_per_night"), (int, float))
    ]
    aligned = [item for item in with_plan if item.get("hotel_area_aligned") is not None]
    mismatched = [item for item in aligned if not item["hotel_area_aligned"]]

    checks = [
        ("选出了住宿", None if not with_plan else len(hotels) == len(with_plan)),
        ("酒店标了所在区域", None if not hotels else check([bool(hotel.get("business_area")) for hotel in hotels])),
        ("住宿区域与偏好一致", None if not aligned else not mismatched),
        ("酒店有评分", None if not hotels else bool(ratings)),
        ("评分不低于 4.0", None if not ratings else all(value >= 4.0 for value in ratings)),
        ("给出了选择理由", None if not hotels else check([bool(hotel.get("has_reason")) for hotel in hotels])),
        ("比较过备选", None if not hotels else all(int(hotel.get("alternatives") or 0) > 0 for hotel in hotels)),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if mismatched:
        findings.append(
            {"level": "medium", "text": f"{len(mismatched)} 个运行的住宿区域和用户偏好对的不是一个地方"}
        )
    no_area = [hotel for hotel in hotels if not hotel.get("business_area")]
    if no_area:
        findings.append(
            {
                "level": "medium",
                "text": f"{len(no_area)} 个运行的酒店没有区域信息，无法判断'选对地方了吗'",
            }
        )
    if not hotels and with_plan:
        findings.append({"level": "high", "text": "有计划却没有住宿选择"})

    return _dimension(
        "hotel_quality",
        "住宿质量",
        score=score,
        metrics=[
            _metric("hotels", "有住宿选择的运行", len(hotels), unit="count"),
            _metric("avg_rating", "平均评分", _mean(ratings), hint="OTA 原值，未做归一"),
            _metric("avg_price_per_night", "平均房价/晚", _mean(prices), unit="cny"),
            _metric("area_mismatch_runs", "区域疑似不一致", len(mismatched), unit="count"),
            _metric("avg_alternatives", "平均备选数", _mean([float(hotel.get("alternatives") or 0) for hotel in hotels]), unit="count"),
        ],
        findings=findings,
        note="到商圈 / 地铁 / 美食区的距离目前没有落库（预算内不做酒店周边检索），因此不在本页报",
    )


def _food_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    foods = [item["food"] for item in with_plan if item.get("food")]
    meal_quality = [
        float(food["meal_time_quality"])
        for food in foods
        if isinstance(food.get("meal_time_quality"), (int, float))
    ]
    real_ratio = [
        _rate(food["with_place"], food["count"]) for food in foods if food.get("count")
    ]
    evidence_ratio = [
        _rate(food["with_evidence"], food["count"]) for food in foods if food.get("count")
    ]
    detours = [
        float(food["avg_detour_meters"])
        for food in foods
        if isinstance(food.get("avg_detour_meters"), (int, float))
    ]
    empty_meals = [item for item in with_plan if not (item.get("food") or {}).get("count")]
    no_evidence = [
        item
        for item in with_plan
        if (item.get("food") or {}).get("count")
        and (item["food"]["with_evidence"] / item["food"]["count"]) < 0.5
    ]
    duplicates = [item for item in with_plan if (item.get("food") or {}).get("duplicate_names")]
    ad_risk = [item for item in with_plan if (item.get("food") or {}).get("high_ad_risk")]

    checks = [
        ("每天都有餐饮", None if not with_plan else not empty_meals),
        ("用餐落在午/晚窗口", None if not meal_quality else _mean(meal_quality) >= 0.6),
        ("餐厅有真实来源", None if not real_ratio else _mean(real_ratio) >= 0.8),
        ("证据覆盖过半", None if not evidence_ratio else _mean(evidence_ratio) >= 0.5),
        ("没有重复店名", None if not with_plan else not duplicates),
        ("没有高风险广告项", None if not with_plan else not ad_risk),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if empty_meals:
        findings.append({"level": "high", "text": f"{len(empty_meals)} 个运行一整天没有安排餐饮"})
    if no_evidence:
        findings.append(
            {"level": "medium", "text": f"{len(no_evidence)} 个运行的餐厅证据覆盖不足一半（攻略没提到）"}
        )
    if duplicates:
        findings.append({"level": "low", "text": f"{len(duplicates)} 个运行里出现了重复的餐饮店"})

    return _dimension(
        "food_quality",
        "美食质量",
        score=score,
        metrics=[
            _metric("avg_meal_time_quality", "用餐时段达标率", _mean(meal_quality), unit="ratio"),
            _metric("avg_real_restaurant_ratio", "带真实门店的比例", _mean(real_ratio), unit="ratio"),
            _metric("avg_evidence_ratio", "证据覆盖率", _mean(evidence_ratio), unit="ratio"),
            _metric("avg_detour_meters", "平均绕路距离", _mean(detours), unit="m"),
            _metric("runs_without_food", "没有餐饮安排的运行", len(empty_meals), unit="count"),
            _metric("runs_high_ad_risk", "含高风险广告项的运行", len(ad_risk), unit="count"),
        ],
        findings=findings,
        note="'真实餐厅比例'只判'有没有 place_id'；是否真的是餐厅由上游 place 归一负责",
    )


def _itinerary_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    itinerary = [item["itinerary"] for item in with_plan if item.get("itinerary")]
    stays = [
        float(entry["stays_per_day"])
        for entry in itinerary
        if isinstance(entry.get("stays_per_day"), (int, float))
    ]
    diversity = [
        float(entry["category_diversity"])
        for entry in itinerary
        if isinstance(entry.get("category_diversity"), (int, float))
    ]
    balance = [
        float(entry["daily_load_balance"])
        for entry in itinerary
        if isinstance(entry.get("daily_load_balance"), (int, float))
    ]
    no_area = [item for item in with_plan if (item.get("itinerary") or {}).get("days_without_area")]
    thin_days = [item for item in with_plan if (item.get("itinerary") or {}).get("max_stays_per_day", 0) > 6]
    must_missing = [item for item in with_plan if item.get("must_missing")]

    checks = [
        ("每天都有可玩内容", None if not with_plan else not thin_days),
        ("类型不太单调", None if not diversity else _mean(diversity) >= 0.6),
        ("每日负荷均衡", None if not balance else _mean(balance) >= 0.6),
        ("每天标注了区域", None if not with_plan else not no_area),
        ("MUST 全覆盖", None if not with_plan else not must_missing),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if no_area:
        findings.append({"level": "low", "text": f"{len(no_area)} 个运行有整天没标注区域，看不出动线是否集中"})
    if thin_days:
        findings.append({"level": "medium", "text": f"{len(thin_days)} 个运行某天塞了 7 个以上停留点"})

    return _dimension(
        "itinerary_quality",
        "行程质量",
        score=score,
        metrics=[
            _metric("avg_stays_per_day", "平均停留点/天", _mean(stays), unit="count"),
            _metric("avg_category_diversity", "类型多样性", _mean(diversity), unit="ratio"),
            _metric("avg_daily_load_balance", "每日负荷均衡", _mean(balance), unit="ratio"),
            _metric("runs_without_area", "有整天未标注区域", len(no_area), unit="count"),
            _metric("runs_overloaded", "单日超 6 个停留点", len(thin_days), unit="count"),
        ],
        findings=findings,
    )


def _route_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    routes = [item["route"] for item in with_plan if item.get("route")]
    verified = [
        float(entry["verified_rate"])
        for entry in routes
        if isinstance(entry.get("verified_rate"), (int, float))
    ]
    backtrack = [
        float(entry["backtracking_score"])
        for entry in routes
        if isinstance(entry.get("backtracking_score"), (int, float))
    ]
    distances = [
        float(entry["avg_distance_meters"])
        for entry in routes
        if isinstance(entry.get("avg_distance_meters"), (int, float))
    ]
    unverified = [item for item in with_plan if not (item.get("route") or {}).get("verified")]
    over_limit = [item for item in with_plan if (item.get("route") or {}).get("over_limit_legs")]

    checks = [
        ("路线经过核实", None if not verified else _mean(verified) >= 0.8),
        ("没有明显折返", None if not backtrack else _mean(backtrack) >= 0.7),
        ("没有超长单段", None if not with_plan else not over_limit),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if unverified:
        findings.append(
            {
                "level": "high" if len(unverified) >= 3 else "medium",
                "text": f"{len(unverified)} 个运行所有路段都没核实过（高德没返回路线，时间只是占位）",
            }
        )
    if over_limit:
        findings.append({"level": "medium", "text": f"{len(over_limit)} 个运行存在超过配置上限的单段通勤"})

    return _dimension(
        "route_quality",
        "路线质量",
        score=score,
        metrics=[
            _metric("avg_verified_rate", "路段核实率", _mean(verified), unit="ratio",
                    hint="verified=true 才是高德返回的真实路线"),
            _metric("avg_backtracking_score", "无折返得分", _mean(backtrack), unit="ratio"),
            _metric("avg_leg_distance", "平均单段距离", _mean(distances), unit="m"),
            _metric("runs_unverified", "完全未核实的运行", len(unverified), unit="count"),
            _metric("runs_over_limit_leg", "含超长单段的运行", len(over_limit), unit="count"),
        ],
        findings=findings,
    )


def _validation_dimension(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    with_plan = [item for item in samples if item.get("days")]
    validations = [item["validation"] for item in with_plan if item.get("validation")]
    hard = [item for item in with_plan if (item.get("validation") or {}).get("errors")]
    unresolved = [item for item in with_plan if (item.get("validation") or {}).get("unresolved")]
    conflicts = [
        item
        for item in with_plan
        if any(
            (item.get("validation") or {}).get(key)
            for key in ("return_conflicts", "transport_missing", "hotel_missing")
        )
    ]
    over_budget = [
        item
        for item in with_plan
        if isinstance(item.get("budget_total"), (int, float))
        and isinstance(item.get("budget_projected"), (int, float))
        and item["budget_projected"] > item["budget_total"]
    ]
    rejected_in_plan = [item for item in with_plan if item.get("rejected_in_plan")]

    checks = [
        ("没有硬错误", None if not with_plan else not hard),
        ("没有未解决告警", None if not with_plan else not unresolved),
        ("返程/接驳/住宿没有缺口", None if not with_plan else not conflicts),
        ("硬预算没超", None if not with_plan else not over_budget),
        ("REJECT 没有进计划", None if not with_plan else not rejected_in_plan),
    ]
    score = _checked_score(checks)

    findings: list[dict[str, Any]] = []
    if hard:
        findings.append({"level": "high", "text": f"{len(hard)} 个运行带着未解决的硬错误交付"})
    if conflicts:
        findings.append({"level": "high", "text": f"{len(conflicts)} 个运行的返程/接驳/住宿存在缺口"})
    if over_budget:
        findings.append({"level": "high", "text": f"{len(over_budget)} 个运行的预计花费超过用户预算"})
    if unresolved:
        findings.append({"level": "medium", "text": f"{len(unresolved)} 个运行有未在计划里解决的告警"})

    codes = Counter()
    for validation in validations:
        codes.update(validation.get("codes") or {})

    return _dimension(
        "hard_validation",
        "硬校验",
        score=score,
        metrics=[
            _metric("runs_with_errors", "有硬错误的运行", len(hard), unit="count"),
            _metric("runs_unresolved", "有未解决告警的运行", len(unresolved), unit="count"),
            _metric("runs_gap", "返程/接驳/住宿有缺口", len(conflicts), unit="count"),
            _metric("runs_over_budget", "超预算的运行", len(over_budget), unit="count"),
            _metric("top_codes", "告警码分布", dict(codes.most_common(8)), hint="FeasibilityIssue.code"),
        ],
        findings=findings,
        note="只看离开系统时仍然存在的问题：已 resolved 的告警不算问题",
    )


def travel_quality(
    store: TravelPlanStore, *, window: Window, limit: int = 30, destination: str | None = None
) -> dict[str, Any]:
    """行程质量页：六个维度 + 单 run 画像 + 目的地维度 + 质量趋势。"""

    notes: list[str] = []
    runs, truncated = _scan(
        lambda page_limit, offset: store.list_runs(
            limit=page_limit, offset=offset, include_benchmark=False
        ),
        since=window.since,
        key="created_at",
    )
    current, _ = _split_window(runs, key="created_at", window=window)
    if truncated:
        notes.append(f"窗口内运行数超过 {_MAX_SCAN_ROWS} 条，仅统计最近 {_MAX_SCAN_ROWS} 条")

    finished = [run for run in current if str(run.get("status")) in FINISHED_STATUSES]
    scored: list[dict[str, Any]] = []
    unscored = 0
    # 并发读计划与会话：这一步的数量级是"条数 × 1 秒"，串行做会把整个页面拖死。
    for context in run_contexts(store, finished[: max(1, min(limit, 100))]):
        if context.snapshot.plan is None:
            unscored += 1
            continue
        if destination and _destination_text(context.snapshot.plan.intent) != destination:
            continue
        scored.append(
            _run_facts(run=context.run, snapshot=context.snapshot, session=context.session)
        )

    if len(finished) > len(scored) + unscored:
        notes.append(f"为保证响应时间，只对最近 {len(scored) + unscored} 个已完成运行算了质量")
    if unscored:
        notes.append(f"{unscored} 个运行没有可解析的计划（历史 run 可能未落 plan.json）")
    if not scored:
        notes.append("窗口内没有可评估的运行：质量页需要已落库的计划")

    dimensions = [
        _preference_dimension(scored),
        _hotel_dimension(scored),
        _food_dimension(scored),
        _itinerary_dimension(scored),
        _route_dimension(scored),
        _validation_dimension(scored),
    ]
    scores = [float(item["score"]) for item in dimensions if item.get("score") is not None]
    overall = _mean(scores)

    # 目的地维度（P2）：按 plan.intent.destination 分组
    by_destination: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scored:
        if item.get("destination"):
            by_destination[str(item["destination"])].append(item)
    destinations = [
        {
            "name": name,
            "runs": len(items),
            "score": _mean([float(item["score"]) for item in items if item.get("score") is not None]),
            "grade": grade(_mean([float(item["score"]) for item in items if item.get("score") is not None])),
            "degraded": sum(1 for item in items if item.get("status") == "DEGRADED"),
            "failed": sum(1 for item in items if item.get("status") == "FAILED"),
        }
        for name, items in sorted(by_destination.items(), key=lambda pair: -len(pair[1]))
    ]

    return {
        "window": window.key,
        "window_label": window.label,
        "generated_at": now_iso(),
        "scan": {
            "runs_in_window": len(current),
            "finished": len(finished),
            "scored": len(scored),
            "unscored": unscored,
        },
        "overall": {
            "score": overall,
            "grade": grade(overall),
            "grade_label": GRADE_LABELS[grade(overall)],
            "weights": QUALITY_WEIGHTS,
            "note": "总分 = 六个维度得分的等权平均；单 run 分 = quality 软指标的加权和",
        },
        "dimensions": dimensions,
        "runs": [
            {
                "run_id": item["run_id"],
                "created_at": item["created_at"],
                "status": item["status"],
                "destination": item.get("destination"),
                "days": item.get("days"),
                "score": item.get("score"),
                "grade": item.get("grade"),
                "profile_source": item.get("profile_source"),
                "hotel_area": item.get("hotel_area_selected"),
                "preference_coverage": (item.get("quality") or {}).get("preference_coverage"),
                "warnings": (item.get("validation") or {}).get("warnings"),
                "errors": (item.get("validation") or {}).get("errors"),
                "route_verified_rate": (item.get("route") or {}).get("verified_rate"),
                "cost": item.get("cost"),
                "duration_ms": item.get("duration_ms"),
            }
            for item in sorted(scored, key=lambda entry: float(entry.get("score") or 0))
        ],
        "destinations": destinations,
        "quality_trend": _series(
            [{"created_at": item["created_at"], "score": item["score"]} for item in scored if item.get("created_at")],
            key="created_at",
            window=window,
            reduce=lambda rows: _mean([float(row["score"]) for row in rows if isinstance(row.get("score"), (int, float))]),
        ),
        "notes": notes,
    }


# ======================================================================
# Planning Funnel
# ======================================================================

#: 漏斗阶段：``(key, label)``。顺序即用户旅程的真实顺序，
#: 每个会话只算出一个"最远走到哪一步"的序号，再从序号反推各阶段人数 ——
#: 这样人数天然单调递减，不会出现"后一步比前一步还多"这种读不通的图。
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("session_created", "会话创建"),
    ("discovery_started", "Discovery 启动"),
    ("discovery_ready", "Discovery 完成"),
    ("preferences_done", "完成偏好"),
    ("poi_selected", "完成 POI 选择"),
    ("confirmed", "确认出行"),
    ("run_started", "开始规划"),
    ("plan_success", "成功生成"),
)

_DISCOVERY_STARTED_STATUSES = ("RUNNING", "READY", "PARTIAL", "FAILED")
_DISCOVERY_READY_STATUSES = ("READY", "PARTIAL")


def _session_stage_index(session: Mapping[str, Any], run_status: str | None) -> int:
    """会话走到第几步（0 = 只创建了会话）。"""

    events = [str((event or {}).get("event") or "") for event in (session.get("events") or [])]
    discovery_status = str(session.get("discovery_status") or "")
    preferences = session.get("preferences") or {}
    selections = session.get("poi_selections") or {}
    status = str(session.get("status") or "")
    stage = 0

    if discovery_status in _DISCOVERY_STARTED_STATUSES or any(name.endswith("_started") for name in events):
        stage = max(stage, 1)
    if discovery_status in _DISCOVERY_READY_STATUSES or "discovery_finished" in events:
        stage = max(stage, 2)
    if (
        "user_preferences_updated" in events
        or any(preferences.get(key) for key in ("transport_priority", "hotel_priority", "pace", "transport_mode"))
    ):
        stage = max(stage, 3)
    if selections:
        stage = max(stage, 4)
    if "session_confirmed" in events or status == "STARTING":
        stage = max(stage, 5)
    if session.get("run_id") or "run_started" in events:
        stage = max(stage, 6)
    if run_status in ("SUCCESS", "DEGRADED"):
        stage = max(stage, 7)
    return stage


def funnel(
    store: TravelPlanStore, *, window: Window, dimension: str = "destination"
) -> dict[str, Any]:
    """引导式会话漏斗：每一步还剩多少人、最大流失在哪、按维度看差异。"""

    notes: list[str] = []
    # 会话与运行两张表互不依赖，一起发出去：经 Neon 的每次往返约 1 秒，
    # 串行做等于把响应时间白送一倍。
    gathered, problems = _gather(
        {
            "sessions": lambda: _scan(
                lambda limit, offset: store.list_planning_sessions(limit=limit, offset=offset)[0],
                since=window.since,
                key="created_at",
            ),
            "runs": lambda: _scan(
                lambda limit, offset: store.list_runs(limit=limit, offset=offset, include_benchmark=False),
                since=window.prev_since,
                key="created_at",
            ),
        }
    )
    notes.extend(problems)
    sessions, truncated = gathered["sessions"] or ([], False)
    current, previous = _split_window(sessions, key="created_at", window=window)
    if truncated:
        notes.append(f"窗口内会话数超过 {_MAX_SCAN_ROWS} 条，仅统计最近 {_MAX_SCAN_ROWS} 条")

    # run 状态表：把窗口内的运行一次性读出来，避免每个会话再查一次库。
    runs, _ = gathered["runs"] or ([], False)
    run_status = {str(run["run_id"]): str(run.get("status") or "") for run in runs}

    def stage_counts(items: Sequence[Mapping[str, Any]]) -> list[int]:
        ordinals = [_session_stage_index(item, run_status.get(str(item.get("run_id")))) for item in items]
        return [sum(1 for value in ordinals if value >= index) for index in range(len(FUNNEL_STAGES))]

    counts = stage_counts(current)
    previous_counts = stage_counts(previous)
    stages = []
    for index, (key, label) in enumerate(FUNNEL_STAGES):
        dropped = counts[index - 1] - counts[index] if index else 0
        stages.append(
            {
                "key": key,
                "label": label,
                "count": counts[index],
                "rate_of_total": _rate(counts[index], counts[0]),
                "dropped": dropped,
                "drop_rate": _rate(dropped, counts[index - 1]) if index else None,
                "previous_count": previous_counts[index],
                "previous_dropped": previous_counts[index - 1] - previous_counts[index] if index else 0,
            }
        )

    biggest = None
    for index in range(1, len(stages)):
        drop = stages[index]["dropped"]
        if not stages[index - 1]["count"]:
            continue
        rate = drop / stages[index - 1]["count"]
        if biggest is None or rate > biggest["drop_rate"]:
            biggest = {
                "from": stages[index - 1]["label"],
                "to": stages[index]["label"],
                "from_key": stages[index - 1]["key"],
                "to_key": stages[index]["key"],
                "drop_rate": round(rate, 4),
                "dropped": drop,
            }

    # 维度拆解：只给要的维度算，避免每个请求都做全量交叉。
    dimensions: dict[str, list[dict[str, Any]]] = {}
    getters: dict[str, Callable[[Mapping[str, Any]], str | None]] = {
        "destination": lambda item: _basic(item).get("destination"),
        "date": lambda item: _iso(item.get("created_at"))[:10] if item.get("created_at") else None,
        "pace": lambda item: (item.get("preferences") or {}).get("pace"),
        "transport_priority": lambda item: (item.get("preferences") or {}).get("transport_priority"),
        "hotel_priority": lambda item: (item.get("preferences") or {}).get("hotel_priority"),
        "session_status": lambda item: str(item.get("status") or "UNKNOWN"),
    }
    if dimension in getters:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in current:
            key = getters[dimension](item)
            if key:
                grouped[str(key)].append(item)
        rows = []
        for name, items in sorted(grouped.items(), key=lambda pair: -len(pair[1]))[:20]:
            group_counts = stage_counts(items)
            rows.append(
                {
                    "key": name,
                    "sessions": len(items),
                    "stages": group_counts,
                    "plan_success": group_counts[-1],
                    "conversion": _rate(group_counts[-1], group_counts[0]),
                }
            )
        dimensions[dimension] = rows
    else:
        notes.append(f"不支持的维度 {dimension!r}；可选 {sorted(getters)}")

    return {
        "window": window.key,
        "window_label": window.label,
        "generated_at": now_iso(),
        "stages": stages,
        "sessions": len(current),
        "previous_sessions": len(previous),
        "overall_conversion": _rate(counts[-1], counts[0]),
        "biggest_drop": biggest,
        "destination_dimension": dimension,
        "breakdown": dimensions,
        "dimension_options": [
            {"key": "destination", "label": "目的地"},
            {"key": "date", "label": "日期"},
            {"key": "pace", "label": "节奏偏好"},
            {"key": "transport_priority", "label": "交通偏好"},
            {"key": "hotel_priority", "label": "住宿偏好"},
            {"key": "session_status", "label": "会话状态"},
        ],
        "notes": notes
        + [
            "设备维度暂无数据来源：会话表没有记录 UA / 端类型，不编造",
            "阶段判定依据是会话事件流（session_events），不是估算",
        ],
    }


def _basic(session: Mapping[str, Any]) -> dict[str, Any]:
    basic = session.get("basic_intent")
    return basic if isinstance(basic, dict) else {}


# ======================================================================
# Bad Case 聚类（问题中心）
# ======================================================================

#: category → 该找人修哪个模块。只覆盖 badcase.py 会产出的那几类；
#: 没登记的类别返回 None（而不是编一个模块名）。
_CATEGORY_MODULES: dict[str, str] = {
    "provider_failure": "providers",
    "degradation": "providers",
    "hard_constraint": "planner.feasibility",
    "budget": "planner.budget",
    "budget_math": "planner.budget",
    "evidence_gap": "evidence",
    "missing_disclosure": "planner.finalize",
    "hallucinated_price": "planner.budget",
    "route_unverified": "planner.routes",
    "discovery_empty": "discovery",
    "discovery_stale": "discovery.city_cache",
    "prefetch_failed": "discovery.prefetch",
    "prefetch_not_reused": "workflow.prefetch",
    "must_poi_missing": "planner.selection",
    "rejected_poi_in_plan": "planner.selection",
    "user_preference_ignored": "planner.selection",
    "guided_intent_mismatch": "sessions",
}

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _provider_from_refs(refs: Iterable[str]) -> list[str]:
    """从 trace_refs 里抠出涉及的 Provider。

    ref 形如 ``{run_id}:provider:{provider}:{tool}`` —— 这是 badcase.py 自己写下的
    稳定格式，不是这里猜的。
    """

    names = set()
    for ref in refs:
        parts = str(ref).split(":")
        if len(parts) >= 4 and parts[1] == "provider":
            names.add(parts[2])
    return sorted(names)


def badcase_groups(store: TravelPlanStore, *, window: Window, limit: int = 20) -> dict[str, Any]:
    """把 Bad Case 按 category 聚成"要解决的问题"。"""

    gathered, problems = _gather(
        {
            "cases": lambda: _scan(
                lambda limit_, offset: store.list_badcases(limit=limit_, offset=offset)[0],
                since=window.prev_since,
                key="created_at",
            ),
            "open_total": lambda: store.count_badcases(analysis_status="pending"),
            "facets": store.badcase_facets,
        }
    )
    cases, truncated = gathered["cases"] or ([], False)
    current, previous = _split_window(cases, key="created_at", window=window)
    notes: list[str] = list(problems)
    if truncated:
        notes.append(f"Bad Case 数超过 {_MAX_SCAN_ROWS} 条，仅统计最近 {_MAX_SCAN_ROWS} 条")

    prev_counter = Counter(str(case.get("category")) for case in previous)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in current:
        grouped[str(case.get("category"))].append(case)

    groups = []
    for category, items in grouped.items():
        runs = {str(case.get("run_id")) for case in items}
        stamps = [stamp for stamp in (_parse_ts(case.get("created_at")) for case in items) if stamp]
        severity = min(
            (str(case.get("severity") or "medium") for case in items),
            key=lambda value: _SEVERITY_ORDER.get(value, 3),
        )
        baseline = prev_counter.get(category, 0)
        trend = None
        if baseline:
            trend = round((len(items) - baseline) / baseline, 4)
        symptoms = Counter(str(case.get("symptom") or "") for case in items)
        groups.append(
            {
                "key": category,
                "category": category,
                "severity": severity,
                "occurrence_count": len(items),
                "previous_count": baseline,
                "trend_pct": trend,
                "affected_runs": len(runs),
                "first_seen": min(stamps).isoformat() if stamps else None,
                "last_seen": max(stamps).isoformat() if stamps else None,
                "last_seen_ago_seconds": int((window.until - max(stamps)).total_seconds()) if stamps else None,
                "related_module": _CATEGORY_MODULES.get(category),
                "related_providers": _provider_from_refs(
                    ref for case in items for ref in (case.get("trace_refs") or [])
                ),
                "pending_analysis": sum(1 for case in items if str(case.get("analysis_status")) == "pending"),
                "unfixed": sum(1 for case in items if str(case.get("fixed_status")) == "unfixed"),
                "top_symptoms": [
                    {"text": text, "count": count} for text, count in symptoms.most_common(3) if text
                ],
                "sample_ids": [str(case.get("badcase_id")) for case in items[:5]],
                "sample_runs": sorted(runs)[:5],
                "sparkline": _series(
                    items,
                    key="created_at",
                    window=window,
                    reduce=lambda rows: float(len(rows)),
                )["points"],
            }
        )

    groups.sort(key=lambda group: (-group["occurrence_count"], _SEVERITY_ORDER.get(group["severity"], 3)))
    return {
        "window": window.key,
        "window_label": window.label,
        "generated_at": now_iso(),
        "totals": {
            "badcases": len(current),
            "previous_badcases": len(previous),
            "groups": len(groups),
            "affected_runs": len({str(case.get("run_id")) for case in current}),
            "pending": sum(1 for case in current if str(case.get("analysis_status")) == "pending"),
            "open_total": gathered["open_total"] or 0,
        },
        "groups": groups[: max(1, min(limit, 100))],
        "facets": gathered["facets"] or {"category": {}, "severity": {}, "analysis_status": {}},
        "notes": notes,
    }


# ======================================================================
# Run 列表的质量补充
# ======================================================================


def enrich_runs(store: TravelPlanStore, runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """给运行列表补上"质量分 / 问题数"两列。

    列表里只保留**列表能回答的问题**（这次行程好不好、有几个问题），
    token / LLM / 工具调用这些技术指标下沉到详情页 —— 一屏塞 12 列没人扫得动。
    """

    enriched: list[dict[str, Any]] = []
    for context in run_contexts(store, list(runs)):
        run = context.run
        snapshot = context.snapshot
        view = dict(run)
        view["created_at"] = _iso(run.get("created_at"))
        view["started_at"] = _iso(run.get("started_at"))
        view["finished_at"] = _iso(run.get("finished_at"))
        view["quality_score"] = snapshot.score
        view["quality_grade"] = grade(snapshot.score)
        view["quality_error"] = snapshot.error
        view["issue_count"] = int(run.get("badcase_count") or 0)
        view["destination"] = _destination_text(snapshot.plan.intent) if snapshot.plan else None
        view["warnings"] = (
            len([issue for issue in snapshot.plan.warnings or [] if not issue.resolved])
            if snapshot.plan
            else None
        )
        enriched.append(view)
    return enriched


# ======================================================================
# 路由
# ======================================================================


def build_admin_router(
    get_store: Callable[[], TravelPlanStore], require_admin: Callable[..., None]
) -> APIRouter:
    """管理端聚合路由。

    依赖以参数注入而不是 `from .api import ...`：`api.py` 反过来要 import 本模块，
    直接互相 import 会成环。顺带让本模块可以被单测独立装配，不必拉起整个 FastAPI 应用。
    """

    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin)])

    @router.get("/dashboard")
    def admin_dashboard(
        window: str = Query(default=DEFAULT_WINDOW, pattern=WINDOW_PATTERN),
        quality_sample: int = Query(default=10, ge=0, le=50),
    ) -> dict[str, Any]:
        """首屏：关键指标 + 环比 + 趋势 + 当前需要关注。"""

        return dashboard(get_store(), window=_window(window), quality_sample=quality_sample)

    @router.get("/travel-quality")
    def admin_travel_quality(
        window: str = Query(default="7d", pattern=WINDOW_PATTERN),
        limit: int = Query(default=30, ge=1, le=100),
        destination: str | None = None,
    ) -> dict[str, Any]:
        """行程质量：偏好 / 住宿 / 美食 / 行程 / 路线 / 硬校验 六个维度。"""

        return travel_quality(
            get_store(), window=_window(window), limit=limit, destination=destination
        )

    @router.get("/funnel")
    def admin_funnel(
        window: str = Query(default="7d", pattern=WINDOW_PATTERN),
        dimension: str = Query(default="destination"),
    ) -> dict[str, Any]:
        """引导式会话漏斗。"""

        return funnel(get_store(), window=_window(window), dimension=dimension)

    @router.get("/badcase-groups")
    def admin_badcase_groups(
        window: str = Query(default="7d", pattern=WINDOW_PATTERN),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Bad Case 聚类：100 条案例聚成几个要解决的问题。"""

        return badcase_groups(get_store(), window=_window(window), limit=limit)

    return router
