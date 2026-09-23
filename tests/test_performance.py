"""性能优化的关键不变量（性能任务 Part A / C / H）。

这些用例只锁"并发不能让系统变得不可信"的部分 —— 并发本身快不快由
`scripts/e2e_real.py` 的真实 run 证明，这里保证它没有偷偷破坏：

  1. 子 span 的时间戳来自真实调用时刻，不是落盘时刻；
  2. 各类上限（并发/token/工具预算）都来自配置，且彼此有明确的大小关系；
  3. 批抽取超时后会退化成逐篇重试，而不是把整批丢掉。

（原先还锁了 `workflow._run_parallel` 的顺序 / 上限 / 异常语义与摘要降级：
那两个 helper 随固定 12 步流程一起退役，相关用例一并删除。）
"""

from __future__ import annotations

from app.observability import finish_iso


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
        # 用户可见候选上限（推荐池每城最多 40，2026-09-23 拍板）不再受 discovery_max_places
        # （原始地名核实上限）约束，只要求落在 EDITABLE_KEYS 的合法区间（USER_VISIBLE_POI_LIMIT：4~60）。
        assert 4 <= config.user_visible_poi_limit <= 60

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


class TestBatchedExtractionRetry:
    """批抽取超时后必须按单篇重试，而不是让一次慢调用带走整批证据。

    这是实测撞到的：4 篇一批时整批在 120s 预算上打满，4 条证据的地点一起丢。
    批内各篇本来是互相独立的，所以"整批失败"不该等于"每篇都失败"。
    """

    class _SlowBatchLLM:
        """整批（多条证据）必超时，单篇必成功：模拟"模型只是慢，不是不会抽"。"""

        def __init__(self) -> None:
            self.tags: list[str] = []

        def invoke_json(self, system: str, user: str, *, tag: str = "", context: str | None = None):
            from app.llm import LLMResult

            self.tags.append(tag)
            ids = [
                line.split("：", 1)[1].strip()
                for line in (context or user).splitlines()
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

            def invoke_json(self, system: str, user: str, *, tag: str = "", context: str | None = None):
                self.tags.append(tag)
                ids = [
                    line.split("：", 1)[1].strip()
                    for line in (context or user).splitlines()
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
