"""装配层（docs/operations/DEVELOPMENT.md §7）：只把 SuperHarness 装起来，不写业务算法。

这一层回答三个问题：

1. **用哪套 Runtime** —— `create_harness_app` 返回的 `HybridRunner`。SuperHarness 的
   Workflow Router / Capability Router / Retry / Observability / Checkpointer 全部直接
   复用，TravelPlan 不另起 Runtime（docs/operations/DEVELOPMENT.md §2.1）。
2. **有哪两条路** ——
   - `route=TRAVEL_WORKFLOW`：**主规划路径**，CLI 与 FastAPI 默认走这里。规划引擎是
     SuperHarness Agent Loop（`app/agent_runner.py::execute_agent_run`）：Agent 在循环里
     自己决定查什么、自己控制预算与时间，最后交出一份结构化行程。决定「做什么」的是
     Agent 自己，Python 只做时间冲突验证（见 feedback/inbox/2026-09-22 的方案文档）。
     旧的固定 12 步流程（`app/workflow.py`）**已不再是主路径**，代码保留待 P7 清理。
   - `route="agent"`：SuperHarness Agent Loop，处理自由追问（「第三天能不能轻松点」
     「帮我看看这个行程哪里不合理」）。此时才真正用到 Tool / Skill / MCP / Memory
     全链路。
3. **业务入口长什么样** —— `run_travel()` 是 CLI 和 FastAPI 共用的唯一入口，
   保证「CLI 与 Web 不各写一套逻辑」（docs/operations/DEVELOPMENT.md §7 / PRD §30）。

关于自动路由：这里默认**显式传 route**，不依赖 Workflow Router 打分。原因是
input 形状两难 —— 自动路由会把同一个 input 既交给 Router 打分、又交给命中的 runnable，
而固定流程要的是 `{"messages": [...]}` 形状的自然语言入口，Agent 要的是 LangGraph
messages 形状，两者不同源。SuperHarness 的 ex15 把这一点写成了明确的使用建议：
上游能判断意图就显式指定 route。需要自动路由时传 `route=None` 并自行保证 input 形状。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware import ModelFallbackMiddleware
from superharness import HarnessContext, create_dev_checkpointer, create_harness_app
from superharness.workflows import WorkflowSpec

from .agent_runner import execute_agent_run
from .config import ModelEndpoint, model_endpoints
from .prompts import TRAVEL_AGENT_SYSTEM_PROMPT
from .providers import default_mcp_servers
from .workflow import (
    DEFAULT_OUTPUT_DIR,
    WORKFLOW_DESCRIPTION,
    WORKFLOW_NAME,
    RunResult,
)

__all__ = [
    "PROJECT_ID",
    "Route",
    "TravelWorkflowRunnable",
    "build_model_with_fallbacks",
    "create_travel_app",
    "extract_query",
    "run_travel",
    "travel_checkpointer",
    "travel_workflow_spec",
]

#: 长期记忆按项目隔离：同一台机器上 SuperHarness 可能服务多个业务。
PROJECT_ID = "travelplan"


class Route:
    """可用的两条路。写成常量而不是散落的字符串字面量。"""

    WORKFLOW = WORKFLOW_NAME
    AGENT = "agent"


def travel_checkpointer():
    """开发态 Checkpointer。生产应换 PostgresSaver（见 superharness.agent 注释）。"""
    return create_dev_checkpointer()


def _chat_model(endpoint: ModelEndpoint, *, timeout: float | None = None) -> Any:
    """按一个端点构造 ChatOpenAI。构造参数与 `superharness.create_model()` 保持一致。

    为什么 `max_retries=0`：SuperHarness 的 `ModelRetryMiddleware` 已经负责重试。SDK 层再叠
    一层默认重试（3 次）会让"主模型失败"在切到备用之前先等满三次超时 —— 降级链的意义就是
    尽快换路，不是在同一条坏路上多等几倍时间。

    为什么 timeout 用 `request_timeout` 传：langchain_openai 1.6.0 的 ChatOpenAI 里这个
    字段的 canonical 名是 `request_timeout`（`timeout` 只是它的 alias），`None` = 不设超时
    （交给 SDK/Provider 默认）。调用方按业务预算传入 `config.llm_timeout_seconds`。

    为什么在 app 侧自己构造、而不是改 `superharness.create_model()`：那个函数是全仓共用的
    默认路径，改它的行为会波及所有使用者；备用模型实例只属于本业务。
    """

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=endpoint.model_name,
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        max_retries=0,
        request_timeout=timeout,
    )


def build_model_with_fallbacks() -> tuple[Any | None, list[Any]]:
    """按 .env 的「主 + bk1 + bk2」构造模型降级链，返回 `(主模型, 中间件列表)`。

    返回值直接喂给 SuperHarness 的装配口：

        model, middleware = build_model_with_fallbacks()
        create_harness_agent(model=model, middleware=middleware)

    - 至少有一套配全 → 返回 `(第一个配全的模型, [ModelFallbackMiddleware(其余…)])`；
      主模型缺失时 bk1 顶上（"主不可用"包含"主没配"）。
    - 一个都没配全 → 返回 `(None, [])`，让上游抛它原本的配置错误，不在这里改文案。
    - 只有主模型 → 返回 `(主模型, [])`：链长为 1，不装任何 middleware。

    为什么用 `ModelFallbackMiddleware` 而不是 `model.with_fallbacks([...])`
    -------------------------------------------------------------------
    `with_fallbacks()` 返回 `RunnableWithFallbacks`，它**不是** `BaseChatModel`；而
    SuperHarness 的装配链要的正是 BaseChatModel —— `create_deep_agent` 的第一步就是
    `deepagents._models.resolve_model(model)`，非 BaseChatModel 会落到
    `init_chat_model(model, **apply_provider_profile(model))` 分支；实测那一步在
    `provider_profiles.get_provider_profile` 里对 spec 调 `.count(":")`，经
    `RunnableWithFallbacks.__getattr__` 转发到 ChatOpenAI 后直接
    `AttributeError: 'ChatOpenAI' object has no attribute 'count'` —— 装配期就炸，
    根本进不到调用。`ModelFallbackMiddleware` 走 SuperHarness 既有的 `middleware=[...]`
    注入口，模型对象本身仍是普通 ChatOpenAI：Capability Router、Memory、子 Agent
    拿到的东西与以前完全一样。

    降级触发时机（与 `ModelRetryMiddleware` 的先后关系）
    -------------------------------------------------
    `superharness.agent._build_middleware` 里 `ModelRetryMiddleware` 排在列表最前面
    （即最外层），这里注入的中间件靠近模型。所以一次失败的顺序是：
        「主 → bk1 → bk2」先各试一次 → 三条都失败 → ModelRetryMiddleware 再把整条链
        按 settings.model_max_retries 重试。
    这是抛错即降级（不是"同一个模型重试 N 次再换"），也是我们要的：主模型限流/欠费时
    在同一条坏路上空耗两次没有意义。
    """

    endpoints = model_endpoints()
    if not endpoints:
        return None, []

    # 单次模型调用的超时预算：agent 路径没有按用途（tag）分档，统一用
    # `llm_timeout_seconds`（默认 180s）。放函数内 import 避免模块级循环依赖。
    from app.config import current_config

    timeout = current_config().llm_timeout_seconds
    primary = _chat_model(endpoints[0], timeout=timeout)
    backups = [_chat_model(endpoint, timeout=timeout) for endpoint in endpoints[1:]]
    if not backups:
        return primary, []
    return primary, [ModelFallbackMiddleware(*backups)]


def extract_query(payload: Any) -> str:
    """从 HybridRunner 透传下来的 input 里取出用户那句话。

    三种形状都要认：纯字符串、LangGraph 的 `{"messages": [...]}`、以及 OpenAI 风格
    的 `[{"role": "user", "content": ...}]` 列表。取不到就如实报错，不猜一个默认值
    ——猜错会变成「用户问 A，系统答 B」。

    空输入也算取不到：返回空串会让下游把「这次没拿到问题」当成「用户问了空问题」，
    两者在审计里必须能分开。
    """
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ValueError("input 里的用户输入是空的")
        return text

    if isinstance(payload, dict):
        return extract_query(payload.get("messages") or payload.get("query") or "")

    if isinstance(payload, (list, tuple)):
        for message in reversed(payload):
            if isinstance(message, str):
                return extract_query(message)
            if isinstance(message, dict):
                role = message.get("role") or message.get("type") or ""
                if role in {"user", "human"}:
                    return extract_query(message.get("content") or "")
            else:
                content = getattr(message, "content", None)
                if content is not None and getattr(message, "type", "") == "human":
                    return extract_query(content)

    raise ValueError(f"无法从 input 中取出用户输入：{type(payload).__name__}")


def _user_id_of(context: Any) -> str:
    """HarnessContext.user_id 决定「谁的长期记忆」，取不到就归到 anonymous。"""
    user_id = getattr(context, "user_id", None)
    if user_id is None and isinstance(context, dict):
        user_id = context.get("user_id")
    return str(user_id or "anonymous")


@dataclass(slots=True)
class TravelWorkflowRunnable:
    """把主规划入口包成 HybridRunner 能调用的 runnable。

    它不是「第二套 Workflow 实现」：真正的规划是 `app/agent_runner.py` 里的
    `execute_agent_run`（SuperHarness Agent Loop 自己调工具出计划）；这里只做输入形状适配
    （messages → query）与结果透传，让主规划能和 Agent Loop 一起挂在同一个 HybridRunner 下。

    名字里的 "Workflow" 是历史遗留（它对应的 route 常量仍是 `Route.WORKFLOW`）：
    route 值属于对外契约（CLI 的 `--route`、前端与文档都在用），本轮不为了改名而改名。
    """

    output_dir: Path = DEFAULT_OUTPUT_DIR
    model: Any | None = None
    store: Any | None = None
    debug: bool = False
    #: 这次运行是谁发起的（quick/cli）。benchmark 由 Benchmark runner 直接传 execute_agent_run。
    source: str = "quick"
    #: SuperHarness 原生 Observability 的上报口。由 `create_travel_app` 在装配完成后回填，
    #: 见 `_Emitter` 的说明。
    emit: Any | None = None
    _results: dict[str, RunResult] = field(default_factory=dict, repr=False)

    def invoke(self, input: Any, config: Any = None, *, context: Any = None) -> RunResult:
        result = execute_agent_run(
            extract_query(input),
            user_id=_user_id_of(context),
            thread_id=self._thread_id(config),
            output_dir=self.output_dir,
            debug=self.debug,
            store=self.store,
            model=self.model,
            emit=self.emit,
            run_id=self._run_id(input),
            source=self.source,
        )
        self._remember(result)
        return result

    async def ainvoke(self, input: Any, config: Any = None, *, context: Any = None) -> RunResult:
        """编排是同步的（Provider 都是阻塞 HTTP），所以放线程里跑，不要堵住事件循环。"""
        return await asyncio.to_thread(self.invoke, input, config, context=context)

    def _thread_id(self, config: Any) -> str | None:
        configurable = (config or {}).get("configurable") or {}
        return configurable.get("thread_id")

    def _run_id(self, input: Any) -> str | None:
        """API 后台任务预先分配 run_id；普通 CLI/Agent 路径仍由 Workflow 自己生成。"""
        if isinstance(input, dict) and isinstance(input.get("run_id"), str):
            return input["run_id"] or None
        return None

    def _remember(self, result: RunResult) -> None:
        self._results[result.run_id] = result


def travel_workflow_spec(**kwargs: Any) -> WorkflowSpec:
    """主规划路径（route=WORKFLOW）的注册信息。

    `description` 会被 Workflow Router 用 BM25 打分，所以要写清楚「什么请求该走它」
    （见 workflow.WORKFLOW_DESCRIPTION），否则自动路由会选不出来。
    """
    return WorkflowSpec(
        name=WORKFLOW_NAME,
        description=WORKFLOW_DESCRIPTION,
        runnable=TravelWorkflowRunnable(**kwargs),
    )


class _Emitter:
    """把 `HybridRunner.emit` 延后回填给先构造好的 runnable。

    装配顺序是「先造 runnable → 再 create_harness_app」：`emit` 由
    `ObservabilityMiddleware` 持有，只有拿到 runner 才取得到。所以中间放一个可变对象，
    两个方向都通过它，而不是让 runnable 反过来去猜 runner。

    runner 之外的地方（单测直接调 `execute_agent_run`）不会 bind，此时所有上报静默丢弃。
    """

    __slots__ = ("_emit",)

    def __init__(self) -> None:
        self._emit: Any | None = None

    def bind(self, emit: Any | None) -> None:
        self._emit = emit

    def __call__(
        self,
        event_type: str,
        component: str,
        *,
        data: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event_type, component, data=data or {}, status=status)
        except Exception:  # noqa: BLE001 —— 观测不能成为规划失败的原因
            pass


def create_travel_app(
    *,
    model: Any | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    checkpointer: Any | None = None,
    debug: bool = False,
    **agent_kwargs: Any,
):
    """装配并返回 HybridRunner（docs/operations/DEVELOPMENT.md §7 / PRD §25）。

    12306 MCP 的清单来自 `app.providers.default_mcp_servers()`，交给 SuperHarness 的
    MCP Router 按需握手 —— 不是每次规划都拉起 npx 进程。清单与 `execute_agent_run`
    自建 ProviderHub 时用的是同一份（各写一份会漏，12306 就这样死过一整轮）。

    主模型 + 备用模型链：只在调用方没显式传 `model` 时才由 `.env` 构造
    （`MODEL_*` / `MODEL_*_bk1` / `MODEL_*_bk2`）。调用方传了自己的模型就照用，
    不在背后换成别的 —— 那是调用方（测试 / benchmark / 灰度）唯一能控制模型的地方。
    """

    emitter = _Emitter()
    # SuperHarness 的 `middleware=` 是本业务注入降级链的唯一入口；调用方自己传的 middleware
    # 排在前面（更外层），我们的降级链贴着模型，避免改变别人中间件的既有语义。
    middleware = list(agent_kwargs.pop("middleware", None) or [])
    agent_model = model
    if agent_model is None:
        agent_model, fallback_middleware = build_model_with_fallbacks()
        middleware = [*fallback_middleware, *middleware]

    workflow = travel_workflow_spec(
        output_dir=Path(output_dir), model=model, debug=debug, emit=emitter
    )

    app = create_harness_app(
        workflows=[workflow],
        system_prompt=TRAVEL_AGENT_SYSTEM_PROMPT,
        mcp_servers=default_mcp_servers(),
        model=agent_model,
        middleware=middleware or None,
        checkpointer=checkpointer if checkpointer is not None else travel_checkpointer(),
        name="travelplan",
        **agent_kwargs,
    )
    # 回填：本次 run 里每一次 Provider 调用与模型调用都会走这个口子报到原生 EventBus，
    # 于是 Console 能看到 Router / Tool / LLM / Trace（PRD §35 A）。
    emitter.bind(getattr(app, "emit", None))
    return app


_APP: Any | None = None


def _shared_app(output_dir: str | Path, model: Any | None) -> Any:
    """进程内复用一份装配结果：装配要读 .env、加载 Router，不该每次请求都做一遍。"""
    global _APP
    if _APP is None:
        _APP = create_travel_app(model=model, output_dir=output_dir)
    return _APP


def run_travel(
    query: str,
    *,
    user_id: str = "anonymous",
    thread_id: str | None = None,
    project_id: str | None = PROJECT_ID,
    route: str | None = Route.WORKFLOW,
    model: Any | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    debug: bool = False,
    run_id: str | None = None,
    app: Any | None = None,
    source: str = "quick",
) -> RunResult:
    """CLI 与 FastAPI 共用的唯一业务入口。

    `route=Route.WORKFLOW`（默认）→ 主规划（Agent Loop 自己调工具出计划），返回 `RunResult`。
    `route=Route.AGENT`           → 自由追问的 Agent Loop，返回 LangGraph 的 messages state；
                                    这条路的 output 形状不同，调用方需要自己处理。
    """
    runner = app if app is not None else _shared_app(output_dir, model)
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": query}]}
    if run_id:
        payload["run_id"] = run_id
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None

    result = runner.invoke(
        payload,
        config=config,
        context=HarnessContext(user_id=user_id, project_id=project_id),
        route=route,
    )

    if isinstance(result, RunResult):
        return result

    # route 明确指向 Agent 时不该走到这里；真走到了说明调用方把 route 传错了，
    # 与其静默返回一个形状不同的字典，不如如实报错。
    raise TypeError(
        f"route={route!r} 返回的是 Agent Loop 的 messages state（{type(result).__name__}），"
        "不是 RunResult。主规划请用 route=Route.WORKFLOW。"
    )
