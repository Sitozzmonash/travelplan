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
from typing import Any

from app import discovery, sessions
from app.discovery import PrefetchBundle
from app.models import TripIntent
from app.workflow import execute_travel_run
from tests.fakes import QUERY, FakeHub, FakeJev, FakeLLM, make_store


def _discover_all(store, *, spread: float = 0.25):
    """跑一次完整的 Discovery（生产保真：Discovery 不落库，所以 store=None）。"""

    hub = FakeHub(store=None, run_id="ps-full", poi_spread=spread)
    intent = TripIntent(
        destination=["成都"], origin="北京", start_date="2026-10-01", days=4, travelers=2, source="guided"
    )
    return intent, discovery.prefetch(hub, FakeLLM(), intent, hotel_pages=2), hub


def _bundle(result: dict[str, Any], hub: FakeHub, *, stages=("transport", "hotels", "social", "places"), session_id="ps-handoff"):
    return PrefetchBundle(
        session_id=session_id,
        outbound=list(result["transport"].outbound) if "transport" in stages else [],
        inbound=list(result["transport"].inbound) if "transport" in stages else [],
        hotels=list(result["hotels"].items) if "hotels" in stages else [],
        evidences=list(result["social"].evidences) if "social" in stages else [],
        places=list(result["places"].places) if "places" in stages else [],
        # 与生产侧 sessions._bundle_from 保持一致：计划 query 与真正搜过的 query 都要过户
        social_queries=list(result["social"].queries) if "social" in stages else [],
        social_served_queries=list(result["social"].served_queries) if "social" in stages else [],
        provider_calls=hub.audit_entries(),
        discovery=dict(result.get("stages") or {}),
    )


def _run(store, intent, bundle: PrefetchBundle, *, run_id: str):
    store.create_run(run_id, source="guided", source_session_id=bundle.session_id)
    hub = FakeHub(store=store, run_id=run_id, poi_spread=0.25)
    result = execute_travel_run(
        QUERY,
        store=store,
        hub=hub,
        llm=FakeLLM(),
        jev=FakeJev(),
        intent=intent,
        prefetch=bundle,
        source="guided",
        source_session_id=bundle.session_id,
        run_id=run_id,
        output_dir=store.db_path.parent / "out",
    )
    return result, hub


def _handoff(result) -> dict[str, str]:
    return {
        key: (info or {}).get("handoff")
        for key, info in (result.audit["user_journey"]["discovery"] or {}).items()
    }


