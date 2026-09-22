"""一座城市的预热 Agent（P6 预热 agent 化）：代码给定城市，Agent 自己决定搜什么、入库什么。

边界：哪归代码、哪归 Agent
-------------------------
* **归代码**（`scripts/preheat_cities.py`）：跑哪些城、串行还是并行、每天限量、跳过判定、
  退出码 —— 批调度不因为 Agent 化而变成"模型决定今天跑几座城"；以及预热的硬约束：
  城市名、入库表结构、实体归一化、步数/超时护栏、失败即不落库。
* **归 Agent**：这座城市内部「搜哪些攻略（小红书/抖音/网页）」「正文里哪些地名值得抽」
  「哪些点要到高德核实、要不要补详情」「哪些条目真的入库」。它能看到工具返回、能换关键词
  重试，但**改不了内容**：正文与 POI 一律由 `preheat_tools.RecordingHub` 从 Provider 原样
  取回（理由见那个模块的说明），模型只能"选"，不能"造"。

失败怎么算
----------
先跑 Agent，跑完才提交，因此失败路径**不写半座城**：

* Agent 超时 / 达到步数上限 / 循环抛错 → 抛 `PreheatAgentError`（缓存一个字没动）；
* Agent 跑完但一个 POI 都没登记 → 返回 `places=0` 的摘要（同样什么都没写）。

两条路在 `scripts/preheat_cities.py` 里都算该城失败（退出码 1），并如实打印原因 ——
"跑完了但没落库"与"没跑成"都不该被当成成功（没有 POI 的城市按 `read_candidates` 的口径
根本不算缓存命中）。

手工跑一次
----------
    python -c "from app.store import TravelPlanStore; from app import preheat_agent as p; \
print(p.preheat_city(TravelPlanStore(), '成都'))"

（会真联网、真花配额。生产入口仍然是 `python scripts/preheat_cities.py --daily --limit N`。）
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app import city_cache
from app.config import current_config
from app.preheat_tools import CityKnowledgeSink, build_preheat_tools
from app.prompts import CITY_PREHEAT_PROMPT_VERSION, CITY_PREHEAT_SYSTEM_PROMPT
from app.providers import ProviderHub, default_mcp_servers

logger = logging.getLogger(__name__)

__all__ = ["PREHEAT_AGENT_NAME", "PreheatAgentError", "preheat_city"]

#: Agent 名字：日志 / 观测里区分"这次是哪条链路"，与主规划的 travelplan-planner 并列。
PREHEAT_AGENT_NAME = "travelplan-city-preheat"


class PreheatAgentError(RuntimeError):
    """预热 Agent 没跑出可提交的结果（装配失败 / 超时 / 步数用尽 / 循环报错）。"""


def _empty_summary(city: str, reason: str) -> dict[str, Any]:
    """一份"什么都没做"的摘要（城市名为空这类调用方错误，不抛异常）。"""

    return {
        "city": city,
        "places": 0,
        "evidences": 0,
        "social_evidences": 0,
        "queries": 0,
        "notes": [],
        "degradations": [reason],
    }


def _model_and_fallbacks(model: Any | None) -> tuple[Any | None, list[Any]]:
    """模型降级链：调用方没传模型时按 `.env` 的「主 + bk1 + bk2」构造（与主规划同一份实现）。"""

    if model is not None:
        return model, []
    # 延迟导入：装配层（app.agent）自己依赖 agent_runner，模块级互相 import 会成环。
    from app.agent import build_model_with_fallbacks

    return build_model_with_fallbacks()


def _harness_helpers() -> tuple[Any, Any, Any]:
    """复用主规划那套 Agent 装配的接线件（唯一实现，见 `app/agent_runner.py`）。

    三样东西都属于"接错就会静默降级"的接线，各写一份迟早漂移：

    * `_AlwaysVisibleTools`：SuperHarness 的能力筛选按 `tool_top_k`（默认 3）挑工具，
      而它打分的 query 是会话里最后一条 HumanMessage —— Agent Loop 里这条消息从头到尾不变，
      不把本业务的工具补回可见列表，预热 Agent 就只能看见 3 个工具（入库工具很可能被筛掉）；
    * `_run_agent_loop`：两层超时（协程取消 + 守护线程 join 预算）。同步工具（高德是阻塞 HTTP）
      跑在执行器线程里，只靠 `asyncio.wait_for` 收尾会被卡死的调用拖住，"N 秒超时"实际不成立；
    * `_truncation_kind`：区分"步数用尽"与"普通报错"，失败原因要如实分得清。

    延迟导入是为了让 `--list` 这类只读路径不必拉起整个规划栈（planner / workflow / 观测）。
    """

    # 私有导入是有意的：这几件接线只能有一份实现（同仓复用优于复制）。
    from app.agent_runner import _AlwaysVisibleTools, _run_agent_loop, _truncation_kind

    return _AlwaysVisibleTools, _run_agent_loop, _truncation_kind


def _user_prompt(city: str) -> str:
    """给 Agent 的这一趟输入：只有城市名（system prompt 是"怎么预热"，这里是"预热哪座城"）。"""

    return (
        f"城市：{city}\n\n"
        "请开始：先搜攻略（小红书 → 抖音 → 网页兜底），把值得保留的正文登记进城市知识库，"
        "并抽出其中的地点名；再用 search_poi 到高德核实这些地点（必要时 poi_detail 补营业时间 / 地址），"
        "把真正属于这座城市的点登记进去。收尾用一句话说明本次入库规模与未获取到的部分，不要再调用工具。"
    )


def _degradations(committed: dict[str, Any]) -> list[str]:
    """把提交结果里"这次不够好"的部分如实说出来（预热写的是所有人共用的缓存）。"""

    items: list[str] = []
    if not committed.get("committed"):
        items.append("Agent 没有登记任何通过校验的高德 POI，本次没有写入城市缓存")
    if int(committed.get("evidences") or 0) and not int(committed.get("social_evidences") or 0):
        items.append(
            "本次入库的攻略全部来自网页搜索，没有任何小红书 / 抖音证据（常见原因是社媒限流或额度耗尽）"
        )
    return items


def preheat_city(
    store: Any,
    city: str,
    *,
    model: Any | None = None,
    hub: Any | None = None,
    max_steps: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """为一座城市跑一次预热 Agent，并把结果提交进城市缓存。

    返回值与 `sessions.refresh_city_cache` 同形（`city` / `places` / `evidences` /
    `social_evidences` / `queries` / `notes` / `degradations`），所以批调度那一层不需要
    为新旧两条路各写一套报告逻辑；额外多一个 `agent` 块（prompt 版本、步数/墙钟上限、
    工具调用数、本次是否提交），供日志与排障使用。

    `model` / `hub` / `max_steps` / `timeout_seconds` 可注入，是为了让离线单测能在不联网、
    不花模型配额的前提下跑完整条路。
    """

    normalized = city_cache.normalize_city(city)
    if not normalized:
        return _empty_summary(city, "城市名为空")

    config = current_config()
    steps = max(
        4, int(max_steps if max_steps is not None else config.preheat_agent_steps)
    )
    timeout = float(
        timeout_seconds if timeout_seconds is not None else config.preheat_agent_timeout_seconds
    )
    #: 城市级 run_id：与 `sessions.refresh_city_cache` 用的是同一个口径（hub 审计与实体层
    #: trace 都按它归属，但不建 run 行 —— 预热不是一次用户 run，不该出现在 runs 列表里）。
    run_id = f"city-cache:{normalized}"
    sink = CityKnowledgeSink(normalized, store=store, run_id=run_id)
    owns_hub = hub is None
    resolved_hub = hub or ProviderHub(run_id=run_id, store=None, mcp_servers=default_mcp_servers())
    started = time.perf_counter()
    try:
        agent_model, fallback = _model_and_fallbacks(model)
        if agent_model is None:
            raise PreheatAgentError(
                "未配置可用的模型：请在 .env 配 MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY"
                "（预热 Agent 需要一个能调用工具的模型），本次没有写入任何缓存。"
            )
        tools = build_preheat_tools(resolved_hub, sink)
        always_visible, run_loop, truncation_kind = _harness_helpers()

        from superharness import HarnessContext
        from superharness.agent import create_harness_agent

        agent = create_harness_agent(
            system_prompt=CITY_PREHEAT_SYSTEM_PROMPT,
            tools=tools,
            model=agent_model,
            middleware=[*fallback, always_visible(tools)],
            name=PREHEAT_AGENT_NAME,
        )
        payload = {"messages": [{"role": "user", "content": _user_prompt(normalized)}]}
        outcome, error_payload = run_loop(
            lambda: agent.ainvoke(
                payload,
                config={"recursion_limit": steps},
                context=HarnessContext(user_id=run_id),
            ),
            timeout=timeout,
        )
        if outcome == "timeout":
            raise PreheatAgentError(
                f"预热 Agent 超时（超过 {timeout:g} 秒）被中止：本次没有写入任何缓存。"
                "可调大 PREHEAT_AGENT_TIMEOUT_SECONDS，或缩小每日城市数。"
            )
        if outcome == "error":
            if truncation_kind(error_payload) == "steps":
                raise PreheatAgentError(
                    f"预热 Agent 达到最大步数（{steps} 步）仍未收尾：本次没有写入任何缓存。"
                    "可调大 MAX_PREHEAT_AGENT_STEPS，或看看是不是检索词一直失败导致它在原地重试。"
                )
            raise PreheatAgentError(
                f"预热 Agent 执行失败：{type(error_payload).__name__}: {error_payload}"
            )
        committed = sink.commit()
    finally:
        if owns_hub:
            try:
                resolved_hub.close()
            except Exception:
                logger.debug("预热 Agent 的 Hub 关闭失败", exc_info=True)

    return {
        "city": normalized,
        "places": int(committed.get("places") or 0),
        "evidences": int(committed.get("evidences") or 0),
        "social_evidences": int(committed.get("social_evidences") or 0),
        "queries": len(list(committed.get("queries") or [])),
        # `notes` 记着"这次入库了什么、哪些被剔除、哪些检索失败"这类事实：它们是解释
        # "为什么这座城市是这样"的关键，批调度会把它们打出来（见 scripts/preheat_cities.py）。
        "notes": list(committed.get("notes") or []),
        "degradations": _degradations(committed),
        "agent": {
            "prompt_version": CITY_PREHEAT_PROMPT_VERSION,
            "max_steps": steps,
            "timeout_seconds": timeout,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "committed": bool(committed.get("committed")),
            "saved_pois": int(sink.counters.get("saved_pois") or 0),
            "rejected_pois": int(sink.counters.get("rejected_pois") or 0),
            "searches": int(sink.counters.get("searches") or 0),
            "provider_calls": _provider_call_count(resolved_hub),
        },
    }


def _provider_call_count(hub: Any) -> int | None:
    """这次真打了多少次 Provider（读取失败写 None —— "不知道"不能用 0 冒充）。"""

    try:
        return len(hub.audit_entries() or [])
    except Exception:  # noqa: BLE001 —— 审计读不到只影响展示
        return None
