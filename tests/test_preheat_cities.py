"""预热脚本的跳过判定（`scripts/preheat_cities.py::_skip_reason`）。

为什么单独测这几行：它是"要不要再花 2~3 分钟 + 一份 Provider 配额重跑一座城市"的唯一判据。
判反了两个方向都很糟 ——

* 该跑的不跑：空的城市永远填不上、降级的城市永远停在降级；
* 每次都重跑：白烧配额与时间（这正是限流本身的一部分原因）。

而它的接口约定本身就容易写反（返回**跳过理由**、返回 `None` 表示执行），所以这里
把四种组合都钉住，而不是只测一条。
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
