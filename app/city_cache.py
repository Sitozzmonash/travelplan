"""跨会话城市攻略 / POI 缓存。

缓存三样会随城市复用的东西：高德 POI 基础信息、攻略→地点提及、**攻略正文与检索词**。
机酒价格和时刻表始终由当前会话实时查询，永远不进这里。

攻略正文入库是这套缓存的关键一半：只缓 POI 的话，第二个去同一座城市的会话仍然要为
社媒检索 + 模型抽取全价重付一遍（实测社媒链 45~180s、批抽取一批可到 54.7s），
而这两步的产物（正文与检索词）本来就是半静态的。

本模块刻意不依赖工作流，避免缓存命中时把一次临时会话变成正式 run。
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from app import planner
from app.config import current_config
from app.models import Evidence, Place, basic_name_key, coerce_datetime, utcnow
from app.planner import normalize_place_name
from app.store import TravelPlanStore


_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="travelplan-city-cache")
_REFRESHING: set[str] = set()
_REFRESH_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class CityCacheHit:
    """供会话和只读 API 使用的已缓存候选。"""

    city: str
    places: list[Place]
    mentions: list[dict[str, Any]]
    updated_at: str
    source: str = "city_cache"
    stale: bool = False
    #: 攻略**正文**（跨会话复用，让第二个会话不必重付社媒检索 + 模型抽取）。
    evidences: list[Evidence] = field(default_factory=list)
    #: 该城市的检索词计划 / 真正搜过的检索词（复用可省掉一次 query_expansion 模型调用）。
    social_queries: list[str] = field(default_factory=list)
    social_served_queries: list[str] = field(default_factory=list)

    def payload(self, *, include_evidence_text: bool = True) -> dict[str, Any]:
        """序列化。

        `include_evidence_text=False` 用于**未鉴权的公开只读接口**：那里只需要"有哪些候选、
        有多少条攻略"，正文既没必要外发（第三方内容 + 体积可达数百 KB），也不该外发。
        关掉时保留元信息与字符数，正文本身留在库里。
        """

        return {
            "city": self.city,
            "places": [place.model_dump(mode="json") for place in self.places],
            "mentions": self.mentions,
            "evidences": [
                evidence.model_dump(mode="json")
                if include_evidence_text
                else {
                    "evidence_id": evidence.id,
                    "title": evidence.title,
                    "author": evidence.author,
                    "source_type": evidence.source_type,
                    "provider": evidence.provider,
                    "source_url": evidence.source_url,
                    "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                    "fetched_at": evidence.fetched_at.isoformat() if evidence.fetched_at else None,
                    "chars": len(evidence.text or ""),
                }
                for evidence in self.evidences
            ],
            "social_queries": list(self.social_queries),
            "social_served_queries": list(self.social_served_queries),
            "updated_at": self.updated_at,
            "source": self.source,
            "stale": self.stale,
        }


def normalize_city(city: str) -> str:
    """统一城市键；不猜测行政别名，避免把不同城市错误合并。"""

    return " ".join(str(city or "").strip().split())


def read_candidates(
    city: str,
    *,
    store: TravelPlanStore | None = None,
    now: datetime | None = None,
    allow_stale: bool = False,
    evidence_limit: int | None = None,
) -> CityCacheHit | None:
    """读取城市候选。

    默认只有 POI 和攻略（提及 + 正文 + 检索词）都在各自 TTL 内才命中；调用方可以显式
    取得过期的旧数据来实现 stale-while-revalidate。没有 POI 不能算命中，防止一次空
    Provider 响应被缓存。

    `evidence_limit` 限制返回的攻略正文条数（默认取 `EVIDENCE_MAX`，与实时抓取同口径），
    避免一次把整座城市的攻略正文全塞进 prefetch_json。
    """

    normalized = normalize_city(city)
    if not normalized:
        return None
    try:
        cache_store = store or TravelPlanStore(read_only=True)
    except FileNotFoundError:
        return None
    poi_rows, mention_rows = cache_store.get_city_cache_rows(normalized)
    if not poi_rows:
        return None

    reference = (now or utcnow()).astimezone(timezone.utc)
    config = current_config()
    evidence_rows = cache_store.get_city_evidence_rows(normalized)
    meta = cache_store.get_city_cache_meta(normalized) or {}

    poi_fresh = _rows_fresh(poi_rows, reference, config.city_cache_poi_ttl_days)
    guide_rows: list[dict[str, Any]] = [*mention_rows, *evidence_rows]
    meta_time = _parse_time(meta.get("updated_at"))
    if meta_time is not None:
        guide_rows.append({"updated_at": meta_time.isoformat()})
    guide_fresh = not guide_rows or _rows_fresh(
        guide_rows, reference, config.city_cache_guide_ttl_days
    )
    stale = not (poi_fresh and guide_fresh)
    if stale and not allow_stale:
        return None

    # 最旧记录决定数据年龄：不能把一条刚补写的数据伪装成整座城市都刚刷新。
    all_times = [_parse_time(row.get("updated_at")) for row in (*poi_rows, *guide_rows)]
    valid_times = [value for value in all_times if value is not None]
    updated_at = min(valid_times).isoformat() if valid_times else ""

    limit = max(0, int(evidence_limit if evidence_limit is not None else config.evidence_max))
    evidences = [_evidence_from_row(row) for row in evidence_rows[:limit] if row.get("text") or row.get("place_mentions_json")]
    places = _dedupe_cached_places(
        [_place_from_row(row, normalized) for row in poi_rows], normalized, cache_store
    )
    return CityCacheHit(
        city=normalized,
        places=places,
        mentions=_restore_mentions(places, mention_rows, evidence_rows),
        updated_at=updated_at,
        stale=stale,
        evidences=evidences,
        social_queries=_loads_list(meta.get("social_queries_json")),
        social_served_queries=_loads_list(meta.get("social_served_queries_json")),
    )


def write_candidates(
    city: str,
    places: Iterable[Place],
    evidences: Iterable[Evidence],
    *,
    store: TravelPlanStore | None = None,
    updated_at: str | None = None,
    social_queries: Iterable[str] | None = None,
    social_served_queries: Iterable[str] | None = None,
) -> int:
    """持久化一次 live Discovery 的可复用部分，返回写入 POI 数量。

    除了 POI 与提及，这里还把**攻略正文**与**检索词**落库：没有这两样，第二个去同一座
    城市的会话仍然要重跑社媒检索与模型抽取。

    写完顺手清掉超过各自 TTL 的行：新鲜度口径是"所有行都在 TTL 内"，只增不删会让一座
    城市在 TTL 之后**永远 stale**（见 `prune_city_cache` 的说明）。
    """

    normalized = normalize_city(city)
    if not normalized:
        return 0
    cache_store = store or TravelPlanStore()
    timestamp = updated_at or utcnow().isoformat()
    place_list = _dedupe_cached_places(list(places), normalized, cache_store)
    evidence_list = list(evidences)
    # 旧版无 URL 内容键也可继续复用：按完整内容匹配旧行，不回填/删除历史键。
    existing_keys: dict[str, str] = {}
    for row in cache_store.get_city_evidence_rows(normalized):
        content_key = _evidence_key(_evidence_from_row(row))
        existing_keys.setdefault(content_key, str(row["evidence_key"]))
    mention_rows = _mention_rows(place_list, evidence_list, timestamp)
    evidence_rows = _evidence_rows(evidence_list, timestamp)
    for row in [*mention_rows, *evidence_rows]:
        row["evidence_key"] = existing_keys.get(row["evidence_key"], row["evidence_key"])
    evidence_keys: dict[str, set[str]] = {}
    for row in mention_rows:
        evidence_keys.setdefault(row["place_id"], set()).add(row["evidence_key"])
    poi_rows = [
        _poi_row(place, timestamp, evidence_count=len(evidence_keys.get(place.place_id, set())))
        for place in place_list if place.place_id
    ]
    cache_store.upsert_city_pois(normalized, poi_rows)
    cache_store.upsert_city_poi_mentions(normalized, mention_rows)
    cache_store.upsert_city_evidences(normalized, evidence_rows)
    # Evidence → 实体挂载：失败不该让整个缓存写入失败（它只是索引，候选与正文才是主体）。
    try:
        cache_store.upsert_place_evidence_links(
            _link_evidence_rows(
                mention_rows, city=normalized, store=cache_store, timestamp=timestamp
            )
        )
    except Exception:  # noqa: BLE001
        pass
    queries = [str(query) for query in (social_queries or []) if str(query).strip()]
    served = [str(query) for query in (social_served_queries or []) if str(query).strip()]
    if queries or served:
        cache_store.upsert_city_cache_meta(
            normalized,
            social_queries=queries,
            social_served_queries=served,
            updated_at=timestamp,
        )
    config = current_config()
    cache_store.prune_city_cache(
        normalized,
        guide_before=_threshold(timestamp, config.city_cache_guide_ttl_days),
        poi_before=_threshold(timestamp, config.city_cache_poi_ttl_days),
    )
    return len(poi_rows)


def refresh_in_background(
    city: str,
    *,
    refresh: Callable[[str], Any] | None = None,
) -> None:
    """合并同城刷新请求并异步执行回调；无回调时安全地不做外部请求。"""

    normalized = normalize_city(city)
    if not normalized or refresh is None:
        return
    with _REFRESH_LOCK:
        if normalized in _REFRESHING:
            return
        _REFRESHING.add(normalized)

    def run() -> None:
        try:
            refresh(normalized)
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(normalized)

    _REFRESH_EXECUTOR.submit(run)


def _rows_fresh(rows: list[dict[str, Any]], reference: datetime, ttl_days: int) -> bool:
    threshold = reference - timedelta(days=max(1, ttl_days))
    times = [_parse_time(row.get("updated_at")) for row in rows]
    return bool(times) and all(value is not None and value >= threshold for value in times)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _dedupe_cached_places(places: list[Place], city: str, store: TravelPlanStore) -> list[Place]:
    """只读已有身份，不运行/persist resolver，也不按别名反查 canonical 身份。"""

    if not places:
        return planner.dedupe_places([])[0]
    refs = [row for row in store.get_place_provider_refs(city) if row.get("provider") == "amap"]
    by_id = {str(row["provider_place_id"]): str(row["canonical_place_id"]) for row in refs}
    entities = {str(row["canonical_place_id"]): row for row in store.get_canonical_places(city)}
    refs_by_entity: dict[str, list[dict[str, Any]]] = {}
    names_by_entity: dict[str, set[str]] = {}
    for row in refs:
        canonical_id = str(row["canonical_place_id"])
        refs_by_entity.setdefault(canonical_id, []).append(row)
        if row.get("provider_name"):
            names_by_entity.setdefault(canonical_id, set()).add(str(row["provider_name"]))
    for row in store.get_place_aliases(city):
        if row.get("alias"):
            names_by_entity.setdefault(str(row["canonical_place_id"]), set()).add(str(row["alias"]))

    restored: list[Place] = []
    for original in places:
        place = original.model_copy(deep=True)
        place.city = place.city or city
        canonical_id = by_id.get(place.place_id)
        # 老的代表点可能没 ref，但 merged_from 里有；仅在身份唯一时采用。
        if not canonical_id:
            identities = {by_id[item] for item in place.merged_from if item in by_id}
            canonical_id = next(iter(identities)) if len(identities) == 1 else None
        entity = entities.get(canonical_id or "")
        if entity is not None:
            name = str(entity.get("canonical_name") or place.name)
            primary = min(
                refs_by_entity[canonical_id],
                key=lambda row: (row.get("provider_name") != name, str(row["provider_place_id"])),
            )
            place.place_id = str(primary["provider_place_id"])
            place.aliases = sorted(
                {original.name, *original.aliases, *names_by_entity.get(canonical_id, set())} - {name, ""}
            )
            place.merged_from = sorted(
                {original.place_id, *original.merged_from,
                 *(str(row["provider_place_id"]) for row in refs_by_entity[canonical_id])}
                - {place.place_id, ""}
            )
            place.name = name
            for field_name in ("lat", "lng", "address", "district", "business_area", "opening_hours"):
                if entity.get(field_name) is not None:
                    setattr(place, field_name, entity[field_name])
        restored.append(place)
    merged, _ = planner.dedupe_places(restored)
    # dedupe 的 Place 只有一个 source_id 槽；代表点缺来源时保留簇中已有来源。
    for place in merged:
        if not place.source_id:
            ids = {place.place_id, *place.merged_from}
            place.source_id = next((item.source_id for item in restored if item.place_id in ids and item.source_id), None)
    return merged


def _poi_row(place: Place, timestamp: str, *, evidence_count: int = 0) -> dict[str, Any]:
    return {
        "place_id": place.place_id,
        "name": place.name,
        "normalized_name": place.normalized_name or basic_name_key(place.name),
        "category": place.type,
        "lng": place.lng,
        "lat": place.lat,
        "address": place.address,
        "business_area": place.business_area,
        "district": place.district,
        "opening_hours": place.opening_hours,
        "amap_verified": place.amap_verified,
        "evidence_count": evidence_count,
        "metadata": {
            "aliases": list(place.aliases),
            "merged_from": list(place.merged_from),
            "source_id": place.source_id,
        },
        "updated_at": timestamp,
    }


def _place_from_row(row: Mapping[str, Any], city: str) -> Place:
    metadata = _loads_dict(row.get("metadata_json"))
    return Place(
        aliases=_loads_list(metadata.get("aliases")),
        merged_from=_loads_list(metadata.get("merged_from")),
        source_id=str(metadata["source_id"]) if metadata.get("source_id") else None,
        place_id=str(row["place_id"]),
        name=str(row.get("name") or row["place_id"]),
        normalized_name=str(row.get("normalized_name") or ""),
        type=str(row.get("category") or ""),
        lng=row.get("lng"),
        lat=row.get("lat"),
        address=row.get("address"),
        city=city,
        district=row.get("district"),
        business_area=row.get("business_area"),
        opening_hours=row.get("opening_hours"),
        amap_verified=bool(row.get("amap_verified")),
    )


def _mention_rows(
    places: list[Place], evidences: Iterable[Evidence], timestamp: str
) -> list[dict[str, Any]]:
    """攻略提及 → 城市级索引行。

    A5 的根因有两半，这里修的是第二半：**匹配不能用字符串相等**。
    攻略里写的是"杜甫草堂"，而高德返回并入库的是"成都杜甫草堂博物馆" —— 归一化之后
    两者都是"杜甫草堂"，但 `basic_name_key` 的字符串相等判不出这一点，于是即便抽出的
    地名已经写回 `evidence.place_mentions`，这里仍然匹配不上、`city_poi_mentions` 还是空的。

    所以匹配键用 `normalize_place_name`（与 run 内去重、实体层同一套），并把地点的
    **别名**（含被收敛进来的子设施名）一起放进索引 —— 用户写"熊猫基地南门"、
    实体叫"成都大熊猫繁育研究基地"时也要能对上。
    """

    by_key = _mention_index(places)
    rows: list[dict[str, Any]] = []
    for evidence in evidences:
        for raw_name in dict.fromkeys(evidence.place_mentions or []):
            place = _mentioned_place(by_key, raw_name, f"{evidence.title}\n{evidence.text}")
            if place is None:
                continue
            rows.append(
                {
                    "place_id": place.place_id,
                    "raw_name": raw_name,
                    "evidence_key": _evidence_key(evidence),
                    "source_type": evidence.source_type,
                    "provider": evidence.provider,
                    "source_url": evidence.source_url,
                    "snippet": (evidence.text or "")[:500],
                    "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                    "updated_at": timestamp,
                }
            )
    return rows


def _mention_index(places: Sequence[Place]) -> dict[str, list[Place]]:
    by_key: dict[str, list[Place]] = {}
    for place in places:
        names = {place.name, *place.aliases}
        if place.district:
            names.update(f"{place.district}{name}" for name in tuple(names))
        for name in names:
            for city in (place.city, None):
                key = normalize_place_name(name, city=city) or basic_name_key(name)
                bucket = by_key.setdefault(key, [])
                if key and all(item.place_id != place.place_id for item in bucket):
                    bucket.append(place)
    return by_key


def _mentioned_place(by_key: Mapping[str, list[Place]], raw_name: str, context: str) -> Place | None:
    key = normalize_place_name(raw_name) or basic_name_key(raw_name)
    candidates = by_key.get(key, [])
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    # 裸同名绝不按顺序选。名称本身的区县优先；正文有多个区县/全称时保守不挂。
    for text in (raw_name, context):
        text_key = basic_name_key(text)
        matched: set[str] = set()
        for place in candidates:
            if place.district and basic_name_key(place.district) in text_key:
                matched.add(place.place_id)
            other_names = {
                basic_name_key(name)
                for other in candidates if other.place_id != place.place_id
                for name in (other.name, *other.aliases)
            }
            for name in (place.name, *place.aliases):
                name_key = basic_name_key(name)
                if name_key and name_key in text_key and not any(name_key in other for other in other_names):
                    matched.add(place.place_id)
        if matched:
            return next((place for place in candidates if place.place_id in matched), None) if len(matched) == 1 else None
    return None


def _restore_mentions(
    places: list[Place], mention_rows: Sequence[Mapping[str, Any]], evidence_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """在内存重挂历史提及；正文可修复旧 PK 覆盖，旧行缺内容键时兼容读取。"""

    by_key = _mention_index(places)
    restored: dict[tuple[str, str], dict[str, Any]] = {}
    evidences = [(row, _evidence_from_row(row)) for row in evidence_rows]
    for row, evidence in evidences:
        for mention in _mention_rows(places, [evidence], str(row.get("updated_at") or "")):
            mention["evidence_key"] = str(row["evidence_key"])
            restored[(mention["evidence_key"], mention["raw_name"])] = _mention_from_row(mention)
    for row in mention_rows:
        raw_name = str(row.get("raw_name") or "")
        evidence_key = str(row.get("evidence_key") or "")
        matches = [
            (saved, evidence) for saved, evidence in evidences
            if (evidence_key and str(saved.get("evidence_key")) == evidence_key)
            or (not evidence_key
                and (row.get("source_url") or "") == (evidence.source_url or "")
                and (not row.get("provider") or row.get("provider") == evidence.provider)
                and bool(row.get("snippet")) and evidence.text.startswith(str(row["snippet"])))
        ]
        if len(matches) > 1:
            # 旧片段不足以识别攻略；正文已逐条重建，不能凭旧行再多算一条。
            continue
        if len(matches) == 1:
            saved, evidence = matches[0]
            evidence_key = str(saved["evidence_key"])
            context = f"{evidence.title}\n{evidence.text}"
        else:
            context = str(row.get("snippet") or "")
        place = _mentioned_place(by_key, raw_name, context)
        if place is None:
            continue
        identity = evidence_key or str(row.get("source_url") or f"legacy:{context}")
        restored.setdefault((identity, raw_name), _mention_from_row({
            **row, "place_id": place.place_id, "evidence_key": evidence_key or None,
        }))
    return list(restored.values())


def _link_evidence_rows(
    mention_rows: Sequence[Mapping[str, Any]], *, city: str, store: TravelPlanStore, timestamp: str
) -> list[dict[str, Any]]:
    """Evidence → Canonical Place 的挂载（方案 P1-11）。

    用的是 `_mention_rows` 已经算出来的 (证据, 地点, 提及文本) 三元组 —— 那里已经做过
    归一化与别名匹配，再算一遍只会多一处会漂移的规则。缺的只是"这个 place_id 属于哪个
    canonical 实体"，从 `place_provider_refs` 反查即可。
    """

    pairs = [("amap", str(row["place_id"])) for row in mention_rows if row.get("place_id")]
    if not pairs:
        return []
    canonical_by_ref = {
        (str(ref.get("provider")), str(ref.get("provider_place_id"))): str(ref.get("canonical_place_id"))
        for ref in store.get_place_provider_refs(city, keys=pairs)
    }
    rows: list[dict[str, Any]] = []
    for row in mention_rows:
        canonical_id = canonical_by_ref.get(("amap", str(row.get("place_id") or "")))
        if not canonical_id:
            # 没有 canonical 映射说明这批 POI 是更早版本写进去的（实体层还没建）。
            # 不猜、不新建：等下一次预热重建时自然补上。
            continue
        rows.append(
            {
                "canonical_place_id": canonical_id,
                "city": city,
                "evidence_key": row.get("evidence_key"),
                "mention_text": str(row.get("raw_name") or ""),
                "confidence": None,
                "updated_at": timestamp,
            }
        )
    return rows


def _mention_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "place_id": row.get("place_id"),
        "raw_name": row.get("raw_name"),
        "evidence_key": row.get("evidence_key"),
        "source_type": row.get("source_type"),
        "provider": row.get("provider"),
        "source_url": row.get("source_url") or None,
        "snippet": row.get("snippet"),
        "tone": row.get("tone"),
        "published_at": row.get("published_at"),
        "updated_at": row.get("updated_at"),
    }


def _threshold(reference: str, ttl_days: int) -> str:
    """按 TTL 算出"早于这个时刻就算过期"的 ISO 阈值（用于清理）。"""

    base = _parse_time(reference) or utcnow().astimezone(timezone.utc)
    return (base - timedelta(days=max(1, ttl_days))).isoformat()


def _evidence_key(evidence: Evidence) -> str:
    """攻略正文的内容键。

    键必须同时满足两件事：**同一篇文章重抓要落到同一行**（否则每次刷新都长出新行），
    而**同一 URL 的不同条目不能被合并**（搜索引擎对多个 query 会返回同一个 URL，
    标题与正文却不同；只按 URL 去重会直接丢掉证据）。

    所以：有 URL 时用 `url + title`；没有 URL 时用 provider + source_type + 标题 +
    发布时间 + 作者 + 完整正文（同元信息、不同正文不能撞键）。**不用** run 级
    `evidence.id`：它内嵌 session_id，跨会话必然不同。历史键不回填、不删除。
    """

    url = " ".join(str(evidence.source_url or "").split())
    title = " ".join(str(evidence.title or "").split())
    if url:
        identity = f"url\x00{url}\x00{title}"
    else:
        identity = "\x00".join(
            [
                "text-v2",
                str(evidence.provider or ""),
                str(evidence.source_type or ""),
                title,
                evidence.published_at.isoformat() if evidence.published_at else "",
                str(evidence.author or ""),
                evidence.text or "",
            ]
        )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]


def _evidence_rows(evidences: Iterable[Evidence], timestamp: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for evidence in evidences:
        if not (evidence.text or evidence.place_mentions or evidence.specific_dishes):
            # 纯空壳（没正文、没提及、没菜品）没有复用价值，存了只会占空间。
            continue
        rows.append(
            {
                "evidence_key": _evidence_key(evidence),
                "source_type": evidence.source_type,
                "provider": evidence.provider,
                "source_url": evidence.source_url,
                "title": evidence.title,
                "author": evidence.author,
                "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                "fetched_at": evidence.fetched_at.isoformat() if evidence.fetched_at else None,
                "text": evidence.text or "",
                "place_mentions": list(evidence.place_mentions or []),
                "specific_dishes": list(evidence.specific_dishes or []),
                "raw_metrics": dict(evidence.raw_metrics or {}),
                "updated_at": timestamp,
            }
        )
    return rows


def _evidence_from_row(row: Mapping[str, Any]) -> Evidence:
    return Evidence(
        # 城市级 id 只是"这条缓存证据"的稳定标识；正式 run 复用时会被重新分配成
        # run 级 id（sources/evidence 都是全局主键，沿用旧 id 会改掉上一轮的归属）。
        id=f"city-{row.get('evidence_key')}",
        source_type=str(row.get("source_type") or "social"),
        provider=str(row.get("provider") or ""),
        source_url=row.get("source_url") or None,
        fetched_at=coerce_datetime(row.get("fetched_at")) or utcnow(),
        title=str(row.get("title") or ""),
        author=row.get("author") or None,
        published_at=coerce_datetime(row.get("published_at")),
        text=str(row.get("text") or ""),
        place_mentions=_loads_list(row.get("place_mentions_json")),
        specific_dishes=_loads_list(row.get("specific_dishes_json")),
        raw_metrics=_loads_dict(row.get("raw_metrics_json")),
    )


def _loads_list(value: Any) -> list[str]:
    parsed = _loads_json(value)
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _loads_dict(value: Any) -> dict[str, Any]:
    parsed = _loads_json(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _loads_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None
