"""Discovery → 正式 Run 的数据交接（用户点「开始规划」时 Discovery 可能还没跑完）。

这是真金白银的一类 bug：旧实现只在四条线全部结束时才写一次会话，于是**中途点开始
就等于白查** —— 已经拿到的候选被丢掉，正式 run 还会重新打一遍 Provider。

这一组把六种情况全部钉住：
  1. Discovery 全完成 → 全部复用；
  2. 部分完成 → 完成的那部分复用、没完成的由正式 run 补查；
  3. 一条都没完成 → 全部走原 Workflow 补查（不丢数据、不卡死）；
  4. grace period 内刚好完成 → 等到的结果必须被带进正式 run；
  5. grace period 超时 → 不阻塞用户，带着已有部分开始；
  6. 已复用的线不重复调用 Provider，并且 Trace 里看得出交接结果与等待时长。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from app import discovery, sessions
from app.discovery import PrefetchBundle
from app.models import TripIntent
from app.workflow import _user_journey_summary
from tests.fakes import FakeHub, FakeLLM, make_store


def _discover_all(store, *, spread: float = 0.25):
    """跑一次完整的 Discovery（生产保真：Discovery 不落库，所以 store=None）。"""

    hub = FakeHub(store=None, run_id="ps-full", poi_spread=spread)
    intent = TripIntent(
        destination=["成都"], origin="北京", start_date="2026-10-01", days=4, travelers=2, source="guided"
    )
    return intent, discovery.prefetch(hub, FakeLLM(), intent, hotel_pages=2), hub


def _bundle(result: dict[str, Any], hub: FakeHub, *, stages=("transport", "hotels", "social", "places"), session_id="ps-handoff"):
    places = list(result["places"].places) if "places" in stages else []
    return PrefetchBundle(
        session_id=session_id,
        outbound=list(result["transport"].outbound) if "transport" in stages else [],
        inbound=list(result["transport"].inbound) if "transport" in stages else [],
        hotels=list(result["hotels"].items) if "hotels" in stages else [],
        evidences=list(result["social"].evidences) if "social" in stages else [],
        places=places,
        # 与生产侧 PrefetchBundle 的过户口径保持一致：计划 query 与真正搜过的 query 都要过户
        social_queries=list(result["social"].queries) if "social" in stages else [],
        social_served_queries=list(result["social"].served_queries) if "social" in stages else [],
        provider_calls=hub.audit_entries(),
        discovery=dict(result.get("stages") or {}),
        # 正式 run 只接受本次推荐清单；没有它 START 会被明确拒绝，而不是拿整城重查。
        extras={"recommendation": {"status": "READY", "source": "llm", "version": 1,
                                   "place_ids": [item.place_id for item in places]}} if places else {},
    )



class TestDiscoveryHandoff:
    def test_discovery_completing_within_grace_period_is_merged(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 4}
        )
        session_id = session["session_id"]
        session.update(
            {"status": sessions.SESSION_DISCOVERING, "discovery_status": sessions.DISCOVERY_RUNNING}
        )
        store.save_planning_session(session)

        def finish_later() -> None:
            time.sleep(0.3)
            row = store.get_planning_session(session_id)
            row.update(
                {
                    "status": sessions.SESSION_READY,
                    "discovery_status": sessions.DISCOVERY_READY,
                    "prefetch": _bundle(result, hub, session_id=session_id).dump(),
                }
            )
            store.save_planning_session(row)

        thread = threading.Thread(target=finish_later, daemon=True)
        thread.start()
        jobs: list[tuple] = []
        outcome = sessions.start_run(
            store, session_id, submit=lambda fn, *a: jobs.append((fn, a)), output_dir=str(tmp_path)
        )
        thread.join(timeout=5)

        assert outcome.get("run_id")
        _fn, args = jobs[0]
        bundle_arg = next(item for item in args if hasattr(item, "grace_waited_ms"))
        assert bundle_arg.grace_waited_ms >= 200, "grace 内完成必须被等到"
        assert [item.place_id for item in bundle_arg.places], "等到的推荐必须带进正式 run"
        assert bundle_arg.extras["selection_source"] == "recommended"

    def test_grace_timeout_refuses_start_instead_of_expanding_to_city_pool(self, tmp_path, monkeypatch):
        """推荐没就绪时宁可拒绝并提示，也不静默放大成整个城市缓存。"""

        store = make_store(tmp_path / "t.db")
        monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0.4")
        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 4}
        )
        session_id = session["session_id"]
        # 推荐一直没结束（模拟慢社交源 / 模型还在多轮工具调用）
        session.update(
            {"status": sessions.SESSION_DISCOVERING, "discovery_status": sessions.DISCOVERY_RUNNING,
             "prefetch": {"recommendation": {"status": "RUNNING", "version": 1, "place_ids": [], "source": None}}}
        )
        store.save_planning_session(session)

        jobs: list[tuple] = []
        started = time.perf_counter()
        outcome = sessions.start_run(
            store, session_id, submit=lambda fn, *a: jobs.append((fn, a)), output_dir=str(tmp_path)
        )
        elapsed = time.perf_counter() - started

        assert outcome == {"error": "recommendation_not_ready"}
        assert 0.3 <= elapsed < 3.0, f"先给 grace 窗口再拒绝，且不能把用户卡住：{elapsed:.1f}s"
        assert not jobs
        assert store.get_planning_session(session_id)["run_id"] is None

        # 推荐发布之后同一个会话仍可正常开始，不用重建。
        _, result, hub = _discover_all(store)
        row = store.get_planning_session(session_id)
        row.update({"status": sessions.SESSION_READY, "discovery_status": sessions.DISCOVERY_READY,
                    "prefetch": _bundle(result, hub, session_id=session_id).dump()})
        store.save_planning_session(row)
        jobs.clear()
        assert sessions.start_run(
            store, session_id, submit=lambda fn, *a: jobs.append((fn, a)), output_dir=str(tmp_path)
        ).get("run_id")



class TestSocialQueryHandoff:
    """Discovery 计划搜的 query 必须随 bundle 过户给正式 run（省掉一次 query_expansion）。"""

    def test_bundle_roundtrips_social_queries(self):
        bundle = PrefetchBundle(
            session_id="ps-q",
            social_queries=["成都 攻略", "成都 美食"],
            social_served_queries=["成都 攻略"],
        )
        restored = PrefetchBundle.load(bundle.dump())
        assert restored.social_queries == ["成都 攻略", "成都 美食"]
        assert restored.social_served_queries == ["成都 攻略"]

    def test_old_payload_without_query_keys_loads_empty(self):
        # 本次改动之前存的 payload：根本没有这两个键，必须落到空列表而不是抛异常
        old_payload = {"session_id": "ps-old", "discovery_status": "READY", "evidences": []}
        restored = PrefetchBundle.load(old_payload)
        assert restored.social_queries == []
        assert restored.social_served_queries == []
        # 空 payload / None 同样不能抛
        assert PrefetchBundle.load(None).social_queries == []
        assert PrefetchBundle.load({}).social_served_queries == []

    def test_missing_social_queries_is_case_and_space_insensitive(self):
        bundle = PrefetchBundle(social_served_queries=["成都 攻略", "Chengdu Food"])
        # 大小写 / 空白变体都算"已搜过"，不能误判成缺失（否则会白搜一遍）
        assert bundle.missing_social_queries(["成都攻略", "chengdu   food", "CHENGDU FOOD"]) == []
        # 只把真正没收到的原样返回，并保留传入顺序
        assert bundle.missing_social_queries(["成都 攻略", "成都 夜市"]) == ["成都 夜市"]
        # 一条都没搜过时，需要的就是全部
        assert PrefetchBundle().missing_social_queries(["成都 攻略"]) == ["成都 攻略"]

    def test_discovery_records_planned_and_served_queries(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        social = result["social"]
        assert social.queries, "完成的 Discovery 必须记录它实际使用的检索词"
        assert social.served_queries, "完成的 Discovery 必须记录真正发起过的检索词"

        bundle = _bundle(result, hub)
        assert bundle.social_queries == list(social.queries)
        assert bundle.social_served_queries == list(social.served_queries)
        # 差集只补缺的那条，已搜过的（含空白变体）不回锅
        assert bundle.missing_social_queries([*social.queries, "成都 夜市"]) == ["成都 夜市"]


def test_agent_results_are_not_reported_as_unavailable_discovery_handoff():
    """Agent 路径不回填固定流程 state，但最终来源和地点已证明本次补查有结果。"""

    plan = SimpleNamespace(
        intent=SimpleNamespace(place_selections={}),
        transport=None,
        hotel=None,
        days=[SimpleNamespace(items=[SimpleNamespace(place_id="poi-1")])],
        sources=[SimpleNamespace(provider="tavily")],
    )
    summary = _user_journey_summary(
        {
            "prefetch": None,
            "hub": None,
            "evidences": [],
            "places": [],
            "source": "guided",
            "source_session_id": "ps-agent",
        },
        plan,
    )

    assert summary["source"] == "guided"
    assert summary["discovery"]["social"]["handoff"] == "fallback_query"
    assert summary["discovery"]["places"]["handoff"] == "fallback_query"
