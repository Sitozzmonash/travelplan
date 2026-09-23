"""旅行取数工具集 —— 把 `ProviderHub` 的取数能力暴露成 Agent 可调用的 langchain Tool。

这个模块为什么存在
------------------
SuperHarness Agent Loop（feedback/inbox/2026-09-22 的主规划改造）要求 Agent 在循环里
**自己决定**要不要查火车 / 机票 / 酒店 / 门票 / POI / 路线 / 攻略。`ProviderHub` 里那些
方法已经是"唯一取数入口"，但它们返回的是 `ProviderResult[BaseModel]` —— 直接塞给 LLM
有两个问题：

1. LLM 不认识 Python 对象，也不认识 Hub 的方法签名，它只认 **Tool 的 name / description /
   args_schema**；
2. 一次查询可能返回几十条（12306 一页 10 趟、POI 一页 20 个、攻略正文几 KB），
   Agent 在循环里调用多次之后，上下文会被原始 payload 撑爆。

所以这里只做两件事，别的什么都不做：

    ProviderHub.<method>(...)  →  闭包进 @tool 函数  →  截断成 top-N + 关键字段  →  JSON 字符串

设计约束（改这个文件前先读）
--------------------------
* **不自己 new Hub**：`build_travel_tools(hub)` 接收调用方已经装配好的 `ProviderHub`
  实例并闭包进去。Hub 持有 MCP 子进程、缓存、审计调用列表和 run_id，agent 侧每次 new
  一个会丢掉审计链、重复拉起 12306 子进程（见 `app/providers.py::ProviderHub` 的注释）。
* **工具返回字符串**：JSON 文本是给模型读的，不是给人看的结构化数据。字段名保留 Hub
  模型的原名（`train_no` / `price` / `distance_meters` ...），因为 Agent Loop 之后的
  下游解析（`app/planner.py`）用的就是这些名字，多一层改名只会多一处漂移点。
* **失败不伪造**：Hub 的空结果 + 非 OK 状态原样带出去（`status` / `error` / `calls_note`），
  由 Agent 自己决定降级还是换工具。这里绝不补一条看起来合理的价格 / 时长 / 坐标。
* **截断必须可测**：`truncate_items` / `truncate_result` 是纯函数，不依赖 Hub、不联网，
  单测直接喂超长列表断言"裁到 N 条且关键字段还在"。

给 Agent 装配（P1 agent 核心的接入点）：

    from app.travel_tools import build_travel_tools, TOOL_DISPLAY_NAMES

    agent = create_harness_agent(system_prompt=..., tools=build_travel_tools(hub))

注意：SuperHarness 的 `capabilities/tools/web_search.py` 也会自动发现一个同名的
`web_search`，而 `superharness/agent.py::_merge_local_tools` 在"自动发现 vs 手动传入"
重名时**直接抛 ValueError**。装配 Agent 时二选一：要么关掉 Tool 自动发现
（`settings.tool_auto_discovery=False`），要么不要把这个列表里的 `web_search` 传进去
（`hub.web_search` 本来就是转发那个内置工具的）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel

from app.providers import ProviderHub, ProviderResult

#: 工具默认只回 top-N 条 —— Agent 可能一次 run 调十几次工具，不截断上下文必爆。
DEFAULT_TOP_N = 5

#: 单条工具结果的上限（agent 显式传 top_n 也不能越过它，防止把上下文一次性拉爆）。
MAX_TOP_N = 20

#: 单个文本字段（攻略正文、网页摘要、地址）保留的最大字符数。
MAX_TEXT_CHARS = 240

#: 工具名 → 前端步骤展示名。前端按这个把 tool 名映射成"在查火车票"这类中文进度文案；
#: 没有映射的工具名由调用方自行兜底显示。
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "search_trains": "在查火车票",
    "search_flights": "在查机票",
    "search_hotels": "在查酒店",
    "search_scenic_tickets": "在查景区门票",
    "geocode": "在定位地址",
    "search_poi": "在搜周边地点",
    "poi_detail": "在查地点详情",
    "route": "在算路上时间",
    "search_xiaohongshu": "在翻小红书攻略",
    "search_douyin": "在翻抖音攻略",
    "web_search": "在搜网页资料",
}

# ==================================================
# 每个业务对象的"关键字段"白名单
# ==================================================
# 为什么是白名单而不是黑名单：Provider 的信封里既有模型字段也有 raw payload，
# 黑名单下任何一个新增的上游字段都会静默漏进上下文；白名单漏字段是显式的、可发现的。

TRAIN_FIELDS: tuple[str, ...] = (
    "train_no",
    "train_type",
    "origin_station",
    "destination_station",
    "departure_at",
    "arrival_at",
    "duration_minutes",
    "price_range",
    "price",
    "seats",
    "currency",
    "provider",
)

FLIGHT_FIELDS: tuple[str, ...] = (
    "flight_no",
    "airline",
    "origin",
    "destination",
    "departure_airport",
    "arrival_airport",
    "departure_at",
    "arrival_at",
    "duration_minutes",
    "price",
    "currency",
    "is_direct",
    "remaining_seats",
    "provider",
)

HOTEL_FIELDS: tuple[str, ...] = (
    "hotel_id",
    "name",
    "address",
    "city",
    "business_area",
    "lat",
    "lng",
    "room_type",
    "price_per_night",
    "total_price",
    "rating",
    "price_note",
    "provider",
)

PLACE_FIELDS: tuple[str, ...] = (
    "place_id",
    "name",
    "type",
    "address",
    "city",
    "district",
    "business_area",
    "lat",
    "lng",
    "opening_hours",
    "amap_verified",
)

ROUTE_FIELDS: tuple[str, ...] = (
    "mode",
    "distance_meters",
    "duration_seconds",
    "duration_minutes",
    "estimated_cost",
    "provider",
    "verified",
)

EVIDENCE_FIELDS: tuple[str, ...] = (
    "id",
    "source_type",
    "title",
    "author",
    "published_at",
    "text",
    "place_mentions",
    "specific_dishes",
    "source_url",
)

#: 高德 geocode 的返回是原始 dict（信封 item），字段随上游版本变，白名单只做收窄。
GEOCODE_FIELDS: tuple[str, ...] = (
    "formatted_address",
    "province",
    "city",
    "district",
    "adcode",
    "level",
    "location",
    "lng",
    "lat",
)

POI_DETAIL_FIELDS: tuple[str, ...] = (
    "poi_id",
    "id",
    "name",
    "type",
    "address",
    "city",
    "district",
    "business_area",
    "location",
    "lng",
    "lat",
    "tel",
    "rating",
    "opening_hours",
    "cost",
)

TICKET_FIELDS: tuple[str, ...] = (
    "scenic_name",
    "name",
    "channel",
    "provider",
    "price",
    "market_price",
    "currency",
    "booking_url",
    "url",
    "note",
)

WEB_FIELDS: tuple[str, ...] = ("title", "url", "content", "snippet", "score")


# ==================================================
# 截断：纯函数，可单独测
# ==================================================


def clamp_top_n(top_n: Any, *, default: int = DEFAULT_TOP_N) -> int:
    """把外部（= LLM 生成的）top_n 收敛到 [1, MAX_TOP_N]。

    LLM 传 `top_n=1000` 或 `top_n=-1` 是常态，这里不抛错、只夹紧 —— 一次参数写错
    不该让整轮 Agent 因为异常而中断。
    """
    try:
        value = int(top_n)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, MAX_TOP_N)


#: 模型上的 `@property`（`model_dump` 不输出它们，但对 Agent 有用）：
#: TrainOption.price（价格区间下限）与 RouteOption.duration_minutes（向上取整的分钟数）。
_COMPUTED_FIELDS: tuple[str, ...] = ("price", "duration_minutes")


def _model_dump(item: Any) -> dict[str, Any]:
    """业务模型 → 可 JSON 化的 dict；已经是 dict 的原样返回。"""
    if isinstance(item, BaseModel):
        payload = item.model_dump(mode="json")
        for name in _COMPUTED_FIELDS:
            if name in payload:
                continue
            value = getattr(item, name, None)
            if isinstance(value, (str, int, float, bool)):
                payload[name] = value
        return payload
    if isinstance(item, Mapping):
        return dict(item)
    return {"value": item}


def _clean(value: Any) -> Any:
    """递归收紧一个字段值：长文本截断、容器限量、None 直接丢弃由调用方处理。"""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT_CHARS else value[:MAX_TEXT_CHARS] + "…"
    if isinstance(value, Mapping):
        return {
            str(key): _clean(sub)
            for key, sub in list(value.items())[:12]
            if sub is not None and sub != "" and sub != [] and sub != {}
        }
    if isinstance(value, (list, tuple)):
        return [_clean(sub) for sub in list(value)[:8]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _pick(payload: Mapping[str, Any], fields: Sequence[str] | None) -> dict[str, Any]:
    """按白名单取字段；白名单一个都没命中时退回"只留标量"的保守截断。

    退回分支存在的理由：门票 / web 结果的信封字段名不稳定，白名单写死了就可能一条
    都取不到。宁可少留几个标量字段，也不能让工具回一堆 `{}` 给模型（那和"没搜到"
    在模型眼里是一样的，属于谎报）。
    """
    if fields:
        picked = {
            key: _clean(payload[key])
            for key in fields
            if key in payload and payload[key] is not None and payload[key] != ""
        }
        if picked:
            return picked
    conservative: dict[str, Any] = {}
    for key, value in payload.items():
        if len(conservative) >= 12:
            break
        if key.startswith("_") or value is None or value == "":
            continue
        if isinstance(value, (Mapping, list, tuple)):
            continue
        conservative[str(key)] = _clean(value)
    return conservative


def truncate_items(
    items: Iterable[Any] | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    fields: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """只留前 top_n 条 + 关键字段。**这是 Agent 上下文的唯一入口过滤。**

    输入可以是 pydantic 业务模型（TrainOption / HotelOption / Place / Evidence ...）
    或原始 dict（门票 / geocode / web 结果），输出一律是精简 dict 列表。
    """
    limit = clamp_top_n(top_n)
    result: list[dict[str, Any]] = []
    for item in list(items or [])[:limit]:
        result.append(_pick(_model_dump(item), fields))
    return result


def truncate_result(
    result: ProviderResult[Any] | None,
    *,
    top_n: int = DEFAULT_TOP_N,
    fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    """`ProviderResult` → 精简信封（status / error / total / truncated / items）。

    `total` 与 `shown` 同时保留，是为了让模型知道"还有更多"（可以调大 top_n 或换关键词），
    而不是以为看到的就是全部。`error` 原样保留 —— Hub 明确说过失败原因时不转述。
    """
    items = list(getattr(result, "items", None) or [])
    kept = truncate_items(items, top_n=top_n, fields=fields)
    envelope: dict[str, Any] = {
        "status": str(getattr(result, "status", "") or ""),
        "provider": str(getattr(result, "provider", "") or ""),
        "total": len(items),
        "shown": len(kept),
        "truncated": len(items) > len(kept),
        "items": kept,
    }
    error = getattr(result, "error", None)
    if error:
        envelope["error"] = str(error)[:MAX_TEXT_CHARS]
    return envelope


def _render(
    result: ProviderResult[Any] | None,
    *,
    top_n: int,
    fields: Sequence[str] | None,
    hint: str = "",
) -> str:
    """工具最终返回值：JSON 文本（中文不转义，方便前端与日志直接可读）。"""
    envelope = truncate_result(result, top_n=top_n, fields=fields)
    if hint:
        envelope["hint"] = hint
    return json.dumps(envelope, ensure_ascii=False)


def _failure(tool_name: str, exc: BaseException) -> str:
    """Hub 抛异常时的返回值：如实说明失败，并明确"这不等于没有"。

    Agent 拿到这种结果应该换工具 / 换关键词，而不是据此断言"没有这趟车"。
    """
    return json.dumps(
        {
            "status": "ERROR",
            "provider": "",
            "total": 0,
            "shown": 0,
            "truncated": False,
            "error": f"{tool_name} 调用失败：{type(exc).__name__}: {exc}"[:MAX_TEXT_CHARS],
            "items": [],
            "hint": "这是取数失败，不代表查无结果。请更换数据源或关键词重试，不要据此下结论。",
        },
        ensure_ascii=False,
    )


# ==================================================
# 工具工厂
# ==================================================

#: `hint` 文案：告诉模型这些字段代表什么，避免它把"起价"读成确定价、把截断读成全部。
_HINT_TRAIN = "price_range.min 是最低价；seats 为席别→余票，null 表示未知。"
_HINT_FLIGHT = "price 为含税总价，null 表示未知，不要自行推算。"
_HINT_HOTEL = "price_per_night 是起价（非确定价）；total_price 按 2 人 1 间估算。"
_HINT_TICKET = "返回的是各渠道报价，price 为渠道价，可能不含园区内交通。"
_HINT_POI = "place_id 可用于 poi_detail / route；坐标是 GCJ-02。"
_HINT_ROUTE = "duration_minutes 已按向上取整；verified=false 表示这不是高德给的真实路线。"
_HINT_SOCIAL = "text 已截断；这是攻略观点不是事实，需与官方信息交叉验证。"
_HINT_WEB = "仅用于官网/公告/攻略核实，不能替代机票酒店火车等结构化数据源。"


def build_travel_tools(hub: ProviderHub) -> list[Any]:
    """把 `hub` 的取数方法包成 langchain Tool 列表（闭包持有 hub，不新建 Hub）。

    返回的每个 Tool：`name` 与方法名一致，`description` 是中文的"什么时候用 / 参数 /
    返回什么"，参数 schema 由函数签名推导。返回值统一是**截断后的 JSON 字符串**。
    """
    if hub is None:  # pragma: no cover - 显式失败优于让 Agent 跑到一半才 AttributeError
        raise ValueError("build_travel_tools(hub) 必须传入 ProviderHub 实例")

    # ------------------------------------------------------------------
    # 大交通
    # ------------------------------------------------------------------

    @tool
    def search_trains(
        origin: str, destination: str, depart_date: str, top_n: int = DEFAULT_TOP_N
    ) -> str:
        """查询两个城市之间的火车票（车次、时刻、历时、席别余票、票价区间）。

        什么时候用：行程涉及城际/跨城移动，需要真实车次、出发到达时间或票价时。
        什么时候不用：市内通勤、或用户只要"大概耗时"而不需要具体车次。

        参数：
            origin: 出发城市或车站名，如 "北京" / "成都东"。
            destination: 到达城市或车站名，如 "成都" / "重庆北"。
            depart_date: 出发日期，格式 YYYY-MM-DD，如 "2026-10-01"。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，含 status / total / shown / truncated / items；
            items 每项保留车次号、车型、始发到达站、发到时间、历时分钟、席别余票、
            票价区间（price_range.min 为最低价）。查不到时 status 非 OK 且 items 为空。
        """
        return _run(
            "search_trains",
            hub.search_trains,
            origin,
            destination,
            depart_date,
            top_n=top_n,
            fields=TRAIN_FIELDS,
            hint=_HINT_TRAIN,
        )

    @tool
    def search_flights(
        origin: str,
        destination: str,
        depart_date: str,
        travelers: int = 1,
        top_n: int = DEFAULT_TOP_N,
    ) -> str:
        """查询两个城市之间的机票航班（航班号、航司、起降机场与时间、含税价）。

        什么时候用：远程城际移动、用户提到坐飞机/航班，或火车耗时明显过长时对比用。
        什么时候不用：短途城际（高铁更合适）、或用户明确要求只坐火车。

        参数：
            origin: 出发城市，如 "北京"。
            destination: 到达城市，如 "成都"。
            depart_date: 出发日期，格式 YYYY-MM-DD。
            travelers: 出行人数，默认 1（影响报价口径时使用）。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含 flight_no / airline / 起降机场 / 起降时间 /
            历时分钟 / price（含税总价，null 表示未知）/ is_direct / 余座。
        """
        return _run(
            "search_flights",
            hub.search_flights,
            origin,
            destination,
            depart_date,
            travelers=travelers,
            top_n=top_n,
            fields=FLIGHT_FIELDS,
            hint=_HINT_FLIGHT,
        )

    # ------------------------------------------------------------------
    # 住宿
    # ------------------------------------------------------------------

    @tool
    def search_hotels(
        city: str,
        check_in: str,
        check_out: str,
        travelers: int = 1,
        top_n: int = DEFAULT_TOP_N,
    ) -> str:
        """查询某城市的酒店列表（名称、地址、房型、每晚起价、评分）。

        什么时候用：行程需要过夜，要选住宿区域或估算住宿预算时。
        什么时候不用：当天往返、或用户只要景点门票与交通。

        会**翻多页**去重凑候选（途牛单页约 8 条，工具内部默认翻 2 页，最多约 16 条），
        再按 top_n 截断返回，所以候选覆盖更广、不只看首页。

        参数：
            city: 城市名，如 "成都"。
            check_in: 入住日期，格式 YYYY-MM-DD。
            check_out: 离店日期，格式 YYYY-MM-DD，必须晚于 check_in。
            travelers: 出行人数，默认 1（用于估算房间数与总价）。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含 hotel_id / name / address / 坐标 / room_type /
            price_per_night（起价，非确定价）/ total_price / rating。
        """
        return _run(
            "search_hotels",
            hub.search_hotels,
            city,
            check_in,
            check_out,
            travelers=travelers,
            top_n=top_n,
            fields=HOTEL_FIELDS,
            hint=_HINT_HOTEL,
        )

    # ------------------------------------------------------------------
    # 门票
    # ------------------------------------------------------------------

    @tool
    def search_scenic_tickets(scenic_name: str, top_n: int = DEFAULT_TOP_N) -> str:
        """查询某个景区/景点的门票渠道与价格（按景区名查各渠道报价）。

        什么时候用：行程包含收费景区，需要门票价格或购票渠道时。
        什么时候不用：免费开放街区/公园，或用户不需要门票信息。

        参数：
            scenic_name: 景区名称，尽量用全称，如 "成都大熊猫繁育研究基地"。
            top_n: 最多返回几条（几个渠道），默认 5，上限 20。

        返回：JSON 文本，items 是各渠道报价（渠道名、价格 price、币种、购票链接）。
            渠道价可能不含园内交通；查不到时 status 非 OK。
        """
        return _run(
            "search_scenic_tickets",
            hub.search_scenic_tickets,
            scenic_name,
            top_n=top_n,
            fields=TICKET_FIELDS,
            hint=_HINT_TICKET,
        )

    # ------------------------------------------------------------------
    # 高德：地理编码 / POI / 详情 / 路线
    # ------------------------------------------------------------------

    @tool
    def geocode(address: str, city: str = "") -> str:
        """把一个中文地址/地名解析成经纬度坐标（GCJ-02，可直接用于 route）。

        什么时候用：拿到一个只有名字或地址的地点，需要坐标才能算路线时。
        什么时候不用：已经有坐标（Place.lat/lng）时直接调 route。

        参数：
            address: 结构化地址或地名，如 "成都东站" / "宽窄巷子"。
            city: 可选，限定城市，如 "成都"；不确定时留空。

        返回：JSON 文本，items[0] 含 formatted_address / 省市区 / location（"经度,纬度"）/
            经纬度字段；定位不到时 items 为空。
        """
        return _run(
            "geocode",
            hub.geocode,
            address,
            city or None,
            top_n=1,
            fields=GEOCODE_FIELDS,
        )

    @tool
    def search_poi(
        keywords: str, region: str, types: str = "", top_n: int = DEFAULT_TOP_N
    ) -> str:
        """按关键词搜索某城市/区域内的 POI（景点、餐厅、商圈、车站等真实地点）。

        什么时候用：需要"这个城市有哪些可去的点"，或要找某类场所（如"火锅""博物馆"）时。
        什么时候不用：只要一个已知地点的详情（用 poi_detail）。

        参数：
            keywords: 搜索关键词，如 "宽窄巷子" / "火锅" / "博物馆"。
            region: 限定城市或区域，如 "成都"。
            types: 可选，高德 POI 分类编码（如 "050000" 餐饮）；留空表示不限。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含 place_id / name / type / address / 区县 /
            商圈 / 经纬度（GCJ-02）/ opening_hours / amap_verified。
            place_id 是后续 poi_detail、route 的入参。
        """
        return _run(
            "search_poi",
            hub.search_poi,
            keywords,
            region,
            types=types or None,
            top_n=top_n,
            fields=PLACE_FIELDS,
            hint=_HINT_POI,
        )

    @tool
    def poi_detail(poi_id: str) -> str:
        """查询单个 POI 的详情（营业时间、电话、评分、人均消费等）。

        什么时候用：已从 search_poi 拿到 place_id，要确认营业时间/评分/电话时。
        什么时候不用：还没拿到 poi_id（先调 search_poi）。

        参数：
            poi_id: 高德 POI ID，来自 search_poi 返回的 place_id。

        返回：JSON 文本，items[0] 含 name / type / address / tel / rating /
            opening_hours / cost / 坐标；POI 不存在时 items 为空。
        """
        return _run("poi_detail", hub.poi_detail, poi_id, top_n=1, fields=POI_DETAIL_FIELDS)

    @tool
    def route(origin: str, destination: str, mode: str = "transit", city: str = "") -> str:
        """计算两个坐标之间的真实路线（距离、耗时、预估费用）。

        什么时候用：排一天行程时，需要判断两个点之间"路上要多久"、能否排得下。
        什么时候不用：只有地名没有坐标（先 geocode，或用 search_poi 拿坐标）。

        参数：
            origin: 起点坐标，**经度在前**，格式 "116.397428,39.90923"。
            destination: 终点坐标，同样"经度,纬度"。
            mode: 出行方式，"transit"（公交/地铁，默认）/ "walking" / "driving" / "riding"。
            city: 可选，公交路线建议填城市名，如 "成都"。

        返回：JSON 文本，items 最多 1 条，含 distance_meters / duration_seconds /
            duration_minutes（向上取整）/ estimated_cost / verified。
            verified=false 表示这不是高德返回的真实路线，排程时按未知处理。
        """
        return _run(
            "route",
            hub.route,
            origin,
            destination,
            mode,
            city=city or None,
            top_n=1,
            fields=ROUTE_FIELDS,
            hint=_HINT_ROUTE,
        )

    # ------------------------------------------------------------------
    # 社交攻略 / 联网搜索
    # ------------------------------------------------------------------

    @tool
    def search_xiaohongshu(keyword: str, top_n: int = DEFAULT_TOP_N) -> str:
        """搜索小红书攻略笔记（真实游客的路线、体验、避坑与推荐）。

        什么时候用：需要"怎么玩""什么好吃""值不值得去"这类主观经验与流行度信号时。
        什么时候不用：需要准确价格/时刻/坐标（用火车、酒店、POI 等结构化工具）。

        参数：
            keyword: 搜索关键词，如 "成都三日游" / "宽窄巷子攻略"。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含标题 / 作者 / 发布时间 / 正文 text（已截断）/
            提到的地点 place_mentions / 来源链接。攻略是观点不是事实，需交叉验证。
        """
        return _run(
            "search_xiaohongshu",
            hub.search_xiaohongshu,
            keyword,
            top_n=top_n,
            fields=EVIDENCE_FIELDS,
            hint=_HINT_SOCIAL,
        )

    @tool
    def search_douyin(keyword: str, top_n: int = DEFAULT_TOP_N) -> str:
        """搜索抖音短视频内容（景点实拍、玩法、近期热度）。

        什么时候用：需要视频类口碑/热度信息，或小红书没搜到有用结果时补充。
        什么时候不用：需要结构化价格与时刻数据时。

        参数：
            keyword: 搜索关键词，如 "成都三日游" / "熊猫基地"。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含标题 / 作者 / 发布时间 / 文案 text（已截断）/
            来源链接；结果同为观点类证据，需交叉验证。
        """
        return _run(
            "search_douyin",
            hub.search_douyin,
            keyword,
            top_n=top_n,
            fields=EVIDENCE_FIELDS,
            hint=_HINT_SOCIAL,
        )

    @tool
    def web_search(query: str, top_n: int = DEFAULT_TOP_N) -> str:
        """联网搜索网页（官网公告、开放时间、政策、新闻等）。

        什么时候用：核实景区官网/预约政策/临时闭馆等结构化数据源覆盖不到的事实。
        什么时候不用：查火车票、机票、酒店价格、门票——那些必须用专用工具，
            联网搜索的结果**不能**当成机票酒店火车的价格来源。

        参数：
            query: 搜索词，问句或关键词都可以，如 "成都大熊猫基地 预约 官网 开放时间"。
            top_n: 最多返回几条，默认 5，上限 20。

        返回：JSON 文本，items 每项含 title / url / content（摘要，已截断）。
            搜索后端不可用时 status 非 OK 且带 error —— 那不等于"查不到"，需如实说明。
        """
        return _run(
            "web_search",
            hub.web_search,
            query,
            max_results=clamp_top_n(top_n),
            top_n=top_n,
            fields=WEB_FIELDS,
            hint=_HINT_WEB,
        )

    return [
        search_trains,
        search_flights,
        search_hotels,
        search_scenic_tickets,
        geocode,
        search_poi,
        poi_detail,
        route,
        search_xiaohongshu,
        search_douyin,
        web_search,
    ]


def _run(
    tool_name: str,
    method: Any,
    /,
    *args: Any,
    top_n: int,
    fields: Sequence[str] | None,
    hint: str = "",
    **kwargs: Any,
) -> str:
    """执行一次 Hub 调用并把结果截断成 JSON 文本（异常也不抛给 Agent）。

    同时传 `tool_name` 与 `method`（而不是只传 method 靠 `__name__` 猜）：工具名是
    给模型和前端看的稳定标识，不能被 Hub 侧方法改名或被测试替身替换影响。
    """
    try:
        result = method(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 取数失败必须变成"可读的失败"，不能中断 Agent 循环
        return _failure(tool_name, exc)
    return _render(result, top_n=top_n, fields=fields, hint=hint)


#: ToolLoader 的清单约定：把 `app/travel_tools.py` 当 Tool 文件扫描时读这个列表。
#: 注意 `build_travel_tools(hub)` 需要 hub 实例，所以这里是**空的占位** ——
#: 需要 hub 的工具走 `tools=build_travel_tools(hub)` 手动传入，不走目录自动发现。
TOOLS: list[Any] = []


__all__ = [
    "DEFAULT_TOP_N",
    "MAX_TOP_N",
    "TOOLS",
    "TOOL_DISPLAY_NAMES",
    "build_travel_tools",
    "clamp_top_n",
    "truncate_items",
    "truncate_result",
]
