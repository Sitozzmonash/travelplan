"""FastAPI 层（START.md §8.2 / PRD §30）。

    uvicorn app.api:api --reload

与 CLI 共用同一个业务入口 `app.agent.run_travel` —— 这里不写规划逻辑，
只做「HTTP 形状 ↔ RunResult」的翻译，以及把 store 里的东西如实吐出来。

关于同步/异步：规划一次要串几十个真实 Provider 调用，属于长任务。V1 按 PRD §30
「不强制做异步任务队列」处理成同步请求；路由写成 `def` 而不是 `async def`，
让 FastAPI 自动丢进线程池，避免堵住事件循环。

关于 Secret：Provider Key 只在后端环境变量里，接口一律不回显；`/health` 只报
「配了没配」，不报值。
"""

from __future__ import annotations

import os
import hmac
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import PROJECT_ID, Route, create_travel_app, run_travel
from .config import EDITABLE_KEYS, current_config, set_runtime_overrides, validate_override
from .evolution import lever_catalog, run_evolution, tuning_snapshot
from .models import TripPlan
from . import sessions
from .db import describe_configured_backend
from .store import TravelPlanStore
from .workflow import DEFAULT_OUTPUT_DIR, WORKFLOW_NAME, new_run_id, plan_payload

# 管理端的聚合视图（Dashboard / 质量 / 漏斗 / Bad Case 聚类 / 轨迹）各自成模块：
# 它们只读既有数据、不改业务逻辑，放在这里会让本文件变成几千行的统计代码仓库。
# 路由在文件末尾注册（见"管理端聚合视图"一节）。
from .admin_analytics import build_admin_router, enrich_runs
from .admin_timeline import build_timeline_router

#: 只允许下载这些产物。白名单而不是拼接路径，避免 `../` 穿越。
ARTIFACTS: dict[str, str] = {
    "plan.json": "application/json",
    "plan.md": "text/markdown; charset=utf-8",
    "audit_report.json": "application/json",
    "trace.jsonl": "application/x-ndjson",
    "metrics.json": "application/json",
    "badcases.json": "application/json",
}

#: 前端开发服务器默认端口。生产部署应改成真实域名。
DEFAULT_CORS_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")

#: 置为真值时跳过"启动收敛孤儿 run"。测试用它避免在装配期写共享库。
SKIP_ORPHAN_SWEEP_ENV = "TRAVELPLAN_SKIP_ORPHAN_SWEEP"

#: 进程被重启（Render 免费层会在 512Mi OOM 后重启）时，跑在后台线程里的 run 随进程消失，
#: 状态就永远停在 RUNNING，前端会一直转圈。启动时收敛一次，前端立刻能看到 CANCELLED
#: （前端本来就有这个状态的文案："通常是服务重启或任务被手动取消"）。
ORPHAN_RUN_REASON = "服务重启导致本次运行中断（进程内的 Run 执行器随进程结束），请重新发起规划"


def _converge_orphan_runs() -> None:
    """启动时把上一进程遗留的 RUNNING 收敛成 CANCELLED。"""

    if os.environ.get(SKIP_ORPHAN_SWEEP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    try:
        cancelled = get_store().cancel_orphan_runs(reason=ORPHAN_RUN_REASON)
    except Exception:  # noqa: BLE001 —— 收敛失败不能拖住服务启动
        return
    if cancelled:
        print(
            f"[travelplan] 已把上次进程遗留的 {len(cancelled)} 条 RUNNING 收敛为 CANCELLED："
            f"{', '.join(cancelled[:5])}"
        )


@asynccontextmanager
async def _lifespan(_app: Any) -> AsyncIterator[None]:
    """启动期只做一件事：收敛上一进程遗留的孤儿 run。

    用 lifespan 而不是 `@api.on_event`：后者在 FastAPI 里已经废弃，新代码不该再引入。
    """

    _converge_orphan_runs()
    yield


api = FastAPI(
    title="TravelPlan API",
    version="0.1.0",
    description="中国国内旅行规划 Agent：Evidence First, LLM Second。",
    lifespan=_lifespan,
)


def _cors_origins() -> list[str]:
    raw = os.environ.get("TRAVELPLAN_CORS_ORIGINS", "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


api.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    # Authorization 必须在白名单里：管理端用 Bearer Token，缺了它浏览器会先被 CORS 挡掉，
    # 于是"鉴权失败"会伪装成"接口不通"，排查方向直接被带偏。
    allow_headers=["Content-Type", "Authorization"],
)


# ======================================================================
# 请求 / 响应模型
# ======================================================================


class CreatePlanRequest(BaseModel):
    message: str = Field(min_length=1, description="用户自然语言需求")
    background: bool = Field(
        default=False,
        description="true 时立即返回 run_id，客户端用 /status 轮询真实的阶段状态。",
    )


class CreatePlanResponse(BaseModel):
    run_id: str
    status: str
    plan: dict[str, Any] | None = None
    message: str | None = None


class ReviseRequest(BaseModel):
    action: str = Field(description="replace | remove | lock | relax_day | lower_budget")
    day_index: int | None = None
    item_id: str | None = None


class ReviseResponse(BaseModel):
    run_id: str
    status: str
    message: str


REVISE_ACTIONS = {"replace", "remove", "lock", "relax_day", "lower_budget"}


# ======================================================================
# 依赖
# ======================================================================


_STORE: TravelPlanStore | None = None


def get_store() -> TravelPlanStore:
    """进程内**复用**一份 store。

    为什么不每次新建：`TravelPlanStore.__init__` 会跑一遍建表 DDL，Postgres 下那是
    二十多条 `CREATE TABLE IF NOT EXISTS` **走网络**。每次请求都付一遍的后果是实测过的：
    Render 的 5 秒健康探针超时 → 部署被判不健康而回滚（`update_failed`），
    管理端接口也白等这些往返。store 自身线程安全（SQLite 每次操作一条短连接，
    Postgres 复用进程内连接池），可以直接复用。

    测试会把这个函数整体换掉（`monkeypatch.setattr(api_module, "get_store", ...)`），
    所以这里的缓存不会影响用例。
    """

    global _STORE
    if _STORE is None:
        _STORE = TravelPlanStore()
    return _STORE


_APP: Any | None = None
_RUN_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="travelplan-run")


def get_runner() -> Any:
    """进程内复用一份 HybridRunner（装配要读 .env、建 Router，不该每请求一遍）。"""
    global _APP
    if _APP is None:
        _APP = create_travel_app(output_dir=_output_dir())
    return _APP


