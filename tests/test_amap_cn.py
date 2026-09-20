"""amap_cn Plugin 的单元测试。

**不联网**：所有 HTTP 都被替身拦住 —— ``_get`` 的两条分支（module 级
``httpx.get`` 与强制 IPv4 的 ``httpx.Client().get``）连同 ``httpx.HTTPTransport``
一起被 monkeypatch 掉，测试只验证「参数怎么拼、key 从哪来、返回怎么解析、
失败怎么映射、key 有没有泄漏」。

模块加载方式与 PluginLoader 完全一致（``spec_from_file_location`` 按文件路径加载），
所以这里也按路径加载 —— 顺便验证了「tool 模块不能 import 同 Plugin 其它模块」这条约束真的成立。

fixture 里的 JSON 是**实测样本**（v3 geocode / v5 place/text / v5 place/detail /
v5 driving / v5 walking / v5 transit integrated），不是编出来的理想结构 ——
字段名、字符串数字（"2385"、"815"、"3.0"）、空串（"" / []）都按原样保留。
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from datetime import datetime
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = (
    REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins" / "amap_cn"
)
TOOLS_DIR = PLUGIN_DIR / "tools"

MODULE_NAMES = ("geocode", "poi", "route")

EXPECTED_TOOL_NAMES = {"geocode", "search_poi", "get_poi_detail", "route"}

FAKE_KEY = "amap-test-key-0000deadbeef"

#: 真实 key 只出现在项目 .env 里，绝不允许出现在 Plugin 源码 / 测试源码里。
ENV_KEY_NAMES = ("AMAP_API_KEY",)


# ==================================================
# 加载 / 替身
# ==================================================

def _load_module(name: str):
    """按文件路径加载 plugin 内的 tool 模块（与 PluginLoader 的做法一致）。"""
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"amap_cn_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        # payload 是异常时模拟 response.json() 抛错（返回体不是 JSON）；
        # 传输层异常由 FakeHTTP.__call__ 直接抛，不会走到这里。
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _RecordingStub:
    """替身的公共部分：记录 ``(url, params, timeout)``、按顺序吐预置响应。

    插件 ``_get`` 有两条分支，实际走哪条由 ``AMAP_FORCE_IPV4`` 决定：
    默认（强制 IPv4）走 ``with httpx.Client(...) as client: client.get(...)``，
    关闭后走 module 级 ``httpx.get(...)``。两条路径共用这一份逻辑，
    所以记录下来的调用形态完全一致（``url`` / ``params`` / ``timeout``）。
    """

    def __init__(self, *payloads, status_code: int = 200):
        self.payloads = list(payloads) or [{}]
        self.status_code = status_code
        self.calls: list[dict] = []

    def _send(self, url, params=None, timeout=None):
        """唯一一处记录 + 吐响应的地方。"""
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})

        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        if isinstance(payload, httpx.HTTPError):
            # 传输层错误：httpx 自己抛
            raise payload
        return FakeResponse(payload, self.status_code)

    @property
    def last(self) -> dict:
        return self.calls[-1]

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


class FakeClient:
    """``httpx.Client`` 的替身：上下文管理器 + ``.get``，复用 owner 的记录逻辑。

    插件以 ``httpx.Client(transport=..., timeout=...)`` 构造，这里按原样收下 kwargs。
    client 级 timeout 在请求没单独给时生效（与 httpx 的语义一致），
    这样 ``fake.last["timeout"]`` 在两条分支上都等于 ``DEFAULT_TIMEOUT``。
    """

    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        self.transport = kwargs.get("transport")
        self._timeout = kwargs.get("timeout")
        self.entered = False
        self.closed = False
        owner.clients.append(self)  # 反证"确实走了 Client 分支"

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False  # 不吞异常：传输层错误要照样抛给插件

    def get(self, url, params=None, timeout=None, **kwargs):
        return self._owner._send(url, params, self._timeout if timeout is None else timeout)


class FakeTransport:
    """``httpx.HTTPTransport`` 的替身：什么都不建（绝不碰真 socket）。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class FakeHTTP(_RecordingStub):
    """``httpx.get`` / ``httpx.Client`` 的替身：记录调用、按顺序吐出预置响应。"""

    def __init__(self, *payloads, status_code: int = 200):
        super().__init__(*payloads, status_code=status_code)
        self.clients: list[FakeClient] = []

    def __call__(self, url, params=None, timeout=None, **kwargs):
        return self._send(url, params, timeout)


