"""测试环境的全局约定。

这里只做一件关键的事：**把第三方密钥从环境里摘掉**。

为什么必须做：`app.providers` 会经 super_harness 间接执行 `load_dotenv()`，于是开发机
根目录的 `.env`（含模型 / 高德 / 途牛 / TikHub 的真实密钥）会被自动加载。后果是
一次没显式注入替身的 run 会**真的**去打第三方接口 —— 测试变得不可重复、依赖额度与网络，
计费服务也会被单测反复消耗。

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


@pytest.fixture(autouse=True, scope="session")
def no_mcp_subprocesses():
    """测试期间不注册 MCP Server。

    为什么摘密钥还不够：12306 走 `npx -y 12306-mcp` 拉起子进程，**不需要任何密钥**。
    于是"未配置 → 降级"这条离线路径对它不适用：一个没显式注入假 Hub 的用例会真的
    联网，而 stdio 握手一旦不返回，`future.result()` 会一直等下去 —— 整套 pytest
    卡死在单个用例上（实测卡在 ~24% 超过半小时，且看不出是哪个用例）。
    关掉之后 12306 报告"未注册"，走的正是 conftest 想要的确定性降级分支。
    需要验证 MCP 装配本身的用例（如 test_agent_api）自己 delenv 打开。
    """

    from app.providers import DISABLE_MCP_ENV

    saved = os.environ.get(DISABLE_MCP_ENV)
    os.environ[DISABLE_MCP_ENV] = "1"
    yield
    if saved is None:
        os.environ.pop(DISABLE_MCP_ENV, None)
    else:
        os.environ[DISABLE_MCP_ENV] = saved


@pytest.fixture(autouse=True, scope="session")
def no_orphan_sweep_on_startup():
    """测试期间不跑"启动收敛孤儿 run"。

    那条逻辑会在 FastAPI 启动事件里把 `run_progress` 里所有 RUNNING 改成 CANCELLED。
    TestClient 会触发启动事件，而多数用例的 store 是替身（安全），但少数用例
    （比如验证未配 Token 时 503 的那条）用的是真 `get_store()` —— 它会写到开发库
    `data/travelplan.db`。测试不该顺手改别人的数据，所以整体关掉；
    需要验证收敛行为本身的用例自己 delenv 打开。
    """

    from app.api import SKIP_ORPHAN_SWEEP_ENV

    saved = os.environ.get(SKIP_ORPHAN_SWEEP_ENV)
    os.environ[SKIP_ORPHAN_SWEEP_ENV] = "1"
    yield
    if saved is None:
        os.environ.pop(SKIP_ORPHAN_SWEEP_ENV, None)
    else:
        os.environ[SKIP_ORPHAN_SWEEP_ENV] = saved
