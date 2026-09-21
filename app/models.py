"""TravelPlan 业务数据模型（PRD §16 与 §28 的 plan.json 结构）。

为什么每个实时结果都带 provenance
--------------------------------
产品原则是 Evidence First：任何一个价格、车次、坐标、营业时间都必须能回答
"谁、在什么时候、通过哪个链接给的"。所以 provider / source_id / source_url /
fetched_at / raw 收敛成一个基类，任何来自 Provider 的选项都继承它 ——
少一个字段，审计链就断了，前端也没法显示"查询时间"。

为什么模型里放容错取值工具（coerce_*）
------------------------------------
脏数据有两个来源：LLM 返回的意图 JSON（字段缺失、类型乱、null）和 Provider 返回的
字段差异（价格是字符串、历时是 "3h"、日期格式不统一）。这两类脏数据如果各自在
workflow 里就地处理，容错规则会散落各处在不同阶段给出不同结果。这里集中一份，
planner 也复用，保证"同一个字符串在哪儿解析结果都一样"。

为什么不在这里写业务判断
------------------------
模型只负责"结构正确 + 可序列化"，Trust / Ad Risk / 预算 / 时间可行性都在 planner.py。
"""

from __future__ import annotations

import json
import re
import datetime as _dt
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ==================================================
# 时间基准
# ==================================================


def utcnow() -> datetime:
    """我们自己的时间戳一律用 UTC aware。

    带时区是硬要求：日志、审计、前端"查询于 14:32"都要能对齐，naive datetime
    在跨时区展示时会悄悄偏移一天。
    """
    return datetime.now(timezone.utc)


# ==================================================
# 容错取值工具
# ==================================================

#: 中文数字。LLM 把"玩五天"直接抄成字符串是很常见的，纯正则取数字会整个丢掉。
_CN_DIGITS: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """"十二" / "二十" / "两" → 12 / 20 / 2。"""
    if not text:
        return None
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for char in text:
        if char not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[char]
    return total


def coerce_int(value: Any, default: int | None = None) -> int | None:
    """尽力把任意值变成 int：'5' / '5天' / '五天' / 5.0 → 5。失败返回 default。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value else default

    text = str(value).strip()
    if not text:
        return default
    match = re.search(r"-?\d+", text)
    if match:
        return int(match.group())
    chinese = _cn_to_int(re.sub(r"[^零一二三四五六七八九十两]", "", text))
    return chinese if chinese is not None else default


def coerce_float(value: Any, default: float | None = None) -> float | None:
    """尽力把任意值变成 float：'¥1,200' / '1200元' / '3.5' → 1200.0 / 3.5。

    空串当 default 而不是 0：途牛用空串表示"这个席别不存在"，当成 0 会算出一张免费机票。
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else default

    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return float(match.group())
    except ValueError:
        return default


def coerce_str(value: Any, default: str = "") -> str:
    """任意值 → 去空白字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip() or default


def coerce_str_list(value: Any) -> list[str]:
    """任意值 → 干净的字符串列表。

    容忍：None → []、单字符串 → [x]（destination:"成都" 而不是 ["成都"]）、
    带 null 元素的数组、以及 {"food": true} 这种被 LLM 写成对象的偏好。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [coerce_str(key) for key, flag in value.items() if flag and coerce_str(key)]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            text = coerce_str(item)
            if text:
                result.append(text)
        return result
    text = coerce_str(value)
    return [text] if text else []


def coerce_datetime(value: Any, default: datetime | None = None) -> datetime | None:
    """解析 Provider / LLM 给的时间文本，支持 ISO、'2026-10-01 07:35'、'10月1日 07:35'。

    解析不出来返回 default（通常是 None），绝不猜一个"大概"的时间。
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)

    text = str(value).strip()
    if not text:
        return default

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    cleaned = (
        text.replace("年", "-").replace("月", "-").replace("日", " ")
        .replace("/", "-").replace(".", "-").replace("T", " ")
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if fmt.startswith("%m"):
            parsed = parsed.replace(year=date.today().year)
        return parsed
    return default


def parse_date_text(value: Any, reference: date | None = None) -> date | None:
    """把日期文本解析成 date；解析不了返回 None。

    支持 '2026-10-01'、'2026/10/1'、'2026年10月1日'、'10月1日'、'10-01'。
    只有"月日"没有年份时：用 reference（或今天）的年份，若因此落到过去 30 天以前，
    就视为明年 —— 用户说"1月5日"时几乎不可能指已过去的那个 1 月。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = coerce_str(value)
    if not text:
        return None

    has_year = bool(re.search(r"\d{4}\s*[年\-/.]", text))
    parsed = coerce_datetime(text)
    if parsed is None:
        return None

    if not has_year:
        base = reference or date.today()
        candidate = parsed.date().replace(year=base.year)
        if (base - candidate).days > 30:
            candidate = candidate.replace(year=base.year + 1)
        return candidate
    return parsed.date()


def _pick(data: dict, *keys: str) -> Any:
    """按候选键名取值（兼容驼峰 / 别名）；全部缺失返回 None。"""
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


# ==================================================
# Provider 信封
# ==================================================


#: `json.loads` 合法的结果里包含 `null`，所以不能用 None 当"没解析出来"的哨兵。
_MISSING = object()


