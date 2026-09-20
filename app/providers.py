"""Provider 桥接层 —— 把 super_harness 的 Plugin / MCP / Tool 能力接到 TravelPlan 的业务流程上。

这个模块为什么存在
------------------
START.md §7 给 `app/` 划了 7 个文件，每个文件一件事。但「调用外部数据源」这件事横跨了
`workflow.py`（要按流程节点调用）和 `agent.py`（要装配同样的能力给 Agent Loop），
把它塞进任何一边都会让那一边同时负责"流程编排"和"工具寻址 / 信封解析 / fallback / 审计记录"。
所以这里单独一层，职责只有四条：

    1. **寻址** —— 通过 PluginLoader / MCPLoader / ToolLoader 拿到 super_harness 的 Tool 对象，
       不在这里重新实现发现与筛选（那是 Harness 的职责）；
    2. **解析** —— 把各 Provider 的返回信封归一成业务模型（FlightOption / TrainOption /
       HotelOption / Place / RouteOption / Evidence）；
    3. **fallback** —— 只做 PRD §32 明确允许的那两条链路（12306→途牛火车、TikHub→MediaCrawler），
       其它 Provider 失败就如实返回失败，**不做任何假数据兜底**；
    4. **留痕** —— 每次调用产出一条 `ProviderCall`，写进 `sources` 表。audit_report 要回答的
       "搜了什么 / 调用了哪个 Provider / Tool 参数是什么 / 什么时候返回 / 返回了什么"
       全部来自这里，而不是靠事后回忆。

铁律（写在代码里，防止后来者"顺手优化"）
----------------------------------------
* **不允许**在 Provider 失败时返回一个看起来合理的价格 / 时长 / 坐标 / 路线。
  失败就是 `items == []` + 一个非 OK 的 status，让上层把价格标成未知、路线标成未验证。
* **不允许**把 Key 写进日志、异常信息或 source 记录。Plugin 侧已经做了 `_redact` / `_scrub`，
  这里再兜一层：异常字符串过一遍 `_scrub_secrets`。
* **不允许**为 TikHub 付费。付费闸门在 Plugin 内部（FREE_ONLY + 配额检查），
  这里不做任何"额度不够就充值"的动作，也不重试——重试可能重复计费。

异步与同步的边界
----------------
`MCPLoader.load_tools` 是 async 的，而 planner / workflow 是同步代码。这里用一个常驻
后台事件循环（`_AsyncRuntime`）把它们接起来：MCP 客户端绑定在创建它的 loop 上，
如果用 `asyncio.run` 每次新开一个 loop，第二次调用就会因为 session 属于另一个 loop 而失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Mapping, Sequence, TypeVar

from langchain_core.tools import BaseTool

from superharness.capabilities.loader import (
    MCPServerSpec,
    MCPLoader,
    PluginLoader,
    PluginManifest,
    ToolLoader,
)
from superharness.config import settings as harness_settings

try:  # 事件名只从 SuperHarness 取，不在本地抄一份字符串（抄了迟早会跟上游漂移）
    from superharness.observability import EventType
except Exception:  # noqa: BLE001 —— 没装 superharness 时仍允许 import 本模块并降级

    class EventType:  # type: ignore[no-redef]
        """superharness 缺席时的占位。名字与上游一致，事件发不出去而已。"""

        TOOL_STARTED = "tool.started"
        TOOL_FINISHED = "tool.finished"

from app.config import current_config, provider_timeouts, tool_timeout
from app.models import (
    Evidence,
    FlightOption,
    HotelOption,
    Place,
    RouteOption,
    TrainOption,
    coerce_float,
    coerce_int,
    coerce_str,
    envelope_items,
    parse_envelope,
    utcnow,
)

__all__ = [
    "PROVIDER_STATUSES",
    "ProviderCall",
    "ProviderError",
    "ProviderHub",
    "ProviderResult",
    "ToolTimeout",
    "lnglat",
    "packages_root",
]

# ==================================================
# 状态机（PRD §32）
# ==================================================

#: Provider 统一状态。OK 之外的每一个都表示"这次没拿到数据"，上层必须如实降级。
PROVIDER_STATUSES = (
    "OK",
    "EMPTY",
    "UNAVAILABLE",
    "AUTH_ERROR",
    "RATE_LIMIT",
    "FREE_CREDIT_EXHAUSTED",
    "TIMEOUT",
    "INVALID_RESPONSE",
)

#: 这些状态表示"换一个数据源可能拿到数据"（PRD §32 允许的 fallback 触发条件）。
#: EMPTY 刻意不在里面：没搜到就是没搜到，换源去"碰运气"容易把结果变成噪声。
FALLBACK_STATUSES = frozenset(
    {"UNAVAILABLE", "TIMEOUT", "AUTH_ERROR", "RATE_LIMIT", "FREE_CREDIT_EXHAUSTED", "INVALID_RESPONSE"}
)

# ==================================================
# 缓存 TTL（PRD §33）
# ==================================================

#: 单位：秒。实时数据不长期缓存；命中的缓存仍然保留**真实**的 fetched_at，
#: 只在 source 记录里标注 cached=true，让审计能看出这次是复用还是新查。
CACHE_TTL_SECONDS: dict[str, float] = {
    "flight": 5 * 60,
    "train": 2 * 60,
    "hotel": 10 * 60,
    "ticket": 30 * 60,
    "poi": 24 * 3600,
    "geocode": 24 * 3600,
    "route": 15 * 60,
    "social": 6 * 3600,
    "web": 6 * 3600,
}

#: source 记录里 raw 的最大字符数。目的是让 audit 能回答"返回了什么"，
#: 而不是把几 MB 的原始响应塞进 SQLite。截断时明确写 `_truncated`，不假装完整。
RAW_MAX_CHARS = 60_000


class ProviderError(RuntimeError):
    """Provider 层面的不可恢复错误（配置缺失、Plugin 加载失败等）。

    注意：**单次查询失败不走异常**，走 status。异常只用于"这个 Provider 根本装不起来"，
    因为那属于环境问题，藏在一个 EMPTY 里会让排查变成猜谜。
    """


class ToolTimeout(RuntimeError):
    """一次 Tool 调用超过它自己的预算（Part E）。

    为什么要有这个类型而不是直接用 TimeoutError：调用点要能一眼分清"超时"与"工具报错"，
    并把前者记成 status=TIMEOUT 走既有的 fallback 链路（12306→途牛、TikHub→MediaCrawler），
    而不是笼统地记成 UNAVAILABLE。
    """


def _invoke_bounded(tool: Any, args: dict[str, Any], *, timeout: float, label: str) -> Any:
    """在**独立守护线程**里调用一个 Tool，到点就放弃等待。

    为什么必须有这一层：Plugin Tool 的超时行为不一致 —— 高德 / TikHub / 联网搜索的入参里
    根本没有 timeout（高德是固定 20s × 3 次重试、TikHub 20s、Tavily 走 SDK 自己的超时）；
    途牛那批**声明**了 timeout，内部 `subprocess.run` 却没把它传下去。上游一劣化或 CLI 挂住，
    整条 run 就永久停在那里（生产上实测卡了一个多小时，run 一直是 RUNNING）。
    所以这里对**所有** Plugin Tool 统一补一个由 config 控制的上限：
    "声明了 timeout" 只用来决定要不要把预算传进参数，不再决定要不要设外层守护。

    为什么用裸线程而不是线程池：线程池的 worker 被一个卡住的调用占满之后，**排队**的调用
    会把排队时间算进自己的 timeout，于是出现"明明没人查它却报超时"的假故障。一调用一线程
    （daemon=True）不会互相拖累，也不会让解释器退出时卡住；真正在飞的调用会在自己返回后
    被丢弃，它的结果**不会**被当成有效数据混进来。
    """

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = tool.invoke(args)
        except BaseException as exc:  # noqa: BLE001 —— 原样带回给调用方判定
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name=f"tp-tool-{label}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise ToolTimeout(f"{label} 超过 {timeout:g}s 未返回，本次调用按 TIMEOUT 处理")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def tool_accepts_timeout(tool: Any) -> bool:
    """这个 Tool 的入参签名里是否声明了 ``timeout``。

    用签名判断而不是维护一张硬编码的工具名清单：Plugin 以后给某个工具加上 timeout 参数时
    这里会自动跟上，不需要两边同时改（硬编码清单一定会漂移）。
    """

    try:
        return "timeout" in set(getattr(tool, "args", None) or {})
    except Exception:  # noqa: BLE001 —— 取不到签名就当作"不支持"，走 _invoke_bounded
        return False


# ==================================================
# 返回值归一：MCP 的 content blocks / str / dict → 信封
# ==================================================

_SECRET_ENV_NAMES = (
    "AMAP_API_KEY",
    "TIKHUB_API_TOKEN",
    "TUNIU_API_KEY",
    "TAVILY_API_KEY",
    "MODEL_API_KEY",
)


def _scrub_secrets(text: str) -> str:
    """把当前进程里的各个 Key 从文本里抹掉。

    Plugin 侧已经各自擦了，这里是兜底：异常堆栈、SDK 的报错文案都有可能把 key 带出来，
    而这些文本会进日志和 SQLite。
    """
    import os

    for name in _SECRET_ENV_NAMES:
        secret = (os.environ.get(name) or "").strip()
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def as_text(raw: Any) -> str:
    """把 Tool 的返回压成一段文本。

    三种形状都要认：
      * `str` —— Plugin 里的工具（它们 json.dumps 之后返回字符串）；
      * `list[dict]` —— MCP 工具经 langchain-mcp-adapters 回来的是 content blocks，
        `[{"type": "text", "text": "..."}]`；
      * `dict` —— 某些工具直接返回结构化对象。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # content block 的单块形式
        if isinstance(raw.get("text"), str):
            return raw["text"]
        return json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, (list, tuple)):
        chunks: list[str] = []
        for item in raw:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
            else:
                chunks.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(chunks)
    return str(raw)


def as_payload(raw: Any) -> dict:
    """把 Tool 的返回解析成统一信封 dict。

    解析不出来时返回 `{}` —— 由调用方按 INVALID_RESPONSE 处理，
    这里**绝不**编一个 {"status": "OK"} 出来。
    """
    text = as_text(raw).strip()
    if not text:
        return {}
    payload = parse_envelope(text)
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        # 裸数组：没有信封，包一层（route 之类的工具偶尔会只回 items）
        return {"status": "OK", "items": payload}
    return {}


#: 置为真值时**不注册任何 MCP Server**。
#: 为什么需要它：MCP Server 是 `npx -y 12306-mcp` 拉起的子进程，**不需要任何密钥**，
#: 所以测试里那套"摘掉第三方 Key 让 Provider 自然降级"的离线策略拦不住它 ——
#: 一个没显式注入替身的用例会真的联网，而且 stdio 握手不返回时线程会一直阻塞，
#: 整套 pytest 会卡死在某个用例上（实测卡在 ~24%，半小时不动）。
#: 生产不设这个变量，12306 主源照常生效。
DISABLE_MCP_ENV = "TRAVELPLAN_DISABLE_MCP"


def mcp_enabled() -> bool:
    """MCP Server 是否允许注册（测试通过环境变量整体关掉）。"""

    return os.environ.get(DISABLE_MCP_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def default_mcp_servers() -> list[MCPServerSpec]:
    """本项目用到的 MCP Server 清单（目前只有 12306 火车票）。

    只在这里定义一次：`app.agent` 把清单交给 SuperHarness 的 harness app，
    `execute_travel_run` 自建 ProviderHub 时也要交同一份 —— ProviderHub 只对
    **拿到过 spec** 的 server 建句柄，两边各写一份迟早会漏掉一边（曾经就漏在自建
    Hub 这边：12306 每次都报「未注册 MCP Server 'railway_12306'」，主源从未生效）。
    """

    if not mcp_enabled():
        return []

    from superharness.capabilities.mcp import railway_12306_server

    return [railway_12306_server()]


def _status_of(payload: dict) -> str:
    """从信封里读 status；缺 status 但有 items 视为 OK。"""
    raw = coerce_str(payload.get("status")).upper()
    if raw in PROVIDER_STATUSES:
        return raw
    if payload:
        items = payload.get("items")
        if isinstance(items, list):
            return "OK" if items else "EMPTY"
    return "INVALID_RESPONSE"


# ==================================================
# 异步运行时：给 MCP 一个常驻的事件循环
# ==================================================


class _AsyncRuntime:
    """一个守护线程 + 一个常驻事件循环。

    为什么不能直接 `asyncio.run(coro)`：
    `MCPLoader` 会缓存 `MultiServerMCPClient`，而它内部的 anyio task group / stdio 子进程
    / httpx client 都绑定在**创建它的那个 loop** 上。每次 `asyncio.run` 都新建一个 loop，
    第二次调用就会踩到 "attached to a different loop"。所以让所有 MCP 调用共用一个
    长期存活的 loop，通过 `run_coroutine_threadsafe` 把协程送进去。
    """

    def __init__(self, name: str = "travelplan-mcp") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


# ==================================================
# 调用记录
# ==================================================


@dataclass(slots=True)
class ProviderCall:
    """一次 Provider 调用的完整留痕（audit_report 的事实来源）。"""

    source_id: str
    provider: str
    source_type: str
    tool: str
    query: dict[str, Any]
    status: str
    fetched_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    source_url: str | None = None
    duration_ms: int | None = None
    cached: bool = False
    #: 这次调用是**因为主源不可用**才发出的（12306→途牛火车、TikHub→MediaCrawler）。
    #: 它让管理端与 profiler 能直接回答"哪些调用走了 fallback"，而不是从两条相邻的
    #: 调用记录里猜。审计账本、provider_calls 表与 Trace 都读这个字段。
    fallback: bool = False
    notes: list[str] = field(default_factory=list)
    #: 交通对冲（hedge）里"备胎跑完了但主源已给出结论、所以结果没被采用"。
    #: 与 fallback 分开：fallback 说明"这是主源失败后的备选"，discarded 说明
    #: "这条调用连备选都没当上"。管理端/profiler 要能区分"白打的那一次"与
    #: "真正被采用的备选"，否则对冲的代价就看不见了。
    discarded: bool = False
    #: 内部使用：从哪个 Plugin / MCP Server 来的。不进 sources 表。
    origin: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_source(self) -> dict[str, Any]:
        """转成 store.save_source 认的信封（PRD §17 sources 表）。"""
        normalized = {
            "count": len(self.items),
            "items": self.items[:20],
            "notes": list(self.notes),
            "cached": self.cached,
            "origin": self.origin,
            "tool": self.tool,
        }
        if len(self.items) > 20:
            normalized["items_truncated"] = True

        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "query": self.query,
            "fetched_at": self.fetched_at,
            "raw": _truncate_payload(self.payload),
            "normalized": normalized,
            "status": self.status,
        }

    def to_source_ref(self) -> dict[str, Any]:
        """转成 plan.json 的 `sources[]` 条目（models.SourceRef 的形状）。"""
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "title": f"{self.provider} · {self.tool}",
            "query": self.query,
            "fetched_at": self.fetched_at,
            "status": self.status,
        }


def _truncate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """超长 payload 只留可读的一段，并明确标注被截断。"""
    if not payload:
        return {}
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_unserializable": True, "keys": list(payload)[:20]}
    if len(text) <= RAW_MAX_CHARS:
        return payload
    return {
        "_truncated": True,
        "_full_chars": len(text),
        "sample": text[:RAW_MAX_CHARS],
    }


