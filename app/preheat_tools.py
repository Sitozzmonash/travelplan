"""预热入库工具：把「城市知识库」的取数与写入包成 Agent 可调用的 Tool（P6 预热 agent 化）。

Agent 在预热里要做的三件事（搜攻略 / 查高德 / 入库）都从这里暴露。**批调度不在这里**：
跑哪些城、串并行、每天限量、跳过判定与退出码仍然归代码，见 `scripts/preheat_cities.py`。

为什么不能直接用 `app/travel_tools.py` 的工具 + 一个"把参数写进库"的工具
----------------------------------------------------------------------
`build_travel_tools` 的工具把结果**截断成给模型看的 JSON**（正文 240 字符、字段白名单）。
让模型把这些字段"抄回来"入库有两个硬伤：

1. **缓存里的正文会变成模型的复述**。`city_evidences` 存的是攻略正文，正式规划会复用它
   （抽取、信任分、行程理由）。模型只看到 240 字符时，抄回来的也必然更短、更失真。
2. 高德 POI 的 `place_id` / 坐标由模型转述，抄错一个字符就落一条**不存在的地点**进城市
   缓存 —— 而预热的产物是所有人共用的。

所以这里换一种分工，**决策归 Agent、内容一律来自 Provider**：

    ProviderHub（真实取数）
        →  RecordingHub：把原始对象录进 CityKnowledgeSink（正文、坐标一个字不改）
        →  Agent：只做"搜什么 / 留哪条 / 抽哪些地名 / 要不要补详情"的决策
        →  入库工具按 id 取回**原始对象**，收尾时一次提交

模型能编造的只剩"选谁"与"从攻略里抽出哪些地名"——这两件事本来就要它判断；正文、坐标、
POI 身份它一个字都改不了（不认识的 id 直接拒绝，并告诉它去用哪个工具重查）。

缓存结构怎么保证与实时 Discovery 一致
------------------------------------
提交（`CityKnowledgeSink.commit`）只走两条**已有**代码路径，没有一行新的写入逻辑：

    places.PlaceResolver.ingest / record_query / flush
        地理围栏、实体归一化、子设施并母体、连锁分店保护、查询缓存
    city_cache.write_candidates
        city_pois / city_poi_mentions / city_evidences / city_cache_meta（含提及匹配与 TTL 清理）

所以预热写出来的行与"真实用户踩出来"的行同结构、同口径，`city_cache.read_candidates`
的复用路径不需要任何改动。

提交只在 Agent 循环正常结束后发生：超时 / 抛错时这座城市**原样不动**，不会留半座城的脏数据。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from app import city_cache, places
from app.models import Evidence, Place, coerce_str
from app.travel_tools import build_travel_tools

#: 给模型看的正文预览长度（字符）。真正入库的正文是 Provider 返回的**全文**；
#: 预览只用来让模型判断"这篇值不值得留"与"里面提到了哪些地点"。
PREVIEW_CHARS = 600

#: 单次取数默认回几条 / 最多回几条（与 `travel_tools` 同量级：模型可以调小，但不能拉爆上下文）。
DEFAULT_TOP_N = 5
MAX_TOP_N = 10

#: 攻略来源。小红书 / 抖音走社媒 Provider，`web` 走联网搜索兜底。
PLATFORMS: tuple[str, ...] = ("xiaohongshu", "douyin", "web")


def _text(value: Any) -> str:
    return coerce_str(value).strip()


def _preview(value: Any) -> str:
    text = coerce_str(value).strip()
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _json(payload: dict[str, Any]) -> str:
    """工具返回值：JSON 文本（中文不转义，日志与前端直接可读）。"""
    return json.dumps(payload, ensure_ascii=False)


def split_mentions(raw: Any) -> list[str]:
    """把模型给的地点串拆成列表：中英文逗号 / 顿号 / 分号 / 竖线 / 斜杠 / 换行都算分隔符。

    收字符串而不是 `list[str]`：模型对"用顿号连起来的地名"最稳定，写成 JSON 数组时
    反而常出现 `["宽窄巷子, 武侯祠"]` 这种"一个元素里塞两个"的形状。这里按所有常见
    分隔符拆开并去重，两种写法都能落对。
    """

    text = coerce_str(raw)
    for sep in ("，", ",", "、", ";", "；", "|", "\n", "/"):
        text = text.replace(sep, "\x00")
    result: list[str] = []
    for item in text.split("\x00"):
        cleaned = item.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def clamp_top_n(value: Any) -> int:
    """把模型给的 top_n 收敛到 [1, MAX_TOP_N]（写错参数不该中断整轮预热）。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TOP_N
    if number <= 0:
        return DEFAULT_TOP_N
    return min(number, MAX_TOP_N)