class ForbiddenHTTP:
    """一旦被调用就炸：用于断言"这种情况根本不该发请求"。

    两条分支都会走到这里：直接调用是 module 级 ``httpx.get``，经 ``clients`` 里的
    ``FakeClient.get`` 则是强制 IPv4 分支 —— 无论哪条都不允许真的发出去。
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.clients: list[FakeClient] = []

    def _send(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        raise AssertionError(f"不应该发起 HTTP 请求，但请求了：{url}")

    def __call__(self, url, params=None, timeout=None, **kwargs):
        return self._send(url, params, timeout)


def _patch_http(monkeypatch, fake) -> None:
    """堵死 httpx 的三条真出口：module 级 ``get``、``Client``、``HTTPTransport``。"""
    monkeypatch.setattr(httpx, "get", fake)
    monkeypatch.setattr(httpx, "HTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: FakeClient(fake, *args, **kwargs))


def _install(monkeypatch, *payloads, key=FAKE_KEY, status_code: int = 200) -> FakeHTTP:
    if key is None:
        monkeypatch.delenv("AMAP_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AMAP_API_KEY", key)

    fake = FakeHTTP(*payloads, status_code=status_code)
    _patch_http(monkeypatch, fake)
    return fake


def _forbid(monkeypatch, key=FAKE_KEY) -> ForbiddenHTTP:
    if key is None:
        monkeypatch.delenv("AMAP_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AMAP_API_KEY", key)

    fake = ForbiddenHTTP()
    _patch_http(monkeypatch, fake)
    return fake


def _out(raw: str) -> dict:
    return json.loads(raw)


# ==================================================
# 实测样本（原样取自真实返回）
# ==================================================

#: v3 geocode/geo 实测返回。
GEOCODE_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "1",
    "geocodes": [
        {
            "formatted_address": "四川省成都市武侯区武侯祠(公交站)",
            "country": "中国",
            "province": "四川省",
            "citycode": "028",
            "city": "成都市",
            "district": "武侯区",
            "township": [],
            "neighborhood": {"name": [], "type": []},
            "adcode": "510107",
            "location": "104.048655,30.644240",
            "level": "公交地铁站点",
        }
    ],
}

#: v5 place/text 实测返回（business / photos 是 show_fields 要求才有的字段）。
POI_TEXT_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "2",
    "pois": [
        {
            "id": "B001C07VJ2",
            "name": "成都武侯祠博物馆",
            "address": "武侯祠大街231号",
            "location": "104.047992,30.646168",
            "type": "科教文化服务;博物馆;博物馆",
            "typecode": "140100",
            "pname": "四川省",
            "cityname": "成都市",
            "adname": "武侯区",
            "citycode": "028",
            "adcode": "510107",
            "business": {
                "opentime_today": "08:30-18:30",
                "opentime_week": "周一至周日 08:30-18:30 最晚进入17:30",
                "rating": "4.8",
                "business_area": "高升桥",
                "alias": "武侯祠|武侯祠景区",
                "tel": "028-85535951",
                "keytag": "4A景区",
                "rectag": "刘备和诸葛亮合祀",
            },
            "photos": [
                {"title": "", "url": "http://store.is.autonavi.com/showpic/2a74b5da7192e4628a9bdcc368e002ac"},
                {"title": "", "url": "http://store.is.autonavi.com/showpic/96a6372e5faa32096847b76b5a59af7c"},
            ],
        }
    ],
}

#: v5 place/detail 实测返回（未知 id 时 pois 为空列表，status 仍是 "1"）。
POI_DETAIL_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "1",
    "pois": [
        {
            "parent": "",
            "address": "武侯祠大街231号",
            "business": {
                "opentime_today": "08:30-18:30",
                "keytag": "4A景区",
                "rating": "4.8",
                "business_area": "高升桥",
                "alias": "武侯祠|武侯祠景区",
                "tel": "028-85535951",
                "opentime_week": "周一至周日 08:30-18:30 最晚进入17:30",
            },
            "pcode": "510000",
            "adcode": "510107",
            "pname": "四川省",
            "cityname": "成都市",
            "type": "科教文化服务;博物馆;博物馆",
            "photos": [{"title": "", "url": "http://store.is.autonavi.com/showpic/d60e252c0c13b260465aeba6b146c832"}],
            "typecode": "140100",
            "adname": "武侯区",
            "citycode": "028",
            "name": "成都武侯祠博物馆",
            "location": "104.047992,30.646168",
            "id": "B001C07VJ2",
        }
    ],
}

#: v5 direction/driving 实测返回（有两条候选；distance 米、cost.duration 秒，都是字符串）。
DRIVING_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "2",
    "route": {
        "origin": "104.048655,30.644240",
        "destination": "104.066,30.657",
        "taxi_cost": "8",
        "paths": [
            {
                "distance": "2385",
                "restriction": "0",
                "cost": {"duration": "759", "tolls": "0", "toll_distance": "0", "traffic_lights": "10"},
                "steps": [
                    {
                        "instruction": "沿武侯祠大街途径通祠路、文翁路向东北行驶1.9千米右转",
                        "road_name": "武侯祠大街",
                        "step_distance": "1898",
                        "cost": {"duration": "609", "tolls": "0"},
                    },
                    {
                        "instruction": "沿西御街途径东御街向东行驶487米到达目的地",
                        "road_name": "西御街",
                        "step_distance": "487",
                        "cost": {"duration": "150", "tolls": "0"},
                    },
                ],
            },
            {
                "distance": "2744",
                "restriction": "0",
                "cost": {"duration": "940", "tolls": "0", "traffic_lights": "12"},
                "steps": [],
            },
        ],
    },
}

#: v5 direction/walking 实测返回（不带 show_fields 也返回 cost.duration）。
WALKING_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "1",
    "route": {
        "origin": "104.048655,30.644240",
        "destination": "104.066,30.657",
        "paths": [
            {
                "distance": "2458",
                "cost": {"duration": "1966"},
                "steps": [
                    {
                        "instruction": "沿武侯祠大街向东步行86米向左前方行走",
                        "orientation": "东",
                        "road_name": "武侯祠大街",
                        "step_distance": "86",
                    },
                    {
                        "instruction": "沿文翁路辅路向东北步行1567米向左前方行走",
                        "orientation": "东北",
                        "road_name": "文翁路辅路",
                        "step_distance": "1567",
                    },
                ],
            }
        ],
    },
}

#: v5 direction/transit/integrated 实测返回（两条候选，第一条是步行 + 旅游专线）。
TRANSIT_PAYLOAD = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "route": {
        "origin": "104.048655,30.644240",
        "destination": "104.066,30.657",
        "distance": "3078",
        "cost": {"taxi_fee": "8"},
        "transits": [
            {
                "cost": {"duration": "1778", "transit_fee": "3.0"},
                "distance": "3145",
                "walking_distance": "472",
                "nightflag": "0",
                "segments": [
                    {
                        "walking": {
                            "destination": "104.050682,30.644861",
                            "distance": "209",
                            "origin": "104.048660,30.644211",
                            "cost": {"duration": "193"},
                            "steps": [{"instruction": "沿武侯祠大街步行209米到达武侯祠(锦里)", "road": "武侯祠大街", "distance": "209"}],
                        },
                        "bus": {
                            "buslines": [
                                {
                                    "departure_stop": {"name": "武侯祠(锦里)", "id": "900000256138001", "location": "104.050690,30.644862"},
                                    "arrival_stop": {"name": "天府广场东", "id": "900000256138003", "location": "104.067457,30.657639"},
                                    "name": "蓉城观光7号线(怀旧巴士)(武侯祠(锦里)--宽窄巷子)",
                                    "id": "900000256138",
                                    "type": "旅游专线",
                                    "distance": "2673",
                                    "cost": {"duration": "1323"},
                                    "via_num": "1",
                                }
                            ]
                        },
                    },
                    {
                        "walking": {
                            "destination": "104.066002,30.657005",
                            "distance": "263",
                            "origin": "104.067459,30.657639",
                            "cost": {"duration": "262"},
                        }
                    },
                ],
            },
            {
                "cost": {"duration": "2145", "transit_fee": "2.0"},
                "distance": "3621",
                "walking_distance": "813",
                "segments": [{"walking": {"distance": "170", "cost": {"duration": "157"}}}],
            },
        ],
    },
}

#: 跨城公交方案的 railway / taxi 段（实测样本，字段名照抄）。
RAILWAY_SEGMENT = {
    "railway": {
        "id": "101005044392",
        "time": "10080",
        "name": "Z588(成都东-重庆北)",
        "trip": "Z588",
        "distance": "316391",
        "type": "Z字头直达特快",
        "departure_stop": {"name": "成都东", "location": "103.979102 30.685329", "time": "2036", "start": "1"},
        "arrival_stop": {"name": "重庆北", "location": "106.550953 29.609378", "time": "2324", "end": "0"},
        "spaces": [{"code": "硬卧", "cost": "96.5"}, {"code": "软卧", "cost": "146.5"}],
    }
}

TAXI_SEGMENT = {
    "taxi": {
        "distance": "9231",
        "price": "",
        "drivetime": "1260",
        "startpoint": "106.550953,29.609378",
        "startname": "重庆北",
        "endpoint": "106.550000,29.560000",
        "endname": "",
    }
}


# ==================================================
# 替身必须覆盖 _get 的两条分支（回归）
# ==================================================

class TestStubCoversBothGetBranches:
    """``_get`` 有两条分支，走哪条由 ``AMAP_FORCE_IPV4`` 决定。

    默认是**强制 IPv4**，即 ``with httpx.Client(...) as client: client.get(...)``；
    关掉之后才走 module 级 ``httpx.get``。历史缺陷：替身只 patch 了 ``httpx.get``，
    默认分支于是真的联网（实测约 107s、36 个 AUTH_ERROR）。
    这里两条分支各测一次，并明确指出走的是哪条 —— 只 patch ``httpx.get`` 会失败。
    """

    def test_ipv4_enabled_by_default_uses_client_and_is_still_intercepted(self, monkeypatch):
        monkeypatch.delenv("AMAP_FORCE_IPV4", raising=False)  # 未设置 == 默认强制 IPv4
        geocode = _load_module("geocode")
        fake = _install(monkeypatch, GEOCODE_PAYLOAD)

        out = _out(geocode.geocode.invoke({"address": "武侯祠", "city": "成都"}))

        assert out["status"] == "OK"
        assert len(fake.calls) == 1, "默认分支的请求没有被替身拦住"
        assert fake.last["params"]["key"] == FAKE_KEY
        assert fake.last["params"]["address"] == "武侯祠"
        assert fake.last["timeout"] == geocode.DEFAULT_TIMEOUT
        assert "restapi.amap.com" in fake.last["url"]
        # 走的是 Client 分支：只 patch httpx.get 时这里会是空的（且真的发出去）
        assert len(fake.clients) == 1, "默认分支没有经过 httpx.Client，替身覆盖不完整"
        assert fake.clients[0].entered and fake.clients[0].closed
        assert isinstance(fake.clients[0].transport, FakeTransport)

    @pytest.mark.parametrize("value", ["1", "true", "yes"])  # 除了 "0"/"false"/"no" 都算强制
    def test_explicit_force_ipv4_also_uses_client(self, monkeypatch, value):
        monkeypatch.setenv("AMAP_FORCE_IPV4", value)
        geocode = _load_module("geocode")
        fake = _install(monkeypatch, GEOCODE_PAYLOAD)

        out = _out(geocode.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "OK"
        assert len(fake.calls) == 1
        assert len(fake.clients) == 1

    def test_ipv4_disabled_uses_module_level_get_and_is_still_intercepted(self, monkeypatch):
        monkeypatch.setenv("AMAP_FORCE_IPV4", "0")
        geocode = _load_module("geocode")
        fake = _install(monkeypatch, GEOCODE_PAYLOAD)

        out = _out(geocode.geocode.invoke({"address": "武侯祠", "city": "成都"}))

        assert out["status"] == "OK"
        assert len(fake.calls) == 1, "httpx.get 分支的请求没有被替身拦住"
        assert fake.last["params"]["key"] == FAKE_KEY
        assert fake.last["timeout"] == geocode.DEFAULT_TIMEOUT
        assert fake.clients == [], "AMAP_FORCE_IPV4=0 时不该构造 Client"

    def test_forbidden_stub_also_guards_the_client_path(self, monkeypatch):
        """``_forbid`` 的替身必须同样堵住强制 IPv4 分支，否则"不许发请求"是假的。"""
        monkeypatch.delenv("AMAP_FORCE_IPV4", raising=False)
        fake = _forbid(monkeypatch)

        with pytest.raises(AssertionError):
            with httpx.Client(transport=FakeTransport(), timeout=1) as client:
                client.get("https://restapi.amap.com/v3/geocode/geo", params={"key": FAKE_KEY})

        assert len(fake.calls) == 1
        assert fake.calls[0]["params"]["key"] == FAKE_KEY


# ==================================================
# 归一化（纯函数，不发请求）
# ==================================================

class TestNormalization:
    def test_location_string_becomes_float_pair(self):
        poi = _load_module("poi")

        assert poi._split_location("104.047992,30.646168") == (104.047992, 30.646168)
        # 尾分号是高德对多坐标的写法，单点也带过，必须容忍
        assert poi._split_location("104.047992,30.646168;") == (104.047992, 30.646168)

    @pytest.mark.parametrize("raw", ["", "abc", "104.05", "104.05,30.64,31.0", "104.05,abc", None, [], 104.05])
    def test_bad_location_is_none_not_zero(self, raw):
        """解析不出来必须是 None —— 退化成 0.0 会得到一个"合法"的错误坐标。"""
        poi = _load_module("poi")
        assert poi._split_location(raw) is None

    def test_out_of_range_location_is_none(self):
        poi = _load_module("poi")
        assert poi._split_location("200.0,30.0") is None
        assert poi._split_location("104.0,95.0") is None

    def test_geocode_normalization(self, monkeypatch):
        geocode = _load_module("geocode")
        fake = _install(monkeypatch, GEOCODE_PAYLOAD)

        out = _out(geocode.geocode.invoke({"address": "武侯祠", "city": "成都"}))
        item = out["items"][0]

        assert out["status"] == "OK"
        assert item["longitude"] == 104.048655
        assert item["latitude"] == 30.644240
        assert item["formatted_address"] == "四川省成都市武侯区武侯祠(公交站)"
        assert item["province"] == "四川省"
        assert item["city"] == "成都市"
        assert item["district"] == "武侯区"
        assert item["adcode"] == "510107"
        assert item["citycode"] == "028"
        assert item["level"] == "公交地铁站点"
        # 坐标是浮点数，不是字符串
        assert isinstance(item["longitude"], float)
        assert fake.last["params"]["address"] == "武侯祠"

    def test_poi_normalization(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, POI_TEXT_PAYLOAD)

        out = _out(poi.search_poi.invoke({"keywords": "博物馆", "region": "成都"}))
        item = out["items"][0]

        assert item["poi_id"] == "B001C07VJ2"
        assert item["name"] == "成都武侯祠博物馆"
        assert item["address"] == "武侯祠大街231号"
        assert item["type"] == "科教文化服务;博物馆;博物馆"
        assert item["typecode"] == "140100"
        assert item["longitude"] == 104.047992
        assert item["latitude"] == 30.646168
        assert item["city"] == "成都市"
        assert item["district"] == "武侯区"
        assert item["province"] == "四川省"
        # business.rating 是字符串 "4.8"
        assert item["rating"] == 4.8
        # 优先整周规律，不是"今天"
        assert item["opening_hours"] == "周一至周日 08:30-18:30 最晚进入17:30"
        assert item["telephone"] == "028-85535951"
        assert item["business_area"] == "高升桥"
        assert item["tags"] == ["4A景区", "刘备和诸葛亮合祀"]
        assert len(item["photos"]) == 2
        assert item["photos"][0].startswith("http://store.is.autonavi.com/showpic/")

    def test_missing_business_fields_are_null_not_invented(self):
        """没有 show_fields=business 的 POI：评分/营业时间必须是 null，不许补造。"""
        poi = _load_module("poi")
        item = poi._normalize({"id": "B0X", "name": "某小馆", "location": "104.05,30.64", "address": "", "business": {}})

        assert item["rating"] is None
        assert item["opening_hours"] is None
        assert item["telephone"] is None
        assert item["tags"] is None
        assert item["photos"] is None
        # 空串地址 → None（不是 ""）
        assert item["address"] is None
        # 基础字段仍然解析出来
        assert item["longitude"] == 104.05

    def test_poi_detail_uses_same_shape_as_search(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, POI_DETAIL_PAYLOAD)

        detail = _out(poi.get_poi_detail.invoke({"poi_id": "B001C07VJ2"}))

        assert detail["status"] == "OK"
        assert set(detail["items"][0]) == set(poi._normalize({}))
        assert detail["items"][0]["rating"] == 4.8

    @pytest.mark.parametrize(
        "raw, expected",
        [("2385", 2385), ("0", 0), (759, 759), ("759.0", 759), ("", None), (None, None), ("暂无", None)],
    )
    def test_to_int(self, raw, expected):
        route = _load_module("route")
        assert route._to_int(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [("3.0", 3.0), ("76.500000", 76.5), ("830", 830.0), ("0", 0.0), ("", None), (None, None)],
    )
    def test_to_float(self, raw, expected):
        route = _load_module("route")
        assert route._to_float(raw) == expected


# ==================================================
# 信封契约
# ==================================================

class TestEnvelope:
    def test_envelope_contract(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, POI_TEXT_PAYLOAD)

        out = _out(poi.search_poi.invoke({"keywords": "博物馆", "region": "成都"}))

        assert set(out) >= {"status", "provider", "fetched_at", "query", "count", "items", "error"}
        assert out["provider"] == "amap"
        assert out["status"] == "OK"
        assert out["error"] is None
        assert out["count"] == len(out["items"]) == 1
        # 上游命中总数与本页条数分开
        assert out["total"] == 2
        # 带时区的 ISO8601
        assert datetime.fromisoformat(out["fetched_at"]).tzinfo is not None

    def test_chinese_is_not_escaped(self, monkeypatch):
        """ensure_ascii=False：中文直接进字符串，下游不必二次解码。"""
        geocode = _load_module("geocode")
        _install(monkeypatch, GEOCODE_PAYLOAD)

        raw = geocode.geocode.invoke({"address": "武侯祠", "city": "成都"})

        assert "武侯祠" in raw
        assert "\\u" not in raw

    def test_query_echoes_effective_paging(self, monkeypatch):
        poi = _load_module("poi")
        fake = _install(monkeypatch, POI_TEXT_PAYLOAD)

        out = _out(poi.search_poi.invoke({"keywords": "博物馆", "region": "成都", "page": 2, "page_size": 99}))

        # 超过高德 v5 单页上限按 25 处理，回显的必须是**生效值**而不是入参
        assert out["query"]["page_size"] == 25
        assert fake.last["params"]["page_size"] == 25
        assert fake.last["params"]["page_num"] == 2

    def test_empty_region_and_types_are_not_sent(self, monkeypatch):
        poi = _load_module("poi")
        fake = _install(monkeypatch, POI_TEXT_PAYLOAD)

        poi.search_poi.invoke({"keywords": "博物馆", "region": ""})
        assert "region" not in fake.last["params"]

        poi.search_poi.invoke({"keywords": "博物馆", "region": "成都", "types": "140100"})
        assert fake.last["params"]["types"] == "140100"
        assert fake.last["params"]["show_fields"] == "business,photos"


# ==================================================
# 失败映射
# ==================================================

class TestFailureMapping:
    def test_invalid_user_key_maps_to_auth_error(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})

        out = _out(poi.search_poi.invoke({"keywords": "博物馆", "region": "成都"}))

        assert out["status"] == "AUTH_ERROR"
        assert "INVALID_USER_KEY" in out["error"]
        assert out["items"] == []
        assert out["count"] == 0

    def test_invalid_user_scode_maps_to_auth_error(self, monkeypatch):
        geocode = _load_module("geocode")
        _install(monkeypatch, {"status": "0", "info": "INVALID_USER_SCODE", "infocode": "10003"})

        out = _out(geocode.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "AUTH_ERROR"

    @pytest.mark.parametrize(
        "info, expected",
        [
            ("DAILY_QUERY_OVER_LIMIT", "RATE_LIMIT"),
            ("CUQPS_HAS_EXCEEDED_THE_LIMIT", "RATE_LIMIT"),
            ("MISSING_REQUIRED_PARAMS", "UNAVAILABLE"),
            ("OVER_DIRECTION_RANGE", "UNAVAILABLE"),
            ("ENGINE_RESPONSE_DATA_ERROR", "UNAVAILABLE"),
        ],
    )
    def test_info_mapping(self, monkeypatch, info, expected):
        route = _load_module("route")
        _install(monkeypatch, {"status": "0", "info": info, "infocode": "20000"})

        out = _out(route.route.invoke(
            {"origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "driving"}
        ))

        assert out["status"] == expected
        assert info in out["error"]
        # 失败时顶层路径字段也在（值为 null），调用方不用先判断 key 是否存在
        assert out["distance_meters"] is None
        assert out["duration_seconds"] is None
        assert out["estimated_cost"] is None

    def test_rate_limit_over_daily_quota(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10003"})

        out = _out(poi.search_poi.invoke({"keywords": "博物馆", "region": "成都"}))

        assert out["status"] == "RATE_LIMIT"
        assert "配额" in out["error"]

    def test_timeout_is_retried_then_maps_to_timeout(self, monkeypatch):
        route = _load_module("route")
        fake = _install(monkeypatch, httpx.ReadTimeout("模拟超时"))

        out = _out(route.route.invoke(
            {"origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "driving"}
        ))

        assert out["status"] == "TIMEOUT"
        assert len(fake.calls) == route.MAX_ATTEMPTS == 3

    def test_non_json_body_maps_to_invalid_response(self, monkeypatch):
        geo = _load_module("geocode")
        fake = _install(monkeypatch, ValueError("not json"))

        out = _out(geo.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "INVALID_RESPONSE"
        assert len(fake.calls) == 1  # 解析失败不重试

    def test_json_array_body_maps_to_invalid_response(self, monkeypatch):
        geo = _load_module("geocode")
        # 替身返回 list：response.json() 会给 list，不是 dict
        _install(monkeypatch, ["unexpected"])

        out = _out(geo.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "INVALID_RESPONSE"

    def test_http_500_maps_to_unavailable(self, monkeypatch):
        geo = _load_module("geocode")
        _install(monkeypatch, {"anything": 1}, status_code=500)

        out = _out(geo.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "UNAVAILABLE"
        assert "500" in out["error"]

    def test_empty_result_maps_to_empty(self, monkeypatch):
        poi = _load_module("poi")
        _install(monkeypatch, {"status": "1", "info": "OK", "count": "0", "pois": []})

        out = _out(poi.search_poi.invoke({"keywords": "zzz", "region": "成都"}))

        assert out["status"] == "EMPTY"
        assert out["items"] == []
        assert out["count"] == 0
        assert out["error"] is None

    def test_unknown_detail_id_maps_to_empty(self, monkeypatch):
        """实测：未知 poi_id 时高德返回 status=1 + pois=[]，不是报错。"""
        poi = _load_module("poi")
        _install(monkeypatch, {"status": "1", "info": "OK", "count": "0", "pois": []})

        out = _out(poi.get_poi_detail.invoke({"poi_id": "NOT_EXIST_ID_XYZ"}))

        assert out["status"] == "EMPTY"

    def test_connection_error_maps_to_unavailable(self, monkeypatch):
        geo = _load_module("geocode")
        _install(monkeypatch, httpx.ConnectError("boom"))

        out = _out(geo.geocode.invoke({"address": "武侯祠"}))

        assert out["status"] == "UNAVAILABLE"
        assert "ConnectError" in out["error"]


# ==================================================
# 凭据
# ==================================================

class TestCredential:
    def test_missing_key_is_auth_error_without_any_request(self, monkeypatch):
        """缺 key 必须**不发请求**：必然失败，白耗配额还会被计入风控。"""
        for name, invoke in (
            ("geocode", {"address": "武侯祠"}),
            ("poi", {"keywords": "博物馆", "region": "成都"}),
            ("route", {"origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "driving"}),
        ):
            module = _load_module(name)
            fake = _forbid(monkeypatch, key=None)

            out = _out(module.TOOLS[0].invoke(invoke))

            assert out["status"] == "AUTH_ERROR", name
            assert "AMAP_API_KEY" in out["error"]
            assert fake.calls == [], name

    def test_missing_key_for_poi_detail(self, monkeypatch):
        poi = _load_module("poi")
        fake = _forbid(monkeypatch, key=None)

        out = _out(poi.get_poi_detail.invoke({"poi_id": "B001C07VJ2"}))

        assert out["status"] == "AUTH_ERROR"
        assert fake.calls == []

    def test_key_goes_into_params_not_into_a_url_we_build(self, monkeypatch):
        geo = _load_module("geocode")
        fake = _install(monkeypatch, GEOCODE_PAYLOAD)

        geo.geocode.invoke({"address": "武侯祠", "city": "成都"})

        assert fake.last["params"]["key"] == FAKE_KEY
        assert FAKE_KEY not in fake.last["url"]
        # 明确带上 timeout：实测 urllib 会 SSL 超时，裸请求不能没有超时
        assert fake.last["timeout"] == geo.DEFAULT_TIMEOUT

    def test_key_echoed_by_upstream_is_scrubbed(self, monkeypatch):
        """上游把 key 回显进 info 时必须被擦掉 —— 返回值会进模型上下文和日志。"""
        poi = _load_module("poi")
        _install(monkeypatch, {"status": "0", "info": f"INVALID_USER_KEY key={FAKE_KEY}", "infocode": "10001"})

        raw = poi.search_poi.invoke({"keywords": "博物馆", "region": "成都"})

        assert FAKE_KEY not in raw
        assert "***" in raw
        assert _out(raw)["status"] == "AUTH_ERROR"

    def test_key_in_exception_text_is_not_leaked(self, monkeypatch):
        """httpx 异常文案里可能带完整 URL（含 key）—— 我们只取异常类名。"""
        geo = _load_module("geocode")
        _install(monkeypatch, httpx.ConnectError(f"failed to connect to https://restapi.amap.com/?key={FAKE_KEY}"))

        raw = geo.geocode.invoke({"address": "武侯祠"})

        assert FAKE_KEY not in raw
        assert _out(raw)["status"] == "UNAVAILABLE"

    def test_scrub_is_noop_without_secret(self):
        geo = _load_module("geocode")
        assert geo._scrub("nothing to hide", "") == "nothing to hide"
        assert geo._scrub(f"x={FAKE_KEY}", None) == f"x={FAKE_KEY}"

    def test_real_key_is_not_hardcoded_in_plugin_sources(self):
        """源码里不得出现项目 .env 里那把真实 key（谁抄进去谁负责）。"""
        env_file = REPO_ROOT / ".env"
        if not env_file.exists():
            pytest.skip("项目根没有 .env，无法取真实 key 做比对")

        real_key = ""
        for line in env_file.read_text(encoding="utf-8").splitlines():
            for name in ENV_KEY_NAMES:
                if line.startswith(f"{name}="):
                    real_key = line.split("=", 1)[1].strip()

        if not real_key:
            pytest.skip(f".env 里没有 {' / '.join(ENV_KEY_NAMES)}")

        for path in list(TOOLS_DIR.glob("*.py")) + [PLUGIN_DIR / "plugin.toml", Path(__file__)]:
            assert real_key not in path.read_text(encoding="utf-8"), f"{path.name} 里硬编码了真实 key"

    def test_key_read_from_env_only(self):
        for name in MODULE_NAMES:
            source = (TOOLS_DIR / f"{name}.py").read_text(encoding="utf-8")
            assert 'os.environ.get(API_KEY_ENV' in source


# ==================================================
# route：mode 分发与解析
# ==================================================

class TestRoute:
    @pytest.mark.parametrize(
        "mode, payload, path",
        [
            ("driving", DRIVING_PAYLOAD, "/v5/direction/driving"),
            ("transit", TRANSIT_PAYLOAD, "/v5/direction/transit/integrated"),
            ("walking", WALKING_PAYLOAD, "/v5/direction/walking"),
        ],
    )
    def test_mode_dispatches_to_its_own_endpoint(self, monkeypatch, mode, payload, path):
        route = _load_module("route")
        fake = _install(monkeypatch, payload)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240",
            "destination": "104.066,30.657",
            "mode": mode,
            "city": "028",
        }))

        assert out["status"] == "OK"
        assert out["mode"] == mode
        assert len(fake.calls) == 1
        assert fake.last["url"].endswith(path)
        assert fake.last["params"]["origin"] == "104.048655,30.644240"
        assert fake.last["params"]["destination"] == "104.066,30.657"
        assert fake.last["params"]["key"] == FAKE_KEY

    def test_driving_requires_show_fields_cost(self, monkeypatch):
        """实测：driving 不带 show_fields 时 paths[].cost 整个消失（duration/tolls 都没了）。"""
        route = _load_module("route")
        fake = _install(monkeypatch, DRIVING_PAYLOAD)

        route.route.invoke({"origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "driving"})

        assert fake.last["params"]["show_fields"] == "cost"
        assert "city1" not in fake.last["params"]

    def test_transit_sends_city1_city2(self, monkeypatch):
        route = _load_module("route")
        fake = _install(monkeypatch, TRANSIT_PAYLOAD)

        route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "transit", "city": "028",
        })

        assert fake.last["params"]["city1"] == "028"
        assert fake.last["params"]["city2"] == "028"

    def test_transit_without_city_does_not_request(self, monkeypatch):
        """高德会回 MISSING_REQUIRED_PARAMS，但那要花一次往返才得到同样的结论。"""
        route = _load_module("route")
        fake = _forbid(monkeypatch)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "transit",
        }))

        assert out["status"] == "UNAVAILABLE"
        assert "city" in out["error"]
        assert out["mode"] == "transit"
        assert fake.calls == []

    def test_unknown_mode_is_rejected_without_request(self, monkeypatch):
        route = _load_module("route")
        fake = _forbid(monkeypatch)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "flying",
        }))

        assert out["status"] == "UNAVAILABLE"
        assert "driving" in out["error"] and "transit" in out["error"] and "walking" in out["error"]
        assert fake.calls == []

    def test_bad_origin_is_rejected_without_request(self, monkeypatch):
        route = _load_module("route")
        fake = _forbid(monkeypatch)

        out = _out(route.route.invoke({"origin": "武侯祠", "destination": "104.066,30.657", "mode": "walking"}))

        assert out["status"] == "UNAVAILABLE"
        assert "坐标" in out["error"]
        assert fake.calls == []

    @pytest.mark.parametrize(
        "origin, expected",
        [
            # 字符串：保留调用方原始数值写法（不改写成 6 位小数，便于对账）
            ("104.05,30.64", "104.05,30.64"),
            ("104.048655,30.644240", "104.048655,30.644240"),
            # 数字数组（list / tuple）：由本工具格式化
            ([104.05, 30.64], "104.050000,30.640000"),
            ((104.05, 30.64), "104.050000,30.640000"),
        ],
    )
    def test_origin_accepts_multiple_forms(self, monkeypatch, origin, expected):
        route = _load_module("route")
        fake = _install(monkeypatch, WALKING_PAYLOAD)

        route.route.invoke({"origin": origin, "destination": "104.066,30.657", "mode": "walking"})

        assert fake.last["params"]["origin"] == expected

    @pytest.mark.parametrize(
        "origin, expected",
        [
            ({"longitude": 104.05, "latitude": 30.64}, "104.050000,30.640000"),
            ({"lng": 104.05, "lat": 30.64}, "104.050000,30.640000"),
        ],
    )
    def test_location_helper_accepts_dict_forms(self, origin, expected):
        """字典形态不在 tool 的 pydantic 注解里（JSON 不会这么传），只在程序内直接调用时出现。"""
        route = _load_module("route")
        assert route._normalize_location(origin) == expected

    @pytest.mark.parametrize("origin", ["", "abc", [1, 2, 3], {"lng": 104.05}, "999,30.64"])
    def test_unparsable_origin_never_becomes_zero(self, origin):
        """宁可失败也不把坐标退化成 0,0（几内亚湾）。"""
        route = _load_module("route")
        assert route._normalize_location(origin) is None

    def test_driving_normalization(self, monkeypatch):
        route = _load_module("route")
        _install(monkeypatch, DRIVING_PAYLOAD)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "driving",
        }))

        # PRD 规定的统一路线结构
        assert out["distance_meters"] == 2385
        assert out["duration_seconds"] == 759
        assert out["estimated_cost"] == 0.0  # 实测 tolls="0"，是"不收费"这一事实
        assert out["mode"] == "driving"
        assert out["provider"] == "amap"
        assert out["status"] == "OK"

        plan = out["items"][0]
        assert plan["cost_basis"] == "过路费"
        assert plan["taxi_cost_estimate"] == 8.0  # 高德给的打车参考价，单列不混用
        assert plan["alternatives"] == 2
        assert plan["steps"][0]["instruction"].startswith("沿武侯祠大街")
        assert plan["steps"][0]["distance_meters"] == 1898
        assert plan["steps"][0]["duration_seconds"] == 609

    def test_walking_has_no_cost(self, monkeypatch):
        route = _load_module("route")
        _install(monkeypatch, WALKING_PAYLOAD)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "walking",
        }))

        assert out["distance_meters"] == 2458
        assert out["duration_seconds"] == 1966
        # 走路不花钱，但"取不到"也不许估算
        assert out["estimated_cost"] is None
        assert out["items"][0]["cost_basis"] is None
        assert out["items"][0]["steps"][0]["road"] == "武侯祠大街"

    def test_transit_normalization(self, monkeypatch):
        route = _load_module("route")
        _install(monkeypatch, TRANSIT_PAYLOAD)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "transit", "city": "028",
        }))

        # 用第一条方案自己的距离，而不是 route.distance（实测 3078 vs 3145）
        assert out["distance_meters"] == 3145
        assert out["duration_seconds"] == 1778
        assert out["estimated_cost"] == 3.0  # cost.transit_fee="3.0"
        assert out["items"][0]["cost_basis"] == "票价"
        assert out["items"][0]["alternatives"] == 2
        assert out["items"][0]["walking_distance_meters"] == 472

        segments = out["items"][0]["segments"]
        assert [seg["type"] for seg in segments] == ["walking", "bus", "walking"]
        assert segments[1]["line"].startswith("蓉城观光7号线")
        assert segments[1]["from_stop"] == "武侯祠(锦里)"
        assert segments[1]["to_stop"] == "天府广场东"
        assert segments[1]["via_stops"] == 1

    def test_case_and_whitespace_insensitive_mode(self, monkeypatch):
        route = _load_module("route")
        _install(monkeypatch, WALKING_PAYLOAD)

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": " WALKING ",
        }))

        assert out["mode"] == "walking"
        assert out["status"] == "OK"

    def test_status_ok_without_paths_is_empty_not_error(self, monkeypatch):
        route = _load_module("route")
        _install(monkeypatch, {"status": "1", "info": "OK", "route": {"paths": []}})

        out = _out(route.route.invoke({
            "origin": "104.048655,30.644240", "destination": "104.066,30.657", "mode": "walking",
        }))

        assert out["status"] == "EMPTY"
        assert out["error"] is None
        assert out["distance_meters"] is None

    def test_railway_and_taxi_segments(self):
        """跨城公交方案的 railway / taxi 段（实测字段名）。"""
        route = _load_module("route")
        segments = route._segments([RAILWAY_SEGMENT, TAXI_SEGMENT])

        railway = segments[0]
        assert railway["type"] == "railway"
        assert railway["line"] == "Z588(成都东-重庆北)"
        assert railway["trip"] == "Z588"
        assert railway["from_stop"] == "成都东"
        assert railway["to_stop"] == "重庆北"
        # railway.time 是乘车秒数；stop.time 是 HHMM 时刻 —— 同名不同义，分开命名
        assert railway["duration_seconds"] == 10080
        assert railway["departure_time"] == "2036"
        assert railway["arrival_time"] == "2324"
        assert railway["seat_prices"] == {"硬卧": 96.5, "软卧": 146.5}

        taxi = segments[1]
        assert taxi["type"] == "taxi"
        assert taxi["duration_seconds"] == 1260
        # price 实测是空串 → None（"高德没给估价"，不是 0 元）
        assert taxi["price"] is None
        assert taxi["from_name"] == "重庆北"

    def test_steps_are_capped(self):
        route = _load_module("route")
        steps = route._steps([{"instruction": str(i), "step_distance": "1"} for i in range(100)])

        assert len(steps) == 100  # 解析不截断
        assert route.MAX_STEPS == 30  # 截断发生在渲染时


# ==================================================
# Plugin 打包 / 注册
# ==================================================

class TestPluginPackaging:
    def test_four_tools_with_expected_names(self):
        names = set()
        for name in MODULE_NAMES:
            module = _load_module(name)
            assert isinstance(module.TOOLS, list) and module.TOOLS
            names.update(tool.name for tool in module.TOOLS)

        assert names == EXPECTED_TOOL_NAMES
        assert len(names) == 4

    def test_tool_docstrings_are_chinese_and_document_args(self):
        for name in MODULE_NAMES:
            for tool in _load_module(name).TOOLS:
                assert tool.description
                assert "Args:" in tool.description
                # 中文说明（英文 docstring 的模型会在参数含义上乱猜）
                assert any("\u4e00" <= char <= "\u9fff" for char in tool.description)

    def test_modules_do_not_import_each_other(self):
        """PluginLoader 按文件路径加载，同 Plugin 内互相 import 会在加载期直接失败。"""
        for name in MODULE_NAMES:
            source = (TOOLS_DIR / f"{name}.py").read_text(encoding="utf-8")
            # 只看语句行（行首的 from ./import），文档里拿它举例不算违规
            assert not re.search(r"^\s*from \.", source, flags=re.MULTILINE)
            assert not re.search(r"^\s*import (geocode|poi|route)\b", source, flags=re.MULTILINE)
            assert not re.search(r"^\s*from ((geocode|poi|route)|amap_cn)", source, flags=re.MULTILINE)

    def test_manifest_declares_three_modules(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))

        assert manifest["plugin"]["name"] == "amap_cn"
        assert manifest["plugin"]["enabled"] is True
        assert manifest["plugin"]["version"] == "0.1.0"

        modules = manifest["tools"]["modules"]
        assert modules == [f"tools.{name}" for name in MODULE_NAMES]
        for dotted in modules:
            assert (PLUGIN_DIR / Path(*dotted.split(".")).with_suffix(".py")).exists()

        # 没有 plugin.py 也必须能加载（loader 在声明了 modules 时允许省略 entrypoint）
        assert not (PLUGIN_DIR / "plugin.py").exists()

    def test_manifest_description_reads_like_user_speech(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
        description = manifest["plugin"]["description"]

        # 会被 BM25 拿去和用户 Query 匹配，所以要含用户会说的词
        for keyword in ("经纬度", "POI", "景点", "驾车", "公交", "地铁", "步行", "坐标"):
            assert keyword in description, keyword
        assert "不下单" in description

    def test_plugin_loader_discovers_amap_cn_and_loads_four_tools(self, monkeypatch):
        """与装配期完全一致的发现 / 加载路径（只 import，不发请求）。"""
        from superharness.capabilities import PluginLoader

        monkeypatch.delenv("AMAP_API_KEY", raising=False)

        root = REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins"
        loader = PluginLoader(root)

        candidates = {candidate.name: candidate for candidate in loader.candidates()}
        assert "amap_cn" in candidates
        assert candidates["amap_cn"].id == "plugin:amap_cn"
        assert candidates["amap_cn"].kind == "plugin"

        manifest = next(item for item in loader.manifests if item.name == "amap_cn")
        tools = loader.load_tools(manifest)

        assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES
