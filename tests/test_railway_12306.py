"""railway_12306 MCP Server Spec 的单元测试。

只验证 spec 的形状（name / description / transport / command），**不 spawn 子进程、
不发任何请求** —— 创建 spec 本身就该是完全离线的（懒连接语义）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from superharness.capabilities import MCPServerSpec
from superharness.capabilities.mcp import railway_12306_http_server, railway_12306_server
from superharness.capabilities.mcp.railway_12306 import (
    DEFAULT_SERVER_DESCRIPTION,
    DEFAULT_STDIO_ARGS,
    DEFAULT_STDIO_COMMAND,
    ENV_COMMAND,
    ENV_URL,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """清掉可能从 .env / 机器继承来的覆盖项，保证默认值可断言。"""
    monkeypatch.delenv(ENV_COMMAND, raising=False)
    monkeypatch.delenv(ENV_URL, raising=False)


@pytest.fixture(autouse=True)
def _forbid_process(monkeypatch):
    """spec 工厂不允许起进程：一 spawn 就让测试炸。"""
    def _boom(*args, **kwargs):  # pragma: no cover - 只在违规时触发
        raise AssertionError("创建 MCP ServerSpec 不应该启动任何进程")

    monkeypatch.setattr(subprocess, "Popen", _boom)


class TestStdioServer:
    def test_spec_shape(self):
        spec = railway_12306_server()

        assert isinstance(spec, MCPServerSpec)
        assert spec.name == "railway_12306"
        assert spec.description == DEFAULT_SERVER_DESCRIPTION
        assert spec.connection["transport"] == "stdio"
        assert spec.connection["args"] == ["-y", "12306-mcp"]
        assert isinstance(spec.connection["command"], str)
        assert spec.connection["command"]

    def test_description_reads_like_user_speech(self):
        """会被 MCP Server Router 拿去 BM25，必须像用户说的话。"""
        description = railway_12306_server().description

        for keyword in ("12306", "火车票", "车次", "余票", "中转"):
            assert keyword in description

    def test_command_is_resolved_to_absolute_path_on_windows(self):
        """npx 在 Windows 上是 npx.cmd，必须 shutil.which 解析（anyio 不过 shell）。"""
        command = railway_12306_server().connection["command"]

        assert command.lower().endswith((".cmd", ".bat", ".exe")) or Path(command).exists()

    def test_args_are_a_fresh_list(self):
        """每次调用返回独立的 list，避免调用方改到共享的可变默认值。"""
        args = railway_12306_server().connection["args"]
        args.append("--tampered")

        assert railway_12306_server().connection["args"] == ["-y", "12306-mcp"]

    def test_command_override_by_argument(self, monkeypatch):
        spec = railway_12306_server(command="C:/fake/npx.cmd")
        assert spec.connection["command"] == "C:/fake/npx.cmd"

    def test_command_override_by_env(self, monkeypatch):
        monkeypatch.setenv(ENV_COMMAND, "C:/env/npx.cmd")

        spec = railway_12306_server()

        assert spec.connection["command"] == "C:/env/npx.cmd"

    def test_command_argument_beats_env(self, monkeypatch):
        monkeypatch.setenv(ENV_COMMAND, "C:/env/npx.cmd")

        spec = railway_12306_server(command="C:/arg/npx.cmd")

        assert spec.connection["command"] == "C:/arg/npx.cmd"

    def test_args_and_name_and_description_override(self):
        spec = railway_12306_server(
            args=["-y", "12306-mcp", "--port", "3000"],
            name="railway",
            description="自定义描述",
        )

        assert spec.connection["args"] == ["-y", "12306-mcp", "--port", "3000"]
        assert spec.name == "railway"
        assert spec.description == "自定义描述"

    def test_no_credentials_needed(self):
        """12306 链路不需要任何 Secret：connection 里不应出现 key / token 字段。"""
        connection = railway_12306_server().connection

        assert set(connection) == {"transport", "command", "args"}
        for key in connection:
            assert "key" not in key.lower()
            assert "token" not in key.lower()

    def test_defaults_are_the_documented_ones(self):
        assert DEFAULT_STDIO_COMMAND == "npx"
        assert tuple(DEFAULT_STDIO_ARGS) == ("-y", "12306-mcp")


class TestHttpServer:
    def test_requires_url(self):
        with pytest.raises(ValueError, match="RAILWAY_12306_URL|HTTP 端点"):
            railway_12306_http_server()

    def test_spec_shape_with_explicit_url(self):
        spec = railway_12306_http_server("http://localhost:3000/mcp")

        assert spec.name == "railway_12306"
        assert spec.description == DEFAULT_SERVER_DESCRIPTION
        assert spec.connection["transport"] == "streamable_http"
        assert spec.connection["url"] == "http://localhost:3000/mcp"
        assert spec.connection["timeout"] == 30.0

    def test_url_from_env(self, monkeypatch):
        monkeypatch.setenv(ENV_URL, "http://localhost:9999/mcp")

        spec = railway_12306_http_server()

        assert spec.connection["url"] == "http://localhost:9999/mcp"


class TestPackageExport:
    def test_importable_from_package_namespace(self):
        from superharness.capabilities.mcp import (  # noqa: F401
            railway_12306_http_server as http_factory,
            railway_12306_server as stdio_factory,
        )

        assert stdio_factory().name == "railway_12306"
        assert http_factory("http://localhost:3000/mcp").name == "railway_12306"

    def test_router_capability_from_spec(self):
        """能被 CapabilityRouter 当成候选（description 进 BM25）。"""
        from superharness.capabilities import Capability

        spec = railway_12306_server()
        capability = Capability(
            id=f"mcp:{spec.name}",
            kind="mcp-server",
            name=spec.name,
            description=spec.description,
            payload=spec,
        )

        assert capability.id == "mcp:railway_12306"
        assert capability.kind == "mcp-server"
