"""预热脚本的跳过判定与 `--daily` 定时模式（`scripts/preheat_cities.py`）。

三块内容：

* 跳过判定（`_skip_reason`）：它是"要不要再花 2~3 分钟 + 一份 Provider 配额重跑一座
  城市"的唯一判据，判反两个方向都很糟 —— 该跑的不跑（空城永远填不上、降级永远停在
  降级）、每次都重跑（白烧配额，这正是限流本身的一部分原因）。接口约定容易写反
  （返回**跳过理由**、返回 `None` 表示执行），所以四种组合都钉住。

* `--daily` 定时模式：每天最多真正尝试 `--limit` 座城市（默认 15）、起点按
  day-of-year 轮转、跳过的城市不占额度、与 `--force` 互斥。

* 失败语义：一座城市失败（抛错 / 没落库）不该中断整批；退出码区分"部分成功"与"真故障"——
  只要至少一座城市落库就算部分成功（退出 0，失败明细打 stderr 可复盘），只有**所有尝试的
  城市都未落库**（额度耗尽 / 数据源全挂 / 代码故障）才退出 1。这条让每天定时任务不会因为
  个别城市失败就发失败邮件刷屏。

这些用例全部用替身跑 `main()` —— monkeypatch 掉 `load_dotenv` / `TravelPlanStore` /
`_row` / `preheat_agent.preheat_city`，不真连库、不真调 Provider、不真起 Agent。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def preheat():
    spec = importlib.util.spec_from_file_location(
        "preheat_cities", ROOT / "scripts" / "preheat_cities.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _city(state: str, *, places: int = 20, social: int = 5) -> dict[str, object]:
    return {"state": state, "places": places, "social": social}


@pytest.mark.parametrize(
    ("info", "force", "retry_degraded", "why"),
    [
        # 没有任何数据的城市必须跑。
        (_city("空", places=0, social=0), False, False, "空城"),
        # 过期的必须跑（惰性 TTL 只判断新鲜度，不会自己去补）。
        (_city("已过期", social=5), False, False, "已过期"),
        # --force 一律重跑。
        (_city("新鲜", social=5), True, False, "--force"),
        # 降级城市：默认不重跑（社媒不可用时重跑是白跑），显式要求时重跑。
        (_city("新鲜", social=0), False, True, "--retry-degraded"),
    ],
)
def test_should_run(preheat, info, force, retry_degraded, why):
    assert preheat._skip_reason(info, force=force, retry_degraded=retry_degraded) is None, why


def test_should_skip_when_fresh_and_social_present(preheat):
    reason = preheat._skip_reason(_city("新鲜", social=5), force=False, retry_degraded=False)
    assert reason, "新鲜且社媒证据齐全时不该白跑一遍"


def test_degraded_city_is_skipped_by_default(preheat):
    """关键取舍：社媒不可用时（TikHub 额度为 0），默认重跑只会白烧网页与高德配额。"""

    reason = preheat._skip_reason(_city("新鲜", social=0), force=False, retry_degraded=False)
    assert reason, "默认不该重跑降级城市"
    assert "--retry-degraded" in reason, "跳过时要告诉用户怎么补社媒"


# ---------------------------------------------------------------- --daily 替身设施

class _FakeStore:
    """只被 `store.describe()` / `store.backend_name` 用到；`_row` 已被替身化。"""

    backend_name = "postgres"

    def describe(self) -> str:
        return "postgres:测试替身"


def _fixed_date(doy: int):
    """让 `date.today().strftime("%j")` 固定返回 doy（1~366）。"""

    class _FakeDate:
        @classmethod
        def today(cls):
            return _FakeDate()

        def strftime(self, fmt: str) -> str:
            assert fmt == "%j"
            return f"{doy:03d}"

    return _FakeDate


def _run_main(
    preheat,
    monkeypatch,
    argv,
    *,
    states: dict[str, str],
    call_log: list[str],
    fail: set[str] | None = None,
    empty: set[str] | None = None,
) -> int:
    """替身化跑 `main()`：不连库、不调 Provider、不起预热 Agent，只记录预热调用序列。

    `states` 决定每座城市 `_row` 返回的 state（缺省"空"= 会真跑）；`call_log` 按序记录
    真正调了 `preheat_agent.preheat_city` 的城市；`fail` 里的城市抛异常（Agent 失败），
    `empty` 里的城市返回 POI 为 0 的摘要（跑完但没落库）。
    """

    monkeypatch.setattr(preheat, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(preheat, "TravelPlanStore", _FakeStore)
    monkeypatch.setattr(
        preheat,
        "_row",
        lambda city, *, store: {
            "city": city,
            "state": states.get(city, "空"),
            "places": 20,
            "evidences": 10,
            "social": 5,
            "queries": 3,
            "age": "-",
        },
    )

    def _fake_preheat_city(store, city):
        call_log.append(city)
        if fail and city in fail:
            raise preheat.preheat_agent.PreheatAgentError(f"测试替身：{city} 的 Agent 失败")
        if empty and city in empty:
            return {
                "city": city, "places": 0, "evidences": 0, "social_evidences": 0,
                "queries": 0, "notes": ["本轮没有任何通过校验的高德 POI，未写入城市缓存"],
                "degradations": ["没有写入城市缓存"], "agent": {"provider_calls": 4, "searches": 2},
            }
        return {
            "city": city,
            "places": 5,
            "evidences": 3,
            "social_evidences": 2,
            "queries": 1,
            "notes": [],
            "agent": {"provider_calls": 9, "searches": 3},
        }

    monkeypatch.setattr(preheat.preheat_agent, "preheat_city", _fake_preheat_city)
    return preheat.main(argv)


# ------------------------------------------------------------ --daily 定时模式用例

def test_daily_rotation_and_truncation(preheat, monkeypatch):
    """day-of-year 决定轮转起点；串行、达到 limit 即停；跳过的城市不占额度。"""

    call_log: list[str] = []
    monkeypatch.setattr(preheat, "date", _fixed_date(2))  # doy=2 → 2 % 5 = 2
    cities = ["北京", "上海", "广州", "深圳", "成都"]
    states = {"深圳": "新鲜"}  # 只有深圳新鲜（跳过），其余是空城（会真跑）
    rc = _run_main(
        preheat, monkeypatch,
        ["--daily", "--limit", "2", "--cities", *cities],
        states=states, call_log=call_log,
    )
    # 轮转后顺序：广州、深圳、成都、北京、上海
    # 广州跑(1) → 深圳跳过(不占额度) → 成都跑(2，达额度) → 北京停
    assert call_log == ["广州", "成都"], f"调用序列应为轮转后前 2 个真跑的，实际 {call_log}"
    assert len(call_log) == 2
    assert rc == 0, "daily 正常截断到额度算成功"


def test_daily_rotation_start_depends_on_day_of_year(preheat, monkeypatch):
    """`start = day_of_year % len(cities)`：不同日期轮转起点不同，整数倍回原点。"""

    cities = ["a", "b", "c", "d", "e"]
    monkeypatch.setattr(preheat, "date", _fixed_date(2))  # 2 % 5 = 2
    assert preheat._rotate_daily(cities) == ["c", "d", "e", "a", "b"]
    monkeypatch.setattr(preheat, "date", _fixed_date(5))  # 5 % 5 = 0 → 保持原顺序
    assert preheat._rotate_daily(cities) == ["a", "b", "c", "d", "e"]


def test_skipped_cities_do_not_count_toward_limit(preheat, monkeypatch):
    """跳过的不占额度：6 座里 3 座新鲜被跳过，limit=3 时 3 个真跑的都被跑完。"""

    call_log: list[str] = []
    monkeypatch.setattr(preheat, "date", _fixed_date(1))  # 1 % 6 = 1 → 从第 2 座开始
    cities = ["A", "B", "C", "D", "E", "F"]
    states = {"B": "新鲜", "D": "新鲜", "F": "新鲜"}
    rc = _run_main(
        preheat, monkeypatch,
        ["--daily", "--limit", "3", "--cities", *cities],
        states=states, call_log=call_log,
    )
    # 轮转后：B, C, D, E, F, A
    # B 跳过、C 跑(1)、D 跳过、E 跑(2)、F 跳过、A 跑(3) → 达额度
    assert call_log == ["C", "E", "A"], f"跳过的城市不该占额度，实际 {call_log}"
    assert rc == 0


def test_non_daily_keeps_original_order_and_runs_all(preheat, monkeypatch):
    """非 daily 模式：不轮转、不截断，保持原顺序全部跑完（回归保护）。"""

    call_log: list[str] = []
    cities = ["a", "b", "c"]
    rc = _run_main(
        preheat, monkeypatch,
        ["--cities", *cities],
        states={}, call_log=call_log,
    )
    assert call_log == ["a", "b", "c"]
    assert rc == 0


def test_force_and_daily_are_mutually_exclusive(preheat, monkeypatch, capsys):
    """`--force` 与 `--daily` 互斥：同时给直接报错返回 1，一座都不跑。"""

    call_log: list[str] = []
    rc = _run_main(
        preheat, monkeypatch,
        ["--daily", "--force", "--cities", "北京"],
        states={}, call_log=call_log,
    )
    assert rc == 1
    assert call_log == [], "互斥报错时不该真的跑任何城市"
    assert "互斥" in capsys.readouterr().err, "报错信息要说明互斥"


def test_partial_failure_is_partial_success_and_batch_continues(preheat, monkeypatch, capsys):
    """一座城市失败但其余落库：整批跑完、退出码 0（部分成功，不让 CI 判失败刷邮件）。"""

    call_log: list[str] = []
    cities = ["a", "b", "c"]
    rc = _run_main(
        preheat, monkeypatch,
        ["--cities", *cities],
        states={}, call_log=call_log, fail={"b"},
    )
    assert call_log == ["a", "b", "c"], "一座城市失败不该中断整批"
    assert rc == 0, "只要还有城市落库就算部分成功，退出码必须是 0（避免 CI 天天判失败）"
    err = capsys.readouterr().err
    assert "1 座城市未成功：b" in err
    assert "测试替身：b 的 Agent 失败" in err, "失败原因要如实打出来，不能只说'失败'"
    assert "部分成功" in err, "部分成功要明说，失败明细仍留在日志里可复盘"


def test_all_attempted_cities_failed_returns_1(preheat, monkeypatch, capsys):
    """所有尝试的城市都未落库（额度/数据源/代码故障）→ 退出码 1，才让 CI 判失败。"""

    call_log: list[str] = []
    rc = _run_main(
        preheat, monkeypatch,
        ["--cities", "a", "b"],
        states={}, call_log=call_log, fail={"a", "b"},
    )
    assert call_log == ["a", "b"]
    assert rc == 1, "全部未落库 = 真故障，必须退出码 1"
    assert "全部" in capsys.readouterr().err


def test_city_without_pois_is_a_failure_and_prints_agent_notes(preheat, monkeypatch, capsys):
    """跑完但 POI 为 0 = 未落库 = 失败，且要把 Agent 侧的说明打出来（便于复盘）。"""

    call_log: list[str] = []
    rc = _run_main(
        preheat, monkeypatch,
        ["--cities", "成都"],
        states={}, call_log=call_log, empty={"成都"},
    )
    assert call_log == ["成都"]
    assert rc == 1, "没有 POI 的城市按 read_candidates 的口径不算命中，必须算失败"
    err = capsys.readouterr().err
    assert "跑完但 POI 为 0" in err
    assert "本轮没有任何通过校验的高德 POI" in err, "失败时要打印 Agent 的 notes"


# ------------------------------------------------------------ 默认城市清单

def test_default_cities_cover_regions_without_duplicates(preheat):
    """清单扩到 45 个左右：长度 ≥ 40、无重复、关键城市与地域都要在。"""

    cities = preheat.DEFAULT_CITIES
    assert len(cities) >= 40, f"默认清单要扩到 45 个左右，实际 {len(cities)}"
    assert len(set(cities)) == len(cities), "默认清单不能有重复城市"
    for name in (
        "北京", "上海", "广州", "深圳", "成都", "重庆", "西安", "杭州",  # 一线 + 顶级流量
        "南京", "苏州", "武汉", "哈尔滨", "昆明", "厦门",               # 各地域代表
        "乌鲁木齐", "拉萨", "呼和浩特",                                  # 西部边疆
    ):
        assert name in cities, f"默认清单缺城市：{name}"
