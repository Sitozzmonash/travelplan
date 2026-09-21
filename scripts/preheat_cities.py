"""为热门城市预热城市知识库（攻略正文 + 提及 + POI + 检索词）。

为什么需要它
------------
城市知识库的**读路径**已经做完（`docs/15_城市知识库攻略正文复用.md`）：命中之后
一个社媒 Provider 都不打、一次地点抽取都不调。但**填充**仍然只能"等第一个用户来踩"——
那位用户要把最贵的社媒检索 + 模型抽取全价付掉，而且一路压在 512Mi 的单实例上
（`docs/09_部署说明.md` §7 记过 OOM 与健康检查超时）。

预热就是把这段挪到离线：提前把行写进 `city_pois` / `city_poi_mentions` /
`city_evidences` / `city_cache_meta`，之后所有去同一座城市的会话都走命中路径。

为什么不在 Render 上跑
----------------------
线上是 512Mi 单实例，预热一跑就跟健康检查抢 CPU 与数据库连接，会把正在服务的请求
拖死。所以预热**只在本地 / CI 跑**，只要求写同一个库（Neon）。

跑的是什么
----------
复用 `sessions.refresh_city_cache()`。它构造的 intent 只有目的地、没有出发地和日期，
而 `fetch_transport_candidates` / `fetch_hotel_candidates` 在缺少这两项时会自己跳过
（`app/discovery.py:158`）——所以预热只跑「攻略检索 + 地点抽取」两条线，正是城市缓存
要存的东西，机酒价格与时刻表**永远不会**进缓存（这是设计不变量）。

一个必须盯住的降级：社媒限流
----------------------------
`tikhub` 免费额度很容易打成 HTTP 429。这时社媒那条线一条证据都拿不到，只剩网页搜索，
但流程仍然"成功"——缓存里 POI 有、正文有，只是**没有任何小红书 / 抖音证据**。
这种结果会被后续会话当成"攻略已就绪"吃满一个 TTL，所以本脚本把它单独标成
**降级**并打印原因，而且**下次运行时默认会重跑降级城市**（除非它们真的补上了社媒证据）。

用法
----
    python scripts/preheat_cities.py                    # 默认 6 个热门城市
    python scripts/preheat_cities.py --cities 成都 重庆
    python scripts/preheat_cities.py --force            # 已新鲜的也重跑
    python scripts/preheat_cities.py --list             # 只看现状，不跑

退出码：0 = 全部成功（降级不算失败，但会打印警告）；1 = 有城市失败 / 未落库。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402 —— 必须先加载 .env，再导入 app

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app import city_cache, sessions  # noqa: E402
from app.store import TravelPlanStore  # noqa: E402

#: 默认预热清单。按"被搜得多、且攻略足够稠密"挑，命中率比覆盖面重要。
DEFAULT_CITIES = ("成都", "重庆", "西安", "北京", "上海", "杭州")


def _row(city: str, *, store: TravelPlanStore) -> dict[str, object]:
    """读这座城市当前的城市缓存状态（不跑任何 Provider）。"""

    hit = city_cache.read_candidates(city, store=store, allow_stale=True)
    fresh = city_cache.read_candidates(city, store=store, allow_stale=False)
    if hit is None:
        return {"city": city, "state": "空", "places": 0, "evidences": 0,
                "social": 0, "queries": 0, "age": "-"}
    return {
        "city": city,
        "state": "新鲜" if fresh is not None else "已过期",
        "places": len(hit.places),
        "evidences": len(hit.evidences),
        "social": sum(1 for item in hit.evidences if sessions.is_social_evidence(item)),
        "queries": len(hit.social_queries),
        "age": (hit.updated_at or "-")[:19],
    }


def _print_row(info: dict[str, object]) -> None:
    social = int(info["social"])
    # 只在"确实有数据却没有社媒证据"时才提示；空城不算降级。
    flag = "  ← 无社媒证据" if int(info["places"]) and not social else ""
    print(
        f"  {str(info['city']):<4} {str(info['state']):<4}"
        f"  POI {int(info['places']):>4}  正文 {int(info['evidences']):>3}"
        f"（社媒 {social:>2}）  检索词 {int(info['queries']):>3}  最早 {info['age']}{flag}"
    )


def _skip_reason(info: dict[str, object], *, force: bool) -> str | None:
    """该不该跳过这座城市；返回**跳过理由**，返回 None 表示要跑。

    注意返回值的方向：返回字符串 = 跳过，返回 None = 执行。这个约定加下面的单测
    一起用，避免再写出"把空的也跳过"这种反了的判断。
    """

    if force:
        return None
    if info["state"] != "新鲜":
        return None  # 空 / 已过期 → 必须跑
    if int(info["social"]) == 0:
        # 新鲜但没有社媒证据 = 上一次很可能是限流期间跑的，应该再试一次。
        return None
    return "缓存新鲜且社媒证据齐全"


def main() -> int:
    parser = argparse.ArgumentParser(description="为热门城市预热城市知识库")
    parser.add_argument("--cities", nargs="*", default=list(DEFAULT_CITIES), help="要预热的城市")
    parser.add_argument("--force", action="store_true", help="已新鲜的城市也重跑")
    parser.add_argument("--list", action="store_true", help="只报告现状，不跑 Provider")
    parser.add_argument(
        "--allow-sqlite",
        action="store_true",
        help="允许写到本地 SQLite（默认拒绝：预热的意义是写共享库）",
    )
    args = parser.parse_args()

    store = TravelPlanStore()
    if store.backend_name != "postgres" and not args.allow_sqlite:
        print(
            f"拒绝执行：当前库是 {store.backend_name}（{store.describe()}），不是共享的 Postgres。\n"
            "预热必须写所有人共用的库，否则只暖了你自己这一台。\n"
            "确认要走本地库时加 --allow-sqlite。",
            file=sys.stderr,
        )
        return 1

    cities = [city.strip() for city in args.cities if city and city.strip()]
    if not cities:
        print("没有要预热城市", file=sys.stderr)
        return 1

    print(f"库：{store.describe()}")
    print(f"预热 {len(cities)} 座城市：{'、'.join(cities)}")
    print("\n预热前：")
    for city in cities:
        _print_row(_row(city, store=store))

    if args.list:
        return 0

    print()
    failed: list[str] = []
    degraded: list[str] = []
    started = time.perf_counter()
    for index, city in enumerate(cities, start=1):
        before = _row(city, store=store)
        skip = _skip_reason(before, force=args.force)
        if skip is not None:
            print(f"[{index}/{len(cities)}] {city}：跳过（{skip}）")
            continue
        print(f"[{index}/{len(cities)}] {city}：开始预热……", flush=True)
        began = time.perf_counter()
        try:
            summary = sessions.refresh_city_cache(store, city)
        except Exception as exc:  # noqa: BLE001 —— 一座城市失败不该中断整批
            took = time.perf_counter() - began
            failed.append(city)
            print(f"    失败（{took:.0f}s）：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        took = time.perf_counter() - began
        places = int(summary.get("places") or 0)
        evidences = int(summary.get("evidences") or 0)
        social = int(summary.get("social_evidences") or 0)
        if not places:
            # 没有 POI 就不算命中（`read_candidates` 的口径），等于白跑，必须如实报出来。
            failed.append(city)
            print(f"    未落库：{took:.0f}s 跑完但 POI 为 0", file=sys.stderr, flush=True)
            continue
        if social:
            print(
                f"    完成（{took:.0f}s）：POI {places}，正文 {evidences}（社媒 {social}），"
                f"检索词 {int(summary.get('queries') or 0)}",
                flush=True,
            )
            continue
        degraded.append(city)
        print(
            f"    降级（{took:.0f}s）：POI {places}，正文 {evidences}，**社媒证据 0 条** —— "
            f"缓存可用了但没有小红书/抖音内容",
            flush=True,
        )
        for note in summary.get("notes") or []:
            print(f"      · {note}", flush=True)

    print(f"\n预热后（总耗时 {time.perf_counter() - started:.0f}s）：")
    for city in cities:
        _print_row(_row(city, store=store))

    if failed:
        print(f"\n{len(failed)} 座城市未成功：{'、'.join(failed)}", file=sys.stderr)
    if degraded:
        print(
            f"\n注意：{len(degraded)} 座城市是**降级**结果（{'、'.join(degraded)}）——"
            "缓存里没有社媒证据。\n"
            "  通常原因是 tikhub 限流（HTTP 429）+ mediacrawler 本地不可用，只剩网页搜索。\n"
            "  等社媒恢复后重跑即可覆盖（本脚本默认会重跑这些城市，不必加 --force）：\n"
            "      python scripts/preheat_cities.py --cities " + " ".join(degraded),
            file=sys.stderr,
        )
    if failed:
        return 1
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
