"""离线测试用的假 Provider / 假 LLM（PRD §34.3 Mock E2E）。

为什么必须假：一次真实规划要串几十个 HTTP（高德 / 途牛 / TikHub / 12306），
跑一遍 3 分钟起，而且数据每天在变。单测要的是「流程形状与字段契约正确」，
不是「今天成都门票多少钱」，所以这里全部离线。

为什么假件要尽量复用真实类：假件比产品宽松，跑出来的失败就是假件的失败。
所以这里刻意复用 `ProviderResult` / `Place` / `TrainOption` / `LLMResult`，
只替换「网络那一层」。另外真实 `ProviderHub` 会把每次调用落进 `sources` 表
（`evidence.source_id` 有外键指向它），假 Hub 必须做同样的事，否则会撞外键约束。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.llm import STATUS_OK, STATUS_UNAVAILABLE, LLMResult
from app.models import (
    Evidence,
    FlightOption,
    HotelOption,
    Place,
    RouteOption,
    TrainOption,
)
from app.providers import ProviderCall, ProviderResult
from app.store import TravelPlanStore

QUERY = "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"

#: 真实 Provider 的 status 用大写词表；测试里断言状态时复用同一套字面量。
STATUS_UNAVAILABLE_PROVIDER = "UNAVAILABLE"


def _call(provider: str, tool: str, query: dict) -> ProviderCall:
    return ProviderCall(
        source_id=f"src-{provider}-{tool}",
        provider=provider,
        source_type=tool,
        tool=tool,
        query=query,
        status="OK",
        fetched_at=datetime.now(),
    )


class FakeHub:
    """只实现流程真正会调用的方法，每个都返回真实形状的 ProviderResult。

    `failing` / `empty` 用来构造降级场景：
    - `failing={"search_trains"}` → 该方法返回 status=UNAVAILABLE 且无 items；
    - `empty={"search_poi"}`     → 该方法返回 status=EMPTY 且无 items。
    """

    def __init__(
        self,
        store: TravelPlanStore | None = None,
        run_id: str = "fake-run",
        *,
        failing: set[str] | None = None,
        empty: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.store = store
        self.run_id = run_id
        self.failing = set(failing or ())
        self.empty = set(empty or ())
        #: audit_report.json 的 provider_calls 来源。真实 Hub 从自己的调用账本生成，
        #: 假 Hub 也按同样的形状记，否则 audit 断言测的就是假件的常量。
        self._audit: list[dict[str, Any]] = []

    # --- 内部工具 ---

    def _mk(self, tool: str, **kwargs: Any) -> ProviderResult:
        """构造 ProviderResult 并落 sources 表，模拟真实 Hub 的副作用。

        失败时也要**保留调用记录**：真实 Hub 会把失败的调用以 `status != OK` 落进
        `sources` 与 audit —— "查了但没查到"和"根本没查"是两件事，审计必须分得清。
        """
        calls = list(kwargs.get("calls") or [])
        provider = kwargs.get("provider", "")

        if tool in self.failing:
            for call in calls:
                call.status = STATUS_UNAVAILABLE_PROVIDER
            result = ProviderResult(status=STATUS_UNAVAILABLE_PROVIDER, provider=provider, calls=calls)
        elif tool in self.empty:
            result = ProviderResult(status="EMPTY", provider=provider, calls=calls)
        else:
            result = ProviderResult(**kwargs)

        if self.store is not None:
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

        for call in result.calls:
            self._audit.append(
                {
                    "provider": call.provider,
                    "tool": call.tool,
                    "arguments": dict(call.query or {}),
                    "status": call.status,
                    "item_count": len(result.items),
                    "duration_ms": call.duration_ms or 1,
                    "fetched_at": call.fetched_at.isoformat(),
                    "source_id": call.source_id,
                    "notes": [],
                    "error": None,
                }
            )
        return result

    # --- 大交通 ---

    def search_trains(self, origin, destination, depart_date, **kwargs):
        self.calls.append("search_trains")
        call = _call("12306", "get-tickets", {"from": origin, "to": destination})
        return self._mk(
            "search_trains",
            status="OK",
            provider="12306",
            calls=[call],
            items=[
                TrainOption(
                    provider="12306",
                    source_id=call.source_id,
                    fetched_at=call.fetched_at,
                    raw={},
                    train_no="G89",
                    origin_station=f"{origin}东",
                    destination_station=f"{destination}东",
                    departure_at=f"{depart_date} 08:00",
                    arrival_at=f"{depart_date} 15:30",
                    duration_minutes=450,
                    seats={"二等座": 12},
                    price_range={"二等座": 780.0},
                    price=780.0,
                    currency="CNY",
                    train_type="高铁",
                ),
                TrainOption(
                    provider="12306",
                    source_id=call.source_id,
                    fetched_at=call.fetched_at,
                    raw={},
                    train_no="K117",
                    origin_station=origin,
                    destination_station=destination,
                    departure_at=f"{depart_date} 22:10",
                    arrival_at=f"{depart_date} 06:40",
                    duration_minutes=1830,
                    seats={"硬卧": 4},
                    price_range={"硬卧": 260.0},
                    price=260.0,
                    currency="CNY",
                    train_type="普快",
                ),
            ],
        )

    def search_flights(self, origin, destination, depart_date, **kwargs):
        self.calls.append("search_flights")
        call = _call("tuniu", "flight", {"from": origin, "to": destination})
        return self._mk(
            "search_flights",
            status="OK",
            provider="tuniu",
            calls=[call],
            items=[
                FlightOption(
                    provider="tuniu",
                    source_id=call.source_id,
                    fetched_at=call.fetched_at,
                    raw={},
                    flight_no="CA4102",
                    airline="国航",
                    origin=origin,
                    destination=destination,
                    departure_airport=f"{origin}首都T3",
                    arrival_airport=f"{destination}天府T1",
                    departure_at=f"{depart_date} 09:00",
                    arrival_at=f"{depart_date} 12:05",
                    duration_minutes=185,
                    price=1180.0,
                    currency="CNY",
                    is_direct=True,
                )
            ],
        )

    # --- 住宿 ---

    def search_hotels(self, city, check_in, check_out, **kwargs):
        self.calls.append("search_hotels")
        call = _call("tuniu", "hotel", {"city": city})
        return self._mk(
            "search_hotels",
            status="OK",
            provider="tuniu",
            calls=[call],
            items=[
                HotelOption(
                    provider="tuniu",
                    source_id=call.source_id,
                    fetched_at=call.fetched_at,
                    raw={},
                    hotel_id="h1",
                    name=f"{city}春熙路智选假日",
                    price_per_night=420.0,
                    rating=4.6,
                    business_area="春熙路",
                    check_in=check_in,
                    check_out=check_out,
                    price_note="420 起价/晚",
                ),
                HotelOption(
                    provider="tuniu",
                    source_id=call.source_id,
                    fetched_at=call.fetched_at,
                    raw={},
                    hotel_id="h2",
                    name=f"{city}郊区快捷",
                    price_per_night=180.0,
                    rating=3.9,
                    business_area="双流",
                    check_in=check_in,
                    check_out=check_out,
                    price_note="180 起价/晚",
                ),
            ],
        )

    # --- 攻略 ---

    #: 三条证据里只有两条带 place_mentions：剩下那条会被流程挑去做模型抽取，
    #: 这样一次 run 能同时覆盖「数据源自带地名」和「模型抽取地名」两条路径。
    GUIDE_TEXT = (
        "成都必去：宽窄巷子、武侯祠、锦里、人民公园。"
        "美食：担担面、龙抄手、串串香、老火锅。住宿建议住春熙路附近，地铁方便。"
    )

    def _evidence(self, provider: str, tool: str, keyword: str, *, source_type: str, mentions: list[str], index: int = 1):
        self.calls.append(tool)
        call = _call(provider, tool, {"keyword": keyword})
        return self._mk(
            tool,
            status="OK",
            provider=provider,
            calls=[call],
            items=[
                Evidence(
                    id=f"ev-{source_type}-{index}",
                    source_type=source_type,
                    provider=provider,
                    source_id=call.source_id,
                    source_url=f"https://example.invalid/note/{index}",
                    fetched_at=call.fetched_at,
                    raw={},
                    title=f"{keyword} 攻略",
                    author="本地人",
                    text=self.GUIDE_TEXT,
                    place_mentions=mentions,
                    specific_dishes=["担担面", "龙抄手", "串串香"],
                    raw_metrics={"likes": 3200, "comments": 210},
                )
            ],
        )

    def search_xiaohongshu(self, keyword, **kwargs):
        return self._evidence(
            "tikhub", "xiaohongshu", keyword, source_type="xiaohongshu",
            mentions=["宽窄巷子", "武侯祠", "锦里", "人民公园"], index=1,
        )

    def search_douyin(self, keyword, **kwargs):
        return self._evidence(
            "tikhub", "douyin", keyword, source_type="douyin",
            mentions=[], index=2,
        )

    def web_search(self, query, **kwargs):
        self.calls.append("web_search")
        call = _call("tavily", "web_search", {"query": query})
        return self._mk(
            "web_search",
            status="OK",
            provider="tavily",
            calls=[call],
            items=[
                {
                    "title": f"{query}（网页）",
                    "snippet": "宽窄巷子、武侯祠、锦里都在市区，人民公园喝盖碗茶。",
                    "url": "https://example.invalid/guide",
                }
            ],
        )

    # --- 高德 ---

    def search_poi(self, keywords, region, *, types=None, page=1, page_size=20, type_hint=None, **kwargs):
        self.calls.append("search_poi")
        call = _call("amap", "search_poi", {"keywords": keywords, "region": region})
        names = [part.strip() for part in str(keywords).replace("，", " ").split() if part.strip()]
        items = [
            Place.from_poi(
                {
                    "poi_id": f"poi-{name}",
                    "name": name,
                    "type": type_hint or "风景名胜",
                    "latitude": 30.6 + index * 0.01,
                    "longitude": 104.05 + index * 0.01,
                    "address": f"{region}示例地址{index}号",
                    "city": region,
                    "district": "青羊区",
                    "business_area": "宽窄巷子",
                    "opening_hours": "08:30-18:00",
                },
                city=region,
                aliases=[name],
                type_hint=type_hint,
                expected_duration_minutes=90,
            )
            for index, name in enumerate(names[:4])
        ]
        return self._mk("search_poi", status="OK" if items else "EMPTY", provider="amap", calls=[call], items=items)

    def poi_detail(self, poi_id):
        self.calls.append("poi_detail")
        call = _call("amap", "get_poi_detail", {"poi_id": poi_id})
        return self._mk(
            "poi_detail",
            status="OK",
            provider="amap",
            calls=[call],
            items=[{"poi_id": poi_id, "opening_hours": "08:30-18:00", "address": "示例地址", "business_area": "宽窄巷子"}],
        )

    def route(self, origin, destination, mode="transit", **kwargs):
        self.calls.append("route")
        call = _call("amap", "route", {"mode": mode})
        return self._mk(
            "route",
            status="OK",
            provider="amap",
            calls=[call],
            items=[
                RouteOption(
                    origin_place_id=str(origin),
                    destination_place_id=str(destination),
                    mode=mode,
                    distance_meters=3200,
                    duration_seconds=1140,
                    estimated_cost=4.0,
                    provider="amap",
                    fetched_at=call.fetched_at,
                    verified=True,
                )
            ],
        )

    def geocode(self, address, city=None):
        self.calls.append("geocode")
        call = _call("amap", "geocode", {"address": address})
        return self._mk(
            "geocode",
            status="OK",
            provider="amap",
            calls=[call],
            items=[{"longitude": 104.07, "latitude": 30.65, "city": city or ""}],
        )

    def search_scenic_tickets(self, scenic_name):
        self.calls.append("search_scenic_tickets")
        call = _call("tuniu", "ticket", {"scenic": scenic_name})
        return self._mk(
            "search_scenic_tickets",
            status="OK",
            provider="tuniu",
            calls=[call],
            items=[{"name": scenic_name, "price": 60.0, "min_price": 55.0}],
        )

    def tikhub_quota(self):
        return self._mk("tikhub_quota", status="OK", provider="tikhub", calls=[], items=[{"free_credit": 100}])

    # --- 生命周期 / 审计 ---

    def audit_entries(self):
        """真实 Hub 的调用账本，形状与 `app.workflow._build_audit` 消费的一致。"""
        return list(self._audit)

    def source_refs(self):
        if self.store is None:
            return []
        return self.store.list_sources(self.run_id)

    def close(self):
        return None


class FakeLLM:
    """按 tag 返回固定内容。

    `calls` 装真实的 `LLMResult`：产品代码会读 `call.ok`（workflow.py:2189）并对每条
    调 `to_audit()`。用字符串当假调用记录，跑出来的失败是假件不合格，不是产品缺陷。

    `unavailable=True` 模拟模型整个不可用（Key 没配 / 超时），用来验证
    「Evidence First, LLM Second」——流程必须照常跑完并如实记降级。
    """

    #: 检索词扩写要的是 list（workflow.py:995）；地点抽取要的是 {"places": [...]}（1130）。
    QUERIES = ["成都 攻略", "成都 美食", "成都 必去"]
    PLACES = [
        {"name": "宽窄巷子", "category": "attraction", "tone": "positive"},
        {"name": "武侯祠", "category": "attraction", "tone": "positive"},
        {"name": "锦里", "category": "attraction", "tone": "neutral"},
    ]

    def __init__(self, *, unavailable: bool = False, intent_payload: dict | None = None) -> None:
        self.calls: list[LLMResult] = []
        self.unavailable = unavailable
        self.intent_payload = intent_payload

    @property
    def tags(self) -> list[str]:
        return [call.tag for call in self.calls]

    def _json(self, tag: str) -> Any:
        if tag == "parse_intent":
            if self.intent_payload is not None:
                return self.intent_payload
            # 只给 origin/destination/days：把日期/人数/预算留给规则回填，覆盖交叉校验路径。
            return {"origin_city": "北京", "destination_city": "成都", "days": 5}
        if tag.startswith("extract_places"):
            return {"places": self.PLACES}
        if tag == "query_expansion":
            return list(self.QUERIES)
        if tag == "critic":
            return {"verdict": "accept", "confidence": 80, "issues": []}
        return {}

    def invoke(self, system, user, *, tag=""):
        if self.unavailable:
            result = LLMResult(
                status=STATUS_UNAVAILABLE,
                model="fake",
                tag=tag,
                error="假 LLM 被设为不可用",
            )
        else:
            result = LLMResult(
                status=STATUS_OK,
                text=f"[{tag}] 由假模型生成的一段说明。",
                model="fake",
                tag=tag,
                duration_ms=1,
            )
        self.calls.append(result)
        return result

    def invoke_json(self, system, user, *, tag=""):
        result = self.invoke(system, user, tag=tag)
        if result.ok:
            result.value = self._json(tag)
        return result

    @property
    def available(self) -> bool:
        return not self.unavailable

    @property
    def model_name(self) -> str:
        return "fake"

    def audit_entries(self) -> list[dict[str, Any]]:
        return [call.to_audit() for call in self.calls]


def make_store(db_path) -> TravelPlanStore:
    store = TravelPlanStore(db_path=db_path)
    store.init_schema()
    return store