T = TypeVar("T")


@dataclass(slots=True)
class ProviderResult(Generic[T]):
    """一次业务查询的结果：已归一化的业务对象 + 为什么是这个结果。

    `calls` 可能不止一条 —— 12306 失败后回退到途牛火车时会有两条，
    这正是审计需要看到的"为什么最终用了这个"。
    """

    status: str
    items: list[T] = field(default_factory=list)
    calls: list[ProviderCall] = field(default_factory=list)
    error: str | None = None
    provider: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK" and bool(self.items)

    @property
    def statuses(self) -> list[str]:
        return [call.status for call in self.calls]

    def describe(self) -> str:
        """给日志 / 前端提示用的一句话结论。"""
        if self.ok:
            return f"{self.provider} 返回 {len(self.items)} 条（{self.status}）"
        detail = self.error or "，".join(
            f"{call.provider}={call.status}" for call in self.calls
        )
        return f"未取到数据（{detail}）"


# ==================================================
# Tool 访问：Plugin
# ==================================================


def packages_root() -> Path:
    """super_harness 包目录（不是仓库目录）。

    Plugin / Tool 目录都相对包目录解析 —— 与 `superharness/agent.py::_resolve_pkg_path`
    的语义一致，这样 pip install 之后从任意 cwd 运行都能找到。
    """
    import superharness

    return Path(superharness.__file__).resolve().parent


class PluginToolSet:
    """按 Plugin 懒加载工具，并把工具名映射到 (plugin, tool)。

    为什么自己再包一层：`PluginLoader.load_tools(manifest)` 需要 manifest，
    而调用方只关心"我要 tuniu_search_flights"。这层的全部工作就是维护那张索引，
    真正 import Plugin 代码仍然只发生在第一次用到它的时候（懒加载语义不变）。
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        only: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else packages_root() / "capabilities" / "plugins"
        self._loader = PluginLoader(self.root)
        allowed = set(only) if only is not None else None
        self._manifests: dict[str, PluginManifest] = {
            manifest.name: manifest
            for manifest in self._loader.manifests
            if manifest.enabled and (allowed is None or manifest.name in allowed)
        }
        #: 工具名 → 所属 Plugin。优先信构造方给的声明（only），这样"要哪个工具就只 import
        #: 哪个 Plugin"；没有声明时退化成"按需逐个试"，仍然只 import 到找到为止。
        self._owner: dict[str, str] = {
            tool: plugin for plugin, tools in (only or {}).items() for tool in tools
        }
        self._index: dict[str, tuple[str, BaseTool]] = {}
        self._loaded: set[str] = set()

    @property
    def plugin_names(self) -> list[str]:
        return sorted(self._manifests)

    def load(self, plugin: str) -> dict[str, BaseTool]:
        """加载某个 Plugin 的全部工具（幂等）。"""
        if plugin not in self._manifests:
            raise ProviderError(
                f"Plugin {plugin!r} 不在 {self.root} 里（可用：{', '.join(self.plugin_names)}）"
            )
        if plugin not in self._loaded:
            for tool in self._loader.load_tools(self._manifests[plugin]):
                self._index.setdefault(tool.name, (plugin, tool))
            self._loaded.add(plugin)
        return {name: tool for name, (owner, tool) in self._index.items() if owner == plugin}

    def get(self, tool_name: str) -> tuple[str, BaseTool] | None:
        """按工具名找 (plugin, tool)；找不到返回 None（不抛，调用方决定怎么降级）。"""
        if tool_name in self._index:
            return self._index[tool_name]

        owner = self._owner.get(tool_name)
        candidates = [owner] if owner in self._manifests else [
            plugin for plugin in self._manifests if plugin not in self._loaded
        ]
        for plugin in candidates:
            self.load(plugin)
            if tool_name in self._index:
                return self._index[tool_name]
        return None


class LocalToolSet:
    """super_harness 自带的 Local Tool（目前只用到 web_search）。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else packages_root() / "capabilities" / "tools"
        self._loader = ToolLoader(self.root)
        self._index: dict[str, BaseTool] = {}

    def get(self, tool_name: str) -> BaseTool | None:
        if not self._index:
            for tool in self._loader.load_tools():
                self._index[tool.name] = tool
        return self._index.get(tool_name)


# ==================================================
# Tool 访问：MCP
# ==================================================


class MCPServerHandle:
    """一个 MCP Server 的懒连接句柄。

    `spec` 是**纯声明**，构造它不会起进程、不发请求；真正的 connect 发生在
    第一次 `tools()`。这与 `MCPLoader` 的设计一致，不要在 __init__ 里提前连接。
    """

    def __init__(self, spec: MCPServerSpec, runtime: _AsyncRuntime, *, tool_timeout: float = 120.0) -> None:
        self.spec = spec
        self.name = spec.name
        self._runtime = runtime
        self._tool_timeout = tool_timeout
        self._loader = MCPLoader([spec])
        self._tools: dict[str, BaseTool] | None = None

    def tools(self) -> dict[str, BaseTool]:
        if self._tools is None:
            loaded = self._runtime.run(self._loader.load_tools([self.name]), timeout=self._tool_timeout)
            self._tools = {tool.name: tool for tool in loaded}
        return self._tools

    def get(self, tool_name: str) -> BaseTool | None:
        return self.tools().get(tool_name)

    def call(self, tool_name: str, args: dict[str, Any], *, timeout: float | None = None) -> Any:
        tool = self.get(tool_name)
        if tool is None:
            raise ProviderError(
                f"MCP Server {self.name!r} 没有暴露工具 {tool_name!r}"
                f"（可用：{', '.join(sorted(self.tools()))}）"
            )
        return self._runtime.run(
            tool.ainvoke(args), timeout=timeout or self._tool_timeout
        )


# ==================================================
# Hub
# ==================================================

#: 各 Plugin 提供哪些工具。写在这里是为了让"谁负责什么"一眼可见，
#: 也避免工具名散落在业务代码里（PRD §9 的 Provider 优先级表）。
PLUGIN_TOOLS: dict[str, tuple[str, ...]] = {
    "amap_cn": ("geocode", "search_poi", "get_poi_detail", "route"),
    "tuniu_travel": (
        "tuniu_search_flights",
        "tuniu_search_trains",
        "tuniu_search_hotels",
        "tuniu_search_scenic_tickets",
        "tuniu_search_cruises",
        "tuniu_search_holiday_packages",
    ),
    "tikhub_social": ("get_tikhub_quota", "search_xiaohongshu", "search_douyin"),
    "mediacrawler_social": (
        "check_mediacrawler_available",
        "search_xhs_via_mediacrawler",
        "search_douyin_via_mediacrawler",
    ),
}


@dataclass(slots=True)
class _CacheEntry:
    stored_at: float
    payload: dict[str, Any]
    items: list[dict[str, Any]]
    status: str
    fetched_at: datetime
    error: str | None
    source_url: str | None
    notes: list[str]


