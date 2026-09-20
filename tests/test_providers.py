"""ProviderHub 自身的归一化约定（插件层的行为由 tests/test_amap_cn.py 等覆盖）。

这里只钉住"把 Provider 返回值映射成领域模型"这一步：哪些字段是 Hub 决定的、
一旦改错会静默污染下游（去重、Trust、provenance）。
"""

import json
import threading
import time
from datetime import date
from types import SimpleNamespace

import app.providers as providers
from app.models import utcnow
from app.providers import ProviderCall, ProviderHub


def _poi_call(items):
    return SimpleNamespace(
        source_id="run1-src-amap-poi-1", status="OK", items=items, error=None
    )


def _hub(monkeypatch, items):
    hub = ProviderHub(run_id="run1", store=None, mcp_servers=[])
    monkeypatch.setattr(hub, "_plugin_call", lambda **kwargs: _poi_call(items))
    return hub


def test_检索词不会变成地点的别名(monkeypatch):
    """aliases 是"这个地点的另一个名字"，检索词只是这次查询用的词。

    曾经把检索词写进 aliases，于是 8 个检索词（景点/美食/拍照…）成了每个 POI 的别名，
    去重按"共享别名"把 64 个互不相关的地点并成 8 个，5 天行程有 3 天没有任何安排。
    """
    hub = _hub(
        monkeypatch,
        [{"name": "宽窄巷子", "poi_id": "B0X", "city": "成都"}],
    )

    result = hub.search_poi("景点", "成都", page_size=10, type_hint="attraction")

    assert result.items[0].aliases == []


def test_地点带上产生它的那次调用的_source_id(monkeypatch):
    """没有 source_id，"只有高德 POI、没有社媒证据"的地点就指不出任何来源。"""
    hub = _hub(
        monkeypatch,
        [{"name": "宽窄巷子", "poi_id": "B0X", "city": "成都"}],
    )

    result = hub.search_poi("宽窄巷子", "成都", page_size=10, type_hint="attraction")

    assert result.items[0].source_id == "run1-src-amap-poi-1"


def test_每次调用都上报给_superharness_observability(monkeypatch):
    """START.md §9：Tool 日志由 SuperHarness 原生 Observability 负责，不要自己再造一套。

    上报口在 `_record` 上，所有 Plugin / MCP 调用都经过它，所以不会漏掉某条链路。
    """
    from app.providers import ProviderCall

    events: list[tuple[str, str, str | None, str]] = []

    def collect(event_type, component, *, data=None, status=None):
        events.append((event_type, component, status, (data or {}).get("name", "")))

    hub = _hub(monkeypatch, [{"name": "宽窄巷子", "poi_id": "B0X", "city": "成都"}])
    hub._emit_hook = collect

    hub._record(
        ProviderCall(
            source_id="run1-src-1",
            provider="amap",
            source_type="poi",
            tool="search_poi",
            query={"keyword": "公园"},
            status="OK",
            fetched_at="2026-09-18T10:00:00Z",
            payload={},
            items=[1, 2, 3],
            error=None,
            duration_ms=42,
        )
    )

    assert [event[0] for event in events] == ["tool.started", "tool.finished"]
    assert events[1][1] == "provider"
    assert events[1][2] == "OK"
    assert events[1][3] == "amap/search_poi"


def test_没有上报口时照样能记录调用(monkeypatch):
    """单测与独立跑 Hub 时没有 emit：观测缺席不该影响留痕。"""
    from app.providers import ProviderCall

    hub = _hub(monkeypatch, [])

    call = hub._record(
        ProviderCall(
            source_id="run1-src-2",
            provider="tuniu",
            source_type="flight",
            tool="search_flights",
            query={},
            status="EMPTY",
            fetched_at="2026-09-18T10:00:00Z",
            payload={},
            items=[],
            error=None,
        )
    )

    assert call.status == "EMPTY"
    assert hub.calls[-1] is call


# ==================================================
# Part E：Tool 级更紧预算
# ==================================================


