"""测试环境的全局约定。

这里只做一件关键的事：**把第三方密钥从环境里摘掉**。

为什么必须做：`app.providers` 会经 super_harness 间接执行 `load_dotenv()`，于是开发机
根目录的 `.env`（含 JEV / 模型 / 高德 / 途牛 / TikHub 的真实密钥）会被自动加载。后果是
一次没显式注入替身的 run 会**真的**去打第三方接口 —— 测试变得不可重复、依赖额度与网络，
而且 Jev 这类计费服务会被单测反复消耗。

摘掉之后，任何没注入替身的路径都会走"未配置 → 降级/SKIPPED"的确定性分支；
需要验证"配置已存在"的用例自己用 monkeypatch 设值，作用域清晰且会还原。
"""

from __future__ import annotations

import os

import pytest

#: 测试期间必须缺席的 Secret。只列会触发外部调用/外部写入的那些。
#: `DATABASE_URL` 也在列：若带着它跑测试，默认构造的 `TravelPlanStore()` 会去连真
#: Neon 并在那里建表 —— 测试既依赖网络，又会污染线上库。测试一律用 tmp_path SQLite。
OFFLINE_SECRETS = (
    "JEV_API_KEY",
    "TYPESAFE_API_KEY",
    "MODEL_API_KEY",
    "AMAP_API_KEY",
    "TIKHUB_API_TOKEN",
    "TUNIU_API_KEY",
    "TAVILY_API_KEY",
    "TRAVELPLAN_ADMIN_TOKEN",
    "DATABASE_URL",
)


@pytest.fixture(autouse=True, scope="session")
def offline_environment():
    """整个测试会话期间保证第三方密钥缺席，结束后原样还原。"""

    saved = {name: os.environ.pop(name, None) for name in OFFLINE_SECRETS}
    yield
    for name, value in saved.items():
        if value is not None:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def no_paid_jev_calls(monkeypatch):
    """即使某个用例自己设置了 JEV_API_KEY，也不能让它变成一次真实调用。

    真正需要验证"有 Key 时会发请求"的用例，注入的是假 HTTP client（见 test_jev.py），
    所以这里再关一层开关不会削弱任何断言。
    """

    monkeypatch.setenv("JEV_MAX_CALLS_PER_RUN", os.environ.get("JEV_MAX_CALLS_PER_RUN", "5"))
    yield