def parse_envelope(raw: Any) -> dict:
    """把 Provider 工具的返回统一成 dict。

    Plugin 的返回是 **JSON 字符串**（`status/provider/fetched_at/query/count/items/error`），
    这里负责拆掉这一层；传入 dict 时原样返回，方便测试和内部直接调用。

    **顶层是 JSON 数组**的情况也要认：12306 MCP 的 `get-tickets` 直接返回
    `[{车次...}, ...]`，没有信封。这种裸数组一律包成
    `{"status": "OK", "items": [...]}` —— 数组本身是有效数据，
    把它当成"解析失败"会让一次成功的查询被降级成 INVALID_RESPONSE。

    解析失败返回空 dict —— 上层据此判定 UNAVAILABLE，而不是抛异常中断整个 run。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (list, tuple)):
        return {"status": "OK", "items": list(raw)}
    text = coerce_str(raw)
    if not text:
        return {}

    loaded: Any = _MISSING
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        pass

    if loaded is _MISSING:
        # 容忍前后有杂音（日志行、进度提示）：分别试数组与对象两种边界。
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = text.find(opener), text.rfind(closer)
            if 0 <= start < end:
                try:
                    loaded = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
                break
    if loaded is _MISSING:
        return {}
    if isinstance(loaded, list):
        return {"status": "OK", "items": loaded}
    return loaded if isinstance(loaded, dict) else {}


def envelope_items(raw: Any) -> list[dict]:
    """取信封里的 items（只保留 dict 元素）。"""
    items = parse_envelope(raw).get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


# ==================================================
# PRD §16.2 Provider 共同字段
# ==================================================


class ProviderProvenance(BaseModel):
    """所有实时结果的来源信息（PRD §16.2）。核心 provenance 字段不可删。"""

    provider: str = "unknown"
    source_id: str | None = None
    source_url: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fetched_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """naive 时间一律视为 UTC：审计里不允许出现"不知道什么时区"的时间。"""
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @field_validator("source_id", "source_url", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if coerce_str(value) == "" else coerce_str(value)


# ==================================================
# PRD §16.1 TripIntent
# ==================================================

#: pace 的合法值。LLM 返回别的词一律降级成 balanced，避免下游要处理无穷多枚举。
PACES = ("relaxed", "balanced", "packed")


class TripIntent(BaseModel):
    """"用户到底想要什么"的结构化表示（PRD §16.1）。

    `from_llm` 是这个类真正的入口：LLM 的输出不可信，任何字段都可能缺失或类型错误，
    但一次 run 不能因为一个 null 就整个失败，所以这里全部走降级而不是抛异常。
    """

    destination: list[str] = Field(default_factory=list)
    origin: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    days: int = 1
    travelers: int = 1
    budget_total: float | None = None
    preferences: list[str] = Field(default_factory=list)
    pace: str = "balanced"
    transport_preferences: list[str] = Field(default_factory=list)
    hotel_preferences: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    # --- 引导式旅程带来的结构化偏好（app/selection.py 负责解释这些值） ---
    # 为什么要有结构化字段：把"用户点过的按钮"重新拼成自然语言再解析一遍，等于把用户
    # 明确表达过的信息交给模型去猜。用户点过的优先级必须高于任何自然语言解析结果。
    #: train | flight | any | auto
    transport_mode: str = "auto"
    #: value | fastest | cheapest | comfort | auto
    transport_priority: str = "auto"
    #: few_transfers | no_early | no_red_eye | any_time（多选）
    transport_constraints: list[str] = Field(default_factory=list)
    #: value | location | rating | comfort | transit | auto
    hotel_priority: str = "auto"
    hotel_max_price_per_night: float | None = None
    hotel_min_rating: float | None = None
    #: 最低星级。**当前数据源（途牛）不返回星级字段**，所以它被记录但不参与排序 ——
    #: 如实标注而不是拿评分冒充星级（见 docs/02 与会话接口的 capabilities）。
    hotel_min_star: float | None = None
    hotel_room_type: str | None = None
    #: yes | no | auto —— 是否接受中途换酒店（当前产品全程只选一家，见 docs/02）
    hotel_allow_change: str | None = None
    #: place_id → MUST / WANT / REJECT（缺省即 NEUTRAL）
    place_selections: dict[str, str] = Field(default_factory=dict)
    #: 这次规划是谁发起的：quick（一句话）/ guided（引导式）/ cli
    source: str = "quick"
    source_session_id: str | None = None

    @property
    def nights(self) -> int:
        """住宿晚数 = 天数 - 1，最小 0（当天来回没有住宿）。"""
        return max(self.days - 1, 0)

    @property
    def date_range(self) -> str:
        """给人和日志看的日期范围文本；日期缺失时如实说明未定。"""
        if self.start_date and self.end_date:
            return (
                f"{self.start_date.isoformat()} ~ {self.end_date.isoformat()}"
                f"（{self.days} 天 {self.nights} 晚）"
            )
        if self.start_date:
            return f"{self.start_date.isoformat()} 出发（共 {self.days} 天，结束日期未定）"
        return f"日期未定（共 {self.days} 天）"

    @classmethod
    def from_llm(cls, data: dict | str | None) -> "TripIntent":
        """从 LLM 输出构造 TripIntent，**保证不抛异常**（PRD §16.1）。

        降级规则：
        * preferences / 各 list 为 null → []
        * pace 为 null 或非法 → "balanced"
        * travelers 为 null → 1；days 为 "5"/"五天" → 5
        * days 缺失但有起止日期 → 用日期算
        * 日期解析不出来 → None，且 days 缺失时回填 1
        * destination 传成单个字符串 → 包成 list
        最后仍然兜一层 try/except：宁可退化成"一天、无目的地"的空意图并让 workflow 去追问，
        也不能让一次 run 因为脏 JSON 直接崩。
        """
        payload = parse_envelope(data) if isinstance(data, str) else data
        if not isinstance(payload, dict):
            payload = {}

        try:
            destination = coerce_str_list(
                _pick(
                    payload,
                    "destination",
                    "destination_city",
                    "destinations",
                    "city",
                    "cities",
                    "dest",
                    "to",
                    "目的地",
                )
            )
            origin = coerce_str(
                _pick(payload, "origin", "origin_city", "from", "from_city", "departure_city", "出发地")
            ) or None

            reference = date.today()
            start_date = parse_date_text(
                _pick(payload, "start_date", "startDate", "start", "departure_date", "出发日期"),
                reference,
            )
            end_date = parse_date_text(
                _pick(payload, "end_date", "endDate", "end", "return_date", "结束日期"),
                start_date or reference,
            )

            raw_days = _pick(payload, "days", "day_count", "duration", "天数")
            days = coerce_int(raw_days)
            if days is None:
                if start_date and end_date:
                    days = (end_date - start_date).days + 1
                else:
                    days = 1
            days = max(1, min(days, 60))

            travelers = coerce_int(_pick(payload, "travelers", "people", "persons", "pax"), 1) or 1
            travelers = max(1, min(travelers, 50))

            pace = coerce_str(_pick(payload, "pace", "节奏")).lower() or "balanced"
            if pace not in PACES:
                pace = "balanced"

            return cls(
                destination=destination,
                origin=origin,
                start_date=start_date,
                end_date=end_date,
                days=days,
                travelers=travelers,
                budget_total=coerce_float(
                    _pick(payload, "budget_total", "budget", "budgetTotal", "预算")
                ),
                preferences=coerce_str_list(_pick(payload, "preferences", "interests", "偏好")),
                pace=pace,
                transport_preferences=coerce_str_list(
                    _pick(
                        payload,
                        "transport_preferences",
                        "transport_preference",
                        "transport",
                        "交通偏好",
                    )
                ),
                hotel_preferences=coerce_str_list(
                    _pick(
                        payload,
                        "hotel_preferences",
                        "hotel_level",
                        "hotel",
                        "住宿偏好",
                    )
                ),
                constraints=coerce_str_list(
                    _pick(payload, "constraints", "avoid", "限制", "约束")
                ),
            )
        except Exception:  # noqa: BLE001 —— 意图解析绝不允许让 run 失败
            return cls()


# ==================================================
# PRD §16.3 - §16.5 大交通与住宿
# ==================================================


class FlightOption(ProviderProvenance):
    """一张可选航班（PRD §16.3）。price 是含税总价，缺失就是 None。"""

    kind: Literal["flight"] = "flight"
    flight_no: str
    origin: str | None = None
    destination: str | None = None
    departure_airport: str | None = None
    arrival_airport: str | None = None
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    duration_minutes: int | None = None
    price: float | None = None
    currency: str = "CNY"
    airline: str | None = None
    is_direct: bool | None = None
    remaining_seats: int | None = None
    price_note: str | None = None
    #: 机场往返市区的额外时间（分钟）。PRD §24 要求比"门到门"而不是"飞行 2 小时"，
    #: 而机场接驳不在航班的 duration_minutes 里，所以单独一列；取值优先级是
    #: 高德真实路线 > 具名常量兜底（拿不到真实路线时 estimated=True 会体现在 selection_reason 里）。
    airport_transfer_minutes: int | None = None
    door_to_door_minutes: int | None = None
    #: 为什么选中/没选中它。前端 Transport Compare 直接展示，不进 LLM 二次改写。
    selection_reason: str | None = None

    @property
    def label(self) -> str:
        """比较面板上的短标签，价格未知时不假装知道。"""
        parts = [self.flight_no or "未知航班", self.airline or "", f"¥{self.price:g}" if self.price else "价格未知"]
        return " ".join(part for part in parts if part)

    @classmethod
    def from_item(
        cls,
        item: dict,
        *,
        provider: str = "tuniu",
        fetched_at: datetime | None = None,
        source_id: str | None = None,
        source_url: str | None = None,
        origin: str | None = None,
        destination: str | None = None,
    ) -> "FlightOption":
        """映射 tuniu_travel flight 信封里的一个 item。

        字段名与 `tools/flight.py::_normalize_item` 一一对应（departure_airport 已合并
        航站楼、price 已含税、duration_minutes 已换算成分钟），这里只做类型收口。
        """
        return cls(
            provider=provider,
            source_id=source_id,
            source_url=source_url,
            fetched_at=fetched_at or utcnow(),
            raw=item,
            flight_no=coerce_str(item.get("flight_no"), "未知航班"),
            origin=origin,
            destination=destination,
            departure_airport=coerce_str(item.get("departure_airport")) or None,
            arrival_airport=coerce_str(item.get("arrival_airport")) or None,
            departure_at=coerce_datetime(item.get("departure_at") or item.get("departure_time")),
            arrival_at=coerce_datetime(item.get("arrival_at") or item.get("arrival_time")),
            duration_minutes=coerce_int(item.get("duration_minutes")),
            price=coerce_float(item.get("price")),
            currency=coerce_str(item.get("currency"), "CNY") or "CNY",
            airline=coerce_str(item.get("airline")) or None,
            is_direct=item.get("is_direct") if isinstance(item.get("is_direct"), bool) else None,
            remaining_seats=coerce_int(item.get("remaining_seats")),
        )


class TrainOption(ProviderProvenance):
    """一趟可选火车（PRD §16.4）。seats 是"席别→余票"，price_range 是有效席别价格区间。"""

    kind: Literal["train"] = "train"
    train_no: str
    origin_station: str | None = None
    destination_station: str | None = None
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    duration_minutes: int | None = None
    seats: dict[str, int | None] = Field(default_factory=dict)
    price_range: dict[str, float | None] = Field(default_factory=dict)
    currency: str = "CNY"
    train_type: str | None = None
    #: 车站往返市区的额外时间（分钟）与门到门总时长，语义同 FlightOption。
    airport_transfer_minutes: int | None = None
    door_to_door_minutes: int | None = None
    selection_reason: str | None = None

    @property
    def price(self) -> float | None:
        """单一可比价格：取区间下限（"最低价"语义与途牛 train 工具一致），无价返回 None。"""
        value = coerce_float((self.price_range or {}).get("min"))
        return value

    @property
    def label(self) -> str:
        price = self.price
        parts = [self.train_no or "未知车次", self.train_type or "", f"¥{price:g}" if price else "价格未知"]
        return " ".join(part for part in parts if part)

    @classmethod
    def from_item(
        cls,
        item: dict,
        *,
        provider: str = "12306",
        fetched_at: datetime | None = None,
        source_id: str | None = None,
        source_url: str | None = None,
    ) -> "TrainOption":
        """映射 train 信封里的一个 item；兼容 `seats` / `seats_available` 两种键名。"""
        seats = item.get("seats") if isinstance(item.get("seats"), dict) else item.get("seats_available")
        price_range = item.get("price_range") if isinstance(item.get("price_range"), dict) else {}
        if not price_range and isinstance(item.get("prices"), dict):
            numeric = [coerce_float(value) for value in item["prices"].values()]
            numeric = [value for value in numeric if value is not None]
            if numeric:
                price_range = {"min": min(numeric), "max": max(numeric)}

        return cls(
            provider=provider,
            source_id=source_id,
            source_url=source_url,
            fetched_at=fetched_at or utcnow(),
            raw=item,
            train_no=coerce_str(item.get("train_no"), "未知车次"),
            origin_station=coerce_str(item.get("origin_station")) or None,
            destination_station=coerce_str(item.get("destination_station")) or None,
            departure_at=coerce_datetime(item.get("departure_at") or item.get("departure_time")),
            arrival_at=coerce_datetime(item.get("arrival_at") or item.get("arrival_time")),
            duration_minutes=coerce_int(item.get("duration_minutes")),
            seats={str(key): coerce_int(value) for key, value in (seats or {}).items()},
            price_range={
                str(key): coerce_float(value) for key, value in (price_range or {}).items()
            },
            currency=coerce_str(item.get("currency"), "CNY") or "CNY",
            train_type=coerce_str(item.get("train_type")) or None,
        )


#: 大交通的联合类型。两个模型的必填字段名不同（flight_no / train_no），
#: pydantic v2 的 smart union 能按必填字段区分，不需要额外的 discriminator。
TransportOption = FlightOption | TrainOption


class HotelOption(ProviderProvenance):
    """一个可选酒店（PRD §16.5）。

    `price_note` 是免责声明位：途牛 hotel 只给"起价"，必须保留原样（前端显示"¥420 起"），
    预算里也要能看出这一项不是确定价。
    """

    hotel_id: str | None = None
    name: str
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    check_in: date | None = None
    check_out: date | None = None
    room_type: str | None = None
    price_per_night: float | None = None
    total_price: float | None = None
    rating: float | None = None
    cancellation_policy: str | None = None
    price_note: str | None = None
    city: str | None = None
    business_area: str | None = None
    #: 为什么选中/没选中它。前端 Hotel Compare 直接展示，不进 LLM 二次改写。
    selection_reason: str | None = None

    @property
    def nights(self) -> int | None:
        """入住晚数；缺任一日期就返回 None（不猜）。"""
        if self.check_in and self.check_out:
            return max((self.check_out - self.check_in).days, 0)
        return None

    @property
    def nightly(self) -> float | None:
        """逐晚价格：优先 Provider 直接给的，否则用总价反推。"""
        if self.price_per_night is not None:
            return self.price_per_night
        nights = self.nights
        if self.total_price is not None and nights:
            return round(self.total_price / nights, 2)
        return None

    @classmethod
    def from_item(
        cls,
        item: dict,
        *,
        provider: str = "tuniu",
        fetched_at: datetime | None = None,
        source_id: str | None = None,
        source_url: str | None = None,
        check_in: date | None = None,
        check_out: date | None = None,
        travelers: int = 1,
    ) -> "HotelOption":
        """映射 hotel 信封里的一个 item。

        途牛 hotel 不返回坐标，lat/lng 只能由高德 geocode 之后回填，这里保持 None ——
        补一个假坐标会让"距当天主要区域"变成假指标。
        价格是"起价"，写进 price_note 而不是偷偷当成确定价。
        total_price = 每间夜价格 × 晚数 × 房间数（按 2 人 1 间折算），是**用真实单价做的乘法**，
        不是编的价；房间数口径写在 raw 里，前端仍可只显示"¥420 起"。
        """
        nightly = coerce_float(item.get("price_per_night"))
        nights = max((check_out - check_in).days, 0) if (check_in and check_out) else None
        rooms = max(1, (travelers + 1) // 2)
        total = round(nightly * nights * rooms, 2) if (nightly is not None and nights) else None
        return cls(
            provider=provider,
            source_id=source_id,
            source_url=source_url,
            fetched_at=fetched_at or utcnow(),
            raw=item,
            hotel_id=coerce_str(item.get("hotel_id")) or None,
            name=coerce_str(item.get("name"), "未知酒店"),
            address=coerce_str(item.get("address")) or None,
            lat=coerce_float(item.get("lat") or item.get("latitude")),
            lng=coerce_float(item.get("lng") or item.get("longitude")),
            check_in=check_in,
            check_out=check_out,
            room_type=coerce_str(item.get("room_type")) or None,
            price_per_night=nightly,
            total_price=coerce_float(item.get("total_price"), total),
            rating=coerce_float(item.get("rating")),
            cancellation_policy=coerce_str(item.get("cancellation_policy")) or None,
            price_note=f"{coerce_float(item.get('price_per_night')):g} 起价/晚" if nightly is not None else None,
            city=coerce_str(item.get("city")) or None,
            business_area=coerce_str(item.get("business_area")) or None,
        )


# ==================================================
# PRD §16.6 Place
# ==================================================


class Place(BaseModel):
    """一个被验证过的地点（PRD §16.6）。

    `merged_from` 记录去重时被吸收掉的 place_id：证据是通过 place_evidence 挂在
    place_id 上的，合并后必须能把这些证据迁到代表点上，否则证据链会在去重这一步断掉。
    """

    place_id: str
    name: str
    normalized_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    type: str = ""
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    city: str | None = None
    district: str | None = None
    #: 商圈（高德 POI 的 business_area，如"高升桥"）。它不是行政区，
    #: 但比 district 更能说明"这个点在哪一片"，用作行程天的区域标签更贴切。
    business_area: str | None = None
    opening_hours: str | None = None
    expected_duration_minutes: int | None = None
    amap_verified: bool = False
    #: 这个 place 是哪一次 Provider 调用返回的（高德 POI 查询的 source_id）。
    #: 没有它的话，"只有高德 POI、没有社媒证据"的地点（真实 run 里是绝大多数）
    #: 在行程 item 上没有任何来源可指 —— place → source 这条链会断在这里。
    source_id: str | None = None
    merged_from: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        """normalized_name 为空时补一个基础键；不做重归一化（那是 planner 的职责）。"""
        if not self.normalized_name:
            self.normalized_name = basic_name_key(self.name)

    @property
    def alias_key(self) -> str:
        """用于去重的归一化名字（PRD §20）。

        为什么要延迟导入：归一化规则住在 planner（本项目算法层），而 planner 依赖 models，
        模块级互相 import 会形成循环。这里只在真正取值时导入，模块加载顺序就不再是约束。
        """
        try:
            from app.planner import normalize_place_name

            return normalize_place_name(self.name, city=self.city) or basic_name_key(self.name)
        except Exception:  # noqa: BLE001 —— models 被单独导入（非包）时退化为基础键
            return basic_name_key(self.name)

    @property
    def coords(self) -> tuple[float, float] | None:
        """(lat, lng)，任一缺失返回 None。"""
        if self.lat is None or self.lng is None:
            return None
        return (self.lat, self.lng)

    @classmethod
    def from_poi(
        cls,
        item: dict,
        *,
        place_id: str | None = None,
        city: str | None = None,
        aliases: list[str] | None = None,
        type_hint: str | None = None,
        expected_duration_minutes: int | None = None,
        source_id: str | None = None,
    ) -> "Place":
        """映射高德 `search_poi` / `get_poi_detail` 的 item（longitude/latitude → lng/lat）。"""
        name = coerce_str(item.get("name"))
        poi_id = coerce_str(place_id or item.get("poi_id") or item.get("id"))
        return cls(
            place_id=poi_id or name,
            name=name,
            source_id=source_id,
            aliases=coerce_str_list(aliases),
            type=coerce_str(item.get("type") or type_hint),
            lat=coerce_float(item.get("latitude") or item.get("lat")),
            lng=coerce_float(item.get("longitude") or item.get("lng")),
            address=coerce_str(item.get("address")) or None,
            city=coerce_str(item.get("city") or city) or None,
            district=coerce_str(item.get("district")) or None,
            business_area=coerce_str(item.get("business_area")) or None,
            opening_hours=coerce_str(item.get("opening_hours")) or None,
            expected_duration_minutes=expected_duration_minutes,
            amap_verified=bool(poi_id),
        )


# ==================================================
# PRD §16.7 Evidence
# ==================================================


class Evidence(ProviderProvenance):
    """一条攻略 / 网页 / 官方信息（PRD §16.7）。

    继承 provenance：Evidence 本来就是"某个 Source 在某时刻给的文本"，
    `raw_metrics` 单独放互动数据（点赞/评论），Trust 与 Ad Risk 都从它取信号。
    """

    id: str
    source_type: str = "social"
    title: str = ""
    author: str | None = None
    published_at: datetime | None = None
    text: str = ""
    place_mentions: list[str] = Field(default_factory=list)
    specific_dishes: list[str] = Field(default_factory=list)
    raw_metrics: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_content(
        cls,
        content: dict,
        *,
        evidence_id: str,
        source_type: str,
        provider: str,
        source_id: str | None = None,
        source_url: str | None = None,
        fetched_at: datetime | None = None,
    ) -> "Evidence":
        """映射社交 / 网页抓取结果（TikHub / MediaCrawler / Tavily 归一化后的形状）。"""
        metrics = content.get("raw_metrics") or content.get("metrics")
        return cls(
            id=evidence_id,
            source_type=source_type,
            provider=provider,
            source_id=source_id,
            source_url=source_url,
            fetched_at=fetched_at or utcnow(),
            raw=content,
            title=coerce_str(content.get("title")),
            author=coerce_str(content.get("author") or content.get("nickname")) or None,
            published_at=coerce_datetime(
                content.get("published_at") or content.get("create_time") or content.get("time")
            ),
            text=coerce_str(content.get("text") or content.get("content") or content.get("desc")),
            place_mentions=coerce_str_list(content.get("place_mentions")),
            specific_dishes=coerce_str_list(content.get("specific_dishes")),
            raw_metrics=metrics if isinstance(metrics, dict) else {},
        )


# ==================================================
# PRD §16.8 Decision
# ==================================================


class DecisionStatus(str, Enum):
    """决策状态（PRD §16.8）。REVISION 是 Critic 阶段"通过但必须改"的显式表达。"""

    KEEP = "KEEP"
    REJECT = "REJECT"
    SELECT = "SELECT"
    PASS = "PASS"
    USED = "USED"
    NOT_USED = "NOT_USED"
    REVISION = "REVISION"


class Decision(BaseModel):
    """一条可审计的决策（PRD §16.8）：谁、对什么、做了什么判断、为什么。"""

    entity_id: str
    status: DecisionStatus
    agent_or_stage: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    reason_text: str = ""
    scores: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)


# ==================================================
# PRD §12 / §32 路线
# ==================================================


class RouteOption(BaseModel):
    """高德返回的一段真实路线（PRD §12）。

    `verified=False` 表示这段不是高德给的（或高德调用失败）。此时时间只用于排程占位，
    行程里必须标 `route_unverified`，Critic 必须降低置信度（PRD §32）。
    """

    origin_place_id: str | None = None
    destination_place_id: str | None = None
    mode: str = "transit"
    distance_meters: int | None = None
    duration_seconds: int | None = None
    estimated_cost: float | None = None
    provider: str = "amap"
    fetched_at: datetime = Field(default_factory=utcnow)
    verified: bool = True

    @property
    def duration_minutes(self) -> int | None:
        """向上取整到分钟：排程里宁可多留一点时间，也不要因为截断而挤掉缓冲。"""
        if self.duration_seconds is None:
            return None
        return int((self.duration_seconds + 59) // 60)

    @classmethod
    def from_envelope(cls, raw: Any) -> "RouteOption | None":
        """从高德 route 工具的信封构造：路径字段平铺在顶层，失败时全为 null。

        失败（status != OK）一律返回 None —— PRD §32 明确不允许在这里猜一条路线。
        """
        payload = parse_envelope(raw)
        if not payload:
            return None
        status = coerce_str(payload.get("status")).upper()
        if status and status != "OK":
            return None
        plan = {}
        items = payload.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            plan = items[0]
        distance = payload.get("distance_meters") or plan.get("distance_meters")
        duration = payload.get("duration_seconds") or plan.get("duration_seconds")
        if distance is None and duration is None:
            return None
        return cls(
            mode=coerce_str(payload.get("mode") or plan.get("mode"), "transit") or "transit",
            distance_meters=coerce_int(distance),
            duration_seconds=coerce_int(duration),
            estimated_cost=coerce_float(payload.get("estimated_cost", plan.get("estimated_cost"))),
            provider=coerce_str(payload.get("provider"), "amap") or "amap",
            fetched_at=coerce_datetime(payload.get("fetched_at")) or utcnow(),
            verified=True,
        )


# ==================================================
# PRD §28 itinerary item / day
# ==================================================

#: item 类型（与 FRONTEND_DESIGN §11 一致）。
ItemType = Literal["attraction", "food", "hotel", "transport", "activity", "free_time"]

#: 价格可信度：realtime=Provider 实时查到的；estimated=我们的估算；unknown=没有价格。
PriceType = Literal["realtime", "estimated", "unknown"]


class TravelLeg(BaseModel):
    """item 之间的移动（PRD §28 的 travel_from_previous）。"""

    mode: str = "transit"
    distance_meters: int | None = None
    duration_seconds: int | None = None
    #: 高德 transit 返回的票价（元）。它是真实数据，预算里的"市内交通"要用它，
    #: 拿不到时才退回估算（PRD §23：不允许把估算写成实时价格）。
    estimated_cost: float | None = None
    verified: bool = True
    from_place_id: str | None = None
    to_place_id: str | None = None
    provider: str | None = None
    estimated: bool = False

    @property
    def duration_minutes(self) -> int | None:
        if self.duration_seconds is None:
            return None
        return int((self.duration_seconds + 59) // 60)

    @classmethod
    def from_route(
        cls,
        route: RouteOption | None,
        *,
        from_place_id: str | None = None,
        to_place_id: str | None = None,
        mode: str = "transit",
        fallback_minutes: int | None = None,
    ) -> "TravelLeg":
        """从 RouteOption 构造；路线缺失/未验证时只填估算时长并标记 verified=False。"""
        if route is None:
            return cls(
                mode=mode,
                from_place_id=from_place_id,
                to_place_id=to_place_id,
                duration_seconds=fallback_minutes * 60 if fallback_minutes else None,
                verified=False,
                estimated=fallback_minutes is not None,
            )
        return cls(
            mode=route.mode or mode,
            distance_meters=route.distance_meters,
            duration_seconds=route.duration_seconds,
            estimated_cost=route.estimated_cost,
            verified=route.verified,
            from_place_id=from_place_id or route.origin_place_id,
            to_place_id=to_place_id or route.destination_place_id,
            provider=route.provider,
            estimated=not route.verified,
        )


class ItineraryItem(BaseModel):
    """行程里的一条安排（PRD §28 逐字段）。

    时间用 "HH:MM" 字符串而不是 datetime：行程的"日期"属于 ItineraryDay，
    item 只表达"当天几点"，这也正好对上 PRD 日志里的 `item="武侯祠 14:00"`。
    需要算数时用 start_minutes / end_minutes 属性。
    """

    id: str
    type: ItemType = "attraction"
    place_id: str | None = None
    name: str = ""
    lat: float | None = None
    lng: float | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration_minutes: int | None = None
    travel_from_previous: TravelLeg | None = None
    price: float | None = None
    price_type: PriceType = "unknown"
    trust_score: float | None = None
    ad_risk: float | None = None
    reason: str = ""
    #: 推荐理由的拆解（Trust 的六项分项、命中偏好、路线适配），供前端 Evidence Drawer 逐条打勾。
    reason_points: list[str] = Field(default_factory=list)
    #: 风险提示（Ad Risk 分项与"证据只有一条"这类判断）。空串表示没有需要提示的风险。
    risk_note: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    #: transport 类 item 的交通方式（flight/train/walk/transit/driving），
    #: 用于选"提前到机场/车站"这类缓冲常量 —— 飞机和高铁的安全 buffer 不一样。
    transport_mode: str | None = None
    area: str | None = None
    booking_note: str | None = None

    @property
    def start_minutes(self) -> int | None:
        # 延迟导入：parse_clock 住在 planner，models ← planner 是单向依赖，不能模块级互导。
        from app.planner import parse_clock

        return parse_clock(self.start_time)

    @property
    def end_minutes(self) -> int | None:
        from app.planner import parse_clock

        if self.end_time is not None:
            return parse_clock(self.end_time)
        start = self.start_minutes
        if start is None or self.duration_minutes is None:
            return None
        return start + self.duration_minutes

    @property
    def coords(self) -> tuple[float, float] | None:
        if self.lat is None or self.lng is None:
            return None
        return (self.lat, self.lng)


class ItineraryDay(BaseModel):
    """一天行程（PRD §28）。day_index 从 0 开始，与 plan_items 表的 day_index 对齐。"""

    day_index: int = 0
    # 注解用 _dt.date（datetime 模块）而不是 date：字段名 date 会遮蔽 datetime.date 这个类，
    # 写成 datetime.date 会解析到 datetime.datetime.date 这个方法描述符，pydantic 直接报 TypeError。
    date: _dt.date | None = None
    area: str | None = None
    items: list[ItineraryItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def start_minutes(self) -> int | None:
        starts = [item.start_minutes for item in self.items if item.start_minutes is not None]
        return min(starts) if starts else None

    @property
    def end_minutes(self) -> int | None:
        ends = [item.end_minutes for item in self.items if item.end_minutes is not None]
        return max(ends) if ends else None

    def item_by_id(self, item_id: str) -> ItineraryItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None


# ==================================================
# PRD §23 预算
# ==================================================

#: 预算分类键（PRD §23 + FRONTEND_DESIGN §17）。固定常量，避免各处拼字符串拼错。
BUDGET_CATEGORY_TRANSPORT = "交通"
BUDGET_CATEGORY_HOTEL = "住宿"
BUDGET_CATEGORY_TICKET = "门票"
BUDGET_CATEGORY_MEAL = "餐饮估算"
BUDGET_CATEGORY_CITY_TRANSPORT = "市内交通"
BUDGET_CATEGORY_OTHER = "其他"
BUDGET_CATEGORIES = (
    BUDGET_CATEGORY_TRANSPORT,
    BUDGET_CATEGORY_HOTEL,
    BUDGET_CATEGORY_TICKET,
    BUDGET_CATEGORY_MEAL,
    BUDGET_CATEGORY_CITY_TRANSPORT,
    BUDGET_CATEGORY_OTHER,
)

BudgetStatus = Literal["within_budget", "over_budget", "unknown"]


class BudgetSummary(BaseModel):
    """预算汇总（PRD §23）。

    `breakdown_price_type` 是"真实 / 估算必须分开"这条硬约束的机器可读表达：
    同一个 breakdown 里，餐饮是 estimated、机票是 realtime，前端和审计都不用猜。
    """

    budget_total: float | None = None
    known_real_cost: float = 0.0
    estimated_cost: float = 0.0
    projected_total: float = 0.0
    remaining: float | None = None
    status: BudgetStatus = "unknown"
    breakdown: dict[str, float] = Field(default_factory=dict)
    breakdown_price_type: dict[str, str] = Field(default_factory=dict)
    optimization_suggestions: list[str] = Field(default_factory=list)
    price_notes: list[str] = Field(default_factory=list)

    @property
    def over_by(self) -> float:
        """超出多少（未超或未给预算时为 0）。"""
        if self.budget_total is None or self.remaining is None:
            return 0.0
        return max(0.0, -self.remaining)


# ==================================================
# PRD §22 / §32 可行性问题
# ==================================================

Severity = Literal["error", "warning"]


class FeasibilityIssue(BaseModel):
    """一条时间/路线可行性问题（PRD §22）。`resolved=True` 表示已被 revise 修好。"""

    day_index: int = 0
    item_id: str | None = None
    severity: Severity = "error"
    code: str = ""
    reason: str = ""
    original_start: str | None = None
    revised_start: str | None = None
    resolved: bool = False


# ==================================================
# plan.json 顶层（PRD §28）
# ==================================================


class TransportPlan(BaseModel):
    """大交通方案：去程 selected + alternatives，回程同理（PRD §24 / §28）。"""

    selected: TransportOption | None = None
    alternatives: list[TransportOption] = Field(default_factory=list)
    inbound_selected: TransportOption | None = None
    inbound_alternatives: list[TransportOption] = Field(default_factory=list)
    selection_reason: str = ""
    #: 门到门时间（含机场/车站额外接驳），比"飞行 2 小时"更能说明哪个方案真的更快。
    door_to_door_minutes: int | None = None
    provider_status: dict[str, str] = Field(default_factory=dict)


class HotelPlan(BaseModel):
    """住宿方案：selected + alternatives（PRD §28）。"""

    selected: HotelOption | None = None
    alternatives: list[HotelOption] = Field(default_factory=list)
    selection_reason: str = ""


class SourceRef(BaseModel):
    """plan.json 里的 sources 条目：前端"来源"面板直接消费（PRD §28 / §20）。"""

    source_id: str
    provider: str = "unknown"
    source_type: str = ""
    source_url: str | None = None
    title: str = ""
    query: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime | None = None
    status: str = "OK"


class TripPlan(BaseModel):
    """一次 run 的最终产物（PRD §28 plan.json）。"""

    run_id: str
    query: str = ""
    intent: TripIntent
    transport: TransportPlan | None = None
    hotel: HotelPlan | None = None
    days: list[ItineraryDay] = Field(default_factory=list)
    budget: BudgetSummary = Field(default_factory=BudgetSummary)
    warnings: list[FeasibilityIssue] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    #: 决策链随 plan 一起落到 audit（PRD §28 要求能回答"为什么保留/为什么拒绝"）。
    decisions: list[Decision] = Field(default_factory=list)

    @property
    def day_count(self) -> int:
        return len(self.days)

    def to_json_dict(self) -> dict[str, Any]:
        """给前端的纯 JSON 结构（PRD §28 plan.json）。

        走 json.dumps/loads 往返一次：datetime、date、Enum 全部在 mode="json" 里转成
        字符串，往返成功就等于"这个对象一定能被 JSON 序列化"，不会在写文件时才炸。
        """
        return json.loads(json.dumps(self.model_dump(mode="json"), ensure_ascii=False))


# ==================================================
# 基础名称归一化（planner.normalize_place_name 的兜底版本）
# ==================================================

#: 需要去掉的装饰性字符（全角括号、间隔号、书名号等）。保留中文与字母数字。
_PUNCT_RE = re.compile(r"[\s\u3000·・•\-—_~()（）\[\]【】{}<>《》\"'“”‘’,，.。、!！?？:：;；/\\|]+")


def basic_name_key(name: str) -> str:
    """最保守的名字键：全角转半角 + 小写 + 去空白与标点。

    只做"同一串字符的不同写法"归并，不做任何语义裁剪 —— planner 有更强的规则，
    这里用于 models 被单独导入（拿不到 planner）时的降级。
    """
    text = coerce_str(name)
    if not text:
        return ""
    text = "".join(
        chr(ord(char) - 0xFEE0) if "\uff01" <= char <= "\uff5e" else char
        for char in text
    )
    return _PUNCT_RE.sub("", text).lower()


# ==================================================
# Dynamic Preference Profile（docs/14 §4：LLM 决定"这个用户喜欢什么"）
# ==================================================
#
# 为什么单独一个模型而不是散成几个 dict：
#   - 五组权重 + travel_style + pace 是**一起生成、一起审计**的一个决策产物，
#     分开存会在"profile 从 LLM 到打分"这条链上丢字段；
#   - 默认值（balanced）就是"没人个性化时的固定基线"，从 LLM 拿不到时用它兜底，
#     保证 Python 永远有一组能用的权重 —— 而不是因为一次模型失败就换一套打分逻辑。

#: 五组动态权重（docs/14 §4）。每组归一化到 1.0；默认值是"均衡旅行者"的基线。
DEFAULT_HOTEL_AREA_WEIGHTS: dict[str, float] = {
    "food_density": 0.20,
    "commercial_area": 0.18,
    "nightlife": 0.12,
    "transit": 0.18,
    "poi_centrality": 0.20,
    "price": 0.08,
    "quietness": 0.04,
}
DEFAULT_HOTEL_WEIGHTS: dict[str, float] = {
    "location": 0.25,
    "food_access": 0.15,
    "rating": 0.20,
    "transit": 0.15,
    "price": 0.15,
    "comfort": 0.10,
}
DEFAULT_ATTRACTION_WEIGHTS: dict[str, float] = {
    "user_interest": 0.25,
    "route_fit": 0.25,
    "evidence": 0.20,
    "iconic": 0.10,
    "photo_value": 0.10,
    "popularity": 0.10,
}
DEFAULT_FOOD_WEIGHTS: dict[str, float] = {
    "local_recommendation": 0.25,
    "distance": 0.25,
    "specific_dishes": 0.15,
    "evidence": 0.15,
    "ad_risk": 0.10,
    "rating": 0.05,
    "price": 0.05,
}
DEFAULT_PACE: dict[str, Any] = {"target_poi_per_day": 2, "prefer_free_time": True}

#: 五组权重里各自的合法键。LLM 可能会塞进没见过的键，白名单之外一律丢弃 ——
#: 一个拼错的键不会污染打分，也不该静默长出第 8 个维度。
PROFILE_WEIGHT_KEYS: dict[str, tuple[str, ...]] = {
    "hotel_area": tuple(DEFAULT_HOTEL_AREA_WEIGHTS),
    "hotel": tuple(DEFAULT_HOTEL_WEIGHTS),
    "attraction": tuple(DEFAULT_ATTRACTION_WEIGHTS),
    "food": tuple(DEFAULT_FOOD_WEIGHTS),
}

PROFILE_GROUPS = tuple(PROFILE_WEIGHT_KEYS)


def _coerce_profile_weights(raw: Any, group: str) -> tuple[dict[str, float], bool]:
    """把 LLM 给的一组权重收口：只认白名单键、钳到 [0,1]、缺键用默认值、最后重归一化。

    返回 (权重, 是否真的带有 LLM 信号)。"有没有信号"决定这组权重该不该影响打分：
    返回空/坏数据时不该被当成"这个用户什么都不在乎"，而是要退回默认基线。

    缺键**用默认值而不是 0**：prompt 明确要求"全部键都要出现，缺键会被补成默认值"，
    模型偶尔漏一个键时，那个维度应当保持中性，而不是被悄悄压到最低（那会误伤
    Trust 这类主项 —— 实测漏一个 `evidence` 键就会把 Trust 权重压到下限）。
    """
    keys = PROFILE_WEIGHT_KEYS[group]
    base: dict[str, float] = {key: _default_weight(group, key) for key in keys}
    meaningful = False
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in base:
                continue
            number = coerce_float(value)
            if number is None:
                continue
            base[key] = min(1.0, max(0.0, number))
            meaningful = True
    if not meaningful:
        return {key: _default_weight(group, key) for key in keys}, False
    total = sum(base.values()) or 1.0
    return {key: round(base[key] / total, 4) for key in keys}, True


def _default_weight(group: str, key: str) -> float:
    return {
        "hotel_area": DEFAULT_HOTEL_AREA_WEIGHTS,
        "hotel": DEFAULT_HOTEL_WEIGHTS,
        "attraction": DEFAULT_ATTRACTION_WEIGHTS,
        "food": DEFAULT_FOOD_WEIGHTS,
    }[group].get(key, 0.0)


class PreferenceProfile(BaseModel):
    """一次旅行专属的动态偏好（docs/14 §4）。

    ``source`` 只有两种：
      * ``llm``     —— LLM 真的生成了一份带信号的 profile，打分据此个性化；
      * ``fallback``—— LLM 不可用/返回空，使用均衡基线（与"没有 profile"行为一致）。

    Profile 只决定**软取舍**（哪一组更重要、一天排几个点）。真实性、时间可行性、
    REJECT/MUST、预算这些硬规则照旧由 Python 强制，profile 碰不到它们。
    """

    travel_style: str = "balanced_explorer"
    hotel_area: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_HOTEL_AREA_WEIGHTS))
    hotel: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_HOTEL_WEIGHTS))
    attraction: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_ATTRACTION_WEIGHTS))
    food: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_FOOD_WEIGHTS))
    pace: dict[str, Any] = Field(default_factory=lambda: dict(DEFAULT_PACE))
    reason: str = ""
    #: llm | fallback
    source: str = "fallback"

    @property
    def personalized(self) -> bool:
        """只有 LLM 真正生成了差异化 profile 才算个性化；均衡基线不算。"""
        return self.source == "llm"

    @classmethod
    def from_llm(cls, payload: Any) -> "PreferenceProfile":
        """从 LLM 输出构造 profile，**保证不抛异常**（与 TripIntent.from_llm 同一原则）。"""

        data = parse_envelope(payload) if isinstance(payload, str) else payload
        if not isinstance(data, dict):
            data = {}

        hotel_area, ha_signal = _coerce_profile_weights(data.get("hotel_area"), "hotel_area")
        hotel, h_signal = _coerce_profile_weights(data.get("hotel"), "hotel")
        attraction, a_signal = _coerce_profile_weights(data.get("attraction"), "attraction")
        food, f_signal = _coerce_profile_weights(data.get("food"), "food")

        style = coerce_str(data.get("travel_style")) or "balanced_explorer"
        raw_pace = data.get("pace") if isinstance(data.get("pace"), dict) else {}
        target = coerce_int(raw_pace.get("target_poi_per_day"), 2) or 2
        target = max(1, min(target, 10))
        pace = {
            "target_poi_per_day": target,
            "prefer_free_time": bool(raw_pace.get("prefer_free_time", True)),
        }
        personalized = bool(style != "balanced_explorer" or ha_signal or h_signal or a_signal or f_signal)
        return cls(
            travel_style=style,
            hotel_area=hotel_area,
            hotel=hotel,
            attraction=attraction,
            food=food,
            pace=pace,
            reason=coerce_str(data.get("reason")),
            source="llm" if personalized else "fallback",
        )

    @classmethod
    def default_profile(cls) -> "PreferenceProfile":
        """均衡基线（LLM 不可用时的确定性兜底，与"无 profile"行为一致）。"""
        return cls(
            reason="LLM 未生成有效的偏好画像，采用均衡基线",
            source="fallback",
        )

    def weight(self, group: str, key: str, default: float = 0.0) -> float:
        """读一组权重里的某个键（不存在就回默认）。"""
        mapping = getattr(self, group, None)
        if not isinstance(mapping, dict):
            return default
        return float(mapping.get(key, default))
