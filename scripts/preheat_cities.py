"""为热门城市预热城市知识库（攻略正文 + 提及 + POI + 检索词）。

为什么需要它
------------
城市知识库的**读路径**已经做完（`docs/architecture/DATA_REUSE_ENTITY.md`）：命中之后
一个社媒 Provider 都不打、一次地点抽取都不调。但**填充**仍然只能"等第一个用户来踩"——
那位用户要把最贵的社媒检索 + 模型抽取全价付掉，而且一路压在 512Mi 的单实例上
（`docs/operations/DEPLOYMENT.md` §7 记过 OOM 与健康检查超时）。

预热就是把这段挪到离线：提前把行写进 `city_pois` / `city_poi_mentions` /
`city_evidences` / `city_cache_meta`，之后所有去同一座城市的会话都走命中路径。

为什么不在 Render 上跑
----------------------
线上是 512Mi 单实例，预热一跑就跟健康检查抢 CPU 与数据库连接，会把正在服务的请求
拖死。所以预热**只在本地 / CI 跑**，只要求写同一个库（Neon）。

跑的是什么
----------
每座城市内部跑一次**预热 Agent**（`app/preheat_agent.py`）：代码给定城市，Agent 自己在循环里
决定搜哪些攻略（小红书 / 抖音 / 网页）、从正文里抽哪些地点、哪些点要到高德核实、把什么入库。
入库仍然只走已有两条路径（`app/places.py` 实体归一化 + `app/city_cache.py` 的城市缓存四张表），
所以缓存结构与实时路径完全一致，读路径（`sessions` 的城市缓存命中）不需要任何改动。

**批调度仍然归代码**：跑哪些城、串行、每天限量、跳过判定、退出码都在本脚本 —— Agent 管不了
"今天跑几座城"。机酒价格与时刻表**永远不会**进缓存（预热工具集里根本没有那些工具，这是设计不变量）。

一个必须盯住的降级：社媒限流
----------------------------
`tikhub` 的免费额度很容易打完（先是 HTTP 429 限流，然后是 `FREE_CREDIT_EXHAUSTED`）。
这时社媒那条线一条证据都拿不到，只剩网页搜索，但流程仍然"成功"——缓存里 POI 有、正文有，
只是**没有任何小红书 / 抖音证据**。这种结果会被后续会话当成"攻略已就绪"吃满一个 TTL，
所以本脚本把它单独标成**降级**并打印原因。

默认**不重跑**降级城市（社媒不可用时重跑也是白跑，还会白烧网页搜索与高德的配额）。
要补社媒得显式加 `--retry-degraded`：判断"什么时候值得重试"是人的判断，脚本不替用户猜。

用法
----
    python scripts/preheat_cities.py                    # 默认 53 个城市；只补空/过期的
    python scripts/preheat_cities.py --daily            # 定时模式：每天最多 15 座，按天轮转起点
    python scripts/preheat_cities.py --daily --limit 30
    python scripts/preheat_cities.py --cities 成都 重庆
    python scripts/preheat_cities.py --retry-degraded   # 社媒恢复后，把无社媒的城市补回来
    python scripts/preheat_cities.py --force            # 全部重跑
    python scripts/preheat_cities.py --list             # 只看现状，不跑

定时模式（--daily）
------------------
由定时任务（每天凌晨 3 点，见 `.github/workflows/preheat-daily.yml`）调用：

* 每天最多**真正尝试**预热 `--limit` 座城市（默认 15），到额度就停 —— 不会一次烧光配额；
* 起点按当天是一年中的第几天轮转（`day_of_year % len(cities)`），失败/卡住的城市
  不会天天排在队首挡住后面的城市，隔几天自然把每座城都轮一遍；
* 因"新鲜 / 未过期"跳过的城市**不占**额度，只有真的调了 `preheat_agent.preheat_city` 才算；
* 与 `--force` 互斥：定时任务不该强制重跑全部城市，两个一起给会直接报错。

退出码：0 = 全部成功 / daily 正常截断到额度 / **部分城市失败但其余已落库**（降级不算失败，
但会打印警告）；1 = 所有尝试预热的城市都未落库（额度耗尽 / 数据源全挂 / 代码故障）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from app import city_cache, preheat_agent, sessions  # noqa: E402
from app.store import TravelPlanStore  # noqa: E402

#: `.env` 的位置。**不在 import 时加载** —— 见 `main()` 里的说明。
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

#: 默认预热清单（53 个）。覆盖全国热门 + 各地域，按"被搜得多"排序，命中率比覆盖面重要。
#: 除了常规热门，刻意放了几个中小城市（泉州 / 景德镇 / 开封 / 邯郸），用来证明
#: 这条预热链路不是只对一线城市有效。
DEFAULT_CITIES = (
    # 第一梯队：一线 + 顶级流量（被搜得最多）
    "北京", "上海", "广州", "深圳",
    "成都", "重庆", "西安", "杭州",
    # 长三角
    "南京", "苏州", "无锡", "宁波", "温州",
    # 京津冀 / 华北 / 山东半岛
    "天津", "青岛", "烟台", "威海", "济南", "郑州",
    # 东北
    "沈阳", "大连", "哈尔滨", "长春",
    # 华中
    "武汉", "长沙", "南昌", "合肥",
    # 广西 / 云贵：桂林山水、民族风情与高原游
    "桂林", "南宁", "柳州", "贵阳", "昆明", "丽江", "大理", "西双版纳",
    # 西北 / 西部边疆
    "兰州", "敦煌", "西宁", "银川", "乌鲁木齐", "拉萨", "呼和浩特",
    # 东南沿海
    "厦门", "福州", "泉州", "珠海",
    # 文化古城（含中小城市）
    "黄山", "景德镇", "洛阳", "开封", "邯郸", "太原", "石家庄",
)


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


def _skip_reason(info: dict[str, object], *, force: bool, retry_degraded: bool) -> str | None:
    """该不该跳过这座城市；返回**跳过理由**，返回 None 表示要跑。

    注意返回值的方向：返回字符串 = 跳过，返回 None = 执行。这个约定加下面的单测
    一起用，避免再写出"把空的也跳过"这种反了的判断。

    默认**不重跑**降级城市：社媒不可用（例如 TikHub 免费额度为 0）时重跑也是白跑，
    还会白烧网页搜索与高德的配额。要补社媒得显式加 `--retry-degraded` ——
    判断"什么时候值得重试"是人的判断，脚本不替用户猜。
    """

    if force:
        return None
    if info["state"] != "新鲜":
        return None  # 空 / 已过期 → 必须跑
    if int(info["social"]) == 0:
        if retry_degraded:
            return None
        return "缓存新鲜但无社媒证据（要重试加 --retry-degraded）"
    return "缓存新鲜且社媒证据齐全"


def _rotate_daily(cities: list[str]) -> list[str]:
    """`--daily`：按当天在一年中的第几天轮转起点。

    失败/卡住的城市如果天天排在队首，会一直挡着后面的城市。每天从不同起点开始、
    走到今天的额度就停，隔几天自然把每座城都轮一遍。非 daily 模式不调用本函数。
    """

    if not cities:
        return cities
    start = int(date.today().strftime("%j")) % len(cities)
    return cities[start:] + cities[:start]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为热门城市预热城市知识库")
    parser.add_argument("--cities", nargs="*", default=list(DEFAULT_CITIES), help="要预热的城市")
    parser.add_argument("--force", action="store_true", help="已新鲜的城市也重跑")
    parser.add_argument(
        "--retry-degraded",
        action="store_true",
        help="额外重跑「新鲜但没有社媒证据」的城市（社媒恢复后用这个补）",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="定时模式：每天最多 --limit 座城市，起点按天轮转（与 --force 互斥）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=15,
        help="每天最多尝试预热的城市数（仅 --daily 生效，默认 15）",
    )
    parser.add_argument("--list", action="store_true", help="只报告现状，不跑 Provider")
    parser.add_argument(
        "--allow-sqlite",
        action="store_true",
        help="允许写到本地 SQLite（默认拒绝：预热的意义是写共享库）",
    )
    args = parser.parse_args(argv)

    # `.env` 只在这里加载，不在 import 时加载：这个模块会被测试 import（`tests/test_preheat_cities.py`），
    # 而 `load_dotenv()` 会往 `os.environ` 里灌值 —— 那样同一进程里**后面跑的其它测试**会
    # 悄悄读到 .env 的配置（实测会把 `tests/test_workflow.py` 的"模型只有 6 个调用点"断言打破）。
    # 副作用只该发生在真正执行的时候。
    load_dotenv(_ENV_PATH)

    if args.daily and args.force:
        print(
            "--force 与 --daily 互斥：定时任务不该强制重跑全部城市。\n"
            "去掉 --force（--daily 每天只跑 --limit 座城市）。",
            file=sys.stderr,
        )
        return 1
    if args.daily and args.limit < 1:
        print("--limit 至少要为 1", file=sys.stderr)
        return 1

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
    if args.daily:
        cities = _rotate_daily(cities)
        print(f"定时模式：起点按 day-of-year 轮转，最多尝试 {args.limit} 座。")

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
    attempted = 0
    limit = args.limit if args.daily else len(cities)
    started = time.perf_counter()
    for index, city in enumerate(cities, start=1):
        if attempted >= limit:
            # 只有 --daily 会把 limit 设成小于城市总数；到额度就停，正常截断不算失败。
            if args.daily:
                print(f"    已达今日额度 {limit} 座，停止（其余留到后续轮次）。", flush=True)
            break
        before = _row(city, store=store)
        skip = _skip_reason(before, force=args.force, retry_degraded=args.retry_degraded)
        if skip is not None:
            print(f"[{index}/{len(cities)}] {city}：跳过（{skip}）")
            continue
        attempted += 1
        print(f"[{index}/{len(cities)}] {city}：开始预热（Agent 自己决定搜什么、入库什么）……", flush=True)
        began = time.perf_counter()
        try:
            summary = preheat_agent.preheat_city(store, city)
        except Exception as exc:  # noqa: BLE001 —— 一座城市失败不该中断整批
            took = time.perf_counter() - began
            failed.append(city)
            print(f"    失败（{took:.0f}s）：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        took = time.perf_counter() - began
        places = int(summary.get("places") or 0)
        evidences = int(summary.get("evidences") or 0)
        social = int(summary.get("social_evidences") or 0)
        agent_info = summary.get("agent") or {}
        if not places:
            # 没有 POI 就不算命中（`read_candidates` 的口径），等于白跑，必须如实报出来 ——
            # 连"Agent 跑了什么、哪一步空了"一起打印，否则这条失败无从复盘。
            failed.append(city)
            print(
                f"    未落库：{took:.0f}s 跑完但 POI 为 0"
                f"（工具调用 {agent_info.get('provider_calls', '?')} 次，"
                f"检索 {agent_info.get('searches', '?')} 次）",
                file=sys.stderr,
                flush=True,
            )
            for note in summary.get("notes") or []:
                print(f"      · {note}", file=sys.stderr, flush=True)
            continue
        if social:
            print(
                f"    完成（{took:.0f}s）：POI {places}，正文 {evidences}（社媒 {social}），"
                f"检索词 {int(summary.get('queries') or 0)}，"
                f"工具调用 {agent_info.get('provider_calls', '?')} 次",
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
            "  常见原因是 tikhub 免费额度耗尽 / 限流，且 mediacrawler 本地不可用，只剩网页搜索。\n"
            "  社媒恢复后补回来（默认不会自动重跑降级城市）：\n"
            "      python scripts/preheat_cities.py --retry-degraded --cities " + " ".join(degraded),
            file=sys.stderr,
        )
    # 退出码语义：个别城市失败不该让每天的定时任务（GitHub Actions）报红、发邮件刷屏。
    # 只要"至少有一座城市落库"就算部分成功（退出 0，失败明细仍打在 stderr 可复盘）；
    # 只有**所有尝试预热的城市都未落库**（额度耗尽 / 数据源全挂 / 代码故障）才退出 1。
    landed = attempted - len(failed)
    if failed and landed == 0:
        print(
            "本次尝试预热的城市**全部**未落库——额度耗尽 / 数据源不可用 / 代码故障，"
            "按失败处理。",
            file=sys.stderr,
        )
        return 1
    if failed:
        print(
            f"\n{len(failed)} 座城市未成功，但其余 {landed} 座已正常落库——按部分成功处理"
            "（失败城市明细见上，日志可复盘）。",
            file=sys.stderr,
        )
    print("\n全部完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