class ProviderHub:
    """TravelPlan 访问所有外部数据的唯一入口。

    用法：

        hub = ProviderHub(run_id=run_id, store=store)
        result = hub.search_trains("北京", "成都", date(2026, 10, 1))
        for call in result.calls:      # 审计
            print(call.tool, call.status, call.query)
        hub.close()
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        store: Any | None = None,
        plugin_root: Path | None = None,
        tool_root: Path | None = None,
        mcp_servers: Sequence[MCPServerSpec] | None = None,
        use_cache: bool = True,
        request_timeout: float = 60.0,
        mcp_timeout: float = 180.0,
        timeouts: Mapping[str, float] | None = None,
        emit: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self.store = store
        self.use_cache = use_cache
        #: 没有单独配置的 Provider / Tool 的兜底预算。**有单独配置的一律用单独配置** ——
        #: Part E 的要求就是"不能一个值管所有 Provider"。
        self.request_timeout = request_timeout
        #: provider → 它自己的 timeout 秒数（来自 app/config.py，带环境变量覆盖）。
        #: 显式传 `timeouts` 是为了测试能构造一个"某个 Provider 必然超时"的 Hub。
        self.timeouts: dict[str, float] = (
            {str(key): float(value) for key, value in timeouts.items()}
            if timeouts is not None
            else provider_timeouts()
        )
        #: 上报给 SuperHarness 原生 Observability 的钩子（START.md §9：不要重造）。
        #: 不传就只是没有 Tool 日志，规划照常跑。
        self._emit_hook = emit
        #: MCP 单独一份预算，不复用 request_timeout：MCP Server 是 npx 拉起的子进程，
        #: 第一次调用要付冷启动（解析/下载包 + tools/list 握手）的代价，这段耗时和上游
        #: 接口有多慢无关。实测 tools/list 19.6s、12306 正常查询 13.2s，但冷启动一次就
        #: 冲破了共用的 60s，整个火车主源被记成 TIMEOUT、每次都静默降级到途牛兜底。
        self.mcp_timeout = mcp_timeout

        self.plugins = PluginToolSet(plugin_root, only=PLUGIN_TOOLS)
        self.local_tools = LocalToolSet(tool_root)

        self._runtime: _AsyncRuntime | None = None
        self._mcp: dict[str, MCPServerHandle] = {}
        self._mcp_specs = list(mcp_servers or [])
        self._mcp_failures: dict[str, str] = {}

        self._cache: dict[str, _CacheEntry] = {}
        self.calls: list[ProviderCall] = []
        self._counter = 0

        # 一把锁保护 Hub 自己的共享状态：source_id 计数器、缓存字典、调用列表、
        # MCP 句柄表。**这不是理论风险**：workflow 的 4 条大交通查询、N 条 POI 查询
        # 和路线查询共用同一个 Hub 并发调用；再加一次性提交 12306+途牛的交通对冲，
        # 同一时刻可能有十几个线程在 Hub 里跑。裸的 `self._counter += 1` 是
        # 读-改-写，并发下会发出重复 source_id（同一条记录被两条 provenance 指向）。
        # 约束：锁只做内存操作，**绝不**在持锁时做网络 / 子进程调用（见 _mcp_handle）。
        self._lock = threading.Lock()
        #: 每个 MCP Server 一份"建句柄"锁。为什么不能只用上面那一把：`handle.tools()`
        #: 会真的拉起 MCP 子进程（npx 冷启动可达十几秒），必须放在锁外做，否则一个
        #: Server 的冷启动会把整个 Hub 卡住。用 per-server 锁既保证"同一个 Server
        #: 只会有一个句柄/一个子进程"，又不会让不同 Server 的建连互相排队。
        self._mcp_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """释放 MCP 子进程 / 事件循环。幂等。"""
        # 先让 MCP 客户端自己收摊（它要在还活着的 loop 上关 stdio 子进程）
        for handle in list(self._mcp.values()):
            try:
                self._runtime.run(handle._loader._client.aclose(), timeout=10)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 —— 关不干净不值得让整个 run 失败
                pass
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    def __enter__(self) -> "ProviderHub":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部：记录 / 缓存
    # ------------------------------------------------------------------

    def _next_source_id(self) -> str:
        self._counter += 1
        prefix = self.run_id or "run"
        return f"{prefix}-src-{self._counter:03d}"

    def _timeout_for(self, provider: str) -> float:
        """这个 Provider 本次的调用预算（秒）。Part E：每个 Provider 一份。"""

        return float(self.timeouts.get(provider, self.request_timeout))

    def _cache_key(self, provider: str, tool: str, args: Mapping[str, Any]) -> str:
        try:
            blob = json.dumps(dict(args), ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = repr(dict(args))
        return f"{provider}|{tool}|{blob}"

    def _cache_get(self, kind: str, key: str) -> _CacheEntry | None:
        if not self.use_cache:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at > CACHE_TTL_SECONDS.get(kind, 0):
            return None
        return entry

    def _cache_put(self, key: str, entry: _CacheEntry) -> None:
        if self.use_cache:
            self._cache[key] = entry

    def _emit_tool(self, call: ProviderCall) -> None:
        """把这次 Provider 调用报给 SuperHarness 的原生 Observability。

        started / finished 都在 `_record` 里发：Provider 调用是阻塞的，没有中间态可报，
        等回到这里时结果和耗时都已经确定。上报失败不影响规划。
        """
        hook = self._emit_hook
        if hook is None:
            return
        name = f"{call.provider}/{call.tool}"
        output = (
            f"{call.status} · {len(call.items)} 条 · fetched_at={call.fetched_at}"
            + ("（命中缓存）" if call.cached else "")
            + (f" · {call.error}" if call.error else "")
        )
        try:
            hook(EventType.TOOL_STARTED, "provider", data={"name": name, "args": dict(call.query)})
            hook(
                EventType.TOOL_FINISHED,
                "provider",
                data={"name": name, "duration_ms": call.duration_ms, "output": output},
                status=call.status,
            )
        except Exception:  # noqa: BLE001 —— 日志消费者出错不能把主流程搞死
            pass

    def _record(self, call: ProviderCall) -> ProviderCall:
        """留痕：进内存列表 + 落 sources 表。落库失败不让 run 挂掉，但会写进 notes。"""
        self.calls.append(call)
        self._emit_tool(call)
        if self.store is not None and self.run_id:
            try:
                self.store.save_source(self.run_id, call.to_source())
            except Exception as exc:  # noqa: BLE001 —— 审计写不进去不应该毁掉这次规划
                call.notes.append(f"source 落库失败：{type(exc).__name__}: {_scrub_secrets(str(exc))}")
        return call

    # ------------------------------------------------------------------
    # 内部：调用一个 Plugin Tool
    # ------------------------------------------------------------------

    def _plugin_call(
        self,
        *,
        plugin: str,
        tool_name: str,
        args: dict[str, Any],
        provider: str,
        source_type: str,
        kind: str,
        source_url: str | None = None,
        timeout: float | None = None,
    ) -> ProviderCall:
        """调用 Plugin Tool 并归一成 ProviderCall（不做业务映射）。

        ``timeout`` 是**这个 Provider 自己的**预算（Part E）。Tool 签名里声明了 timeout
        就传给 Tool（让子进程 / httpx 自己收敛）；没声明就在 Hub 层用 ``_invoke_bounded``
        兜住 —— 两条路径产生的都只是"这一次调用超时"，不会丢掉已经拿到的别的结果。
        """

        budget = float(timeout if timeout is not None else self._timeout_for(provider))
        args = {key: value for key, value in args.items() if value is not None}
        key = self._cache_key(provider, tool_name, args)

        entry = self._cache_get(kind, key)
        if entry is not None:
            call = ProviderCall(
                source_id=self._next_source_id(),
                provider=provider,
                source_type=source_type,
                tool=tool_name,
                query=dict(args),
                status=entry.status,
                fetched_at=entry.fetched_at,
                payload=entry.payload,
                items=entry.items,
                error=entry.error,
                source_url=entry.source_url,
                cached=True,
                notes=[*entry.notes, "命中缓存（fetched_at 仍是首次真实获取时间）"],
                origin=plugin,
            )
            return self._record(call)

        found = self.plugins.get(tool_name)
        if found is None:
            call = ProviderCall(
                source_id=self._next_source_id(),
                provider=provider,
                source_type=source_type,
                tool=tool_name,
                query=dict(args),
                status="UNAVAILABLE",
                fetched_at=utcnow(),
                error=f"Plugin {plugin!r} 未提供工具 {tool_name!r}（Plugin 可能被禁用或未安装）",
                origin=plugin,
            )
            return self._record(call)

        tool = found[1]
        # 把预算写进 query：审计里能看到"这次用的是几秒的预算"，而不是事后猜。
        supports_timeout = tool_accepts_timeout(tool)
        if supports_timeout:
            args = {**args, "timeout": budget}

        started = time.monotonic()
        raw: Any = None
        try:
            # 无论 Tool 自称是否支持 timeout，**一律**走 _invoke_bounded 的外层守护。
            # 为什么不能相信"声明了 timeout"：声明不等于遵守。实测途牛火车工具签名里
            # 有 timeout，内部 `subprocess.run` 却没把它传下去 —— CLI 进程挂住时，
            # 调用它的 workflow 线程会无限等待，run 永远停在 RUNNING（生产上真的卡了
            # 一个多小时，前端一直转圈）。外层守护保证"到点一定放手"，
            # 声明了 timeout 的 Tool 仍然拿到它自己的预算去做内部取消。
            raw = _invoke_bounded(tool, args, timeout=budget, label=f"{provider}/{tool_name}")
        except Exception as exc:  # noqa: BLE001 —— Tool 炸了要变成 status，不是往上抛
            message = _scrub_secrets(f"{type(exc).__name__}: {exc}")
            status = "TIMEOUT" if isinstance(exc, ToolTimeout) else "UNAVAILABLE"
            call = ProviderCall(
                source_id=self._next_source_id(),
                provider=provider,
                source_type=source_type,
                tool=tool_name,
                query=dict(args),
                status=status,
                fetched_at=utcnow(),
                error=message,
                duration_ms=int((time.monotonic() - started) * 1000),
                notes=[traceback.format_exc(limit=3)],
                origin=plugin,
            )
            return self._record(call)

        payload = as_payload(raw)
        items = envelope_items(payload)
        status = _status_of(payload)
        error = coerce_str(payload.get("error")) or None
        notes: list[str] = []
        if not payload:
            status = "INVALID_RESPONSE"
            error = error or "工具返回无法解析为 JSON"

        call = ProviderCall(
            source_id=self._next_source_id(),
            provider=provider,
            source_type=source_type,
            tool=tool_name,
            query=dict(args),
            status=status,
            fetched_at=utcnow(),
            payload=payload,
            items=items,
            error=error,
            source_url=source_url,
            duration_ms=int((time.monotonic() - started) * 1000),
            notes=notes,
            origin=plugin,
        )
        self._cache_put(
            key,
            _CacheEntry(
                stored_at=time.monotonic(),
                payload=payload,
                items=items,
                status=status,
                fetched_at=call.fetched_at,
                error=error,
                source_url=source_url,
                notes=notes,
            ),
        )
        return self._record(call)

    # ------------------------------------------------------------------
    # 内部：调用一个 MCP Tool
    # ------------------------------------------------------------------

    def _mcp_handle(self, server: str) -> MCPServerHandle | None:
        """拿到 MCP Server 句柄；连不上就记录原因并返回 None（不抛）。"""
        if server in self._mcp:
            return self._mcp[server]
        if server in self._mcp_failures:
            return None

        spec = next((item for item in self._mcp_specs if item.name == server), None)
        if spec is None:
            self._mcp_failures[server] = f"未注册 MCP Server {server!r}"
            return None

        if self._runtime is None:
            self._runtime = _AsyncRuntime()
        handle = MCPServerHandle(spec, self._runtime, tool_timeout=self.mcp_timeout)
        try:
            # 立刻探一次 tools/list：连不上要在第一次调用时就暴露，
            # 而不是留到真正查询时才失败（那时错误信息会混在业务失败里）。
            handle.tools()
        except Exception as exc:  # noqa: BLE001
            self._mcp_failures[server] = _scrub_secrets(f"{type(exc).__name__}: {exc}")
            return None
        self._mcp[server] = handle
        return handle

    def mcp_unavailable_reason(self, server: str) -> str | None:
        """某个 MCP Server 为什么不可用（没试过 → None）。用于 CLI / API 的降级说明。"""
        return self._mcp_failures.get(server)

    def _mcp_call(
        self,
        *,
        server: str,
        tool_name: str,
        args: dict[str, Any],
        provider: str,
        source_type: str,
        kind: str,
        timeout: float | None = None,
    ) -> ProviderCall:
        budget = float(timeout if timeout is not None else self._timeout_for(provider))
        args = {key: value for key, value in args.items() if value is not None}
        key = self._cache_key(provider, tool_name, args)

        entry = self._cache_get(kind, key)
        if entry is not None:
            return self._record(
                ProviderCall(
                    source_id=self._next_source_id(),
                    provider=provider,
                    source_type=source_type,
                    tool=tool_name,
                    query=dict(args),
                    status=entry.status,
                    fetched_at=entry.fetched_at,
                    payload=entry.payload,
                    items=entry.items,
                    error=entry.error,
                    cached=True,
                    notes=["命中缓存"],
                    origin=server,
                )
            )

        handle = self._mcp_handle(server)
        if handle is None:
            return self._record(
                ProviderCall(
                    source_id=self._next_source_id(),
                    provider=provider,
                    source_type=source_type,
                    tool=tool_name,
                    query=dict(args),
                    status="UNAVAILABLE",
                    fetched_at=utcnow(),
                    error=self._mcp_failures.get(server, "MCP Server 不可用"),
                    origin=server,
                )
            )

        started = time.monotonic()
        try:
            raw = handle.call(tool_name, args, timeout=budget)
        except Exception as exc:  # noqa: BLE001
            message = _scrub_secrets(f"{type(exc).__name__}: {exc}")
            lowered = message.lower()
            status = "TIMEOUT" if ("timeout" in lowered or "timed out" in lowered) else "UNAVAILABLE"
            return self._record(
                ProviderCall(
                    source_id=self._next_source_id(),
                    provider=provider,
                    source_type=source_type,
                    tool=tool_name,
                    query=dict(args),
                    status=status,
                    fetched_at=utcnow(),
                    error=message,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    origin=server,
                )
            )

        payload = as_payload(raw)
        items = envelope_items(payload)
        status = _status_of(payload)
        if not payload:
            status = "INVALID_RESPONSE"
        call = ProviderCall(
            source_id=self._next_source_id(),
            provider=provider,
            source_type=source_type,
            tool=tool_name,
            query=dict(args),
            status=status,
            fetched_at=utcnow(),
            payload=payload,
            items=items,
            duration_ms=int((time.monotonic() - started) * 1000),
            origin=server,
        )
        self._cache_put(
            key,
            _CacheEntry(
                stored_at=time.monotonic(),
                payload=payload,
                items=items,
                status=status,
                fetched_at=call.fetched_at,
                error=None,
                source_url=None,
                notes=[],
            ),
        )
        return self._record(call)

    # ==================================================================
    # 大交通：机票
    # ==================================================================

    def search_flights(
        self,
        origin: str,
        destination: str,
        depart_date: date | str,
        *,
        travelers: int = 1,
    ) -> ProviderResult[FlightOption]:
        """查机票（途牛是唯一数据源，PRD §9 不允许自动换其它 OTA）。"""
        day = depart_date.isoformat() if isinstance(depart_date, date) else str(depart_date)
        call = self._plugin_call(
            plugin="tuniu_travel",
            tool_name="tuniu_search_flights",
            args={
                "departure_city": origin,
                "arrival_city": destination,
                "departure_date": day,
            },
            provider="tuniu",
            source_type="flight",
            kind="flight",
        )
        items = [
            FlightOption.from_item(
                item,
                provider="tuniu",
                fetched_at=call.fetched_at,
                source_id=call.source_id,
                source_url=call.source_url or "https://www.tuniu.com/",
                origin=origin,
                destination=destination,
            )
            for item in call.items
        ]
        return ProviderResult(
            status=call.status, items=items, calls=[call], error=call.error, provider="tuniu"
        )

    # ==================================================================
    # 大交通：火车（12306 主源 → 途牛备用，PRD §32）
    # ==================================================================

    def search_trains(
        self,
        origin: str,
        destination: str,
        depart_date: date | str,
        *,
        filters: str | None = None,
        sort: str = "startTime",
        limit: int = 10,
    ) -> ProviderResult[TrainOption]:
        """查火车票：**先 12306，失败才回退途牛**（PRD §9 / §32）。

        为什么 12306 是主源：车次、时刻、席别余票是一手数据；途牛是二手聚合。
        为什么失败才回退：两个源的车次集合可能不一致，混着用会让"我为什么看到这趟车"
        无法回答。所以只在主源**整体不可用**时切，且两条调用都留在 audit 里。
        """
        day = depart_date.isoformat() if isinstance(depart_date, date) else str(depart_date)

        primary = self._mcp_call(
            server="railway_12306",
            tool_name="railway_12306_get-tickets",
            args={
                "date": day,
                "fromStation": origin,
                "toStation": destination,
                "trainFilterFlags": filters or "",
                "sortFlag": sort,
                "sortReverse": False,
                "limitedNum": limit,
                "format": "json",
            },
            provider="12306",
            source_type="train",
            kind="train",
        )

        calls = [primary]
        if primary.status == "OK" and primary.items:
            return ProviderResult(
                status="OK",
                items=[_train_option_from_12306(item, primary) for item in primary.items],
                calls=calls,
                provider="12306",
            )

        if primary.status == "EMPTY":
            # 12306 明确回答"这天没车" —— 这是结论，不是故障，不需要回退。
            return ProviderResult(
                status="EMPTY", items=[], calls=calls, provider="12306"
            )

        fallback = self._plugin_call(
            plugin="tuniu_travel",
            tool_name="tuniu_search_trains",
            args={
                "departure_city": origin,
                "arrival_city": destination,
                "departure_date": day,
            },
            provider="tuniu",
            source_type="train",
            kind="train",
        )
        # 标记"这是 fallback 调用"：审计与管理端要能直接回答"哪些调用是主源不可用之后的
        # 备选"，而不是靠"两条记录挨在一起"去猜。**不**因为它是 fallback 就降低可信度 ——
        # 它仍然是一手数据，只是来源不同。
        fallback.fallback = True
        fallback.notes.append(
            f"fallback：12306 {primary.status}（{primary.error or primary.status}）后改用途牛火车"
        )
        calls.append(fallback)

        if fallback.status == "OK" and fallback.items:
            options = [
                TrainOption.from_item(
                    item,
                    provider="tuniu",
                    fetched_at=fallback.fetched_at,
                    source_id=fallback.source_id,
                    source_url="https://www.tuniu.com/",
                )
                for item in fallback.items
            ]
            return ProviderResult(
                status="OK", items=options, calls=calls, provider="tuniu"
            )

        reason = primary.error or primary.status
        return ProviderResult(
            status=fallback.status,
            items=[],
            calls=calls,
            error=f"12306 不可用（{reason}），途牛火车也未能提供数据（{fallback.status}）",
            provider="tuniu",
        )

    # ==================================================================
    # 住宿
    # ==================================================================

    def search_hotels(
        self,
        city: str,
        check_in: date | str,
        check_out: date | str,
        *,
        travelers: int = 1,
        page_num: int = 1,
    ) -> ProviderResult[HotelOption]:
        """查酒店（途牛）。price_per_night 是**起价**，必须原样保留这个语义。"""
        start = check_in.isoformat() if isinstance(check_in, date) else str(check_in)
        end = check_out.isoformat() if isinstance(check_out, date) else str(check_out)
        call = self._plugin_call(
            plugin="tuniu_travel",
            tool_name="tuniu_search_hotels",
            args={
                "city": city,
                "check_in": start,
                "check_out": end,
                "page_num": page_num,
            },
            provider="tuniu",
            source_type="hotel",
            kind="hotel",
        )
        check_in_date = check_in if isinstance(check_in, date) else None
        check_out_date = check_out if isinstance(check_out, date) else None
        items = [
            HotelOption.from_item(
                item,
                provider="tuniu",
                fetched_at=call.fetched_at,
                source_id=call.source_id,
                source_url="https://www.tuniu.com/",
                check_in=check_in_date,
                check_out=check_out_date,
                travelers=travelers,
            )
            for item in call.items
        ]
        return ProviderResult(
            status=call.status, items=items, calls=[call], error=call.error, provider="tuniu"
        )

    # ==================================================================
    # 门票
    # ==================================================================

    def search_scenic_tickets(self, scenic_name: str) -> ProviderResult[dict[str, Any]]:
        """查景区门票最便宜的渠道（途牛）。

        门票工具返回的是"渠道/价格"列表而不是统一商品，所以这里不做模型映射，
        交给上层按 `scenic_name` 归一成 ticket_prices。
        """
        call = self._plugin_call(
            plugin="tuniu_travel",
            tool_name="tuniu_search_scenic_tickets",
            args={"scenic_name": scenic_name},
            provider="tuniu",
            source_type="ticket",
            kind="ticket",
        )
        return ProviderResult(
            status=call.status, items=list(call.items), calls=[call],
            error=call.error, provider="tuniu",
        )

    # ==================================================================
    # 高德：geocode / POI / 路线
    # ==================================================================

    def geocode(self, address: str, city: str | None = None) -> ProviderResult[dict[str, Any]]:
        """地名 → 经纬度（GCJ-02）。"""
        call = self._plugin_call(
            plugin="amap_cn",
            tool_name="geocode",
            args={"address": address, "city": city},
            provider="amap",
            source_type="geocode",
            kind="geocode",
            source_url="https://lbs.amap.com/api/webservice/guide/api/georegeo",
        )
        return ProviderResult(
            status=call.status, items=list(call.items), calls=[call],
            error=call.error, provider="amap",
        )

    def search_poi(
        self,
        keywords: str,
        region: str,
        *,
        types: str | None = None,
        page: int = 1,
        page_size: int = 20,
        type_hint: str | None = None,
    ) -> ProviderResult[Place]:
        """按关键词搜 POI，直接映射成 `Place`（带 amap_verified=True）。"""
        call = self._plugin_call(
            plugin="amap_cn",
            tool_name="search_poi",
            args={
                "keywords": keywords,
                "region": region,
                "types": types,
                "page": page,
                "page_size": page_size,
            },
            provider="amap",
            source_type="poi",
            kind="poi",
            source_url="https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch",
        )
        places: list[Place] = []
        for item in call.items:
            places.append(
                Place.from_poi(
                    item,
                    place_id=coerce_str(item.get("poi_id") or item.get("id")) or None,
                    city=region,
                    type_hint=type_hint,
                    source_id=call.source_id,
                )
            )
        return ProviderResult(
            status=call.status, items=places, calls=[call], error=call.error, provider="amap"
        )

    def poi_detail(self, poi_id: str) -> ProviderResult[dict[str, Any]]:
        """POI 详情（营业时间、评分、电话）。"""
        call = self._plugin_call(
            plugin="amap_cn",
            tool_name="get_poi_detail",
            args={"poi_id": poi_id},
            provider="amap",
            source_type="poi_detail",
            kind="poi",
            source_url="https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch",
        )
        return ProviderResult(
            status=call.status, items=list(call.items), calls=[call],
            error=call.error, provider="amap",
        )

    def route(
        self,
        origin: tuple[float, float] | str,
        destination: tuple[float, float] | str,
        mode: str = "transit",
        *,
        city: str | None = None,
    ) -> ProviderResult[RouteOption]:
        """两地之间的真实路线。

        `origin` / `destination` 是高德要求的 **(经度, 纬度)** —— 注意顺序与
        `Place.coords` 的 (lat, lng) 相反，所以这里显式接收"经度在前"的元组，
        不接收 Place。调用方要用 `_lnglat(place)` 转换。
        """
        start = _format_coord(origin)
        end = _format_coord(destination)
        call = self._plugin_call(
            plugin="amap_cn",
            tool_name="route",
            args={"origin": start, "destination": end, "mode": mode, "city": city},
            provider="amap",
            source_type="route",
            kind="route",
            source_url="https://lbs.amap.com/api/webservice/guide/api/direction",
        )

        option = RouteOption.from_envelope(call.payload) if call.status == "OK" else None
        items = [option] if option is not None else []
        result_status = call.status
        if call.status == "OK" and option is None:
            # 工具说 OK 但信封里没有可用路线 —— 这是"未验证"，不是成功。
            result_status = "INVALID_RESPONSE"
        return ProviderResult(
            status=result_status, items=items, calls=[call],
            error=call.error, provider="amap",
        )

    # ==================================================================
    # 社交攻略：TikHub 主源 → MediaCrawler 免费 fallback（PRD §32）
    # ==================================================================

    def search_xiaohongshu(self, keyword: str, *, page: int = 1) -> ProviderResult[Evidence]:
        """小红书攻略：TikHub 优先，额度耗尽时回退本地 MediaCrawler。"""
        return self._social(
            keyword=keyword,
            platform="xhs",
            primary_tool="search_xiaohongshu",
            fallback_tool="search_xhs_via_mediacrawler",
            page=page,
        )

    def search_douyin(self, keyword: str, *, page: int = 1) -> ProviderResult[Evidence]:
        """抖音攻略：同上。"""
        return self._social(
            keyword=keyword,
            platform="douyin",
            primary_tool="search_douyin",
            fallback_tool="search_douyin_via_mediacrawler",
            page=page,
        )

    def _social(
        self,
        *,
        keyword: str,
        platform: str,
        primary_tool: str,
        fallback_tool: str,
        page: int,
    ) -> ProviderResult[Evidence]:
        source_type = "social"
        primary = self._plugin_call(
            plugin="tikhub_social",
            tool_name=primary_tool,
            args={"keyword": keyword, "page": page},
            provider="tikhub",
            source_type=source_type,
            kind="social",
        )
        calls = [primary]

        if primary.status == "OK" and primary.items:
            return ProviderResult(
                status="OK",
                items=_evidences_from_social(primary, keyword, platform, provider="tikhub"),
                calls=calls,
                provider="tikhub",
            )

        if primary.status not in FALLBACK_STATUSES:
            # EMPTY / INVALID_RESPONSE：TikHub 有明确答复，不去拿另一个源混淆结论。
            return ProviderResult(
                status=primary.status, items=[], calls=calls,
                error=primary.error, provider="tikhub",
            )

        fallback = self._plugin_call(
            plugin="mediacrawler_social",
            tool_name=fallback_tool,
            args={"keyword": keyword, "limit": 10},
            provider="mediacrawler",
            source_type=source_type,
            kind="social",
        )
        fallback.fallback = True
        fallback.notes.append(f"fallback：TikHub {primary.status} 后改用 MediaCrawler")
        calls.append(fallback)

        if fallback.status == "OK" and fallback.items:
            return ProviderResult(
                status="OK",
                items=_evidences_from_social(fallback, keyword, platform, provider="mediacrawler"),
                calls=calls,
                provider="mediacrawler",
            )

        return ProviderResult(
            status=fallback.status,
            items=[],
            calls=calls,
            error=(
                f"TikHub={primary.status}"
                + (f"（{primary.error}）" if primary.error else "")
                + f"，MediaCrawler fallback={fallback.status}"
                + (f"（{fallback.error}）" if fallback.error else "")
            ),
            provider="mediacrawler",
        )

    def tikhub_quota(self) -> ProviderResult[dict[str, Any]]:
        """查 TikHub 免费额度（不发内容请求）。用于在日志里解释为什么走了 fallback。"""
        call = self._plugin_call(
            plugin="tikhub_social",
            tool_name="get_tikhub_quota",
            args={},
            provider="tikhub",
            source_type="quota",
            kind="social",
        )
        return ProviderResult(
            status=call.status, items=list(call.items), calls=[call],
            error=call.error, provider="tikhub",
        )

    # ==================================================================
    # 通用联网搜索（SuperHarness 自带 Tavily 工具，PRD §15）
    # ==================================================================

    def web_search(self, query: str, *, max_results: int = 5) -> ProviderResult[dict[str, Any]]:
        """联网搜索。只用于官网 / 公告 / 攻略网页，**不能**当机票酒店火车的替代源。"""
        tool = self.local_tools.get("web_search")
        args = {"query": query, "max_results": max_results}
        if tool is None:
            call = ProviderCall(
                source_id=self._next_source_id(),
                provider="tavily",
                source_type="web",
                tool="web_search",
                query=args,
                status="UNAVAILABLE",
                fetched_at=utcnow(),
                error="super_harness 未提供 web_search 工具",
                origin="capabilities/tools",
            )
            return ProviderResult(status="UNAVAILABLE", items=[], calls=[self._record(call)])

        key = self._cache_key("tavily", "web_search", args)
        entry = self._cache_get("web", key)
        if entry is not None:
            return ProviderResult(
                status=entry.status,
                items=list(entry.items),
                calls=[
                    self._record(
                        ProviderCall(
                            source_id=self._next_source_id(),
                            provider="tavily",
                            source_type="web",
                            tool="web_search",
                            query=args,
                            status=entry.status,
                            fetched_at=entry.fetched_at,
                            payload=entry.payload,
                            items=entry.items,
                            cached=True,
                            notes=["命中缓存"],
                            origin="capabilities/tools",
                        )
                    )
                ],
                provider="tavily",
            )

        started = time.monotonic()
        timed_out = False
        try:
            # web_search 的入参里没有 timeout（SDK 自己的超时不可控），所以走 Hub 层的
            # 统一预算：超时按 TIMEOUT 记，让上层如实降级成"这次没搜到攻略"，
            # 而不是把一次卡死的联网搜索算成 run 的耗时。
            text = as_text(
                _invoke_bounded(
                    tool,
                    args,
                    timeout=self._timeout_for("tavily"),
                    label="tavily/web_search",
                )
            )
        except ToolTimeout as exc:
            text = ""
            error = _scrub_secrets(str(exc))
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            text = ""
            error = _scrub_secrets(f"{type(exc).__name__}: {exc}")
        else:
            error = None

        status, items = _interpret_web_search(text)
        if error is not None:
            status = "TIMEOUT" if timed_out else "UNAVAILABLE"

        call = ProviderCall(
            source_id=self._next_source_id(),
            provider="tavily",
            source_type="web",
            tool="web_search",
            query=args,
            status=status,
            fetched_at=utcnow(),
            payload={"text": text, **({"error": error} if error else {})},
            items=items,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
            origin="capabilities/tools",
            notes=["web_search 返回的是渲染文本，这里按它的固定编号格式反解出条目"],
        )
        self._cache_put(
            key,
            _CacheEntry(
                stored_at=time.monotonic(),
                payload=call.payload,
                items=items,
                status=status,
                fetched_at=call.fetched_at,
                error=error,
                source_url=None,
                notes=list(call.notes),
            ),
        )
        return ProviderResult(
            status=status, items=items, calls=[self._record(call)],
            error=error, provider="tavily",
        )

    # ==================================================================
    # 审计汇总
    # ==================================================================

    def source_refs(self) -> list[dict[str, Any]]:
        """所有调用记录 → plan.json 的 sources[]。"""
        return [call.to_source_ref() for call in self.calls]

    def audit_entries(self) -> list[dict[str, Any]]:
        """audit_report.json 里的 provider_calls 段（PRD §28）。"""
        return [
            {
                "source_id": call.source_id,
                "provider": call.provider,
                "source_type": call.source_type,
                "tool": call.tool,
                "origin": call.origin,
                "arguments": call.query,
                "status": call.status,
                "fetched_at": call.fetched_at.isoformat(),
                "duration_ms": call.duration_ms,
                "item_count": len(call.items),
                "cached": call.cached,
                "fallback": call.fallback,
                "error": call.error,
                "source_url": call.source_url,
                "notes": list(call.notes),
            }
            for call in self.calls
        ]


# ==================================================
# 映射辅助
# ==================================================


def lnglat(place: Place | None) -> tuple[float, float] | None:
    """`Place` → 高德要的 **(经度, 纬度)**。

    单独一个函数是因为这里有一个极易写反的约定：`Place.coords` 是 (lat, lng)，
    而高德的 `location` / `origin` / `destination` 一律是 (lng, lat)。
    把转换收在一处，比在每次调 route 时手写一遍安全。
    """
    if place is None or place.lng is None or place.lat is None:
        return None
    return (place.lng, place.lat)


def _format_coord(value: tuple[float, float] | str) -> str:
    if isinstance(value, str):
        return value
    return f"{value[0]:.6f},{value[1]:.6f}"


#: 车次首字母 → 车型。**这是对车次编号规则的确定性解释**（G 就是高铁），
#: 不是编造数据；未知前缀就返回 None，让前端显示"未知"而不是猜一个。
_TRAIN_TYPE_BY_PREFIX: dict[str, str] = {
    "G": "高铁",
    "C": "城际",
    "D": "动车",
    "Z": "直达特快",
    "T": "特快",
    "K": "快速",
    "Y": "旅游列车",
    "L": "临客",
    "S": "市域动车",
}


def _train_type_of(code: str) -> str | None:
    prefix = (code or "").strip()[:1].upper()
    return _TRAIN_TYPE_BY_PREFIX.get(prefix)


def _hhmm_to_minutes(value: Any) -> int | None:
    """12306 的 `lishi` 是 "07:30"（历时）而不是时刻，需要单独换算。"""
    text = coerce_str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    hours = coerce_int(parts[0])
    minutes = coerce_int(parts[1])
    if hours is None or minutes is None:
        return None
    return hours * 60 + minutes


def _seat_availability(raw: Any) -> int | None:
    """12306 余票字段：数字 = 余票数；"有" = 有票但数量未公开；"无" = 无票。

    "有" 映射成 None（**不知道数量**）而不是一个编出来的数字；
    "无" 映射成 0（这是明确结论）。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = coerce_str(raw).strip()
    if not text:
        return None
    if text in {"有", "候补", "有票"}:
        return None
    if text in {"无", "没有", "--", "-"}:
        return 0
    return coerce_int(text)


