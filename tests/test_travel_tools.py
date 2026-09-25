"""travel_tools 的离线约定（不联网、不读密钥、不 new ProviderHub）。

这里钉住四件事 —— 它们错了 Agent Loop 会以很难排查的方式退化：

1. **工具有几个、叫什么、参数长什么样**：Agent 是靠 name + description + args_schema
   选工具的，名字漂了一个（比如 `web_search` 改成 `search_web`）前端的中文步骤映射、
   prompt 里的工具名、历史 trace 解析会同时断；
2. **TOOL_DISPLAY_NAMES 覆盖全部工具**：前端把 tool 名映射成"在查火车票"，漏一个
   就会在用户面前退化成英文方法名；
3. **截断真的生效**：不截断 = Agent 调三次工具后上下文爆掉；截断错 = 模型拿到没有价格
   的火车、没有时间的路线，于是开始编；
4. **hub 是被传入的**：工具里自己 new 一个 Hub 会丢掉审计链并重复拉起 12306 子进程。

替身 Hub 只复刻方法签名与返回形状（`ProviderResult`），真实 Provider 行为由
tests/test_providers.py 覆盖。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app import travel_tools
from app.models import (
    Evidence,
    HotelOption,
    Place,
    RouteOption,
    TrainOption,
)
from app.providers import ProviderResult
from app.travel_tools import (
    DEFAULT_TOP_N,
    MAX_TOP_N,
    TOOL_DISPLAY_NAMES,
    build_travel_tools,
    clamp_top_n,
    truncate_items,
    truncate_result,
)

#: 工具名与方法名一一对应（也是前端 TOOL_DISPLAY_NAMES 的 key）。
EXPECTED_TOOLS = (
    "search_trains",
    "search_flights",
    "search_hotels",
    "search_scenic_tickets",
    "geocode",
    "search_poi",
    "poi_detail",
    "route",
    "search_xiaohongshu",
    "search_douyin",
    "web_search",
)


# ======================================================================
# 离线替身
# ======================================================================


def _trains(count: int) -> list[TrainOption]:
    return [
        TrainOption.from_item(
            {
                "train_no": f"G{1000 + index}",
                "train_type": "高铁",
                "origin_station": "北京西",
                "destination_station": "成都东",
                "departure_at": "2026-10-01T08:00:00",
                "arrival_at": "2026-10-01T15:30:00",
                "duration_minutes": 450 + index,
                "seats": {"二等座": 10 + index, "一等座": None},
                "price_range": {"min": 780.5, "max": 1200.0},
            },
            provider="12306",
            source_id="src-1",
        )
        for index in range(count)
    ]


def _evidences(count: int) -> list[Evidence]:
    return [
        Evidence.from_content(
            {
                "title": f"成都三日游笔记 {index}",
                "author": "某博主",
                "text": "攻略正文" * 500,  # 远超 MAX_TEXT_CHARS，用来验证正文被截断
                "place_mentions": ["宽窄巷子", "熊猫基地"],
                "source_url": "https://example.invalid/note",
            },
            evidence_id=f"ev-{index}",
            source_type="social",
            provider="tikhub",
            source_id="src-2",
        )
        for index in range(count)
    ]


class FakeHub:
    """按真实签名复刻 ProviderHub 的 11 个取数方法，返回预制 ProviderResult。"""

    def __init__(self, *, train_count: int = 30, status: str = "OK", error=None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._train_count = train_count
        self._status = status
        self._error = error
        self.closed = False

    # ---- 记录 ----
    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def _empty(self, provider: str) -> ProviderResult:
        return ProviderResult(
            status=self._status, items=[], provider=provider, error=self._error
        )

    # ---- 大交通 ----
    def search_trains(self, origin, destination, depart_date, *, filters=None, sort="startTime", limit=10, hedge=False):
        self._record("search_trains", (origin, destination, depart_date), {"limit": limit})
        if self._train_count == 0:
            return self._empty("12306")
        return ProviderResult(
            status="OK", items=_trains(self._train_count), provider="12306"
        )

    def search_flights(self, origin, destination, depart_date, *, travelers=1):
        self._record("search_flights", (origin, destination, depart_date), {"travelers": travelers})
        return ProviderResult(
            status="EMPTY", items=[], provider="tuniu", error="当日无航班"
        )

    def search_hotels(self, city, check_in, check_out, *, travelers=1, page_num=1):
        self._record("search_hotels", (city, check_in, check_out), {"travelers": travelers})
        return ProviderResult(
            status="OK",
            items=[
                HotelOption.from_item(
                    {
                        "hotel_id": f"H{index}",
                        "name": f"某酒店 {index}",
                        "address": "成都市锦江区某路 1 号",
                        "price_per_night": 420.0,
                        "rating": 4.6,
                    },
                    check_in=date(2026, 10, 1),
                    check_out=date(2026, 10, 3),
                )
                for index in range(8)
            ],
            provider="tuniu",
        )

    def search_scenic_tickets(self, scenic_name):
        self._record("search_scenic_tickets", (scenic_name,), {})
        return ProviderResult(
            status="OK",
            items=[{"channel": "官方", "price": 55.0, "url": "https://example.invalid/t"}],
            provider="tuniu",
        )

    # ---- 高德 ----
    def geocode(self, address, city=None):
        self._record("geocode", (address, city), {})
        return ProviderResult(
            status="OK",
            items=[{"formatted_address": "四川省成都市", "location": "104.06,30.67", "city": "成都"}],
            provider="amap",
        )

    def search_poi(self, keywords, region, *, types=None, page=1, page_size=20, type_hint=None):
        self._record("search_poi", (keywords, region), {"types": types})
        return ProviderResult(
            status="OK",
            items=[
                Place.from_poi(
                    {"poi_id": f"B{index}", "name": f"宽窄巷子{index}", "city": region, "type": "景点"},
                    city=region,
                    source_id="src-3",
                )
                for index in range(25)
            ],
            provider="amap",
        )

    def poi_detail(self, poi_id):
        self._record("poi_detail", (poi_id,), {})
        return ProviderResult(
            status="OK",
            items=[{"poi_id": poi_id, "name": "宽窄巷子", "opening_hours": "全天", "rating": "4.5"}],
            provider="amap",
        )

    def route(self, origin, destination, mode="transit", *, city=None):
        self._record("route", (origin, destination, mode), {"city": city})
        return ProviderResult(
            status="OK",
            items=[
                RouteOption(
                    mode=mode, distance_meters=5200, duration_seconds=1810, estimated_cost=4.0
                )
            ],
            provider="amap",
        )

    # ---- 攻略 / 联网 ----
    def search_xiaohongshu(self, keyword, *, page=1):
        self._record("search_xiaohongshu", (keyword,), {"page": page})
        return ProviderResult(status="OK", items=_evidences(9), provider="tikhub")

    def search_douyin(self, keyword, *, page=1):
        self._record("search_douyin", (keyword,), {"page": page})
        return ProviderResult(status="EMPTY", items=[], provider="tikhub", error="无结果")

    def web_search(self, query, *, max_results=5):
        self._record("web_search", (query,), {"max_results": max_results})
        return ProviderResult(
            status="OK",
            items=[{"title": "官网公告", "url": "https://example.invalid/a", "content": "内容" * 400}],
            provider="tavily",
        )


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def tools(hub: FakeHub) -> list:
    return build_travel_tools(hub)


def _invoke(tools: list, name: str, args: dict) -> dict:
    tool = next(item for item in tools if item.name == name)
    payload = tool.invoke(args)
    assert isinstance(payload, str), "工具必须返回给模型读的字符串"
    return json.loads(payload)


# ======================================================================
# 1. 工具清单与描述
# ======================================================================


def test_工具数量与名称与_hub_方法一一对应(tools):
    assert [tool.name for tool in tools] == list(EXPECTED_TOOLS)


def test_每个工具都有写给_LLM_的中文说明(tools):
    """description 是 Agent 唯一的"什么时候用这个工具"的依据，不能空、不能是英文方法名。"""
    for tool in tools:
        assert tool.description.strip(), f"{tool.name} 缺少 description"
        assert len(tool.description) >= 40, f"{tool.name} 的 description 太短，不足以让模型判断用途"
        assert "什么时候用" in tool.description, f"{tool.name} 没有说明使用时机"
        assert "返回" in tool.description, f"{tool.name} 没有说明返回内容"
        assert tool.description != tool.name


def test_展示名覆盖全部工具(tools):
    names = {tool.name for tool in tools}
    assert set(TOOL_DISPLAY_NAMES) == names
    for name, display in TOOL_DISPLAY_NAMES.items():
        assert display.strip(), f"{name} 的中文展示名为空"
        assert any("\u4e00" <= char <= "\u9fff" for char in display), f"{name} 的展示名不是中文"


# ======================================================================
# 2. 参数 schema
# ======================================================================


def test_必填与可选参数_schema_正常(tools):
    def schema(name: str) -> dict:
        return next(item for item in tools if item.name == name).tool_call_schema.model_fields

    trains = schema("search_trains")
    assert list(trains) == ["origin", "destination", "depart_date", "top_n"]
    assert trains["origin"].is_required() and trains["destination"].is_required()
    assert trains["depart_date"].is_required()
    assert not trains["top_n"].is_required() and trains["top_n"].default == DEFAULT_TOP_N
    assert trains["depart_date"].annotation is str, "日期必须是 YYYY-MM-DD 字符串，不能是 date 对象"

    hotels = schema("search_hotels")
    assert hotels["check_in"].is_required() and hotels["check_out"].is_required()
    assert hotels["travelers"].default == 1

    route = schema("route")
    assert set(route) == {"origin", "destination", "mode", "city"}
    assert route["mode"].default == "transit", "路线默认公交/地铁路由"
    assert not route["city"].is_required()

    assert schema("geocode")["address"].is_required()
    assert not schema("geocode")["city"].is_required()
    assert schema("poi_detail")["poi_id"].is_required()

    # 单条结果的工具不该暴露 top_n（暴露了模型会以为能拿到更多）
    for name in ("geocode", "poi_detail", "route"):
        assert "top_n" not in schema(name)


# ======================================================================
# 3. 工具确实调用传入的 hub（不自己 new）
# ======================================================================


def test_工具调用传入的_hub_而不是自己新建(monkeypatch, hub):
    def _boom(*args, **kwargs):  # pragma: no cover - 只在实现写错时被调用
        raise AssertionError("工具不允许自己 new ProviderHub")

    monkeypatch.setattr(travel_tools, "ProviderHub", _boom)
    tools = build_travel_tools(hub)

    _invoke(tools, "search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"})
    _invoke(tools, "search_flights", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"})

    assert [name for name, _, _ in hub.calls] == ["search_trains", "search_flights"]

    # 传参必须落到 hub 的真实签名上（位置参数顺序错了这里会直接 TypeError）
    _, args, kwargs = hub.calls[0]
    assert args == ("北京", "成都", "2026-10-01")
    assert kwargs["limit"] == 10


def test_关键字参数与可选参数正确透传(hub, tools):
    _invoke(tools, "search_flights", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-02", "travelers": 3})
    _invoke(tools, "search_hotels", {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-03"})
    _invoke(tools, "search_poi", {"keywords": "火锅", "region": "成都", "types": "050000"})
    _invoke(tools, "route", {"origin": "104.06,30.67", "destination": "104.07,30.66", "city": "成都"})
    _invoke(tools, "web_search", {"query": "熊猫基地 开放时间", "top_n": 3})

    recorded = {name: (args, kwargs) for name, args, kwargs in hub.calls}
    assert recorded["search_flights"][1] == {"travelers": 3}
    assert recorded["search_poi"][1] == {"types": "050000"}
    assert recorded["route"][1] == {"city": "成都"}
    # web_search 的 top_n 要变成 max_results 传给 hub（否则截断与请求条数脱节）
    assert recorded["web_search"][1] == {"max_results": 3}


# ======================================================================
# 4. 截断
# ======================================================================


def test_超长结果被裁到_top_n(tools):
    payload = _invoke(
        tools, "search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}
    )
    assert payload["status"] == "OK"
    assert payload["total"] == 30
    assert payload["shown"] == DEFAULT_TOP_N
    assert payload["truncated"] is True
    assert len(payload["items"]) == DEFAULT_TOP_N

    first = payload["items"][0]
    # 关键字段（价格/车次/时间/名称）必须留下，否则模型会开始编
    assert first["train_no"] == "G1000"
    assert first["price_range"] == {"min": 780.5, "max": 1200.0}
    assert first["price"] == 780.5
    assert first["departure_at"] == "2026-10-01T08:00:00"
    assert first["duration_minutes"] == 450


def test_top_n_可以调大但被上限夹住(tools):
    payload = _invoke(
        tools,
        "search_trains",
        {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01", "top_n": 12},
    )
    assert len(payload["items"]) == 12

    huge = _invoke(
        tools,
        "search_poi",
        {"keywords": "景点", "region": "成都", "top_n": 1000},
    )
    assert len(huge["items"]) == MAX_TOP_N, "LLM 传了 1000 也不能把上下文拉爆"

    negative = _invoke(
        tools,
        "search_poi",
        {"keywords": "景点", "region": "成都", "top_n": -3},
    )
    assert len(negative["items"]) == DEFAULT_TOP_N, "非法 top_n 退回默认值而不是报错中断"


def test_search_trains_同参数重复查询命中缓存(tools, hub):
    """同 run 内相同 (出发, 到达, 日期) 只打一次 Provider，第二次从缓存按当前 top_n 重新切片。"""
    first = _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    second = _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    assert second == first
    assert [name for name, _, _ in hub.calls].count("search_trains") == 1


def test_search_trains_参数归一化后命中同一缓存(tools, hub):
    """两端名带空白/全角空格、日期带空格时，与已查过的键等价，不重复打 Provider。"""
    _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    _invoke(tools, "search_trains", {"origin": " 重庆 ", "destination": "成都\u3000", "depart_date": " 2026-10-01 "})
    assert [name for name, _, _ in hub.calls].count("search_trains") == 1


def test_search_trains_top_n不同则命中同一缓存按各自条数切片(tools, hub):
    """top_n 不同 → 命中同一份 Provider 缓存（hub 只打一次），按当前 top_n 各自切片。

    缓存存的是完整 ProviderResult 而非渲染文本：top_n=5 先查、top_n=20 再查时，后者
    不重新打 Provider，total 仍是全量条数、shown=20。
    """
    small = _invoke(
        tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01", "top_n": 5}
    )
    large = _invoke(
        tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01", "top_n": 20}
    )
    assert len(small["items"]) == 5
    assert len(large["items"]) == 20
    assert large["total"] == 30, "total 是 Provider 全量条数，不随 top_n 变化"
    assert large["shown"] == 20
    assert large["truncated"] is True
    assert small["items"][:5] == large["items"][:5], "同一份 Provider 结果，小 top_n 是大 top_n 的前缀"
    assert [name for name, _, _ in hub.calls].count("search_trains") == 1, "不同 top_n 共享同一份 Provider 结果"


def test_search_trains_返回可用车站汇总(tools):
    """available_stations 把本次车次的发到站去重汇总，Agent 一次看全、不用挨个试站。"""
    payload = _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    # 测试替身的车次固定发到站是 北京西 / 成都东（见 _trains），机制断言去重汇总生效
    assert payload["available_stations"] == {"origin": ["北京西"], "destination": ["成都东"]}
    assert "available_stations" in payload["hint"]


def test_search_trains_失败结果不缓存(tools, hub, monkeypatch):
    """取数失败返回可读失败且不写缓存：下次同参数还会重试，不会把失败当结果。"""
    real = hub.search_trains

    def _explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hub, "search_trains", _explode)
    first = _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    assert first["status"] == "ERROR"
    monkeypatch.setattr(hub, "search_trains", real)
    second = _invoke(tools, "search_trains", {"origin": "重庆", "destination": "成都", "depart_date": "2026-10-01"})
    assert second["status"] == "OK", "失败没被缓存，恢复后同参数能重新取到数据"


def test_长文本被截断(tools):
    payload = _invoke(tools, "search_xiaohongshu", {"keyword": "成都三日游"})
    assert payload["total"] == 9 and payload["shown"] == DEFAULT_TOP_N
    for item in payload["items"]:
        assert len(item["text"]) <= travel_tools.MAX_TEXT_CHARS + 1  # +1 是省略号
        assert item["title"] and item["place_mentions"]


def test_截断函数可单独调用():
    items = [{"name": f"点位{index}", "price": index, "junk": "x" * 5000} for index in range(40)]

    kept = truncate_items(items, top_n=5, fields=("name", "price"))
    assert len(kept) == 5
    assert kept[0] == {"name": "点位0", "price": 0}
    assert "junk" not in kept[0], "白名单外字段不应进入上下文"

    # 空输入 / None / 0 条都不炸
    assert truncate_items(None) == []
    assert truncate_items([]) == []


def test_truncate_result_保留状态与错误():
    result = ProviderResult(status="EMPTY", items=[], provider="12306", error="这天没车")
    envelope = truncate_result(result)
    assert envelope["status"] == "EMPTY" and envelope["error"] == "这天没车"
    assert envelope["items"] == [] and envelope["truncated"] is False

    assert truncate_result(None)["shown"] == 0


def test_clamp_top_n():
    assert clamp_top_n(None) == DEFAULT_TOP_N
    assert clamp_top_n("abc") == DEFAULT_TOP_N
    assert clamp_top_n(0) == DEFAULT_TOP_N
    assert clamp_top_n(3) == 3
    assert clamp_top_n(999) == MAX_TOP_N


# ======================================================================
# 5. 失败语义：不抛异常、不伪造数据
# ======================================================================


def test_hub_抛异常时返回可读失败而不是抛错(tools, hub, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("12306 连接被重置")

    monkeypatch.setattr(hub, "search_trains", _explode)
    tools = build_travel_tools(hub)

    payload = _invoke(
        tools, "search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}
    )
    assert payload["status"] == "ERROR"
    assert "12306 连接被重置" in payload["error"]
    assert payload["items"] == []
    assert "search_trains" in payload["error"], "报错要带工具名，便于定位是哪一步失败"


def test_空结果如实返回不伪造():
    hub = FakeHub(status="EMPTY", error="当日无航班")
    payload = _invoke(
        build_travel_tools(hub),
        "search_flights",
        {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"},
    )
    assert payload["status"] == "EMPTY"
    assert payload["items"] == []
    assert payload["error"] == "当日无航班"
    assert payload["hint"], "失败结果要提示'这不等于查无此项'，避免模型据此下结论"
