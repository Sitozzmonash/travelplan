"""把 case 的注入参数翻译成一次**真的 Agent run** 的依赖（离线假件）。

为什么假件要自成一档，而不 import `tests/fakes.py`：`benchmark/` 与 `tests/` 都被
`.dockerignore` 排除 —— 评测资产不能依赖测试资产的可用性。所以这里自带脚本模型与假 Hub，
但**技巧完全照抄 `tests/test_agent_runner.py`**（同一套 patch 点、同一套"非规划调用不推进
剧本"的判定），否则"单测通过、评测失败"会变成无法归因的分歧。

跑的是**真的 Agent Loop**（`create_harness_agent` + LangChain 的工具调用循环），
不是对 `execute_agent_run` 的桩：被测的正是这条接线，桩掉就等于什么都没测。
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.agent_runner import SUBMIT_TOOL_NAME, execute_agent_run
from app.models import TripIntent
from app.providers import ProviderCall, ProviderResult
from app.store import TravelPlanStore

#: 假件版本。改了本文件里的世界数据 / 注入语义 / 剧本形状，**必须**改它并重跑基线，
#: 否则两份基线不是在比同一件事。
FIXTURE_VERSION = "agent_loop.fixtures.v1"

#: 主规划调用的 user prompt 里有这一句（见 `agent_runner._user_prompt`）。脚本模型用它
#: 区分"这是主规划的调用"与"这是别的模型调用"（例如长期记忆 consolidation）。
PLANNER_MARK = "请开始：先补齐缺的关键事实"

#: 固定的取数时间：假件的 fetched_at 不能取 `now()`，否则 audit / 基线不可复现。
FETCHED_AT = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)


# ======================================================================
# 固定世界（车次 / 航班 / 酒店 / 门票 / POI / 路线）
# ======================================================================
# 为什么所有数字都写成常量：判分的核心是"行程里的数字能不能指回假工具真的返回过的
# 东西"。世界必须可枚举才能对账；随机数会让"编造"与"真实"无法区分。

OUTBOUND_TRAINS: tuple[dict[str, Any], ...] = (
    {
        "train_no": "G89",
        "train_type": "高铁",
        "origin_station": "北京西",
        "destination_station": "成都东",
        "depart": "07:00",
        "arrive": "12:30",
        "duration_minutes": 330,
        "price": 780.0,
        "seats": {"二等座": 12},
    },
    {
        "train_no": "G307",
        "train_type": "高铁",
        "origin_station": "北京西",
        "destination_station": "成都东",
        "depart": "09:05",
        "arrive": "15:15",
        "duration_minutes": 370,
        "price": 760.0,
        "seats": {"二等座": 4},
    },
    {
        "train_no": "K117",
        "train_type": "普快",
        "origin_station": "北京西",
        "destination_station": "成都",
        "depart": "22:10",
        "arrive": "06:40",
        "duration_minutes": 1830,
        "price": 260.0,
        "seats": {"硬卧": 6},
    },
)

#: 回程车次刻意与去程不同：判分要能看出"回程填的是回程那趟车"，填成去程车次是可抓的错。
INBOUND_TRAIN: dict[str, Any] = {
    "train_no": "G90",
    "train_type": "高铁",
    "origin_station": "成都东",
    "destination_station": "北京西",
    "depart": "18:00",
    "arrive": "23:30",
    "duration_minutes": 330,
    "price": 780.0,
    "seats": {"二等座": 9},
}

FLIGHT: dict[str, Any] = {
    "flight_no": "CA4102",
    "airline": "国航",
    "departure_airport": "北京首都T3",
    "arrival_airport": "成都天府T1",
    "depart": "09:00",
    "arrive": "12:05",
    "duration_minutes": 185,
    "price": 1180.0,
    "is_direct": True,
    "remaining_seats": 7,
}

HOTELS: tuple[dict[str, Any], ...] = (
    {
        "hotel_id": "h1",
        "name": "成都春熙路智选假日酒店",
        "business_area": "春熙路",
        "address": "成都市锦江区红星路三段",
        "lat": 30.657,
        "lng": 104.081,
        "room_type": "标准大床房",
        "price_per_night": 420.0,
        "rating": 4.6,
        "price_note": "420 起价/晚",
    },
    {
        "hotel_id": "h2",
        "name": "成都双流机场快捷酒店",
        "business_area": "双流",
        "address": "成都市双流区机场路",
        "lat": 30.574,
        "lng": 103.925,
        "room_type": "标准双床房",
        "price_per_night": 180.0,
        "rating": 3.9,
        "price_note": "180 起价/晚",
    },
)

#: POI 注册表。坐标是 GCJ-02；`coord_of()` 给出 `route` 工具要的 "经度,纬度" 串。
POIS: tuple[dict[str, Any], ...] = (
    {
        "place_id": "B0FF1",
        "name": "宽窄巷子",
        "type": "风景名胜",
        "district": "青羊区",
        "business_area": "宽窄巷子",
        "lat": 30.669,
        "lng": 104.057,
        "opening_hours": "08:30-21:30",
    },
    {
        "place_id": "B0FF2",
        "name": "武侯祠",
        "type": "风景名胜",
        "district": "武侯区",
        "business_area": "武侯祠",
        "lat": 30.646,
        "lng": 104.043,
        "opening_hours": "08:00-18:00",
    },
    {
        "place_id": "B0FF3",
        "name": "人民公园",
        "type": "风景名胜",
        "district": "青羊区",
        "business_area": "人民公园",
        "lat": 30.662,
        "lng": 104.053,
        "opening_hours": "06:00-22:00",
    },
    {
        "place_id": "B0FF4",
        "name": "成都大熊猫繁育研究基地",
        "type": "风景名胜",
        "district": "成华区",
        "business_area": "熊猫基地",
        "lat": 30.733,
        "lng": 104.145,
        "opening_hours": "07:30-18:00",
    },
    {
        "place_id": "B0FF5",
        "name": "锦里",
        "type": "风景名胜",
        "district": "武侯区",
        "business_area": "武侯祠",
        "lat": 30.644,
        "lng": 104.039,
        "opening_hours": "09:00-22:00",
    },
    {
        "place_id": "B0FF6",
        "name": "春熙路",
        "type": "购物中心",
        "district": "锦江区",
        "business_area": "春熙路",
        "lat": 30.657,
        "lng": 104.081,
        "opening_hours": "10:00-22:00",
    },
)

#: 景区门票（按景区名）。不在表里的景区返回 EMPTY —— 假件不替上游编一个价。
TICKETS: dict[str, float] = {
    "成都大熊猫繁育研究基地": 55.0,
    "武侯祠": 50.0,
    "宽窄巷子": 0.0,
}

#: 路线耗时（分钟）。键是四舍五入到 3 位小数的 (经度, 纬度) 对，避免"4 位还是 3 位小数"
#: 这种无关差异让查表失配。不在表里的点对走 `DEFAULT_ROUTE_MINUTES`。
ROUTE_MINUTES: dict[tuple[tuple[float, float], tuple[float, float]], int] = {
    ((104.057, 30.669), (104.053, 30.662)): 19,  # 宽窄巷子 → 人民公园
    ((104.057, 30.669), (104.043, 30.646)): 25,  # 宽窄巷子 → 武侯祠
    ((104.043, 30.646), (104.039, 30.644)): 8,  # 武侯祠 → 锦里
    ((104.043, 30.646), (104.081, 30.657)): 22,  # 武侯祠 → 春熙路
    ((104.081, 30.657), (104.145, 30.733)): 45,  # 春熙路 → 熊猫基地
    # 刻意的一条长距离路线（约 28km）：时间冲突用例靠它构造"上一站 16:00 结束、
    # 下一站 16:10 开始"这种排不下的排法。
    ((104.145, 30.733), (104.043, 30.646)): 60,  # 熊猫基地 → 武侯祠
}
DEFAULT_ROUTE_MINUTES = 20

GUIDE_TEXT = (
    "成都必去：宽窄巷子、武侯祠、锦里、人民公园。"
    "美食：担担面、龙抄手、串串香、老火锅。住宿建议住春熙路附近，地铁方便。"
)

#: 假 Hub 的 provider 名（与真实链路一致，audit 里能直接对读）。
PROVIDER_BY_TOOL: dict[str, str] = {
    "search_trains": "12306",
    "search_flights": "tuniu",
    "search_hotels": "tuniu",
    "search_scenic_tickets": "tuniu",
    "geocode": "amap",
    "search_poi": "amap",
    "poi_detail": "amap",
    "route": "amap",
    "search_xiaohongshu": "tikhub",
    "search_douyin": "tikhub",
    "web_search": "tavily",
}


def poi(place_id: str) -> dict[str, Any]:
    """按 place_id 取假世界里的 POI（用例剧本与判分共用同一份坐标）。"""

    for spec in POIS:
        if spec["place_id"] == place_id:
            return spec
    raise KeyError(f"假世界里没有 place_id={place_id}")


def coord_of(place_id: str) -> str:
    """`place_id` → `route` 工具要的 "经度,纬度" 串。"""

    spec = poi(place_id)
    return f"{spec['lng']:.3f},{spec['lat']:.3f}"


def _parse_coord(text: str) -> tuple[float, float] | None:
    numbers: list[float] = []
    for part in re.split(r"[,，\s]+", str(text or "").strip()):
        try:
            numbers.append(float(part))
        except ValueError:
            continue
    if len(numbers) < 2:
        return None
    # 经度在前（与 `route` 工具的 description 一致）
    return (round(numbers[0], 3), round(numbers[1], 3))


def _next_day(text: str) -> str:
    try:
        return (date.fromisoformat(text) + timedelta(days=1)).isoformat()
    except ValueError:
        return text


def _nights(check_in: str, check_out: str) -> int:
    try:
        return max(1, (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days)
    except ValueError:
        return 1


# ======================================================================
# 事实账本：假 Hub 真的返回过什么
# ======================================================================


@dataclass
class FactLedger:
    """本次 run 里假 Hub **真的返回过**的数字与标识。

    判分用它给行程"对账"：行程里出现的价格 / 车次 / 店名 / place_id / source_id 只要不在
    这里，就是无来源的数字（编造）。空账本 = 一次取数都没发生，那任何数字都对不上。
    """

    prices: set[float] = field(default_factory=set)
    durations: set[int] = field(default_factory=set)
    route_seconds: set[int] = field(default_factory=set)
    place_ids: set[str] = field(default_factory=set)
    hotel_names: set[str] = field(default_factory=set)
    transport_numbers: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)

    def price(self, value: Any) -> None:
        try:
            self.prices.add(round(float(value), 2))
        except (TypeError, ValueError):
            return

    def as_dict(self) -> dict[str, Any]:
        return {
            "prices": sorted(self.prices),
            "durations": sorted(self.durations),
            "route_seconds": sorted(self.route_seconds),
            "place_ids": sorted(self.place_ids),
            "hotel_names": sorted(self.hotel_names),
            "transport_numbers": sorted(self.transport_numbers),
            "source_ids": sorted(self.source_ids),
        }


class BenchmarkHub:
    """假 ProviderHub：固定的世界 + 可控注入（失败 / 返回空）。

    与真实 Hub 一致的地方：每次调用都落 `sources` 表、都进 `audit_entries()`
    （`app/agent_runner._metrics` 与 audit_report.json 都读它）。失败与空结果**同样留痕**
    ——"查了但没查到"和"根本没查"必须分得清。
    """

    def __init__(
        self,
        *,
        store: TravelPlanStore | None = None,
        run_id: str = "fake-run",
        failing: set[str] | None = None,
        empty: set[str] | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.failing = set(failing or ())
        self.empty = set(empty or ())
        self.calls: list[str] = []
        #: 真的触发了注入的工具名（用例据此断言"这个场景确实被构造出来了"）
        self.injected: list[str] = []
        self.facts = FactLedger()
        self._calls: list[ProviderCall] = []

    # --- 内部 ---

    def _call(self, tool: str, query: dict[str, Any]) -> ProviderCall:
        """一次假调用。`source_id` **按工具**（不是按调用序号）生成。

        为什么按工具：用例剧本里要写"这个数字来自哪一次工具返回"，按序号会随剧本里工具
        调用顺序的调整而漂移（改一次 `tool_calls` 顺序，所有 case 的 source_id 全错）。
        代价是同一工具调用多次共享一个 source_id —— 对"这份数据的来源是不是真的存在"
        这个判定没有影响，而判分要的正是这个。
        """

        call = ProviderCall(
            source_id=f"src-bm-{tool}",
            provider=PROVIDER_BY_TOOL.get(tool, "fake"),
            source_type=tool,
            tool=tool,
            query=dict(query),
            status="OK",
            fetched_at=FETCHED_AT,
            duration_ms=3,
        )
        self._calls.append(call)
        return call

    def _result(
        self,
        tool: str,
        *,
        calls: list[ProviderCall],
        items: list[Any],
        inject_key: str | None = None,
    ) -> ProviderResult[Any]:
        key = inject_key or tool
        if key in self.failing:
            self.injected.append(key)
            for call in calls:
                call.status = "UNAVAILABLE"
            result: ProviderResult[Any] = ProviderResult(
                status="UNAVAILABLE",
                provider=calls[0].provider if calls else "",
                calls=calls,
                error="假件注入：该 Provider 本次不可用",
            )
        elif key in self.empty:
            self.injected.append(key)
            # 空结果也要在账本上留下"这次确实没拿到东西"：只把 status 留在 OK 会让
            # "查了但空"和"查到了"看起来一样（audit 与 Provider 健康页都读这个字段）。
            for call in calls:
                call.status = "EMPTY"
            result = ProviderResult(
                status="EMPTY", provider=calls[0].provider if calls else "", calls=calls
            )
        else:
            result = ProviderResult(
                status="OK" if items else "EMPTY",
                provider=calls[0].provider if calls else "",
                calls=calls,
                items=items,
            )
        self._persist(result)
        self._record_facts(result)
        return result

    def _persist(self, result: ProviderResult[Any]) -> None:
        if self.store is None:
            return
        for call in result.calls:
            self.store.save_source(
                self.run_id,
                {
                    "source_id": call.source_id,
                    "provider": call.provider,
                    "source_type": call.source_type,
                    "source_url": call.source_url,
                    "query": call.query,
                    "fetched_at": call.fetched_at,
                    "status": call.status,
                },
            )

    def _record_facts(self, result: ProviderResult[Any]) -> None:
        """把"这次真的返回了什么"记进账本。**只记 OK 的结果**：失败/空结果里没有事实。"""

        if result.status != "OK":
            return
        for call in result.calls:
            self.facts.source_ids.add(call.source_id)
        for item in result.items:
            payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for key in ("price", "price_per_night", "market_price", "estimated_cost"):
                if payload.get(key) is not None:
                    self.facts.price(payload[key])
            if isinstance(payload.get("duration_minutes"), int):
                self.facts.durations.add(int(payload["duration_minutes"]))
            if isinstance(payload.get("duration_seconds"), int):
                self.facts.route_seconds.add(int(payload["duration_seconds"]))
            if payload.get("place_id"):
                self.facts.place_ids.add(str(payload["place_id"]))
            if payload.get("hotel_id"):
                self.facts.place_ids.add(str(payload["hotel_id"]))
            if payload.get("name") and payload.get("hotel_id"):
                self.facts.hotel_names.add(str(payload["name"]))
            for key in ("train_no", "flight_no"):
                if payload.get(key):
                    self.facts.transport_numbers.add(str(payload[key]))

    # --- 大交通 ---

    def search_trains(
        self, origin: str, destination: str, depart_date: str, **_: Any
    ) -> ProviderResult[Any]:
        self.calls.append("search_trains")
        call = self._call("search_trains", {"from": origin, "to": destination})
        specs = (INBOUND_TRAIN,) if "成都" in str(origin) else OUTBOUND_TRAINS
        items = [self._train_item(spec, origin, destination, depart_date, call) for spec in specs]
        return self._result("search_trains", calls=[call], items=items)

    @staticmethod
    def _train_item(
        spec: dict[str, Any], origin: str, destination: str, depart_date: str, call: ProviderCall
    ) -> dict[str, Any]:
        """把规格拼成"这一天"的车次（跨午夜的车按次日到达算）。"""

        arrive_date = _next_day(depart_date) if spec["arrive"] < spec["depart"] else depart_date
        return {
            "train_no": spec["train_no"],
            "train_type": spec["train_type"],
            "origin_station": spec["origin_station"] or origin,
            "destination_station": spec["destination_station"] or destination,
            "departure_at": f"{depart_date} {spec['depart']}",
            "arrival_at": f"{arrive_date} {spec['arrive']}",
            "duration_minutes": spec["duration_minutes"],
            "price_range": {"二等座": spec["price"]},
            "price": spec["price"],
            "seats": spec["seats"],
            "currency": "CNY",
            "provider": "12306",
            "source_id": call.source_id,
        }

    def search_flights(
        self, origin: str, destination: str, depart_date: str, **_: Any
    ) -> ProviderResult[Any]:
        self.calls.append("search_flights")
        call = self._call("search_flights", {"from": origin, "to": destination})
        item = {
            "flight_no": FLIGHT["flight_no"],
            "airline": FLIGHT["airline"],
            "origin": origin,
            "destination": destination,
            "departure_airport": FLIGHT["departure_airport"],
            "arrival_airport": FLIGHT["arrival_airport"],
            "departure_at": f"{depart_date} {FLIGHT['depart']}",
            "arrival_at": f"{depart_date} {FLIGHT['arrive']}",
            "duration_minutes": FLIGHT["duration_minutes"],
            "price": FLIGHT["price"],
            "currency": "CNY",
            "is_direct": FLIGHT["is_direct"],
            "remaining_seats": FLIGHT["remaining_seats"],
            "provider": "tuniu",
            "source_id": call.source_id,
        }
        return self._result("search_flights", calls=[call], items=[item])

    # --- 住宿 ---

    def search_hotels(
        self, city: str, check_in: str, check_out: str, **_: Any
    ) -> ProviderResult[Any]:
        self.calls.append("search_hotels")
        call = self._call("search_hotels", {"city": city})
        nights = _nights(check_in, check_out)
        items = [
            {
                **{
                    key: spec[key]
                    for key in (
                        "hotel_id",
                        "name",
                        "address",
                        "business_area",
                        "lat",
                        "lng",
                        "room_type",
                        "price_per_night",
                        "rating",
                        "price_note",
                    )
                },
                "city": city,
                "check_in": check_in,
                "check_out": check_out,
                "total_price": round(spec["price_per_night"] * nights, 2),
                "provider": "tuniu",
                "source_id": call.source_id,
            }
            for spec in HOTELS
        ]
        return self._result("search_hotels", calls=[call], items=items)

    # --- 门票 ---

    def search_scenic_tickets(self, scenic_name: str, **_: Any) -> ProviderResult[Any]:
        self.calls.append("search_scenic_tickets")
        call = self._call("search_scenic_tickets", {"scenic": scenic_name})
        price = TICKETS.get(str(scenic_name))
        items = (
            []
            if price is None
            else [
                {
                    "scenic_name": scenic_name,
                    "name": scenic_name,
                    "channel": "途牛",
                    "provider": "tuniu",
                    "price": price,
                    "market_price": price,
                    "currency": "CNY",
                    "booking_url": "https://example.invalid/ticket",
                    "source_id": call.source_id,
                }
            ]
        )
        return self._result("search_scenic_tickets", calls=[call], items=items)

    # --- 高德 ---

    def geocode(self, address: str, city: str | None = None) -> ProviderResult[Any]:
        self.calls.append("geocode")
        call = self._call("geocode", {"address": address, "city": city})
        match = next((spec for spec in POIS if spec["name"] == str(address)), None)
        items = (
            []
            if match is None
            else [
                {
                    "formatted_address": f"四川省成都市{match['district']}{match['name']}",
                    "province": "四川省",
                    "city": city or "成都",
                    "district": match["district"],
                    "adcode": "510100",
                    "level": "兴趣点",
                    "location": f"{match['lng']:.6f},{match['lat']:.6f}",
                    "lng": match["lng"],
                    "lat": match["lat"],
                    "source_id": call.source_id,
                }
            ]
        )
        return self._result("geocode", calls=[call], items=items)

    def search_poi(self, keywords: str, region: str, **_: Any) -> ProviderResult[Any]:
        self.calls.append("search_poi")
        call = self._call("search_poi", {"keywords": keywords, "region": region})
        names = [part.strip() for part in re.split(r"[,，\s]+", str(keywords)) if part.strip()]
        items = [
            {
                "place_id": spec["place_id"],
                "name": spec["name"],
                "type": spec["type"],
                "address": f"{region}示例地址{spec['name']}",
                "city": region,
                "district": spec["district"],
                "business_area": spec["business_area"],
                "lat": spec["lat"],
                "lng": spec["lng"],
                "opening_hours": spec["opening_hours"],
                "amap_verified": True,
                "source_id": call.source_id,
            }
            for name in names[:6]
            for spec in POIS
            if spec["name"] == name
        ]
        return self._result("search_poi", calls=[call], items=items)

    def poi_detail(self, poi_id: str) -> ProviderResult[Any]:
        self.calls.append("poi_detail")
        call = self._call("poi_detail", {"poi_id": poi_id})
        match = next((spec for spec in POIS if spec["place_id"] == str(poi_id)), None)
        items = (
            []
            if match is None
            else [
                {
                    **match,
                    "poi_id": match["place_id"],
                    "city": "成都",
                    "location": f"{match['lng']:.6f},{match['lat']:.6f}",
                    "tel": "028-00000000",
                    "rating": "4.5",
                    "cost": 60.0,
                    "source_id": call.source_id,
                }
            ]
        )
        return self._result("poi_detail", calls=[call], items=items)

    def route(
        self, origin: str, destination: str, mode: str = "transit", **_: Any
    ) -> ProviderResult[Any]:
        self.calls.append("route")
        call = self._call("route", {"mode": mode})
        start, end = _parse_coord(origin), _parse_coord(destination)
        minutes = (
            ROUTE_MINUTES.get((start, end), DEFAULT_ROUTE_MINUTES) if start and end else DEFAULT_ROUTE_MINUTES
        )
        item = {
            "mode": mode,
            "distance_meters": minutes * 1100,
            "duration_seconds": minutes * 60,
            "duration_minutes": minutes,
            "estimated_cost": 4.0,
            "provider": "amap",
            "verified": True,
            "source_id": call.source_id,
        }
        return self._result("route", calls=[call], items=[item])

    # --- 攻略 / 网页 ---

    def _evidence(self, tool: str, keyword: str, index: int) -> ProviderResult[Any]:
        self.calls.append(tool)
        call = self._call(tool, {"keyword": keyword})
        item = {
            "id": f"ev-bm-{index}",
            "source_type": "xiaohongshu" if tool == "search_xiaohongshu" else "douyin",
            "title": f"{keyword} 攻略",
            "author": "本地人",
            "published_at": "2026-09-01",
            "text": GUIDE_TEXT,
            "place_mentions": [spec["name"] for spec in POIS],
            "specific_dishes": ["担担面", "龙抄手", "串串香"],
            "source_url": f"https://example.invalid/note/{index}",
            "source_id": call.source_id,
        }
        return self._result(tool, calls=[call], items=[item])

    def search_xiaohongshu(self, keyword: str, **_: Any) -> ProviderResult[Any]:
        return self._evidence("search_xiaohongshu", keyword, 1)

    def search_douyin(self, keyword: str, **_: Any) -> ProviderResult[Any]:
        return self._evidence("search_douyin", keyword, 2)

    def web_search(self, query: str, **_: Any) -> ProviderResult[Any]:
        self.calls.append("web_search")
        call = self._call("web_search", {"query": query})
        item = {
            "title": f"{query}（网页）",
            "snippet": "宽窄巷子、武侯祠、锦里都在市区，人民公园喝盖碗茶。",
            "url": "https://example.invalid/guide",
            "score": 0.9,
            "source_id": call.source_id,
        }
        return self._result("web_search", calls=[call], items=[item])

    # --- 生命周期 / 审计 ---

    def audit_entries(self) -> list[dict[str, Any]]:
        """真实 Hub 的调用账本，形状与 `app.workflow._build_audit` 消费的一致。"""

        return [
            {
                "provider": call.provider,
                "tool": call.tool,
                "arguments": dict(call.query or {}),
                "status": call.status,
                # 失败/空的调用没有条目：与真实 Hub 一样，账本不编造 item_count。
                "item_count": 0 if call.status != "OK" else 1,
                "duration_ms": call.duration_ms or 1,
                "fetched_at": call.fetched_at.isoformat(),
                "source_id": call.source_id,
                "notes": [],
                "error": None,
            }
            for call in self._calls
        ]

    def source_refs(self) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        return self.store.list_sources(self.run_id)

    def close(self) -> None:
        return None


# ======================================================================
# 脚本模型
# ======================================================================


def _chat(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


def _is_planner_call(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage) and PLANNER_MARK in str(message.content)
        for message in messages
    )


class ScriptedPlannerModel(BaseChatModel):
    """按剧本逐轮出招的离线模型：每一步要么调一个工具，要么回一段文本。

    剧本里"非规划调用"（例如长期记忆 consolidation）一律回一句空文本且**不推进脚本**，
    因此评测结果不受开发机 `data/memory.db` 里攒了多少 rollout 影响。
    """

    script: list[dict[str, Any]] = Field(default_factory=list)
    #: 每次模型调用时它看到的工具名（用来验证工具可见性）
    visible: list[list[str]] = Field(default_factory=list)
    turns: int = 0
    #: 每轮回报的 token 用量（input / output / cached）
    usage: tuple[int, int, int] = (30, 10, 4)
    #: 每轮模型调用前的固定延迟（用来验证超时截断）
    delay_seconds: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "scripted-benchmark"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedPlannerModel:
        self.visible.append(sorted(getattr(tool, "name", "") for tool in tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not _is_planner_call(messages):
            return _chat(AIMessage(content="（非规划流程调用，跳过）"))

        if self.delay_seconds:
            time.sleep(self.delay_seconds)

        step = self.script[min(self.turns, len(self.script) - 1)]
        self.turns += 1
        usage = {
            "input_tokens": self.usage[0],
            "output_tokens": self.usage[1],
            "total_tokens": self.usage[0] + self.usage[1],
            "input_token_details": {"cache_read": self.usage[2]},
        }
        name = step.get("tool")
        if name:
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": step.get("args") or {}, "id": f"call-{self.turns}"}
                ],
                usage_metadata=usage,
            )
        else:
            message = AIMessage(content=step.get("text") or "", usage_metadata=usage)
        return _chat(message)


# ======================================================================
# 长期记忆 / 终端 Trace 的隔离（与 tests/agent_fixture.isolated_harness 同源）
# ======================================================================


@contextmanager
def isolated_harness() -> Iterator[None]:
    """一次 run 期间关掉长期记忆 / 终端 Trace。

    必须做：`MemoryMiddleware` 会把每次 run 的 rollout 写进 `settings.memory_db_url`
    （默认开发库 `data/memory.db`），攒够条数还会**再调一次模型**做 consolidation ——
    那一次会打乱脚本模型的动作序列，让评测"有时过有时不过"。替换的是
    `superharness.agent` 模块里的 `settings`（`create_harness_agent` 装配时读的就是它），
    以及 `ObservabilityMiddleware.__init__` 的默认参数（终端渲染读的是 def 时绑定的默认值）。
    """

    from superharness import agent as harness_agent
    from superharness.config import settings
    from superharness.middleware import ObservabilityMiddleware

    patched = dataclasses.replace(
        settings,
        memory_enabled=False,
        retrieval_enabled=False,
        log_enabled=False,
        trace_enabled=False,
    )
    original_settings = harness_agent.settings
    original_defaults = ObservabilityMiddleware.__init__.__defaults__
    harness_agent.settings = patched
    ObservabilityMiddleware.__init__.__defaults__ = (patched,)
    try:
        yield
    finally:
        harness_agent.settings = original_settings
        ObservabilityMiddleware.__init__.__defaults__ = original_defaults


# ======================================================================
# 一次 case 的世界
# ======================================================================


@dataclasses.dataclass
class Observation:
    """判分与指标的唯一输入：**已发生的事实**（不是模型自述）。"""

    run_id: str
    status: str
    error: str | None
    plan: Any
    degradations: list[str]
    progress_status: str
    warnings: list[Any]
    unresolved_conflicts: int
    conflicts_detected: int
    conflicts_resolved: int
    reject_ids: list[str]
    reject_intruders: list[str]
    grounding: dict[str, Any]
    provider_calls: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    run_metrics: dict[str, Any]
    audit: dict[str, Any]
    injected: list[str]
    artifacts: list[str]
    facts: dict[str, Any]
    elapsed_ms: int

    @property
    def plan_place_ids(self) -> list[str]:
        if self.plan is None:
            return []
        return sorted(
            {str(item.place_id) for day in self.plan.days for item in day.items if item.place_id}
        )


@dataclasses.dataclass
class CaseWorld:
    """一个 case 的全套离线依赖（假 Hub + 脚本模型）与一次真的 run。"""

    case: dict[str, Any]
    store: TravelPlanStore
    run_id: str
    output_dir: Path
    hub: BenchmarkHub
    model: ScriptedPlannerModel

    def run(self) -> tuple[Any, int]:
        """跑一次真的 Agent Loop。返回 `(RunResult, 耗时毫秒)`。"""

        from app.config import override_env

        fixtures = dict(self.case.get("fixtures") or {})
        env = {str(key): str(value) for key, value in (fixtures.get("env") or {}).items()}
        intent = TripIntent.model_validate(self.case["intent"]) if self.case.get("intent") else None
        started = time.perf_counter()
        with override_env(env), isolated_harness():
            result = execute_agent_run(
                str(self.case.get("query") or ""),
                user_id="benchmark",
                run_id=self.run_id,
                output_dir=self.output_dir,
                store=self.store,
                model=self.model,
                hub=self.hub,
                intent=intent,
                source="benchmark",
            )
        return result, int((time.perf_counter() - started) * 1000)


def build_script(case: dict[str, Any]) -> list[dict[str, Any]]:
    """case → 模型剧本。

    两种写法：
    1. 声明式（`cases/*.jsonl` 用）：`tool_calls` + `submit`（+ 可选 `tail`）；
    2. 原始剧本：直接给 `script`（评测"判卷器能不能抓到坏行为"时用）。

    无论哪种写法，末尾都保证有一个纯文本步骤：否则模型会被反复喂同一个工具调用，
    一路撞到步数上限 —— 那样测的是递归上限，而不是被测行为。

    唯一的例外是显式声明 `repeat_last_step: true` 的用例（专门测"撞上步数上限"时
    循环被截断、已交卷的行程仍被保留）：那时**必须**让模型一直重复最后一步。
    """

    if case.get("script"):
        script = [dict(step) for step in case["script"]]
    else:
        script = [{"tool": name, "args": args} for name, args in (case.get("tool_calls") or [])]
        if case.get("submit"):
            script.append({"tool": SUBMIT_TOOL_NAME, "args": case["submit"]})
        script.extend(dict(step) for step in (case.get("tail") or []))
    if not script:
        script = [{"text": "（本用例不调用任何工具）"}]
    if script[-1].get("tool") and not case.get("repeat_last_step"):
        script.append({"text": "本轮到此为止。"})
    return script


def build_world(
    case: dict[str, Any], *, store: TravelPlanStore, run_id: str, output_dir: Path
) -> CaseWorld:
    """按 case 的 fixtures 装配世界：假 Hub（含注入）+ 脚本模型。"""

    fixtures = dict(case.get("fixtures") or {})
    hub = BenchmarkHub(
        store=store,
        run_id=run_id,
        failing=set(fixtures.get("failing") or ()),
        empty=set(fixtures.get("empty") or ()),
    )
    model = ScriptedPlannerModel(
        script=build_script(case),
        usage=tuple(fixtures.get("usage") or (30, 10, 4)),  # type: ignore[arg-type]
        delay_seconds=float(fixtures.get("delay_seconds") or 0.0),
    )
    return CaseWorld(
        case=case, store=store, run_id=run_id, output_dir=output_dir, hub=hub, model=model
    )


# ======================================================================
# 最小交卷参数（用例与单测共用的"honest plan"底板）
# ======================================================================


def attraction_item(**overrides: Any) -> dict[str, Any]:
    """一个能指回假世界的地点项（宽窄巷子 / B0FF1 / 免费）。"""

    item: dict[str, Any] = {
        "name": "宽窄巷子",
        "type": "attraction",
        "place_id": "B0FF1",
        "lat": 30.669,
        "lng": 104.057,
        "start_time": "14:00",
        "end_time": "16:00",
        "price": 0,
        "price_type": "realtime",
        "reason": "高德核实的真实地点",
    }
    item.update(overrides)
    return item


def minimal_plan(*, days: int = 3, **overrides: Any) -> dict[str, Any]:
    """一份最小但完整的交卷参数：所有数字都能指回本模块的固定世界。"""

    payload: dict[str, Any] = {
        "intent": {
            "destination": ["成都"],
            "origin": "北京",
            "start_date": "2026-10-01",
            "days": days,
            "travelers": 2,
            "budget_total": 6000,
            "preferences": ["美食", "拍照"],
        },
        "transport": {
            "kind": "train",
            "no": "G89",
            "provider": "12306",
            "origin": "北京西",
            "destination": "成都东",
            "departure_at": "2026-10-01 07:00",
            "arrival_at": "2026-10-01 12:30",
            "duration_minutes": 330,
            "price": 780,
        },
        "inbound_transport": {
            "kind": "train",
            "no": "G90",
            "provider": "12306",
            "origin": "成都东",
            "destination": "北京西",
            "departure_at": "2026-10-03 18:00",
            "arrival_at": "2026-10-03 23:30",
            "duration_minutes": 330,
            "price": 780,
        },
        "hotel": {
            "name": "成都春熙路智选假日酒店",
            "provider": "tuniu",
            "price_per_night": 420,
            "price_note": "420 起价/晚",
            "rating": 4.6,
        },
        "days": [
            {
                "day_index": index,
                "date": f"2026-10-0{index + 1}",
                "items": [attraction_item(start_time="14:00", end_time="16:00")],
            }
            for index in range(days)
        ],
        "budget": {
            "known_real_cost": 2400,
            "estimated_cost": 600,
            "projected_total": 3000,
            "breakdown": {"交通": 1560, "住宿": 840},
        },
        # source_id 必须是假 Hub 真的写进 sources 的那一条（按工具名生成，见 `_call`）。
        "sources": [
            {"source_id": "src-bm-search_hotels", "provider": "tuniu", "title": "春熙路智选假日"}
        ],
    }
    payload.update(overrides)
    return payload


__all__ = [
    "FIXTURE_VERSION",
    "PLANNER_MARK",
    "SUBMIT_TOOL_NAME",
    "BenchmarkHub",
    "CaseWorld",
    "FactLedger",
    "Observation",
    "ScriptedPlannerModel",
    "attraction_item",
    "build_script",
    "build_world",
    "coord_of",
    "isolated_harness",
    "minimal_plan",
    "poi",
]