# ======================================================================
# 缓冲：Agent 选中的东西先攒着，收尾一次提交
# ======================================================================


class CityKnowledgeSink:
    """一座城市这一轮的「已取数 + 已选择」缓冲。

    三份数据分开存，因为它们回答的是不同的问题：

    * `_stashed_*`：Provider 真的返回过什么（按短 id 存**原始对象**）—— 入库工具只认这里，
      所以模型编不出不存在的地点与正文；
    * `_evidences` / `_records`：Agent 选中要入库的条目（它的决策）；
    * `_searches`：真正发起过的检索词（写进 `city_cache_meta`，下次同词不再重搜）。
    """

    def __init__(self, city: str, *, store: Any, run_id: str) -> None:
        self.city = city_cache.normalize_city(city)
        self.store = store
        #: 实体解析器：与实时 Discovery 是同一个类、同一份落库路径（`persist=False` 的
        #: 纯内存模式只发生在 `store=None` 的测试里）。
        self.resolver = places.PlaceResolver(store, city=self.city, run_id=run_id)
        self.counters: dict[str, int] = {
            "searches": 0,
            "stashed_evidences": 0,
            "stashed_places": 0,
            "detail_calls": 0,
            "saved_evidences": 0,
            "saved_pois": 0,
            "rejected_pois": 0,
        }
        self._stashed_evidences: dict[str, Evidence] = {}
        #: 对象身份 → 短 id。用 `id(evidence)` 而不是 `evidence.id`：Provider 的 id 在同一次
        #: 响应内唯一，但**同一个 id 在不同调用里可能重复出现**（分页、重试、假件都是这样），
        #: 按它去重会让"第二次搜到的另一条"顶掉第一次那条。对象身份不会：库里存着强引用，
        #: 地址不会被回收再分配。
        self._stash_id_of: dict[int, str] = {}
        self._stashed_places: dict[str, Place] = {}
        self._stashed_by_name: dict[str, str] = {}
        self._searches: list[str] = []
        self._evidences: list[Evidence] = []
        self._evidence_keys: set[str] = set()
        self._records: list[places.PoiRecord] = []
        self._saved_ids: set[str] = set()
        self._queries: dict[str, list[str]] = {}
        self._notes: list[str] = []
        self._rejected: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 取数暂存

    def note(self, text: str) -> None:
        """记一条"这次发生了什么"（收尾随摘要交给调用方，不编、不吞）。"""

        if text and text not in self._notes:
            self._notes.append(text)

    def record_search(self, platform: str, keyword: str) -> None:
        """登记一次**真正发起**的检索（无论成败），供 `city_cache_meta` 复用。"""

        cleaned = _text(keyword)
        self.counters["searches"] += 1
        if cleaned and cleaned not in self._searches:
            self._searches.append(cleaned)
        if platform == "web" and cleaned:
            self.note(f"攻略来源：网页搜索「{cleaned}」")
        elif cleaned:
            self.note(f"攻略来源：{platform}「{cleaned}」")

    def stash_evidence(self, evidence: Evidence, *, platform: str = "") -> str:
        """把一条证据按**短 id** 存起来，返回给模型看的 id（形如 `g1`）。

        短 id 是有意的：Provider 侧的 id 是 `src-xxxx-e3` 这种长串，模型转述时容易抄错，
        而抄错在入库侧的后果是"这条不要了"——白搜一次。
        """

        key = id(evidence)
        existing = self._stash_id_of.get(key)
        if existing:
            return existing
        short = f"g{len(self._stashed_evidences) + 1}"
        self._stashed_evidences[short] = evidence
        self._stash_id_of[key] = short
        self.counters["stashed_evidences"] = len(self._stashed_evidences)
        return short

    def stash_place(self, place: Place, *, query: str = "") -> str:
        """把一条高德 POI 存起来（按 place_id，并留一份名字索引给模型偷懒用）。"""

        key = str(place.place_id or place.name)
        if key not in self._stashed_places:
            self._stashed_places[key] = place
            self.counters["stashed_places"] = len(self._stashed_places)
        name_key = places.place_key(place.name, self.city)
        if name_key:
            self._stashed_by_name.setdefault(name_key, key)
        if query:
            self._queries.setdefault(_text(query), [])
        return key

    def enrich_place(self, poi_id: str, detail: dict[str, Any]) -> bool:
        """用 `poi_detail` 的返回补全暂存的 POI（只补缺，不覆盖已有字段）。

        复用 Discovery 的 `_apply_poi_detail`：详情"补什么、不覆盖什么"的口径只有一份，
        这里再写一遍迟早会与实时路径不一致。
        """

        place = self._stashed_places.get(_text(poi_id))
        if place is None or not isinstance(detail, dict) or not detail:
            return False
        # 私有导入是有意的：详情"补什么、不覆盖什么"的口径只能有一份（同仓复用优于复制）。
        from app.discovery import _apply_poi_detail

        self.counters["detail_calls"] += 1
        _apply_poi_detail(place, detail)
        return True

    def evidence_of(self, short_id: str) -> Evidence | None:
        return self._stashed_evidences.get(_text(short_id))

    def place_of(self, reference: str) -> Place | None:
        """按 `place_id` 或 `name` 取回暂存的 POI（两个键都认，模型少抄一次 id）。"""

        cleaned = _text(reference)
        place = self._stashed_places.get(cleaned)
        if place is not None:
            return place
        key = self._stashed_by_name.get(places.place_key(cleaned, self.city))
        return self._stashed_places.get(key) if key else None

    # ---------------------------------------------------------------- Agent 的入库决策

    def add_evidence(self, short_id: str, place_mentions: Any = "") -> dict[str, Any]:
        """登记一条攻略正文（正文按 id 取回原文，模型不参与转述）。"""

        evidence = self.evidence_of(short_id)
        if evidence is None:
            return {
                "status": "UNKNOWN_ID",
                "evidence_id": _text(short_id),
                "hint": "这个 id 不是本轮 search_guides 返回过的条目。请用它返回的 evidence_id，"
                        "或重新调用 search_guides 取一次。",
            }
        for name in split_mentions(place_mentions):
            if name not in evidence.place_mentions:
                evidence.place_mentions.append(name)
        key = _evidence_identity(evidence)
        if key in self._evidence_keys:
            return {
                "status": "DUPLICATE",
                "evidence_id": short_id,
                "title": evidence.title,
                "total_evidences": len(self._evidences),
                "hint": "这一条（同来源同标题）已经登记过了，不用重复登记。",
            }
        self._evidence_keys.add(key)
        self._evidences.append(evidence)
        self.counters["saved_evidences"] = len(self._evidences)
        return {
            "status": "SAVED",
            "evidence_id": short_id,
            "title": evidence.title,
            "provider": evidence.provider,
            "source_type": evidence.source_type,
            "place_mentions": list(evidence.place_mentions),
            "chars": len(evidence.text or ""),
            "total_evidences": len(self._evidences),
        }

    def add_poi(self, reference: str, *, query: str = "") -> dict[str, Any]:
        """登记一个高德 POI：先过两道硬过滤给出**当场反馈**，收尾统一做实体归一化。"""

        place = self.place_of(reference)
        if place is None:
            return {
                "status": "UNKNOWN_ID",
                "place_id": _text(reference),
                "hint": "这不是本轮 search_poi 返回过的地点。地点必须来自 search_poi，"
                        "不要凭攻略正文编一个。",
            }
        reason = places.non_destination_reason(place.type)
        if reason is None and not places.place_in_city(place.city, place.district, self.city):
            reason = f"它属于「{place.city or place.district}」，不是目的地「{self.city}」"
        if reason:
            self.counters["rejected_pois"] += 1
            self._rejected.append({"name": place.name, "reason": reason})
            return {
                "status": "REJECTED",
                "place_id": place.place_id,
                "name": place.name,
                "reason": reason,
                "hint": "这个点不会进城市知识库，别再试它；换一个真正属于这座城市的地点。",
            }
        identity = str(place.place_id or place.name)
        if identity in self._saved_ids:
            return {
                "status": "DUPLICATE",
                "place_id": place.place_id,
                "name": place.name,
                "total_pois": len(self._records),
                "hint": "这个点已经登记过了，不用重复登记。",
            }
        self._saved_ids.add(identity)
        self._records.append(
            places.PoiRecord.from_place(
                place, source_query=_text(query), order=len(self._records)
            )
        )
        if _text(query):
            self._queries.setdefault(_text(query), []).append(str(place.place_id or ""))
        self.counters["saved_pois"] = len(self._records)
        return {
            "status": "SAVED",
            "place_id": place.place_id,
            "name": place.name,
            "total_pois": len(self._records),
            "note": "收尾时统一做实体归一化后写入城市知识库",
        }

    # ---------------------------------------------------------------- 提交

    def commit(self) -> dict[str, Any]:
        """把 Agent 选中的东西写进城市缓存；一条 POI 都没有时**什么都不写**。

        顺序不能换：`resolver.flush()` 先落实体层（canonical / provider refs / 别名 / 查询缓存），
        `city_cache.write_candidates` 才能把"攻略提及"挂到 canonical 实体上
        （`upsert_place_evidence_links` 反查 `place_provider_refs`，refs 还没落库时那条链是空的）。
        这也是实时 Discovery 的顺序（`extract_place_candidates` 先 flush，会话再写缓存）。
        """

        result: dict[str, Any] = {
            "places": 0,
            "evidences": len(self._evidences),
            "social_evidences": sum(1 for item in self._evidences if _is_social(item)),
            "queries": list(self._searches),
            "notes": list(self._notes),
            "dropped": [],
            "folded": 0,
            "rejected": list(self._rejected),
            "committed": False,
        }
        if not self._records:
            result["notes"].append(
                "本轮没有任何通过校验的高德 POI，未写入城市缓存"
                "（没有 POI 的城市不算缓存命中，写了也只占空间）"
            )
            return result

        outcome = self.resolver.ingest(self._records)
        # ID 映射由 ingest 建立，必须在它之后登记查询缓存，否则首次查询永远缓存空列表。
        for term, provider_ids in self._queries.items():
            self.resolver.record_query(
                term, provider_result_ids=[item for item in provider_ids if item]
            )
        resolver_summary = self.resolver.flush()
        visible = list(outcome.places)
        written = city_cache.write_candidates(
            self.city,
            visible,
            self._evidences,
            store=self.store,
            social_queries=self._searches,
            social_served_queries=self._searches,
        )
        result.update(
            places=written,
            committed=True,
            dropped=list(outcome.dropped),
            folded=len(list(getattr(outcome, "folded", []) or [])),
            entities=int(resolver_summary.get("entities") or 0),
        )
        if outcome.dropped:
            out_of_city = [item for item in outcome.dropped if item.get("reason") == "out_of_city"]
            if out_of_city:
                result["notes"].append(
                    f"地理围栏剔除 {len(out_of_city)} 条不属于「{self.city}」的 POI："
                    + "、".join(str(item.get("name")) for item in out_of_city[:5])
                )
        if result["folded"]:
            result["notes"].append(
                f"{result['folded']} 个子设施在实体层并到了母体上（不并列成候选）"
            )
        if outcome.merged:
            result["notes"].append(f"实体层合并了 {outcome.merged} 条同实体的重复记录")
        # 检索词写进 meta 的口径：Agent 路径下"计划要搜的"与"真的搜过的"是同一份 ——
        # 它没有再提前规划一份检索词清单，这几条就是它真实发起过的调用。
        return result