class TestDiscoveryHandoff:
    def test_all_discovery_finished_reuses_everything(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        run, run_hub = _run(store, intent, _bundle(result, hub), run_id="tp-h-all")

        assert run.status == "completed", run.error
        assert _handoff(run) == {
            "transport": "reused",
            "hotels": "reused",
            "social": "reused",
            "places": "reused",
        }
        # 复用就不该再打这几个源（只允许 route / 门票 / geocode 这类必须按最终候选算的调用）
        assert not ({"search_trains", "search_flights", "search_hotels"} & set(run_hub.calls))
        assert not ({"search_xiaohongshu", "search_douyin", "web_search"} & set(run_hub.calls))
        assert run.audit["user_journey"]["grace_waited_ms"] == 0

    def test_partial_discovery_reuses_done_and_queries_the_rest(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        # 只完成了交通 + 攻略；酒店与地点还没跑完
        run, run_hub = _run(
            store, intent, _bundle(result, hub, stages=("transport", "social")), run_id="tp-h-partial"
        )

        assert run.status == "completed", run.error
        handoff = _handoff(run)
        assert handoff["transport"] == "reused"
        assert handoff["social"] == "reused"
        assert handoff["hotels"] == "fallback_query"
        assert handoff["places"] == "fallback_query"
        # 分界线必须清楚：补查了酒店与 POI，但没有重查交通与攻略
        assert "search_hotels" in run_hub.calls
        assert "search_poi" in run_hub.calls
        assert not ({"search_trains", "search_flights"} & set(run_hub.calls))
        assert not ({"search_xiaohongshu", "search_douyin", "web_search"} & set(run_hub.calls))

    def test_one_direction_only_still_queries_the_missing_direction(self, tmp_path):
        """只复用了去程时，回程必须**真的去查**，不能被静默当成"已复用"。

        这是上一版的真实缺陷：复用判据是 `bundle.outbound or bundle.inbound`（只要有一个
        方向有数据就整体复用），于是"Discovery 只跑完了去程"会让回程候选被置空 ——
        用户看到的是"回程没票"，真相是"回程没查"。少查一个方向是 bug，不是优化。
        """

        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        partial = _bundle(result, hub, stages=("transport",))
        # 去掉回程，模拟"Discovery 只跑完去程"
        partial.inbound = []

        run, run_hub = _run(store, intent, partial, run_id="tp-h-one-direction")

        assert run.status == "completed", run.error
        # 回程确实发起了查询：不能因为去程有数据就跳过它
        assert "search_trains" in run_hub.calls
        # 而且交接状态不能再自称"交通已复用" —— 一半复用一半补查要如实说成补查
        assert _handoff(run)["transport"] == "fallback_query"

    def test_both_directions_reused_does_not_touch_transport(self, tmp_path):
        """去程与回程都在 bundle 里时，交通必须一次 Provider 都不打（对照组）。"""

        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        run, run_hub = _run(store, intent, _bundle(result, hub, stages=("transport",)), run_id="tp-h-both-ways")

        assert run.status == "completed", run.error
        assert _handoff(run)["transport"] == "reused"
        assert not ({"search_trains", "search_flights"} & set(run_hub.calls))

    def test_no_discovery_result_falls_back_to_workflow(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        run, run_hub = _run(store, intent, _bundle(result, hub, stages=()), run_id="tp-h-none")

        assert run.status == "completed", run.error
        assert all(value == "fallback_query" for value in _handoff(run).values())
        assert {"search_trains", "search_hotels", "search_poi"} <= set(run_hub.calls)
        assert run.plan is not None and run.plan.days

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
        assert bundle_arg.outbound and bundle_arg.hotels, "等到的候选必须带进正式 run"

    def test_grace_period_timeout_does_not_block(self, tmp_path, monkeypatch):
        store = make_store(tmp_path / "t.db")
        monkeypatch.setenv("DISCOVERY_GRACE_SECONDS", "0.4")
        session = sessions.create_session(
            store, {"origin": "北京", "destination": "成都", "start_date": "2026-10-01", "days": 4}
        )
        session_id = session["session_id"]
        # Discovery 一直没结束（模拟慢社交源）
        session.update(
            {"status": sessions.SESSION_DISCOVERING, "discovery_status": sessions.DISCOVERY_RUNNING}
        )
        store.save_planning_session(session)

        jobs: list[tuple] = []
        started = time.perf_counter()
        outcome = sessions.start_run(
            store, session_id, submit=lambda fn, *a: jobs.append((fn, a)), output_dir=str(tmp_path)
        )
        elapsed = time.perf_counter() - started

        assert outcome.get("run_id")
        assert elapsed < 3.0, f"grace period 把用户卡住了 {elapsed:.1f}s"
        _fn, args = jobs[0]
        bundle_arg = next(item for item in args if hasattr(item, "grace_waited_ms"))
        assert bundle_arg.grace_waited_ms >= 300
        assert not bundle_arg.reused, "没有候选时必须带回空 bundle，让 Workflow 自己补查"
        events = [
            event["event"]
            for event in (store.get_planning_session(session_id) or {}).get("events") or []
        ]
        assert "discovery_grace_waited" in events

    def test_reused_lines_are_not_queried_again_and_trace_shows_it(self, tmp_path):
        store = make_store(tmp_path / "t.db")
        intent, result, hub = _discover_all(store)
        run, run_hub = _run(store, intent, _bundle(result, hub), run_id="tp-h-trace")

        assert run.status == "completed", run.error
        assert run_hub.calls.count("search_trains") == 0
        assert run_hub.calls.count("search_hotels") == 0
        assert run_hub.calls.count("search_poi") == 0

        handoff_spans = [
            span for span in store.get_trace_spans("tp-h-trace") if span["name"] == "discovery_handoff"
        ]
        assert len(handoff_spans) == 1
        attributes = handoff_spans[0]["attributes"]
        assert attributes["transport"] == "reused"
        assert attributes["hotels"] == "reused"
        assert "grace_waited_ms" in attributes


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
