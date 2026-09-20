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


class TestToolLevelBudgets:
    """同一个 Provider 下不同 Tool 的预算必须能分别设（门票 vs 拉列表差一个量级）。"""

    def test_ticket_budget_is_tighter_than_the_provider_budget(self) -> None:
        from app.config import current_config, tool_timeout

        config = current_config()
        override = tool_timeout("tuniu", "tuniu_search_scenic_tickets")
        assert override is not None, "门票必须有自己的预算，否则一次挂死就独占整次 run"
        assert override == config.ticket_timeout_seconds
        # 它必须**更紧**：共用途牛那份预算就等于没设。
        assert override < config.tuniu_timeout_seconds

    def test_tools_without_an_override_report_none(self) -> None:
        from app.config import tool_timeout

        # 返回 None（而不是悄悄回退成 Provider 预算）是有意的：让调用方明确表达
        # "这次没有 Tool 级覆盖"，避免两处各自回退、把"实际用了哪个值"埋掉。
        assert tool_timeout("tuniu", "tuniu_search_hotels") is None
        assert tool_timeout("amap", "search_poi") is None


class TestPerTagLlmBudgets:
    """模型预算按用途收紧，但**绝不能**超过调用方给的硬上限。"""

    def test_tag_budget_tightens_below_the_cap(self) -> None:
        from app.config import current_config
        from app.llm import LLM

        llm = LLM(model=None, timeout=600.0)
        config = current_config()
        assert llm._budget_for("critic") == config.llm_timeout_critic_seconds
        # 前缀匹配：`extract_places:<evidence id>` 也要命中 extract_places 那一份预算。
        assert llm._budget_for("extract_places:ev-3") == config.llm_timeout_extract_seconds
        # 没归类的 tag 用默认预算。
        assert llm._budget_for("query_expansion") == config.llm_timeout_seconds

    def test_caller_supplied_cap_always_wins(self) -> None:
        from app.llm import LLM

        # 10s 是调用方给的硬上限；按用途的预算只能在它之下收紧，不能把它放大。
        llm = LLM(model=None, timeout=10.0)
        assert llm._budget_for("critic") == 10.0
        assert llm._budget_for("final_answer") == 10.0


class TestDigestNarrowingKeepsJsonValid:
    """给模型的 digest 超预算时要**按结构**瘦身，绝不能把 JSON 从中间切断。"""

    @staticmethod
    def _digest() -> dict:
        return {
            "days": [
                {
                    "day_index": index,
                    "items": [
                        {"id": f"d{index}-i{item}", "reason": "很长的理由" * 40}
                        for item in range(9)
                    ],
                }
                for index in range(9)
            ],
            "budget": {"projected_total": 1.0, "breakdown": {"hotel": 1.0}, "breakdown_price_type": {"hotel": "real"}},
        }

    def test_narrowing_levels_keep_the_payload_serialisable(self) -> None:
        import json

        from app.workflow import DIGEST_DAYS, DIGEST_ITEMS_PER_DAY, _apply_narrowing

        for level in (1, 2, 3):
            narrowed = _apply_narrowing(self._digest(), level)
            # 每一级都必须仍然是一份合法 JSON（这正是"不按字符切断"的全部意义）。
            assert json.loads(json.dumps(narrowed, ensure_ascii=False, default=str))

        final = _apply_narrowing(self._digest(), 3)
        assert len(final["days"]) == DIGEST_DAYS
        assert final["days_dropped"] == 3
        assert len(final["days"][0]["items"]) == DIGEST_ITEMS_PER_DAY
        assert final["days"][0]["items_dropped"] == 5
        # 明细被裁掉时预算只留汇总，避免"看不见的部分被当成不存在"。
        assert "breakdown" not in final["budget"]

    def test_level_zero_leaves_the_digest_untouched(self) -> None:
        from app.workflow import _apply_narrowing

        original = self._digest()
        assert _apply_narrowing(original, 0) is original
        assert "days_dropped" not in original



class TestBatchedExtractionRetry:
    """批抽取超时后必须按单篇重试，而不是让一次慢调用带走整批证据。

    这是实测撞到的：4 篇一批时整批在 120s 预算上打满，4 条证据的地点一起丢。
    批内各篇本来是互相独立的，所以"整批失败"不该等于"每篇都失败"。
    """

    class _SlowBatchLLM:
        """整批（多条证据）必超时，单篇必成功：模拟"模型只是慢，不是不会抽"。"""

        def __init__(self) -> None:
            self.tags: list[str] = []

        def invoke_json(self, system: str, user: str, *, tag: str = ""):
            from app.llm import LLMResult

            self.tags.append(tag)
            ids = [
                line.split("：", 1)[1].strip()
                for line in user.splitlines()
                if line.startswith("### 证据 id：") and line.split("：", 1)[1].strip()
            ]
            if len(ids) > 1:
                return LLMResult(status="TIMEOUT", tag=tag, error="整批太慢", duration_ms=1)
            return LLMResult(
                status="OK",
                tag=tag,
                duration_ms=1,
                value={
                    "results": [
                        {"evidence_id": item_id, "places": [{"name": f"{item_id} 抽出的地点"}]}
                        for item_id in ids
                    ]
                },
            )

    @staticmethod
    def _evidence(item_id: str):
        from app.models import Evidence

        return Evidence(
            id=item_id,
            provider="xhs",
            source_type="social",
            title=f"{item_id} 标题",
            text="正文足够长，满足抽取的最小长度要求，用来触发模型抽取这条分支。" * 2,
        )

    def test_timed_out_batch_is_retried_per_evidence(self) -> None:
        from app.discovery import extract_places_from_evidences

        llm = self._SlowBatchLLM()
        items, degradations = extract_places_from_evidences(
            llm, [self._evidence("e1"), self._evidence("e2")]
        )

        # 重试生效：两篇各自被抽出来了，而不是整批一起丢
        assert {item["evidence_id"] for item in items} == {"e1", "e2"}
        # 重试用的是**单篇**标签（因此拿到更紧的预算），并且留下了降级说明
        assert any(tag.startswith("extract_places_retry") for tag in llm.tags)
        assert any("按单篇重试" in note for note in degradations)

    def test_successful_batch_is_not_retried(self) -> None:
        from app.discovery import extract_places_from_evidences
        from app.llm import LLMResult

        class _OkLLM:
            def __init__(self) -> None:
                self.tags: list[str] = []

            def invoke_json(self, system: str, user: str, *, tag: str = ""):
                self.tags.append(tag)
                ids = [
                    line.split("：", 1)[1].strip()
                    for line in user.splitlines()
                    if line.startswith("### 证据 id：") and line.split("：", 1)[1].strip()
                ]
                return LLMResult(
                    status="OK",
                    tag=tag,
                    duration_ms=1,
                    value={
                        "results": [
                            {"evidence_id": item_id, "places": [{"name": "一次就抽到"}]}
                            for item_id in ids
                        ]
                    },
                )

        llm = _OkLLM()
        items, degradations = extract_places_from_evidences(
            llm, [self._evidence("e1"), self._evidence("e2")]
        )
        assert len(items) == 2
        assert degradations == []
        # 没有发出任何重试调用（不重复问模型同一件事）
        assert not any(tag.startswith("extract_places_retry") for tag in llm.tags)
