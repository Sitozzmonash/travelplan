"""ProviderHub 自身的归一化约定（插件层的行为由 tests/test_amap_cn.py 等覆盖）。

这里只钉住"把 Provider 返回值映射成领域模型"这一步：哪些字段是 Hub 决定的、
一旦改错会静默污染下游（去重、Trust、provenance）。
"""

from types import SimpleNamespace

from app.providers import ProviderHub


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