def _evidence_identity(evidence: Evidence) -> str:
    """同一篇攻略在一轮里的去重键（与 `city_cache._evidence_key` 同思路，但只用于内存去重）。

    有 URL 用 `url+title`（同一篇被两个关键词搜到只留一条）；没有 URL 用 provider+标题+正文
    （标题相同、正文不同的两条不能撞键）。真正的库级主键仍由 `city_cache` 自己算。
    """

    url = " ".join(str(evidence.source_url or "").split())
    title = " ".join(str(evidence.title or "").split())
    if url:
        return f"url\x00{url}\x00{title}"
    return "\x00".join(["text", str(evidence.provider or ""), title, evidence.text or ""])


def _is_social(evidence: Evidence) -> bool:
    """是不是社媒证据。**复用** `sessions.is_social_evidence` 的口径，不另立一份判断。"""

    from app.sessions import is_social_evidence

    return is_social_evidence(evidence)


# ======================================================================
# 录制代理：Provider 返回的原始对象在被 Agent "看见"之前先留一份
# ======================================================================


class RecordingHub:
    """`ProviderHub` 的录制代理：拦下攻略 / 高德这几类取数，把原始结果录进 sink，其余透传。

    为什么在 Hub 这一层拦而不是在工具里录：工具交给模型的是截断后的 JSON，正文到模型手上
    已经短了一截；代理录的是**截断之前**的原始对象，缓存里存的因此永远是抓到的原文。
    透传（`__getattr__`）保证 `build_travel_tools(recorder)` 造出来的工具行为与真实 Hub
    完全一致 —— 只是结果多留了一份给入库用。
    """

    def __init__(self, hub: Any, sink: CityKnowledgeSink) -> None:
        self._hub = hub
        self._sink = sink

    # --- 攻略 ---

    def search_xiaohongshu(self, keyword: str, **kwargs: Any) -> Any:
        return self._record_social(
            self._hub.search_xiaohongshu(keyword, **kwargs), keyword, platform="xiaohongshu"
        )

    def search_douyin(self, keyword: str, **kwargs: Any) -> Any:
        return self._record_social(
            self._hub.search_douyin(keyword, **kwargs), keyword, platform="douyin"
        )

    def _record_social(self, result: Any, keyword: str, *, platform: str) -> Any:
        self._sink.record_search(platform, keyword)
        for item in list(getattr(result, "items", None) or []):
            if isinstance(item, Evidence):
                self._sink.stash_evidence(item, platform=platform)
        return result

    def web_search(self, query: str, **kwargs: Any) -> Any:
        """网页搜索：Hub 的 items 是原始 dict（title / snippet / url），这里按实时路径的同一
        映射（`discovery.discover_social_evidence`）变成 Evidence 再录 —— 缓存结构因此一致。"""

        result = self._hub.web_search(query, **kwargs)
        self._sink.record_search("web", query)
        for index, item in enumerate(list(getattr(result, "items", None) or [])):
            if not isinstance(item, dict):
                continue
            text = _text(item.get("snippet")) or _text(item.get("content")) or _text(item.get("title"))
            if not text:
                continue
            self._sink.stash_evidence(
                Evidence.from_content(
                    {
                        "title": item.get("title"),
                        "text": text,
                        "url": item.get("url"),
                    },
                    evidence_id=f"web-{index}",
                    source_type="web",
                    provider="tavily",
                    source_url=_text(item.get("url")) or None,
                ),
                platform="web",
            )
        return result

    # --- 高德 ---

    def search_poi(self, keywords: str, region: str, **kwargs: Any) -> Any:
        result = self._hub.search_poi(keywords, region, **kwargs)
        for item in list(getattr(result, "items", None) or []):
            if isinstance(item, Place):
                self._sink.stash_place(item, query=keywords)
        return result

    def poi_detail(self, poi_id: str, **kwargs: Any) -> Any:
        result = self._hub.poi_detail(poi_id, **kwargs)
        items = list(getattr(result, "items", None) or [])
        if items and isinstance(items[0], dict):
            self._sink.enrich_place(poi_id, items[0])
        return result

    # --- 其余一律透传 ---

    def __getattr__(self, name: str) -> Any:
        return getattr(self._hub, name)


