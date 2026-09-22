"""Canonical Place 实体层：让"同一个现实地点"在 TravelPlan 里只有一个身份。

为什么不能靠高德 `poi_id` 去重
------------------------------
同一个现实地点在高德那边可能是好几条记录：景区主 POI 与它的入口 / 游客中心 / 停车场、
旧 ID 与新 ID、不同查询返回的近似结果。反过来，一个 `poi_id` 也不代表"用户想去的地方"
—— 地铁站、停车场、售票处都有自己真实且不同的 `poi_id`。

所以本模块把 `poi_id` 降级成**证据**（`place_provider_refs` 里的一行），身份由 TravelPlan
自己维护：

    canonical_places      一个现实地点 = 一行
    place_aliases         这个名字属于谁（解析名字时的第一站）
    place_provider_refs   哪个 Provider 的哪个 ID 指向它（一次查询命中的最短路径）
    place_relations       主点 / 子点（"熊猫基地" 与 "熊猫基地南门"）
    poi_query_cache       "这个 query 以前查到了什么"（不是"这个 query 是谁"）

三条必须守住的原则（方案 §9 / §11 / §14）：

1. **漏合并比错合并安全**：拿不准就留两个实体。错并是静默丢地点，用户看不到任何解释。
2. **Query Cache 不直接决定实体**：缓存只给"历史候选"，实体仍要过一遍 Resolver。
3. **不同类别用不同策略**：景点看名字 + 坐标 + 地址，餐饮 / 门店更看重地址与坐标，
   连锁分店之间**绝不**因为名字像就合并。

与 `planner.dedupe_places` 的分工
--------------------------------
后者是 run 内、纯内存的一次性去重，输入是当次拿到的 `Place`；本模块跨 run / 跨会话持久化，
并且额外负责子设施收敛、连锁分店保护、地理围栏与留痕。两者共用同一套归一化与相似度原语
（`planner.normalize_place_name` / `name_similarity` / `haversine_meters`），不重写一份。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from app import planner
from app.models import Place, basic_name_key, utcnow
from app.store import TravelPlanStore

#: 目前只有高德会喂 POI 进实体层；留成常量是为了以后接第二个 Provider 时不必改签名。
PROVIDER_AMAP = "amap"

#: 一次 ingest 最多留多少条"值得看"的 Resolver 留痕。全量留痕会把 400 个 POI × 每城
#: 变成几万行，真正需要复盘的反而被淹掉；命中复用这类高频动作按词聚合成一条。
MAX_TRACES_PER_INGEST = 60


# ======================================================================
# 1. 文本归一化与设施识别
# ======================================================================

#: 名字里这些词说明它是**某个地方的附属设施**，而不是用户想去的地点本身。
#: 键是设施种类，值是判定用的词；匹配时按词长倒序，避免"车站"吃掉"地铁站"。
FACILITY_TOKENS: dict[str, tuple[str, ...]] = {
    "metro": ("地铁站", "地铁口", "轻轨站", "轨道交通站"),
    "parking": ("停车场", "停车楼", "地下车库", "车库"),
    "ticket_office": ("售票处", "售票厅", "售票点", "取票处"),
    "service_center": ("游客中心", "服务中心", "咨询中心", "服务点", "问询处"),
    "entrance": (
        "检票口", "检票处", "正大门", "正门", "大门", "门口", "入口", "出口",
        "南门", "北门", "东门", "西门",
    ),
    "bus_stop": ("公交站", "公交总站", "观光车站", "索道站", "缆车站"),
    "utility": ("公共厕所", "卫生间", "洗手间", "母婴室", "派出所", "医务室"),
}

#: 高德 type 串里的结构化标记 → 设施种类。**比名字可靠得多**：type 是平台给的结构化
#: 分类，而名字里的"草""塔"这类单字在中文地名里歧义极大（"杜甫草堂"曾被匹配成草原）。
FACILITY_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("地铁站", "metro"),
    ("轻轨站", "metro"),
    ("轨道交通", "metro"),
    ("停车场", "parking"),
    ("停车楼", "parking"),
    ("售票", "ticket_office"),
    ("游客中心", "service_center"),
    ("信息咨询", "service_center"),
    ("咨询中心", "service_center"),
    ("服务中心", "service_center"),
    ("住宿服务", "hotel"),
    ("宾馆酒店", "hotel"),
    ("旅馆", "hotel"),
    ("公交站", "bus_stop"),
    ("长途汽车站", "bus_stop"),
    ("汽车站", "bus_stop"),
    ("火车站", "rail_station"),
    ("机场", "airport"),
    ("港口", "pier"),
    ("码头", "pier"),
    ("收费站", "utility"),
    ("加油站", "utility"),
    ("加气站", "utility"),
    ("充电站", "utility"),
    ("公共厕所", "utility"),
    ("卫生间", "utility"),
    ("出入口", "entrance"),
)

#: 这些设施**永远不该**作为"可选地点"出现：用户不会把停车场 / 地铁站 / 售票处当成目的地。
#: `hotel` 也在内 —— 住宿由机酒那条链单独负责，混进景点池只会让用户在探索确认页看到
#: 一堆酒店（线上真实发生过）。
DROP_FACILITY_KINDS = frozenset(
    {
        "metro", "parking", "ticket_office", "service_center", "hotel",
        "bus_stop", "rail_station", "airport", "utility",
    }
)

#: 高德 type 里这些标记说明这条 POI **根本不构成一个可以去的地方**。
#: 与设施分开列：设施是"某个地点的组成部分"（可以挂到母体上），这些是"压根不是目的地"。
#:
#: 两个词都是从真实候选里捞出来的：
#:   * 地名地址信息 —— 地址桩。"窄巷子"就是一条地址记录，旁边的"宽窄巷子景区"已经覆盖它；
#:   * 商务住宅   —— 住宅小区 / 写字楼。"锦里苑""锦里花园"因为名字含"锦里"被关键词捞了进来，
#:                  它们是住人的小区，不是能去玩的地方。
NON_DESTINATION_TYPE_MARKERS = ("地名地址信息", "商务住宅")


def non_destination_reason(amap_type: str | None) -> str | None:
    """这条 POI 是不是"不是目的地"（地址桩 / 住宅小区）；是就返回一句给人看的理由。"""

    text = str(amap_type or "")
    if not text:
        return None
    for marker in NON_DESTINATION_TYPE_MARKERS:
        if marker in text:
            return f"高德类型是「{marker}」，不是可游玩的目的地"
    return None

#: 高德 type 串 → 用户看到的分类。**顺序即优先级**：先命中的赢。
#:
#: 「景点」族排在「购物」之前是有意的：高德给宽窄巷子 / 锦里这类历史文化街区的类型是
#: `购物服务;特色商业街 | 风景名胜;旅游景点` —— 两条都命中。按"购物优先"会把这些
#: 用户心里的景点归进"特色体验"池，按"景点优先"才是对的。纯商业体（商场 / 超市）
#: 不含风景名胜标记，仍然归购物。
TYPE_CATEGORY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("food", ("餐饮", "中餐", "快餐", "小吃", "咖啡", "茶艺", "甜品", "糕饼", "冷饮", "外国餐厅")),
    ("attraction", ("风景名胜", "旅游景点", "游乐园", "主题公园", "公园广场", "水族馆")),
    ("history", ("博物馆", "纪念馆", "展览馆", "美术馆", "科技馆", "图书馆", "文化宫", "寺庙", "道观", "教堂", "古迹", "遗址", "故居", "陵园", "祠堂", "古镇", "历史")),
    ("nature", ("公园", "动物园", "植物园", "自然", "湿地", "森林", "山", "湖", "江", "河")),
    ("shopping", ("购物服务", "商场", "商业街", "步行街", "商圈", "超级市场", "便民商店", "专卖店", "集市")),
    ("experience", ("体育休闲", "娱乐场所", "影剧院", "电影院", "歌舞厅", "温泉", "滑雪", "游船", "度假", "健身", "网吧", "酒吧", "运动场馆")),
    ("nightview", ("夜景", "观景台", "灯光", "夜游")),
)

#: 分类 → 候选池。景点族（历史 / 自然 / 亲子 / 拍照）同属"游玩"池。
#: 放在这里而不是 discovery：判定"两条 POI 能不能合并"要用它比较"是不是同一类地方"，
#: 而 discovery 依赖本模块，反向 import 会成环。
POOL_BY_CATEGORY: dict[str, str] = {
    "attraction": "attraction",
    "history": "attraction",
    "nature": "attraction",
    "family": "attraction",
    "photo": "attraction",
    "food": "food",
    "experience": "experience",
    "nightview": "experience",
    "shopping": "experience",
    "other": "experience",
}

#: "XX分店 / XX店" 的尾巴。连锁门店必须靠它区分：`蜀大侠火锅春熙路店` 与
#: `蜀大侠火锅太古里店` 名字极像，但显然是两个地点。
_BRANCH_RE = re.compile(r"([^（）()\s]{1,6})(分店|旗舰店|总店|直营店|门店|店)$")
#: "酒/饭/书/药/商" 结尾的"店"是词的一部分（酒店 / 饭店 / 书店 / 药店），不是分店后缀。
_BRANCH_STOP_TAIL = ("酒", "饭", "书", "药", "商", "餐", "网", "分", "门")
#: 地铁出口那种 "B口" / "A出入口"。
_ALPHANUM_EXIT_RE = re.compile(r"^[A-Za-z0-9]{1,2}(口|出入口|出口)$")

_SEPARATORS = ("-", "—", "–", "－", "·", "•", "・")


def place_key(name: str, city: str | None = None) -> str:
    """比较用的名字键（委托 planner，保证与 run 内去重同一套规则）。"""

    return planner.normalize_place_name(name, city=city) or basic_name_key(name)


def split_suffix(name: str) -> tuple[str, str | None]:
    """把 "杜甫草堂(地铁站)" / "宽窄巷子·宽厂" 拆成 (主体名, 后缀)。

    后缀是子设施的关键线索：括号里写"游客中心"、破折号后写"大雅堂"，都说明这一条
    描述的是**已有地点的某个部分**。没有后缀时返回 (名字, None)。
    """

    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not text:
        return "", None
    bracket = re.search(r"[（(]([^（()）]*)[)）]\s*$", text)
    if bracket and bracket.group(1).strip():
        head = text[: bracket.start()].strip()
        if head:
            return head, bracket.group(1).strip()
    for separator in _SEPARATORS:
        index = text.rfind(separator)
        if index > 0:
            tail = text[index + len(separator) :].strip()
            if tail:
                return text[:index].strip(), tail
    return text, None


def facility_kind(name: str, amap_type: str | None = None) -> str | None:
    """这条 POI 是不是某个地点的附属设施；是就返回设施种类。

    判定顺序（可靠 → 不可靠）：
      1. 高德 type 的结构化标记；
      2. 名字括号 / 分隔符后缀里的设施词；
      3. 名字整体包含设施词（"成都大熊猫繁育研究基地迎迎停车场"这种没有括号的写法）。

    刻意**不**把"博物馆""塔""堂"这类词当设施：它们是景点本身的常见组成部分，
    当成设施会把真景点删掉。
    """

    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    type_text = str(amap_type or "")
    for marker, kind in FACILITY_TYPE_MARKERS:
        if marker in type_text:
            return kind
    if not text:
        return None

    head, suffix = split_suffix(text)
    for token, kind in _token_pairs():
        if suffix and token in suffix:
            return kind
    if suffix and _ALPHANUM_EXIT_RE.match(suffix):
        return "entrance"
    # 整名包含设施词：要求设施词出现在名字的**后半段**，避免"停车场咖啡"这种
    # "设施词 + 真店名"的写法被整条判成设施。
    half = len(text) // 2
    for token, kind in _token_pairs():
        position = text.find(token)
        if position >= 0 and position >= half:
            return kind
    # 名字正好等于设施词（就叫"停车场"的 POI）没有实体价值，也不算母体。
    if head == text and any(text == token for token, _ in _token_pairs()):
        return "uncategorized"
    return None


def _token_pairs() -> list[tuple[str, str]]:
    """设施词按长度倒序展开（长的先匹配，避免"车站"吃掉"地铁站"）。"""

    pairs: list[tuple[str, str]] = []
    for kind, tokens in FACILITY_TOKENS.items():
        pairs.extend((token, kind) for token in tokens)
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def category_from_type(amap_type: str | None) -> str | None:
    """高德 type → 用户可见分类；判断不出来返回 None（**不猜**）。"""

    text = str(amap_type or "")
    if not text:
        return None
    for category, markers in TYPE_CATEGORY_MARKERS:
        if any(marker in text for marker in markers):
            return category
    return None


def branch_signature(name: str) -> str:
    """连锁门店的"哪一家分店"签名；不是分店返回空串。

    只在**两边都有签名且不同**时才判冲突：一边本名、一边带门店后缀时，坐标接近的
    情况太常见（同一家店的两个高德记录），一刀切会把它们拆成两条。
    """

    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not text:
        return ""
    head, suffix = split_suffix(text)
    for candidate in (suffix, head):
        if not candidate:
            continue
        match = _BRANCH_RE.search(candidate)
        if not match:
            continue
        tail = match.group(1)
        if not tail or tail.endswith(_BRANCH_STOP_TAIL):
            continue
        return tail
    return ""


def branch_conflict(left: str, right: str) -> bool:
    """两个名字是不是"同一品牌的**不同**分店"。名字像也绝不能合并。

    做法是取两个名字的公共前缀（品牌部分），再看各自剩下的尾巴：
      * `蜀大侠火锅|春熙路店` vs `蜀大侠火锅|太古里店` → 尾巴都是门店后缀且不同 → 冲突；
      * `星巴克|成都春熙路店` vs `星巴克|春熙路店` → 剥掉城市前缀后尾巴相同 → 不冲突。

    为什么不用"各自的门店后缀"直接比：后缀是从名字尾部反向截出来的，`星巴克春熙路店`
    会把品牌名一起截进去（"星巴克春熙路"），于是同一家店被自己判成冲突。公共前缀法
    不需要知道品牌名在哪里结束。
    """

    left_key = planner._name_text_key(left)
    right_key = planner._name_text_key(right)
    if not left_key or not right_key:
        return False
    prefix_length = _common_prefix_length(left_key, right_key)
    if prefix_length <= 0:
        return False
    left_tail = _strip_city_prefix(left_key[prefix_length:])
    right_tail = _strip_city_prefix(right_key[prefix_length:])
    if not left_tail or not right_tail or left_tail == right_tail:
        return False
    return _looks_like_branch_tail(left_tail) and _looks_like_branch_tail(right_tail)


def _common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def _strip_city_prefix(key: str) -> str:
    """剥掉尾巴开头的城市名（"成都春熙路店" → "春熙路店"）。"""

    return planner._strip_city_prefix(key, None)  # noqa: SLF001 —— 复用唯一实现


def _looks_like_branch_tail(tail: str) -> bool:
    """"XX店 / XX分店"这类门店后缀。`酒店`/`饭店`不算（"店"是词的一部分）。"""

    match = _BRANCH_RE.search(tail)
    if not match:
        return False
    name = match.group(1)
    return bool(name) and not name.endswith(_BRANCH_STOP_TAIL)


def city_key(city: str | None) -> str:
    """城市键：去掉"市/地区/自治州"这类行政后缀，让"成都"与"成都市"等价。

    只做这一层：把"乐山"与"成都"归并这种事绝不允许发生（那是跨城错并）。
    """

    key = planner._name_text_key(city)
    for suffix in ("特别行政区", "自治区", "自治州", "地区", "市", "省"):
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)]
    return key


def same_city(left: str | None, right: str | None) -> bool:
    """两个城市名是否同一座城市（键完全相等）。任一为空时返回 False（**不猜**）。"""

    left_key, right_key = city_key(left), city_key(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key


def city_related(left: str | None, right: str | None) -> bool:
    """两个行政名是否指向同一片区域 —— 容许"目的地是县/县级市，Provider 报地级市"。

    为什么需要：高德的 `cityname` 是**地级行政区**。查"婺源"时它回 `上饶市`，查"大理"时
    回 `大理白族自治州` —— 与用户输入的目的地名字都不相等。只做相等比较会把整座城市的
    POI 全部判成"越界"（线上真实发生过：婺源与大理整城候选被清空）。
    所以：键相等、或短的是长的前缀（`大理` ⊂ `大理白族`）都算同一片区域。

    只做前缀，不做包含："山" 这类包含关系会把不相邻的地方混在一起。
    """

    left_key, right_key = city_key(left), city_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    short, long = sorted((left_key, right_key), key=len)
    return len(short) >= 2 and long.startswith(short)


def place_in_city(
    city: str | None, district: str | None, destination: str | None
) -> bool:
    """这条 POI 是不是目的地的地点。

    三级判定，逐级放宽，**只在能证明它属于别处时才判否**：
      1. Provider 报的城市与目的地相关 → 是；
      2. Provider 报的行政区（`adname`，如"婺源县""大理市"）与目的地相关 → 是
         （这一条专门救"目的地是县 / 县级市"的情形）；
      3. 两者都拿不到 → 放行（拿不到字段不等于结果无关）；
      4. 两者都拿到了、却都与目的地无关 → 判否（这才是真的越界，例如成都检索里的乐山大佛）。
    """

    destination_key = city_key(destination)
    if not destination_key:
        return True
    has_city = bool(city_key(city))
    has_district = bool(city_key(district))
    if city_related(city, destination) or city_related(district, destination):
        return True
    return not (has_city or has_district)


def canonical_id_for(city: str, name: str, lat: float | None, lng: float | None) -> str:
    """由 (城市, 名字键, 坐标) 派生确定性的实体 ID。

    为什么不用自增序号：重建 / 重跑必须落回**同一行**，否则每次预热都会长出一批新实体，
    而 `place_provider_refs` 的指向会在两批之间来回跳。坐标进哈希是为了让"同名不同地"
    天然拿到两个 ID（方案 §9.3：宁可保留两个实体，也不要错并）。
    """

    if lat is None or lng is None:
        location = "nocoord"
    else:
        location = f"{lat:.5f},{lng:.5f}"
    identity = f"{city_key(city)}|{place_key(name, city)}|{location}"
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"place_canonical_{digest}"


# ======================================================================
# 2. 记录结构
# ======================================================================


@dataclass(slots=True)
class PoiRecord:
    """一条 Provider POI（在进实体层之前）。字段名对齐高德返回。"""

    provider: str
    provider_place_id: str
    name: str
    amap_type: str = ""
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    city: str | None = None
    district: str | None = None
    business_area: str | None = None
    opening_hours: str | None = None
    source_query: str = ""
    order: int = 0

    @classmethod
    def from_place(cls, place: Place, *, source_query: str = "", order: int = 0) -> "PoiRecord":
        return cls(
            provider=PROVIDER_AMAP,
            provider_place_id=str(place.place_id or ""),
            name=str(place.name or ""),
            amap_type=str(place.type or ""),
            lat=place.lat,
            lng=place.lng,
            address=place.address,
            city=place.city,
            district=place.district,
            business_area=place.business_area,
            opening_hours=place.opening_hours,
            source_query=source_query,
            order=order,
        )

    @property
    def coords(self) -> tuple[float, float] | None:
        if self.lat is None or self.lng is None:
            return None
        return (self.lat, self.lng)

    @property
    def name_key(self) -> str:
        return place_key(self.name, self.city)

    @property
    def facility(self) -> str | None:
        return facility_kind(self.name, self.amap_type)

    @property
    def is_facility(self) -> bool:
        return self.facility is not None

    def to_place(self) -> Place:
        return Place(
            place_id=self.provider_place_id or self.name,
            name=self.name,
            normalized_name=self.name_key,
            type=self.amap_type,
            lat=self.lat,
            lng=self.lng,
            address=self.address,
            city=self.city,
            district=self.district,
            business_area=self.business_area,
            opening_hours=self.opening_hours,
            amap_verified=bool(self.provider_place_id),
        )


@dataclass(slots=True)
class EntityRecord:
    """一个 Canonical Place 实体的内存视图。"""

    canonical_place_id: str
    canonical_name: str
    normalized_name: str
    city: str
    category: str
    kind: str = "place"
    parent_place_id: str | None = None
    district: str | None = None
    business_area: str | None = None
    lng: float | None = None
    lat: float | None = None
    address: str | None = None
    opening_hours: str | None = None
    confidence: float = 0.0
    evidence_count: int = 0
    #: 首次出现的次序（取所有引用的最小 order）。截断"用户可见候选"时按它排 ——
    #: 攻略提到过的词排在关键词兜底之前，这一层顺序不能丢。
    order: int = 0
    #: Provider 引用：[(provider, provider_place_id, PoiRecord)]。第一个是主引用。
    refs: list[PoiRecord] = field(default_factory=list)
    #: 归属这个实体的名字（含被收敛进来的子设施名）。
    aliases: list[str] = field(default_factory=list)
    is_new: bool = False

    @property
    def coords(self) -> tuple[float, float] | None:
        if self.lat is None or self.lng is None:
            return None
        return (self.lat, self.lng)

    @property
    def primary_ref(self) -> PoiRecord | None:
        return self.refs[0] if self.refs else None

    def to_place(self) -> Place:
        """还原成下游使用的 `Place`（place_id 用主引用的 Provider ID）。

        为什么 place_id 仍是高德 ID：run 级 `places` / `place_evidence` 是围绕它建链的，
        改成一个 TravelPlan 内部 ID 会让历史证据链、前端契约一起断。Canonical 身份通过
        `city_cache` 的 payload 与 Resolver 留痕对外可见。
        """

        primary = self.primary_ref
        aliases = [name for name in self.aliases if name and name != self.canonical_name]
        return Place(
            place_id=(primary.provider_place_id if primary else self.canonical_place_id),
            name=self.canonical_name,
            normalized_name=self.normalized_name,
            aliases=sorted(set(aliases)),
            type=(primary.amap_type if primary else ""),
            lat=self.lat,
            lng=self.lng,
            address=self.address,
            city=self.city,
            district=self.district,
            business_area=self.business_area,
            opening_hours=self.opening_hours,
            amap_verified=bool(primary and primary.provider_place_id),
            merged_from=sorted(
                ref.provider_place_id
                for ref in self.refs[1:]
                if ref.provider_place_id
            ),
        )

    def to_row(self, timestamp: str) -> dict[str, Any]:
        return {
            "canonical_place_id": self.canonical_place_id,
            "canonical_name": self.canonical_name,
            "normalized_name": self.normalized_name,
            "city": self.city,
            "district": self.district,
            "business_area": self.business_area,
            "lng": self.lng,
            "lat": self.lat,
            "address": self.address,
            "opening_hours": self.opening_hours,
            "category": self.category,
            "kind": self.kind,
            "parent_place_id": self.parent_place_id,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "updated_at": timestamp,
        }


# ======================================================================
# 3. 合并判定（方案 §9 / §11）
# ======================================================================

#: 坐标"中信号"半径：名字不是完全一样，但同一个商圈内 + 地址一致时可以合并。
GEO_MEDIUM_METERS = 500.0
#: 中信号需要的名字相似度下限（比强信号低，但必须配地址或商圈证据）。
NAME_SIMILARITY_MED = 0.45


def identity_conflict(left: PoiRecord | Place, right: PoiRecord | Place) -> dict[str, Any]:
    """两条记录的硬冲突，供实体层及 run 内去重的所有信号/合簇路径共用。"""

    if left.city and right.city and not city_related(left.city, right.city):
        return {"reason": "different_city"}
    if branch_conflict(left.name, right.name):
        return {
            "reason": "branch_conflict",
            "left": branch_signature(left.name),
            "right": branch_signature(right.name),
        }
    distance = planner.haversine_meters(left.coords, right.coords)
    # 缺坐标不等于同地点。明确跨区且没有近距离佐证时，名字/地址弱信号不能合并。
    if left.district and right.district and city_key(left.district) != city_key(right.district):
        if distance is None or distance > planner.GEO_DUPLICATE_METERS:
            return {"reason": "different_district", "left": left.district, "right": right.district}
    if distance is not None and distance > planner.GEO_NAME_CONFLICT_METERS:
        same_key = place_key(left.name, left.city) == place_key(right.name, right.city)
        return {
            "reason": "same_name_far_apart" if same_key else "far_apart",
            "distance_meters": round(distance, 1),
        }
    return {}


def merge_signal(left: PoiRecord, right: PoiRecord) -> tuple[str | None, dict[str, Any]]:
    """判断两条 POI 记录是不是同一个现实地点；返回 (信号码, 依据)；None = 不合并。

    信号强弱与方案 §9 对齐：

    强信号（高置信度合并）：
      * 两个 Provider ID 指向同一个实体（由调用方先行处理）
      * 名字键完全相同，且坐标没有明确冲突
      * 名字高度相似 + 坐标 150m 内

    中信号（可以合并）：
      * 名字相似 + 地址一致 + 坐标不冲突
      * 名字相似 + 坐标 500m 内 + 行政区 / 商圈一致

    冲突（**绝不合并**）：
      * 城市不同、坐标相距 1.5km 以上
      * 连锁品牌的不同分店（名字像也不行）
    """

    if not same_city(left.city, right.city) and not city_related(left.city, right.city):
        return None, {"reason": "different_city"}
    conflict = identity_conflict(left, right)
    if conflict:
        return None, conflict
    # 子设施与母体**不是**同一个实体（方案 §10）：它们靠 place_relations 关联，
    # 不靠合并。这里只要有一边是设施，就不走"合并成一个"的路。
    left_facility, right_facility = left.facility, right.facility
    if left_facility and right_facility:
        if left.name_key == right.name_key:
            return "same_facility_name", {"facility": left_facility}
        return None, {"reason": "both_facilities"}
    if left_facility or right_facility:
        return None, {"reason": "facility_vs_place"}

    distance = planner.haversine_meters(left.coords, right.coords)
    similarity = planner.name_similarity(left.name, right.name, city=left.city or right.city)
    same_key = bool(left.name_key) and left.name_key == right.name_key
    address_equal = _address_key(left.address) == _address_key(right.address) if (
        _address_key(left.address) and _address_key(right.address)
    ) else False

    if same_key:
        return "normalized_name", {
            "normalized_name": left.name_key,
            "distance_meters": None if distance is None else round(distance, 1),
        }

    if (
        distance is not None
        and distance <= planner.GEO_DUPLICATE_METERS
        and similarity >= planner.NAME_SIMILARITY_MIN
    ):
        return "geo_name", {"distance_meters": round(distance, 1), "name_similarity": similarity}

    # 简称与全称：「熊猫基地」不是「成都大熊猫繁育研究基地」的**连续子串**
    # （中间夹着"繁育研究"），所以字符串包含判不出来。方案 §9.2 的中等信号要的正是
    # 这一组：city 相同、category 同类、坐标 150m 内、名字中度相似。
    if (
        distance is not None
        and distance <= planner.GEO_DUPLICATE_METERS
        and similarity >= NAME_SIMILARITY_MED
        and _same_pool(left, right)
    ):
        return "geo_pool_name", {
            "distance_meters": round(distance, 1),
            "name_similarity": similarity,
            "category": left.amap_type or right.amap_type,
        }

    if distance is not None and distance <= planner.GEO_DUPLICATE_METERS and _contained_name(
        left, right
    ):
        return "name_containment", {
            "distance_meters": round(distance, 1),
            "name_similarity": similarity,
            "shorter": left.name_key if len(left.name_key) <= len(right.name_key) else right.name_key,
        }

    if address_equal and similarity >= 0.4:
        return "same_address", {"address": left.address, "name_similarity": similarity}

    if (
        distance is not None
        and distance <= GEO_MEDIUM_METERS
        and similarity >= NAME_SIMILARITY_MED
        and _area_equal(left, right)
    ):
        return "geo_area_name", {
            "distance_meters": round(distance, 1),
            "name_similarity": similarity,
            "district": left.district or right.district,
            "business_area": left.business_area or right.business_area,
        }
    return None, {}


def _contained_name(left: PoiRecord, right: PoiRecord) -> bool:
    """一条的名字键是另一条的真子串，且短的那个足够长（≥3 字）。

    长度护栏很重要：两字的通用词（"火锅""公园"）是半个城市所有 POI 的子串，放它进来
    会把一堆不相干的地点并成一个。
    """

    short, long = sorted((left.name_key, right.name_key), key=len)
    if len(short) < 3 or short == long:
        return False
    return short in long


def _same_pool(left: PoiRecord, right: PoiRecord) -> bool:
    """"同一类地方"：两边的分类都判得出来且落在同一个候选池。

    判不出来就不算证据 —— 只拿"都没分类"当作"同类"会让两个毫不相关的 POI 因为缺字段
    而互相合并（这仓库在关键词分类上已经栽过一次）。
    """

    left_category = category_from_type(left.amap_type)
    right_category = category_from_type(right.amap_type)
    if not left_category or not right_category:
        return False
    return POOL_BY_CATEGORY.get(left_category) == POOL_BY_CATEGORY.get(right_category)


def _area_equal(left: PoiRecord, right: PoiRecord) -> bool:
    for left_value, right_value in (
        (left.business_area, right.business_area),
        (left.district, right.district),
    ):
        a, b = basic_name_key(left_value or ""), basic_name_key(right_value or "")
        if a and b and a == b:
            return True
    return False


def _address_key(address: str | None) -> str:
    """地址键。复用 planner 的实现，避免两处规则漂移（同一份地址在两个模块里
    得到不同的键，会让"地址一致"这个信号在一个路径上生效、在另一个路径上失效）。"""

    return planner._address_key(address)  # noqa: SLF001 —— 刻意复用唯一实现


# ======================================================================
# 4. Resolver
# ======================================================================


@dataclass(slots=True)
class QueryResolution:
    """一个检索词的解析结果：能不能不打 Provider。"""

    term: str
    action: str  # alias_hit | query_cache_hit | miss
    places: list[Place] = field(default_factory=list)
    canonical_ids: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def hit(self) -> bool:
        return self.action != "miss"


@dataclass(slots=True)
class IngestOutcome:
    """一次结果收敛的产物。"""

    places: list[Place] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    folded: list[dict[str, Any]] = field(default_factory=list)
    created: int = 0
    merged: int = 0
    reused_refs: int = 0
    traces: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class PlaceResolver:
    """地点实体解析器：把 Provider 返回的一批 POI 收敛成 canonical 候选。

    生命周期与一次 Discovery / run 对齐：

        resolver = PlaceResolver(store, city="成都", run_id=...)
        plan = resolver.plan_queries(terms)          # A3：命中就别打 Provider
        outcome = resolver.ingest_results(results)   # 收敛 + 留痕
        resolver.flush()                             # 落库

    `store=None` 时退化成纯内存模式（跑得通、但没有跨会话复用），用于测试与
    "不该写库"的场景；此时 `flush()` 只返回统计，不写任何东西。
    """

    def __init__(
        self,
        store: TravelPlanStore | None = None,
        *,
        city: str,
        run_id: str | None = None,
        session_id: str | None = None,
        now: datetime | None = None,
        query_cache_ttl_days: int | None = None,
        persist: bool = True,
    ) -> None:
        self.store = store
        self.city = str(city or "")
        self.run_id = run_id
        self.session_id = session_id
        self.persist = bool(persist and store is not None)
        self._now = (now or utcnow()).astimezone(timezone.utc)
        self._query_cache_ttl_days = _query_cache_ttl(query_cache_ttl_days)
        self._loaded = False
        self.entities: dict[str, EntityRecord] = {}
        self._alias_index: dict[str, set[str]] = {}
        self._ref_index: dict[tuple[str, str], str] = {}
        self._query_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._parent_of: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._traces: list[dict[str, Any]] = []
        self._notes: list[str] = []
        #: 待写的查询缓存行（按归一化 query 去重，同一个词在一轮里只留最后一次结果）。
        self._pending_query_rows: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- 装载

    def load(self) -> "PlaceResolver":
        """把这座城市的实体层读进内存（一次连接、四张表）。

        为什么整城装载而不是逐条查：一次 Discovery 要对几十个高德 POI 做判定，
        逐条查库会是几十次往返（Neon 上一次往返就是几十毫秒），而一座城市的实体层
        本来就只有几百到几千行。
        """

        if self._loaded or self.store is None or not self.city:
            self._loaded = True
            return self
        for row in self.store.get_canonical_places(self.city):
            record = EntityRecord(
                canonical_place_id=str(row["canonical_place_id"]),
                canonical_name=str(row.get("canonical_name") or ""),
                normalized_name=str(row.get("normalized_name") or ""),
                city=str(row.get("city") or self.city),
                category=str(row.get("category") or "other"),
                kind=str(row.get("kind") or "place"),
                parent_place_id=row.get("parent_place_id"),
                district=row.get("district"),
                business_area=row.get("business_area"),
                lng=row.get("lng"),
                lat=row.get("lat"),
                address=row.get("address"),
                opening_hours=row.get("opening_hours"),
                confidence=float(row.get("confidence") or 0.0),
                evidence_count=int(row.get("evidence_count") or 0),
            )
            self.entities[record.canonical_place_id] = record
        for row in self.store.get_place_aliases(self.city):
            entity = self.entities.get(str(row["canonical_place_id"]))
            alias = str(row.get("alias") or row.get("normalized_alias") or "")
            if entity is not None and alias and alias != entity.canonical_name:
                if alias not in entity.aliases:
                    entity.aliases.append(alias)
        for row in self.store.get_place_provider_refs(self.city):
            ref = PoiRecord(
                provider=str(row.get("provider") or ""),
                provider_place_id=str(row.get("provider_place_id") or ""),
                name=str(row.get("provider_name") or ""),
                amap_type=str(row.get("provider_type") or ""),
                lat=row.get("lat"),
                lng=row.get("lng"),
                address=row.get("address"),
                city=self.city,
                district=row.get("district"),
                business_area=row.get("business_area"),
                opening_hours=row.get("opening_hours"),
            )
            canonical_id = str(row["canonical_place_id"])
            entity = self.entities.get(canonical_id)
            if entity is not None:
                entity.refs.append(ref)
                if ref.name and ref.name != entity.canonical_name and ref.name not in entity.aliases:
                    entity.aliases.append(ref.name)
            self._ref_index[(ref.provider, ref.provider_place_id)] = canonical_id
        self._rebuild_alias_index()
        # 主引用的顺序决定"复用回来的候选带哪个 Provider ID"，必须稳定：数据库不保证
        # SELECT 的顺序，不排序的话同一个实体在两次会话里会带着不同的 place_id 出现，
        # "同一个地点只有一个身份"就只活在文档里。名字与实体名一致的那条排最前。
        for entity in self.entities.values():
            entity.refs.sort(
                key=lambda ref: (
                    ref.name != entity.canonical_name,
                    ref.provider,
                    ref.provider_place_id,
                )
            )
        for row in self.store.get_place_relations(self.city):
            child = str(row.get("child_place_id") or "")
            if child:
                self._parent_of[child] = str(row.get("parent_place_id") or "")
        for row in self.store.get_poi_query_cache_rows(self.city):
            key = (str(row.get("query_type") or ""), str(row.get("normalized_query") or ""))
            if all(key):
                self._query_cache[key] = row
        self._loaded = True
        return self

    def _rebuild_alias_index(self) -> None:
        """DB 别名表只有单值；从全部实体/引用/别名恢复歧义，不能相信那一个赢家。"""

        self._alias_index.clear()
        for entity in self.entities.values():
            names = {entity.canonical_name, *entity.aliases, *(ref.name for ref in entity.refs)}
            for name in names:
                key = place_key(name, entity.city)
                if key and key not in planner.CATEGORY_TERMS:
                    self._alias_index.setdefault(key, set()).add(entity.canonical_place_id)

    # ---------------------------------------------------------------- A3：查询前

    def plan_queries(self, terms: Sequence[str]) -> tuple[list[str], list[QueryResolution]]:
        """把检索词分成"必须打 Provider"和"已有实体可以复用"两组。

        A3 / A4 的落点：**任何 search_poi 之前都必须先来这里问一次**。命中就意味着
        这次不产生高德调用 —— 而不是调完再靠缓存"省一次"。

        注意顺序：先 Alias（名字就是身份），再 Query Cache（名字不认识，但这个词以前查过）。
        Query Cache 只提供"历史候选"，候选仍要按 Provider 引用映射回实体，映射不到就当未命中
        （方案 §14：query 相同 ≠ 同一个地点）。
        """

        self.load()
        to_search: list[str] = []
        hits: list[QueryResolution] = []
        for term in terms:
            resolution = self.resolve_query(term)
            if resolution.hit:
                hits.append(resolution)
            else:
                to_search.append(term)
        return to_search, hits

    def resolve_query(self, term: str) -> QueryResolution:
        """单个检索词的解析：能复用就复用，复用不了返回 miss。"""

        self.load()
        cleaned = str(term or "").strip()
        if not cleaned:
            return QueryResolution(term=cleaned, action="miss", reason="空检索词")
        key = place_key(cleaned, self.city)
        self._rebuild_alias_index()
        alias_ids = self._alias_index.get(key, set())
        if len(alias_ids) > 1:
            return QueryResolution(term=cleaned, action="miss", reason="别名存在歧义，需重新检索候选")
        if len(alias_ids) == 1:
            entity = self.entities[next(iter(alias_ids))]
            places = self._places_of(entity)
            if places:
                return QueryResolution(
                    term=cleaned,
                    action="alias_hit",
                    places=places,
                    canonical_ids=[entity.canonical_place_id],
                    reason=f"别名索引命中「{entity.canonical_name}」",
                )
        cached = self._query_cache.get(("search_poi", key)) if key else None
        if cached and self._cache_fresh(cached):
            canonical_ids = _loads_list(cached.get("matched_place_ids_json"))
            places: list[Place] = []
            used: list[str] = []
            for canonical_id in canonical_ids:
                entity = self.entities.get(canonical_id)
                if entity is None:
                    continue
                places.extend(self._places_of(entity))
                used.append(canonical_id)
            if places:
                return QueryResolution(
                    term=cleaned,
                    action="query_cache_hit",
                    places=places,
                    canonical_ids=used,
                    reason=f"查询缓存命中（{len(places)} 个候选，原文 {str(cached.get('fetched_at') or '')[:19]}）",
                )
        return QueryResolution(term=cleaned, action="miss")

    def _places_of(self, entity: EntityRecord) -> list[Place]:
        """实体 → 可展示的 `Place` 列表（只给母体，不给子设施）。"""

        if entity.kind != "place":
            return []
        return [entity.to_place()]

    def _cache_fresh(self, row: Mapping[str, Any]) -> bool:
        expires_at = _parse_time(row.get("expires_at"))
        if expires_at is None:
            return True
        return expires_at >= self._now

    # ---------------------------------------------------------------- 结果收敛

    def ingest(
        self,
        records: Iterable[PoiRecord],
        *,
        source: str = "provider",
    ) -> IngestOutcome:
        """把一批 POI 记录收敛成 canonical 候选。

        做四件事，顺序不能换：
          1. **地理围栏**：不属于目的地城市的先剔掉（成都缓存里混进乐山大佛就是这么来的）；
          2. **Provider 引用复用**：见过的 `poi_id` 直接归位，不再做相似度判断；
          3. **聚类**：同一实体的多条记录并成一个，连锁分店 / 子设施各按自己的规则处理；
          4. **子设施收敛**：子设施不并列成候选，挂到母体上（方案 §10）。
        """

        self.load()
        outcome = IngestOutcome()
        accepted: list[PoiRecord] = []
        seen_ids: set[tuple[str, str]] = set()
        for record in records:
            if not record.provider_place_id and not record.name:
                continue
            if not self._in_city(record):
                outcome.dropped.append(
                    {
                        "place_id": record.provider_place_id,
                        "name": record.name,
                        "reason": "out_of_city",
                        "detail": f"Provider 报的城市是「{record.city or '未知'}」，不是目的地「{self.city}」",
                    }
                )
                continue
            # 同一条 Provider 记录在一批里出现两次（同一个词被两个并发分支各返回一遍）
            # 只该留一条：同一个 `provider_place_id` 写两行会让 `place_provider_refs`
            # 的主键在同一条语句里被命中两次，Postgres 会直接报错。
            identity = (record.provider, record.provider_place_id or record.name)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
            not_destination = non_destination_reason(record.amap_type)
            if not_destination:
                outcome.dropped.append(
                    {
                        "place_id": record.provider_place_id,
                        "name": record.name,
                        "reason": "not_destination",
                        "detail": not_destination,
                    }
                )
                continue
            accepted.append(record)

        # 2) Provider 引用复用：命中已知引用就直接归位（**不做任何相似度判断**）。
        unresolved: list[PoiRecord] = []
        for record in accepted:
            canonical_id = self._ref_index.get((record.provider, record.provider_place_id))
            if canonical_id and canonical_id in self.entities:
                entity = self.entities[canonical_id]
                self._attach_ref(entity, record)
                outcome.reused_refs += 1
                continue
            unresolved.append(record)

        # 3) 聚类：union-find，但每次合并都要过 `merge_signal` 的强 / 中 / 冲突判断。
        clusters = self._cluster(unresolved)
        for members in clusters:
            self._absorb(members, outcome)

        # 4) 收尾：子设施挂母体 + 组装用户可见候选。
        self._link_facilities(outcome)
        self._rebuild_alias_index()
        outcome.places = self._visible_places()
        if outcome.reused_refs:
            outcome.notes.append(
                f"{outcome.reused_refs} 个 POI 命中已有 Provider 引用（未做重复判断）"
            )
        return outcome

    def _in_city(self, record: PoiRecord) -> bool:
        """地理围栏：Provider 明确报了别处才拒收（判定规则见 `place_in_city`）。"""

        return place_in_city(record.city, record.district, self.city)

    def _cluster(self, records: list[PoiRecord]) -> list[list[PoiRecord]]:
        """按合并信号聚类。宁可多留几簇，也不要把两个真地点并成一个。"""

        count = len(records)
        parent = list(range(count))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        groups = {index: [index] for index in range(count)}

        def union(left: int, right: int) -> bool:
            root_left, root_right = find(left), find(right)
            if any(
                identity_conflict(records[a], records[b])
                for a in groups[root_left] for b in groups[root_right]
            ):
                return False
            parent[root_right] = root_left
            groups[root_left].extend(groups.pop(root_right))
            return True

        for i in range(count):
            for j in range(i + 1, count):
                if find(i) == find(j):
                    continue
                signal, detail = merge_signal(records[i], records[j])
                if signal and union(i, j):
                    self._trace(
                        operation="merge",
                        action="merged",
                        query=records[i].source_query,
                        canonical_place_id=None,
                        confidence=None,
                        reason=f"{records[i].name} ↔ {records[j].name}（{signal}）",
                        signals=detail,
                    )

        clusters: dict[int, list[PoiRecord]] = {}
        for index in range(count):
            clusters.setdefault(find(index), []).append(records[index])
        # 保持输入顺序：同一批结果在不同并发下顺序不同，按顺序输出才能让"哪个当母体"稳定。
        return sorted(clusters.values(), key=lambda members: min(item.order for item in members))

    def _matching_entity(self, members: list[PoiRecord]) -> EntityRecord | None:
        """新 Provider ID 也要与旧实体比对；整个新簇不能与旧实体的任一证据冲突。"""

        matches: list[EntityRecord] = []
        for entity in self.entities.values():
            if entity.kind != "place":
                continue
            canonical = PoiRecord.from_place(entity.to_place())
            evidence = [canonical, *entity.refs]
            # 别名提供名字信号，不提供独立坐标；设施别名不作为同一实体的证据。
            for alias in entity.aliases:
                if not facility_kind(alias):
                    evidence.append(PoiRecord(
                        provider="", provider_place_id="", name=alias,
                        amap_type=canonical.amap_type, city=entity.city,
                        lat=entity.lat, lng=entity.lng, address=entity.address,
                        district=entity.district, business_area=entity.business_area,
                    ))
            if any(identity_conflict(item, old) for item in members for old in evidence):
                continue
            if any(merge_signal(item, old)[0] for item in members for old in evidence):
                matches.append(entity)
        # 多个旧身份都可能命中时，不能任意选一个，也不能顺手把它们合并。
        return matches[0] if len(matches) == 1 else None

    def _absorb(self, members: list[PoiRecord], outcome: IngestOutcome) -> None:
        """一簇 POI → 一个（或两个）实体。

        簇里全是设施时（例如只搜到了"熊猫基地南门"与"熊猫基地游客中心"），**不**合成一个
        地点，而是各自建实体再挂到母体上 —— 母体可能这次没被搜到，那就等真正的母体出现。
        """

        places = [item for item in members if not item.is_facility]
        if not places:
            for item in members:
                self._create_entity(item, outcome, kind="facility")
            return
        entity = self._matching_entity(places)
        representative = None
        if entity is None:
            representative = _pick_representative(places)
            entity = self._create_entity(representative, outcome, kind="place")
        for item in places:
            if item is representative:
                continue
            self._attach_ref(entity, item)
            entity.confidence = min(1.0, entity.confidence + 0.05)
            outcome.merged += 1
            self._trace(
                operation="merge",
                action="merged",
                query=item.source_query,
                canonical_place_id=entity.canonical_place_id,
                confidence=entity.confidence,
                reason=f"「{item.name}」并入「{entity.canonical_name}」（多信号一致且整簇无冲突）",
                signals={"ref": item.provider_place_id},
            )
        for item in members:
            if item.is_facility:
                self._create_entity(item, outcome, kind="facility", parent_hint=entity)

    def _create_entity(
        self,
        record: PoiRecord,
        outcome: IngestOutcome,
        *,
        kind: str,
        parent_hint: EntityRecord | None = None,
    ) -> EntityRecord:
        """建（或取回）一个实体，并把这条 Provider 引用挂上去。

        已有同 ID 的实体直接返回：`canonical_id_for` 是确定性的，同一地点重复出现要落在
        同一行，而不是每次新建。
        """

        canonical_id = canonical_id_for(self.city, record.name, record.lat, record.lng)
        entity = self.entities.get(canonical_id)
        if entity is not None and any(
            identity_conflict(record, previous)
            for previous in [PoiRecord.from_place(entity.to_place()), *entity.refs]
        ):
            # 旧键只含城市/名称/坐标，两个缺坐标的同名跨区记录会碰撞；不能让取键绕过合并护栏。
            discriminator = "\x00".join((record.provider, record.provider_place_id,
                                         record.district or "", record.address or ""))
            canonical_id += "_" + hashlib.sha1(discriminator.encode("utf-8")).hexdigest()[:10]
            entity = self.entities.get(canonical_id)
        if entity is None:
            entity = EntityRecord(
                canonical_place_id=canonical_id,
                canonical_name=record.name,
                normalized_name=record.name_key,
                city=self.city,
                category=category_from_type(record.amap_type) or "other",
                kind=kind,
                parent_place_id=(parent_hint.canonical_place_id if parent_hint else None),
                district=record.district,
                business_area=record.business_area,
                lng=record.lng,
                lat=record.lat,
                address=record.address,
                opening_hours=record.opening_hours,
                confidence=0.6 if kind == "place" else 0.5,
                order=record.order,
                is_new=True,
            )
            self.entities[canonical_id] = entity
            outcome.created += 1
            self._dirty.add(canonical_id)
            self._trace(
                operation="resolve",
                action="created",
                query=record.source_query,
                canonical_place_id=canonical_id,
                confidence=entity.confidence,
                reason=f"新建实体「{record.name}」（{kind}）",
                signals={"provider_place_id": record.provider_place_id, "type": record.amap_type},
            )
        elif kind == "place" and entity.kind != "place":
            # 母体迟到：先前按设施建的实体现在拿到了真正的母体身份。
            entity.kind = "place"
            entity.parent_place_id = None
            self._dirty.add(canonical_id)
        self._attach_ref(entity, record)
        if parent_hint is not None and kind == "facility" and not entity.parent_place_id:
            entity.parent_place_id = parent_hint.canonical_place_id
            self._dirty.add(canonical_id)
        return entity

    def _attach_ref(self, entity: EntityRecord, record: PoiRecord) -> None:
        entity.order = record.order if not entity.refs else min(entity.order, record.order)
        identity = (record.provider, record.provider_place_id or record.name)
        for index, previous in enumerate(entity.refs):
            if (previous.provider, previous.provider_place_id or previous.name) != identity:
                continue
            if previous.name and previous.name != entity.canonical_name and previous.name not in entity.aliases:
                entity.aliases.append(previous.name)
            # 保留主引用位置，刷新已有字段且不让缺失字段覆盖旧详情。
            updates = {
                key: getattr(record, key) if getattr(record, key) not in (None, "") else getattr(previous, key)
                for key in (
                    "name", "amap_type", "lat", "lng", "address", "city", "district",
                    "business_area", "opening_hours", "source_query",
                )
            }
            record = replace(record, **updates, order=min(previous.order, record.order))
            entity.refs[index] = record
            break
        else:
            entity.refs.append(record)
        if record.provider_place_id:
            self._ref_index[(record.provider, record.provider_place_id)] = entity.canonical_place_id
        self._dirty.add(entity.canonical_place_id)
        # 字段缺失时用新记录补齐（母体先出现、详情后到的情况）。
        entity.lat = entity.lat if entity.lat is not None else record.lat
        entity.lng = entity.lng if entity.lng is not None else record.lng
        entity.address = entity.address or record.address
        entity.district = entity.district or record.district
        entity.business_area = entity.business_area or record.business_area
        entity.opening_hours = entity.opening_hours or record.opening_hours
        # 原始名字照留：即便名字键与代表点相同（"杜甫草堂景区" vs "成都杜甫草堂博物馆"，
        # 归一化后都是"杜甫草堂"），用户看到的仍是五花八门的写法，留着他才能认得出来。
        if record.name and record.name != entity.canonical_name and record.name not in entity.aliases:
            entity.aliases.append(record.name)

    def _link_facilities(self, outcome: IngestOutcome) -> None:
        """把设施实体挂到母体上；挂不上的按"不是目的地"处理。

        母体判定按两级：
          1. 同一批结果里已经有母体（例如"熊猫基地"与"熊猫基地南门"同时被搜到）；
          2. 名字主体命中已有实体的名字键 / 别名（"杜甫草堂(地铁站)" → 杜甫草堂）。
        """

        for entity in list(self.entities.values()):
            if entity.kind != "facility" or entity.parent_place_id:
                continue
            head, _suffix = split_suffix(entity.canonical_name)
            parent = self._find_parent(head, entity)
            if parent is None:
                continue
            entity.parent_place_id = parent.canonical_place_id
            self._parent_of[entity.canonical_place_id] = parent.canonical_place_id
            self._dirty.add(entity.canonical_place_id)
            self._dirty.add(parent.canonical_place_id)
            parent.aliases = sorted({*parent.aliases, entity.canonical_name})
            outcome.folded.append(
                {
                    "place_id": entity.canonical_place_id,
                    "name": entity.canonical_name,
                    "parent_place_id": parent.canonical_place_id,
                    "parent_name": parent.canonical_name,
                    "relation_type": _relation_type(entity),
                }
            )
            self._trace(
                operation="relation",
                action="folded",
                query=None,
                canonical_place_id=parent.canonical_place_id,
                parent_place_id=parent.canonical_place_id,
                confidence=entity.confidence,
                reason=f"子设施「{entity.canonical_name}」挂到母体「{parent.canonical_name}」",
                signals={"relation_type": _relation_type(entity)},
            )
        # 挂不上母体的设施不进候选（"熊猫基地游客中心"单独出现时不值得让用户勾选），
        # 但要如实记下来：静默消失比显示错了更难排查。
        for entity in list(self.entities.values()):
            if entity.kind != "facility" or entity.parent_place_id:
                continue
            if entity.canonical_place_id not in self._dirty:
                continue
            outcome.dropped.append(
                {
                    "place_id": (
                        entity.primary_ref.provider_place_id
                        if entity.primary_ref is not None
                        else entity.canonical_place_id
                    ),
                    "canonical_place_id": entity.canonical_place_id,
                    "name": entity.canonical_name,
                    "reason": "orphan_facility",
                    "detail": f"「{entity.canonical_name}」是附属设施且没找到母体，不作为可选地点",
                }
            )

    def _find_parent(self, head: str, entity: EntityRecord) -> EntityRecord | None:
        """找一个设施实体的母体。宁可不挂，也不挂错（方案 §10 的 parent-child）。

        母体判据分两档：
          * 主体名的名字键与母体**完全一致** —— 强信号，坐标缺失也认；
          * 名字中度相似 + 坐标 150m 内 —— "熊猫基地南门"的主体"熊猫基地"与
            "成都大熊猫繁育研究基地"就属于这一档（它不是连续子串，字符串包含判不出来）。

        坐标被判为 1.5km 之外时直接放弃：同名不同地时挂错母体比不挂更糟。
        """

        for candidate_head in _parent_head_candidates(head, entity.canonical_name):
            head_key = place_key(candidate_head, self.city)
            if not head_key:
                continue
            best: tuple[float, EntityRecord] | None = None
            for candidate in self.entities.values():
                if candidate.canonical_place_id == entity.canonical_place_id:
                    continue
                if candidate.kind != "place":
                    continue
                distance = planner.haversine_meters(entity.coords, candidate.coords)
                if distance is not None and distance > planner.GEO_NAME_CONFLICT_METERS:
                    continue
                if candidate.normalized_name == head_key:
                    return candidate
                similarity = planner.name_similarity(
                    candidate_head, candidate.canonical_name, city=self.city
                )
                if similarity < NAME_SIMILARITY_MED:
                    continue
                if distance is None or distance > planner.GEO_DUPLICATE_METERS:
                    continue
                # 相似度越高越优先，同分时取更近的那个。
                score = similarity - (distance / 100000.0)
                if best is None or score > best[0]:
                    best = (score, candidate)
            if best is not None:
                return best[1]
        return None

    def _visible_places(self) -> list[Place]:
        """用户可见候选：只有 `kind=place` 的实体，**按首次出现的先后**返回。

        顺序很重要：调用方会按这个顺序截断到"用户可见上限"，而截断口径是
        "攻略提到过的在前"。按置信度或名字排序会把攻略里真正提到的地点挤到后面，
        让一屏候选被泛关键词（"公园""博物馆"）的结果占满 —— 那是另一种形式的答非所问。
        """

        visible = [
            entity
            for entity in self.entities.values()
            if entity.kind == "place" and self._claimed(entity)
        ]
        visible.sort(key=lambda item: (item.order, item.canonical_name))
        return [entity.to_place() for entity in visible]

    def _claimed(self, entity: EntityRecord) -> bool:
        """这个实体这次是否真的有候选价值：要么见过 Provider 引用，要么本次新建 / 更新过。"""

        return bool(entity.refs) and entity.canonical_place_id in self._dirty

    # ---------------------------------------------------------------- 查询缓存

    def record_query(
        self,
        term: str,
        *,
        provider_result_ids: Sequence[str] = (),
        matched_place_ids: Sequence[str] = (),
        provider: str = PROVIDER_AMAP,
    ) -> None:
        """登记"这个词这次查到了什么"。

        `matched_place_ids` 存的是 **canonical id**，不是 Provider ID —— 缓存只回答
        "以前这个 query 查到过哪些实体"，用 Provider ID 会让缓存命中时又要做一次映射。
        """

        key = place_key(term, self.city)
        if not key:
            return
        entities = [
            self._ref_index.get((provider, provider_id))
            for provider_id in provider_result_ids
        ]
        resolved = sorted({item for item in [*entities, *matched_place_ids] if item})
        ttl_days = self._query_cache_ttl_days
        expires_at = (
            (self._now + timedelta(days=max(1, int(ttl_days)))).isoformat()
            if ttl_days
            else None
        )
        row = {
            "query_type": "search_poi",
            "normalized_query": key,
            "provider": provider,
            "matched_place_ids": resolved,
            "provider_result_ids": [str(item) for item in provider_result_ids if item],
            "result_count": len([item for item in provider_result_ids if item]),
            "hit_count": 0,
            "fetched_at": self._now.isoformat(),
            "expires_at": expires_at,
        }
        self._query_cache[("search_poi", key)] = {
            "query_type": "search_poi",
            "normalized_query": key,
            "matched_place_ids_json": _dump_json(resolved),
            "provider_result_ids_json": _dump_json(row["provider_result_ids"]),
            "result_count": row["result_count"],
            "fetched_at": row["fetched_at"],
            "expires_at": expires_at,
        }
        self._pending_query_rows[key] = row

    # ---------------------------------------------------------------- 明细复用

    def needs_detail(self, provider_place_id: str, *, provider: str = PROVIDER_AMAP) -> bool:
        """这个 POI 还需不需要单独查一次详情。

        A4 的"Cache First"：景点详情（营业时间 / 地址 / 商圈）在实体层已经有时就不该再打
        一次 Provider。缺地址或营业时间才算需要补。
        """

        self.load()
        canonical_id = self._ref_index.get((provider, provider_place_id))
        entity = self.entities.get(canonical_id) if canonical_id else None
        if entity is None:
            return True
        return not (entity.address and entity.opening_hours)

    # ---------------------------------------------------------------- 留痕

    def traces(self) -> list[dict[str, Any]]:
        """本次解析的留痕（方案 §21）：能看到"为什么没有重新查高德"。"""

        rows: list[dict[str, Any]] = []
        scope = self.run_id or self.session_id or "resolver"
        for index, trace in enumerate(self._traces[:MAX_TRACES_PER_INGEST]):
            rows.append(
                {
                    "trace_id": f"{scope}:{self.city}:{index}",
                    "run_id": self.run_id,
                    "session_id": self.session_id,
                    "city": self.city,
                    "created_at": self._now.isoformat(),
                    **trace,
                }
            )
        return rows

    def _trace(self, **trace: Any) -> None:
        if len(self._traces) >= MAX_TRACES_PER_INGEST:
            return
        self._traces.append(trace)

    def flush(self) -> dict[str, Any]:
        """把本次的变更写回实体层；返回一份统计摘要。

        `store=None` 时只返回统计（纯内存模式），不写任何东西 —— 调用方在这种情况下
        拿到的候选与写库时完全一致，只是没有跨会话复用。
        """

        summary: dict[str, Any] = {
            "city": self.city,
            "entities": len(self.entities),
            "dirty": len(self._dirty),
            "traces": len(self._traces),
            "persisted": False,
        }
        if not self.persist or self.store is None:
            return summary
        timestamp = self._now.isoformat()
        dirty = [self.entities[key] for key in sorted(self._dirty) if key in self.entities]
        if dirty:
            self.store.upsert_canonical_places([item.to_row(timestamp) for item in dirty])
            ref_rows: list[dict[str, Any]] = []
            alias_rows: list[dict[str, Any]] = []
            relation_rows: list[dict[str, Any]] = []
            for entity in dirty:
                for record in entity.refs:
                    ref_rows.append(
                        {
                            "provider": record.provider,
                            "provider_place_id": record.provider_place_id,
                            "canonical_place_id": entity.canonical_place_id,
                            "city": self.city,
                            "provider_name": record.name,
                            "provider_type": record.amap_type,
                            "lng": record.lng,
                            "lat": record.lat,
                            "address": record.address,
                            "district": record.district,
                            "business_area": record.business_area,
                            "opening_hours": record.opening_hours,
                            "fetched_at": timestamp,
                            "updated_at": timestamp,
                        }
                    )
                names = {entity.canonical_name, *entity.aliases}
                for name in names:
                    alias_key = place_key(name, self.city)
                    if not alias_key or alias_key in planner.CATEGORY_TERMS:
                        # 类别词（"景点""美食""步行街"）不能当别名：它说明"哪一类地方"，
                        # 拿它做索引会让"景点"这个词直接命中某一家。
                        continue
                    alias_rows.append(
                        {
                            "city": self.city,
                            "normalized_alias": alias_key,
                            "alias": name,
                            "canonical_place_id": entity.canonical_place_id,
                            "source": "derived",
                            "confidence": entity.confidence,
                            "updated_at": timestamp,
                        }
                    )
                if entity.parent_place_id:
                    relation_rows.append(
                        {
                            "parent_place_id": entity.parent_place_id,
                            "child_place_id": entity.canonical_place_id,
                            "relation_type": _relation_type(entity),
                            "city": self.city,
                            "confidence": entity.confidence,
                            "updated_at": timestamp,
                        }
                    )
            self.store.upsert_place_aliases(_unique_rows(alias_rows, ("city", "normalized_alias")))
            self.store.upsert_place_provider_refs(
                _unique_rows(ref_rows, ("provider", "provider_place_id"))
            )
            self.store.upsert_place_relations(
                _unique_rows(relation_rows, ("parent_place_id", "child_place_id", "relation_type"))
            )
            summary["refs"] = len(ref_rows)
            summary["aliases"] = len(alias_rows)
            summary["relations"] = len(relation_rows)
        pending = getattr(self, "_pending_query_rows", None) or {}
        if pending:
            self.store.upsert_poi_query_cache(self.city, list(pending.values()))
            summary["query_cache"] = len(pending)
        traces = self.traces()
        if traces:
            self.store.save_place_resolver_traces(traces)
        summary["persisted"] = True
        return summary

    # ---------------------------------------------------------------- A5：Evidence

    def alias_names_of(self, provider_place_id: str, *, provider: str = PROVIDER_AMAP) -> list[str]:
        """某个 Provider POI 所属实体的全部名字（含被收敛的子设施名）。

        A5 用：攻略里写"杜甫草堂"、高德那次返回的是"成都杜甫草堂博物馆"，只按字符串
        相等来匹配就是 0 命中 —— `city_poi_mentions` 恒为 0 正是这么来的。
        """

        self.load()
        canonical_id = self._ref_index.get((provider, provider_place_id))
        entity = self.entities.get(canonical_id) if canonical_id else None
        if entity is None:
            return []
        return [entity.canonical_name, *entity.aliases]


def _pick_representative(records: Sequence[PoiRecord]) -> PoiRecord:
    """选母体代表点：优先"字段更全 + 先出现"的那条。

    先出现优先与 Discovery 的截断口径一致（攻略提到过的在前），所以用户最可能看到的
    就是排在最前面的那条；字段齐全度放在前面是为了避免选到一个缺坐标 / 缺地址的记录
    当代表点（那会让下游的路线与营业时间校验全部退化）。
    """

    return min(
        records,
        key=lambda item: (
            0 if item.coords is not None else 1,
            0 if item.address else 1,
            item.order,
        ),
    )


def _relation_type(entity: EntityRecord) -> str:
    """设施 → 母体的关系类型（方案 §10 的 relation_type）。"""

    kind = facility_kind(entity.canonical_name)
    if kind == "entrance":
        return "entrance"
    _head, suffix = split_suffix(entity.canonical_name)
    if suffix:
        return "inside"
    return "child"


def site_matches(head_key: str, entity: EntityRecord) -> bool:
    """名字主体是否指向该实体（用于把子设施挂到母体上）。

    两个方向都认：
      * 主体名里含母体名 —— `熊猫基地` → `大熊猫繁育研究基地`（母体名是主体名的子串）；
      * 母体名里含主体名 —— 母体名比主体名长时的常见情形。

    长度护栏：短的一侧必须 ≥3 字（"公园""火锅"这种词能命中半座城市），否则宁可不挂。
    """

    keys = {entity.normalized_name, place_key(entity.canonical_name, entity.city)}
    keys.update(place_key(alias, entity.city) for alias in entity.aliases)
    keys.discard("")
    if head_key in keys:
        return True
    for key in keys:
        short, long = sorted((head_key, key), key=len)
        if len(short) >= 3 and short in long:
            return True
    return False


def _parent_head_candidates(head: str, full_name: str) -> list[str]:
    """把"设施名"还原成可能的母体名候选。

    `熊猫基地南门` 去掉设施词得到 `熊猫基地`；`成都大熊猫繁育研究基地迎迎停车场` 去掉
    `停车场` 得到 `成都大熊猫繁育研究基地迎迎`，再靠 `site_matches` 的子串关系对上母体。
    不剥设施词的话，"南门""游客中心"这些尾巴会让主体名永远对不上母体。
    """

    candidates: list[str] = []
    for text in (head, full_name):
        cleaned = unicodedata.normalize("NFKC", str(text or "")).strip()
        if not cleaned:
            continue
        candidates.append(cleaned)
        for token, _kind in _token_pairs():
            if token in cleaned:
                trimmed = cleaned.replace(token, "").strip()
                if len(trimmed) >= 2:
                    candidates.append(trimmed)
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    # 长的先试：`熊猫基地南门`→`熊猫基地` 之前先试原串，避免把"熊猫基地"误当母体名。
    return sorted(unique, key=len, reverse=True)


def _unique_rows(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """按复合键去重，保留第一条。

    为什么必须做：这些表的主键在**一条 executemany 语句内**被命中两次时，SQLite 只是
    忽略第二次，Postgres 会直接抛 `ON CONFLICT DO UPDATE command cannot affect row a
    second time`。同一批结果里出现同一个 `poi_id` 或同一个别名是正常现象（并发分支重叠、
    子设施名与别处撞名），所以去重要放在写库这一层，而不是指望上游永远不重复。
    """

    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        identity = tuple(row.get(field) for field in key_fields)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


# ======================================================================
# 5. 小工具
# ======================================================================


def _dump_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _query_cache_ttl(override: int | None) -> int:
    """查询缓存的 TTL（天）。

    默认跟城市 POI 缓存同一份（`CITY_CACHE_POI_TTL_DAYS`）：两者回答的是同一类半静态
    问题（"这个地方还在不在、在哪儿"），各自一套 TTL 只会让"POI 还有效但查询缓存已过期"
    这种无意义的中间态出现。配置层出问题时退回 15 天而不是关闭过期 —— 缓存永不过期
    意味着一个错的查询结果会被一直复用。
    """

    if override is not None:
        return max(1, int(override))
    try:
        from app.config import current_config

        return max(1, int(current_config().city_cache_poi_ttl_days))
    except Exception:  # noqa: BLE001 —— 配置层异常不该让解析器不可用
        return 15


def _loads_list(value: Any) -> list[str]:
    import json

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
