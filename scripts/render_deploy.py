#!/usr/bin/env python
"""用 Render REST API 建/配置/部署 TravelPlan 后端服务（凭证只在内存里，绝不打印）。

为什么不用控制台点
------------------
部署要可复现：这个脚本把「服务怎么建的、环境变量有哪几个 key、部署怎么触发的」写成代码，
下一次重建（或换账号）照着跑一遍就行。凭证取自 ``~/.render/cli.yaml`` 的 ``api.key``
（Render CLI 的登录态），或环境变量 ``RENDER_API_KEY``；两者都**只读取不打印**。

子命令
------
``info``    列出当前 workspace 的服务（只看，不碰别人的服务）
``create``  创建 Docker 类型的 web service（已存在则跳过）
``env``     把 ``.env`` 里的 Provider Key / DATABASE_URL 推到服务上（值不回显）
``deploy``  触发一次部署并等待到 live/failed
``job``     在服务镜像里跑一条一次性命令（用来验证容器内的 node/npm）

用法::

    python scripts/render_deploy.py info
    python scripts/render_deploy.py create --name travelplan-api --region ohio --plan free
    python scripts/render_deploy.py env --service travelplan-api
    python scripts/render_deploy.py deploy --service travelplan-api
    python scripts/render_deploy.py job --service travelplan-api --command "node --version"
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_BASE = os.environ.get("RENDER_API_BASE", "https://api.render.com/v1")

#: 推到 Render 上的 Secret 环境变量（值来自 .env，键名才是可以打印的东西）。
REQUIRED_ENV_KEYS = (
    "MODEL_NAME",
    "MODEL_BASE_URL",
    "MODEL_API_KEY",
    "TAVILY_API_KEY",
    "AMAP_API_KEY",
    "TIKHUB_API_TOKEN",
    "TUNIU_API_KEY",
    "JEV_API_KEY",
    "DATABASE_URL",
    # 前端域名白名单。缺了它浏览器会先被 CORS 挡掉，表现成"接口不通"（Failed to fetch），
    # 而 curl 直连后端一切正常 —— 这是最容易误判成"后端挂了"的一种故障。
    "TRAVELPLAN_CORS_ORIGINS",
)

#: 非 Secret 的部署参数（与 Dockerfile ENV 一致；Render 免费层没有持久盘，
#: 这两个路径只是容器内的临时目录，业务数据在 Neon）。
PLAIN_ENV = {
    "TRAVELPLAN_OUTPUT_DIR": "/data/outputs",
    "TRAVELPLAN_DB_PATH": "/data/travelplan.db",
}

ADMIN_TOKEN_KEY = "TRAVELPLAN_ADMIN_TOKEN"


# ----------------------------------------------------------------------
# 凭证与 HTTP
# ----------------------------------------------------------------------


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(ROOT / ".env")


def api_key() -> str:
    """从 Render CLI 的登录态或环境变量取 API Key（只读，绝不打印）。"""

    key = os.environ.get("RENDER_API_KEY", "").strip()
    if key:
        return key
    try:
        import yaml  # PyYAML：Render CLI 配置文件是 YAML
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("需要 PyYAML 才能读 ~/.render/cli.yaml（或设置 RENDER_API_KEY）") from exc
    path = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~")) / ".render" / "cli.yaml"
    if not path.exists():
        raise SystemExit(f"找不到 {path}，请先 `render login` 或设置 RENDER_API_KEY")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    key = str(((data.get("api") or {}).get("key") or "")).strip()
    if not key:
        raise SystemExit("cli.yaml 里没有 api.key，请先 `render login`")
    return key


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key()}",  # 只放进请求头，不进日志
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode() or "null"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"{method} {path} 失败：{exc}") from exc


def list_services() -> list[dict]:
    status, data = _request("GET", "/services?limit=100")
    if status != 200:
        raise SystemExit(f"列服务失败 HTTP {status}: {data}")
    return [item["service"] for item in (data or []) if item.get("service")]


def find_service(name: str) -> dict | None:
    for service in list_services():
        if service.get("name") == name:
            return service
    return None


def workspace_id() -> str:
    status, data = _request("GET", "/owners?limit=20")
    if status != 200 or not data:
        raise SystemExit(f"取 workspace 失败 HTTP {status}: {data}")
    return str(data[0]["owner"]["id"])


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    print(f"workspace: {workspace_id()}")
    for service in sorted(list_services(), key=lambda s: s.get("name") or ""):
        details = service.get("serviceDetails") or {}
        print(
            f"  - {service['id']}  {service.get('name')}  type={service.get('type')}"
            f"  runtime={details.get('runtime')}  plan={details.get('plan')}"
            f"  region={details.get('region')}  url={details.get('url')}"
        )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    existing = find_service(args.name)
    if existing:
        print(f"服务已存在：{existing['id']} {existing.get('name')}")
        return 0
    payload = {
        "type": "web_service",
        "name": args.name,
        "ownerId": workspace_id(),
        "repo": args.repo,
        "branch": args.branch,
        "autoDeploy": args.auto_deploy,
        "serviceDetails": {
            "env": "docker",
            "plan": args.plan,
            "region": args.region,
            "healthCheckPath": args.health_check_path,
            "envSpecificDetails": {
                "dockerfilePath": args.dockerfile,
                "dockerContext": args.context,
            },
        },
    }
    status, data = _request("POST", "/services", payload)
    if status not in (200, 201):
        raise SystemExit(f"创建服务失败 HTTP {status}: {json.dumps(data)[:500]}")
    service = data.get("service", data)
    print(f"已创建：{service['id']} {service.get('name')} region={args.region} plan={args.plan}")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    _load_dotenv()
    service = find_service(args.service)
    if not service:
        raise SystemExit(f"找不到服务 {args.service}")

    values: dict[str, str] = {}
    missing: list[str] = []
    for key in REQUIRED_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            values[key] = value
        else:
            missing.append(key)
    values.update(PLAIN_ENV)

    token = os.environ.get(ADMIN_TOKEN_KEY, "").strip()
    if not token:
        token = secrets.token_urlsafe(32)
        env_path = ROOT / ".env"
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write("\n# 管理端 Bearer Token（Render 上同名 Secret；本文件已被 .gitignore）\n")
            handle.write(f"{ADMIN_TOKEN_KEY}={token}\n")
        print(f"已生成 {ADMIN_TOKEN_KEY} 并写入 .env（值不打印），同时推到 Render")
    values[ADMIN_TOKEN_KEY] = token

    # PUT 是全量替换：把现有变量取回来合并，避免手滑删掉别人加的东西。
    status, current = _request("GET", f"/services/{service['id']}/env-vars?limit=100")
    merged: dict[str, str] = {}
    if status == 200 and isinstance(current, list):
        for item in current:
            env_var = item.get("envVar") or {}
            if env_var.get("key"):
                merged[str(env_var["key"])] = str(env_var.get("value") or "")
    merged.update(values)

    payload = [{"key": k, "value": v} for k, v in sorted(merged.items())]
    status, data = _request("PUT", f"/services/{service['id']}/env-vars", payload)
    if status not in (200, 201):
        raise SystemExit(f"写环境变量失败 HTTP {status}: {json.dumps(data)[:500]}")
    print(f"已写入 {len(payload)} 个环境变量（只列 key）：{', '.join(sorted(merged))}")
    if missing:
        print(f"[warn] .env 里缺这些 key，未推送：{', '.join(missing)}")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    service = find_service(args.service)
    if not service:
        raise SystemExit(f"找不到服务 {args.service}")
    status, data = _request(
        "POST", f"/services/{service['id']}/deploys", {"clearCache": "do_not_clear"}
    )
    if status not in (200, 201, 202):
        raise SystemExit(f"触发部署失败 HTTP {status}: {json.dumps(data)[:500]}")
    deploy_id = ((data or {}).get("deploy") or (data or {})).get("id") if data else None
    if not deploy_id:
        # 有的响应体是空的（202 + no content）：那就问一次最新部署是谁。
        code, latest = _request("GET", f"/services/{service['id']}/deploys?limit=1")
        if code == 200 and latest:
            deploy_id = (latest[0].get("deploy") or {}).get("id")
    if not deploy_id:
        raise SystemExit("触发了部署但拿不到 deploy id，去控制台看一眼")
    print(f"已触发部署 {deploy_id}；开始等待（构建 Docker 镜像通常要几分钟）")
    deadline = time.monotonic() + args.timeout
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        code, detail = _request("GET", f"/services/{service['id']}/deploys/{deploy_id}")
        if code != 200:
            continue
        current = (detail.get("deploy") or detail).get("status")
        if current != last_status:
            print(f"  deploy status = {current}")
            last_status = current
        if current in {"live", "build_failed", "update_failed", "canceled", "pre_deploy_failed", "deactivated"}:
            break
    if last_status != "live":
        raise SystemExit(f"部署未成功：{last_status}")
    url = (find_service(args.service).get("serviceDetails") or {}).get("url")
    print(f"部署成功，服务地址：{url}")
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    """在**服务镜像**里跑一条一次性命令（用来证明容器内真的有 node/npm）。"""

    service = find_service(args.service)
    if not service:
        raise SystemExit(f"找不到服务 {args.service}")
    status, data = _request(
        "POST", f"/services/{service['id']}/jobs", {"startCommand": args.command}
    )
    if status not in (200, 201):
        raise SystemExit(
            f"创建一次性任务失败 HTTP {status}: {json.dumps(data)[:300]}"
            "（免费层可能不允许；见 docs/09_部署说明.md 的替代验证方式）"
        )
    job = data.get("job") or data
    print(f"已创建一次性任务 {job.get('id')}：{args.command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render 部署助手（不打印任何 Secret）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="列出当前 workspace 的服务")

    create = sub.add_parser("create", help="创建 Docker web service")
    create.add_argument("--name", default="travelplan-api")
    create.add_argument("--repo", default="https://github.com/Sitozzmonash/travelplan")
    create.add_argument("--branch", default="main")
    create.add_argument("--region", default="ohio", help="与 Neon us-east-2 同区")
    create.add_argument("--plan", default="free")
    create.add_argument("--dockerfile", default="./Dockerfile")
    create.add_argument("--context", default=".")
    create.add_argument("--health-check-path", default="/api/v1/health")
    create.add_argument("--auto-deploy", default="yes", choices=["yes", "no"])

    env = sub.add_parser("env", help="推送环境变量（值来自 .env，不回显）")
    env.add_argument("--service", default="travelplan-api")

    deploy = sub.add_parser("deploy", help="触发部署并等待")
    deploy.add_argument("--service", default="travelplan-api")
    deploy.add_argument("--timeout", type=int, default=1800)
    deploy.add_argument("--interval", type=float, default=15.0)

    job = sub.add_parser("job", help="在服务镜像里跑一次性命令")
    job.add_argument("--service", default="travelplan-api")
    job.add_argument("--command", default="node --version && npm --version")

    args = parser.parse_args(argv)
    return {
        "info": cmd_info,
        "create": cmd_create,
        "env": cmd_env,
        "deploy": cmd_deploy,
        "job": cmd_job,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
