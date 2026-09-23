"""TravelPlan 旅行领域算法（PRD §18 - §24、§32）。

这个模块是项目的真正核心，也是"Evidence First, LLM Second"落地的地方：
**没有任何 LLM 调用、没有任何网络调用、没有随机数**。输入输出都是 models 里的对象
（或已归一化的 Provider 数据），因此可以脱网单测。

为什么全部写成纯函数
------------------
价格比较、预算加法、时间约束、折返检测都有唯一正确答案，必须可复现：同一份输入
任何时候都给出同一份输出，测试才能锁住行为，审计才能解释"为什么这么排"。

三块必须由代码判断的东西
----------------------
* 预算：真实价格（机票/火车/酒店/门票/高德票价）与估算（餐饮、部分市内交通）严格分开；
* 时间：所有 buffer 都是本模块顶部的具名常量，改一处即可全量调整并可被审计引用；
* 路线：耗时以高德真实值为准；高德失败时只做**显式标记的粗估**并降置信度，绝不猜。
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from app.config import PlannerTuning, current_tuning
from app.profile import emphasis
from app.models import (
    BUDGET_CATEGORIES,
    BUDGET_CATEGORY_CITY_TRANSPORT,
    BUDGET_CATEGORY_HOTEL,
    BUDGET_CATEGORY_MEAL,
    BUDGET_CATEGORY_OTHER,
    BUDGET_CATEGORY_TICKET,
    BUDGET_CATEGORY_TRANSPORT,
    BudgetSummary,
    Decision,
    DecisionStatus,
    Evidence,
    FeasibilityIssue,
    FlightOption,
    HotelOption,
    ItineraryDay,
    ItineraryItem,
    Place,
    PreferenceProfile,
    RouteOption,
    TrainOption,
    TransportPlan,
    TravelLeg,
    TripIntent,
    TripPlan,
    basic_name_key,
    coerce_float,
    coerce_int,
    coerce_str,
    coerce_str_list,
)

# ==================================================
# 一、时间缓冲常量（PRD §22）
# ==================================================
# 这些数字不来自任何 Provider，是我们对真实世界的**保守建模**：宁可多留 10 分钟，
# 也不要排出"落地 10 分钟就进景区"的计划。集中命名是为了审计报告能直接引用、
# 测试能断言，以后要调只改这一处。


def tuning() -> PlannerTuning:
    """当前生效的 Planner 阈值。

    这几个值的存在意义是让 Evolution 能**真的**改一个候选并测出差异：默认值等于下面
    写的模块常量，所以不设 ``TP_*`` 环境变量时行为与历史完全一致；Benchmark / Evolution
    用 ``app.config.override_env`` 临时改它们，就能得到"同一份输入、两套阈值"的对照。
    """

    return current_tuning()


#: 飞机：起飞前到机场（值机 + 安检 + 走到登机口，大型机场保守取 2 小时）。
AIRPORT_CHECKIN_BUFFER_MINUTES = 120
#: 飞机：落地到走出航站楼（滑行 + 下机 + 等行李）。
AIRPORT_BAGGAGE_EXIT_MINUTES = 45
#: 机场 ↔ 市区兜底单程（拿不到高德真实路线时才用，且会被标记未经核实）。
AIRPORT_CITY_TRANSFER_MINUTES = 60
#: 火车：开车前到站（进站安检 + 检票）。
TRAIN_STATION_BUFFER_MINUTES = 45
#: 火车：到站到出站。
TRAIN_EXIT_BUFFER_MINUTES = 15
#: 火车站 ↔ 市区兜底单程。
TRAIN_STATION_TRANSFER_MINUTES = 30
#: 酒店：前台办理入住（"入住"这件事本身占用的时间）。
HOTEL_CHECKIN_MINUTES = 20
#: 酒店：只能寄存行李时的耗时。只有存在明确依据（booking_note 写明可寄存）才使用。
HOTEL_LUGGAGE_DROP_MINUTES = 10
#: 酒店：退房耗时。
HOTEL_CHECKOUT_MINUTES = 15
#: 酒店业内默认入住时间（早于此只能寄存行李）。
HOTEL_STANDARD_CHECKIN_MINUTES = 14 * 60
#: 普通换乘缓冲（找车、等车、进出站点）。
DEFAULT_TRANSFER_BUFFER_MINUTES = 10
#: 到达景点/餐厅后，从"到门口"到"真正开始游览/吃饭"的缓冲。
ATTRACTION_ARRIVAL_BUFFER_MINUTES = 10
MEAL_ARRIVAL_BUFFER_MINUTES = 5
#: 正餐时长。
MEAL_DURATION_MINUTES = 60
#: 午餐 / 晚餐的合理时间窗（PRD §21.2：午餐晚餐处在合理时间）。
LUNCH_WINDOW = (11 * 60, 14 * 60)
DINNER_WINDOW = (17 * 60, 21 * 60)
#: 常规游玩日的最早 / 最晚边界，用于"不要一天排太满"。
DAY_START_MINUTES = 8 * 60 + 30
DAY_END_MINUTES = 21 * 60 + 30
#: 一天绝对不允许越过 24:00：再晚也是第二天的事。深夜抵达日会放宽到这条线为止
#: （见 _trim_to_window 的 late-arrival 分支），但不会放过这条线。
MAX_DAY_MINUTES = 24 * 60
#: 深夜抵达（落地/到站已过 DAY_END_MINUTES）当天还剩多少余地：只够"出站 → 到酒店 → 入住"。
LATE_ARRIVAL_WINDOW_MINUTES = 90
#: 一天最多安排几个"要停留"的点（不含大交通与酒店手续）。
MAX_ITEMS_PER_DAY = 5
#: 一天有效游览时长上限（8 小时），超过就是"排太满"。
MAX_ACTIVE_MINUTES_PER_DAY = 8 * 60
#: 拿不到任何距离信息时，一段同城移动的兜底耗时（会被标记 route_unverified）。
UNKNOWN_ROUTE_MINUTES_FALLBACK = 15

#: 交通方式 → "走出站/取行李"缓冲。
_EXIT_BUFFER_BY_MODE = {
    "flight": AIRPORT_BAGGAGE_EXIT_MINUTES,
    "train": TRAIN_EXIT_BUFFER_MINUTES,
}
#: 交通方式 → "提前到站/机场"缓冲（作为该 item 的 arrival_buffer 参与不等式）。
_BOARDING_BUFFER_BY_MODE = {
    "flight": AIRPORT_CHECKIN_BUFFER_MINUTES,
    "train": TRAIN_STATION_BUFFER_MINUTES,
}
#: item 类型 → 到达缓冲。
_ARRIVAL_BUFFER_BY_TYPE = {
    "attraction": ATTRACTION_ARRIVAL_BUFFER_MINUTES,
    "activity": ATTRACTION_ARRIVAL_BUFFER_MINUTES,
    "food": MEAL_ARRIVAL_BUFFER_MINUTES,
    "hotel": MEAL_ARRIVAL_BUFFER_MINUTES,
    "free_time": 0,
}
#: item 类型 → 默认停留时长（Provider 没有 expected_duration 时用）。
DEFAULT_DURATION_BY_TYPE = {
    "attraction": 120,
    "activity": 90,
    "food": MEAL_DURATION_MINUTES,
    "hotel": 30,
    "transport": 60,
    "free_time": 60,
}

# ==================================================
# 二、时钟 / 距离工具
# ==================================================

_CLOCK_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
#: 地球半径（米）。
EARTH_RADIUS_METERS = 6371000.0
#: 直距 → 实际路程的绕行系数：城市道路不是直线。
DETOUR_FACTOR = 1.35
#: 各交通方式平均速度（km/h），只在没有高德真实路线时用于粗估。
SPEED_KMH = {"walking": 4.5, "transit": 18.0, "driving": 25.0, "taxi": 25.0}


def parse_clock(value: Any) -> int | None:
    """把任意时间表示解析成"从 00:00 起的分钟数"。

    支持 "14:00"、"14:00:00"、"2026-10-01T14:00:00"、"14：00"（全角冒号），
    以及**数字类型**的 int / float（直接当作分钟数，848 → 848）。
    注意 "840" 这种**字符串**里的裸数字不算 —— 它既可能是 8:40 也可能是 840 分钟，
    歧义太大，返回 None 而不是猜一个。
    解析不出来返回 None —— 排程宁可跳过也不猜。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, date) and not isinstance(value, str):
        return None

    match = _CLOCK_RE.search(str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59:
        return None
    return hour * 60 + minute


def minutes_to_clock(minutes: int | None) -> str | None:
    """分钟数 → "HH:MM"；跨过午夜时标出"次日"，避免出现 25:30 这种不可读时间。"""
    if minutes is None or minutes < 0:
        return None
    if minutes >= 24 * 60:
        return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}(次日)"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def haversine_meters(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    """两点的球面距离（米）。坐标缺失返回 None（不猜位置）。"""
    if not a or not b:
        return None
    lat1, lng1 = a[0], a[1]
    lat2, lng2 = b[0], b[1]
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(h)))


def _bearing(a: Sequence[float], b: Sequence[float]) -> float:
    """a → b 的方位角（度，0=正北）。用于判断"是不是在折返"。"""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    d_lng = math.radians(b[1] - a[1])
    x = math.sin(d_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _angle_gap(a: float, b: float) -> float:
    """两个方位角的夹角（0~180 度）。"""
    diff = abs(a - b) % 360
    return 360 - diff if diff > 180 else diff


def estimate_route_minutes(
    origin: Sequence[float] | None,
    destination: Sequence[float] | None,
    mode: str = "transit",
) -> tuple[int | None, dict]:
    """没有高德路线时的**粗估**：直距 × 绕行系数 ÷ 速度。

    返回 (分钟, 明细)。明细里 method 写明这是 haversine 估算 —— 调用方必须把它标成
    `route_unverified`，Critic 也要因此降低置信度（PRD §32：不允许让模型猜路线）。
    """
    distance = haversine_meters(origin, destination)
    if distance is None:
        return None, {"method": "unavailable", "reason": "缺少坐标，无法估算"}
    speed = SPEED_KMH.get(mode, SPEED_KMH["transit"])
    meters = distance * DETOUR_FACTOR
    minutes = max(3, int(round(meters / (speed * 1000 / 60))))
    return minutes, {
        "method": "haversine*detour",
        "straight_line_meters": round(distance, 1),
        "detour_factor": DETOUR_FACTOR,
        "speed_kmh": speed,
    }


def lookup_route(
    routes: Mapping | None,
    origin_id: str | None,
    destination_id: str | None,
    origin_name: str | None = None,
    destination_name: str | None = None,
) -> RouteOption | None:
    """在 routes 里找一段真实路线。

    routes 的键形式不统一（workflow 里可能是 tuple、字符串、或按起点嵌套），这里一次兜完，
    免得每个调用点各写一套查找逻辑：
      * `{(o, d): RouteOption}` / `{"o->d": ...}` / `"o|d"` / `"o→d"`
      * `{o: {d: RouteOption}}` / `{o: RouteOption}`
    值可以是 RouteOption、dict 或高德工具返回的信封 JSON 字符串。
    """
    if not routes or not origin_id or not destination_id:
        return None

    def _coerce(value: Any) -> RouteOption | None:
        if isinstance(value, RouteOption):
            return value
        if isinstance(value, dict):
            try:
                return RouteOption.model_validate(value)
            except Exception:  # noqa: BLE001 —— 脏数据当作没有路线
                return None
        if isinstance(value, str):
            return RouteOption.from_envelope(value)
        return None

    keys: list[Any] = [
        (origin_id, destination_id),
        f"{origin_id}->{destination_id}",
        f"{origin_id}|{destination_id}",
        f"{origin_id}→{destination_id}",
        f"{origin_id}:{destination_id}",
    ]
    if origin_name and destination_name:
        keys.append((origin_name, destination_name))
        keys.append(f"{origin_name}->{destination_name}")
    for key in keys:
        if key in routes:
            route = _coerce(routes[key])
            if route is not None:
                return route

    nested = routes.get(origin_id)
    if isinstance(nested, dict):
        for key in (destination_id, *((destination_name,) if destination_name else ())):
            if key in nested:
                route = _coerce(nested[key])
                if route is not None:
                    return route
    if isinstance(routes.get(origin_id), (RouteOption, dict)):
        route = _coerce(routes[origin_id])
        if route is not None and route.destination_place_id in (None, destination_id):
            return route
    return None


# ==================================================
# 三、POI 归一化与去重（PRD §20）
# ==================================================

#: 通用冗余后缀：剥掉它们**不改变指代**（"宽窄巷子景区" 就是 "宽窄巷子"）。
GENERIC_SUFFIXES = ("风景区", "旅游景区", "游览区", "旅游区", "景区", "景点")
#: 语义性后缀：剥掉可能换成另一个地方（"人民公园" → "人民"），所以要求剩余部分
#: 仍有 ≥2 个字，且同名合并时还要过坐标一致性检查（见 dedupe_places）。
WEAK_SUFFIXES = (
    "步行街", "小吃街", "美食街", "商业街", "博物馆", "纪念馆", "美术馆", "科技馆",
    "公园", "植物园", "动物园", "广场", "古镇", "老街", "火锅店", "串串店",
    "小吃店", "甜品店", "餐厅", "饭店", "酒店", "客栈", "民宿", "店", "馆",
)
#: 类别词：说的是"哪一类地方"，不是"哪一个地方"。它们会出现在检索词和攻略文本里
#: （"成都的博物馆值得去"），但**不能**用来判定两个地点是同一个 —— 两个地点都带
#: 别名"景点"绝不代表它们是同一地点。
CATEGORY_TERMS = frozenset(
    (
        *GENERIC_SUFFIXES,
        *WEAK_SUFFIXES,
        "美食", "小吃", "火锅", "串串", "拍照", "打卡", "夜景", "购物", "必去",
        "推荐", "网红", "亲子", "咖啡", "酒吧", "演出", "表演", "美食街",
    )
)
#: 需要剥离的城市前缀（"成都宽窄巷子" → "宽窄巷子"）。按长度倒序匹配，避免"哈尔"吃掉"哈尔滨"。
CITY_PREFIXES = (
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州", "南京", "武汉",
    "西安", "长沙", "郑州", "济南", "青岛", "大连", "沈阳", "长春", "哈尔滨", "合肥",
    "福州", "厦门", "南昌", "昆明", "贵阳", "南宁", "海口", "三亚", "兰州", "西宁",
    "银川", "乌鲁木齐", "呼和浩特", "拉萨", "石家庄", "太原", "苏州", "无锡", "常州",
    "宁波", "温州", "绍兴", "嘉兴", "金华", "台州", "佛山", "东莞", "珠海", "中山",
    "惠州", "汕头", "湛江", "桂林", "洛阳", "开封", "徐州", "南通", "扬州", "镇江",
    "盐城", "唐山", "保定", "廊坊", "秦皇岛", "烟台", "潍坊", "淄博", "威海", "泰安",
    "济宁", "泉州", "莆田", "龙岩", "赣州", "九江", "襄阳", "宜昌", "岳阳", "常德",
    "株洲", "湘潭", "衡阳", "柳州", "北海", "遵义", "绵阳", "乐山", "泸州", "宜宾",
    "南充", "大理", "丽江", "西双版纳",
)


# 仅收录常见、明确的繁简对应；不把「里/裏」「台/臺」「后/後」等字义折叠。
_NAME_VARIANTS = str.maketrans("園館廣場區縣鄉鎮門風遊觀貓龍華寬峽慶陽陰東雲貴蘇莊灣橋廟樓", "园馆广场区县乡镇门风游观猫龙华宽峡庆阳阴东云贵苏庄湾桥庙楼")


def _name_text_key(name: str | None) -> str:
    return basic_name_key(name).translate(_NAME_VARIANTS)


def _strip_city_prefix(key: str, city: str | None = None) -> str:
    """先匹配完整行政前缀，避免「成都市人民公园」被截成「市人民公园」。

    护栏：只有 city 参数明确匹配、或剩余部分 ≥3 个字时才剥。"重庆火锅"这类
    **以城市名开头的店名**剥完只剩"火锅"，会跟别家撞车。
    """
    city_name = _name_text_key(city)
    candidates = {*CITY_PREFIXES, city_name} - {""}
    variants: dict[str, bool] = {}
    for prefix in candidates:
        for variant in (prefix, prefix + "市", prefix + "省"):
            variants[variant] = variants.get(variant, False) or prefix == city_name
    for variant in sorted(variants, key=len, reverse=True):
        if not key.startswith(variant):
            continue
        remainder = key[len(variant):]
        if len(remainder) >= 3 or (variants[variant] and len(remainder) >= 2):
            return remainder
        # 长前缀已经命中但余串太短，不能退回短前缀留下孤立的「市/省」。
        return key
    return key


def _strip_suffix(key: str) -> str:
    """按"先通用、后语义"的顺序剥离冗余后缀，每一步都保证剩余部分仍能独立指代。

    GENERIC_SUFFIXES（景区/旅游区）剥完留 2 个字即可："宽窄巷子景区" → "宽窄巷子"。
    WEAK_SUFFIXES（公园/博物馆/步行街）语义更重，剥完必须留 ≥3 个**非行政**字，否则
    "人民公园" 会变成 "人民"、"四川省博物馆" 会变成 "四川省" —— 前者会和"人民广场"撞键，
    后者会和"四川省图书馆"撞键。错并比不合并危险得多，所以宁可保守。
    """
    for suffix in sorted(GENERIC_SUFFIXES, key=len, reverse=True):
        if key.endswith(suffix) and len(key) - len(suffix) >= 2:
            key = key[: -len(suffix)]
            break
    for suffix in sorted(WEAK_SUFFIXES, key=len, reverse=True):
        # 单字后缀（"店"/"馆"）一律不用：它会把"明婷饭店"截成"明婷饭"、
        # "四川省博物馆"截成"四川省博物"，纯属制造噪声。
        if len(suffix) < 2 or not key.endswith(suffix):
            continue
        remainder = key[: -len(suffix)]
        if len(remainder) >= 3 and not remainder.endswith(("省", "市", "区", "县")):
            key = remainder
            break
    return key


def normalize_place_name(name: str, city: str | None = None) -> str:
    """POI 名字归一化（PRD §20）。

    做四件事：全半角统一 + 去空白标点 → 剥城市前缀 → 剥通用后缀 → 剥语义后缀。
    结果是**用于比较的键**，不是给人看的名字（展示永远用 Place.name）。

    为什么不能只靠字符串完全相等：同一地点在不同来源里会写成"宽窄巷子"、
    "宽窄巷子景区"、"成都宽窄巷子"，完全相等永远合并不掉，证据就永远分散在三条上，
    Trust 的"多来源"分量也就永远拿不到分。
    """
    key = _name_text_key(name)
    if not key:
        return ""
    return _strip_suffix(_strip_city_prefix(key, city))


def name_similarity(left: str, right: str, city: str | None = None) -> float:
    """名字相似度 0~1：归一化相等 → 1.0；包含关系按长度比；否则字符集 + 序列相似度加权。

    单看任一指标都会出错（"宽窄巷子" vs "窄巷子"字符集重叠很高，但不是同一件事），
    所以两个指标取平均，再由调用方叠加坐标条件。
    """
    a = normalize_place_name(left, city)
    b = normalize_place_name(right, city)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return round(min(len(a), len(b)) / max(len(a), len(b)), 4)
    jaccard = len(set(a) & set(b)) / len(set(a) | set(b))
    ratio = SequenceMatcher(None, a, b).ratio()
    return round(0.5 * jaccard + 0.5 * ratio, 4)


#: 坐标合并阈值（PRD §20：坐标距离优先于名字）。
GEO_DUPLICATE_METERS = 150
#: 归一化同名但坐标相距超过此值 → 判定为"同名不同地"，不合并。
GEO_NAME_CONFLICT_METERS = 1500
#: 名字相似度下限（配合坐标条件使用）。
NAME_SIMILARITY_MIN = 0.6


def dedupe_places(places: Sequence[Place]) -> tuple[list[Place], list[Decision]]:
    """按 PRD §20 的优先级合并同一地点，返回 (合并后的地点, 决策列表)。

    优先级：高德 POI ID 相同 > 坐标接近且名字相似 > 归一化名字相同 > 地址相同 > alias 命中。

    合并时保留 aliases、被吸收的 place_id（merged_from）与坐标等字段 —— 证据挂在
    place_id 上，丢掉 place_id 等于丢掉证据链。同名但坐标明显冲突（>1.5km）时**不合并**：
    宁可留两条候选，也不要错并成一个。
    """
    items = [place.model_copy(deep=True) for place in places]
    count = len(items)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    groups = {index: [index] for index in range(count)}

    def union(left: int, right: int) -> bool:
        root_left, root_right = find(left), find(right)
        # A-B 与 B-C 有信号，不代表 A-C 没有硬冲突（B 可能缺坐标/城市/分店）。
        if any(
            _duplicate_conflict(items[a], items[b])
            for a in groups[root_left] for b in groups[root_right]
        ):
            return False
        parent[root_right] = root_left
        groups[root_left].extend(groups.pop(root_right))
        return True

    signals: dict[tuple[int, int], tuple[str, dict]] = {}
    for i in range(count):
        for j in range(i + 1, count):
            if find(i) == find(j):
                continue
            signal, detail = _duplicate_signal(items[i], items[j])
            if signal and union(i, j):
                signals[(i, j)] = (signal, detail)

    clusters: dict[int, list[int]] = {}
    for index in range(count):
        clusters.setdefault(find(index), []).append(index)

    merged: list[Place] = []
    decisions: list[Decision] = []
    for members in clusters.values():
        # _pick_representative 返回的是 members 内部的下标，必须映回全局下标再取 items；
        # 直接拿它当 items 的下标会把别的簇的 Place 当成代表点（曾把无关地点合并进来）。
        representative_index = members[_pick_representative([items[index] for index in members])]
        representative = items[representative_index]
        absorbed = [index for index in members if index != representative_index]

        aliases: list[str] = []
        merged_from = list(representative.merged_from)
        for index in absorbed:
            other = items[index]
            aliases.extend(name for name in (other.name, *other.aliases) if name and name != representative.name)
            merged_from.extend(other.merged_from)
            if other.place_id != representative.place_id:
                merged_from.append(other.place_id)
            if representative.lat is None and other.lat is not None:
                representative.lat, representative.lng = other.lat, other.lng
            representative.address = representative.address or other.address
            representative.district = representative.district or other.district
            representative.city = representative.city or other.city
            representative.opening_hours = representative.opening_hours or other.opening_hours
            representative.expected_duration_minutes = (
                representative.expected_duration_minutes or other.expected_duration_minutes
            )
            representative.amap_verified = representative.amap_verified or other.amap_verified
            representative.type = representative.type or other.type

        representative.aliases = sorted({*representative.aliases, *aliases} - {representative.name})
        representative.normalized_name = (
            normalize_place_name(representative.name, representative.city)
            or basic_name_key(representative.name)
        )
        representative.merged_from = sorted(set(merged_from) - {representative.place_id, ""})
        merged.append(representative)

        if not absorbed:
            continue
        decisions.append(
            Decision(
                entity_id=representative.place_id,
                status=DecisionStatus.KEEP,
                agent_or_stage="dedupe_places",
                reason_codes=["representative", f"merged:{len(absorbed)}"],
                reason_text=f"作为「{representative.name}」的代表点，合并了 {len(absorbed)} 条重复候选",
                scores={"merged_count": len(absorbed)},
            )
        )
        for index in absorbed:
            other = items[index]
            signal, detail = _recorded_signal(signals, index, representative_index)
            decisions.append(
                Decision(
                    entity_id=other.place_id,
                    status=DecisionStatus.REJECT,
                    agent_or_stage="dedupe_places",
                    reason_codes=[f"duplicate_of:{representative.place_id}", signal or "duplicate"],
                    reason_text=(
                        f"与「{representative.name}」判定为同一地点（{signal or 'duplicate'}），"
                        "证据并入代表点"
                    ),
                    scores=detail,
                )
            )
    return merged, decisions


def _duplicate_conflict(left: Place, right: Place) -> dict:
    # 延迟导入：places 复用本模块的归一化，模块级导入会成环。
    from app.places import identity_conflict

    if left.place_id and left.place_id == right.place_id:
        return {}
    return identity_conflict(left, right)


def _duplicate_signal(left: Place, right: Place) -> tuple[str | None, dict]:
    """判定两个 Place 是否同一地点；所有弱信号共享同一套硬冲突护栏。"""
    if left.place_id and left.place_id == right.place_id:
        return "same_poi_id", {"poi_id": left.place_id}
    conflict = _duplicate_conflict(left, right)
    if conflict:
        return None, conflict

    distance = haversine_meters(left.coords, right.coords)
    similarity = name_similarity(left.name, right.name, city=left.city or right.city)

    if distance is not None and distance <= GEO_DUPLICATE_METERS and similarity >= NAME_SIMILARITY_MIN:
        return "geo_name", {"distance_meters": round(distance, 1), "name_similarity": similarity}

    same_key = bool(left.alias_key) and left.alias_key == right.alias_key
    if same_key:
        return "normalized_name", {
            "normalized_name": left.alias_key,
            "distance_meters": None if distance is None else round(distance, 1),
        }

    left_address = _address_key(left.address)
    right_address = _address_key(right.address)
    if left_address and left_address == right_address and similarity >= 0.4:
        return "same_address", {"address": left.address, "name_similarity": similarity}

    left_aliases = _distinctive_alias_keys(left)
    right_aliases = _distinctive_alias_keys(right)
    if (right.alias_key and right.alias_key in left_aliases) or (
        left.alias_key and left.alias_key in right_aliases
    ):
        return "alias_hit", {"name_similarity": similarity}
    shared = left_aliases & right_aliases
    if shared:
        return "alias_hit", {"shared_aliases": sorted(shared)}
    return None, {}


def _distinctive_alias_keys(place: Place) -> set[str]:
    """`place.aliases` 里**可以拿来判重**的别名键。

    别名不一定是地名：类别词（"景点"、"美食"、"步行街"）表示"哪一类地方"，两个地点
    共享它绝不代表它们是同一地点。实测某次真实 run：8 个检索词被登记成每个 POI 的别名，
    去重因此把 64 个互不相关的地点并成 8 个，行程里只剩 2 个地点可用，5 天有 3 天没有
    任何安排。判重宁可漏并，也不能错并 —— 错并是静默丢地点，比留两条候选危险得多。
    """
    keys: set[str] = set()
    for alias in place.aliases:
        raw = basic_name_key(alias)
        if not raw or raw in CATEGORY_TERMS:
            continue
        key = normalize_place_name(alias, city=place.city) or raw
        if key in CATEGORY_TERMS:
            continue
        keys.add(key)
    return keys


def _recorded_signal(
    signals: dict[tuple[int, int], tuple[str, dict]], index: int, representative_index: int
) -> tuple[str, dict]:
    """取被吸收点当时的合并信号。

    union-find 会形成链（0-1、1-2 合并成 {0,1,2}），被吸收点与代表点之间不一定直接比较过，
    所以先找两者的直接记录，再退回到"任何一条包含该点的记录"，最后才是未知。
    """
    direct = (min(index, representative_index), max(index, representative_index))
    if direct in signals:
        return signals[direct]
    for (left, right), value in signals.items():
        if index in (left, right):
            return value
    return "", {}


def _address_key(address: str | None) -> str:
    """地址键：去标点 + 小写后，剥掉**末尾一个**行政区划单位。

    已实现的范围就是"末尾一个 省/市/区/县/镇/街道/办事处"：
    "成都市武侯区人民南路" → "成都市武侯区人民南路"（末尾不是区划单位，不动），
    "武侯区" → "武侯"。跨级前缀（"成都市武侯区X路1号" vs "武侯区X路1号"）**不**在此处理，
    那类归并靠 name_similarity，不要指望这个函数。
    """
    key = basic_name_key(address)
    if not key:
        return ""
    return re.sub(r"(省|市|区|县|镇|街道|办事处)$", "", key)


def _pick_representative(candidates: Sequence[Place]) -> int:
    """选代表点：高德验证过 > 字段更完整 > 名字更长（更具体）。"""
    def rank(place: Place) -> tuple:
        filled = sum(
            1
            for value in (
                place.lat, place.lng, place.address, place.district,
                place.opening_hours, place.expected_duration_minutes, place.type,
            )
            if value not in (None, "")
        )
        return (1 if place.amap_verified else 0, filled, len(place.name))

    return max(range(len(candidates)), key=lambda index: rank(candidates[index]))


# ==================================================
# 四、Trust Score（PRD §18）
# ==================================================
# 完整权重合计 100，每一项都由**可观测信号**折算，并在明细里写明依据，
# 而不是用一个 0~5 的主观评分乘系数。

TRUST_WEIGHT_SOURCES = 25.0       # 多来源独立证据
TRUST_WEIGHT_SCALE = 20.0         # 评价 / 出现规模
TRUST_WEIGHT_DETAIL = 20.0        # 具体体验细节
TRUST_WEIGHT_PERSISTENCE = 15.0   # 跨时间持续出现
TRUST_WEIGHT_LOCAL = 10.0         # 本地人 / 重复访问线索
TRUST_WEIGHT_OFFICIAL = 10.0      # POI / 官方信息一致

#: 互动量目标值：达到该量级（各字段取最大值再求和）算"规模充分"。
TRUST_ENGAGEMENT_TARGET = 20000
#: 期望的独立平台数与证据条数，用于把"多来源"折算成 0~1。
TRUST_PLATFORM_TARGET = 3
TRUST_EVIDENCE_TARGET = 5
#: 时间跨度满一年视为"跨时间持续出现"。
TRUST_PERSISTENCE_DAYS = 365
#: 本地人 / 重复访问线索词。
LOCAL_HINTS = (
    "本地人", "本地朋友", "第二次来", "第三次来", "经常来", "每次都来", "常来",
    "住附近", "就在附近", "从小吃", "老店", "开了很多年", "回头客", "老字号",
)
#: 具体体验细节的文本线索。只保留"必须真的经历过才写得出"的动词/量词，
#: 不把孤零零的"元""分钟"当依据 —— 营销文里同样会写"人均50元"。
STRONG_DETAIL_HINTS = ("人均", "点了", "吃了", "排队", "分量", "多少钱", "几点")
#: 数字 + 单位（价格/时长/距离），单独出现也算一条可核对的细节。
NUMBER_UNIT_RE = re.compile(r"\d+\s*(?:元|块|分钟|小时|公里|千米|km|KM)")
#: 细节判定的最小长度：太短的喊话式文案不认。
DETAIL_MIN_CHARS = 20
#: 互动量字段名（各平台叫法不同）。
ENGAGEMENT_KEYS = (
    "likes", "like_count", "liked_count", "digg_count", "comments", "comment_count",
    "views", "view_count", "play_count", "collect", "collected_count", "share", "share_count",
    "favorites", "favorite_count", "interaction",
)


def _engagement(raw_metrics: Mapping[str, Any]) -> float:
    """单条内容的互动量：取各字段最大值，避免 likes+views 相加把规模放大十倍的假象。"""
    values = [coerce_float(raw_metrics.get(key)) or 0.0 for key in ENGAGEMENT_KEYS]
    return max(values) if values else 0.0


def _dish_ratio(evidences: Sequence[Evidence]) -> float:
    """明确提到具体菜品的证据占比。"""
    if not evidences:
        return 0.0
    return sum(1 for evidence in evidences if evidence.specific_dishes) / len(evidences)


def _detail_ratio(evidences: Sequence[Evidence]) -> float:
    """有具体体验细节的证据占比（抽出了具体菜品，或文本里有可核对的经历细节）。"""
    if not evidences:
        return 0.0
    detailed = 0
    for evidence in evidences:
        if evidence.specific_dishes:
            detailed += 1
            continue
        text = evidence.text or ""
        if len(text) < DETAIL_MIN_CHARS:
            continue
        if any(hint in text for hint in STRONG_DETAIL_HINTS) or NUMBER_UNIT_RE.search(text):
            detailed += 1
    return detailed / len(evidences)


def trust_score(
    place: Place,
    evidences: Sequence[Evidence],
    amap_verified: bool = False,
) -> tuple[float, dict]:
    """Trust Score（PRD §18）：0~100，附六项分项明细（明细进 audit）。

    六项分别对应 PRD 给的权重，每一项都用真实可观测信号折算：
      1. 多来源独立证据 25：独立平台数（6 成）+ 证据条数（4 成）
      2. 评价/出现规模 20：互动量对数刻度；完全没有互动数据时退化为出现条数（只给 4 成）
      3. 具体体验细节 20：有具体菜品的证据占比（6 成）+ 有价格/排队/时长细节的占比（4 成）
      4. 跨时间持续出现 15：最早与最晚发布时间跨度（按 365 天折算），或跨 3 个不同月份
      5. 本地人/重复访问线索 10："本地人/第二次来/经常吃/住附近"等线索命中（3 种满分）
      6. POI/官方信息一致 10：高德 POI 验证 +6、高德详情完整（地址+营业时间）+2、
         存在官方/网页来源 +2

    完全没有证据时只剩高德那几分 —— 这是刻意的：Evidence First 的含义就是
    "没有证据支撑的地点拿不到高 Trust"，而不是靠一个中性默认值蒙混过去。
    """
    evidences = list(evidences or [])
    details: dict[str, Any] = {}

    platforms = {coerce_str(evidence.provider) or coerce_str(evidence.source_type) for evidence in evidences}
    platforms.discard("")
    sources_ratio = 0.6 * min(1.0, len(platforms) / TRUST_PLATFORM_TARGET) + 0.4 * min(
        1.0, len(evidences) / TRUST_EVIDENCE_TARGET
    )
    sources_score = TRUST_WEIGHT_SOURCES * sources_ratio
    details["multi_source"] = {
        "score": round(sources_score, 2),
        "max": TRUST_WEIGHT_SOURCES,
        "platforms": sorted(platforms),
        "evidence_count": len(evidences),
        "basis": (
            f"独立平台 {len(platforms)} 个、证据 {len(evidences)} 条"
            f"（目标 {TRUST_PLATFORM_TARGET} 平台 / {TRUST_EVIDENCE_TARGET} 条）"
        ),
    }

    engagement = sum(_engagement(evidence.raw_metrics) for evidence in evidences)
    if engagement > 0:
        scale_ratio = min(1.0, math.log10(1 + engagement) / math.log10(1 + TRUST_ENGAGEMENT_TARGET))
        scale_basis = f"互动量合计 {int(engagement)}（目标 {TRUST_ENGAGEMENT_TARGET}）"
    else:
        scale_ratio = 0.4 * min(1.0, len(evidences) / TRUST_EVIDENCE_TARGET)
        scale_basis = f"没有互动量数据，退化为按出现次数 {len(evidences)} 条折算（只给 4 成）"
    details["scale"] = {
        "score": round(TRUST_WEIGHT_SCALE * scale_ratio, 2),
        "max": TRUST_WEIGHT_SCALE,
        "engagement": int(engagement),
        "basis": scale_basis,
    }

    dish = _dish_ratio(evidences)
    detail = _detail_ratio(evidences)
    details["specific_detail"] = {
        "score": round(TRUST_WEIGHT_DETAIL * (0.6 * dish + 0.4 * detail), 2),
        "max": TRUST_WEIGHT_DETAIL,
        "dish_ratio": round(dish, 3),
        "detail_ratio": round(detail, 3),
        "dishes": sorted({name for evidence in evidences for name in evidence.specific_dishes})[:10],
        "basis": f"{dish * 100:.0f}% 证据有具体菜品、{detail * 100:.0f}% 有价格/排队/时长等细节",
    }

    timestamps = sorted(
        stamp for stamp in (evidence.published_at for evidence in evidences) if stamp is not None
    )
    span_days = (timestamps[-1] - timestamps[0]).days if len(timestamps) >= 2 else 0
    months = {(stamp.year, stamp.month) for stamp in timestamps}
    persistence_ratio = max(min(1.0, span_days / TRUST_PERSISTENCE_DAYS), min(1.0, len(months) / 3))
    details["persistence"] = {
        "score": round(TRUST_WEIGHT_PERSISTENCE * persistence_ratio, 2),
        "max": TRUST_WEIGHT_PERSISTENCE,
        "span_days": span_days,
        "distinct_months": len(months),
        "basis": f"发布时间跨度 {span_days} 天、跨 {len(months)} 个月",
    }

    local_hits = sorted(
        {hint for evidence in evidences for hint in LOCAL_HINTS if hint in (evidence.text or "")}
    )
    details["local_repeat"] = {
        "score": round(TRUST_WEIGHT_LOCAL * min(1.0, len(local_hits) / 3), 2),
        "max": TRUST_WEIGHT_LOCAL,
        "hits": local_hits,
        "basis": f"命中本地人/重复访问线索 {len(local_hits)} 种（{min(len(local_hits), 3) / 3 * 100:.0f}% 权重）",
    }

    official_score = 0.0
    official_basis: list[str] = []
    if amap_verified or place.amap_verified:
        official_score += 6.0
        official_basis.append("高德 POI 已验证 +6")
    if place.address and place.opening_hours:
        official_score += 2.0
        official_basis.append("高德详情含地址与营业时间 +2")
    if any(
        coerce_str(evidence.source_type).lower() in ("official", "gov", "web", "poi")
        or coerce_str(evidence.provider).lower() in ("amap", "tavily")
        for evidence in evidences
    ):
        official_score += 2.0
        official_basis.append("存在官方/网页来源 +2")
    details["official_consistency"] = {
        "score": round(min(official_score, TRUST_WEIGHT_OFFICIAL), 2),
        "max": TRUST_WEIGHT_OFFICIAL,
        "basis": "；".join(official_basis) or "没有高德或官方来源",
    }

    total = sum(
        details[key]["score"]
        for key in (
            "multi_source", "scale", "specific_detail", "persistence", "local_repeat",
            "official_consistency",
        )
    )
    details["total"] = round(total, 2)
    details["evidence_count"] = len(evidences)
    return round(min(100.0, max(0.0, total)), 1), details


# ==================================================
# 五、Ad Risk（PRD §19）
# ==================================================

AD_RISK_WEIGHT_SIMILARITY = 25.0   # 大量雷同文案
AD_RISK_WEIGHT_COMMERCIAL = 20.0   # 明确团购 / 商单
AD_RISK_WEIGHT_HYPE = 20.0         # 强营销关键词
AD_RISK_WEIGHT_VAGUE = 15.0        # 没有具体体验
AD_RISK_WEIGHT_CLUSTER = 10.0      # 同期达人集中推荐
AD_RISK_WEIGHT_EMOTION = 10.0      # 只有情绪词 / 没有缺点

#: 商单 / 探店标记词。
COMMERCIAL_HINTS = (
    "探店", "合作", "商单", "广告", "致谢", "赞助", "团购", "品牌方", "官方邀请",
    "寄拍", "试吃官", "体验官", "免单", "到店体验", "抽奖",
)
#: 强营销词。
HYPE_WORDS = (
    "必吃", "必去", "天花板", "绝绝子", "yyds", "封神", "巨好吃", "好吃到哭", "无敌",
    "顶流", "第一名", "最强", "宝藏", "不踩雷", "吹爆", "全网最", "没有之一", "太绝了",
)
#: 高情绪词（单独出现不算问题，配合"没有缺点"才可疑）。
EMOTION_WORDS = ("超级", "绝了", "太赞", "惊艳", "感动", "太香", "爱了", "氛围感满满", "好幸福")
#: 缺点 / 转折词：有它们说明作者在讲真实体验。
DOWNSIDE_HINTS = (
    "但是", "不过", "缺点", "不足", "建议", "排队", "偏贵", "有点咸", "不够",
    "不推荐", "服务慢", "一般般", "有待", "缺点是",
)
#: 文案相似度区间：低于下限算正常，高于上限算模板化。
SIMILARITY_FLOOR = 0.45
SIMILARITY_CEIL = 0.85
#: 同期集中推荐：窗口内出现这么多条，就认为是集中投放。
AD_CLUSTER_WINDOW_DAYS = 30
#: 风险扣减（PRD §19 明确的"降低风险"信号）。
AD_RELIEF_DISH = 8.0
AD_RELIEF_BALANCED = 10.0
AD_RELIEF_REPEAT = 8.0
AD_RELIEF_CROSS_PLATFORM = 8.0
AD_RELIEF_CASUAL = 6.0
#: 重复访问线索（与 Trust 的第 5 项部分共用词表）。
REPEAT_HINTS = ("第二次来", "第三次来", "经常来", "每次都来", "住附近", "常来", "从小吃")


def _shingles(text: str, size: int = 3) -> set[str]:
    """中文按 3-gram 切分：比按空格分词稳，模板化文案的 n-gram 重合度会非常高。"""
    key = basic_name_key(text)
    if not key:
        return set()
    if len(key) <= size:
        return {key}
    return {key[index: index + size] for index in range(len(key) - size + 1)}


def _text_similarity(texts: Sequence[str]) -> float:
    """两两相似度平均值（0~1）。少于两条无法比较，返回 0。"""
    keys = [text for text in texts if text and basic_name_key(text)]
    if len(keys) < 2:
        return 0.0
    scores: list[float] = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            left, right = _shingles(keys[i]), _shingles(keys[j])
            if not left or not right:
                continue
            scores.append(len(left & right) / len(left | right))
    return sum(scores) / len(scores) if scores else 0.0


def ad_risk(place: Place, evidences: Sequence[Evidence]) -> tuple[float, dict]:
    """Ad Risk（PRD §19）：0~100，越高越像广告，附六项分项明细与扣减依据。

    这是**风险分**而不是判决：PRD §19 结尾明确"Ad Risk 高 ≠ 一定删除"，
    最终筛选要结合 Trust + Ad Risk + 用户偏好 + 路线适配（见 score_candidate 与 critique）。
    """
    evidences = list(evidences or [])
    texts = [evidence.text for evidence in evidences if evidence.text]
    details: dict[str, Any] = {}
    raw = 0.0

    similarity = _text_similarity(texts)
    similarity_ratio = 0.0
    if len(texts) >= 2:
        span = SIMILARITY_CEIL - SIMILARITY_FLOOR
        similarity_ratio = min(1.0, max(0.0, (similarity - SIMILARITY_FLOOR) / span))
    similarity_score = AD_RISK_WEIGHT_SIMILARITY * similarity_ratio
    raw += similarity_score
    details["repeated_copy"] = {
        "score": round(similarity_score, 2),
        "max": AD_RISK_WEIGHT_SIMILARITY,
        "avg_similarity": round(similarity, 3),
        "basis": (
            f"{len(texts)} 条文本两两 3-gram 平均相似度 {similarity * 100:.0f}%"
            f"（≥{SIMILARITY_CEIL * 100:.0f}% 记满）"
        ),
    }

    commercial_hits = [hint for evidence in evidences for hint in COMMERCIAL_HINTS if hint in (evidence.text or "")]
    commercial_score = AD_RISK_WEIGHT_COMMERCIAL * min(1.0, len(set(commercial_hits)) / 2)
    raw += commercial_score
    details["commercial_signal"] = {
        "score": round(commercial_score, 2),
        "max": AD_RISK_WEIGHT_COMMERCIAL,
        "hits": sorted(set(commercial_hits)),
        "basis": f"命中商单/探店类关键词 {sorted(set(commercial_hits)) or '无'}（2 种记满）",
    }

    hype_hits = sorted({word for evidence in evidences for word in HYPE_WORDS if word in (evidence.text or "")})
    hype_score = AD_RISK_WEIGHT_HYPE * min(1.0, len(hype_hits) / 4)
    raw += hype_score
    details["hype_words"] = {
        "score": round(hype_score, 2),
        "max": AD_RISK_WEIGHT_HYPE,
        "hits": hype_hits,
        "basis": f"营销词命中 {len(hype_hits)} 种（4 种记满）：{hype_hits[:8]}",
    }

    dish = _dish_ratio(evidences)
    detail = _detail_ratio(evidences)
    # 没有证据文本时，"含糊""只夸不贬"都**无从判断**：它们说的是"有人这么说，但说得
    # 不具体 / 只挑好话"，不是"没人说过"。把"没有证据"折算成 21 分风险会让任何只有
    # 高德 POI 的地点（真实 run 里是绝大多数）score 直接变负、永远进不了行程 ——
    # 而"缺证据"的代价已经体现在 Trust 里了，不需要再用一个虚构的风险分罚两次。
    evaluable = bool(texts)
    vague_ratio = max(0.0, 1.0 - (0.6 * dish + 0.4 * detail)) if evaluable else 0.0
    vague_score = AD_RISK_WEIGHT_VAGUE * vague_ratio
    raw += vague_score
    details["vague"] = {
        "score": round(vague_score, 2),
        "max": AD_RISK_WEIGHT_VAGUE,
        "basis": (
            f"只有 {dish * 100:.0f}% 证据提到具体菜品、{detail * 100:.0f}% 有价格/排队等细节"
            if evaluable
            else "没有任何证据文本，本项无从判断，记 0"
        ),
    }

    stamps = sorted(
        stamp for stamp in (evidence.published_at for evidence in evidences) if stamp is not None
    )
    best_window = 0
    for stamp in stamps:
        window = [other for other in stamps if 0 <= (other - stamp).days <= AD_CLUSTER_WINDOW_DAYS]
        best_window = max(best_window, len(window))
    cluster_score = AD_RISK_WEIGHT_CLUSTER * min(1.0, max(0, best_window - 2) / 3)
    raw += cluster_score
    details["same_period_cluster"] = {
        "score": round(cluster_score, 2),
        "max": AD_RISK_WEIGHT_CLUSTER,
        "max_in_window": best_window,
        "window_days": AD_CLUSTER_WINDOW_DAYS,
        "basis": f"{AD_CLUSTER_WINDOW_DAYS} 天内最多 {best_window} 条（≥5 条记满）",
    }

    emotion_hits = [
        word for evidence in evidences for word in EMOTION_WORDS if word in (evidence.text or "")
    ]
    downside_hits = [
        word for evidence in evidences for word in DOWNSIDE_HINTS if word in (evidence.text or "")
    ]
    emotion_ratio = (
        0.6 * (1.0 if emotion_hits else 0.0) + 0.4 * (0.0 if downside_hits else 1.0)
    ) if evaluable else 0.0
    emotion_score = AD_RISK_WEIGHT_EMOTION * emotion_ratio
    raw += emotion_score
    details["emotion_no_downside"] = {
        "score": round(emotion_score, 2),
        "max": AD_RISK_WEIGHT_EMOTION,
        "emotion_hits": sorted(set(emotion_hits)),
        "downside_hits": sorted(set(downside_hits)),
        "basis": (
            f"情绪词 {len(set(emotion_hits))} 种、缺点/转折描述 {len(set(downside_hits))} 处"
            "（只夸不贬会拿满这一项）"
            if evaluable
            else "没有任何证据文本，本项无从判断，记 0"
        ),
    }

    relief_score = 0.0
    relief_basis: list[str] = []
    platforms = {coerce_str(evidence.provider) for evidence in evidences}
    platforms.discard("")
    repeat_hits = [
        hint for evidence in evidences for hint in REPEAT_HINTS if hint in (evidence.text or "")
    ]
    casual = any(
        len(evidence.text or "") <= 160 and not any(word in (evidence.text or "") for word in HYPE_WORDS)
        for evidence in evidences
    )
    if dish > 0:
        relief_score += AD_RELIEF_DISH
        relief_basis.append(f"有具体菜品 -{AD_RELIEF_DISH:g}")
    if downside_hits:
        relief_score += AD_RELIEF_BALANCED
        relief_basis.append(f"同时描述了缺点 -{AD_RELIEF_BALANCED:g}")
    if repeat_hits:
        relief_score += AD_RELIEF_REPEAT
        relief_basis.append(f"重复访问线索 -{AD_RELIEF_REPEAT:g}")
    if len(platforms) >= 2 and len(evidences) >= 3:
        relief_score += AD_RELIEF_CROSS_PLATFORM
        relief_basis.append(f"多平台重复出现 -{AD_RELIEF_CROSS_PLATFORM:g}")
    if casual:
        relief_score += AD_RELIEF_CASUAL
        relief_basis.append(f"存在普通用户口吻的短评 -{AD_RELIEF_CASUAL:g}")

    details["relief"] = {
        "score": -round(relief_score, 2),
        "max": 0.0,
        "basis": "；".join(relief_basis) or "没有任何降风险信号",
    }
    total = min(100.0, max(0.0, raw - relief_score))
    details["total"] = round(total, 2)
    details["evidence_count"] = len(evidences)
    return round(total, 1), details


# ==================================================
# 六、地理聚类 / 顺路 / 折返（PRD §21、§10.3）
# ==================================================

#: 同一天主要区域的聚类半径（km）。城市里 1.5km 大约步行 20 分钟，是"可以连着逛"的尺度。
DEFAULT_CLUSTER_KM = 1.5
#: 路线无效率惩罚的分项权重（分数越低越顺路，见 route_efficiency_penalty）。
ROUTE_MISSING_PENALTY = 40.0
ROUTE_UNVERIFIED_PENALTY = 20.0
ROUTE_LONG_DISTANCE_METERS = 3000
ROUTE_LONG_DURATION_MINUTES = 35
#: 折返检测：同一个地点往返判定的距离容差。
BACKTRACK_SAME_PLACE_METERS = 200
#: 折返检测：连续两段都超过这个距离，且方向相近，视为来回横跨。
BACKTRACK_LEG_METERS = 3000
#: 一天总移动距离上限，超过就是"反复横跨城市"。
BACKTRACK_TOTAL_METERS = 15000


def cluster_places(
    places: Sequence[Place],
    max_cluster_km: float = DEFAULT_CLUSTER_KM,
) -> list[list[Place]]:
    """按真实经纬度做贪心聚类（PRD §21：每天一个主要区域）。

    规则可解释、结果稳定：按输入顺序，把点并到"离某个已有成员最近且在阈值内"的簇，
    否则自己开一个新簇。没有坐标的点各自成簇 —— 位置未知就不该被塞进任何区域，
    否则"顺路"就是编出来的。
    返回按簇大小降序（同大小按首元素名字）排列，保证同样的输入永远同样的输出。
    """
    threshold = max_cluster_km * 1000
    clusters: list[list[Place]] = []
    for place in places:
        coordinates = place.coords
        best_index, best_distance = None, None
        if coordinates is not None:
            for index, cluster in enumerate(clusters):
                for member in cluster:
                    distance = haversine_meters(coordinates, member.coords)
                    if distance is None:
                        continue
                    if best_distance is None or distance < best_distance:
                        best_index, best_distance = index, distance
        if best_index is not None and best_distance is not None and best_distance <= threshold:
            clusters[best_index].append(place)
        else:
            clusters.append([place])
    return sorted(clusters, key=lambda cluster: (-len(cluster), cluster[0].name))


def route_efficiency_penalty(route: RouteOption | None, verified: bool | None = None) -> float:
    """一段路线的"无效率"惩罚（分数越大越差），用于候选排序时压掉折返与长距离折返。

    设计取向：
      * 完全没有路线数据 → 40 分（排程只能兜底估算，风险最大）
      * 高德返回但 verified=False → 20 分（路线不可信）
      * 距离超过 3km 每多 1km +5 分，耗时超过 35 分钟每多 1 分钟 +1 分
    单位不是分钟而是"分"，因为它只参与排序，不进入时间可行性判断。
    """
    is_verified = (route.verified if route is not None else False) if verified is None else verified
    if route is None:
        return ROUTE_MISSING_PENALTY + (0.0 if is_verified else ROUTE_UNVERIFIED_PENALTY)

    penalty = 0.0 if is_verified else ROUTE_UNVERIFIED_PENALTY
    distance = route.distance_meters or 0
    if distance > ROUTE_LONG_DISTANCE_METERS:
        penalty += (distance - ROUTE_LONG_DISTANCE_METERS) / 1000 * 5.0
    minutes = route.duration_minutes or 0
    if minutes > ROUTE_LONG_DURATION_MINUTES:
        penalty += (minutes - ROUTE_LONG_DURATION_MINUTES) * 1.0
    return round(penalty, 2)


def detect_backtracking(items: Sequence[ItineraryItem]) -> list[str]:
    """检测同一天明显折返（PRD §21.2：避免反复横跨城市）。

    三类问题：
      1. A → B → A：一天内回到同一个地点（同一 place_id / 归一化同名 / 200m 内）
      2. 连续两段都 >3km 且方向接近反向：来回横跨
      3. 全天总移动距离超过 15km
    返回原因文本列表（可直接进 audit 与 FeasibilityIssue.reason）。
    """
    places = [item for item in items if item.coords is not None]
    reasons: list[str] = []

    for index in range(len(items) - 2):
        first, second, third = items[index], items[index + 1], items[index + 2]
        if _same_place(first, second) or _same_place(second, third):
            continue
        if _same_place(first, third):
            reasons.append(
                f"折返：{first.name} → {second.name} → {third.name}，同一天回到同一地点"
            )

    legs: list[tuple[float, float]] = []
    for index in range(1, len(places)):
        previous, current = places[index - 1], places[index]
        distance = haversine_meters(previous.coords, current.coords)
        if distance is None:
            continue
        legs.append((distance, _bearing(previous.coords, current.coords)))
    for index in range(len(legs) - 1):
        distance_a, bearing_a = legs[index]
        distance_b, bearing_b = legs[index + 1]
        if distance_a > BACKTRACK_LEG_METERS and distance_b > BACKTRACK_LEG_METERS:
            if _angle_gap(bearing_a, bearing_b) > 120:
                reasons.append(
                    f"折返：连续两段各 {distance_a / 1000:.1f}km / {distance_b / 1000:.1f}km 且方向相反，"
                    "属于来回横跨"
                )

    total = sum(distance for distance, _ in legs)
    if total > BACKTRACK_TOTAL_METERS:
        reasons.append(f"当天总移动 {total / 1000:.1f}km，超过建议上限 {BACKTRACK_TOTAL_METERS / 1000:.0f}km")
    return reasons


def _same_place(left: ItineraryItem, right: ItineraryItem) -> bool:
    """两个 item 是否同一个地点（id / 归一化名字 / 坐标容差）。"""
    if left.place_id and right.place_id and left.place_id == right.place_id:
        return True
    if left.name and right.name and normalize_place_name(left.name) == normalize_place_name(right.name):
        return True
    distance = haversine_meters(left.coords, right.coords)
    return distance is not None and distance <= BACKTRACK_SAME_PLACE_METERS


# ==================================================
# 七、预算引擎（PRD §23）
# ==================================================

#: 城市餐饮档次（元/人/天）。构成是"三餐 + 小吃饮料"，写清楚是为了让估算可解释：
#:   tier1（京沪广深）: 早 25 + 午 55 + 晚 60 + 小吃 20 = 160
#:   tier2（省会/热门旅游城市）: 早 18 + 午 40 + 晚 45 + 小吃 17 = 120
#:   tier3（其它）: 早 12 + 午 30 + 晚 35 + 小吃 13 = 90
MEAL_ESTIMATE_PER_DAY = {"tier1": 160.0, "tier2": 120.0, "tier3": 90.0}
#: 市内交通：没有高德票价时按每天这么多元/人估算（地铁公交 2-4 段 + 偶发打车）。
CITY_TRANSPORT_PER_DAY = {"tier1": 40.0, "tier2": 30.0, "tier3": 20.0}
#: 单段市内交通没有票价时的兜底（大城市地铁 3-5 元 + 换乘）。
CITY_TRANSPORT_LEG_FALLBACK = 6.0
#: 城市 → 档次。
CITY_TIERS = {
    "tier1": ("北京", "上海", "广州", "深圳"),
    "tier2": (
        "成都", "重庆", "杭州", "南京", "武汉", "西安", "长沙", "苏州", "天津", "青岛",
        "厦门", "昆明", "大连", "宁波", "无锡", "郑州", "济南", "福州", "合肥", "沈阳",
        "哈尔滨", "长春", "三亚", "丽江", "桂林", "西双版纳",
    ),
}
DEFAULT_CITY_TIER = "tier2"
#: 每间房住几人（折算房间数用）。
TRAVELERS_PER_ROOM = 2


def city_tier(city: str | None) -> str:
    """城市 → 餐饮/市内交通档次；认不出来按 tier2（全国中位）处理，并在预算备注里说明。"""
    key = basic_name_key(city)
    if not key:
        return DEFAULT_CITY_TIER
    for tier, cities in CITY_TIERS.items():
        if any(basic_name_key(name) in key or key in basic_name_key(name) for name in cities):
            return tier
    return "tier3"


def _lookup_price(prices: Mapping[str, float] | None, *keys: str | None) -> float | None:
    if not prices:
        return None
    for key in keys:
        if key and key in prices:
            value = coerce_float(prices[key])
            if value is not None:
                return value
    return None


def _transport_price(option: Any) -> float | None:
    """交通方案的单人价格：航班看 price，火车看 price_range.min。取不到就是 None。"""
    if option is None:
        return None
    return coerce_float(getattr(option, "price", None))


def build_budget(
    intent: TripIntent,
    transport: TransportPlan | FlightOption | TrainOption | None = None,
    hotel: HotelOption | None = None,
    days: Sequence[ItineraryDay] = (),
    ticket_costs: Mapping[str, float] | None = None,
    *,
    city: str | None = None,
    day_count: int | None = None,
    hotel_alternatives: Sequence[HotelOption] = (),
    other_cost: float | None = None,
) -> BudgetSummary:
    """预算汇总（PRD §23）：real 与 estimated 严格分开，超预算给可执行的优化建议。

    真实（realtime）：大交通、住宿、门票、以及高德返回了票价的那部分市内交通
    估算（estimated）：餐饮（按城市档次 × 人数 × 天数）、没有票价的路段按段兜底、其它

    三条硬规则：
      1. 拿不到真实价的项**计入 0 并写进 price_notes**，而不是猜一个数；
      2. 住宿只用真实单价做乘法（晚数 × 房间数），房间数口径写明；
      3. 优化建议只能来自**真实候选方案的价格差**，没有更便宜的候选就如实说明。
    """
    travelers = max(1, intent.travelers)
    tier = city_tier(city or (intent.destination[0] if intent.destination else None))
    nights = intent.nights
    if hotel is not None and hotel.nights:
        nights = hotel.nights
    if day_count is not None:
        nights = max(day_count - 1, 0) if day_count else nights
    rooms = max(1, math.ceil(travelers / TRAVELERS_PER_ROOM))

    plan = transport if isinstance(transport, TransportPlan) else None
    outbound = plan.selected if plan is not None else transport
    inbound = plan.inbound_selected if plan is not None else None

    breakdown: dict[str, float] = {category: 0.0 for category in BUDGET_CATEGORIES}
    price_type: dict[str, str] = {}
    notes: list[str] = []

    # --- 大交通（真实） ---
    outbound_price = _transport_price(outbound)
    inbound_price = _transport_price(inbound)
    transport_real = sum(price for price in (outbound_price, inbound_price) if price is not None) * travelers
    breakdown[BUDGET_CATEGORY_TRANSPORT] = round(transport_real, 2)
    price_type[BUDGET_CATEGORY_TRANSPORT] = "realtime" if transport_real else "unknown"
    if outbound is not None and outbound_price is None:
        notes.append(f"去程 {coerce_str(getattr(outbound, 'label', ''))} 未返回价格，预算未计入该段")
    if outbound is None:
        notes.append("没有选定大交通方案，交通费用未计入预算")

    # --- 住宿（真实） ---
    hotel_real = 0.0
    if hotel is not None:
        if hotel.total_price is not None:
            hotel_real = hotel.total_price
        elif hotel.nightly is not None and nights:
            hotel_real = round(hotel.nightly * nights * rooms, 2)
        if hotel_real == 0.0:
            notes.append(f"酒店「{hotel.name}」没有返回价格，住宿费用未计入预算")
        if hotel.price_note:
            notes.append(
                f"酒店「{hotel.name}」价格为 {hotel.price_note}，实际可能上浮，最终以预订页为准"
            )
        notes.append(f"住宿按 {nights} 晚 × {rooms} 间折算")
    else:
        notes.append("没有选定酒店，住宿费用未计入预算")
    breakdown[BUDGET_CATEGORY_HOTEL] = round(hotel_real, 2)
    price_type[BUDGET_CATEGORY_HOTEL] = "realtime" if hotel_real else "unknown"

    # --- 门票（真实） ---
    ticket_real = 0.0
    unknown_tickets = 0
    for day in days:
        for item in day.items:
            if item.type != "attraction":
                continue
            price = item.price if item.price_type == "realtime" else None
            if price is None:
                price = _lookup_price(ticket_costs, item.place_id, item.name)
            if price is None:
                unknown_tickets += 1
                continue
            ticket_real += price * travelers
    breakdown[BUDGET_CATEGORY_TICKET] = round(ticket_real, 2)
    price_type[BUDGET_CATEGORY_TICKET] = "realtime" if ticket_real else "unknown"
    if unknown_tickets:
        notes.append(f"{unknown_tickets} 个景点没有实时门票价，门票预算只统计了有价的部分")

    # --- 市内交通（高德票价算真实，其余按段/按天估算） ---
    city_real = 0.0
    missing_legs = 0
    days_without_route = 0
    for day in days:
        day_has_fare = False
        for item in day.items:
            leg = item.travel_from_previous
            if leg is None:
                continue
            if leg.estimated_cost is not None:
                city_real += leg.estimated_cost * travelers
                day_has_fare = True
            else:
                missing_legs += 1
        if not day.items:
            continue
        if not day_has_fare and len(day.items) > 1:
            days_without_route += 1
    city_estimated = (
        missing_legs * CITY_TRANSPORT_LEG_FALLBACK * travelers
        + days_without_route * CITY_TRANSPORT_PER_DAY[tier] * travelers
    )

    # --- 餐饮（估算，明确标注） ---
    meal_estimated = MEAL_ESTIMATE_PER_DAY[tier] * max(1, intent.days) * travelers

    # --- 其它 ---
    other_estimated = float(other_cost) if other_cost else 0.0

    breakdown[BUDGET_CATEGORY_CITY_TRANSPORT] = round(city_real + city_estimated, 2)
    price_type[BUDGET_CATEGORY_CITY_TRANSPORT] = (
        "realtime" if city_real and not city_estimated else "estimated" if city_estimated else "unknown"
    )
    breakdown[BUDGET_CATEGORY_MEAL] = round(meal_estimated, 2)
    price_type[BUDGET_CATEGORY_MEAL] = "estimated"
    breakdown[BUDGET_CATEGORY_OTHER] = round(other_estimated, 2)
    price_type[BUDGET_CATEGORY_OTHER] = "estimated" if other_estimated else "unknown"

    notes.append(
        f"餐饮按{tier}城市 ¥{MEAL_ESTIMATE_PER_DAY[tier]:g}/人/天 × {travelers} 人 × "
        f"{max(1, intent.days)} 天估算（估算值，不是实时价格）"
    )
    if city_estimated:
        notes.append(
            f"市内交通中有 {missing_legs} 段没有高德票价，按 ¥{CITY_TRANSPORT_LEG_FALLBACK:g}/段估算；"
            f"{days_without_route} 天完全没有路线数据，按 ¥{CITY_TRANSPORT_PER_DAY[tier]:g}/天折算"
        )

    known_real = round(transport_real + hotel_real + ticket_real + city_real, 2)
    estimated = round(meal_estimated + city_estimated + other_estimated, 2)
    projected = round(known_real + estimated, 2)
    budget_total = intent.budget_total
    remaining = None if budget_total is None else round(budget_total - projected, 2)
    if budget_total is None:
        status = "unknown"
    else:
        status = "within_budget" if (remaining or 0) >= 0 else "over_budget"

    summary = BudgetSummary(
        budget_total=budget_total,
        known_real_cost=known_real,
        estimated_cost=estimated,
        projected_total=projected,
        remaining=remaining,
        status=status,
        breakdown=breakdown,
        breakdown_price_type=price_type,
        price_notes=notes,
    )

    if status == "over_budget" and remaining is not None:
        summary.optimization_suggestions = _budget_suggestions(
            shortfall=abs(remaining),
            hotel=hotel,
            hotel_alternatives=hotel_alternatives,
            nights=nights or 0,
            rooms=rooms,
            travelers=travelers,
            outbound=outbound,
            outbound_alternatives=(plan.alternatives if plan is not None else []),
            inbound=inbound,
            inbound_alternatives=(plan.inbound_alternatives if plan is not None else []),
        )
    return summary


def _budget_suggestions(
    *,
    shortfall: float,
    hotel: HotelOption | None,
    hotel_alternatives: Sequence[HotelOption],
    nights: int,
    rooms: int,
    travelers: int,
    outbound: Any,
    outbound_alternatives: Sequence[Any],
    inbound: Any,
    inbound_alternatives: Sequence[Any],
) -> list[str]:
    """超预算优化建议：**只能用候选方案的真实价格算差价**，没有候选就如实说明。"""
    candidates: list[tuple[float, str]] = []

    selected_nightly = hotel.nightly if hotel is not None else None
    for alternative in hotel_alternatives or []:
        nightly = alternative.nightly
        if selected_nightly is None or nightly is None or nights <= 0:
            continue
        if nightly >= selected_nightly:
            continue
        saving = (selected_nightly - nightly) * nights * rooms
        candidates.append((
            saving,
            f"酒店换至「{alternative.name}」（¥{nightly:g}/晚 × {nights} 晚 × {rooms} 间）"
            f"可省约 ¥{saving:,.0f}",
        ))

    def _compare(selected: Any, alternatives: Sequence[Any], label: str) -> None:
        selected_price = _transport_price(selected)
        if selected_price is None:
            return
        for alternative in alternatives or []:
            price = _transport_price(alternative)
            if price is None or price >= selected_price:
                continue
            saving = (selected_price - price) * travelers
            candidates.append((
                saving,
                f"{label}改「{alternative.label}」（¥{price:g}/人）可省约 ¥{saving:,.0f}",
            ))

    _compare(outbound, outbound_alternatives, "去程")
    _compare(inbound, inbound_alternatives, "回程")

    candidates.sort(key=lambda item: (-item[0], item[1]))
    suggestions = [text for _, text in candidates[:3]]
    if not suggestions:
        compared = len(list(hotel_alternatives or [])) + len(list(outbound_alternatives or [])) + len(
            list(inbound_alternatives or [])
        )
        suggestions.append(
            f"已知候选中没有更便宜的方案（已比较 {compared} 个候选），"
            f"缺口 ¥{shortfall:,.0f} 需要减少天数、人数或降低住宿档次"
        )
    return suggestions


# ==================================================
# 八、营业时间解析（PRD §22：景点 / 餐厅）
# ==================================================

#: "最晚进入 / 停止入场"这类限制的捕获：高德把它写在营业时间字符串里。
_LAST_ENTRY_RE = re.compile(
    r"(?:最晚进入|最晚入场|最晚入园|最晚入馆|停止入场|停止入园|停止售票|停止入馆|最后入场|最后入园|last\s*entry)"
    r"[^\d]{0,8}(\d{1,2})\s*[:：]\s*(\d{2})"
)
#: 一个"开门-关门"区间。必须带连接符，才不会把「08:30-18:30 最晚进入17:30」里的
#: 最晚进入误当成关门时刻。
_HOURS_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—－至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)


@dataclass(frozen=True)
class OpeningHours:
    """解析后的营业时间（分钟制）。None 表示高德没给这一项 —— 不允许推断。"""

    open_minutes: int | None = None
    close_minutes: int | None = None
    last_entry_minutes: int | None = None
    raw: str = ""
    note: str = ""


def parse_opening_hours(text: str | None) -> OpeningHours | None:
    """解析高德的营业时间字符串。

    实测出现过的形态：`08:30-18:30`、`周一至周日 08:30-18:30 最晚进入17:30`、
    `09:00-12:00,13:00-17:00`、`08:00-00:30`、`全天开放`。解析策略：
      * 先统一全角冒号与各种连接符；
      * 逐个解析"开门-关门"区间，**关门早于开门就是跨零点**（`08:00-00:30`
        的 00:30 是次日，记作 1470 而不是 30）—— 不这样处理，`08:00-00:30` 会被
        读成"00:30 开门、08:00 关门"，于是下午 13:43 吃饭被报成"超过闭园时间"；
      * 取所有区间的最早开门与最晚关门；
      * 带"最晚进入/停止入场"字样的那一句单独取 last entry（这个才是硬约束：
        进不去，关门时间再晚也没用）；
      * 分时段与固定闭馆日**只给提示不建模** —— 宁可让审计看到"待确认"，
        也不要假装我们知道周几闭馆。
    """
    raw = coerce_str(text)
    if not raw:
        return None
    normalized = raw.replace("：", ":").replace("~", "-").replace("—", "-").replace("－", "-")
    if any(token in normalized for token in ("全天", "24小时", "24h", "24H")):
        return OpeningHours(0, 24 * 60, None, raw=raw, note="全天开放")

    times = [parse_clock(match.group(0)) for match in _CLOCK_RE.finditer(normalized)]
    times = [value for value in times if value is not None]
    last_entry = None
    match = _LAST_ENTRY_RE.search(normalized)
    if match:
        last_entry = int(match.group(1)) * 60 + int(match.group(2))
    if not times and last_entry is None:
        return OpeningHours(None, None, None, raw=raw, note="营业时间无法解析，按未知处理")

    opens: list[int] = []
    closes: list[int] = []
    for start_h, start_m, end_h, end_m in _HOURS_RANGE_RE.findall(normalized):
        start = int(start_h) * 60 + int(start_m)
        end = int(end_h) * 60 + int(end_m)
        if end <= start:
            # 跨零点：关门时刻属于次日。等于（08:00-08:00）按全天处理，也走这里。
            end += 24 * 60
        opens.append(start)
        closes.append(end)

    notes: list[str] = []
    if len(times) >= 4:
        notes.append("分时段开放，午休时段未单独建模")
    if "闭" in normalized and ("周" in normalized or "每" in normalized):
        notes.append("存在固定闭馆/闭园日，需人工确认")
    if any(close > MAX_DAY_MINUTES for close in closes):
        notes.append("营业时间跨零点，关门时刻记在次日")

    open_minutes = min(opens) if opens else (min(times) if times else None)
    close_minutes = max(closes) if closes else (max(times) if times else None)
    return OpeningHours(
        open_minutes=open_minutes,
        close_minutes=close_minutes,
        last_entry_minutes=last_entry,
        raw=raw,
        note="；".join(notes),
    )


# ==================================================
# 九、时间可行性引擎（PRD §22、§32）
# ==================================================

#: 修订时"只能靠删除解决"的问题码（返程赶不上：往后挪只会更赶不上）。
REMOVAL_CODES = ("RETURN_DEPARTURE_CONFLICT",)

#: 营业时间类问题：能向后平移就平移（见 revise_day 的第 1 组），平移不了就**从当天移除该项**。
#:
#: 为什么必须移除而不是"报告但保留"：这三种问题都是 error 级，意味着我们自己的检查判定
#: "这个点在它开门/关门的时刻之外"。把它留在 plan 里，用户读到的就是一份写着
#: "17:51 去一个 18:00 关门的祠堂"的行程 —— 报告它却不改它，等于默认产出错误行程。
#: 真实数据里高德给的营业时间五花八门（寺庙 18:00 关门很常见），所以这不是理论问题：
#: 历史 run 里每一次都留下了未解决的营业时间冲突。
OPENING_HOURS_CODES = ("CLOSING_TIME_OVERRUN", "LAST_ENTRY_MISSED", "OPENING_TIME_CONFLICT")


def boarding_buffer_minutes(option: Any) -> int:
    """起飞/发车前必须到机场/车站的缓冲（分钟）。"""
    mode = "flight" if isinstance(option, FlightOption) else "train"
    return _BOARDING_BUFFER_BY_MODE[mode]


def transfer_buffer_minutes(option: Any) -> int:
    """机场/车站 → 市区的兜底接驳时间（高德有真实路线时由调用方覆盖）。"""
    return AIRPORT_CITY_TRANSFER_MINUTES if isinstance(option, FlightOption) else TRAIN_STATION_TRANSFER_MINUTES


def _option_clock(value: Any) -> int | None:
    if isinstance(value, ItineraryItem):
        return parse_clock(value.end_time or value.start_time)
    stamp = getattr(value, "arrival_at", None) or getattr(value, "departure_at", None)
    return None if stamp is None else stamp.hour * 60 + stamp.minute


def arrival_ready_minutes(
    option: FlightOption | TrainOption | None,
    *,
    transfer_minutes: int | None = None,
    with_checkin: bool = True,
) -> int | None:
    """抵达链：落地/到站 → 走出站 → 到市区 → 可开始行程的绝对分钟数。

    这就是 PRD §4.3 的公式：`12:10 落地 + 45 分钟出机场 + 70 分钟到酒店 + 20 分钟入住`
    → 最早 14:25 才能出门。所有分量都是具名常量（可由调用方用高德真实耗时覆盖接驳段）。
    """
    if option is None or option.arrival_at is None:
        return None
    arrive = option.arrival_at.hour * 60 + option.arrival_at.minute
    exit_buffer = AIRPORT_BAGGAGE_EXIT_MINUTES if isinstance(option, FlightOption) else TRAIN_EXIT_BUFFER_MINUTES
    transfer = transfer_buffer_minutes(option) if transfer_minutes is None else transfer_minutes
    total = arrive + exit_buffer + transfer
    if with_checkin:
        total += HOTEL_CHECKIN_MINUTES
    return total


def arrival_day_start_minutes(
    option: FlightOption | TrainOption | None,
    *,
    transfer_minutes: int | None = None,
    with_checkin: bool = True,
) -> int | None:
    """抵达日**该**从几点开始排行程 —— 物理上能开始 ≠ 该开始。

    红眼航班 00:15 落地、02:20 到酒店，`arrival_ready_minutes` 说"最早 02:20 能出门"，
    这是事实；但拿它当天的起排时刻，排出来的就是「02:20 入住、03:15 夜景打卡、
    05:06 吃晚饭」这种东西 —— 那时旅客在睡觉，景点和餐馆也没开门。

    所以落在正常起床时刻之前的抵达一律从 `DAY_START_MINUTES` 开始排（人要休息），
    晚于它的（例如 12:10 落地 → 14:25）照旧按抵达链算。原始抵达时刻仍然如实写进
    D0 的在途说明与 transport 理由里，不因为这里取整就把事实抹掉。
    """
    ready = arrival_ready_minutes(
        option, transfer_minutes=transfer_minutes, with_checkin=with_checkin
    )
    if ready is None:
        return None
    return max(ready, DAY_START_MINUTES)


def arrival_day_index(
    option: FlightOption | TrainOption | None,
    start_date: date | None,
    day_count: int,
) -> int:
    """去程把"人真正到得了目的地"的那一天排在第几天（0 起）。

    这一条是为跨午夜/跨天的去程准备的：北京 10-01 21:30 起飞的航班 10-02 00:15 落地，
    旅客在 10-01 白天还在出发地；长春 10-01 22:57 上的火车 10-03 21:35 才到成都西。
    以前的代码只看"到达时刻是几点"，把这类航班/车次的抵达链套在**出发日**上，于是排出了
    "10-01 08:30 在成都逛景点、当天 21:30 才起飞"这种物理上不可能的行程。

    取不到抵达时间就退回 0 —— 没有证据时按原语义（抵达日=第一天）处理，不猜一个更晚的天。
    超出行程天数时夹到最后一天：那意味着整趟行程基本在路上，由上层另行告警（见
    `arrival_beyond_trip`），这里不擅自把日期改掉。
    """
    if option is None or start_date is None or option.arrival_at is None:
        return 0
    delta = (option.arrival_at.date() - start_date).days
    return max(0, min(delta, max(0, day_count - 1)))


def arrival_beyond_trip(
    option: FlightOption | TrainOption | None,
    start_date: date | None,
    day_count: int,
) -> bool:
    """去程抵达日是否已经超出行程最后一天（= 这趟行程根本还没开始玩就要回程）。"""
    if option is None or start_date is None or option.arrival_at is None:
        return False
    return (option.arrival_at.date() - start_date).days > max(0, day_count - 1)


def departure_day_offset(
    option: FlightOption | TrainOption | None,
    expected: date | None,
) -> int | None:
    """候选的发车日与行程基准日差几天；``0`` = 同一天，负数是发得更早。

    为什么要单独有这个：12306 会返回发车日与查询日不符的车次（实测查 2026-10-02
    返回 09-30 / 10-01 的车），一旦只按价格和时长比选，一趟早一天的车就会因为
    "便宜 50 块"胜出，行程整体前移一天而没人发现。日期不是偏好项，是"这趟车到底
    在不在那天"，所以这个差值必须参与比选并如实写进理由。

    取不到发车时间或没有基准日就返回 ``None``（无从比较，不当成 0 蒙混过关）。
    """
    if option is None or expected is None:
        return None
    departure = getattr(option, "departure_at", None)
    if departure is None:
        return None
    return (departure.date() - expected).days


def departure_deadline_minutes(
    option: FlightOption | TrainOption | None,
    *,
    transfer_minutes: int | None = None,
) -> int | None:
    """返程当天"最晚必须结束市内活动"的分钟数（PRD §21.2：最后一天必须考虑返程）。"""
    if option is None or option.departure_at is None:
        return None
    depart = option.departure_at.hour * 60 + option.departure_at.minute
    transfer = transfer_buffer_minutes(option) if transfer_minutes is None else transfer_minutes
    return depart - boarding_buffer_minutes(option) - transfer


def _mode_from_intent(intent: TripIntent | None) -> str:
    """用户交通偏好 → 市内默认出行方式（用于没有真实路线时选估算速度）。"""
    text = " ".join(coerce_str_list(getattr(intent, "transport_preferences", None))) if intent else ""
    if any(word in text for word in ("步行", "走", "walk")):
        return "walking"
    if any(word in text for word in ("打车", "自驾", "租车", "driving", "taxi")):
        return "driving"
    return "transit"


def _effective_duration(item: ItineraryItem, place: Place | None) -> int:
    """item 的停留时长：显式时长 > 起止时间差 > 景点自身的建议时长 > 类型默认。"""
    if item.duration_minutes:
        return item.duration_minutes
    start, end = item.start_minutes, parse_clock(item.end_time)
    if start is not None and end is not None and end > start:
        return end - start
    if place is not None and place.expected_duration_minutes:
        return place.expected_duration_minutes
    return DEFAULT_DURATION_BY_TYPE.get(item.type, 60)


def _arrival_buffer(item: ItineraryItem) -> int:
    """PRD §22 不等式里的 arrival_buffer：到达这个 item 之后还要花掉的时间。

    对交通 item 而言它不是"到达缓冲"而是**提前到站缓冲** —— 飞机要提前 2 小时到机场，
    这个约束必须体现在上一段结束时间上，否则会排出"17:00 还在景区、18:00 起飞"。
    """
    if item.type == "transport":
        return _BOARDING_BUFFER_BY_MODE.get(coerce_str(item.transport_mode).lower(), 0)
    return _ARRIVAL_BUFFER_BY_TYPE.get(item.type, DEFAULT_TRANSFER_BUFFER_MINUTES)


def _exit_buffer(item: ItineraryItem, is_last: bool) -> int:
    """抵达类交通（当天后面还有安排）的"走出站/取行李"时间。"""
    if is_last:
        return 0
    return _EXIT_BUFFER_BY_MODE.get(coerce_str(item.transport_mode).lower(), 0)


def _transfer_buffer(previous: ItineraryItem | None, item: ItineraryItem) -> int:
    """普通换乘缓冲；上一段是抵达类交通时不再叠加（出站 buffer 已经覆盖）。"""
    if previous is not None and previous.type == "transport":
        if coerce_str(previous.transport_mode).lower() in _EXIT_BUFFER_BY_MODE:
            return 0
    if item.type == "transport":
        return 0
    return DEFAULT_TRANSFER_BUFFER_MINUTES


def _leg_minutes(
    routes: Mapping | None,
    previous: ItineraryItem | None,
    item: ItineraryItem,
    mode: str,
) -> tuple[int | None, RouteOption | None, bool, str | None]:
    """一段移动的耗时；返回 (分钟, 真实路线, 是否已核实, 未核实原因)。

    路线只认高德：拿不到就退化成 haversine 粗估并返回 verified=False，
    调用方必须把它标成 `route_unverified`（PRD §32）。
    """
    if previous is None:
        return 0, None, True, None
    route = lookup_route(routes, previous.place_id, item.place_id, previous.name, item.name)
    if route is not None and route.duration_minutes is not None:
        return route.duration_minutes, route, route.verified, (
            None if route.verified else "高德路线被标记为未核实"
        )
    estimated, meta = estimate_route_minutes(previous.coords, item.coords, mode)
    if estimated is not None:
        return estimated, None, False, f"高德路线不可用，按直距 × 绕行系数估算（{meta['method']}）"
    return UNKNOWN_ROUTE_MINUTES_FALLBACK, None, False, "缺少坐标与路线数据，按同城兜底值估算"


@dataclass
class _WalkResult:
    """一天走一遍的结果：时间表、每段路线、发现的问题。"""

    schedule: list[tuple[int, int]]
    legs: list[TravelLeg | None]
    issues: list[FeasibilityIssue] = field(default_factory=list)


def _walk_day(
    day: ItineraryDay,
    routes: Mapping | None,
    places: Mapping[str, Place] | None,
    *,
    day_start_floor: str | int | None = None,
    day_end_ceiling: str | int | None = None,
    mode_hint: str = "transit",
    announce_missing: bool = True,
) -> _WalkResult:
    """按 PRD §22 的不等式走一遍当天所有 item。

    核心推进（每一步都取 `max(排定时间, 最早可行时间)`，所以某一项被推迟会**级联**
    影响到后面所有项 —— 这正是 §4.3 场景里"14:00 武侯祠"必须被识别出来的原因）：

        previous_end + exit_buffer + transfer_buffer + route_duration + arrival_buffer <= next_start
    """
    items = list(day.items)
    schedule: list[tuple[int, int]] = []
    legs: list[TravelLeg | None] = []
    issues: list[FeasibilityIssue] = []
    floor = parse_clock(day_start_floor)
    ceiling = parse_clock(day_end_ceiling)
    previous: ItineraryItem | None = None
    previous_end_effective: int | None = None

    for index, item in enumerate(items):
        place = places.get(item.place_id) if (places and item.place_id) else None
        duration = _effective_duration(item, place)
        scheduled = item.start_minutes
        is_last = index == len(items) - 1

        if index == 0:
            legs.append(None)
            # 当天以交通段开场时，floor 不能套在它身上：航班/车次的时刻是工具返回的真实数据，
            # 而 floor（"抵达后最早能开始活动"）约束的是它**之后**那一段。
            required = None if item.type == "transport" else floor
        else:
            minutes, route, verified, note = _leg_minutes(routes, previous, item, mode_hint)
            transfer = _transfer_buffer(previous, item)
            arrival = _arrival_buffer(item)
            required = (previous_end_effective or 0) + transfer + (minutes or 0) + arrival
            legs.append(
                TravelLeg.from_route(
                    route,
                    from_place_id=previous.place_id if previous else None,
                    to_place_id=item.place_id,
                    mode=mode_hint,
                    fallback_minutes=None if route is not None else minutes,
                )
            )
            if not verified:
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="warning",
                        code="ROUTE_UNVERIFIED",
                        reason=(
                            f"{previous.name} → {item.name} 的路线未经高德核实：{note}；"
                            f"排程按 {minutes} 分钟占位，不能当作真实路况"
                        ),
                        original_start=minutes_to_clock(scheduled),
                        revised_start=None,
                    )
                )

        if scheduled is None:
            start = required if required is not None else DAY_START_MINUTES
            if announce_missing:
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="warning",
                        code="MISSING_START_TIME",
                        reason="该 item 没有开始时间，已按上一段推算的可行时间补齐",
                        original_start=None,
                        revised_start=minutes_to_clock(start),
                    )
                )
        elif required is not None and scheduled < required:
            code = "ARRIVAL_CHAIN_CONFLICT" if index == 0 else "TRANSFER_TIME_SHORTFALL"
            if index == 0:
                reason = (
                    f"当天最早可开始时间为 {minutes_to_clock(required)}，"
                    f"排在此之前的 {minutes_to_clock(scheduled)} 不可行"
                )
            else:
                reason = (
                    f"{previous.name if previous else '上一地点'} 结束于 "
                    f"{minutes_to_clock(previous_end_effective)}, 加缓冲与路线后最早 "
                    f"{minutes_to_clock(required)} 才能到达 {item.name}，"
                    f"排定的 {minutes_to_clock(scheduled)} 不可行"
                )
            issues.append(
                FeasibilityIssue(
                    day_index=day.day_index,
                    item_id=item.id,
                    severity="error",
                    code=code,
                    reason=reason,
                    original_start=minutes_to_clock(scheduled),
                    revised_start=minutes_to_clock(required),
                )
            )
            start = required
        else:
            start = scheduled

        end = start + duration
        schedule.append((start, end))

        # --- 景点/餐厅的营业时间与最晚进入 ---
        opening = parse_opening_hours(place.opening_hours) if place is not None else None
        if opening is not None:
            if opening.open_minutes is not None and start < opening.open_minutes:
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="error",
                        code="OPENING_TIME_CONFLICT",
                        reason=(
                            f"{item.name} 营业时间 {opening.raw}，"
                            f"{minutes_to_clock(start)} 早于开门时间 {minutes_to_clock(opening.open_minutes)}"
                        ),
                        original_start=minutes_to_clock(start),
                        revised_start=minutes_to_clock(opening.open_minutes),
                    )
                )
            if opening.last_entry_minutes is not None and start > opening.last_entry_minutes:
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="error",
                        code="LAST_ENTRY_MISSED",
                        reason=(
                            f"{item.name} 最晚进入 {minutes_to_clock(opening.last_entry_minutes)}"
                            f"（{opening.raw}），已经赶不上，需要改到其它日期或删除"
                        ),
                        original_start=minutes_to_clock(start),
                        revised_start=None,
                    )
                )
            if opening.close_minutes is not None and end > opening.close_minutes:
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="error",
                        code="CLOSING_TIME_OVERRUN",
                        reason=(
                            f"{item.name} 停留到 {minutes_to_clock(end)}，"
                            f"超过闭园时间 {minutes_to_clock(opening.close_minutes)}"
                            f"（{opening.raw}），需要缩短停留或改期"
                        ),
                        original_start=minutes_to_clock(start),
                        revised_start=None,
                    )
                )
            if "闭" in (opening.note or ""):
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index,
                        item_id=item.id,
                        severity="warning",
                        code="OPENING_HOURS_NEEDS_CONFIRMATION",
                        reason=f"{item.name} 的营业时间存在例外（{opening.note}），需人工确认当天是否开放",
                        original_start=minutes_to_clock(start),
                        revised_start=None,
                    )
                )

        # --- 用餐时间是否合理 ---
        if item.type == "food":
            if LUNCH_WINDOW[0] - 60 <= start <= LUNCH_WINDOW[1] + 60:
                if not (LUNCH_WINDOW[0] <= start <= LUNCH_WINDOW[1]):
                    issues.append(
                        FeasibilityIssue(
                            day_index=day.day_index, item_id=item.id, severity="warning",
                            code="MEAL_TIME_ODDITY",
                            reason=(
                                f"{item.name} 是午餐安排，但落在 {minutes_to_clock(start)}"
                                f"（合理区间 {minutes_to_clock(LUNCH_WINDOW[0])}-{minutes_to_clock(LUNCH_WINDOW[1])}）"
                            ),
                            original_start=minutes_to_clock(start), revised_start=None,
                        )
                    )
            elif 16 * 60 <= start <= 22 * 60 and not (DINNER_WINDOW[0] <= start <= DINNER_WINDOW[1]):
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index, item_id=item.id, severity="warning",
                        code="MEAL_TIME_ODDITY",
                        reason=(
                            f"{item.name} 是晚餐安排，但落在 {minutes_to_clock(start)}"
                            f"（合理区间 {minutes_to_clock(DINNER_WINDOW[0])}-{minutes_to_clock(DINNER_WINDOW[1])}）"
                        ),
                        original_start=minutes_to_clock(start), revised_start=None,
                    )
                )

        # --- 酒店入住时间（早于 14:00 只能寄存行李，且必须说明依据） ---
        if item.type == "hotel" and (item.booking_note or "").startswith("check-in"):
            if start < HOTEL_STANDARD_CHECKIN_MINUTES and "寄存" not in (item.booking_note or ""):
                issues.append(
                    FeasibilityIssue(
                        day_index=day.day_index, item_id=item.id, severity="warning",
                        code="HOTEL_CHECKIN_EARLY",
                        reason=(
                            f"{minutes_to_clock(start)} 到达酒店，早于业界常规入住时间 "
                            f"{minutes_to_clock(HOTEL_STANDARD_CHECKIN_MINUTES)}，"
                            "一般只能先寄存行李（是否有寄存服务需确认）；"
                            "若确认可寄存，可用 HOTEL_LUGGAGE_DROP_MINUTES 覆盖该段时长"
                        ),
                        original_start=minutes_to_clock(start), revised_start=None,
                    )
                )

        # --- 最后一天的返程硬约束 ---
        if ceiling is not None and end > ceiling and item.type != "transport":
            issues.append(
                FeasibilityIssue(
                    day_index=day.day_index, item_id=item.id, severity="error",
                    code="RETURN_DEPARTURE_CONFLICT",
                    reason=(
                        f"{item.name} 到 {minutes_to_clock(end)} 才结束，"
                        f"但最后一天必须在 {minutes_to_clock(ceiling)} 前结束市内行程才能赶上返程交通"
                    ),
                    original_start=minutes_to_clock(start), revised_start=None,
                )
            )

        previous_end_effective = end + _exit_buffer(item, is_last)
        previous = item

    # --- 一天是否排太满 ---
    stay_items = [item for item in items if item.type != "transport"]
    max_items = tuning().max_items_per_day
    if len(stay_items) > max_items:
        issues.append(
            FeasibilityIssue(
                day_index=day.day_index, item_id=None, severity="warning", code="DAY_OVERLOADED",
                reason=f"当天安排了 {len(stay_items)} 个停留点，超过建议上限 {max_items} 个",
            )
        )
    active_minutes = sum(
        _effective_duration(item, places.get(item.place_id) if (places and item.place_id) else None)
        for item in stay_items
    )
    if active_minutes > MAX_ACTIVE_MINUTES_PER_DAY:
        issues.append(
            FeasibilityIssue(
                day_index=day.day_index, item_id=None, severity="warning", code="DAY_OVERLOADED",
                reason=(
                    f"当天有效游览 {active_minutes} 分钟（{active_minutes / 60:.1f} 小时），"
                    f"超过建议上限 {MAX_ACTIVE_MINUTES_PER_DAY / 60:.0f} 小时"
                ),
            )
        )

    # --- 折返 / 反复横跨 ---
    for reason in detect_backtracking(items):
        issues.append(
            FeasibilityIssue(
                day_index=day.day_index, item_id=None, severity="warning", code="BACKTRACKING",
                reason=reason,
            )
        )
    return _WalkResult(schedule=schedule, legs=legs, issues=issues)


def check_feasibility(
    day: ItineraryDay,
    routes: Mapping | None = None,
    intent: TripIntent | None = None,
    *,
    places: Mapping[str, Place] | None = None,
    day_start_floor: str | int | None = None,
    day_end_ceiling: str | int | None = None,
    mode_hint: str | None = None,
) -> list[FeasibilityIssue]:
    """检查一天的可行性（PRD §22）：**由代码判断，不由模型自由判断**。

    `day_start_floor`：当天最早可开始时间（第一天=抵达链结束时间，见 arrival_ready_minutes）。
    `day_end_ceiling`：当天最晚必须结束时间（最后一天=departure_deadline_minutes）。
    `places`：place_id → Place，用于取营业时间。
    """
    return _walk_day(
        day,
        routes,
        places,
        day_start_floor=day_start_floor,
        day_end_ceiling=day_end_ceiling,
        mode_hint=mode_hint or _mode_from_intent(intent),
    ).issues


def revise_day(
    day: ItineraryDay,
    issues: Sequence[FeasibilityIssue],
    routes: Mapping | None = None,
    intent: TripIntent | None = None,
    *,
    places: Mapping[str, Place] | None = None,
    day_start_floor: str | int | None = None,
    day_end_ceiling: str | int | None = None,
    mode_hint: str | None = None,
) -> tuple[ItineraryDay, list[FeasibilityIssue]]:
    """修订一天：只做**能证明是对的**动作。

    1. 时间冲突（TRANSFER_TIME_SHORTFALL / ARRIVAL_CHAIN_CONFLICT / OPENING_TIME_CONFLICT /
       MISSING_START_TIME）：按不等式**向后平移**，并把级联影响传给后面所有 item；
    2. 返程赶不上（RETURN_DEPARTURE_CONFLICT）：向后平移没有意义，删掉该 item 及其之后
       的非交通 item（返程交通本身必须保留），删除动作记入 day.notes 与 issue.reason；
    3. 无解的问题（LAST_ENTRY_MISSED / CLOSING_TIME_OVERRUN）保持未解决，
       交给 Critic 去拒绝或要求换方案 —— 不假装修好了。

    返回 (修订后的 day, 带 original_start/revised_start/resolved 的完整问题列表)。
    """
    already_resolved = [issue for issue in issues if issue.resolved]
    pending = [issue for issue in issues if not issue.resolved]
    if not pending:
        return day, list(issues)

    removal_ids = {
        issue.item_id
        for issue in pending
        if issue.severity == "error" and issue.code in REMOVAL_CODES and issue.item_id
    }
    removal_index = min(
        (index for index, item in enumerate(day.items) if item.id in removal_ids), default=None
    )
    kept: list[ItineraryItem] = []
    dropped: list[ItineraryItem] = []
    for index, item in enumerate(day.items):
        if removal_index is not None and index >= removal_index and item.type != "transport":
            dropped.append(item)
            continue
        kept.append(item.model_copy(deep=True))

    # 营业时间来不及的点：只移除**它自己**（后面的点还能去，不像返程冲突那样必须截断），
    # 并如实写进 notes。这样 plan 里不会再出现"闭园之后还安排参观"的条目。
    opening_drop_ids = {
        issue.item_id
        for issue in pending
        if issue.severity == "error" and issue.code in OPENING_HOURS_CODES and issue.item_id
    }
    if opening_drop_ids:
        opening_dropped = [item for item in kept if item.id in opening_drop_ids]
        if opening_dropped:
            kept = [item for item in kept if item.id not in opening_drop_ids]
            dropped.extend(opening_dropped)

    resolved_mode = mode_hint or _mode_from_intent(intent)
    working = ItineraryDay(
        day_index=day.day_index,
        date=day.date,
        area=day.area,
        items=kept,
        notes=list(day.notes),
    )
    if dropped:
        # 两类移除的说明分开写：返程截断与营业时间移除的后果完全不同，混成一句话
        # 用户看不出"为什么少了一个点"。
        removed_for_return = [item for item in dropped if item.type != "transport" and item.id in removal_ids]
        removed_for_hours = [item for item in dropped if item.id in opening_drop_ids]
        if removed_for_return:
            working.notes.append(
                "因返程时间不足已移除：" + "、".join(item.name for item in removed_for_return)
            )
        if removed_for_hours:
            working.notes.append(
                "因营业时间内安排不下已移除：" + "、".join(item.name for item in removed_for_hours)
            )
        note_names = {item.name for item in removed_for_return} | {item.name for item in removed_for_hours}
        remaining_names = [item.name for item in dropped if item.name not in note_names]
        if remaining_names:
            working.notes.append("因时间来不及已移除：" + "、".join(remaining_names))
    if not any(item.type not in ("transport", "free_time") for item in working.items):
        # 一个点都留不下时，别把这一天渲染成空白卡片；如实说明"没排下"。
        working.items.insert(
            0,
            ItineraryItem(
                id=f"d{day.day_index}-free",
                type="free_time",
                name="自由活动 / 机动时间",
                duration_minutes=DEFAULT_DURATION_BY_TYPE["free_time"],
                reason="当天的候选点都落在营业时间之外，已留作机动（不编造行程，也不安排闭园后的参观）",
            ),
        )
    walk = _walk_day(
        working,
        routes,
        places,
        day_start_floor=day_start_floor,
        day_end_ceiling=day_end_ceiling,
        mode_hint=resolved_mode,
        announce_missing=False,
    )

    revised_items: list[ItineraryItem] = []
    for index, item in enumerate(working.items):
        start, end = walk.schedule[index]
        revised_items.append(
            item.model_copy(
                update={
                    "start_time": minutes_to_clock(start),
                    "end_time": minutes_to_clock(end),
                    "duration_minutes": end - start,
                    "travel_from_previous": walk.legs[index],
                    "area": item.area or working.area,
                }
            )
        )
    revised_day = ItineraryDay(
        day_index=working.day_index,
        date=working.date,
        area=working.area,
        items=revised_items,
        notes=working.notes,
    )

    # 再走一遍"已按新时间排定"的行程，用它的 issues 当作真正残留的问题。
    # 第一遍走的是还带着旧排定时间的 item，那些 TRANSFER_TIME_SHORTFALL 正是本次修订
    # 要修掉的对象；若拿第一遍的结果当残留，会把"已平移好"误报成"没修好"。
    settle = _walk_day(
        revised_day,
        routes,
        places,
        day_start_floor=day_start_floor,
        day_end_ceiling=day_end_ceiling,
        mode_hint=resolved_mode,
        announce_missing=False,
    )
    # 用第二遍的 legs 覆盖：第一遍的 legs 是按旧时间算的，第二遍才是最终排定下的真实分段。
    if len(settle.legs) == len(revised_day.items):
        revised_day = revised_day.model_copy(
            update={
                "items": [
                    item.model_copy(update={"travel_from_previous": settle.legs[index]})
                    for index, item in enumerate(revised_day.items)
                ]
            }
        )

    remaining: dict[tuple[str | None, str], FeasibilityIssue] = {
        (issue.item_id, issue.code): issue for issue in settle.issues
    }
    dropped_ids = {item.id for item in dropped}
    final: list[FeasibilityIssue] = list(already_resolved)
    for issue in pending:
        key = (issue.item_id, issue.code)
        if key in remaining:
            replacement = remaining.pop(key)
            final.append(
                issue.model_copy(update={"revised_start": replacement.revised_start or issue.revised_start})
            )
            continue
        update: dict[str, Any] = {"resolved": True}
        new_item = revised_day.item_by_id(issue.item_id) if issue.item_id else None
        if new_item is not None and new_item.start_time:
            update["revised_start"] = new_item.start_time
        if issue.item_id in dropped_ids and issue.code in REMOVAL_CODES:
            update["reason"] = f"{issue.reason}（修订动作：移除该 item）"
        elif issue.item_id in dropped_ids and issue.code in OPENING_HOURS_CODES:
            update["reason"] = f"{issue.reason}（修订动作：该点在营业时间内安排不下，已从当天移除）"
        final.append(issue.model_copy(update=update))
    final.extend(remaining.values())
    return revised_day, final


def apply_feasibility_pass(
    days: Sequence[ItineraryDay],
    routes: Mapping | None = None,
    intent: TripIntent | None = None,
    *,
    places: Mapping[str, Place] | None = None,
    first_day_start: str | int | None = None,
    first_day_index: int = 0,
    last_day_deadline: str | int | None = None,
    mode_hint: str | None = None,
) -> tuple[list[ItineraryDay], list[FeasibilityIssue]]:
    """对整份行程逐天检查 + 修订（PRD §25 workflow 的 check_feasibility → critic_and_revise）。

    `first_day_start` 只作用在 `first_day_index` 那一天：跨午夜/跨天的去程里，
    "抵达链最早能出门的时间"属于抵达日，不属于出发日 —— 套错天会把整天的行程
    推到出发日的深夜，排出"人还没起飞就在目的地逛景点"。
    """
    revised_days: list[ItineraryDay] = []
    all_issues: list[FeasibilityIssue] = []
    last_index = len(days) - 1
    for index, day in enumerate(days):
        floor = first_day_start if index == first_day_index else None
        ceiling = last_day_deadline if index == last_index else None
        issues = check_feasibility(
            day, routes, intent, places=places, day_start_floor=floor,
            day_end_ceiling=ceiling, mode_hint=mode_hint,
        )
        new_day, final = revise_day(
            day, issues, routes, intent, places=places, day_start_floor=floor,
            day_end_ceiling=ceiling, mode_hint=mode_hint,
        )
        revised_days.append(new_day)
        all_issues.extend(final)
    return revised_days, all_issues


# ==================================================
# 十、候选排序与行程生成（PRD §21、§24）
# ==================================================

CANDIDATE_TRUST_WEIGHT = 0.55
CANDIDATE_AD_RISK_WEIGHT = 0.45
CANDIDATE_PREFERENCE_BONUS_PER_HIT = 12.0
CANDIDATE_PREFERENCE_BONUS_CAP = 24.0
CANDIDATE_ROUTE_FIT_BONUS = 15.0
#: 入选门槛：score_candidate 低于此值的地点不进计划。负分意味着"广告风险已经盖过证据价值"，
#: 排进去反而是负价值；被排除的地点会写进 day.notes，不静默丢弃。
MIN_CANDIDATE_SCORE = 0.0
#: 用户偏好 → 地点/文本线索词。命中越多加成越高（PRD §19：最终筛选要结合偏好与路线适配）。
PREFERENCE_KEYWORDS = {
    "美食": ("美食", "吃", "餐厅", "小吃", "火锅", "夜市", "烧烤", "串串", "川菜", "food"),
    "拍照": ("拍照", "摄影", "出片", "机位", "打卡", "网红", "观景"),
    "历史文化": ("历史", "文化", "博物馆", "古迹", "古镇", "寺", "祠", "遗址", "故居"),
    "自然": ("自然", "公园", "山", "湖", "湿地", "森林", "徒步", "风景"),
    "购物": ("购物", "商圈", "步行街", "百货", "市场", "市集"),
    "夜景": ("夜景", "灯光", "夜游", "酒吧", "观景台"),
    "亲子": ("亲子", "乐园", "动物", "科技馆", "儿童"),
    "放松": ("放松", "休闲", "温泉", "茶馆", "慢", "水疗"),
}
#: 路线适配的归一化尺度：5km 内线性衰减到 0。
ROUTE_FIT_RANGE_KM = 5.0


def preference_hits(
    place: Place,
    user_preferences: Sequence[str],
    evidences: Sequence[Evidence] = (),
) -> list[str]:
    """用户偏好命中：名字/类型/攻略文本里出现偏好关键词即算命中。

    偏好词表之外的自定义偏好（例如"想看大熊猫"）直接按子串匹配，
    匹配不到就是没命中 —— 不给"大概符合"的分。
    """
    haystack = " ".join(
        [place.name, place.type or "", " ".join(evidence.text or "" for evidence in evidences)]
    )
    hits: list[str] = []
    for preference in user_preferences or []:
        words = PREFERENCE_KEYWORDS.get(preference)
        if words is None:
            words = tuple(
                word for word in re.split(r"[、,/\s]+", preference) if word
            ) or (preference,)
        if any(word and word in haystack for word in words):
            hits.append(preference)
    return hits


def score_candidate(
    place: Place,
    trust: float,
    ad_risk_score: float,
    user_preferences: Sequence[str],
    route_fit: float,
    evidences: Sequence[Evidence] = (),
    *,
    profile: PreferenceProfile | None = None,
) -> tuple[float, dict]:
    """候选得分（PRD §19 结尾 + §24）：Trust 与 Ad Risk 是主项，偏好与路线适配是修正项。

    公式（全部为常量，便于审计复述）：
        score = 0.55×Trust − 0.45×AdRisk + min(12×命中偏好数, 24) + 15×路线适配%
    Ad Risk 高**不等于删除**：一个 Trust 90 / AdRisk 60 的地点依然可能被保留，
    而 Trust 40 / AdRisk 80 的网红店会被挤下去（PRD §19）。

    ``profile`` 是动态偏好画像（E1）：个性化时按 profile.attraction 强调"用户兴趣 /
    证据支撑 / 路线适配"三项在得分里的权重（emphasis = profile/default，均衡基线 = 1.0，
    与历史公式完全一致）。
    """
    hits = preference_hits(place, user_preferences, evidences)
    pref_emph = emphasis(profile, "attraction", "user_interest")
    evidence_emph = emphasis(profile, "attraction", "evidence")
    route_emph = emphasis(profile, "attraction", "route_fit")
    preference_bonus = (
        min(CANDIDATE_PREFERENCE_BONUS_PER_HIT * len(hits), CANDIDATE_PREFERENCE_BONUS_CAP)
        * pref_emph
    )
    fit = min(1.0, max(0.0, route_fit))
    score = (
        CANDIDATE_TRUST_WEIGHT * trust * evidence_emph
        - CANDIDATE_AD_RISK_WEIGHT * ad_risk_score
        + preference_bonus
        + CANDIDATE_ROUTE_FIT_BONUS * fit * route_emph
    )
    detail = {
        "trust": round(trust, 2),
        "trust_component": round(CANDIDATE_TRUST_WEIGHT * trust, 2),
        "ad_risk": round(ad_risk_score, 2),
        "ad_risk_component": round(-CANDIDATE_AD_RISK_WEIGHT * ad_risk_score, 2),
        "preference_hits": hits,
        "preference_bonus": round(preference_bonus, 2),
        "route_fit": round(fit, 3),
        "route_fit_bonus": round(CANDIDATE_ROUTE_FIT_BONUS * fit, 2),
        "score": round(score, 2),
        "basis": (
            f"Trust {trust:.0f}×0.55 − AdRisk {ad_risk_score:.0f}×0.45 "
            f"+ 偏好 {len(hits)} 项（{preference_bonus:.0f}）+ 路线适配 {fit * 100:.0f}%（{CANDIDATE_ROUTE_FIT_BONUS * fit:.0f}）"
        ),
    }
    return round(score, 2), detail


def _anchor_distance(place: Place, anchor: Sequence[float] | None) -> float | None:
    return haversine_meters(place.coords, anchor)


def order_by_proximity(places: Sequence[Place], anchor: Sequence[float] | None) -> list[Place]:
    """从锚点出发的最近邻排序（PRD §21.1：地理聚类 → 每天主要区域 → 路线耗时）。

    没有坐标的点排在最后（位置未知的不该插进顺路序列），顺序用名字兜底保证稳定。
    """
    ordered: list[Place] = []
    remaining = list(places)
    cursor = anchor
    while remaining:
        if cursor is None:
            ordered.extend(sorted(remaining, key=lambda place: place.name))
            break
        def key(place: Place) -> tuple:
            distance = _anchor_distance(place, cursor)
            return (float("inf") if distance is None else distance, place.name)

        nearest = min(remaining, key=key)
        ordered.append(nearest)
        remaining.remove(nearest)
        cursor = nearest.coords or cursor
    return ordered


def _proximity_km(place: Place, anchor: Sequence[float] | None) -> float | None:
    distance = _anchor_distance(place, anchor)
    return None if distance is None else round(distance / 1000, 2)


def _place_duration(place: Place, item_type: str) -> int:
    if place.expected_duration_minutes:
        return place.expected_duration_minutes
    return DEFAULT_DURATION_BY_TYPE.get(item_type, 90)


def _item_type_for(place: Place) -> str:
    """高德 type / 名字 → item 类型。只做保守判断：认不出就是景点。"""
    text = f"{place.type or ''} {place.name}"
    if any(word in text for word in ("餐饮", "餐厅", "小吃", "火锅", "串串", "烧烤", "面馆", "咖啡", "茶楼", "美食")):
        return "food"
    if any(word in text for word in ("购物", "商场", "商圈", "步行街", "市集", "市场")):
        return "activity"
    if any(word in text for word in ("公园", "广场", "观景", "夜景")):
        return "activity"
    return "attraction"


def _ticket_price(ticket_prices: Mapping[str, float] | None, place: Place) -> float | None:
    return _lookup_price(ticket_prices, place.place_id, place.name)


def _build_stay_items(
    places: Sequence[Place],
    *,
    day_index: int,
    trust_scores: Mapping[str, float],
    ad_risks: Mapping[str, float],
    trust_details: Mapping[str, dict],
    evidences: Mapping[str, Sequence[Evidence]],
    ticket_prices: Mapping[str, float] | None,
    user_preferences: Sequence[str],
    anchor: Sequence[float] | None,
    profile: PreferenceProfile | None = None,
) -> list[ItineraryItem]:
    """把候选地点变成"要停留的 item"，并写好 reason / evidence_ids / price_type。

    reason 必须由真实计算出来的数字拼成：Trust 分、命中的偏好、路线适配度、
    以及 Trust 明细里贡献最大的那一项依据 —— 不允许出现"值得一去"这种模板话。
    """
    items: list[ItineraryItem] = []
    for place in places:
        item_type = _item_type_for(place)
        place_evidences = list(evidences.get(place.place_id, []) or [])
        trust = coerce_float(trust_scores.get(place.place_id), 0.0) or 0.0
        risk = coerce_float(ad_risks.get(place.place_id), 0.0) or 0.0
        fit_km = _proximity_km(place, anchor)
        fit = 0.0 if fit_km is None else max(0.0, 1.0 - fit_km / ROUTE_FIT_RANGE_KM)
        score, detail = score_candidate(place, trust, risk, user_preferences, fit, place_evidences, profile=profile)

        basis_parts: list[str] = []
        detail_map = trust_details.get(place.place_id) or {}
        components = {
            key: value for key, value in detail_map.items()
            if isinstance(value, dict) and "score" in value and key != "total"
        }
        if components:
            top_key = max(components, key=lambda key: components[key]["score"])
            basis_parts.append(f"主要依据：{components[top_key].get('basis', top_key)}")
        if place.alias_key and place.name != place.alias_key:
            basis_parts.append(f"归一化名「{place.normalized_name or place.alias_key}」")

        ticket = _ticket_price(ticket_prices, place)
        items.append(
            ItineraryItem(
                id=f"d{day_index}-{place.place_id}",
                type=item_type,
                place_id=place.place_id,
                name=place.name,
                lat=place.lat,
                lng=place.lng,
                duration_minutes=_place_duration(place, item_type),
                price=ticket,
                price_type="realtime" if ticket is not None else "unknown",
                trust_score=round(trust, 1),
                ad_risk=round(risk, 1),
                reason="；".join(
                    [f"候选得分 {score:.0f}（{detail['basis']}）"] + basis_parts
                ),
                evidence_ids=sorted({evidence.id for evidence in place_evidences}),
                source_ids=sorted(
                    {evidence.source_id for evidence in place_evidences if evidence.source_id}
                    | ({place.source_id} if place.source_id else set())
                ),
                area=place.district or place.business_area or None,
            )
        )
    return items


def _transport_item(
    option: FlightOption | TrainOption,
    *,
    item_id: str,
    role: str,
) -> ItineraryItem:
    """把航班/车次变成 transport item：start=出发时刻、end=抵达时刻。"""
    is_flight = isinstance(option, FlightOption)
    if is_flight:
        name = f"{option.flight_no} {option.departure_airport or option.origin or ''}→{option.arrival_airport or option.destination or ''}".strip()
        mode = "flight"
        place_id = f"transport-{option.flight_no}-{role}"
    else:
        name = f"{option.train_no} {option.origin_station or ''}→{option.destination_station or ''}".strip()
        mode = "train"
        place_id = f"transport-{option.train_no}-{role}"
    start = option.departure_at
    end = option.arrival_at
    duration = option.duration_minutes
    if duration is None and start and end:
        duration = int((end - start).total_seconds() // 60)
    # 跨天的段必须把**日期**写进说明：start/end 是纯时刻，"21:30 → 00:15" 单看会
    # 以为是同一天，实际是次日凌晨落地；46 小时的火车更是跨了两天。
    cross_day = start is not None and end is not None and start.date() != end.date()
    span = ""
    if cross_day:
        span = (
            f"（{start.strftime('%m-%d %H:%M')} 出发 → {end.strftime('%m-%d %H:%M')} 抵达"
            + (f"，历时 {duration // 60} 小时 {duration % 60} 分" if duration else "")
            + "，跨天）"
        )
    return ItineraryItem(
        id=item_id,
        type="transport",
        place_id=place_id,
        name=name,
        start_time=minutes_to_clock(start.hour * 60 + start.minute) if start else None,
        # 跨天段的 end_time 留空：抵达时刻不属于这一天，硬写会变成"结束早于开始"或
        # "跨出当天"。真实抵达时刻在 reason 里写清楚，抵达日那天会重新按抵达链排。
        end_time=None if cross_day else (minutes_to_clock(end.hour * 60 + end.minute) if end else None),
        duration_minutes=duration,
        transport_mode=mode,
        price=option.price,
        price_type="realtime" if option.price is not None else "unknown",
        source_ids=[option.source_id] if option.source_id else [],
        reason=(
            f"{'去程' if role == 'outbound' else '回程'}交通方案{span}，价格来自 {option.provider}"
            f"（查询时间 {option.fetched_at.isoformat(timespec='seconds')}）"
        ),
    )


def _transit_day_note(
    outbound: FlightOption | TrainOption | None,
    intent: TripIntent,
    arrival_index: int,
) -> str:
    """在途日的说明。写清楚"人在哪、什么时候到"，避免用户以为这天是被漏排了。"""
    if outbound is None:
        return "在途：去程安排未确定，本日不排游玩点"
    label = (
        f"{outbound.flight_no} {outbound.departure_airport or ''}→{outbound.arrival_airport or ''}".strip()
        if isinstance(outbound, FlightOption)
        else f"{outbound.train_no} {outbound.origin_station or ''}→{outbound.destination_station or ''}".strip()
    )
    departure = outbound.departure_at
    arrival = outbound.arrival_at
    when = ""
    if departure and arrival:
        when = (
            f"（{departure.strftime('%m-%d %H:%M')} 出发 → "
            f"{arrival.strftime('%m-%d %H:%M')} 抵达）"
        )
    return (
        f"在途：{label}{when}，第 {arrival_index + 1} 天才抵达目的地，"
        "本日不安排游玩点 —— 不是漏排，是那天人还在路上"
    )


def _hotel_coords(hotel: HotelOption | None) -> tuple[float, float] | None:
    """酒店坐标。HotelOption 没有 Place 的 coords，这里统一取 lat/lng 并校验成对存在。"""
    if hotel is None or hotel.lat is None or hotel.lng is None:
        return None
    return (hotel.lat, hotel.lng)


def _hotel_item(hotel: HotelOption, *, item_id: str, check_in: bool) -> ItineraryItem:
    """酒店的入住/退房 item。入住时长用 HOTEL_CHECKIN_MINUTES（PRD §22 需要它参与不等式）。"""
    return ItineraryItem(
        id=item_id,
        type="hotel",
        place_id=hotel.hotel_id or f"hotel-{hotel.name}",
        name=f"{hotel.name}（{'办理入住' if check_in else '退房'}）",
        lat=hotel.lat,
        lng=hotel.lng,
        duration_minutes=HOTEL_CHECKIN_MINUTES if check_in else HOTEL_CHECKOUT_MINUTES,
        price=hotel.total_price,
        price_type="realtime" if hotel.total_price is not None else "unknown",
        source_ids=[hotel.source_id] if hotel.source_id else [],
        booking_note="check-in" if check_in else "check-out",
        reason=(
            f"住宿来自 {hotel.provider}，"
            + (hotel.price_note or "价格以预订页为准")
        ),
    )


#: 候选方案的拓扑变体（`_order_clusters` 的排序策略）。
#: 它们**只改"区域先后的排序策略"**，不改任何时间/约束逻辑，因此每个变体都是同一套
#: 硬约束下的合法行程。
PLAN_VARIANT_NEAREST = "nearest"
PLAN_VARIANT_SCORE_FIRST = "score_first"
PLAN_VARIANT_PREFERENCE_FIRST = "preference_first"
PLAN_VARIANT_LIGHT_FIRST = "light_first"
PLAN_VARIANT_DISTINCT_FIRST = "distinct_first"


def _order_clusters(
    clusters: Sequence[Sequence[Place]],
    *,
    anchor: Sequence[float] | None,
    variant: str,
    scores: Mapping[str, float] | None = None,
    preferences: Sequence[str] | None = None,
) -> list[list[Place]]:
    """把地理簇排成"先玩哪个区"的顺序。

    ``nearest`` 保持历史行为不变（从住宿点出发找最近的区，再以该区最近的点为游标继续），
    其余变体只换比较函数；任何一种都不会丢点，只是访问顺序不同。
    """

    remaining = [list(cluster) for cluster in clusters]
    scores = scores or {}
    preferences = list(preferences or ())

    def proximity(place: Place, cursor: Sequence[float] | None) -> float:
        distance = _anchor_distance(place, cursor)
        return float("inf") if distance is None else distance

    def cluster_proximity(cluster: Sequence[Place], cursor: Sequence[float] | None) -> float:
        return min((proximity(place, cursor) for place in cluster), default=float("inf"))

    if variant == PLAN_VARIANT_NEAREST:
        ordered: list[list[Place]] = []
        cursor = anchor
        while remaining:
            best_index, best_distance = 0, None
            for index, cluster in enumerate(remaining):
                for member in cluster:
                    distance = _anchor_distance(member, cursor)
                    if distance is None:
                        continue
                    if best_distance is None or distance < best_distance:
                        best_index, best_distance = index, distance
            cluster = remaining.pop(best_index)
            ordered.append(cluster)
            first = min(cluster, key=lambda place: (proximity(place, cursor), place.name))
            cursor = first.coords
        return ordered

    def mean_score(cluster: Sequence[Place]) -> float:
        values = [coerce_float(scores.get(place.place_id), 0.0) or 0.0 for place in cluster]
        return sum(values) / len(values) if values else 0.0

    def preference_weight(cluster: Sequence[Place]) -> int:
        return sum(len(preference_hits(place, preferences, [])) for place in cluster)

    def dominant_type(cluster: Sequence[Place]) -> str:
        types = [_item_type_for(place) for place in cluster]
        return max(sorted(set(types)), key=types.count) if types else "other"

    if variant == PLAN_VARIANT_SCORE_FIRST:
        return sorted(remaining, key=lambda cluster: (-mean_score(cluster), cluster_proximity(cluster, anchor)))
    if variant == PLAN_VARIANT_PREFERENCE_FIRST:
        return sorted(
            remaining,
            key=lambda cluster: (-preference_weight(cluster), cluster_proximity(cluster, anchor)),
        )
    if variant == PLAN_VARIANT_LIGHT_FIRST:
        return sorted(
            remaining,
            key=lambda cluster: (len(cluster), cluster_proximity(cluster, anchor)),
        )

    # distinct_first：按"当天主导类型"分组后轮转取用，让相邻两天的活动性质差异更大。
    groups: dict[str, list[list[Place]]] = {}
    for cluster in remaining:
        groups.setdefault(dominant_type(cluster), []).append(cluster)
    for members in groups.values():
        members.sort(key=lambda cluster: cluster_proximity(cluster, anchor))
    order = sorted(groups, key=lambda key: (-len(groups[key]), key))
    interleaved: list[list[Place]] = []
    while any(groups[key] for key in order):
        for key in order:
            if groups[key]:
                interleaved.append(groups[key].pop(0))
    return interleaved


def build_initial_plan(
    intent: TripIntent,
    places: Sequence[Place],
    *,
    trust_scores: Mapping[str, float] | None = None,
    ad_risks: Mapping[str, float] | None = None,
    trust_details: Mapping[str, dict] | None = None,
    evidences: Mapping[str, Sequence[Evidence]] | None = None,
    routes: Mapping | None = None,
    hotel: HotelOption | None = None,
    outbound: FlightOption | TrainOption | None = None,
    inbound: FlightOption | TrainOption | None = None,
    ticket_prices: Mapping[str, float] | None = None,
    city: str | None = None,
    mode_hint: str | None = None,
    variant: str = PLAN_VARIANT_NEAREST,
    profile: PreferenceProfile | None = None,
) -> list[ItineraryDay]:
    """生成初始行程（PRD §21）。

    步骤与 PRD §21.1 一致：
      候选（已按 score_candidate 排序）→ 地理聚类 → 每天一个主要区域 → 时间窗
      → 路线耗时与缓冲 → 第一天从抵达时间开始 → 最后一天预留返程 → 午饭晚饭在合理时间。

    时间只在最后一步由 `_walk_day` 统一推算（与可行性检查用**同一套 buffer**），
    所以这里排出来的时间本身就是可行的，后续 feasibility 阶段只负责发现问题与修订。

    ``variant`` 只改变"区域先后的排序策略"（见 `_order_clusters`），不改变任何时间
    与约束逻辑 —— 因此每个变体都是同一套硬约束下的合法方案：选项之间只有偏好与节奏
    的差别，没有可行性差别。
    """
    day_count = max(1, intent.days)
    city = city or (intent.destination[0] if intent.destination else None)
    mode = mode_hint or _mode_from_intent(intent)
    trust_scores = trust_scores or {}
    ad_risks = ad_risks or {}
    trust_details = trust_details or {}
    evidences = evidences or {}

    # 候选打分：与 _build_stay_items 用的 score_candidate 同源，保证"排序依据"和"入选依据"一致。
    # 顺序 = 先取分数高的，再谈地理聚类（PRD §21.1 的"候选 → 聚类"）。
    candidate_scores: dict[str, tuple[float, dict]] = {}
    for place in places:
        place_evidences = list(evidences.get(place.place_id, []) or [])
        fit = 0.0 if _hotel_coords(hotel) is None else max(
            0.0, 1.0 - (_proximity_km(place, _hotel_coords(hotel)) or ROUTE_FIT_RANGE_KM) / ROUTE_FIT_RANGE_KM
        )
        candidate_scores[place.place_id] = score_candidate(
            place,
            coerce_float(trust_scores.get(place.place_id), 0.0) or 0.0,
            coerce_float(ad_risks.get(place.place_id), 0.0) or 0.0,
            intent.preferences,
            fit,
            place_evidences,
            profile=profile,
        )
    sorted_places = sorted(
        places, key=lambda place: (-candidate_scores[place.place_id][0], place.name)
    )

    # 分数为负 = "风险大于收益"（几乎没有证据支撑、又带广告风险）。这种点不排进计划，
    # 但要如实写进 notes，而不是静默消失 —— 用户有权知道候选里有什么被排除了。
    min_score = tuning().min_candidate_score
    eligible = [place for place in sorted_places if candidate_scores[place.place_id][0] >= min_score]
    rejected = [place for place in sorted_places if candidate_scores[place.place_id][0] < min_score]

    def day_date(index: int) -> date | None:
        return None if intent.start_date is None else intent.start_date + timedelta(days=index)

    def _arrives_on(option: Any, index: int) -> bool:
        stamp = getattr(option, "arrival_at", None) if option is not None else None
        if stamp is None:
            return option is not None and index == 0
        return day_date(index) is None or stamp.date() == day_date(index)

    def _departs_on(option: Any, index: int) -> bool:
        stamp = getattr(option, "departure_at", None) if option is not None else None
        if stamp is None:
            return False
        return day_date(index) is None or stamp.date() == day_date(index)

    # --- 分天容量：第一天要给抵达链留时间，最后一天要给返程留时间 ---
    #: 去程真正抵达的那一天（跨午夜/跨天的去程会落在后面几天）。
    arrival_index = arrival_day_index(outbound, intent.start_date, day_count)
    capacities: list[int] = []
    for index in range(day_count):
        if index < arrival_index:
            # 人在路上：这几天不排任何游玩点。以前这里给 0 会被下面的 max(1, ...) 顶回 1，
            # 于是出发日也被排上了景点 —— 现在真的给 0，交通日就是交通日。
            capacities.append(0)
            continue
        capacity = tuning().max_items_per_day
        if index == arrival_index and outbound is not None:
            capacity -= 2
        if index == day_count - 1 and inbound is not None and _departs_on(inbound, day_count - 1):
            capacity -= 1
        capacities.append(max(1, capacity))

    # --- 地理聚类 → 按顺序分配给每一天 ---
    clusters = cluster_places(eligible, max_cluster_km=tuning().default_cluster_km)
    anchor_coords = _hotel_coords(hotel)
    ordered_clusters = _order_clusters(
        clusters,
        anchor=anchor_coords,
        variant=variant,
        scores={place_id: score for place_id, (score, _) in candidate_scores.items()},
        preferences=list(intent.preferences or ()),
    )

    # 分配时**整簇优先**（PRD §21.2"一个主要区域优先、避免反复横跨城市"）：
    # 一个簇能塞进当天就整簇塞；塞不下且当天已有点，就把整簇留给下一天，避免把一个区域劈成两天。
    cluster_queue = [list(cluster) for cluster in ordered_clusters]

    days: list[ItineraryDay] = []
    for index in range(day_count):
        capacity = capacities[index]
        day_places: list[Place] = []
        while cluster_queue and len(day_places) < capacity:
            head = cluster_queue[0]
            room = capacity - len(day_places)
            if len(head) <= room:
                day_places.extend(head)
                cluster_queue.pop(0)
                continue
            if day_places:
                break
            day_places.extend(head[:room])
            cluster_queue[0] = head[room:]
        floor: int | None = None
        ceiling: int | None = None
        items: list[ItineraryItem] = []
        anchor: Sequence[float] | None = anchor_coords

        # 去程交通挂在**出发日**：跨天时它是第 0 天唯一的一件事，后面的天才是"在路上"。
        # 同一天出发并抵达（arrival_index == 0）时行为与以前一致。
        if outbound is not None and index == 0:
            items.append(_transport_item(outbound, item_id="d0-outbound", role="outbound"))
        if index == arrival_index and outbound is not None:
            floor = arrival_day_start_minutes(outbound, with_checkin=True)
            anchor = _hotel_coords(hotel) or anchor
        if hotel is not None and index == arrival_index:
            items.append(_hotel_item(hotel, item_id=f"d{index}-hotel-checkin", check_in=True))
        if index < arrival_index:
            day_note = _transit_day_note(outbound, intent, arrival_index)
        else:
            day_note = None
        if hotel is not None and index == day_count - 1 and day_count > 1:
            items.append(_hotel_item(hotel, item_id=f"d{index}-hotel-checkout", check_in=False))
        if inbound is not None and _departs_on(inbound, index):
            ceiling = departure_deadline_minutes(inbound)

        # 选点：已按得分排序，这里再按地理顺序串成一条线（先分数选、后地理排）。
        ordered_day_places = order_by_proximity(day_places, anchor or _hotel_coords(hotel))
        foods = [place for place in ordered_day_places if _item_type_for(place) == "food"]
        others = [place for place in ordered_day_places if _item_type_for(place) != "food"]

        stay_items = _build_stay_items(
            others,
            day_index=index,
            trust_scores=trust_scores,
            ad_risks=ad_risks,
            trust_details=trust_details,
            evidences=evidences,
            ticket_prices=ticket_prices,
            user_preferences=intent.preferences,
            anchor=anchor,
            profile=profile,
        )
        meal_items = _build_stay_items(
            foods,
            day_index=index,
            trust_scores=trust_scores,
            ad_risks=ad_risks,
            trust_details=trust_details,
            evidences=evidences,
            ticket_prices=ticket_prices,
            user_preferences=intent.preferences,
            anchor=anchor,
            profile=profile,
        )
        items.extend(_insert_meals(stay_items, meal_items))

        if inbound is not None and _departs_on(inbound, index):
            items.append(_transport_item(inbound, item_id=f"d{index}-inbound", role="inbound"))

        day = ItineraryDay(
            day_index=index,
            date=day_date(index),
            area=_area_name(day_places),
            items=items,
            notes=[day_note] if day_note else [],
        )
        day = _trim_to_window(day, routes, places_map=None, floor=floor, ceiling=ceiling, mode=mode)
        if not day_places and index < arrival_index:
            # 在途日：给一条明确的"人在路上"条目，别让前端渲染出一张空白日程卡。
            day.items.append(
                ItineraryItem(
                    id=f"d{index}-transit",
                    type="free_time",
                    name="在途（交通日）",
                    duration_minutes=DEFAULT_DURATION_BY_TYPE["free_time"],
                    reason=day_note or "在途",
                )
            )
            day = _trim_to_window(day, routes, places_map=None, floor=floor, ceiling=ceiling, mode=mode)
        if not day_places and index >= arrival_index:
            day.items.insert(
                0,
                ItineraryItem(
                    id=f"d{index}-free",
                    type="free_time",
                    name="自由活动 / 机动时间",
                    duration_minutes=DEFAULT_DURATION_BY_TYPE["free_time"],
                    reason=(
                        "当天没有安排到符合证据与偏好门槛的地点："
                        "候选不足或时间窗不够，留作机动（不编造行程）"
                    ),
                ),
            )
            day = _trim_to_window(day, routes, places_map=None, floor=floor, ceiling=ceiling, mode=mode)
        if not day.items:
            # 兜底：候选点有、但一个都没排进来（末日要赶早班返程时最典型 —— 时间窗比
            # 起床时刻还早）。这里**不重新 trim**（再 trim 一次还是会被掐掉），
            # 也**不给它编一个开始时刻**：如实说明"当天没有排得下的安排"。
            day.items.append(
                ItineraryItem(
                    id=f"d{index}-free",
                    type="free_time",
                    name="自由活动 / 机动时间",
                    duration_minutes=DEFAULT_DURATION_BY_TYPE["free_time"],
                    reason=(
                        "当天可用时间窗排不下任何一个符合门槛的安排"
                        "（抵达太晚或末日要赶返程），留作机动 —— 不编造行程"
                    ),
                )
            )
        # 被时间窗裁掉的停留点放回队首交给后面的天，而不是直接丢掉：
        # 第一天塞不下的地方往往第二天能去，静默丢弃会白白浪费已经查到的候选。
        placed_ids = {item.place_id for item in day.items if item.place_id}
        carried = [place for place in day_places if place.place_id not in placed_ids]
        if carried:
            # 裁掉的那些本来已经进了 notes（"时间窗不足未安排"），这里改成"顺延到次日"，
            # 否则同一批地点会被重复报告两次。
            day.notes = [note for note in day.notes if not note.startswith("时间窗不足未安排")]
            cluster_queue.insert(0, carried)
        days.append(day)

    dropped_candidates: list[Place] = [place for cluster in cluster_queue for place in cluster]
    if dropped_candidates:
        note = "因天数与时间窗限制未安排：" + "、".join(place.name for place in dropped_candidates[:8])
        if days:
            days[-1].notes.append(note)
    if rejected and days:
        days[0].notes.append(
            "因证据不足/广告风险高于收益未纳入候选："
            + "、".join(place.name for place in rejected[:8])
        )
    return days


def _area_name(places: Sequence[Place]) -> str | None:
    """当天主要区域：取出现最多的行政区，其次城市名（没有就 None，不猜）。"""
    districts = [place.district for place in places if place.district]
    if districts:
        return max(sorted(set(districts)), key=lambda name: districts.count(name))
    cities = [place.city for place in places if place.city]
    return cities[0] if cities else None


def _insert_meals(
    stay_items: Sequence[ItineraryItem],
    meal_items: Sequence[ItineraryItem],
) -> list[ItineraryItem]:
    """把餐厅插到午餐/晚餐时间窗附近。

    做法：先看"如果插在第 i 个位置，餐厅大概几点开始"（用上一项的结束时间 + 距离粗估），
    选第一个落在 11:30-14:00 的位置放午饭、17:00-21:00 的位置放晚饭。
    实在放不进窗口就追加在当天末尾 —— 至少不会把午饭排到下午三点之后还硬说"合理"。
    """
    meals = deque(meal_items)
    if not meals:
        return list(stay_items)
    result: list[ItineraryItem] = []
    lunch_placed = dinner_placed = False
    for index, item in enumerate(stay_items):
        while meals:
            probe = _probe_meal_start(result, item, meals[0])
            if not lunch_placed and LUNCH_WINDOW[0] <= probe <= LUNCH_WINDOW[1]:
                result.append(meals.popleft())
                lunch_placed = True
                continue
            if lunch_placed and not dinner_placed and DINNER_WINDOW[0] <= probe <= DINNER_WINDOW[1]:
                result.append(meals.popleft())
                dinner_placed = True
                continue
            break
        result.append(item)
    while meals:
        result.append(meals.popleft())
    return result


def _probe_meal_start(
    placed: Sequence[ItineraryItem],
    upcoming: ItineraryItem,
    meal: ItineraryItem,
) -> int:
    """粗估"如果在 upcoming 之前插入 meal，它大概几点开始"。

    只用上一项的结束时间、坐标距离与默认缓冲，不引入高德调用 —— 这里只是排序用，
    真正的时刻由 `_walk_day` 统一算，所以粗估不会污染最终时间。
    """
    if placed:
        previous = placed[-1]
        base = previous.end_minutes or previous.start_minutes or DAY_START_MINUTES
        distance = haversine_meters(previous.coords, meal.coords)
        minutes = 0 if distance is None else max(3, int(round(distance * DETOUR_FACTOR / (SPEED_KMH["transit"] * 1000 / 60))))
        return base + DEFAULT_TRANSFER_BUFFER_MINUTES + minutes + MEAL_ARRIVAL_BUFFER_MINUTES
    base = upcoming.end_minutes or upcoming.start_minutes or DAY_START_MINUTES
    return base - MEAL_DURATION_MINUTES - DEFAULT_TRANSFER_BUFFER_MINUTES


def _trim_to_window(
    day: ItineraryDay,
    routes: Mapping | None,
    *,
    places_map: Mapping[str, Place] | None,
    floor: int | None,
    ceiling: int | None,
    mode: str,
) -> ItineraryDay:
    """掐掉超出当天时间窗（含最后一天返程）的停留点，并重算时间。

    上限取 `day_end_ceiling`（有返程）与 `DAY_END_MINUTES`（常规）中的更早者 ——
    "不要一天排太满"和"最后一天要赶得上车"在这里落地。

    唯一的例外是**深夜抵达**：floor 本身已经晚于 DAY_END_MINUTES 时，当天就该只剩
    "出站 → 到酒店 → 入住"，所以把窗口顺延到 floor + LATE_ARRIVAL_WINDOW_MINUTES。
    但无论怎么顺延都不越过 MAX_DAY_MINUTES —— 一天不会有 25 点，也不会有凌晨三点的博物馆。
    """
    limit = tuning().day_end_minutes if ceiling is None else min(tuning().day_end_minutes, ceiling)
    floor_minutes = parse_clock(floor)
    if floor_minutes is not None and floor_minutes > limit:
        limit = min(MAX_DAY_MINUTES, floor_minutes + LATE_ARRIVAL_WINDOW_MINUTES)
    working_items = [item.model_copy(deep=True) for item in day.items]
    dropped: list[ItineraryItem] = []
    for _ in range(4):  # 掐掉尾部后重算，最多迭代几次（级联不会超过 4 次）
        candidate = ItineraryDay(
            day_index=day.day_index, date=day.date, area=day.area, items=working_items, notes=list(day.notes)
        )
        walk = _walk_day(
            candidate, routes, places_map,
            day_start_floor=floor, day_end_ceiling=ceiling, mode_hint=mode, announce_missing=False,
        )
        overflow = [
            index
            for index, (_, end) in enumerate(walk.schedule)
            if end > limit and working_items[index].type != "transport"
        ]
        if not overflow:
            break
        first = min(overflow)
        # 只丢被掐掉的**停留点**，交通段一律留着：它是既成事实（票已经买了 / 车就那趟），
        # 不是可以因为"当天时间不够"就取消的可选项。
        # 这里以前是 `working_items[first:]` 整段切片，末日返程段排在末尾，
        # 于是"最后一个景点装不下"会把**回家的那一段一起丢掉**，
        # 行程显示旅客玩到最后一天再也没回来 —— 返程只在 transport 块里，不在日程里。
        kept: list[ItineraryItem] = []
        for offset, item in enumerate(working_items):
            if offset >= first and item.type != "transport":
                dropped.append(item)
            else:
                kept.append(item)
        working_items = kept
    if not working_items:
        return ItineraryDay(day_index=day.day_index, date=day.date, area=day.area, items=[], notes=list(day.notes))
    candidate = ItineraryDay(
        day_index=day.day_index, date=day.date, area=day.area, items=working_items, notes=list(day.notes)
    )
    walk = _walk_day(
        candidate, routes, places_map,
        day_start_floor=floor, day_end_ceiling=ceiling, mode_hint=mode, announce_missing=False,
    )
    items = [
        item.model_copy(
            update={
                "start_time": minutes_to_clock(walk.schedule[index][0]),
                "end_time": minutes_to_clock(walk.schedule[index][1]),
                "duration_minutes": walk.schedule[index][1] - walk.schedule[index][0],
                "travel_from_previous": walk.legs[index],
                "area": item.area or day.area,
            }
        )
        for index, item in enumerate(working_items)
    ]
    notes = list(day.notes)
    if dropped:
        notes.append("时间窗不足未安排：" + "、".join(item.name for item in dropped))
    return ItineraryDay(day_index=day.day_index, date=day.date, area=day.area, items=items, notes=notes)


# ==================================================
# 十一、Critic（PRD §25、§32）
# ==================================================

#: Critic 判定阈值。
AD_RISK_HIGH = 70.0
AD_RISK_TRUST_FLOOR = 50.0
#: 基准置信度与扣分项。
PLAN_BASE_CONFIDENCE = 0.95
CONFIDENCE_PENALTY_UNVERIFIED_ROUTE = 0.15
CONFIDENCE_PENALTY_UNRESOLVED_ERROR = 0.2
CONFIDENCE_PENALTY_OVER_BUDGET = 0.1
CONFIDENCE_PENALTY_AMAP_DEGRADED = 0.3


def critique(
    plan: TripPlan,
    decisions: Sequence[Decision] | None = None,
    *,
    evidences: Sequence[Evidence] | None = None,
    amap_status: str = "OK",
    stage: str = "critic",
) -> list[Decision]:
    """规则化 Critic（PRD §25 的 critic 步骤、§32）。

    产出的决策：
      * 未解决的时间冲突 → REJECT（带 PROBLEM 代码与原因文本）
      * 已自动修订 → PASS / AUTO_REVISED（保留 original → revised 供审计展示）
      * 高 Ad Risk 且低 Trust 的入选地点 → REVISION（建议人工确认或替换）
      * 未经核实的高德路线 → REVISION（PRD §32）
      * 超预算 → REJECT（附上来自真实候选的优化建议）
      * 未被任何 item 引用的证据 → NOT_USED（PRD §28：要能回答"哪些来源没被采用"）
      * 汇总一条置信度决策：高德失败时**下调置信度**（PRD §32）

    `decisions` 传入已有决策时，同一 (entity_id, code) 不会重复产出。
    """
    decisions = list(decisions or [])
    seen = {
        (decision.entity_id, code)
        for decision in decisions
        for code in (decision.reason_codes or ["__none__"])
    }
    result: list[Decision] = []

    def add(entity_id: str, status: DecisionStatus, codes: list[str], reason: str, scores: dict | None = None) -> None:
        key = (entity_id, codes[0] if codes else "__none__")
        if key in seen:
            return
        seen.add(key)
        result.append(
            Decision(
                entity_id=entity_id,
                status=status,
                agent_or_stage=stage,
                reason_codes=codes,
                reason_text=reason,
                scores=scores or {},
            )
        )

    unresolved_errors = 0
    unverified_legs = 0
    for day in plan.days:
        for item in day.items:
            if item.travel_from_previous is not None and not item.travel_from_previous.verified:
                unverified_legs += 1
                add(
                    item.id,
                    DecisionStatus.REVISION,
                    ["ROUTE_UNVERIFIED", "CONFIDENCE_REDUCED"],
                    (
                        f"{item.name} 的上一段路线未经高德核实"
                        f"（{'估算' if item.travel_from_previous.estimated else '缺失'}），"
                        "时间仅供占位，出发前需确认"
                    ),
                    {"verified": False, "mode": item.travel_from_previous.mode},
                )
            if (
                item.type in ("attraction", "food", "activity")
                and (item.ad_risk or 0) >= tuning().ad_risk_high
                and (item.trust_score or 0) < AD_RISK_TRUST_FLOOR
            ):
                add(
                    item.id,
                    DecisionStatus.REVISION,
                    ["HIGH_AD_RISK", "LOW_TRUST", "SUGGEST_DROP"],
                    (
                        f"{item.name} Ad Risk {item.ad_risk:.0f} 且 Trust {item.trust_score or 0:.0f}，"
                        "广告嫌疑高且证据不足，建议人工确认或替换"
                    ),
                    {"ad_risk": item.ad_risk, "trust": item.trust_score},
                )

    for issue in plan.warnings or []:
        entity = issue.item_id or f"day:{issue.day_index}"
        if issue.severity == "error" and not issue.resolved:
            unresolved_errors += 1
            add(
                entity,
                DecisionStatus.REJECT,
                ["FEASIBILITY_ERROR", issue.code],
                f"时间/营业时间冲突未解决：{issue.reason}",
                {"code": issue.code, "day_index": issue.day_index},
            )
        elif issue.resolved:
            add(
                entity,
                DecisionStatus.PASS,
                ["AUTO_REVISED", issue.code],
                (
                    f"已自动修订：{issue.original_start or '未定时'} → {issue.revised_start or '已移除'}；"
                    f"原因：{issue.reason}"
                ),
                {
                    "code": issue.code,
                    "original_start": issue.original_start,
                    "revised_start": issue.revised_start,
                },
            )

    budget = plan.budget
    if budget is not None:
        if budget.status == "over_budget":
            add(
                "budget",
                DecisionStatus.REJECT,
                ["OVER_BUDGET"],
                (
                    f"预计总花费 ¥{budget.projected_total:,.0f} 超出预算 "
                    f"¥{budget.budget_total:,.0f}（超 ¥{budget.over_by:,.0f}）。优化建议："
                    + "；".join(budget.optimization_suggestions or ["没有可用的省钱候选"])
                ),
                {"projected_total": budget.projected_total, "budget_total": budget.budget_total},
            )
        elif budget.status == "unknown":
            add(
                "budget",
                DecisionStatus.PASS,
                ["BUDGET_UNKNOWN"],
                f"用户没有给预算，只给出预计总花费 ¥{budget.projected_total:,.0f}"
                f"（其中实时费用 ¥{budget.known_real_cost:,.0f}、估算 ¥{budget.estimated_cost:,.0f}）",
                {"projected_total": budget.projected_total},
            )

    if evidences:
        used = {evidence_id for day in plan.days for item in day.items for evidence_id in item.evidence_ids}
        for evidence in evidences:
            if evidence.id in used:
                add(
                    evidence.id,
                    DecisionStatus.USED,
                    ["REFERENCED_BY_ITEM"],
                    "被 itinerary item 引用为推荐依据",
                    {"source_url": evidence.source_url},
                )
            else:
                add(
                    evidence.id,
                    DecisionStatus.NOT_USED,
                    ["NO_ITINERARY_REFERENCE"],
                    "没有被任何 itinerary item 采用（未通过筛选或与行程路线不匹配）",
                    {"provider": evidence.provider, "source_type": evidence.source_type},
                )

    confidence = PLAN_BASE_CONFIDENCE
    confidence -= CONFIDENCE_PENALTY_UNVERIFIED_ROUTE * unverified_legs
    confidence -= CONFIDENCE_PENALTY_UNRESOLVED_ERROR * unresolved_errors
    if budget is not None and budget.status == "over_budget":
        confidence -= CONFIDENCE_PENALTY_OVER_BUDGET
    degraded = coerce_str(amap_status).upper() not in ("", "OK")
    if degraded:
        confidence -= CONFIDENCE_PENALTY_AMAP_DEGRADED
    confidence = round(max(0.05, min(1.0, confidence)), 2)
    codes = ["PLAN_CONFIDENCE"]
    if degraded:
        codes.append("AMAP_DEGRADED")
    if unverified_legs:
        codes.append("HAS_UNVERIFIED_ROUTE")
    if unresolved_errors:
        codes.append("HAS_UNRESOLVED_ERROR")
    add(
        plan.run_id,
        DecisionStatus.PASS if not unresolved_errors else DecisionStatus.REVISION,
        codes,
        (
            f"计划置信度 {confidence:.0%}（高德状态 {amap_status}、"
            f"未核实路段 {unverified_legs} 段、未解决冲突 {unresolved_errors} 个）"
        ),
        {
            "confidence": confidence,
            "unverified_legs": unverified_legs,
            "unresolved_errors": unresolved_errors,
            "amap_status": amap_status,
        },
    )
    return result


# ==================================================
# 十二、方案质量度量（接管任务 §4 §9）
# ==================================================
# 这些指标是**横向比较用的相对量**，不是绝对真理：它们的用途是回答
# "这份行程的节奏与连贯性到底怎么样"。因此全部由代码从已算好的
# days 上计算，可复现、可单测、可审计 —— 不引入任何模型打分。

#: 节奏目标（分钟/天）。与 models.PACES 的三个取值一一对应。
PACE_TARGET_MINUTES: dict[str, int] = {"relaxed": 240, "balanced": 330, "packed": 420}

#: 停留类 item（要算"玩得累不累"的那些）。
STAY_TYPES = ("attraction", "food", "activity")

REVERSAL_ANGLE_DEGREES = 120.0
MEAL_WINDOWS = ((11 * 60, 14 * 60 + 30), (17 * 60, 21 * 60))


@dataclass(slots=True)
class PlanQuality:
    """一份行程的质量画像。字段名与 Benchmark §9 的 Plan Quality 指标同名。"""

    category_diversity: float
    consecutive_same_type_count: int
    backtracking_score: float
    daily_load_balance: float
    meal_time_quality: float
    pace_match: float
    preference_coverage: float
    first_day_quality: float
    last_day_quality: float
    route_efficiency: float
    item_count: int
    stay_item_count: int
    active_minutes: int
    playable_days: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category_diversity": round(self.category_diversity, 4),
            "consecutive_same_type_count": self.consecutive_same_type_count,
            "backtracking_score": round(self.backtracking_score, 4),
            "daily_load_balance": round(self.daily_load_balance, 4),
            "meal_time_quality": round(self.meal_time_quality, 4),
            "pace_match": round(self.pace_match, 4),
            "preference_coverage": round(self.preference_coverage, 4),
            "first_day_quality": round(self.first_day_quality, 4),
            "last_day_quality": round(self.last_day_quality, 4),
            "route_efficiency": round(self.route_efficiency, 4),
            "item_count": self.item_count,
            "stay_item_count": self.stay_item_count,
            "active_minutes": self.active_minutes,
            "playable_days": self.playable_days,
        }


def _stay_items(day: ItineraryDay) -> list[ItineraryItem]:
    return [item for item in day.items if item.type != "transport" and item.type != "free_time"]


def _active_minutes(day: ItineraryDay) -> int:
    return sum(int(item.duration_minutes or 0) for item in _stay_items(day))


def plan_quality(
    days: Sequence[ItineraryDay],
    intent: TripIntent,
    *,
    places: Mapping[str, Place] | None = None,
    evidences: Mapping[str, Sequence[Evidence]] | None = None,
) -> PlanQuality:
    """计算一份行程的质量画像。

    ``places`` / ``evidences`` 可选：只在算"偏好覆盖"时需要（要拿到地点的分类与证据）。
    拿不到时按 item 名称与理由文本近似匹配，并在文档里说明这是近似 —— 不让一个
    可选输入把整份指标算成 0。
    """

    places = places or {}
    evidences = evidences or {}
    all_stay = [item for day in days for item in _stay_items(day)]
    total_items = sum(len(day.items) for day in days)
    active_minutes = sum(_active_minutes(day) for day in days)
    playable = [day for day in days if _stay_items(day)]

    # --- 类型多样性：非停留项越多越像"只有景点"---
    types = [item.type for item in all_stay]
    distinct = len(set(types))
    expected = min(len(STAY_TYPES) + 2, max(1, len(all_stay)))
    category_diversity = min(1.0, distinct / expected) if all_stay else 0.0

    # --- 相邻同类型：连续三个以上同类才算"单调"，用相邻对数衡量 ---
    consecutive = sum(1 for left, right in zip(types, types[1:]) if left == right)

    # --- 折返：同一天内两段路线的方向变化超过阈值就算一次折返 ---
    reversals = 0
    transitions = 0
    for day in days:
        located = [item for item in _stay_items(day) if item.lat is not None and item.lng is not None]
        bearings: list[float] = []
        for left, right in zip(located, located[1:]):
            bearings.append(_bearing((left.lat, left.lng), (right.lat, right.lng)))
        for left, right in zip(bearings, bearings[1:]):
            transitions += 1
            if _angle_gap(left, right) > REVERSAL_ANGLE_DEGREES:
                reversals += 1
    backtracking_score = 1.0 if transitions == 0 else max(0.0, 1.0 - reversals / transitions)

    # --- 每日负荷均衡：用变异系数衡量 ---
    loads = [_active_minutes(day) for day in playable]
    if len(loads) >= 2 and sum(loads) > 0:
        mean = sum(loads) / len(loads)
        variance = sum((value - mean) ** 2 for value in loads) / len(loads)
        daily_load_balance = max(0.0, 1.0 - (variance**0.5) / mean)
    else:
        daily_load_balance = 1.0

    # --- 用餐时间：每天该有的午饭/晚饭是否落在窗口内 ---
    food_days = [day for day in days if any(item.type == "food" for item in day.items)]
    if food_days:
        satisfied = 0
        for day in food_days:
            starts = [
                parse_clock(item.start_time)
                for item in day.items
                if item.type == "food" and parse_clock(item.start_time) is not None
            ]
            for window in MEAL_WINDOWS:
                if any(window[0] <= value <= window[1] for value in starts):
                    satisfied += 1
        meal_time_quality = satisfied / (len(food_days) * len(MEAL_WINDOWS))
    else:
        meal_time_quality = 0.0

    # --- 节奏匹配 ---
    target = PACE_TARGET_MINUTES.get(str(intent.pace or "balanced"), PACE_TARGET_MINUTES["balanced"])
    if playable:
        average = active_minutes / len(playable)
        pace_match = max(0.0, 1.0 - abs(average - target) / target)
    else:
        pace_match = 0.0

    # --- 偏好覆盖 ---
    preferences = [str(item) for item in (intent.preferences or ()) if str(item)]
    if not preferences:
        preference_coverage = 1.0
    else:
        hit_preferences: set[str] = set()
        for item in all_stay:
            place = places.get(item.place_id) if item.place_id else None
            if place is not None:
                hit_preferences.update(
                    preference_hits(place, preferences, list(evidences.get(place.place_id, []) or []))
                )
            else:
                haystack = f"{item.name} {item.reason or ''}"
                hit_preferences.update(pref for pref in preferences if pref in haystack)
        preference_coverage = len(hit_preferences) / len(preferences)

    # --- 首末日质量：第一天能不能玩、最后一天是不是被返程吃掉 ---
    def _window_quality(day: ItineraryDay | None) -> float:
        if day is None:
            return 0.0
        starts = [parse_clock(item.start_time) for item in _stay_items(day)]
        starts = [value for value in starts if value is not None]
        if not starts:
            return 0.0
        return 1.0 if DAY_START_MINUTES <= min(starts) <= 12 * 60 else 0.5

    first_day_quality = _window_quality(playable[0] if playable else None)
    last_day_quality = _window_quality(playable[-1] if playable else None)

    # --- 路线核实率 ---
    legs = [
        item.travel_from_previous
        for day in days
        for item in day.items
        if item.travel_from_previous is not None
    ]
    route_efficiency = (sum(1 for leg in legs if leg.verified) / len(legs)) if legs else 0.0

    return PlanQuality(
        category_diversity=category_diversity,
        consecutive_same_type_count=consecutive,
        backtracking_score=backtracking_score,
        daily_load_balance=daily_load_balance,
        meal_time_quality=meal_time_quality,
        pace_match=pace_match,
        preference_coverage=preference_coverage,
        first_day_quality=first_day_quality,
        last_day_quality=last_day_quality,
        route_efficiency=route_efficiency,
        item_count=total_items,
        stay_item_count=len(all_stay),
        active_minutes=active_minutes,
        playable_days=len(playable),
    )


#: 质量软指标 → 单 run 质量分的合成权重（口径单点：管理端与 run_metrics 落库共用这一份）。
#: 页面总得有一个可排序的数，"哪个分值多少"必须明说（payload 里带回 weights），
#: 不能让读者猜 —— 所以权重只在这里定义一次，`admin_analytics.plan_snapshot` 与
#: `agent_runner._save_metrics` 都从本模块取，避免两处口径打架。
QUALITY_WEIGHTS: dict[str, float] = {
    "preference_coverage": 0.25,
    "pace_match": 0.2,
    "category_diversity": 0.15,
    "backtracking_score": 0.15,
    "daily_load_balance": 0.1,
    "meal_time_quality": 0.1,
    "route_efficiency": 0.05,
}


def quality_score(quality: Mapping[str, Any]) -> float:
    """把 `plan_quality` 的软指标合成为一个可排序的分。

    与 `plan_quality` 同层：输入只有指标字典，没有任何 LLM / 网络调用，可脱网单测。
    除不尽的权重补零处理与历史 `admin_analytics._quality_score` 完全一致，
    保证新旧口径（列值 vs 解析 plan_json）对同一份计划给出同一个数。
    """

    total = 0.0
    weight_sum = 0.0
    for key, weight in QUALITY_WEIGHTS.items():
        value = quality.get(key)
        if isinstance(value, (int, float)):
            total += float(value) * weight
            weight_sum += weight
    return round(total / weight_sum, 4) if weight_sum else 0.0
