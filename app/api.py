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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import PROJECT_ID, Route, create_travel_app, run_travel
from .models import TripPlan
from .store import DEFAULT_DB_PATH, TravelPlanStore
from .workflow import DEFAULT_OUTPUT_DIR, WORKFLOW_NAME, plan_payload

#: 只允许下载这三件产物。白名单而不是拼接路径，避免 `../` 穿越。
ARTIFACTS: dict[str, str] = {
    "plan.json": "application/json",
    "plan.md": "text/markdown; charset=utf-8",
    "audit_report.json": "application/json",
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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ======================================================================
# 请求 / 响应模型
# ======================================================================


class CreatePlanRequest(BaseModel):
    message: str = Field(min_length=1, description="用户自然语言需求")


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


def get_runner() -> Any:
    """进程内复用一份 HybridRunner（装配要读 .env、建 Router，不该每请求一遍）。"""
    global _APP
    if _APP is None:
        _APP = create_travel_app(output_dir=_output_dir())
    return _APP


def _output_dir() -> Path:
    return Path(os.environ.get("TRAVELPLAN_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))


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
        "store": {"ok": store_ok, "path": str(DEFAULT_DB_PATH), "error": store_error},
        "providers_configured": configured,
        "notes": [
            "此接口只报告环境变量是否已配置，不返回值。",
            "tuniu / 12306 MCP 由各自进程提供，是否可用请看具体 run 的 audit_report.json。",
        ],
    }


@api.post("/api/v1/plans", response_model=CreatePlanResponse)
def create_plan(request: CreatePlanRequest):
    """跑一次完整规划。长任务，同步返回（PRD §30：V1 不强制异步队列）。"""
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
