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
from typing import Any, Callable, Iterable, Mapping

from app.config import current_config
from app.models import Evidence, Place, basic_name_key, coerce_datetime, utcnow
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
    cache_store = store or TravelPlanStore()
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
    return CityCacheHit(
        city=normalized,
        places=[_place_from_row(row, normalized) for row in poi_rows],
        mentions=[_mention_from_row(row) for row in mention_rows],
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
    place_list = list(places)
    evidence_list = list(evidences)
    poi_rows = [_poi_row(place, timestamp) for place in place_list if place.place_id]
    cache_store.upsert_city_pois(normalized, poi_rows)
    cache_store.upsert_city_poi_mentions(
        normalized, _mention_rows(place_list, evidence_list, timestamp)
    )
    cache_store.upsert_city_evidences(
        normalized, _evidence_rows(evidence_list, timestamp)
    )
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


def _poi_row(place: Place, timestamp: str) -> dict[str, Any]:
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
        "evidence_count": len(place.merged_from or []) or (1 if place.source_id else 0),
        "updated_at": timestamp,
    }


def _place_from_row(row: Mapping[str, Any], city: str) -> Place:
    return Place(
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
    by_name = {
        basic_name_key(place.name): place
        for place in places
        if basic_name_key(place.name)
    }
    rows: list[dict[str, Any]] = []
    for evidence in evidences:
        for raw_name in evidence.place_mentions or []:
            place = by_name.get(basic_name_key(raw_name))
            if place is None:
                continue
            rows.append(
                {
                    "place_id": place.place_id,
                    "raw_name": raw_name,
                    "source_type": evidence.source_type,
                    "provider": evidence.provider,
                    "source_url": evidence.source_url,
                    "snippet": (evidence.text or "")[:500],
                    "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                    "updated_at": timestamp,
                }
            )
    return rows


def _mention_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "place_id": row.get("place_id"),
        "raw_name": row.get("raw_name"),
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

    所以：有 URL 时用 `url + title`；没有 URL 时退到 provider + source_type + 标题 +
    发布时间；都没有才退到正文前缀。**不用** run 级 `evidence.id`：它内嵌 session_id，
    跨会话必然不同。
    """

    url = " ".join(str(evidence.source_url or "").split())
    title = " ".join(str(evidence.title or "").split())
    if url:
        identity = f"url\x00{url}\x00{title}"
    else:
        identity = "\x00".join(
            [
                "meta",
                str(evidence.provider or ""),
                str(evidence.source_type or ""),
                title,
                evidence.published_at.isoformat() if evidence.published_at else "",
            ]
        )
        if identity == "meta\x00\x00\x00\x00":
            identity = f"text\x00{(evidence.text or '')[:400]}"
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
