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
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import PROJECT_ID, Route, create_travel_app, run_travel
from .config import current_config
from .evolution import lever_catalog, run_evolution, tuning_snapshot
from .models import TripPlan
from .store import TravelPlanStore, default_db_path
from .workflow import DEFAULT_OUTPUT_DIR, WORKFLOW_NAME, new_run_id, plan_payload

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

api = FastAPI(
    title="TravelPlan API",
    version="0.1.0",
    description="中国国内旅行规划 Agent：Evidence First, LLM Second。",
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


def get_store() -> TravelPlanStore:
    """每次调用新建一个 store；它内部是「每次操作一条 SQLite 连接」，线程安全。"""
    return TravelPlanStore()


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


@api.get("/api/v1/health")
def health() -> dict[str, Any]:
    """只上报「配了没配」和能不能连库，不回显任何 Key 的值。"""
    store_ok = True
    store_error: str | None = None
    try:
        get_store().init_schema()
    except Exception as exc:  # noqa: BLE001 —— 健康检查要能报告"库坏了"而不是自己崩
        store_ok = False
        store_error = f"{type(exc).__name__}: {exc}"

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
        "store": {"ok": store_ok, "path": str(default_db_path()), "error": store_error},
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
    """component=llm 的 span → 管理端的 "LLM 调用" 列表。"""

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
            }
        )
    return calls


@api.get("/api/v1/admin/overview", dependencies=[Depends(require_admin)])
def admin_overview() -> dict[str, Any]:
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

    return {
        "run_count": store.count_runs(),
        "statuses": statuses,
        "degraded_count": statuses.get("DEGRADED", 0),
        "failed_count": statuses.get("FAILED", 0),
        "running_count": statuses.get("RUNNING", 0),
        "total_tokens": sum(int(run["total_tokens"] or 0) for run in runs),
        "total_llm_calls": sum(int(run["llm_calls"] or 0) for run in runs),
        "total_jev_calls": sum(int(run["jev_calls"] or 0) for run in runs),
        "total_tool_calls": sum(int(run["tool_calls"] or 0) for run in runs),
        "provider_failures": sum(int(run["provider_failures"] or 0) for run in runs),
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


@api.get("/api/v1/admin/runs", dependencies=[Depends(require_admin)])
def admin_runs(limit: int = 50, offset: int = 0, status: str | None = None) -> dict[str, Any]:
    store = get_store()
    return {
        "items": store.list_runs(limit=limit, offset=offset, status=status),
        "limit": limit,
        "offset": offset,
        "total": store.count_runs(status=status),
    }


@api.get("/api/v1/admin/runs/{run_id}", dependencies=[Depends(require_admin)])
def admin_run_detail(run_id: str) -> dict[str, Any]:
    store = get_store()
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有 run_id={run_id} 的运行记录")
    spans = store.get_trace_spans(run_id)
    badcases, _ = store.list_badcases(run_id=run_id, limit=200)
    return {
        "run": run,
        "metrics": store.get_run_metrics(run_id) or {},
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
        "baseline_compare": (metrics.get("baseline_compare") if isinstance(metrics, dict) else None),
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


@api.get("/api/v1/admin/config", dependencies=[Depends(require_admin)])
def admin_config() -> dict[str, Any]:
    config = current_config()
    return {
        "config": config.public_dict(),
        "planner_tuning": tuning_snapshot(),
        "secret_configured": {
            "jev": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
            "admin": bool(os.environ.get("TRAVELPLAN_ADMIN_TOKEN")),
            "amap": bool(os.environ.get("AMAP_API_KEY")),
            "tikhub": bool(os.environ.get("TIKHUB_API_TOKEN")),
            "tuniu": bool(os.environ.get("TUNIU_API_KEY")),
        },
        "evolution": {"enabled": config.evolution_enabled, "levers": lever_catalog()},
        "note": "配置来自服务端环境变量；Secret 永不通过 API 回显，这里只报“配了没配”。",
    }


@api.get("/api/v1/admin/jev/health", dependencies=[Depends(require_admin)])
def admin_jev_health() -> dict[str, Any]:
    store = get_store()
    health = _jev_health(store, store.list_runs(limit=20))
    health["note"] = (
        "quota 只在 Jev 响应里真的带了额度时才有值，否则为 unknown（不推算、不伪造）；"
        "fallback_count 是这些 run 里最终由 Python 确定性决策的次数。"
    )
    return health


@api.get("/api/v1/admin/providers", dependencies=[Depends(require_admin)])
def admin_providers() -> dict[str, Any]:
    configured = {
        "railway_12306": bool(os.environ.get("RAILWAY_12306_COMMAND", "npx")),
        "tuniu": bool(os.environ.get("TUNIU_API_KEY")),
        "amap": bool(os.environ.get("AMAP_API_KEY")),
        "tikhub": bool(os.environ.get("TIKHUB_API_TOKEN")),
        "mediacrawler": bool(os.environ.get("MEDIACRAWLER_DIR")),
        "jev": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
    }
    store = get_store()
    stats: dict[str, dict[str, int]] = {}
    for run in store.list_runs(limit=50):
        for span in store.get_trace_spans(run["run_id"]):
            if span.get("component") != "tool":
                continue
            provider = str(_span_attributes(span).get("provider") or "unknown")
            bucket = stats.setdefault(provider, {"calls": 0, "failures": 0})
            bucket["calls"] += 1
            if str(_span_attributes(span).get("status")) != "OK":
                bucket["failures"] += 1
    return {
        "configured": configured,
        "stats": stats,
        "note": "configured 是环境变量状态；stats 是最近 50 个 run 的真实调用统计。",
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
