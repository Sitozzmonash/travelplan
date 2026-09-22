"""整城清库 + 重建城市知识库（脏数据必须有显式出口）。

为什么不能只靠 `prune_city_cache` / 时间
----------------------------------------
成都那批候选里，"杜甫草堂"一个人占了 5 行（博物馆 / 售票处 / 地铁站 / 大雅堂 / 南邻），
20 行候选只对应 4 个真实景点。这类脏数据的四条退路**全都不成立**：

1. 过期不等于读不到 —— 会话走 `read_candidates(..., allow_stale=True)`，过期数据照旧回吐；
2. 清理只在写入时发生（`prune_city_cache` 在 `write_candidates` 末尾才调），没人写就一直躺着；
3. 写新数据**覆盖不掉**它 —— upsert 键是 `(city, place_id)`，而"杜甫草堂博物馆"与
   "杜甫草堂(地铁站)"在高德是两个不同的 `place_id`，新一次预热只会再加一批，不会顶掉旧的；
4. 于是用得越久，堆得越多。

结论：**必须整城清空再重建**，`scripts/preheat_cities.py` 只负责"重建"，本脚本负责"清"。

顺序上的一句话：先修代码，再清库重建。
--------------------------------------
清洗规则就是待改的那套规则本身 —— 用没修好的代码重跑一遍，只会拿回同样一份脏候选
（实测 20 城 × 20 条全部带子设施扎堆 + 误分类）。所以本脚本**默认要求代码已经改好**，
并把"重建后候选里还有没有子设施"当作自检项报出来：还有就说明代码没生效，而不是数据的事。

清哪些、不清哪些
----------------
清：`city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta`，
以及从这批脏 POI 派生出来的 canonical 实体层（`canonical_places` / `place_aliases` /
`place_provider_refs` / `place_evidence_links` / `place_relations` / `poi_query_cache`）——
实体层的输入就是这批 POI，留着它等于让重建读回旧身份。

**不动** run 级 `places` / `place_evidence` / `sources` / `evidence`：那是历史 run 的证据链，
改写它们等于让过去某次 run 查不到自己的地点。

用法
----
    python scripts/rebuild_city_cache.py --cities 成都 --dry-run    # 先看清会删多少行
    python scripts/rebuild_city_cache.py --cities 成都             # 单城重建
    python scripts/rebuild_city_cache.py                            # 默认 20 城（逐城串行）
    python scripts/rebuild_city_cache.py --yes                      # 跳过交互确认

退出码：0 = 全部成功；1 = 有城市失败 / 未落库 / 自检发现仍有子设施。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from app import city_cache, places, sessions  # noqa: E402
from app.store import TravelPlanStore  # noqa: E402
from scripts.preheat_cities import DEFAULT_CITIES  # noqa: E402

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

#: purge 会动的表（用于 `--dry-run` 报告与非空校验）。
PURGED_TABLES = (
    "city_pois",
    "city_poi_mentions",
    "city_evidences",
    "city_cache_meta",
    "canonical_places",
    "place_aliases",
    "place_provider_refs",
    "place_evidence_links",
    "place_relations",
    "poi_query_cache",
)


def _count_rows(store: TravelPlanStore, city: str) -> dict[str, int]:
    """只读统计：这座城市的缓存表 + 实体层各有多少行。

    走 `store._connect()`（两种后端都支持 `?` 占位符，方言由 backend 翻译）而不是新加一个
    公开方法：这是**只读的诊断**，不该为了一个脚本往 store 的对外接口上加查询。
    """

    counts: dict[str, int] = {}
    with store._connect() as conn:  # noqa: SLF001 —— 只读统计
        for table in PURGED_TABLES:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE city=?", (city,)).fetchone()
                counts[table] = int(row["n"] if row is not None else 0)
            except Exception:  # noqa: BLE001 —— 老库可能还没有实体层那几张表
                counts[table] = -1
    return counts


def _quality_report(store: TravelPlanStore, city: str) -> dict[str, object]:
    """重建后的自检：候选里还有没有附属设施？提及表有没有真的建起来？

    这两个数字就是这轮改造要修的两个问题（子设施扎堆 / `city_poi_mentions` 恒为 0），
    所以让脚本自己报出来 —— "跑完了"和"跑对了"是两件事。
    """

    hit = city_cache.read_candidates(city, store=store, allow_stale=True)
    if hit is None:
        return {"places": 0, "facilities": [], "mentions": 0, "duplicates": 0, "cities": []}
    facilities = [
        place.name
        for place in hit.places
        if places.facility_kind(place.name, place.type) is not None
    ]
    # 重复判定按归一化名字键：这正是"20 行候选只对应 4 个景点"里被拆开的那一层。
    keys: dict[str, int] = {}
    for place in hit.places:
        key = places.place_key(place.name, city)
        if key:
            keys[key] = keys.get(key, 0) + 1
    mention_rows = [row for row in hit.mentions]
    other_cities = sorted(
        {
            str(place.city)
            for place in hit.places
            if place.city and places.city_key(place.city) != places.city_key(city)
        }
    )
    return {
        "places": len(hit.places),
        "facilities": facilities,
        "mentions": len(mention_rows),
        "duplicates": sum(count - 1 for count in keys.values() if count > 1),
        "cities": other_cities,
    }


def _print_counts(label: str, counts: dict[str, int]) -> None:
    rendered = "  ".join(
        f"{table}={count}" for table, count in counts.items() if count > 0 or count == -1
    )
    print(f"    {label}：{rendered or '（全为 0）'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="整城清空并重建城市知识库")
    parser.add_argument("--cities", nargs="*", default=list(DEFAULT_CITIES), help="要重建的城市")
    parser.add_argument("--dry-run", action="store_true", help="只报告会删多少行，不删不建")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument(
        "--allow-sqlite",
        action="store_true",
        help="允许写本地 SQLite（默认拒绝：重建的意义是修所有人共用的那份缓存）",
    )
    args = parser.parse_args()

    load_dotenv(_ENV_PATH)

    store = TravelPlanStore()
    if store.backend_name != "postgres" and not args.allow_sqlite:
        print(
            f"拒绝执行：当前库是 {store.backend_name}（{store.describe()}），不是共享的 Postgres。\n"
            "脏数据在共享库里，只清本地 SQLite 等于没清。确认要走本地库时加 --allow-sqlite。",
            file=sys.stderr,
        )
        return 1

    cities = [city.strip() for city in args.cities if city and city.strip()]
    if not cities:
        print("没有要重建的城市", file=sys.stderr)
        return 1

    print(f"库：{store.describe()}")
    print(f"待重建 {len(cities)} 座城市：{'、'.join(cities)}")
    before = {city: _count_rows(store, city) for city in cities}
    print("\n重建前：")
    for city in cities:
        print(f"  {city}：")
        _print_counts("现有行", before[city])
        report = _quality_report(store, city)
        print(
            f"    自检：候选 {report['places']} 个，其中附属设施 {len(report['facilities'])} 个，"
            f"同名重复 {report['duplicates']} 个，提及行 {report['mentions']} 条"
        )
        if report["cities"]:
            print(f"    ⚠ 越界城市：{'、'.join(report['cities'])}")

    if args.dry_run:
        print("\n--dry-run：没有删除、没有调用任何 Provider。")
        return 0

    if not args.yes:
        print(
            "\n这会**永久删除**上面这些行（含 canonical 实体层），然后逐城重新预热。\n"
            "run 级的 places / sources / evidence 不受影响。\n"
            "确认请输入 yes：",
            end="",
            flush=True,
        )
        if input().strip().lower() != "yes":
            print("已取消。")
            return 1

    failed: list[str] = []
    flagged: list[str] = []
    started = time.perf_counter()
    for index, city in enumerate(cities, start=1):
        print(f"\n[{index}/{len(cities)}] {city}：清空旧缓存……", flush=True)
        try:
            removed = store.purge_city_cache(city)
        except Exception as exc:  # noqa: BLE001 —— 一座城市失败不该中断整批
            failed.append(city)
            print(f"    清空失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        _print_counts("已删除", removed)

        print("    重新预热（攻略 + 地点抽取 + 实体收敛）……", flush=True)
        began = time.perf_counter()
        try:
            summary = sessions.refresh_city_cache(store, city)
        except Exception as exc:  # noqa: BLE001
            failed.append(city)
            print(f"    预热失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        took = time.perf_counter() - began
        if not int(summary.get("places") or 0):
            failed.append(city)
            print(f"    未落库：{took:.0f}s 跑完但 POI 为 0", file=sys.stderr, flush=True)
            continue

        report = _quality_report(store, city)
        print(
            f"    完成（{took:.0f}s）：写入 POI {summary.get('places')} 个，"
            f"正文 {summary.get('evidences')} 条（社媒 {summary.get('social_evidences')}），"
            f"检索词 {summary.get('queries')}",
            flush=True,
        )
        print(
            f"    重建后自检：候选 {report['places']} 个，附属设施 {len(report['facilities'])} 个，"
            f"同名重复 {report['duplicates']} 个，提及行 {report['mentions']} 条",
            flush=True,
        )
        if report["facilities"]:
            flagged.append(city)
            print(
                f"    ⚠ 候选里仍有附属设施：{'、'.join(str(item) for item in report['facilities'][:6])}\n"
                "      这说明收敛规则没生效（先修代码再重建），不是数据的问题。",
                file=sys.stderr,
                flush=True,
            )
        if report["cities"]:
            flagged.append(city)
            print(f"    ⚠ 仍有越界城市：{'、'.join(str(item) for item in report['cities'])}", file=sys.stderr)
        if not report["mentions"]:
            print(
                "    ⚠ 提及表仍为 0 行：说明攻略抽出的地名没有回到 evidence.place_mentions，"
                "或者缓存命中的候选整批来自高德关键词、没有对上任何一篇攻略。",
                file=sys.stderr,
            )

    print(f"\n总耗时 {time.perf_counter() - started:.0f}s")
    if failed:
        print(f"{len(failed)} 座城市未成功：{'、'.join(failed)}", file=sys.stderr)
    if flagged:
        print(f"{len(flagged)} 座城市自检未通过：{'、'.join(flagged)}", file=sys.stderr)
    if failed or flagged:
        return 1
    print("全部完成，自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