# ======================================================================
# 工具工厂
# ======================================================================


def build_preheat_tools(hub: Any, sink: CityKnowledgeSink) -> list[Any]:
    """给预热 Agent 的工具：搜攻略 / 查高德（含详情）/ 两个入库工具。

    刻意**不**把机酒火车门票工具带进来：城市知识库永远不存价格与时刻表（这是设计不变量），
    暴露它们只会多烧 token、并给模型制造"顺手记一笔价格"的机会。高德的 `search_poi` /
    `poi_detail` 直接复用 `build_travel_tools`（同一份截断与字段白名单），只是 Hub 换成了
    录制代理。
    """

    recorder = RecordingHub(hub, sink)

    @tool
    def search_guides(keyword: str, platform: str = "xiaohongshu", top_n: int = DEFAULT_TOP_N) -> str:
        """搜这座城市的攻略（小红书 / 抖音 / 网页），返回可入库的条目及其 id。

        什么时候用：要"怎么玩 / 什么好吃 / 值不值得去"这类真实游客经验时。先搜 `xiaohongshu`；
        它没结果或结果太少再搜 `douyin`；还缺客观信息（官网、公告、开放时间）时用 `web`。
        什么时候不用：需要坐标 / 营业时间这类结构化事实（用 search_poi、poi_detail）。

        参数：
            keyword: 搜索词，**必须带城市名**，如 "成都三日游" / "成都 美食 必吃" / "成都 避坑"。
            platform: "xiaohongshu"（默认）/ "douyin" / "web"。
            top_n: 最多返回几条，默认 5，上限 10。

        返回：JSON，items 每项含 evidence_id（入库时用它）/ 平台 / 标题 / 作者 / 发布时间 /
            url / 正文预览（已截断）。**正文全文由系统保留**，你不需要抄正文，只要用
            save_city_evidence 登记值得留的条目，并写出里面提到的地点名。
        """

        name = _text(platform).lower() or "xiaohongshu"
        if name not in PLATFORMS:
            return _json({
                "status": "INVALID",
                "error": f"platform 只能是 {'/'.join(PLATFORMS)}，收到 {platform!r}",
                "items": [],
            })
        try:
            if name == "xiaohongshu":
                result = recorder.search_xiaohongshu(keyword)
            elif name == "douyin":
                result = recorder.search_douyin(keyword)
            else:
                result = recorder.web_search(keyword, max_results=clamp_top_n(top_n))
        except Exception as exc:  # noqa: BLE001 —— 取数失败要变成可读的失败，不能中断整轮
            return _json({
                "status": "ERROR",
                "platform": name,
                "keyword": keyword,
                "items": [],
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "这是取数失败，不等于查无攻略。可以换关键词或换平台再试一次。",
            })

        evidences = [item for item in list(getattr(result, "items", None) or []) if isinstance(item, Evidence)]
        limit = clamp_top_n(top_n)
        items: list[dict[str, Any]] = []
        for evidence in evidences[:limit]:
            items.append(
                {
                    "evidence_id": sink.stash_evidence(evidence, platform=name),
                    "platform": name,
                    "provider": evidence.provider,
                    "title": evidence.title,
                    "author": evidence.author,
                    "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                    "url": evidence.source_url,
                    "place_mentions": list(evidence.place_mentions or []),
                    "text_preview": _preview(evidence.text),
                }
            )
        payload: dict[str, Any] = {
            "status": str(getattr(result, "status", "") or ""),
            "platform": name,
            "keyword": keyword,
            "total": len(evidences),
            "shown": len(items),
            "items": items,
        }
        error = getattr(result, "error", None)
        if error:
            payload["error"] = str(error)[:PREVIEW_CHARS]
        if not evidences:
            payload["hint"] = (
                "这次没有可用条目（请按 status 如实理解：EMPTY=平台没返回，UNAVAILABLE/ERROR=取数失败）。"
                "换关键词或换平台再试，不要编造攻略内容。"
            )
        return _json(payload)

    @tool
    def save_city_evidence(evidence_id: str, place_mentions: str = "") -> str:
        """把一条攻略 / 网页登记进这座城市的知识库（正文由系统按 id 取回原文）。

        什么时候用：`search_guides` 返回里**值得长期复用**的条目，逐条登记。攻略"值不值得留"
        由你判断（有具体地点与经验的最值得留）；不要为了凑数登记，也不要登记你没看过预览的条目。
        什么时候不用：只想要坐标 / 营业时间（那是 search_poi / poi_detail 的事）。

        参数：
            evidence_id: `search_guides` 返回的 items[].evidence_id（形如 g1 / g2）。必须用它；
                自己编的 id 会被拒绝（正文与来源不允许由你转述）。
            place_mentions: 这篇正文里提到的**地点名**，用逗号 / 顿号分隔，如
                "宽窄巷子、武侯祠、杜甫草堂"。原文怎么写就怎么写，不要改名、不要写"景点""美食"
                这类类别词、不要写城市名；正文里没有具体地点就留空。
                系统会把它与高德 POI 做归一化匹配，写对名字即可。

        返回：JSON，status=SAVED / DUPLICATE / UNKNOWN_ID。
        """

        return _json(sink.add_evidence(evidence_id, place_mentions))

    @tool
    def save_city_poi(place_id: str, query: str = "") -> str:
        """把一个高德 POI 登记进这座城市的知识库（收尾统一做实体归一化后落库）。

        什么时候用：`search_poi` 返回里真正属于这座城市、值得进候选池的地点（景点、街区、
        特色餐馆、博物馆…）。判断"值不值得进候选"由你做，但**只能登记 search_poi 返回过的点**。
        什么时候不用：附属设施（某景区南门、停车场）、车站 / 地址桩、别座城市的点 —— 它们会被
        剔除，登记也是白费。

        参数：
            place_id: `search_poi` 返回的 place_id；也认它返回的 name，两个都能对上。
            query: 查到它的那个搜索词（写进城市级查询缓存，下次同词不再打高德）。可留空。

        返回：JSON，status=SAVED / DUPLICATE / REJECTED / UNKNOWN_ID。
            REJECTED 表示这个点不属于这座城市、或不是可去的地点（原因在 reason 里），换一个点；
            UNKNOWN_ID 表示它不是本轮 search_poi 返回过的地点，先去 search_poi 查。
        """

        return _json(sink.add_poi(place_id, query=query))

    # 高德两个工具直接复用旅行工具集（同一份截断 / 字段白名单 / hint），只把 Hub 换成录制代理。
    travel = build_travel_tools(recorder)
    amap = [item for item in travel if getattr(item, "name", None) in {"search_poi", "poi_detail"}]
    return [search_guides, *amap, save_city_evidence, save_city_poi]


#: ToolLoader 的清单约定（与 `travel_tools.TOOLS` 同理）：这些工具需要 hub / sink 实例，
#: 走 `build_preheat_tools(...)` 手动传入，不参与目录自动发现。
TOOLS: list[Any] = []


__all__ = [
    "DEFAULT_TOP_N",
    "MAX_TOP_N",
    "PLATFORMS",
    "PREVIEW_CHARS",
    "TOOLS",
    "CityKnowledgeSink",
    "RecordingHub",
    "build_preheat_tools",
    "clamp_top_n",
    "split_mentions",
]
