"""性能优化的关键不变量（性能任务 Part A / C / H）。

这些用例只锁"并发不能让系统变得不可信"的部分 —— 并发本身快不快由
`scripts/e2e_real.py` 的真实 run 证明，这里保证它没有偷偷破坏：

  1. 结果顺序与任务定义顺序一致（否则同一份输入会产出不同行程）；
  2. 并发上限真的被遵守（否则"限流"只是文案）；
  3. 异常仍然按串行语义抛出（第一个出错的先炸，不随线程调度变化）；
  4. 子 span 的时间戳来自真实调用时刻，不是落盘时刻。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.observability import finish_iso
from app.workflow import _run_parallel


class TestRunParallel:
    def test_preserves_definition_order_even_when_finish_order_differs(self) -> None:
        """先提交的慢、后提交的快 —— 结果仍必须按定义顺序排列。"""

        def slow() -> str:
            time.sleep(0.12)
            return "slow"

        def fast() -> str:
            time.sleep(0.01)
            return "fast"

        results = _run_parallel([("a", slow), ("b", fast), ("c", fast)], limit=3)

        assert list(results) == ["a", "b", "c"]
        assert results == {"a": "slow", "b": "fast", "c": "fast"}

    def test_never_exceeds_the_cap(self) -> None:
        """并发上限来自 config，必须真的生效：峰值并发不能超过 limit。"""

        lock = threading.Lock()
        active = 0
        peak = 0

        def task() -> int:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return 1

        tasks = [(f"t{i}", task) for i in range(12)]
        _run_parallel(tasks, limit=3)

        assert peak <= 3, f"并发峰值 {peak} 超过了上限 3"

    def test_limit_one_degrades_to_serial(self) -> None:
        """上限=1 时退化成纯串行：测试与保守部署都拿它当开关。"""

        lock = threading.Lock()
        active = 0
        peak = 0

        def task() -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

        _run_parallel([("a", task), ("b", task), ("c", task)], limit=1)

        assert peak == 1

    def test_raises_the_first_failure_in_definition_order(self) -> None:
        """两个任务都失败时，抛的是定义顺序里靠前的那个 —— 与串行版本一致。"""

        def boom_a() -> None:
            raise ValueError("A")

        def boom_b() -> None:
            raise RuntimeError("B")

        with pytest.raises(ValueError, match="A"):
            _run_parallel([("a", boom_a), ("b", boom_b)], limit=2)

    def test_empty_task_list_is_cheap_and_empty(self) -> None:
        assert _run_parallel([], limit=4) == {}


class TestSpanTimestamps:
    """子 span 的时间戳必须来自真实调用时刻。

    修之前 provider/tool/llm 子 span 都拿"落盘时刻"当 started_at/finished_at，
    于是每个 Provider 看起来都跑了 280~400s，时间先后完全不可信。
    """

    def test_finish_iso_uses_duration_not_wall_clock(self) -> None:
        started = "2026-09-20T10:00:00+00:00"
        assert finish_iso(started, 3000) == "2026-09-20T10:00:03+00:00"

    def test_finish_iso_never_returns_a_later_than_expected_window(self) -> None:
        """并行调用下，单条 tool span 的窗口必须远小于整段墙钟。"""

        started = "2026-09-20T10:00:00+00:00"
        # 一次 8s 的调用，不该因为"等到 run 结束才落盘"而变成几百秒。
        finished = finish_iso(started, 8000)
        assert finished == "2026-09-20T10:00:08+00:00"

    def test_finish_iso_falls_back_to_now_only_when_duration_is_unknown(self) -> None:
        """真的拿不到 duration 时才退回"当前时刻"，且窗口必须仍然合法（finish >= start）。

        这条兜底是刻意的：宁可在甘特图上画一个偏长的条，也不要让 span 的终点早于起点
        （那样管理端会显示负耗时）。但它只在 Provider 没上报 duration 时触发 ——
        正常路径必须走 §`test_finish_iso_uses_duration_not_wall_clock` 的推算。
        """

        from datetime import datetime

        started = "2026-09-20T10:00:00+00:00"
        finished = finish_iso(started, None)
        assert datetime.fromisoformat(finished) >= datetime.fromisoformat(started)


class TestCapsAreConfigDriven:
    """并发与数量上限必须集中在 app/config.py，节点里不许写死。"""

    def test_caps_exist_and_are_sane(self) -> None:
        from app.config import current_config

        config = current_config()
        assert config.provider_max_concurrency >= 1
        assert config.poi_verify_max_concurrency >= 1
        assert config.route_max_concurrency >= 1
        assert config.max_route_lookups >= 1
        assert config.planner_poi_limit >= 1
        assert 1 <= config.user_visible_poi_limit <= config.discovery_max_places

    def test_provider_timeouts_are_per_provider_and_bounded(self) -> None:
        from app.config import provider_timeouts

        timeouts = provider_timeouts()
        assert timeouts, "至少要有一份 Provider 超时预算"
        assert all(value > 0 for value in timeouts.values())
        # 不要求每个 Provider 都不一样，但绝不能是"所有 Provider 一个值"的偷懒做法：
        # 至少有两个不同的预算，说明它们是分别调过的。
        assert len(set(timeouts.values())) >= 2