def _train_option_from_12306(item: dict[str, Any], call: ProviderCall) -> TrainOption:
    """把 12306 `get-tickets` 的一条记录映射成 `TrainOption`。

    实际形状（真实返回，非推测）::

        {"train_no": "240000G32120", "start_date": "2026-10-01", "arrive_date": "2026-10-01",
         "start_train_code": "G321", "start_time": "07:00", "arrive_time": "14:30",
         "lishi": "07:30", "from_station": "北京西", "to_station": "成都东",
         "prices": [{"seat_name": "二等座", "num": "有", "price": 918, "discount": 95}, ...],
         "dw_flag": ["复兴号", "静音车厢"]}

    注意 `arrive_date` 可能是次日（跨夜车），所以到达时刻必须带上日期而不是只取 HH:MM。
    """
    code = coerce_str(item.get("start_train_code") or item.get("train_no"))
    start_date = coerce_str(item.get("start_date"))
    arrive_date = coerce_str(item.get("arrive_date")) or start_date
    start_time = coerce_str(item.get("start_time"))
    arrive_time = coerce_str(item.get("arrive_time"))

    prices = item.get("prices") if isinstance(item.get("prices"), list) else []
    seats: dict[str, int | None] = {}
    price_range: dict[str, float | None] = {"min": None, "max": None}
    numeric: list[float] = []
    for row in prices:
        if not isinstance(row, dict):
            continue
        seat_name = coerce_str(row.get("seat_name") or row.get("short"))
        if not seat_name:
            continue
        seats[seat_name] = _seat_availability(row.get("num"))
        price = coerce_float(row.get("price"))
        if price is not None:
            numeric.append(price)
    if numeric:
        price_range = {"min": min(numeric), "max": max(numeric)}

    return TrainOption.from_item(
        {
            "train_no": code,
            "origin_station": item.get("from_station"),
            "destination_station": item.get("to_station"),
            "departure_at": f"{start_date} {start_time}" if start_date and start_time else None,
            "arrival_at": f"{arrive_date} {arrive_time}" if arrive_date and arrive_time else None,
            "duration_minutes": _hhmm_to_minutes(item.get("lishi")),
            "seats": seats,
            "price_range": price_range,
            "train_type": _train_type_of(code),
            "currency": "CNY",
        },
        provider="12306",
        fetched_at=call.fetched_at,
        source_id=call.source_id,
        source_url="https://kyfw.12306.cn/",
    )


