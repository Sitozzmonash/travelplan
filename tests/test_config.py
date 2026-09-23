"""`app/config.py` 的解析与可编辑键约束。

配置层大多数改动是「加一个环境变量 + 一个 `EDITABLE_KEYS` 条目」。这里用一组小用例
把「新键要出现在哪几处、默认值与边界是什么」钉死，避免下一次加键时漏掉 `from_env`
或 `EDITABLE_KEYS` 中的任意一处；同时验证 `app/agent_runner` 模块顶层的 harness 瘦身
patch（关 memory/retrieval/log/trace，幂等、全局）。
"""

from __future__ import annotations

import pytest

from app.config import EDITABLE_KEYS, current_config, validate_override


class TestRunWorkers:
    """`MAX_RUN_WORKERS`：API 后台执行器并发，默认 2（Render 免费档瘦身结论）。"""

    def test_default_is_two(self) -> None:
        assert current_config().run_workers == 2

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_RUN_WORKERS", "5")
        assert current_config().run_workers == 5

    def test_invalid_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_RUN_WORKERS", "很多")
        assert current_config().run_workers == 2

    def test_editable_keys_declare_the_key_and_bounds(self) -> None:
        assert EDITABLE_KEYS["MAX_RUN_WORKERS"] == ("int", 1, 8)

    def test_validate_override_enforces_bounds(self) -> None:
        assert validate_override("MAX_RUN_WORKERS", 2) == 2
        assert validate_override("MAX_RUN_WORKERS", "8") == 8
        with pytest.raises(ValueError):
            validate_override("MAX_RUN_WORKERS", 0)
        with pytest.raises(ValueError):
            validate_override("MAX_RUN_WORKERS", 9)


class TestAgentRunnerHarnessPatch:
    """`app/agent_runner` 模块顶层的 harness 瘦身 patch。

    这个用例不依赖任何 harness 夹具（本文件也没有 autouse 的
    `isolated_harness_settings`），直接验证模块自己做的全局 patch：
    `import app.agent_runner` 后，`create_harness_agent` 装配时读到的
    `superharness.agent.settings` 已经关掉 memory/retrieval/log/trace。
    """

    def test_import_disables_memory_and_retrieval(self) -> None:
        from superharness import agent as harness_agent

        import app.agent_runner  # noqa: F401

        assert harness_agent.settings.memory_enabled is False
        assert harness_agent.settings.retrieval_enabled is False
        assert harness_agent.settings.log_enabled is False
        assert harness_agent.settings.trace_enabled is False

    def test_patch_is_idempotent(self) -> None:
        """重复调用 patch 不会换一个新的 settings 对象，也不会把状态改回去。"""

        from superharness import agent as harness_agent

        import app.agent_runner

        first = harness_agent.settings
        app.agent_runner._disable_harness_memory_and_logs()
        assert harness_agent.settings is first
        assert harness_agent.settings.memory_enabled is False
        assert harness_agent.settings.retrieval_enabled is False
