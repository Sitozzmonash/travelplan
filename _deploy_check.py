#!/usr/bin/env python
"""一次性部署体检脚本（只读线上，不改任何东西）。

检查三处：
  1) Render 后端  https://travelplan-api.onrender.com
  2) Vercel 前端  https://travelplan-web.vercel.app          （国外）
  3) EdgeOne 前端 https://travelplan-web.edgeone.cool（国内）

前端最关键的一项：编译期变量 NEXT_PUBLIC_API_BASE_URL 是否真的被内联进
浏览器包（docs/09 第 6.2 节：站点能打开、资源全 200，但地址是 localhost，看不出来坏）。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:
    from dotenv import dotenv_values

    _env = dotenv_values(ROOT / ".env")
except Exception:  # pragma: no cover
    _env = {}

ADMIN_TOKEN = os.environ.get("TRAVELPLAN_ADMIN_TOKEN") or _env.get("TRAVELPLAN_ADMIN_TOKEN") or ""

BACKEND = "https://travelplan-api.onrender.com"
FRONTENDS = {
    "vercel": "https://travelplan-web.vercel.app",
    "edgeone": "https://travelplan-web.edgeone.cool",
}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def req(url: str, method: str = "GET", headers: dict | None = None, data: bytes | None = None, timeout: int = 90):
    r = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
            body = resp.read()
            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip":
                body = gzip.decompress(body)
            elif enc == "deflate":
                body = zlib.decompress(body, -zlib.MAX_WBITS)
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, dict(e.headers), body
    except Exception as e:  # noqa: BLE001
        return None, {}, str(e).encode()


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def check_backend() -> dict:
    head("1) 后端 Render  https://travelplan-api.onrender.com")

    # 冷启动重试
    status, _, body = None, {}, b""
    for attempt in range(12):
        status, hdrs, body = req(f"{BACKEND}/api/v1/health", timeout=60)
        if status == 200:
            break
        print(f"    ... 第 {attempt + 1} 次尝试 HTTP {status}（免费层冷启动），20s 后重试")
        time.sleep(20)

    print(f"GET /api/v1/health -> HTTP {status}")
    health = {}
    if status == 200:
        health = json.loads(body.decode("utf-8", "replace"))
        for k in ("status", "database_backend", "database_connected", "workflow", "project_id"):
            print(f"    {k} = {health.get(k)}")
        st = health.get("store", {})
        print(f"    store.backend = {st.get('backend')}  ok = {st.get('ok')}")
        print(f"    store.host    = {st.get('host')}")
        print(f"    store.target  = {st.get('target')}")
        print(f"    store.error   = {st.get('error')}")
        print(f"    providers_configured = {health.get('providers_configured')}")
    else:
        print(f"    body: {body[:300]!r}")

    # CORS 预检：两个前端域名都要被放行
    print("\n-- CORS 预检 OPTIONS /api/v1/plans --")
    cors = {}
    for name, origin in FRONTENDS.items():
        status, hdrs, body = req(
            f"{BACKEND}/api/v1/plans",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
            timeout=60,
        )
        allow = hdrs.get("Access-Control-Allow-Origin") or hdrs.get("access-control-allow-origin")
        cors[name] = allow
        verdict = "放行" if allow else "未放行(浏览器会 Failed to fetch)"
        print(f"    {name:8s} Origin={origin}")
        print(f"             HTTP {status}  Access-Control-Allow-Origin={allow!r}  -> {verdict}")

    # 管理端鉴权
    print("\n-- 管理端 GET /api/v1/admin/overview --")
    if ADMIN_TOKEN:
        status, _, body = req(
            f"{BACKEND}/api/v1/admin/overview",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            timeout=90,
        )
        print(f"    带 Token -> HTTP {status}")
        if status == 200:
            ov = json.loads(body.decode("utf-8", "replace"))
            print(f"    keys = {sorted(ov.keys())[:14]}")
        else:
            print(f"    body: {body[:300]!r}")
    status, _, body = req(f"{BACKEND}/api/v1/admin/overview", timeout=60)
    print(f"    不带 Token -> HTTP {status}（期望 401/403）")

    return {"health": health, "cors": cors}


def parse_chunks(html: str) -> list[str]:
    paths = set(re.findall(r'(/_next/static/[^"\'\\\s<>]+?\.js)', html))
    return sorted(paths)


def check_frontend(name: str, base: str) -> dict:
    head(f"前端 [{name}]  {base}")

    status, hdrs, body = req(base + "/", timeout=90)
    print(f"GET / -> HTTP {status}  bytes={len(body)}")
    info: dict = {"status": status, "chunks": 0, "inlined": None, "localhost_leak": None}
    if status != 200:
        print(f"    body: {body[:300]!r}")
        return info

    html = body.decode("utf-8", "replace")
    build_id = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    info["build_id"] = build_id.group(1) if build_id else None
    print(f"    Next.js buildId = {info['build_id']}")

    # 首页自带的可见文案，用来判断是不是最新一版前端
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    print(f"    <title> = {title.group(1).strip()[:90] if title else None}")
    h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    h1_text = re.sub(r"<[^>]+>", "", h1[0]).strip()[:80] if h1 else None
    print(f"    首个 <h1> = {h1_text}")
    info["h1"] = h1_text

    # 路由是否存在（404 说明线上是旧包）
    for route in ("/guided", "/admin"):
        s, _, b = req(base + route, timeout=90)
        print(f"    GET {route} -> HTTP {s}")
        info[f"route{route}"] = s

    chunks = parse_chunks(html)
    info["chunks"] = len(chunks)
    print(f"    引用到的 _next/static JS chunk = {len(chunks)} 个")

    # 关键：把 chunk 拉下来看后端地址到底内联成了什么
    blob = []
    for path in chunks:
        s, _, b = req(base + path, timeout=90)
        if s == 200:
            blob.append(b.decode("utf-8", "replace"))
    joined = "\n".join(blob)
    info["chunk_bytes"] = len(joined)

    for needle in ("travelplan-api.onrender.com", "localhost:8000", "127.0.0.1:8000", "localhost:8013"):
        found = needle in joined
        if needle == "travelplan-api.onrender.com":
            info["inlined"] = found
        if needle.startswith(("localhost", "127.0.0.1")):
            if found and info["localhost_leak"] is None:
                info["localhost_leak"] = needle
        print(f"    包内出现 {needle!r}: {found}")

    if info["inlined"] and not info["localhost_leak"]:
        print("    => 后端地址已正确内联为生产域名")
    elif info["inlined"] and info["localhost_leak"]:
        print(f"    => 生产域名在，但包里同时残留 {info['localhost_leak']}（需确认哪个是兜底）")
    elif not info["inlined"]:
        print("    => 包里没有生产域名，前端很可能指向 localhost（docs/09 §6.2 的静默失败）")
    return info


def main() -> None:
    results = {"backend": check_backend(), "frontends": {}}
    for name, base in FRONTENDS.items():
        results["frontends"][name] = check_frontend(name, base)

    head("汇总")
    h = results["backend"]["health"]
    print(f"后端  : HTTP 200={bool(h)}  status={h.get('status')}  db={h.get('database_backend')} connected={h.get('database_connected')}")
    for name, origin in FRONTENDS.items():
        c = results["backend"]["cors"].get(name)
        f = results["frontends"][name]
        print(
            f"{name:8s}: 站点 HTTP {f.get('status')}  buildId={f.get('build_id')}  "
            f"内联后端={'是' if f.get('inlined') else '否'}  CORS={'放行' if c else '未放行'}"
        )

    (ROOT / "_deploy_check_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n详细结果写入 _deploy_check_result.json")


if __name__ == "__main__":
    main()
