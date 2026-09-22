"""app/agent.py 装配层与 app/api.py HTTP 层的测试（PRD §30 / docs/operations/DEVELOPMENT.md §7）。

两点要守住：

1. **装配边界**：TravelPlan 不另起 Runtime。这里验证的是「我们把什么交给了
   SuperHarness」，而不是重测 SuperHarness 自己的路由/重试。
2. **HTTP 只做翻译**：接口不许自己算价格、不许编行程，也不许把 Provider Key 回显出去。

全部离线：Runner 换成桩，Provider 与 LLM 换成 tests/fakes.py 的假件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent import (
    PROJECT_ID,
    Route,
    TravelWorkflowRunnable,
    extract_query,
    run_travel,
    travel_workflow_spec,
)
from app.models import TripPlan
from app.store import TravelPlanStore
from app.workflow import DEFAULT_OUTPUT_DIR, WORKFLOW_DESCRIPTION, WORKFLOW_NAME, RunResult

from superharness import HarnessContext
from tests.agent_fixture import run_agent
from tests.fakes import QUERY, FakeHub


# ======================================================================
# extract_query：HybridRunner 透传下来的 input 有三种形状
# ======================================================================


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("从北京去成都", "从北京去成都"),
        ("  从北京去成都  ", "从北京去成都"),
        ({"messages": [{"role": "user", "content": "从北京去成都"}]}, "从北京去成都"),
        ({"query": "从北京去成都"}, "从北京去成都"),
        ([{"role": "user", "content": "从北京去成都"}], "从北京去成都"),
        ([{"role": "system", "content": "你是助手"}, {"role": "user", "content": "从北京去成都"}], "从北京去成都"),
        # 多条 user 消息取最后一条：本轮的问题才是要去处理的。
        ([{"role": "user", "content": "先问我"}, {"role": "user", "content": "从北京去成都"}], "从北京去成都"),
    ],
)
def test_extract_query_accepts_known_shapes(payload: Any, expected: str) -> None:
    assert extract_query(payload) == expected


@pytest.mark.parametrize("payload", [{}, [], {"messages": []}, None, 42, "", "   "])
def test_extract_query_refuses_to_guess(payload: Any) -> None:
    """取不到就报错，不返回默认值 —— 猜错会变成「用户问 A，系统答 B」。

    空输入也算取不到：返回空串等于把「这次没拿到问题」当成「用户问了空问题」。
    """
    with pytest.raises(ValueError):
        extract_query(payload)


def test_extract_query_reads_human_typed_message_object() -> None:
    """LangGraph 会把消息实例化成对象（HumanMessage），也要认。"""

    class _Human:
        type = "human"
        content = "从北京去成都"

    assert extract_query([_Human()]) == "从北京去成都"


# ======================================================================
# 装配：交给 SuperHarness 的东西对不对
# ======================================================================


def test_workflow_spec_registers_the_fixed_flow() -> None:
    spec = travel_workflow_spec()
    assert spec.name == WORKFLOW_NAME
    assert spec.description == WORKFLOW_DESCRIPTION
    assert isinstance(spec.runnable, TravelWorkflowRunnable)
    # description 会被 BM25 打分，太短会让自动路由选不出来。
    assert len(spec.description) >= 30


def test_route_constants_point_at_the_registered_names() -> None:
    assert Route.WORKFLOW == WORKFLOW_NAME
    assert Route.AGENT == "agent"
    assert PROJECT_ID == "travelplan"


def test_create_travel_app_hands_the_workflow_to_superharness(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `create_harness_app` 拦住：验证我们传了 workflow / MCP / system_prompt。

    这里刻意不真的装配 —— 那要读 .env、建 Router、起 Checkpointer，属于
    SuperHarness 自己的测试范围。这里只确认边界参数。
    """
    captured: dict[str, Any] = {}

    def fake_create_harness_app(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "STUBBED-RUNNER"

    # 全局 conftest 为了不让测试拉起 `npx 12306-mcp` 子进程，默认关掉了 MCP 注册；
    # 这个用例验证的正是"MCP 清单被原样传给 SuperHarness"，所以在本用例内打开。
    monkeypatch.delenv("TRAVELPLAN_DISABLE_MCP", raising=False)
    monkeypatch.setattr("app.agent.create_harness_app", fake_create_harness_app)

    from app.agent import create_travel_app

    result = create_travel_app(output_dir="out", checkpointer=None)
    assert result == "STUBBED-RUNNER"

    assert len(captured["workflows"]) == 1
    assert captured["workflows"][0].name == WORKFLOW_NAME
    assert captured["name"] == "travelplan"
    assert captured["system_prompt"].strip()
    # 12306 走 MCP：只注册 spec，不在装配期拉起 npx 进程。
    assert [server.name for server in captured["mcp_servers"]] == ["railway_12306"]


def test_create_travel_app_binds_superharness_emit_to_the_runnable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider / LLM 的日志要报到 SuperHarness 原生 Observability（不要重造）。

    装配顺序是「先造 runnable → 再拿 runner」，所以 emit 得靠一个可变占位对象回填：
    runnable 拿到的是占位对象，装配完成后它才转发到 runner 真正的 emit。
    """
    events: list[tuple[str, str, dict]] = []

    class StubRunner:
        def emit(self, event_type, component, *, data=None, status=None):
            events.append((event_type, component, data or {}))

    captured: dict[str, Any] = {}

    def fake_create_harness_app(**kwargs: Any) -> StubRunner:
        captured.update(kwargs)
        return StubRunner()

    monkeypatch.setattr("app.agent.create_harness_app", fake_create_harness_app)

    from app.agent import create_travel_app

    runner = create_travel_app(output_dir="out", checkpointer=None)
    assert runner.emit

    emit = captured["workflows"][0].runnable.emit
    assert emit is not None, "runnable 必须拿到上报口，否则 Provider/LLM 日志全丢"

    emit("tool.finished", "provider", data={"name": "amap/search_poi"}, status="OK")

    assert events == [("tool.finished", "provider", {"name": "amap/search_poi"})]


# ======================================================================
# run_travel：唯一业务入口
# ======================================================================


class _StubRunner:
    """假 HybridRunner：只实现 invoke，并记录调用参数。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.seen: dict[str, Any] = {}

    def invoke(self, input, config=None, *, context=None, route=None):
        self.seen = {"input": input, "config": config, "context": context, "route": route}
        return self.result


def _stub_result(run_id: str = "stub-run") -> RunResult:
    return RunResult(run_id=run_id, status="completed")


def test_run_travel_passes_route_context_and_thread() -> None:
    runner = _StubRunner(_stub_result())
    result = run_travel(
        "从北京去成都",
        user_id="u1",
        thread_id="t1",
        app=runner,
        route=Route.WORKFLOW,
    )
    assert result.run_id == "stub-run"
    assert runner.seen["route"] == WORKFLOW_NAME
    # Agent 与 workflow 共用一套 input 形状：LangGraph messages。
    assert runner.seen["input"] == {"messages": [{"role": "user", "content": "从北京去成都"}]}
    assert runner.seen["config"] == {"configurable": {"thread_id": "t1"}}
    assert runner.seen["context"].user_id == "u1"
    assert runner.seen["context"].project_id == PROJECT_ID


def test_run_travel_without_thread_id_passes_no_config() -> None:
    runner = _StubRunner(_stub_result())
    run_travel("从北京去成都", app=runner)
    assert runner.seen["config"] is None


def test_run_travel_raises_when_route_mismatches_the_return_shape() -> None:
    """route 说走固定流程，却拿回 Agent Loop 的 dict：必须报错而不是静默返回。"""
    runner = _StubRunner({"messages": ["agent 的回答"]})
    with pytest.raises(TypeError, match="RunResult"):
        run_travel("从北京去成都", route=Route.WORKFLOW, app=runner)


def test_workflow_runnable_shims_input_to_the_main_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runnable 只做形状适配，真正的规划交给 Agent Loop 入口（execute_agent_run）。"""
    seen: dict[str, Any] = {}

    def fake_execute(query: str, **kwargs: Any) -> RunResult:
        seen.update({"query": query, **kwargs})
        return _stub_result()

    monkeypatch.setattr("app.agent.execute_agent_run", fake_execute)

    runnable = TravelWorkflowRunnable(output_dir=DEFAULT_OUTPUT_DIR)
    result = runnable.invoke(
        {"messages": [{"role": "user", "content": "从北京去成都"}]},
        config={"configurable": {"thread_id": "t9"}},
        context=HarnessContext(user_id="u9", project_id=PROJECT_ID),
    )

    assert result.run_id == "stub-run"
    assert seen["query"] == "从北京去成都"
    assert seen["user_id"] == "u9"
    assert seen["thread_id"] == "t9"
    # 结果留在手里，便于调试时回看（HybridRunner 只把 RunResult 透传出去）。
    assert runnable._results["stub-run"] is result


# ======================================================================
# HTTP 层
# ======================================================================

# FastAPI 是 app/api.py 的硬依赖，不做 importorskip —— 缺依赖时应当直接报错，
# 而不是把整组接口测试静默跳过。
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """跑一次离线规划，产出一份真实的库 + 三件产物，供所有读接口复用。

    走的是主路径 `execute_agent_run`（Agent Loop 自己调工具出 plan），脚本模型 + 假 Hub
    保证离线。module 作用域：读接口之间共用同一份 fixture 才不会重复跑。
    """
    base: Path = tmp_path_factory.mktemp("api-fixture")
    output_dir = base / "outputs"
    store = TravelPlanStore(db_path=base / "travelplan.db")
    run_id = "api-fixture-run"
    result = run_agent(
        store=store,
        run_id=run_id,
        output_dir=output_dir,
        hub=FakeHub(store=store, run_id=run_id, poi_spread=0.25),
        user_id="api-test",
        tool_calls=(
            ("search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}),
        ),
    )
    assert result.status == "completed", result.error
    return {"store": store, "output_dir": output_dir, "result": result}


@pytest.fixture()
def client(finished_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 api 模块里直接调用的 get_store / _output_dir 换成 fixture 的临时库。

    注意：路由函数里是直接 `get_store()`（不是 `Depends`），所以
    `dependency_overrides` 拦不住，必须 monkeypatch 模块属性。
    """
    from app import api as api_module

    monkeypatch.setattr(api_module, "get_store", lambda: finished_run["store"])
    monkeypatch.setattr(api_module, "_output_dir", lambda: finished_run["output_dir"])
    return TestClient(api_module.api)


def test_health_reports_configuration_without_leaking_secrets(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "super-secret-value")
    monkeypatch.setenv("AMAP_API_KEY", "amap-secret-value")
    monkeypatch.delenv("TIKHUB_API_TOKEN", raising=False)

    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "ok"
    assert body["workflow"] == WORKFLOW_NAME
    assert body["project_id"] == PROJECT_ID
    assert body["store"]["ok"] is True
    assert body["providers_configured"]["MODEL_API_KEY"] is True
    assert body["providers_configured"]["TIKHUB_API_TOKEN"] is False

    # 只报「配了没配」，任何 Secret 的值都不许出现在响应里。
    raw = response.text
    assert "super-secret-value" not in raw
    assert "amap-secret-value" not in raw


def test_create_plan_returns_the_plan(
    client: Any, finished_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """长任务同步返回（PRD §30）：200 + plan_payload。

    这里直接复用 fixture 里那次真实离线 run 的 plan —— 造第二份手工数据只会让
    断言的其实是手工数据本身。
    """
    from app import api as api_module

    stored = finished_run["store"].get_plan("api-fixture-run")
    plan = TripPlan.model_validate(stored["plan"])
    monkeypatch.setattr(
        api_module,
        "run_travel",
        lambda *args, **kwargs: RunResult(run_id="api-fixture-run", status="completed", plan=plan),
    )

    response = client.post("/api/v1/plans", json={"message": QUERY})
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "api-fixture-run"
    assert body["status"] == "completed"
    assert body["plan"]["days"]
    # Agent 路径不写证据库（攻略由 Agent 在循环里查、只把结论写进 plan 的 sources），
    # 所以 evidence 明细如实为空 —— 这条断言要的是"字段在且形状对"，不是"必须有内容"。
    assert body["plan"]["evidence"] == []
    assert body["plan"]["sources"]


def test_create_plan_maps_needs_clarification_to_422(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import api as api_module

    monkeypatch.setattr(
        api_module,
        "run_travel",
        lambda *args, **kwargs: RunResult(
            run_id="r-clarify",
            status="needs_clarification",
            error="没能确定目的地城市，无法开始查询。",
        ),
    )
    response = client.post("/api/v1/plans", json={"message": "帮我规划一下"})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert body["plan"] is None
    assert "目的地" in body["message"]


def test_create_plan_maps_failure_to_500(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import api as api_module

    monkeypatch.setattr(
        api_module,
        "run_travel",
        lambda *args, **kwargs: RunResult(run_id="r-fail", status="failed", error="RuntimeError: 数据源全挂"),
    )
    response = client.post("/api/v1/plans", json={"message": QUERY})
    assert response.status_code == 500
    assert response.json()["plan"] is None


def test_create_plan_rejects_empty_message(client: Any) -> None:
    assert client.post("/api/v1/plans", json={"message": ""}).status_code == 422


def test_get_plan_returns_stored_plan_and_markdown(client: Any) -> None:
    response = client.get("/api/v1/plans/api-fixture-run")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "api-fixture-run"
    assert body["days"]
    # 前端详情页要直接渲染 markdown。
    assert body["plan_md"].strip()


def test_run_status_exposes_real_agent_step_progress(client: Any) -> None:
    """轮询状态来自 Agent 每一步的真实开始/结束事件，不是前端计时器模拟。

    stage_id 是**中文展示名**（`app/agent_trace` 的口径）：工具调用按工具显示名、
    模型调用统一是「在思考行程」。这里用那两份常量拼期望值，避免把中文抄死在测试里。
    """
    from app.agent_trace import MODEL_STEP_LABEL, display_name_for

    response = client.get("/api/v1/plans/api-fixture-run/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"SUCCESS", "DEGRADED"}
    stages = {stage["stage_id"]: stage for stage in body["stages"]}
    # 模型思考 + 真实工具调用 + 交卷，三者都要留下步骤行。
    assert MODEL_STEP_LABEL in stages
    assert display_name_for("search_trains") in stages
    assert display_name_for("submit_final_plan") in stages
    assert stages[MODEL_STEP_LABEL]["status"] in {"SUCCESS", "WARNING"}
    assert stages[display_name_for("submit_final_plan")]["finished_at"]


def test_background_create_returns_run_id_without_faking_completion(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台模式应返回 RUNNING，真实完成由 status endpoint 后续观察。"""
    from app import api as api_module

    submitted: dict[str, Any] = {}

    class StubExecutor:
        def submit(self, fn: Any, *args: Any) -> None:
            submitted["fn"] = fn
            submitted["args"] = args

    monkeypatch.setattr(api_module, "_RUN_EXECUTOR", StubExecutor())
    monkeypatch.setattr(api_module, "new_run_id", lambda: "background-run")

    response = client.post("/api/v1/plans", json={"message": QUERY, "background": True})
    assert response.status_code == 202
    assert response.json()["run_id"] == "background-run"
    assert response.json()["status"] == "RUNNING"
    assert submitted["args"] == ("background-run", QUERY)

    progress = client.get("/api/v1/plans/background-run/status")
    assert progress.status_code == 200
    assert progress.json()["status"] == "RUNNING"


def test_admin_api_requires_token_and_does_not_expose_secrets(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRAVELPLAN_ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("AMAP_API_KEY", "never-return-this")

    assert client.get("/api/v1/admin/overview").status_code == 401
    response = client.get(
        "/api/v1/admin/config", headers={"Authorization": "Bearer admin-test-token"}
    )
    assert response.status_code == 200
    assert response.json()["secret_configured"]["amap"] is True
    # Jev 决策层已删：它不再出现在密钥探针里。
    assert "jev" not in response.json()["secret_configured"]
    assert "never-return-this" not in response.text


def test_get_plan_404_for_unknown_run(client: Any) -> None:
    assert client.get("/api/v1/plans/tp-does-not-exist").status_code == 404


def test_get_audit_returns_the_real_audit_report(client: Any) -> None:
    response = client.get("/api/v1/plans/api-fixture-run/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "api-fixture-run"
    assert body["provider_calls"]
    assert body["llm_calls"]
    # 审计要能回答"搜了什么、返回了什么"。
    assert any(call["tool"] for call in body["provider_calls"])


def test_get_audit_404_for_unknown_run(client: Any) -> None:
    assert client.get("/api/v1/plans/nope/audit").status_code == 404


def test_get_audit_404_when_artifact_missing(
    client: Any, finished_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里有计划但磁盘上没有 audit_report.json：如实 404，不返回空壳。"""
    from app import api as api_module

    monkeypatch.setattr(api_module, "_output_dir", lambda: finished_run["output_dir"] / "nonexistent")
    assert client.get("/api/v1/plans/api-fixture-run/audit").status_code == 404


def test_artifact_download_serves_whitelisted_files(client: Any) -> None:
    for filename in ("plan.json", "plan.md", "audit_report.json"):
        response = client.get(f"/api/v1/plans/api-fixture-run/artifacts/{filename}")
        assert response.status_code == 200, filename
        assert response.content


def test_artifact_download_refuses_anything_not_whitelisted(client: Any) -> None:
    """白名单而不是拼路径：`../` 这类穿越必须拿不到东西。"""
    for filename in ("secrets.txt", "..%2F..%2F.env", "travelplan.db"):
        response = client.get(f"/api/v1/plans/api-fixture-run/artifacts/{filename}")
        assert response.status_code == 404, filename


def test_artifact_download_404_for_unknown_run(client: Any) -> None:
    assert client.get("/api/v1/plans/nope/artifacts/plan.json").status_code == 404


@pytest.mark.parametrize("action", ["replace", "remove", "lock", "relax_day", "lower_budget"])
def test_revise_accepts_the_documented_actions(client: Any, action: str) -> None:
    payload: dict[str, Any] = {"action": action}
    if action in {"replace", "remove"}:
        payload["item_id"] = "item-1"
    if action == "relax_day":
        payload["day_index"] = 0

    response = client.post("/api/v1/plans/api-fixture-run/revise", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    # 不做假装修好了：必须明确说页面不会变化。
    assert "不会变化" in body["message"]


def test_revise_rejects_unknown_action(client: Any) -> None:
    response = client.post("/api/v1/plans/api-fixture-run/revise", json={"action": "delete_everything"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "replace"},
        {"action": "remove"},
        {"action": "relax_day"},
    ],
)
def test_revise_requires_the_fields_each_action_needs(client: Any, payload: dict[str, Any]) -> None:
    assert client.post("/api/v1/plans/api-fixture-run/revise", json=payload).status_code == 422


def test_revise_404_for_unknown_run(client: Any) -> None:
    response = client.post("/api/v1/plans/nope/revise", json={"action": "lock", "item_id": "i1"})
    assert response.status_code == 404


def test_audit_report_json_is_serialisable(finished_run: dict[str, Any]) -> None:
    """产物必须是纯 JSON：前端会直接 parse，落盘也不该带 NaN/自定义类型。"""
    path = finished_run["output_dir"] / "api-fixture-run" / "audit_report.json"
    json.loads(path.read_text(encoding="utf-8"))