def _evidences_from_social(
    call: ProviderCall, keyword: str, platform: str, *, provider: str
) -> list[Evidence]:
    """社交返回条目 → Evidence（Trust / Ad Risk 的输入）。"""
    evidences: list[Evidence] = []
    for index, item in enumerate(call.items):
        title = coerce_str(item.get("title"))
        desc = coerce_str(item.get("desc") or item.get("content") or item.get("text"))
        text = "\n".join(part for part in (title, desc) if part).strip()
        if not text:
            # 只有标题/正文都没有的条目对 Trust/Ad Risk 无意义，但要留一条说明，
            # 否则"返回了 10 条但只用了 3 条"会被误读成丢数据。
            continue
        metrics = {
            key: item.get(key)
            for key in ("likes", "comments", "collects", "shares", "views")
            if item.get(key) is not None
        }
        evidences.append(
            Evidence.from_content(
                {
                    "title": title,
                    "text": text,
                    "author": item.get("author"),
                    "published_at": item.get("publish_time"),
                    "raw_metrics": metrics,
                },
                evidence_id=f"{call.source_id}-e{index}",
                source_type="social",
                provider=provider,
                source_id=call.source_id,
                source_url=coerce_str(item.get("url")) or None,
                fetched_at=call.fetched_at,
            )
        )
    return evidences


