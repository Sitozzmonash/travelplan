#!/usr/bin/env python
"""生产部署验收脚本：把「Neon 真的可用」「绝不静默回退 SQLite」「Render 部署真的能跑」跑成可复现证据。

为什么要有这个脚本
------------------
部署验收最忌讳"我说连上了"。本脚本只做**可观察**的断言，并且**只打印脱敏结果**
（连接串一律走 ``app.db.mask_dsn``，绝不回显用户名/密码/Token）。

三个子命令
----------
``neon``
    真实连 Neon：``get_store() -> init_schema() -> describe()``，数 ``information_schema`` 里的表，
    再写一条 run 与一个 planning session 并读回来（真回环），最后断言 ``/api/v1/health``
    的部署契约字段 ``database_backend`` / ``database_connected``。

``no-fallback``
    在**子进程里真的起一个 uvicorn**，把 ``DATABASE_URL`` 指向不可达主机，然后：
      - ``/api/v1/health`` 必须 ``status=degraded``、``database_connected=false``、``database_backend=postgres``；
      - ``POST /api/v1/plans`` 必须 5xx 且报错里带 postgres/neon；
      - ``TRAVELPLAN_DB_PATH`` 指向的 SQLite 文件**必须不存在**（静默回退的话它一定被建出来）。
    （对应任务书 §14：DATABASE_URL 已配置却连不上，绝不偷偷退回 SQLite。）

``render <base_url>``
    对已部署的 Render 服务做真实 HTTP 验收：
      - ``/api/v1/health`` 可访问且 ``database_backend=postgres``；
      - ``POST /api/v1/plans {background:true}`` 返回 202，随后**进程内后台任务继续跑**；
      - 轮询 ``/api/v1/plans/{run_id}/status`` 直到 ``SUCCESS`` / ``DEGRADED`` / ``FAILED``。

用法::

    python scripts/verify_deployment.py neon
    python scripts/verify_deployment.py no-fallback
    python scripts/verify_deployment.py render https://travelplan-api.onrender.com
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL_STATUSES = {"SUCCESS", "DEGRADED", "FAILED", "CANCELLED"}


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


def _load_dotenv() -> None:
    """把仓库根 ``.env`` 装载进环境（已存在同名环境变量时不覆盖）。只读不打印。"""

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover —— 只在缺依赖时
        return
    load_dotenv(ROOT / ".env")


def _ok(message: str) -> None:
    print(f"[ok]   {message}")


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")


def _http(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 60.0,
    attempts: int = 2,
) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        request = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode() or "{}"
                return response.status, json.loads(body)
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把 body 读出来
            try:
                body = exc.read().decode() or "{}"
            except Exception:  # noqa: BLE001 —— 读 body 失败就只看状态码
                body = "{}"
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw": body}
        except Exception as exc:  # noqa: BLE001 —— 网络抖动/连接被重置，重试一次
            last = exc
    raise RuntimeError(f"{method} {url} 失败：{last}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, *, seconds: float = 60.0, interval: float = 1.0) -> dict:
    deadline = time.monotonic() + seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, body = _http("GET", url, timeout=15.0)
            if status == 200:
                return body
            last = RuntimeError(f"HTTP {status}")
        except Exception as exc:  # noqa: BLE001 —— 冷启动/未就绪都算"还没起来"
            last = exc
        time.sleep(interval)
    raise RuntimeError(f"{url} 在 {seconds:.0f}s 内未就绪：{last}")


# ----------------------------------------------------------------------
# neon
# ----------------------------------------------------------------------


def cmd_neon(args: argparse.Namespace) -> int:
    _load_dotenv()
    from app.db import close_all_pools, mask_dsn
    from app.store import TravelPlanStore

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        _fail("环境里没有 DATABASE_URL（检查 .env），无法验收 Neon")
        return 1
    _ok(f"DATABASE_URL 已配置，脱敏后：{mask_dsn(dsn)}")

    from app.api import get_store

    store = get_store()
    _ok(f"get_store() -> backend_name={store.backend_name}")
    store.init_schema()
    _ok("init_schema() 成功（幂等建表）")
    described = store.describe()
    print(f"       describe(): {described}")
    if described.get("backend") != "postgres":
        _fail("describe() 报的后端不是 postgres")
        return 1
    if dsn.split("@")[-1].split("/")[0] not in str(described):
        _fail("describe() 里的 host 与 DATABASE_URL 不一致")
        return 1

    with store._connect() as conn:  # noqa: SLF001 —— 验收脚本刻意直连，确认真的落到 Neon
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = current_schema() ORDER BY table_name"
        ).fetchall()
        server = conn.execute("SELECT current_database() AS db, version() AS v").fetchone()
    tables = [row["table_name"] for row in rows]
    _ok(f"information_schema 里能看到 {len(tables)} 张表（Neon 上真的建出来了）")
    print(f"       database={server['db']}  server={server['v'].split(',')[0]}")

    # --- 真实回环：写 run / stage / 指标，再读回来 ---
    run_id = f"verify-{uuid.uuid4().hex[:12]}"
    session_id = f"verify-sess-{uuid.uuid4().hex[:12]}"
    store.create_run(run_id, original_query="verify_deployment: neon round trip")
    store.update_stage(run_id, "verify", "RUNNING", message="round trip")
    store.update_stage(run_id, "verify", "DONE", message="round trip ok", facts={"ok": True})
    store.save_run_metrics(run_id, {"duration_ms": 1234, "llm_calls": 2})
    store.finish_run(run_id, "completed")
    store.save_planning_session(
        {
            "session_id": session_id,
            "status": "CONFIRMED",
            "discovery_status": "READY",
            "basic_intent": {"destination": "杭州", "days": 3},
            "preferences": {"pace": "relaxed"},
            "prefetch": {"verify": True},
        }
    )

    run = store.get_run(run_id)
    progress = store.get_run_progress(run_id)
    metrics = store.get_run_metrics(run_id)
    session = store.get_planning_session(session_id)
    checks = {
        "runs 行写回": bool(run and run["original_query"].startswith("verify_deployment")),
        "run_progress 状态": progress["status"] == "SUCCESS",
        "run_stages 阶段": any(s["stage_id"] == "verify" and s["facts"] == {"ok": True} for s in progress["stages"]),
        "run_metrics 指标": bool(metrics and metrics.get("llm_calls") == 2),
        "planning_sessions 行写回": bool(
            session and session["basic_intent"].get("destination") == "杭州"
        ),
    }
    for name, passed in checks.items():
        (_ok if passed else _fail)(f"回环 {name}：{'通过' if passed else '失败'}")
    if not all(checks.values()):
        return 1
    _ok(f"真实回环 run_id={run_id} session_id={session_id}（Neon 上写入后可读回）")

    # --- /health 契约字段 ---
    from fastapi.testclient import TestClient

    from app.api import api

    with TestClient(api) as client:
        payload = client.get("/api/v1/health").json()
    print(f"       health: {json.dumps(payload, ensure_ascii=False)[:400]}")
    contract = {
        "database_backend=postgres": payload.get("database_backend") == "postgres",
        "database_connected=true": payload.get("database_connected") is True,
        "status=ok": payload.get("status") == "ok",
        "store.backend=postgres": (payload.get("store") or {}).get("backend") == "postgres",
    }
    for name, passed in contract.items():
        (_ok if passed else _fail)(f"health 契约 {name}：{'通过' if passed else '失败'}")

    store.close()
    close_all_pools()
    return 0 if all(contract.values()) else 1


# ----------------------------------------------------------------------
# no-fallback
# ----------------------------------------------------------------------


def cmd_no_fallback(args: argparse.Namespace) -> int:
    """DATABASE_URL 指向不可达主机：必须显式失败，绝不静默退回 SQLite。"""

    _load_dotenv()
    import tempfile

    unreachable = args.database_url or (
        "postgresql://verify:verify@127.0.0.1:1/never_used?connect_timeout=2"
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix="travelplan-nofallback-"))
    sqlite_target = tmp_dir / "should_not_exist.db"
    port = _free_port()

    env = dict(os.environ)
    env["DATABASE_URL"] = unreachable
    env["TRAVELPLAN_DB_PATH"] = str(sqlite_target)
    env["PYTHONPATH"] = str(ROOT)

    print(f"       DATABASE_URL -> 不可达主机（{unreachable.split('@')[-1]}），"
          f"TRAVELPLAN_DB_PATH -> {sqlite_target.name}")
    log_path = tmp_dir / "uvicorn.log"
    log_handle = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api:api", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(ROOT),
        stdout=log_handle,  # 写文件而不是 PIPE：管道写满会反过来卡死服务进程
        stderr=subprocess.STDOUT,
        text=True,
    )
    failures: list[str] = []
    try:
        base = f"http://127.0.0.1:{port}"
        try:
            health = _wait_http(f"{base}/api/v1/health", seconds=90)
        except Exception as exc:  # noqa: BLE001
            _fail(f"服务没起来（连 /health 都打不到）：{exc}")
            return 1
        print(f"       health: {json.dumps(health, ensure_ascii=False)[:500]}")

        expected = {
            "status=degraded": health.get("status") == "degraded",
            "database_backend=postgres（配置目标是 Pg，别假装是 sqlite）": health.get(
                "database_backend"
            )
            == "postgres",
            "database_connected=false": health.get("database_connected") is False,
            "store.ok=false": (health.get("store") or {}).get("ok") is False,
        }
        for name, passed in expected.items():
            (_ok if passed else _fail)(f"健康检查 {name}：{'通过' if passed else '失败'}")
            if not passed:
                failures.append(name)
        error_text = str((health.get("store") or {}).get("error") or "")
        loud = ("postgres" in error_text.lower() or "neon" in error_text.lower()) and bool(
            error_text.strip()
        )
        (_ok if loud else _fail)(f"health 里的错误信息足够明确：{error_text[:160]!r}")
        if not loud:
            failures.append("error_message")

        # 连续多次失败都必须"快速且清晰"：连接池名额没还回去的话，
        # 第 N+1 次会退化成干等 15s 的"连接池已满"超时。
        timings: list[float] = []
        for _ in range(5):
            started = time.monotonic()
            code, body = _http("GET", f"{base}/api/v1/health", timeout=60)
            timings.append(round(time.monotonic() - started, 2))
            if code != 200 or body.get("database_connected") is not False:
                failures.append("repeated_health")
        fast = max(timings) < 10.0
        (_ok if fast else _fail)(f"连续 5 次健康检查都失败得干脆：耗时 {timings}s（无 15s 池满假象）")
        if not fast:
            failures.append("pool_accounting")

        # 业务接口也必须显式失败（而不是偷偷写 SQLite）。
        status, body = _http(
            "POST",
            f"{base}/api/v1/plans",
            {"message": "验证绝不静默回退", "background": True},
            timeout=90,
            attempts=1,
        )
        detail = json.dumps(body, ensure_ascii=False)
        (_ok if status >= 500 else _fail)(
            f"POST /api/v1/plans 返回 {status}（期望 5xx）：{detail[:200]}"
        )
        if status < 500:
            failures.append("post_plans_status")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()
        log_handle.close()

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if "PostgresUnavailable" in log_text and "postgres" in log_text.lower():
        line = next(
            (ln for ln in log_text.splitlines() if "PostgresUnavailable" in ln), ""
        )
        _ok(f"服务端日志明确报出：{line.strip()[:160]}")
    else:
        _fail("服务端日志里没有 PostgresUnavailable（故障可能被吞掉了）")
        failures.append("server_log")

    if sqlite_target.exists():
        _fail(f"静默回退发生了：SQLite 文件被建出来 -> {sqlite_target}")
        failures.append("sqlite_fallback")
    else:
        _ok(f"未见静默回退：{sqlite_target.name} 不存在")
    return 1 if failures else 0


# ----------------------------------------------------------------------
# render
# ----------------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    failures: list[str] = []

    health = _wait_http(f"{base}/api/v1/health", seconds=args.wait_health)
    print(f"       health: {json.dumps(health, ensure_ascii=False)[:600]}")
    store = health.get("store") or {}
    # 契约字段（database_backend / database_connected）是 docs/operations/DEPLOYMENT.md §2.4 的要求。已部署的旧版本
    # 可能还没有这两个字段，此时退回 store.backend / store.ok 判定并明确提示 —— 不假装通过。
    if "database_backend" in health:
        backend_ok = health.get("database_backend") == "postgres"
        backend_note = "database_backend=postgres"
    else:
        backend_ok = store.get("backend") == "postgres"
        backend_note = "database_backend 字段缺失（部署版本早于该字段），改用 store.backend=postgres 判定"
    if "database_connected" in health:
        connected_ok = health.get("database_connected") is True
        connected_note = "database_connected=true"
    else:
        connected_ok = store.get("ok") is True
        connected_note = "database_connected 字段缺失（部署版本早于该字段），改用 store.ok=true 判定"
    expected = {
        "status=ok": health.get("status") == "ok",
        backend_note: backend_ok,
        connected_note: connected_ok,
    }
    for name, passed in expected.items():
        (_ok if passed else _fail)(f"部署健康检查 {name}：{'通过' if passed else '失败'}")
        if not passed:
            failures.append(f"health:{name}")

    status, created = _http(
        "POST",
        f"{base}/api/v1/plans",
        {"message": args.message, "background": True},
        timeout=60,
    )
    (_ok if status == 202 else _fail)(f"POST /api/v1/plans -> {status}（期望 202）")
    if status != 202:
        return 1
    run_id = created.get("run_id")
    print(f"       run_id={run_id} 已接受；开始轮询（这证明 HTTP 已返回而任务仍在跑）")

    deadline = time.monotonic() + args.timeout
    seen_stages: list[str] = []
    last: dict = {}
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        code, last = _http("GET", f"{base}/api/v1/plans/{run_id}/status", timeout=30)
        if code != 200:
            print(f"       轮询 HTTP {code}: {json.dumps(last, ensure_ascii=False)[:200]}")
            continue
        stage = last.get("current_stage") or "-"
        if stage not in seen_stages:
            seen_stages.append(stage)
        print(f"       t+{args.timeout - int(deadline - time.monotonic())}s "
              f"status={last.get('status')} stage={stage} msg={(last.get('message') or '')[:60]}")
        if last.get("status") in TERMINAL_STATUSES:
            break

    final = last.get("status")
    (_ok if final in TERMINAL_STATUSES else _fail)(
        f"轮询到终态：{final}（经过阶段：{seen_stages}）"
    )
    if final not in TERMINAL_STATUSES:
        failures.append("no_terminal_status")

    status, detail = _http("GET", f"{base}/api/v1/plans/{run_id}", timeout=30)
    print(f"       GET /api/v1/plans/{run_id} -> {status}："
          f"{json.dumps(detail, ensure_ascii=False)[:300]}")

    # 产物接口能下载 ⇒ 容器里的 TRAVELPLAN_OUTPUT_DIR 真的可写（Render 免费层没有持久盘）。
    for filename in ("plan.json", "audit_report.json"):
        code, _ = _http(
            "GET", f"{base}/api/v1/plans/{run_id}/artifacts/{filename}", timeout=60
        )
        (_ok if code == 200 else _fail)(f"产物 {filename} 可下载（HTTP {code}）")
        if code != 200:
            failures.append(f"artifact:{filename}")
    return 1 if failures else 0


# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TravelPlan 部署验收")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("neon", help="真实连 Neon：建表 + 读写回环 + health 契约")

    no_fallback = sub.add_parser("no-fallback", help="DATABASE_URL 不可达时必须显式失败")
    no_fallback.add_argument("--database-url", default=None, help="自定义不可达连接串（默认 127.0.0.1:1）")

    render = sub.add_parser("render", help="对已部署服务做 202 + 轮询验收")
    render.add_argument("base_url", help="服务根 URL，例如 https://travelplan-api.onrender.com")
    render.add_argument(
        "--message",
        default="帮我规划一个杭州 2 天轻松行程，只要一个大概框架即可，预算中等。",
        help="用于验收的规划请求（选便宜、短的行程）",
    )
    render.add_argument("--timeout", type=int, default=900, help="轮询上限秒数")
    render.add_argument("--interval", type=float, default=10.0, help="轮询间隔秒数")
    render.add_argument("--wait-health", type=int, default=300, help="等待冷启动的秒数")

    args = parser.parse_args(argv)
    if args.command == "neon":
        return cmd_neon(args)
    if args.command == "no-fallback":
        return cmd_no_fallback(args)
    return cmd_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