def _output_dir() -> Path:
    return Path(os.environ.get("TRAVELPLAN_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """管理 API 默认关闭，防止 Trace/错误详情在生产环境被公开。"""
    expected = os.environ.get("TRAVELPLAN_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="管理接口未配置 TRAVELPLAN_ADMIN_TOKEN")
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="管理接口需要有效的 Bearer Token")


def _run_background(run_id: str, message: str) -> None:
    """后台线程只复用既有唯一业务入口，不另写一套规划逻辑。"""
    run_travel(
        message,
        route=Route.WORKFLOW,
        output_dir=_output_dir(),
        run_id=run_id,
        app=get_runner(),
    )


# ======================================================================
# 路由
# ======================================================================


def _load_runtime_overrides() -> None:
    """启动时把落库的运行时覆盖装载进配置层（重启后仍然生效）。"""

    try:
        set_runtime_overrides(
            {key: item["value"] for key, item in get_store().list_runtime_config().items()}
        )
    except Exception:  # noqa: BLE001 —— 装载失败就用默认配置启动，不影响服务可用
        return


_load_runtime_overrides()


@api.get("/api/v1/health")
def health() -> dict[str, Any]:
    """只上报「配了没配」和能不能连库，不回显任何 Key / 连接串凭据的值。

    `store` 里同时报**后端类型**：SQLite 报本地库路径，Postgres 报脱敏后的
    `postgresql://***@host/db`（绝不回显用户名/密码）。连库失败时也要如实报
    "配置目标是哪个后端"，而不是假装一切正常。

    顶层另有两个**契约字段**（部署契约，见 `docs/09_部署说明.md` §2.4；供部署/监控直接消费，不必再解析
    `store` 嵌套结构）：`database_backend = sqlite|postgres`、
    `database_connected = true|false`。`database_connected` 表示"这次真的把库连上了"
    （一次 `SELECT 1` 走通；建表在 store 构造时已经做过，探针里不重复跑 DDL）；
    配了 `DATABASE_URL` 却连不上时它为 `false` 且 `status=degraded`，
    **绝不会**因为退回本地 SQLite 而变成 `true`。
    """
    store_ok = True
    store_error: str | None = None
    store_info: dict[str, Any] = {}
    try:
        store = get_store()
        # 只做一次 `SELECT 1`，**不跑建表**。建表在 store 构造时就做过；把二十多条
        # `CREATE TABLE IF NOT EXISTS` 放在健康检查里意味着每次探针都走一遍远端网络 ——
        # Render 的健康检查只给 5 秒，Neon 冷启动时必然超时，部署会被判为不健康回滚
        # （实测 update_failed: "HTTP health check failed (timed out after 5 seconds)"）。
        store.ping()
        store_info = store.describe()
    except Exception as exc:  # noqa: BLE001 —— 健康检查要能报告"库坏了"而不是自己崩
        store_ok = False
        store_error = f"{type(exc).__name__}: {exc}"
    if not store_info:
        # 连不上时仍要说清"本该是 sqlite 还是 postgres"，否则线上分不清是哪种故障。
        try:
            store_info = describe_configured_backend()
        except Exception:  # noqa: BLE001 —— 描述失败也不能让 /health 自己 500
            store_info = {}
    store_info["ok"] = store_ok
    store_info["error"] = store_error

    configured = {
        name: bool(os.environ.get(name))
        for name in (
            "MODEL_NAME",
            "MODEL_BASE_URL",
            "MODEL_API_KEY",
            "AMAP_API_KEY",
            "TIKHUB_API_TOKEN",
        )
    }
    return {
        "status": "ok" if store_ok else "degraded",
        "workflow": WORKFLOW_NAME,
        "project_id": PROJECT_ID,
        # 部署契约字段（docs/09_部署说明.md §2.4）：后端类型固定二选一，连接状态来自"真的连上并建表"。
        "database_backend": store_info.get("backend") or "unknown",
        "database_connected": store_ok,
        "store": store_info,
        "providers_configured": configured,
        "notes": [
            "此接口只报告环境变量是否已配置，不返回值。",
            "tuniu / 12306 MCP 由各自进程提供，是否可用请看具体 run 的 audit_report.json。",
        ],
    }


@api.post("/api/v1/plans", response_model=CreatePlanResponse)
def create_plan(request: CreatePlanRequest):
    """创建规划；保留同步兼容路径，并提供真实的后台轮询路径。"""
    if request.background:
        run_id = new_run_id()
        get_store().create_run(run_id, original_query=request.message)
        _RUN_EXECUTOR.submit(_run_background, run_id, request.message)
        return JSONResponse(
            status_code=202,
            content={
                "run_id": run_id,
                "status": "RUNNING",
                "plan": None,
                "message": "规划已开始；请轮询 /api/v1/plans/{run_id}/status 获取真实进度。",
            },
        )

    result = run_travel(
        request.message,
        route=Route.WORKFLOW,
        output_dir=_output_dir(),
        app=get_runner(),
    )

    if result.status == "needs_clarification":
        # 4xx 而不是 200：这确实是一次没有产出 plan 的请求，前端会按服务端错误提示。
        return JSONResponse(
            status_code=422,
            content={
                "run_id": result.run_id,
                "status": result.status,
                "plan": None,
                "message": result.clarification,
            },
        )

    if not result.ok or result.plan is None:
        return JSONResponse(
            status_code=500,
            content={
                "run_id": result.run_id,
                "status": result.status,
                "plan": None,
                "message": result.error or "规划过程中出错，未产出可用行程。",
            },
        )

    return CreatePlanResponse(
        run_id=result.run_id,
        status=result.status,
        plan=plan_payload(result.plan, get_store()),
        message=None if not result.degradations else "；".join(result.degradations[:5]),
    )


@api.get("/api/v1/plans/{run_id}/status")
def get_plan_status(run_id: str) -> dict[str, Any]:
    """长任务真实状态：阶段记录只在节点实际开始/结束时更新。"""
    progress = get_store().get_run_progress(run_id)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")
    return progress


# ======================================================================
# 管理 API（必须鉴权；列表接口只给摘要，详情才按需加载 trace）
# ======================================================================

#: 只有这些列允许被管理员改写。白名单而不是"任意字段"，避免把 PATCH 变成可以改
#: run_id / 分类 / 时间的后门。
BADCASE_PATCH_FIELDS = (
    "analysis_status",
    "fixed_status",
    "root_cause_status",
    "suspected_root_cause",
    "severity",
    "introduced_in",
    "fixed_in",
)

ANALYSIS_STATUSES = ("pending", "analyzed")
FIXED_STATUSES = ("unfixed", "fixed", "wontfix")
ROOT_CAUSE_STATUSES = ("suspected", "verified", "rejected")
SEVERITIES = ("low", "medium", "high")


class BadCasePatch(BaseModel):
    analysis_status: str | None = None
    fixed_status: str | None = None
    root_cause_status: str | None = None
    suspected_root_cause: str | None = None
    severity: str | None = None
    introduced_in: str | None = None
    fixed_in: str | None = None


class BenchmarkRequest(BaseModel):
    suites: list[str] | None = Field(default=None, description="留空表示跑全部离线套件")
    jev_enabled: bool | None = Field(default=None, description="留空跟随当前配置")
    limit: int | None = Field(default=None, ge=1, le=200)
    live: bool = Field(default=False, description="true 时额外跑 Live Smoke（会真的打第三方）")


class EvolutionRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


def _span_attributes(span: dict) -> dict[str, Any]:
    attributes = span.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _jev_calls_from_spans(spans: list[dict]) -> list[dict]:
    """把 component=jev 的 span 还原成管理端 Run Detail 的 "Jev Calls"。"""

    calls: list[dict] = []
    for span in spans:
        if span.get("component") != "jev":
            continue
        attributes = _span_attributes(span)
        calls.append(
            {
                "tag": attributes.get("tag") or span.get("name"),
                "decision_type": attributes.get("decision_type"),
                "status": attributes.get("status"),
                "choice": attributes.get("choice"),
                "confidence": attributes.get("confidence"),
                "latency_ms": attributes.get("latency_ms"),
                "model": attributes.get("model"),
                "fallback": attributes.get("fallback"),
                "fallback_reason": attributes.get("fallback_reason"),
                "quota": attributes.get("quota"),
                "input_summary": attributes.get("input_summary"),
                "criteria": attributes.get("criteria"),
                "error": span.get("error"),
            }
        )
    return calls


def _llm_calls_from_spans(spans: list[dict]) -> list[dict]:
    """component=llm 的 span → 管理端的 "LLM 调用" 列表。

    预览与 token 直接取 span 属性（``app/llm.py`` 写下的脱敏截断值）；早期版本记录的
    run 没有这些属性，此时一律回 None —— 不回 0/空串，管理端才不会把"没记"显示成
    "模型没消耗 token"。
    """

    calls: list[dict] = []
    for span in spans:
        if span.get("component") != "llm":
            continue
        attributes = _span_attributes(span)
        calls.append(
            {
                "tag": attributes.get("tag") or span.get("name"),
                "model": attributes.get("model"),
                "status": attributes.get("status"),
                "duration_ms": attributes.get("duration_ms"),
                "chars": attributes.get("chars"),
                "error": span.get("error"),
                "started_at": span.get("started_at"),
                "finished_at": span.get("finished_at"),
                "input_tokens": attributes.get("input_tokens"),
                "output_tokens": attributes.get("output_tokens"),
                "cached_tokens": attributes.get("cached_tokens"),
                "total_tokens": attributes.get("total_tokens"),
                "system_preview": attributes.get("system_preview"),
                "user_preview": attributes.get("user_preview"),
                "context_preview": attributes.get("context_preview"),
                "assistant_preview": attributes.get("assistant_preview"),
                "prompt_version": attributes.get("prompt_version"),
                "prompt_hash": attributes.get("prompt_hash"),
            }
        )
    return calls


@api.get("/api/v1/admin/overview", dependencies=[Depends(require_admin)])
def admin_overview(include_tokens: bool = True) -> dict[str, Any]:
    """历史累计量。``total_tokens`` 是**累计口径**（所有 run 的 run_metrics 之和），
    与 Dashboard 那个"平均每个规划的 token"（窗口 + 只看有快照的 run）不是一回事，
    两个口径各有自己的标签，不要互相引用。

    ``include_tokens`` 默认 True 保持既有契约；不需要累计用量的消费方传 false 即可，
    这样以后再往这里加明细也不会让首屏响应跟着长。
    """

    store = get_store()
    runs = store.list_runs(limit=200)
    statuses: dict[str, int] = {}
    for run in runs:
        status = str(run.get("status") or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1

    badcase_open = store.count_badcases(analysis_status="pending")
    badcase_total = store.count_badcases()
    evolves = store.list_evolution_runs(limit=1)
    benchmarks = store.list_benchmark_runs(limit=1)

    payload = {
        "run_count": store.count_runs(),
        "statuses": statuses,
        "degraded_count": statuses.get("DEGRADED", 0),
        "failed_count": statuses.get("FAILED", 0),
        "running_count": statuses.get("RUNNING", 0),
        "total_llm_calls": sum(int(run.get("llm_calls") or 0) for run in runs),
        "total_jev_calls": sum(int(run.get("jev_calls") or 0) for run in runs),
        "total_tool_calls": sum(int(run.get("tool_calls") or 0) for run in runs),
        "provider_failures": sum(int(run.get("provider_failures") or 0) for run in runs),
        "badcase_open": badcase_open,
        "badcase_total": badcase_total,
        "jev": _jev_health(store, runs),
        "benchmark": {
            "latest_run_id": benchmarks[0]["benchmark_run_id"] if benchmarks else None,
            # 数组而不是逗号串：字段名是复数，前端的契约校验按数组断言（曾经这里是字符串，
            # 结果页面判为"结构不符合契约"整块不渲染 —— 契约要按消费者需要的形状给）。
            "latest_suites": _suite_list(benchmarks[0]) if benchmarks else [],
            "latest_pass_rate": _pass_rate(benchmarks[0]) if benchmarks else None,
        },
        "evolution": {
            "enabled": current_config().evolution_enabled,
            "pending_badcases": badcase_open,
            "last_run_id": evolves[0]["evolution_run_id"] if evolves else None,
            "last_decision": evolves[0]["decision"] if evolves else None,
        },
    }
    if include_tokens:
        # 累计口径：没有 token 快照的 run 贡献 0（它本来就没被计入），所以这里是下界而不是 0。
        payload["total_tokens"] = sum(int(run.get("total_tokens") or 0) for run in runs)
        payload["total_tokens_runs"] = sum(
            1 for run in runs if run.get("total_tokens") is not None
        )
    return payload


def _pass_rate(benchmark_run: dict) -> float | None:
    case_count = int(benchmark_run.get("case_count") or 0)
    passed = benchmark_run.get("passed")
    if not case_count or passed is None:
        return None
    return round(int(passed) / case_count, 4)


def _suite_list(benchmark_run: dict) -> list[str]:
    """`benchmark_runs.suite` 存的是逗号串（一次可以跑多个套件），对外给数组。"""

    raw = benchmark_run.get("suite")
    if not raw:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _jev_health(store: TravelPlanStore, runs: list[dict]) -> dict[str, Any]:
    """Jev 健康度：从最近的 run 的 jev span 汇总。

    ``quota`` 只报告**服务端真的回过**的额度；没有就写 ``unknown`` —— 不推算、不编造。
    """

    config = current_config()
    calls = 0
    fallback = 0
    statuses: dict[str, int] = {}
    latencies: list[int] = []
    quota: Any = "unknown"
    last_error: str | None = None
    for run in runs[:20]:
        for call in _jev_calls_from_spans(store.get_trace_spans(run["run_id"])):
            calls += 1
            status = str(call.get("status") or "UNKNOWN")
            statuses[status] = statuses.get(status, 0) + 1
            if call.get("fallback"):
                fallback += 1
            if isinstance(call.get("latency_ms"), int):
                latencies.append(call["latency_ms"])
            if call.get("quota") not in (None, "unknown"):
                quota = call["quota"]
            if call.get("error"):
                last_error = str(call["error"])
    return {
        "enabled": config.jev_enabled,
        "configured": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
        "status": "ok" if calls and not fallback else ("no-calls" if not calls else "degraded"),
        "latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "quota": quota,
        "quota_source": "reported" if quota != "unknown" else "unknown",
        "fallback_count": fallback,
        "calls": calls,
        "low_confidence": statuses.get("LOW_CONFIDENCE", 0),
        "timeout": statuses.get("TIMEOUT", 0),
        "invalid_response": statuses.get("INVALID_RESPONSE", 0),
        "last_error": last_error,
        "statuses": statuses,
    }


# ======================================================================
# 引导式旅程：Planning Session（无需鉴权 —— 它还不代表一次正式 Run）
# ======================================================================


class CreateSessionRequest(BaseModel):
    origin: str = Field(min_length=1, description="出发地")
    destination: str = Field(min_length=1, description="目的地")
    start_date: str | None = None
    end_date: str | None = None
    days: int | None = Field(default=None, ge=1, le=60)
    travelers: int | None = Field(default=None, ge=1, le=30)
    budget_total: float | None = None


class PatchSessionRequest(BaseModel):
    transport_mode: str | None = None
    transport_priority: str | None = None
    transport_constraints: list[str] | None = None
    hotel_priority: str | None = None
    hotel_max_price_per_night: float | None = None
    hotel_min_rating: float | None = None
    #: 最低星级（当前数据源不返回星级，见 capabilities.hotel_star_filter）
    hotel_min_star: float | None = None
    hotel_room_type: str | None = None
    #: 是否接受中途换酒店。前端发布尔、老客户端可能发 yes/no/auto —— 两种都收。
    hotel_allow_change: bool | str | None = None
    pace: str | None = None
    budget_total: float | None = None
    poi_selections: dict[str, str] | None = None


@api.post("/api/v1/planning-sessions", status_code=202)
def create_planning_session(request: CreateSessionRequest) -> dict[str, Any]:
    """建会话并**立刻**在后台开始 Discovery（用户还在选偏好时数据已经在查了）。"""

    session = sessions.create_session(
        get_store(),
        request.model_dump(exclude_none=True),
        submit=lambda fn, *args: _RUN_EXECUTOR.submit(fn, *args),
    )
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "discovery_status": session["discovery_status"],
    }


@api.get("/api/v1/planning-sessions/{session_id}")
def get_planning_session(session_id: str) -> dict[str, Any]:
    session = sessions.get_session(get_store(), session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有 session_id={session_id} 的会话")
    view = sessions.session_view(session)
    return _with_decision_fields(view, session)


def _with_decision_fields(view: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """把契约 2 的三个决策字段补到会话视图上（B 的追加，不改 A 的既有字段）。

    用 ``setdefault``：一旦 A 在 `session_view` 里自己透传了这三个键（A3 的职责），
    这里就自动让位，不会用一份可能为空的副本把 A 的数据盖掉。
    """

    prefetch = session.get("prefetch") or {}
    view.setdefault("profile", prefetch.get("profile"))
    view.setdefault("hotel_areas", prefetch.get("hotel_areas") or [])
    view.setdefault("poi_pools", prefetch.get("poi_pools") or {})
    return view


@api.patch("/api/v1/planning-sessions/{session_id}")
def patch_planning_session(session_id: str, request: PatchSessionRequest) -> dict[str, Any]:
    session = sessions.patch_session(
        get_store(), session_id, request.model_dump(exclude_unset=True)
    )
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有 session_id={session_id} 的会话")
    return _with_decision_fields(sessions.session_view(session), session)


@api.delete("/api/v1/planning-sessions/{session_id}")
def cancel_planning_session(session_id: str) -> dict[str, Any]:
    session = sessions.cancel_session(get_store(), session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有 session_id={session_id} 的会话")
    return {"session_id": session["session_id"], "status": session["status"]}


@api.post("/api/v1/planning-sessions/{session_id}/start", status_code=202)
def start_planning_session(session_id: str) -> dict[str, Any]:
    """只有走到这里才创建正式 run_id。"""

    outcome = sessions.start_run(
        get_store(),
        session_id,
        submit=lambda fn, *args: _RUN_EXECUTOR.submit(fn, *args),
        output_dir=str(_output_dir()),
    )
    if outcome.get("error") == "not_found":
        raise HTTPException(status_code=404, detail=f"没有 session_id={session_id} 的会话")
    if outcome.get("error") == "session_closed":
        raise HTTPException(status_code=409, detail="会话已取消或已过期，请重新开始")
    return outcome


@api.get("/api/v1/city-cache/{city}")
def get_city_cache_candidates(city: str) -> dict[str, Any]:
    """读取可公开展示的城市候选缓存；未命中时让客户端走正常会话 Discovery。

    这个接口**不需要鉴权**，所以只返回候选与攻略的元信息，**不外发攻略正文**
    （第三方内容 + 单城市可达数百 KB）；正文只在会话 Discovery 与正式 run 内部使用。
    """

    from app import city_cache

    hit = city_cache.read_candidates(city, store=get_store())
    if hit is None:
        raise HTTPException(status_code=404, detail=f"城市 {city!r} 暂无有效缓存")
    return hit.payload(include_evidence_text=False)


@api.post("/api/v1/admin/city-cache/{city}/refresh", status_code=202, dependencies=[Depends(require_admin)])
def refresh_city_cache_candidates(city: str) -> dict[str, Any]:
    """管理员触发城市知识异步刷新；同城并发请求会合并，避免重复消耗 Provider 配额。"""

    from app import city_cache

    normalized = city_cache.normalize_city(city)
    if not normalized:
        raise HTTPException(status_code=422, detail="城市不能为空")
    city_cache.refresh_in_background(
        normalized, refresh=lambda cached_city: sessions.refresh_city_cache(get_store(), cached_city)
    )
    return {"city": normalized, "status": "refresh_queued"}


@api.get("/api/v1/admin/city-cache/{city}/entities", dependencies=[Depends(require_admin)])
def admin_city_entities(city: str, limit: int = 200) -> dict[str, Any]:
    """这座城市的 Canonical Place 实体底座（只读）。

    为什么需要它：候选列表只给"用户能选的地点"，而"同一个现实地点为什么只有一个身份"
    要看实体层本身 —— 实体、别名、Provider 引用、主点子点关系、查询缓存各自的规模。
    这些数字是判断"实体层到底建起来了没有"的唯一依据，不该只存在于库里。
    """

    from app import city_cache

    store = get_store()
    normalized = city_cache.normalize_city(city)
    if not normalized:
        raise HTTPException(status_code=422, detail="城市不能为空")
    cap = max(1, min(int(limit), 1000))
    entities = store.get_canonical_places(normalized)
    kinds: dict[str, int] = {}
    for row in entities:
        key = str(row.get("kind") or "place")
        kinds[key] = kinds.get(key, 0) + 1
    return {
        "city": normalized,
        "counts": {
            "entities": len(entities),
            "places": kinds.get("place", 0),
            "facilities": kinds.get("facility", 0),
            "aliases": len(store.get_place_aliases(normalized)),
            "provider_refs": len(store.get_place_provider_refs(normalized)),
            "relations": len(store.get_place_relations(normalized)),
            "evidence_links": len(store.get_place_evidence_links(normalized)),
            "query_cache": len(store.get_poi_query_cache_rows(normalized)),
        },
        "kinds": kinds,
        "entities": entities[:cap],
    }


@api.get("/api/v1/admin/places/resolver-traces", dependencies=[Depends(require_admin)])
def admin_place_resolver_traces(
    run_id: str | None = None,
    session_id: str | None = None,
    city: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """地点实体解析器的留痕（方案 §21）。

    每条回答一件事：这个地点被**复用 / 新建 / 合并 / 折叠 / 丢弃**了，依据是什么。
    "这次为什么没有重新查高德"这类问题只能从这里回答 —— 只看得见结果、看不见判断过程，
    错并或漏并就永远无法复盘。
    """

    store = get_store()
    if not any([run_id, session_id, city]):
        raise HTTPException(status_code=422, detail="至少给一个过滤条件（run_id / session_id / city）")
    rows = store.list_place_resolver_traces(
        run_id=run_id, session_id=session_id, city=city, action=action, limit=limit
    )
    return {"count": len(rows), "filter": {"run_id": run_id, "session_id": session_id, "city": city, "action": action}, "traces": rows}


@api.get("/api/v1/admin/runs", dependencies=[Depends(require_admin)])
def admin_runs(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    q: str | None = Query(default=None, description="按 run_id 或原始需求模糊搜索"),
    include_benchmark: bool = Query(
        default=False,
        description="是否包含 Benchmark 用例运行。默认不包含：一次评测会产生几十条 run，"
        "会把真实运行挤出列表。",
    ),
) -> dict[str, Any]:
    store = get_store()
    # 列表只补"这次行程好不好、有几个问题"两列质量信息；token / LLM / 工具调用
    # 这些技术指标下沉到详情页 —— 一屏 12 列没人扫得动（见 docs/TravelPlan_Admin_整体优化方案.md §6）。
    return {
        "items": enrich_runs(
            store,
            store.list_runs(
                limit=limit, offset=offset, status=status, q=q, include_benchmark=include_benchmark
            ),
        ),
        "limit": limit,
        "offset": offset,
        "total": store.count_runs(status=status, q=q, include_benchmark=include_benchmark),
    }


@api.get("/api/v1/admin/planning-sessions", dependencies=[Depends(require_admin)])
def admin_planning_sessions(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    status: str | None = None,
    destination: str | None = None,
    has_run: bool | None = None,
) -> dict[str, Any]:
    """Guided Session 列表：从"用户前置选择"追溯到最终 Run 的入口。"""

    store = get_store()
    items, total = store.list_planning_sessions(
        limit=limit, offset=offset, q=q, status=status, destination=destination, has_run=has_run
    )
    return {
        "items": [_admin_session_row(item) for item in items],
        "limit": limit,
        "offset": offset,
        "total": total,
    }


@api.get("/api/v1/admin/planning-sessions/{session_id}", dependencies=[Depends(require_admin)])
def admin_planning_session_detail(session_id: str) -> dict[str, Any]:
    store = get_store()
    session = sessions.get_session(store, session_id, expire=False)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有 session_id={session_id} 的会话")
    view = sessions.session_view(session)
    # 详情按文档信封返回：session / preferences / discovery / prefetch_summary / events / run_link。
    # 分成几块而不是一个大对象：前端每一块独立折叠与独立降级，缺一块不影响其余部分渲染。
    basic = session.get("basic_intent") or {}
    prefetch = session.get("prefetch") or {}
    discovery = dict(session.get("discovery") or {})
    run_id = session.get("run_id")
    run = store.get_run(run_id) if run_id else None
    progress = store.get_run_progress(run_id) if run_id else None
    selections = dict(session.get("poi_selections") or {})
    places = {str(card.get("place_id")): str(card.get("name")) for card in session.get("place_candidates") or []}
    return {
        "session": {
            **view,
            "discovery": None,  # 下面单独给，避免同一份数据出现两种形状
        },
        "basic_info": {
            **basic,
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "expires_at": session.get("expires_at"),
        },
        "preferences": view.get("preferences"),
        "preference_labels": view.get("preference_labels"),
        "discovery": {
            "overall": session.get("discovery_status"),
            "stages": discovery,
            "degradations": session.get("degradations") or [],
        },
        "prefetch_summary": {
            "transport_candidates": len(prefetch.get("outbound") or []) + len(prefetch.get("inbound") or []),
            "hotel_candidates": len(prefetch.get("hotels") or []),
            "evidence_count": len(prefetch.get("evidences") or []),
            "place_candidates": len(prefetch.get("places") or []),
            "provider_calls": len(prefetch.get("provider_calls") or []),
        },
        "poi_selections": {
            "counts": {
                "must": sum(1 for v in selections.values() if str(v).upper() == "MUST"),
                "want": sum(1 for v in selections.values() if str(v).upper() == "WANT"),
                "reject": sum(1 for v in selections.values() if str(v).upper() == "REJECT"),
                "neutral": len(session.get("place_candidates") or []) - len(selections),
            },
            "items": [
                {"place_id": key, "name": places.get(str(key), str(key)), "state": str(value).upper()}
                for key, value in selections.items()
            ],
        },
        "events": session.get("events") or [],
        "run_link": {
            "has_run": bool(run_id),
            "run_id": run_id,
            "run_status": (progress or {}).get("status") if progress else (run or {}).get("status"),
            "started_at": (progress or {}).get("started_at") if progress else None,
            "finished_at": (progress or {}).get("finished_at") if progress else None,
        },
        # 角色 B 的追加（契约 2）：动态偏好画像 / 住宿区域 / 候选池。字段名与 user 会话视图、
        # 与 run 的 user_journey 一致，管理端不必区分数据来自哪个入口。
        # A 的 prefetch_json 透传（A3）就绪后这里即为真实数据；未就绪时为空占位。
        "decision": {
            "profile": prefetch.get("profile"),
            "hotel_areas": prefetch.get("hotel_areas") or [],
            "poi_pools": prefetch.get("poi_pools") or {},
        },
    }


def _admin_session_row(session: dict[str, Any]) -> dict[str, Any]:
    basic = session.get("basic_intent") or {}
    preferences = session.get("preferences") or {}
    selections = session.get("poi_selections") or {}
    counts: dict[str, int] = {"must": 0, "want": 0, "reject": 0}
    for state in selections.values():
        key = str(state).lower()
        if key in counts:
            counts[key] += 1
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "discovery_status": session["discovery_status"],
        "origin": basic.get("origin"),
        "destination": basic.get("destination"),
        "start_date": basic.get("start_date"),
        "days": basic.get("days"),
        "travelers": basic.get("travelers"),
        "budget_total": basic.get("budget_total"),
        "transport_priority": preferences.get("transport_priority"),
        "hotel_priority": preferences.get("hotel_priority"),
        "pace": preferences.get("pace"),
        "must_count": counts["must"],
        "want_count": counts["want"],
        "reject_count": counts["reject"],
        "place_count": len(session.get("place_candidates") or []),
        "run_id": session.get("run_id"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "expires_at": session.get("expires_at"),
    }


@api.get("/api/v1/admin/runs/{run_id}", dependencies=[Depends(require_admin)])
def admin_run_detail(run_id: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")
    spans = store.get_trace_spans(run_id)
    badcases, _ = store.list_badcases(run_id=run_id, limit=200)
    progress = store.get_run_progress(run_id) or {}
    # 起止时间在 run_progress 里（runs 表没有这两列）。合并进 run 对象，
    # 免得消费方要同时看两张表才知道"到底几点结束的"（曾经因此整天显示"—"）。
    run = {
        **run,
        "started_at": progress.get("started_at"),
        "finished_at": progress.get("finished_at"),
    }
    metrics_row = store.get_run_metrics(run_id) or {}
    # cost_source / 单价快照不在 run_metrics 表里（表结构固定）。这里按"成本非空即由用户单价算出"
    # 拼出来，让前端能区分"后端没给定价"与"按你填的单价估算"。
    config = current_config()
    if metrics_row.get("cost") is not None:
        metrics_row = {
            **metrics_row,
            "cost_source": "user_price",
            "cost_breakdown": {
                "input_tokens": metrics_row.get("input_tokens"),
                "output_tokens": metrics_row.get("output_tokens"),
                "cached_tokens": metrics_row.get("cached_tokens"),
                "input_price_per_million": config.model_price_input_per_million,
                "output_price_per_million": config.model_price_output_per_million,
                "cached_price_per_million": config.model_price_cached_per_million,
            },
        }
    return {
        "run": run,
        # 用上面补过 cost_source / cost_breakdown 的 metrics_row。这里曾经同时写了
        # 两个 "metrics" 键（后一个直接取原始行），字典字面量是后者胜，于是成本拆分
        # 永远到不了前端 —— 表现成"填了单价也没有成本明细"。
        "metrics": metrics_row,
        "user_journey": _user_journey(store, run, store.get_plan(run_id)),
        "stages": _stage_details(store, run_id),
        "progress": store.get_run_progress(run_id),
        "trace": spans,
        "decisions": store.get_decisions(run_id),
        "provider_calls": store.list_sources(run_id),
        # 明细从 span 还原：它与管理端看到的 trace 是同一份事实，不会出现两套数字。
        "jev_calls": _jev_calls_from_spans(spans),
        "llm_calls": _llm_calls_from_spans(spans),
        "badcases": badcases,
    }


@api.get("/api/v1/admin/badcases", dependencies=[Depends(require_admin)])
def admin_badcases(
    limit: int = 50,
    offset: int = 0,
    analysis_status: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    store = get_store()
    items, total = store.list_badcases(
        limit=limit,
        offset=offset,
        analysis_status=analysis_status,
        category=category,
        severity=severity,
        run_id=run_id,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": store.badcase_facets(),
    }


@api.get("/api/v1/admin/badcases/{badcase_id}", dependencies=[Depends(require_admin)])
def admin_badcase_detail(badcase_id: str) -> dict[str, Any]:
    store = get_store()
    badcase = store.get_badcase(badcase_id)
    if badcase is None:
        raise HTTPException(status_code=404, detail=f"没有 badcase_id={badcase_id} 的记录")
    spans = store.get_trace_spans(badcase["run_id"])
    by_id = {span["span_id"]: span for span in spans}
    return {
        "badcase": badcase,
        # 把 trace_refs 展开成真实 span，管理端可以直接跳转；找不到的保持原样并标 missing。
        "trace": [
            {"ref": ref, "span": by_id.get(ref), "missing": ref not in by_id}
            for ref in badcase.get("trace_refs") or []
        ],
    }


@api.patch("/api/v1/admin/badcases/{badcase_id}", dependencies=[Depends(require_admin)])
def admin_update_badcase(badcase_id: str, patch: BadCasePatch) -> dict[str, Any]:
    store = get_store()
    if store.get_badcase(badcase_id) is None:
        raise HTTPException(status_code=404, detail=f"没有 badcase_id={badcase_id} 的记录")

    fields = patch.model_dump(exclude_none=True)
    unknown = set(fields) - set(BADCASE_PATCH_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"不可修改的字段：{sorted(unknown)}")
    # 枚举校验放在服务端：前端下拉框只是便利，不是约束。
    for value, allowed, label in (
        (fields.get("analysis_status"), ANALYSIS_STATUSES, "analysis_status"),
        (fields.get("fixed_status"), FIXED_STATUSES, "fixed_status"),
        (fields.get("root_cause_status"), ROOT_CAUSE_STATUSES, "root_cause_status"),
        (fields.get("severity"), SEVERITIES, "severity"),
    ):
        if value is not None and value not in allowed:
            raise HTTPException(status_code=422, detail=f"{label} 必须是 {list(allowed)} 之一")

    updated = store.update_badcase(badcase_id, fields)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"没有 badcase_id={badcase_id} 的记录")
    return updated


# ----------------------------------------------------------------------
# Benchmark
# ----------------------------------------------------------------------


def _benchmark_runner() -> Any:
    """延迟导入：`benchmark/` 是评测资产，不属于服务运行时的必需依赖。

    导入失败时给出明确 503，而不是让管理端看到一个 500 堆栈 —— "没安装评测套件"
    和"评测跑挂了"是两件事。
    """

    try:
        from benchmark.runner import run_benchmark
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Benchmark 套件不可用：{type(exc).__name__}: {exc}") from exc
    return run_benchmark


def _run_benchmark_job(benchmark_run_id: str, request: BenchmarkRequest) -> None:
    try:
        runner = _benchmark_runner()
        runner(
            store=get_store(),
            suites=request.suites,
            jev_enabled=request.jev_enabled,
            limit=request.limit,
            live=request.live,
            benchmark_run_id=benchmark_run_id,
        )
    except Exception as exc:  # noqa: BLE001 —— 后台任务不能把进程打挂，状态如实落库
        store = get_store()
        store.update_benchmark_run(
            benchmark_run_id,
            {"status": "FAILED", "notes": f"{type(exc).__name__}: {exc}", "finished_at": _now()},
        )


@api.get("/api/v1/admin/benchmark/runs", dependencies=[Depends(require_admin)])
def admin_benchmark_runs(limit: int = 20) -> dict[str, Any]:
    runs = get_store().list_benchmark_runs(limit=limit)
    for run in runs:
        run["pass_rate"] = _pass_rate(run)
    return {"items": runs, "limit": limit}


@api.get("/api/v1/admin/benchmark/runs/{benchmark_run_id}", dependencies=[Depends(require_admin)])
def admin_benchmark_detail(benchmark_run_id: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_benchmark_run(benchmark_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有 benchmark_run_id={benchmark_run_id} 的记录")
    run["pass_rate"] = _pass_rate(run)
    metrics = run.get("metrics") or {}
    case_results = store.get_benchmark_case_results(benchmark_run_id)
    groups: dict[str, Any] = {}
    if isinstance(metrics, dict) and isinstance(metrics.get("groups"), dict):
        groups = metrics["groups"]
    else:
        # 兼容"扁平指标"的写法：按指标名前缀归组，保证管理端永远能画出六个维度。
        groups = _group_metrics(metrics if isinstance(metrics, dict) else {})
    return {
        "run": run,
        "metrics": groups,
        "case_results": case_results,
        "baseline_compare": _baseline_compare(_benchmark_baselines()),
    }


def _benchmark_baselines() -> tuple[dict[str, Any], dict[str, Any]]:
    """读两份基线文件；读不到就返回空（页面显示空表，而不是崩）。"""

    try:
        from benchmark.runner import BASELINES_DIR
    except Exception:  # noqa: BLE001
        return {}, {}
    out: list[dict[str, Any]] = []
    for mode in ("off", "on"):
        path = BASELINES_DIR / f"jev_{mode}.json"
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
        except (OSError, ValueError):
            out.append({})
    return out[0], out[1]


def _baseline_compare(pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any] | None:
    """返回契约里的 `{off, on, delta_pct}` 形状。

    历史上这里返回的是扁平结构（quality_delta_with_jev 摊在顶层），而前端按
    `Object.keys(baseline.off)` 渲染 → 直接抛 "Cannot convert undefined or null to
    object"，整个 Benchmark 详情页白屏。契约与实现必须对齐，以文档形状为准。
    """

    off, on = pair
    off_metrics = dict((off or {}).get("metrics") or {})
    on_metrics = dict((on or {}).get("metrics") or {})
    if not off_metrics and not on_metrics:
        return None
    keys = sorted(set(off_metrics) | set(on_metrics))
    delta_pct: dict[str, float | None] = {}
    for key in keys:
        base = off_metrics.get(key)
        current = on_metrics.get(key)
        if isinstance(base, (int, float)) and isinstance(current, (int, float)) and base:
            delta_pct[key] = round((current - base) / abs(base) * 100, 2)
        else:
            delta_pct[key] = None
    return {
        "off": {key: off_metrics.get(key) for key in keys},
        "on": {key: on_metrics.get(key) for key in keys},
        "delta_pct": delta_pct,
        "off_generated_at": (off or {}).get("generated_at"),
        "on_generated_at": (on or {}).get("generated_at"),
    }


#: 扁平指标 → 六维分组的前缀表（与 §9 的指标清单一致）。
METRIC_GROUP_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hard_constraints", ("date_mismatch", "hard_time_conflict", "opening_hours", "missing_transport",
                          "missing_hotel", "budget_math", "hallucinated", "missing_source",
                          "hard_constraint")),
    ("plan_quality", ("category_diversity", "consecutive_same_type", "backtracking", "daily_load",
                      "meal_time", "pace_", "preference_coverage", "first_day", "last_day", "route_efficiency")),
    ("evidence", ("evidence_coverage", "multi_source", "poi_verified", "unsupported_recommendation")),
    ("provider", ("provider_success", "fallback_success", "timeout_rate")),
    ("performance", ("latency", "_tokens", "calls", "e2e")),
    ("jev", ("jev_", "quality_delta", "latency_delta")),
)


def _group_metrics(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {name: {} for name, _ in METRIC_GROUP_PREFIXES}
    groups.setdefault("other", {})
    for key, value in metrics.items():
        if key in ("groups", "baseline_compare", "version", "fixture_version"):
            continue
        placed = False
        for name, prefixes in METRIC_GROUP_PREFIXES:
            if any(key.startswith(prefix) or prefix.strip("_") in key for prefix in prefixes):
                groups[name][key] = value
                placed = True
                break
        if not placed:
            groups["other"][key] = value
    return {name: values for name, values in groups.items() if values}


@api.post("/api/v1/admin/benchmark/runs", status_code=202, dependencies=[Depends(require_admin)])
def admin_start_benchmark(request: BenchmarkRequest = Body(default=BenchmarkRequest())) -> dict[str, Any]:
    _benchmark_runner()  # 先确认套件可用，避免建了一条永远停在 RUNNING 的记录
    benchmark_run_id = f"bm-{_now().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"
    config = current_config()
    get_store().save_benchmark_run(
        {
            "benchmark_run_id": benchmark_run_id,
            "suite": ",".join(request.suites) if request.suites else "all",
            "version": "v0.1",
            "jev_enabled": 1 if (config.jev_enabled if request.jev_enabled is None else request.jev_enabled) else 0,
            "live": 1 if request.live else 0,
            "status": "RUNNING",
            "started_at": _now(),
            "notes": "由管理端触发",
        }
    )
    _RUN_EXECUTOR.submit(_run_benchmark_job, benchmark_run_id, request)
    return {"benchmark_run_id": benchmark_run_id, "status": "RUNNING"}


# ----------------------------------------------------------------------
# Config / Evolution
# ----------------------------------------------------------------------


def _config_payload() -> dict[str, Any]:
    config = current_config()
    store = get_store()
    return {
        "config": config.public_dict(),
        "values": {**config.public_dict(), **tuning_snapshot()},
        "editable_keys": sorted(EDITABLE_KEYS),
        "runtime_overrides": store.list_runtime_config(),
        "editable_spec": {
            key: {"kind": spec[0], "min": spec[1], "max": spec[2]}
            for key, spec in EDITABLE_KEYS.items()
        },
        "planner_tuning": tuning_snapshot(),
        "secret_configured": {
            "jev": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
            "admin": bool(os.environ.get("TRAVELPLAN_ADMIN_TOKEN")),
            "amap": bool(os.environ.get("AMAP_API_KEY")),
            "tikhub": bool(os.environ.get("TIKHUB_API_TOKEN")),
            "tuniu": bool(os.environ.get("TUNIU_API_KEY")),
        },
        "evolution": {"enabled": config.evolution_enabled, "levers": lever_catalog()},
        "note": (
            "配置来自服务端环境变量；Secret 永不通过 API 回显，这里只报“配了没配”。"
            "非 Secret 项可以在本页修改并立即生效，改动落库、重启后仍在；"
            "Benchmark/Evolution 的对照实验期间，显式环境变量优先于这里的覆盖。"
        ),
    }


@api.get("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def admin_config() -> dict[str, Any]:
    return _config_payload()


class ConfigPatchRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list, description="要恢复默认（删除覆盖）的键")


@api.patch("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def admin_update_config(request: ConfigPatchRequest) -> dict[str, Any]:
    """改非 Secret 的运行时配置。

    校验必须在写入前做：把 JEV_MIN_CONFIDENCE 手滑写成 8（本意 0.8）会让所有 Jev 决策
    变成低置信度，而线上不会报任何错 —— 只是"突然都不采纳 Jev 了"。
    """

    store = get_store()
    applied: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    for key, value in request.values.items():
        try:
            applied[key] = validate_override(key, value)
        except ValueError as exc:
            invalid[str(key)] = str(exc)
    if invalid:
        raise HTTPException(status_code=422, detail={"message": "以下配置未通过校验", "errors": invalid})
    for key, value in applied.items():
        store.set_runtime_config(key, value, updated_by="admin")
    for key in request.reset:
        if key in EDITABLE_KEYS:
            store.delete_runtime_config(key)
    # 重新装载：本次进程立刻生效，不必重启
    set_runtime_overrides({key: item["value"] for key, item in store.list_runtime_config().items()})
    return _config_payload()


@api.get("/api/v1/admin/jev/health", dependencies=[Depends(require_admin)])
def admin_jev_health() -> dict[str, Any]:
    store = get_store()
    health = _jev_health(store, store.list_runs(limit=20))
    health["note"] = (
        "quota 只在 Jev 响应里真的带了额度时才有值，否则为 unknown（不推算、不伪造）；"
        "fallback_count 是这些 run 里最终由 Python 确定性决策的次数。"
    )
    return health


#: Provider → 中文名（展示用）。未列出的 Provider 原样显示 —— 页面必须能自动扩展。
PROVIDER_LABELS: dict[str, str] = {
    "12306": "铁路 12306",
    "tuniu": "途牛",
    "amap": "高德地图",
    "tikhub": "小红书 / 抖音（TikHub）",
    "mediacrawler": "本地抓取（MediaCrawler）",
    "tavily": "网页搜索",
    "jev": "Jev 决策",
    "unknown": "未知来源",
}

#: 状态判定阈值：只看**最近这批调用**，样本太小时不硬下结论。
PROVIDER_SAMPLE_FOR_STATUS = 5
PROVIDER_DEGRADED_FAILURE_RATE = 0.2
PROVIDER_UNAVAILABLE_FAILURE_RATE = 0.6


def _provider_status_reason(stats: dict[str, Any], status: str) -> str:
    """给 `UNKNOWN` 一个具体说法。

    "没有调用历史"与"样本不足以下结论"在运维上是两件事：前者要确认是不是没用起来，
    后者只需要再攒几次调用。只给一个 UNKNOWN 会让人无从下手。
    """

    calls = int(stats.get("calls") or 0)
    if calls == 0:
        return "no_history"
    if status == "UNKNOWN":
        return "insufficient_sample"
    if status == "HEALTHY":
        return "ok"
    if status == "DEGRADED":
        return "elevated_failure_rate"
    if status == "UNAVAILABLE":
        return "high_failure_rate"
    return "unknown"


def _provider_status(stats: dict[str, Any]) -> str:
    """健康状态。没有调用历史时必须是 UNKNOWN，**不能**显示成失败。

    这条例外很重要：刚部署、或某个 Provider 这周没被用到，都不是故障。
    把"没有数据"画成红色会让运维去查一个根本没坏的东西。
    """

    calls = int(stats.get("calls") or 0)
    last_status = stats.get("last_status")
    if calls == 0:
        return "UNKNOWN"
    if last_status in {"AUTH_ERROR", "UNAVAILABLE"} and calls >= PROVIDER_SAMPLE_FOR_STATUS:
        rate = int(stats.get("failures") or 0) / calls
        return "UNAVAILABLE" if rate >= PROVIDER_UNAVAILABLE_FAILURE_RATE else "DEGRADED"
    if calls < PROVIDER_SAMPLE_FOR_STATUS:
        # 样本太少：只说"最近一次是什么状态"，不下健康结论。
        return "UNKNOWN" if last_status == "OK" else "DEGRADED"
    rate = int(stats.get("failures") or 0) / calls
    if rate >= PROVIDER_UNAVAILABLE_FAILURE_RATE:
        return "UNAVAILABLE"
    if rate >= PROVIDER_DEGRADED_FAILURE_RATE:
        return "DEGRADED"
    return "HEALTHY"


def _provider_configured() -> dict[str, bool]:
    return {
        "12306": bool(os.environ.get("RAILWAY_12306_COMMAND", "npx")),
        "tuniu": bool(os.environ.get("TUNIU_API_KEY")),
        "amap": bool(os.environ.get("AMAP_API_KEY")),
        "tikhub": bool(os.environ.get("TIKHUB_API_TOKEN")),
        "mediacrawler": bool(os.environ.get("MEDIACRAWLER_DIR")),
        "tavily": bool(os.environ.get("TAVILY_API_KEY")),
        "jev": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
    }


@api.get("/api/v1/admin/providers", dependencies=[Depends(require_admin)])
def admin_providers(limit_per_provider: int = 200) -> dict[str, Any]:
    """Provider Health：用**真实调用账本**聚合，不做假探活。

    口径：每个 Provider 取最近 `limit_per_provider` 次调用（固定样本量，才能让不同
    Provider 的成功率可比 —— 它们的调用量差几十倍）。没有任何调用历史 → UNKNOWN。
    """

    store = get_store()
    configured = _provider_configured()
    configured_keys = list(configured)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stats in store.provider_call_stats(limit_per_provider=limit_per_provider):
        provider = str(stats["provider"])
        seen.add(provider)
        rows.append(
            {
                **stats,
                "label": PROVIDER_LABELS.get(provider, provider),
                "configured": configured.get(provider),
                "status": _provider_status(stats),
                "status_reason": _provider_status_reason(stats, _provider_status(stats)),
                "success_rate": round(int(stats["successes"]) / int(stats["calls"]), 4)
                if stats["calls"]
                else None,
                "failure_rate": round(int(stats["failures"]) / int(stats["calls"]), 4)
                if stats["calls"]
                else None,
            }
        )
    # 配置了但最近没被调用过的 Provider 也要出现（状态 UNKNOWN），
    # 否则运维会以为"这个数据源不存在"，而不是"这段时间没用到"。
    for provider in configured_keys:
        if provider in seen:
            continue
        rows.append(
            {
                "provider": provider,
                "label": PROVIDER_LABELS.get(provider, provider),
                "configured": configured[provider],
                "status": "UNKNOWN",
                "status_reason": "no_history",
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "timeouts": 0,
                "auth_errors": 0,
                "rate_limited": 0,
                "empty": 0,
                "fallback_count": 0,
                "last_call_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "avg_latency_ms": None,
                "p95_latency_ms": None,
                "tools": [],
                "sources": [],
                "last_error": None,
                "last_status": None,
                "success_rate": None,
                "failure_rate": None,
            }
        )
    rows.sort(key=lambda row: (row["status"] == "HEALTHY", row["provider"]))
    failing = [row["provider"] for row in rows if row["status"] in {"DEGRADED", "UNAVAILABLE"}]
    return {
        "items": rows,
        "summary": {
            "providers": len(rows),
            "healthy": sum(1 for row in rows if row["status"] == "HEALTHY"),
            "degraded": sum(1 for row in rows if row["status"] == "DEGRADED"),
            "unavailable": sum(1 for row in rows if row["status"] == "UNAVAILABLE"),
            "unknown": sum(1 for row in rows if row["status"] == "UNKNOWN"),
            "needs_attention": failing,
        },
        "note": (
            "统计口径：每个 Provider 最近 "
            f"{limit_per_provider} 次真实调用（含 Discovery 阶段）。"
            "没有调用历史显示 UNKNOWN，不代表故障；本页不主动打第三方接口（不做假探活），"
            "也不会回显任何密钥。"
        ),
    }


@api.get("/api/v1/admin/providers/{provider}", dependencies=[Depends(require_admin)])
def admin_provider_detail(provider: str, limit: int = 20) -> dict[str, Any]:
    """某个 Provider 的最近调用明细（含来源：Discovery / 正式 Run / Benchmark）。"""

    store = get_store()
    calls = store.list_provider_calls(provider=provider, limit=limit)
    stats = next(
        (row for row in store.provider_call_stats() if row["provider"] == provider), None
    )
    return {
        "provider": provider,
        "label": PROVIDER_LABELS.get(provider, provider),
        "configured": _provider_configured().get(provider),
        "status": _provider_status(stats) if stats else "UNKNOWN",
        "status_reason": (
            _provider_status_reason(stats, _provider_status(stats)) if stats else "no_history"
        ),
        "stats": stats,
        "calls": calls,
        "note": "调用明细不含任何密钥；query 只保留解释'查了什么'的键值。",
    }


@api.get("/api/v1/admin/evolution", dependencies=[Depends(require_admin)])
def admin_evolution_state() -> dict[str, Any]:
    store = get_store()
    return {
        "enabled": current_config().evolution_enabled,
        "pending_badcases": store.count_badcases(analysis_status="pending"),
        "analyzed_badcases": store.count_badcases(analysis_status="analyzed"),
        "runs": store.list_evolution_runs(limit=20),
        "levers": lever_catalog(),
        "note": "演进只处理 analysis_status=pending 的 Bad Case；本轮不会自动改代码或自动上线。",
    }


@api.get("/api/v1/admin/evolution/runs/{evolution_run_id}", dependencies=[Depends(require_admin)])
def admin_evolution_detail(evolution_run_id: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_evolution_run(evolution_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有 evolution_run_id={evolution_run_id} 的记录")
    experiences = store.list_experiences(evolution_run_id=evolution_run_id)
    badcase_ids = {bid for experience in experiences for bid in experience.get("source_badcases") or []}
    badcases = [store.get_badcase(bid) for bid in sorted(badcase_ids)]
    return {
        "run": run,
        "experiences": experiences,
        "badcases": [badcase for badcase in badcases if badcase is not None],
    }


def _evolution_measure() -> Any:
    """把 Benchmark 的基线段包装成 Evolution 需要的 measure(overrides) -> metrics。"""

    try:
        from benchmark.runner import make_measure
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"评测入口不可用：{type(exc).__name__}: {exc}") from exc
    return make_measure(get_store())


def _run_evolution_job(evolution_run_id: str, limit: int) -> None:
    store = get_store()
    try:
        measure = _evolution_measure()
    except HTTPException as exc:
        # 没有评测入口：仍然把 Bad Case 聚类并给出建议，只是 decision 一定是 REJECT。
        measure = None
        store.update_evolution_run(
            evolution_run_id, {"summary": f"评测入口不可用，本次只做聚类与建议：{exc.detail}"}
        )
    try:
        run_evolution(store, measure=measure, limit=limit, evolution_run_id=evolution_run_id)
    except Exception as exc:  # noqa: BLE001
        store.update_evolution_run(
            evolution_run_id,
            {"status": "FAILED", "summary": f"{type(exc).__name__}: {exc}", "finished_at": _now()},
        )


@api.post("/api/v1/admin/evolution/runs", status_code=202, dependencies=[Depends(require_admin)])
def admin_start_evolution(request: EvolutionRequest = Body(default=EvolutionRequest())) -> dict[str, Any]:
    if not current_config().evolution_enabled:
        raise HTTPException(
            status_code=409,
            detail="EVOLUTION_ENABLED=false：演进只能显式开启后触发（避免在长驻服务里被误触）",
        )
    evolution_run_id = f"evo-{_now().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"
    get_store().save_evolution_run(
        {
            "evolution_run_id": evolution_run_id,
            "status": "RUNNING",
            "summary": "由管理端触发",
        }
    )
    _RUN_EXECUTOR.submit(_run_evolution_job, evolution_run_id, request.limit)
    return {"evolution_run_id": evolution_run_id, "status": "RUNNING"}


def _now() -> str:
    from .observability import now_iso

    return now_iso()


@api.get("/api/v1/plans/{run_id}")
def get_plan(run_id: str) -> dict[str, Any]:
    """读回一份已落库的计划。前端 `getPlan()` 直接消费。"""
    stored = get_store().get_plan(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的计划")

    try:
        plan = TripPlan.model_validate(stored["plan"])
    except Exception as exc:  # noqa: BLE001 —— 库里存了不合法的结构，如实报 500
        raise HTTPException(status_code=500, detail=f"run_id={run_id} 的计划数据无法解析") from exc

    payload = plan_payload(plan, get_store())
    payload["plan_md"] = stored.get("plan_md") or ""
    return payload


@api.get("/api/v1/plans/{run_id}/audit")
def get_audit(run_id: str) -> dict[str, Any]:
    """读回规划依据。前端 `getAudit()` 直接消费。"""
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")

    stored = store.get_plan(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"run_id={run_id} 还没有产出计划")

    audit_path = _output_dir() / run_id / "audit_report.json"
    if not audit_path.is_file():
        raise HTTPException(status_code=404, detail=f"run_id={run_id} 的 audit_report.json 不存在")

    import json

    return json.loads(audit_path.read_text(encoding="utf-8"))


def _stage_details(store: TravelPlanStore, run_id: str) -> list[dict[str, Any]]:
    """每个阶段的完整明细：步骤、facts、该阶段的工具/模型调用。

    为什么要有这个：`run_stages` 只存摘要，`steps`（人话解释列表）以前只写进
    audit_report.json，于是管理端点开阶段看不到内容。这里把 trace / 调用账本按阶段
    归拢，让"这一阶段到底干了什么、花了多少"一次看全。
    """

    progress = store.get_run_progress(run_id) or {}
    spans = store.get_trace_spans(run_id)
    run_metrics = store.get_run_metrics(run_id) or {}
    audit = _load_audit(run_id)
    audit_stages = {item.get("id"): item for item in (audit.get("stages") or []) if isinstance(item, dict)}

    rows: list[dict[str, Any]] = []
    for stage in progress.get("stages") or []:
        stage_id = stage.get("stage_id")
        related = [span for span in spans if (span.get("parent_span_id") or "").endswith(f":{stage_id}")]
        source = audit_stages.get(stage_id) or {}
        rows.append(
            {
                "stage_id": stage_id,
                "title": source.get("title") or stage_id,
                "status": stage.get("status"),
                "message": stage.get("message") or "",
                "started_at": stage.get("started_at"),
                "finished_at": stage.get("finished_at"),
                "duration_ms": _duration_between(stage.get("started_at"), stage.get("finished_at")),
                "steps": source.get("steps") or [],
                "facts": stage.get("facts") or {},
                "tool_calls": [
                    _span_attributes(span)
                    for span in related
                    if span.get("component") in ("tool", "provider", "mcp")
                ],
                # 与顶层的 llm_calls 用同一个还原函数：阶段视图与调用列表的形状必须一致，
                # 前端才不用为"这一行有没有 started_at"分两套渲染。多出来的也只是 span 的
                # 起止时刻（attributes 里没有这两列），原有的属性一个不少。
                "llm_calls": _llm_calls_from_spans(related),
                "tokens": {
                    "input_tokens": run_metrics.get("input_tokens"),
                    "output_tokens": run_metrics.get("output_tokens"),
                    "cached_tokens": run_metrics.get("cached_tokens"),
                    "total_tokens": run_metrics.get("total_tokens"),
                    "note": "这四项是整次 run 的合计（run_metrics）；每个阶段/每次调用的用量在"
                    " llm_calls 里逐条给出（模型回报的 usage，取不到的为 null 而不是 0）",
                },
            }
        )
    return rows


def _duration_between(started: str | None, finished: str | None) -> int | None:
    if not (started and finished):
        return None
    from datetime import datetime

    try:
        return int(
            (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() * 1000
        )
    except ValueError:
        return None


def _load_audit(run_id: str) -> dict[str, Any]:
    path = _output_dir() / run_id / "audit_report.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _user_journey(store: TravelPlanStore, run: dict[str, Any], stored_plan: dict | None) -> dict[str, Any]:
    """这次 run 的"用户前置选择"：来源、策略、POI 计数、prefetch 是否复用。

    管理端默认折叠（用户旅程落地任务 §22：适度可见，不做复杂行为分析系统）。

    分阶段交接结果（`discovery`：复用预取 / 本次补查 / 无可用结果、`prefetch_stages`、
    `grace_waited_ms`）在 run 的审计里已经算好了，这里**并进来**：管理端要能直接回答
    "这次 Guided run 哪部分是复用的、哪部分补查了"，而不是自己去 Trace 里猜。
    """

    session_id = run.get("source_session_id")
    session = store.get_planning_session(session_id) if session_id else None
    intent: dict[str, Any] = {}
    if isinstance(stored_plan, dict) and isinstance(stored_plan.get("plan"), dict):
        intent = stored_plan["plan"].get("intent") or {}
    selections = (session or {}).get("poi_selections") or {}
    base: dict[str, Any] = {
        "source": str(run.get("source") or "quick"),
        "source_session_id": session_id,
        "transport_mode": intent.get("transport_mode"),
        "transport_priority": intent.get("transport_priority"),
        "hotel_priority": intent.get("hotel_priority"),
        "pace": intent.get("pace"),
        "place_selections": {
            "must": sum(1 for value in selections.values() if str(value).upper() == "MUST"),
            "want": sum(1 for value in selections.values() if str(value).upper() == "WANT"),
            "reject": sum(1 for value in selections.values() if str(value).upper() == "REJECT"),
        },
        "prefetch_reused": bool((session or {}).get("prefetch")) if session else None,
        "discovery_status": (session or {}).get("discovery_status"),
    }
    rich = (_load_audit(str(run.get("run_id") or "")) or {}).get("user_journey")
    if isinstance(rich, dict):
        base.update({key: value for key, value in rich.items() if value is not None})
    return base


@api.get("/api/v1/admin/runs/{run_id}/stages", dependencies=[Depends(require_admin)])
def admin_run_stages(run_id: str) -> dict[str, Any]:
    store = get_store()
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")
    return {"run_id": run_id, "items": _stage_details(store, run_id)}


@api.post("/api/v1/plans/{run_id}/revise", response_model=ReviseResponse)
def revise_plan(run_id: str, request: ReviseRequest) -> ReviseResponse:
    """V1 只做请求校验与登记，**不**重新规划（FRONTEND_DESIGN §22 允许 v0 预留）。

    这条路径刻意不做「假装修好了」：没有真正重跑规划，就明确告诉调用方
    页面上的行程不会变化，而不是返回一个 completed 让前端以为改了。
    """
    if request.action not in REVISE_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 {sorted(REVISE_ACTIONS)} 之一，收到 {request.action!r}",
        )
    if get_store().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")
    if request.action == "replace" and not request.item_id:
        raise HTTPException(status_code=422, detail="replace 必须带 item_id")
    if request.action == "remove" and not request.item_id:
        raise HTTPException(status_code=422, detail="remove 必须带 item_id")
    if request.action == "relax_day" and request.day_index is None:
        raise HTTPException(status_code=422, detail="relax_day 必须带 day_index")

    target = request.item_id or (f"Day {request.day_index}" if request.day_index is not None else "整个行程")
    return ReviseResponse(
        run_id=run_id,
        status="queued",
        message=(
            f"已登记修改请求（{request.action} → {target}）。"
            "V1 的重新规划需要重跑完整流程（重新查实时价格与路线），不由本接口同步完成，"
            "因此页面上的行程不会变化；请回到首页重新发起一次规划。"
        ),
    )


@api.get("/api/v1/plans/{run_id}/artifacts/{filename}")
def get_artifact(run_id: str, filename: str) -> FileResponse:
    """下载 plan.json / plan.md / audit_report.json。"""
    media_type = ARTIFACTS.get(filename)
    if media_type is None:
        raise HTTPException(status_code=404, detail=f"不提供 {filename}；可选：{sorted(ARTIFACTS)}")

    # run_id 来自 URL，必须先确认它真是一条已落库的 run，再拼路径。
    if get_store().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")

    path = _output_dir() / run_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"run_id={run_id} 的 {filename} 不存在")
    return FileResponse(path, media_type=media_type, filename=filename)


# ======================================================================
# 管理端聚合视图（Dashboard / 行程质量 / 漏斗 / Bad Case 聚类 / 轨迹）
# ======================================================================

# 注册动作放在文件末尾：上面全是"HTTP 形状 ↔ RunResult"的翻译，聚合统计各自成模块，
# 这里只把它们的 router 挂上来。两个 router 都自带 require_admin 依赖，
# 不依赖具体路由是不是忘了加 —— 新增端点默认就是受保护的。


def _admin_store() -> TravelPlanStore:
    """聚合 router 的取库入口。

    为什么是这一层转发而不是把 `get_store` 函数对象直接传进去：测试用
    ``monkeypatch.setattr(api_module, "get_store", ...)`` 换 store 替身，
    传对象会让两个新页面绕过替换、直连真库。每次调用重新查一次模块全局，
    替换才对它们同样生效。
    """

    return get_store()


api.include_router(build_admin_router(_admin_store, require_admin))
api.include_router(build_timeline_router(_admin_store, require_admin))
