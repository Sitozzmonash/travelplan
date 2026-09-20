"""固定 Workflow 的运行状态、轻量 Trace 契约，以及性能优化的公共原语。

SuperHarness 仍是 Runtime 级 EventBus/Trace 的唯一所有者；这里仅定义 TravelPlan
业务层需要持久化、供轮询 API 与管理端消费的 Run/Stage 事实，绝不复制工具执行器。

除状态与契约外，本模块还提供**性能优化（Part A / C / E）唯一一份公共实现**：

  * ``parallel_map``   —— 受控并发 + **与输入同序**的结果（可重复的行程从可重复的
    合并顺序开始；谁先返回不参与决策）；
  * ``call_with_deadline`` —— 单个阻塞调用的兜底超时（守护线程，到点放弃等待，不抛异常）；
  * ``CallLedger``     —— 一次 run 内的查询键复用账本与 Trace 标记。

为什么放在这里而不是各节点自己写一份：这三个东西一旦各写一份，"并发上限"
"超时到底算不算超时""同一个查询到底查了几次"就会各说各话，审计也就失去意义。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, TypeVar

_T = TypeVar("_T")


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"
    CANCELLED = "CANCELLED"


class StageStatus(StrEnum):
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"


class SpanKind(StrEnum):
    """Trace span 的组件分类（管理端按它分组展示与聚合）。

    §5 要求 Trace 至少覆盖这些类别。分类的意义是：看到一次 run 变慢或变差时，
    维护者能先定位到"是 Provider 慢"还是"Jev 拒绝"还是"Planner 反复重排"，
    而不是在一堆同名 span 里翻。
    """

    WORKFLOW = "workflow"
    LLM = "llm"
    JEV = "jev"
    PROVIDER = "provider"
    TOOL = "tool"
    MCP = "mcp"
    PLANNER = "planner"
    BUDGET = "budget"
    FEASIBILITY = "feasibility"
    CRITIC = "critic"
    STORE = "store"


def span_id(run_id: str, component: str | SpanKind, name: str, *, suffix: str | None = None) -> str:
    """span_id 必须稳定且唯一：同一 run 下 (component, name) 可能重复出现。

    ``suffix`` 供调用方在确实会重复的场景（例如多个 Jev 问题）上区分实例；
    不传时退化为 ``run:component:name``，与既有 workflow span 的命名保持一致。
    """

    tail = f":{suffix}" if suffix else ""
    return f"{run_id}:{component}:{name}{tail}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_run_status(legacy_status: str, *, degraded: bool = False) -> RunStatus:
    """把旧 Workflow 返回值映射到对外稳定的状态模型。"""

    if legacy_status == "completed":
        return RunStatus.DEGRADED if degraded else RunStatus.SUCCESS
    if legacy_status == "cancelled":
        return RunStatus.CANCELLED
    if legacy_status == "running":
        return RunStatus.RUNNING
    return RunStatus.FAILED


@dataclass(slots=True)
class StageProgress:
    stage_id: str
    status: StageStatus
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# 时间窗：span 的 started_at / finished_at 必须由 duration 推出来
# ======================================================================


def finish_iso(started_at: str | None, duration_ms: int | float | None) -> str:
    """由 ``started_at + duration_ms`` 算出结束时刻。

    为什么不能用 ``now_iso()``：Provider 与模型的调用账本是**事后**统一转录成 span 的
    （见 ``workflow._emit_run_spans``），落盘时刻比真实调用时刻晚几十秒到几分钟。
    用落盘时刻当 ``finished_at``，每一条子 span 看上去都"跑到 run 结束"，
    管理端的时间轴与甘特图全部失真。这里按真实耗时反推，缺 duration 时才退回当前时刻。
    """

    if started_at is None:
        return now_iso()
    if duration_ms is None:
        return now_iso()
    try:
        started = datetime.fromisoformat(str(started_at))
    except ValueError:
        return now_iso()
    return (started + timedelta(milliseconds=max(0.0, float(duration_ms)))).isoformat()


def start_iso(finished_at: str | None, duration_ms: int | float | None) -> str:
    """由 ``finished_at - duration_ms`` 反推开始时刻（账本只记了完成时刻时用）。"""

    if finished_at is None:
        return now_iso()
    if duration_ms is None:
        return str(finished_at)
    try:
        finished = datetime.fromisoformat(str(finished_at))
    except ValueError:
        return str(finished_at)
    return (finished - timedelta(milliseconds=max(0.0, float(duration_ms)))).isoformat()


# ======================================================================
# 受控并发（Part A）：结果与输入同序，绝不无限并发打 Provider
# ======================================================================


@dataclass(slots=True)
class CallOutcome:
    """一次并发调用的结果。``ok=False`` 时 ``error`` 一定非空，``value`` 一定是 None。

    为什么要一个包装而不是直接返回裸值：worker 里的异常必须变成**数据**（一条降级记录），
    否则"某个 Provider 炸了"会以异常的形式穿透到整个节点，把一趟本来可以降级跑完的
    规划变成失败 run。
    """

    ok: bool
    value: Any = None
    error: str | None = None

    def __bool__(self) -> bool:  # 兼容 `if outcome:` 的写法
        return self.ok


def parallel_map(
    tasks: Sequence[Callable[[], Any]],
    *,
    max_workers: int,
    thread_prefix: str = "tp-parallel",
) -> list[CallOutcome]:
    """受控并发跑一组**互不依赖**的调用，返回与输入**同序**的结果列表。

    三条硬性约定：
      1. 并发上限由 ``max_workers`` 决定（来自 ``app/config.py``，节点不许写死）；
      2. 结果顺序 == 输入顺序。谁先返回不参与任何决策，所以同一份输入永远得到同一份输出；
      3. worker 抛异常 → 那一条变成 ``CallOutcome(ok=False, error=...)``，其余照常返回。

    绝不用它跑有依赖关系的链（攻略 → 抽取 → 验证）：并发化依赖链只会制造竞态。
    """

    if not tasks:
        return []
    workers = max(1, min(int(max_workers), len(tasks)))
    if workers == 1:
        # 单并发时不建线程池：保持与串行完全一致的调用线程（测试里最容易复现）。
        outcomes: list[CallOutcome] = []
        for task in tasks:
            try:
                outcomes.append(CallOutcome(ok=True, value=task()))
            except Exception as exc:  # noqa: BLE001 —— 失败要变成数据，不是异常
                outcomes.append(CallOutcome(ok=False, error=f"{type(exc).__name__}: {exc}"))
        return outcomes

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_prefix) as pool:
        futures = [pool.submit(_safe_call, task) for task in tasks]
        # 按提交顺序取结果：顺序由输入决定，不受完成先后影响。
        return [future.result() for future in futures]


def _safe_call(task: Callable[[], Any]) -> CallOutcome:
    try:
        return CallOutcome(ok=True, value=task())
    except Exception as exc:  # noqa: BLE001
        return CallOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")


def call_with_deadline(func: Callable[[], _T], *, timeout: float, label: str) -> tuple[_T | None, bool, str | None]:
    """给一个**签名里没有超时**的阻塞调用加兜底预算，返回 ``(结果, 是否超时, 错误)``。

    为什么用裸守护线程而不是线程池：线程池的 worker 被一个卡死的调用占满之后，排队中的
    调用会把**排队时间**算进自己的超时，于是出现"没人查它却报超时"的假故障；退出时
    ``shutdown(wait=True)`` 还会把节点重新卡回卡死的那一次调用上。

    超时**不抛异常**，也不返回任何编造的结果：调用方必须走自己的 fallback（换源 /
    披露 / 用规则估算并标 verified=False）。被放弃的那次调用仍会在 Provider 自己的
    账本里留下记录 —— 它确实发生过，这一点不能抹掉。
    """

    budget = max(0.0, float(timeout))
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 —— 原样带回给调用方判定
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name=f"tp-deadline-{label}", daemon=True)
    thread.start()
    thread.join(budget)
    if thread.is_alive():
        return None, True, f"{label} 超过 {budget:g}s 未返回，本次调用按 TIMEOUT 处理"
    if "error" in outcome:
        exc = outcome["error"]
        return None, False, f"{type(exc).__name__}: {exc}"
    return outcome.get("value"), False, None


# ======================================================================
# 查询键复用账本（Part C）：同一个键在一次 Session / Run 内只打一次 Provider
# ======================================================================

#: Trace / Audit 里能直接看到的性能标记词表。管理端按这些字符串做聚合，
#: 所以它们必须集中在一个地方定义（而不是各节点各写一个同义词）。
MARK_CACHE_HIT = "cache_hit"
MARK_PREFETCH_REUSED = "prefetch_reused"
MARK_PROVIDER_CALLED = "provider_called"
MARK_FALLBACK_QUERY = "fallback_query"
MARK_DUPLICATE_QUERY = "duplicate_query"
MARK_PROVIDER_TIMEOUT = "provider_timeout"
MARK_ROUTE_REQUESTED = "route_requested"
MARK_ROUTE_CACHE_HIT = "route_cache_hit"
MARK_ROUTE_SKIPPED = "route_skipped"


def query_key(
    provider: str,
    tool: str,
    *,
    origin: str | None = None,
    destination: str | None = None,
    date: str | None = None,
    query: str | None = None,
    page: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """稳定的查询键：``provider + tool + origin + destination + date + query + page``。

    字段固定、顺序固定、缺失即空 —— 同一个请求在任何调用点都得到同一个键，
    这样"首页查过一次、正式 Workflow 又查一次"才可能被真正挡住。
    额外参数走 ``extra``（按键排序），不影响前七个字段的可读性。
    """

    parts = [
        f"provider={provider or '?'}",
        f"tool={tool or '?'}",
        f"origin={origin or ''}",
        f"destination={destination or ''}",
        f"date={date or ''}",
        f"query={query or ''}",
        f"page={'' if page is None else page}",
    ]
    for name in sorted(extra or {}):
        parts.append(f"{name}={extra[name]}")
    return "|".join(parts)


class CallLedger:
    """一次 run（或一次 Planning Session）的查询键复用账本。

    它回答三个问题，全部是**事实**而非估计：
      * 哪个查询键已经查到过结果 → 直接复用，不再打 Provider（``cache_hit``）；
      * 哪些结果是 Discovery 阶段查好、本次 run 直接用的（``prefetch_reused``）；
      * 哪些线是本次补查的（``provider_called``）或走了 fallback（``fallback_query``）。

    线程安全：受控并发下 worker 只做 Provider 调用，账本由父线程写；但这里仍加锁，
    因为"少一次加锁"换不来任何东西，而一次竞态就能让计数在审计里对不上。
    """

    def __init__(self, *, scope: str = "run") -> None:
        self.scope = scope
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._satisfied: dict[str, str] = {}
        self.markers: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {
            MARK_CACHE_HIT: 0,
            MARK_PREFETCH_REUSED: 0,
            MARK_PROVIDER_CALLED: 0,
            MARK_FALLBACK_QUERY: 0,
            MARK_DUPLICATE_QUERY: 0,
            MARK_PROVIDER_TIMEOUT: 0,
            MARK_ROUTE_REQUESTED: 0,
            MARK_ROUTE_CACHE_HIT: 0,
            MARK_ROUTE_SKIPPED: 0,
        }

    # --- 记账 ---

    def mark(self, key: str, marker: str, detail: str = "") -> None:
        with self._lock:
            self.counts[marker] = self.counts.get(marker, 0) + 1
            self.markers.append({"key": key, "marker": marker, "detail": detail})

    def remember(self, key: str, value: Any, *, marker: str, detail: str = "") -> None:
        """记下"这个键已经有结果了"。

        调用方有两种用法：Provider 刚返回（``provider_called``），或复用了 Discovery
        已经查好的数据（``prefetch_reused``）。两种都会让后续同一个键直接命中。
        """

        with self._lock:
            self._values[key] = value
            self._satisfied[key] = marker
            self.counts[marker] = self.counts.get(marker, 0) + 1
            self.markers.append({"key": key, "marker": marker, "detail": detail})

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._values or key in self._satisfied

    def fetch(self, key: str, func: Callable[[], _T], *, detail: str = "") -> tuple[_T, bool]:
        """取结果：命中就复用，没命中就调 ``func`` 并记下结果。

        返回 ``(结果, 是否命中复用)``。命中复用不消耗任何 Provider 配额 —— 这正是
        "同一个 Planning Session / Run 内同一个查询键不许打第二次"的实现方式。
        """

        with self._lock:
            if key in self._values:
                self.counts[MARK_CACHE_HIT] = self.counts.get(MARK_CACHE_HIT, 0) + 1
                self.markers.append({"key": key, "marker": MARK_CACHE_HIT, "detail": detail})
                return self._values[key], True
            known = self._satisfied.get(key)

        if known is not None and known != MARK_PROVIDER_CALLED:
            # 键在 Discovery 阶段被满足过，但载荷没有随会话传过来。**绝不**编一个结果，
            # 也绝不假装查过了：照常调用，同时把"本不该再查一次"记成重复查询暴露出来。
            self.mark(key, MARK_DUPLICATE_QUERY, detail or known)
        value = func()
        self.remember(key, value, marker=MARK_PROVIDER_CALLED, detail=detail)
        return value, False

    def seed(self, entries: Iterable[Mapping[str, Any]]) -> int:
        """把 Discovery 的 Provider 调用账本播种成"这些键已经满足过"。

        只播种**标记**，不播种载荷：载荷在 PrefetchBundle 里已经由各节点按类型复用，
        这里负责的是"别再打第二次"这件事与可审计的计数。
        """

        seeded = 0
        for entry in entries or []:
            provider = str(entry.get("provider") or "?")
            tool = str(entry.get("tool") or "?")
            args = dict(entry.get("arguments") or entry.get("query") or {})
            key = _key_from_audit(provider, tool, args)
            with self._lock:
                if key in self._satisfied:
                    continue
                self._satisfied[key] = MARK_PREFETCH_REUSED
            seeded += 1
        if seeded:
            self.mark("discovery", MARK_PREFETCH_REUSED, f"Discovery 已完成 {seeded} 次调用，本次 run 复用")
        return seeded

    # --- 汇总 ---

    def summary(self) -> dict[str, int]:
        with self._lock:
            return {marker: count for marker, count in self.counts.items() if count}

    def top_markers(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            return self.markers[:limit]


def _key_from_audit(provider: str, tool: str, args: Mapping[str, Any]) -> str:
    """从 Provider 账本的原始参数反推查询键（Discovery 的账本没有统一的键形状）。

    参数名在各 Provider 之间不统一（``from`` / ``departure_city`` / ``keywords``…），
    所以这里把可识别的字段映射到键的七个固定位置，其余进 ``extra``。
    """

    def pick(*names: str) -> str | None:
        for name in names:
            value = args.get(name)
            if value not in (None, ""):
                return str(value)
        return None

    origin = pick("from", "fromStation", "departure_city", "origin", "city")
    destination = pick("to", "toStation", "arrival_city", "destination", "region")
    date = pick("date", "departure_date", "check_in")
    query = pick("keyword", "keywords", "query", "scenic_name", "poi_id", "address")
    page = args.get("page") or args.get("page_num")
    known = {
        "from", "fromStation", "departure_city", "origin", "city",
        "to", "toStation", "arrival_city", "destination", "region",
        "date", "departure_date", "check_in", "keyword", "keywords", "query",
        "scenic_name", "poi_id", "address", "page", "page_num", "timeout",
    }
    extra = {key: value for key, value in args.items() if key not in known}
    return query_key(
        provider,
        tool,
        origin=origin,
        destination=destination,
        date=date,
        query=query,
        page=int(page) if isinstance(page, (int, float, str)) and str(page).isdigit() else None,
        extra=extra,
    )