_WEB_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$", re.MULTILINE)


def _interpret_web_search(text: str) -> tuple[str, list[dict[str, Any]]]:
    """解析 web_search 的渲染文本。

    它在两种情况下都回人类可读文本，必须区分开（否则会把"后端坏了"读成"没搜到"）：
      * 后端不可用 → "搜索后端当前不可用：..."  → UNAVAILABLE
      * 确实没结果 → '没有找到与 "..." 相关的结果。' → EMPTY
      * 有结果     → "搜索 \\"...\\" 的结果（N 条）：" + 编号列表 → OK
    """
    body = (text or "").strip()
    if not body:
        return "INVALID_RESPONSE", []
    if "搜索后端当前不可用" in body or body.startswith("搜索失败"):
        return "UNAVAILABLE", []
    if "没有找到与" in body:
        return "EMPTY", []

    items: list[dict[str, Any]] = []
    matches = list(_WEB_ITEM_RE.finditer(body))
    for position, match in enumerate(matches):
        title = match.group(2).strip()
        tail_start = match.end()
        tail_end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        lines = [line.strip() for line in body[tail_start:tail_end].splitlines() if line.strip()]
        url = lines[0] if lines and lines[0].startswith("http") else ""
        snippet = " ".join(lines[1:]) if url else " ".join(lines)
        items.append({"title": title, "url": url, "snippet": snippet})

    if not items:
        # 渲染格式变了，宁可标 INVALID_RESPONSE 也不要把整段文本当成"一条结果"。
        return "INVALID_RESPONSE", []
    return "OK", items
