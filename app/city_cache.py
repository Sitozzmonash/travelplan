"""跨会话城市攻略 / POI 缓存。

只缓存会随城市复用的攻略与 POI；机酒价格和时刻表始终由当前会话实时查询。
本模块刻意不依赖工作流，避免缓存命中时把一次临时会话变成正式 run。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Iterable, Mapping

from app.config import current_config
from app.models import Evidence, Place, basic_name_key, utcnow
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

    def payload(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "places": [place.model_dump(mode="json") for place in self.places],
            "mentions": self.mentions,
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
) -> CityCacheHit | None:
    """读取城市候选。

    默认只有 POI 和攻略都在各自 TTL 内才命中；调用方可以显式取得过期的旧数据来
    实现 stale-while-revalidate。没有 POI 不能算命中，防止一次空 Provider 响应被缓存。
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
    poi_fresh = _rows_fresh(poi_rows, reference, config.city_cache_poi_ttl_days)
    guide_fresh = not mention_rows or _rows_fresh(
        mention_rows, reference, config.city_cache_guide_ttl_days
    )
    stale = not (poi_fresh and guide_fresh)
    if stale and not allow_stale:
        return None

    # 最旧记录决定数据年龄：不能把一条刚补写的数据伪装成整座城市都刚刷新。
    all_times = [_parse_time(row.get("updated_at")) for row in (*poi_rows, *mention_rows)]
    valid_times = [value for value in all_times if value is not None]
    updated_at = min(valid_times).isoformat() if valid_times else ""
    return CityCacheHit(
        city=normalized,
        places=[_place_from_row(row, normalized) for row in poi_rows],
        mentions=[_mention_from_row(row) for row in mention_rows],
        updated_at=updated_at,
        stale=stale,
    )


def write_candidates(
    city: str,
    places: Iterable[Place],
    evidences: Iterable[Evidence],
    *,
    store: TravelPlanStore | None = None,
    updated_at: str | None = None,
) -> int:
    """持久化一次 live Discovery 的可复用部分，返回写入 POI 数量。"""

    normalized = normalize_city(city)
    if not normalized:
        return 0
    cache_store = store or TravelPlanStore()
    timestamp = updated_at or utcnow().isoformat()
    place_list = list(places)
    poi_rows = [_poi_row(place, timestamp) for place in place_list if place.place_id]
    cache_store.upsert_city_pois(normalized, poi_rows)
    cache_store.upsert_city_poi_mentions(
        normalized, _mention_rows(place_list, evidences, timestamp)
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