def test_门票工具用自己的更紧预算而不是途牛整包(monkeypatch):
    """同一个 tuniu 下"查门票"是轻查询，不该共用火车/酒店那份 90s 预算。

    正常 2~6s 就返回；一次挂死若按 90s 计，会独占整个 run（真实 run 里 1 次
    TIMEOUT 185.3s 占了墙钟的 70.7%）。这里断言传进受控调用的就是门票自己的预算。
    """
    monkeypatch.setenv("TICKET_TIMEOUT_SECONDS", "3")
    captured: dict[str, object] = {}

    def fake_bounded(tool, args, *, timeout, label):
        captured["timeout"] = timeout
        captured["args"] = args
        return json.dumps(
            {"status": "OK", "items": [{"scenic_name": "宽窄巷子", "price": 50}]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(providers, "_invoke_bounded", fake_bounded)
    hub = ProviderHub(run_id="run1", store=None, mcp_servers=[], timeouts={"tuniu": 90.0})
    fake_tool = SimpleNamespace(
        name="tuniu_search_scenic_tickets", args={"timeout": None, "scenic_name": None}
    )
    monkeypatch.setattr(hub.plugins, "get", lambda name: ("tuniu_travel", fake_tool))

    result = hub.search_scenic_tickets("宽窄巷子")

    assert captured["timeout"] == 3.0
    # 声明的 timeout 入参也要写进去：审计里能看到"这次用的是几秒的预算"。
    assert captured["args"]["timeout"] == 3.0
    assert result.calls[0].query["timeout"] == 3.0


# ==================================================
# 并发安全：hub 被并行 task 共用
# ==================================================


def test_并发下_source_id_不重复():
    """`self._counter += 1` 是读-改-写；并发下必须靠锁，否则会发出重复 source_id。

    重复的 source_id 会让"这个地点来自哪次调用"出现两个答案（provenance 串味）。
    """
    hub = ProviderHub(run_id="run1", store=None, mcp_servers=[])
    ids: list[str] = []
    guard = threading.Lock()

    def worker() -> None:
        local = [hub._next_source_id() for _ in range(25)]
        with guard:
            ids.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(ids) == 200
    assert len(set(ids)) == 200


# ==================================================
# 交通对冲（hedge）
# ==================================================


_12306_ITEM = {
    "start_train_code": "G321",
    "start_date": "2026-10-01",
    "arrive_date": "2026-10-01",
    "start_time": "07:00",
    "arrive_time": "14:30",
    "lishi": "07:30",
    "from_station": "北京西",
    "to_station": "成都东",
    "prices": [{"seat_name": "二等座", "num": "有", "price": 918}],
}


def _tuniu_item(code: str) -> dict:
    return {
        "train_no": code,
        "origin_station": "北京",
        "destination_station": "成都",
        "departure_at": "2026-10-01 08:00",
        "arrival_at": "2026-10-01 15:00",
        "duration_minutes": 420,
        "seats": {"二等座": 10},
        "price_range": {"min": 900, "max": 900},
    }


def _fake_call(hub, *, provider: str, tool: str, status: str, items: list, error=None):
    """造一条**完整构造后**再进账本的 ProviderCall（模拟 _mcp_call/_plugin_call 的留痕）。"""
    return hub._record(
        ProviderCall(
            source_id=hub._next_source_id(),
            provider=provider,
            source_type="train",
            tool=tool,
            query={},
            status=status,
            fetched_at=utcnow(),
            items=list(items),
            error=error,
        )
    )


def _hedge_hub(monkeypatch, *, primary_budget: float = 1.0, hedge_budget: float = 0.5):
    """对冲测试用的 Hub：小预算 + 小等待窗口，保证每个用例都在 2s 内跑完。"""
    monkeypatch.setenv("TRANSPORT_HEDGE_ENABLED", "1")
    monkeypatch.setenv("TRANSPORT_HEDGE_WAIT_SECONDS", "0.1")
    return ProviderHub(
        run_id="run1",
        store=None,
        mcp_servers=[],
        timeouts={"12306": primary_budget, "tuniu": hedge_budget},
    )


def test_对冲_主源慢备胎快_提前采用备胎且主源仍留痕(monkeypatch):
    """12306 慢、途牛已有非空结果 → 提前提交，不等 12306 跑满预算。

    但"提前提交"不等于"丢掉主源"：12306 那次调用仍要在账本里（后台跑完补记），
    否则审计无法回答"用了谁、放弃了谁"。
    """
    hub = _hedge_hub(monkeypatch)

    def slow_primary(**kwargs):
        time.sleep(0.6)
        return _fake_call(
            hub,
            provider="12306",
            tool="railway_12306_get-tickets",
            status="OK",
            items=[_12306_ITEM],
        )

    def fast_hedge(**kwargs):
        return _fake_call(
            hub,
            provider="tuniu",
            tool="tuniu_search_trains",
            status="OK",
            items=[_tuniu_item("D1")],
        )

    monkeypatch.setattr(hub, "_mcp_call", slow_primary)
    monkeypatch.setattr(hub, "_plugin_call", fast_hedge)

    started = time.monotonic()
    result = hub.search_trains("北京", "成都", date(2026, 10, 1), hedge=True)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "备胎已有结果就不该等主源跑满 1.0s 预算"
    assert result.provider == "tuniu"
    assert [item.train_no for item in result.items] == ["D1"]
    assert result.items[0].provider == "tuniu"
    assert result.calls[-1].fallback is True

    # 主源后台跑完（0.6s）后必须补记进 self.calls。
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(c.provider == "12306" for c in hub.calls):
        time.sleep(0.05)
    assert any(c.provider == "12306" for c in hub.calls)


def test_对冲_主源快且成功_只采用主源且备胎标discarded(monkeypatch):
    """主源成功时备胎结果绝不混入（PRD §32 单源不变量），并留下 discarded 痕迹。"""
    hub = _hedge_hub(monkeypatch)

    def fast_primary(**kwargs):
        return _fake_call(
            hub,
            provider="12306",
            tool="railway_12306_get-tickets",
            status="OK",
            items=[_12306_ITEM],
        )

    def hedge_with_items(**kwargs):
        time.sleep(0.05)
        return _fake_call(
            hub,
            provider="tuniu",
            tool="tuniu_search_trains",
            status="OK",
            items=[_tuniu_item("D1")],
        )

    monkeypatch.setattr(hub, "_mcp_call", fast_primary)
    monkeypatch.setattr(hub, "_plugin_call", hedge_with_items)

    result = hub.search_trains("北京", "成都", date(2026, 10, 1), hedge=True)

    assert result.provider == "12306"
    assert [item.train_no for item in result.items] == ["G321"]
    assert all(item.provider == "12306" for item in result.items)

    hedge_call = next(call for call in hub.calls if call.provider == "tuniu")
    assert hedge_call.discarded is True
    assert hedge_call.fallback is True
    assert "未被采用" in " ".join(hedge_call.notes)


def test_对冲_主源EMPTY_返回空且不采用备胎(monkeypatch):
    """主源 EMPTY 是"这天没车"的结论，备胎即使有车也不能被采用。"""
    hub = _hedge_hub(monkeypatch)

    def empty_primary(**kwargs):
        return _fake_call(
            hub,
            provider="12306",
            tool="railway_12306_get-tickets",
            status="EMPTY",
            items=[],
        )

    def hedge_with_items(**kwargs):
        time.sleep(0.05)
        return _fake_call(
            hub,
            provider="tuniu",
            tool="tuniu_search_trains",
            status="OK",
            items=[_tuniu_item("D1")],
        )

    monkeypatch.setattr(hub, "_mcp_call", empty_primary)
    monkeypatch.setattr(hub, "_plugin_call", hedge_with_items)

    result = hub.search_trains("北京", "成都", date(2026, 10, 1), hedge=True)

    assert result.status == "EMPTY"
    assert result.items == []
    assert result.provider == "12306"
    assert all(item.provider != "tuniu" for item in result.items)

    hedge_call = next(call for call in hub.calls if call.provider == "tuniu")
    assert hedge_call.discarded is True


def test_对冲_备胎提前返回空_不得提前提交_跟随主源结论(monkeypatch):
    """"备胎没找到"不等于"这天没车"：备胎空必须继续等主源，否则一次慢查询会误判。

    这里途牛在 0ms 就返回 EMPTY，12306 在 0.4s 才带着 G321 回来；结果必须是 12306 的，
    而且墙钟要等到 12306（不能被 0.1s 的窗口提前"提交"成 EMPTY）。
    """
    hub = _hedge_hub(monkeypatch)

    def slow_primary(**kwargs):
        time.sleep(0.4)
        return _fake_call(
            hub,
            provider="12306",
            tool="railway_12306_get-tickets",
            status="OK",
            items=[_12306_ITEM],
        )

    def empty_hedge(**kwargs):
        return _fake_call(
            hub,
            provider="tuniu",
            tool="tuniu_search_trains",
            status="EMPTY",
            items=[],
        )

    monkeypatch.setattr(hub, "_mcp_call", slow_primary)
    monkeypatch.setattr(hub, "_plugin_call", empty_hedge)

    started = time.monotonic()
    result = hub.search_trains("北京", "成都", date(2026, 10, 1), hedge=True)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.3, "备胎 EMPTY 不得触发提前提交，必须等主源"
    assert result.provider == "12306"
    assert [item.train_no for item in result.items] == ["G321"]

