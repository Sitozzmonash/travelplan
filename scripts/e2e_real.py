"""真实 E2E 驱动（性能任务 §15）：走完整 Guided 旅程直到拿到终态行程。

为什么要这个脚本：性能验收要求"真实 3 天 / 5 天各跑一次，并和优化前对比"。
手工点向导既慢又不可复现，所以这里把整条链路脚本化，并把**用户感知耗时**拆开：

    t0 建 Session ──► t1 Discovery 完成 ──► t2 点「开始规划」 ──► t3 Run 终态

  * `discovery_s` = t1 - t0：用户在前端选偏好时后台已经查了多久（可被复用，不计入等待）；
  * `plan_s`      = t3 - t2：点「开始规划」之后真正让用户等的时间；
  * `total_s`     = t3 - t0：从打开页面到拿到行程的总时间。

优化是否有效，看的是 `plan_s` 与 `total_s`，而不是某个阶段的自我感觉。
跑完再用 `python scripts/profile_run.py <run_id>` 看阶段级拆分。

用法：
    python scripts/e2e_real.py --origin 北京 --destination 成都 \\
        --start-date 2026-10-01 --days 5 --travelers 2 --budget 6000
    python scripts/e2e_real.py ... --json _acceptance/e2e_5d_after.json

前置：API 已在 `--base-url` 上运行（默认 127.0.0.1:8013）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

#: Run 阶段轮询上限。真实 5 天行程优化前约 600s，给足余量再判定超时。
RUN_TIMEOUT_S = 1500.0
#: Discovery 上限。优化前真实 Discovery 可以跑到 600s+（各家 Provider 串行），
#: 所以默认给到 900s；超时也不视为失败（会退化成正式流程自己补查）。
DISCOVERY_TIMEOUT_S = 900.0
POLL_INTERVAL_S = 3.0
TERMINAL = {"SUCCESS", "DEGRADED", "FAILED", "CANCELLED"}


def _discovery_states(session: dict[str, Any]) -> dict[str, str]:
    """把 discovery_status 压成 {stage: state}，兼容 dict/str 两种形态。"""

    raw = session.get("discovery_status") or {}
    if not isinstance(raw, dict):
        return {}
    states: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            states[key] = str(value.get("status") or value.get("state") or "?")
        else:
            states[key] = str(value)
    return states


def _discovery_done(session: dict[str, Any]) -> bool:
    states = _discovery_states(session)
    if not states:
        return False
    return all(state.upper() not in {"RUNNING", "PENDING", "QUEUED"} for state in states.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 Guided 旅程 E2E：建会话 → Discovery → 开始规划 → 终态")
    parser.add_argument("--base-url", default="http://127.0.0.1:8013")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--travelers", type=int, default=2)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument(
        "--wait-discovery",
        action="store_true",
        default=True,
        help="等 Discovery 全部完成再开始规划（默认；能测到「全部复用」路径）",
    )
    parser.add_argument("--no-wait-discovery", dest="wait_discovery", action="store_false")
    parser.add_argument("--transport-priority", default="auto")
    parser.add_argument("--hotel-priority", default="auto")
    parser.add_argument("--pace", default="auto")
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=DISCOVERY_TIMEOUT_S,
        help="等 Discovery 的上限秒数（优化前真实 Discovery 可能 600s+）",
    )
    parser.add_argument("--json", help="把结果写到这个路径（便于 Before/After 对比）")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=60.0)
    result: dict[str, Any] = {
        "origin": args.origin,
        "destination": args.destination,
        "start_date": args.start_date,
        "days": args.days,
        "travelers": args.travelers,
        "budget_total": args.budget,
        "waited_for_discovery": args.wait_discovery,
    }

    t0 = time.monotonic()
    created = client.post(
        "/api/v1/planning-sessions",
        json={
            "origin": args.origin,
            "destination": args.destination,
            "start_date": args.start_date,
            "days": args.days,
            "travelers": args.travelers,
            **({"budget_total": args.budget} if args.budget else {}),
        },
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    result["session_id"] = session_id
    print(f"[t0] session {session_id} 已创建，后台 Discovery 开始")

    # ---- 等 Discovery（模拟用户在前端选偏好花掉的时间）----
    discovery_deadline = time.monotonic() + args.discovery_timeout
    session: dict[str, Any] = {}
    while True:
        session = client.get(f"/api/v1/planning-sessions/{session_id}").json()
        if _discovery_done(session):
            break
        if time.monotonic() > discovery_deadline:
            print("[warn] Discovery 超时，按「仍未完成」继续（应退化为补查，不算失败）")
            break
        time.sleep(POLL_INTERVAL_S)
    t1 = time.monotonic()
    states = _discovery_states(session)
    result["discovery_s"] = round(t1 - t0, 1)
    result["discovery_states"] = states
    result["place_candidates"] = len(session.get("place_candidates") or [])
    result["transport_candidates"] = len(session.get("transport_candidates") or [])
    result["hotel_candidates"] = len(session.get("hotel_candidates") or [])
    print(f"[t1] Discovery 用时 {t1 - t0:.1f}s  {states}")
    print(
        f"     候选：交通 {result['transport_candidates']} / 酒店 {result['hotel_candidates']} / "
        f"地点 {result['place_candidates']}"
    )

    # ---- 偏好：默认全 auto，与「用户全点帮我选」一致 ----
    patched = client.patch(
        f"/api/v1/planning-sessions/{session_id}",
        json={
            "transport_priority": args.transport_priority,
            "hotel_priority": args.hotel_priority,
            "pace": args.pace,
        },
    )
    patched.raise_for_status()

    # ---- 开始规划 ----
    t2 = time.monotonic()
    started = client.post(f"/api/v1/planning-sessions/{session_id}/start")
    started.raise_for_status()
    run_id = started.json()["run_id"]
    result["run_id"] = run_id
    print(f"[t2] 正式 Run {run_id} 已创建，开始轮询")

    status: dict[str, Any] = {}
    run_deadline = time.monotonic() + RUN_TIMEOUT_S
    last_message = ""
    while True:
        status = client.get(f"/api/v1/plans/{run_id}/status").json()
        state = str(status.get("status") or "")
        message = str(status.get("message") or "")
        if message and message != last_message:
            elapsed = time.monotonic() - t2
            print(f"      [{elapsed:6.1f}s] {state} {message}")
            last_message = message
        if state in TERMINAL:
            break
        if time.monotonic() > run_deadline:
            print(f"[error] Run 超过 {RUN_TIMEOUT_S}s 仍未终态，按超时记录")
            break
        time.sleep(POLL_INTERVAL_S)
    t3 = time.monotonic()

    result["run_status"] = status.get("status")
    result["run_message"] = status.get("message")
    result["run_error"] = status.get("error")
    result["plan_s"] = round(t3 - t2, 1)
    result["total_s"] = round(t3 - t0, 1)
    result["timed_out"] = str(status.get("status") or "") not in TERMINAL

    print()
    print(f"[t3] Run 终态={result['run_status']}")
    print(f"     Discovery {result['discovery_s']}s | 点开始规划后 {result['plan_s']}s | 总 {result['total_s']}s")
    if result["run_error"]:
        print(f"     错误：{result['run_error']}")

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"     已写入 {path}")

    # 非终态才算失败；DEGRADED 是"可用但有降级"，属于正常结果。
    return 0 if result["run_status"] in {"SUCCESS", "DEGRADED"} else 1


if __name__ == "__main__":
    sys.exit(main())
