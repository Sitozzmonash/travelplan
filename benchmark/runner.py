"""Benchmark 运行器：跑一遍 Agent Loop 主路径的固定用例集。

    python -m benchmark                      # 全部套件（离线、秒级）
    python -m benchmark --suite basic        # 只跑一个套件
    python -m benchmark --limit 1            # 每个套件最多 1 例（冒烟）
    python -m benchmark --no-baseline        # 只出结果，不动基线

管理端也可以触发：`POST /api/v1/admin/benchmark/runs`（需要管理员 Token），
它调的正是本模块的 `run_benchmark`。

两条口径必须记住
----------------
1. **全部离线、全部确定性**。假 ProviderHub + 脚本模型，不联网、不花额度、可重复。
   本套件**没有** live 套件 —— 真实链路的抽检不在这里（它会引入"今天接口抖了"的噪声，
   而噪声会被读成"代码退化了"）。
2. **`passed` 是"结果符合预期"，不是"run 成功"**。有的用例的期望就是"如实失败"
   （工具返回空时不许编造行程）—— 那种用例 run 是 failed、judge 判 pass。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import current_config, override_env
from app.store import TravelPlanStore
from app.version import superharness_commit, travelplan_commit
from benchmark.fixtures.world import FIXTURE_VERSION, build_world
from benchmark.judges import grounding_report, judge
from benchmark.metrics import (
    ALL_METRICS,
    combine_suites,
    compare_metrics,
    compute_case_metrics,
    group,
)

BENCHMARK_VERSION = "v0.3-agent-loop"

#: 确定性套件（离线、可重复）。
DETERMINISTIC_SUITES: tuple[str, ...] = (
    "basic",
    "hard",
    "provider_failure",
    "badcase_regression",
)

#: 旧套件名里对本路径**不适用**的那两个。保留在 `SUITES` 里是为了让管理端的复选框
#: （前端 `BENCHMARK_SUITES` 是固定清单）传进来的名字不会直接把整轮打挂 ——
#: 它们会被显式跳过并在 notes 里说明，而不是静默无视。
RETIRED_SUITES: tuple[str, ...] = ("jev_decision", "live_smoke")

SUITES: tuple[str, ...] = (*DETERMINISTIC_SUITES, *RETIRED_SUITES)

LIVE_SUITE = "live_smoke"

#: 结果里的"模型"一栏。这一路用的是脚本模型 —— 如实写，不借 MODEL_NAME 冒充真实模型。
MODEL_LABEL = "scripted-offline"

CASES_DIR = Path(__file__).resolve().parent / "cases"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

#: 本路径的基线文件。**只有一份**：这里没有 Jev OFF/ON 两种配置可对照
#: （`app/api.py::_benchmark_baselines` 读的是历史 Jev 对，本路径下它必然是空）。
BASELINE_FILE = "agent_loop.json"


# ======================================================================
# 用例装载
# ======================================================================


def load_cases(suite: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """读一个套件的 case。格式错直接抛 —— 静默跳过坏 case 会让指标悄悄失真。"""

    path = CASES_DIR / f"{suite}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"没有 {suite} 套件：{path} 不存在")
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{number} 不是合法 JSON：{exc}") from exc
        _validate_case(case, where=f"{path.name}:{number}")
        cases.append(case)
    if limit is not None:
        cases = cases[: max(1, limit)]
    return cases


def _validate_case(case: Mapping[str, Any], *, where: str) -> None:
    for key in ("case_id", "suite", "title", "query"):
        if not case.get(key):
            raise ValueError(f"{where} 缺少必填字段 {key}")
    if not case.get("submit") and not case.get("script") and not case.get("expect", {}).get(
        "expect_no_plan"
    ):
        # 既不交卷、也没有原始剧本、又不期待"没有 plan"：这条用例没有可判定的结果。
        raise ValueError(f"{where} 没有任何交卷参数（submit）或原始剧本（script）")
    expect = case.get("expect")
    if expect is not None and not isinstance(expect, dict):
        raise ValueError(f"{where} 的 expect 必须是对象")


# ======================================================================
# 单例执行
# ======================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def observe(case: Mapping[str, Any], world: Any, result: Any, *, elapsed_ms: int) -> Any:
    """把一次 run 的产物收敛成判分/指标可用的**事实集合**。"""

    from benchmark.fixtures.world import Observation

    store = world.store
    run_id = world.run_id
    plan = result.plan
    warnings = list(getattr(plan, "warnings", []) or [])
    errors = [issue for issue in warnings if str(getattr(issue, "severity", "")) == "error"]
    reject_ids = sorted(
        str(key)
        for key, value in (dict(case.get("intent") or {}).get("place_selections") or {}).items()
        if str(value).upper() == "REJECT"
    )
    if plan is not None:
        reject_ids = sorted(
            str(key)
            for key, value in (plan.intent.place_selections or {}).items()
            if str(value).upper() == "REJECT"
        )
    plan_place_ids = (
        {str(item.place_id) for day in plan.days for item in day.items if item.place_id}
        if plan is not None
        else set()
    )
    progress = store.get_run_progress(run_id) or {}
    output_dir = Path(world.output_dir) / run_id

    return Observation(
        run_id=run_id,
        status=str(result.status),
        error=result.error,
        plan=plan,
        degradations=list(result.degradations or ()),
        progress_status=str(progress.get("status") or ""),
        warnings=warnings,
        unresolved_conflicts=sum(1 for issue in errors if not getattr(issue, "resolved", False)),
        conflicts_detected=len(errors),
        conflicts_resolved=sum(1 for issue in errors if getattr(issue, "resolved", False)),
        reject_ids=reject_ids,
        reject_intruders=sorted(plan_place_ids & set(reject_ids)),
        grounding={},
        provider_calls=list(world.hub.audit_entries()),
        sources=list(store.list_sources(run_id) or ()),
        run_metrics=dict(store.get_run_metrics(run_id) or {}),
        audit=dict(result.audit or {}),
        injected=list(world.hub.injected),
        artifacts=sorted(path.name for path in output_dir.glob("*")) if output_dir.is_dir() else [],
        facts=world.hub.facts.as_dict(),
        elapsed_ms=elapsed_ms,
    )


def run_case(
    case: Mapping[str, Any], *, store: TravelPlanStore, output_dir: Path | None = None
) -> dict[str, Any]:
    """跑一个 case：真的 Agent Loop → 判分 → 指标。单例崩了不影响其他 case。"""

    case_id = str(case["case_id"])
    run_id = f"bm-{case_id}-{uuid.uuid4().hex[:6]}"
    workdir = Path(output_dir) if output_dir is not None else Path(tempfile.mkdtemp(prefix="tp-bench-"))
    world = build_world(dict(case), store=store, run_id=run_id, output_dir=workdir)
    try:
        result, elapsed_ms = world.run()
    except Exception as exc:  # noqa: BLE001 —— 一个 case 崩了不能带走整个套件
        return {
            "case_id": case_id,
            "suite": case.get("suite") or "",
            "title": case.get("title") or "",
            "status": "fail",
            "failures": [f"{type(exc).__name__}: {exc}"],
            "checks": {},
            "metrics": {name: None for name in ALL_METRICS},
            "detail": {"run_id": run_id, "injected": world.hub.injected, "error": repr(exc)},
        }

    observation = observe(case, world, result, elapsed_ms=elapsed_ms)
    observation.grounding = grounding_report(observation)
    status, failures, checks = judge(case, observation)
    metrics = compute_case_metrics(case, observation, passed=status == "pass")
    return {
        "case_id": case_id,
        "suite": case.get("suite") or "",
        "title": case.get("title") or "",
        "status": status,
        "failures": failures,
        "checks": checks,
        "metrics": metrics,
        "detail": {
            "run_id": run_id,
            "run_status": observation.status,
            "run_error": observation.error,
            "progress_status": observation.progress_status,
            "degradations": observation.degradations,
            "artifacts": observation.artifacts,
            "injected": observation.injected,
            "reject_ids": observation.reject_ids,
            "reject_intruders": observation.reject_intruders,
            "unresolved_conflicts": observation.unresolved_conflicts,
            "conflicts_detected": observation.conflicts_detected,
            "conflicts_resolved": observation.conflicts_resolved,
            "grounding": observation.grounding,
            "provider_calls": [
                {"tool": row.get("tool"), "status": row.get("status")}
                for row in observation.provider_calls
            ],
            "plan_days": len(observation.plan.days) if observation.plan is not None else 0,
            "plan_place_ids": observation.plan_place_ids,
        },
    }


# ======================================================================
# 整轮运行
# ======================================================================


def run_benchmark(
    *,
    store: TravelPlanStore,
    suites: Sequence[str] | None = None,
    jev_enabled: bool | None = None,
    limit: int | None = None,
    live: bool = False,
    benchmark_run_id: str | None = None,
    write_baseline: bool = False,
    results_dir: Path | None = None,
    baselines_dir: Path | None = None,
) -> dict[str, Any]:
    """跑一轮 Benchmark：逐 case 执行 → 判定 → 聚合 → 落库（可选写基线/结果）。

    参数里的 `jev_enabled` / `live` 是**管理端契约的兼容位**（`app/api.py` 会传）：
    本路径没有 Jev、也不支持联网 live，所以两者都不改变运行方式，只在 notes 里如实说明
    被忽略了什么 —— 静默接受会让人以为"关掉 Jev 跑了一轮对照"。

    `write_baseline` 默认 **False**：管理端触发的一轮评测不该覆盖仓库里的基线。
    CLI 默认打开（`--no-baseline` 可关）。
    """

    selected = [str(suite) for suite in (suites or DETERMINISTIC_SUITES)]
    if live and LIVE_SUITE not in selected:
        selected.append(LIVE_SUITE)
    unknown = [suite for suite in selected if suite not in SUITES]
    if unknown:
        raise ValueError(f"未知套件：{unknown}；可用：{list(SUITES)}")

    retired = [suite for suite in selected if suite in RETIRED_SUITES]
    runnable = [suite for suite in selected if suite in DETERMINISTIC_SUITES]
    # 用户只勾了退役套件时，不能跑出"0 例 100% 通过"这种假结论 —— 明确报错。
    if not runnable:
        raise ValueError(
            f"选中的套件都已退役：{retired}。本路径没有 Jev、也不做联网 live，"
            f"可用套件：{list(DETERMINISTIC_SUITES)}"
        )

    resolved_id = benchmark_run_id or f"bm-{_now().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"
    existing = store.get_benchmark_run(resolved_id) or {}

    cases: list[dict[str, Any]] = []
    for suite in runnable:
        cases.extend(load_cases(suite, limit=limit))

    store.save_benchmark_run(
        {
            "benchmark_run_id": resolved_id,
            "suite": ",".join(runnable),
            "version": BENCHMARK_VERSION,
            "travelplan_commit": travelplan_commit(),
            "superharness_commit": superharness_commit(),
            "model": MODEL_LABEL,
            "fixture_version": FIXTURE_VERSION,
            "config_json": existing.get("config") or current_config().public_dict(),
            # 本路径没有 Jev：如实写 0，不留一个"看起来开着 Jev"的字段。
            "jev_enabled": 0,
            "live": 0,
            "status": "RUNNING",
            "started_at": existing.get("started_at") or _now(),
            "case_count": len(cases),
            "notes": existing.get("notes") or "",
        }
    )

    scope = Path(tempfile.mkdtemp(prefix="tp-benchmark-"))
    results: list[dict[str, Any]] = []
    for case in cases:
        result = run_case(case, store=store, output_dir=scope)
        results.append(result)
        store.save_benchmark_case_result(
            {
                "benchmark_run_id": resolved_id,
                "case_id": result["case_id"],
                "suite": result["suite"],
                "title": result["title"],
                "status": result["status"],
                "metrics": result["metrics"],
                "detail": result["detail"],
            }
        )

    combined, per_suite = combine_suites(results, runnable)
    passed = sum(1 for result in results if result["status"] == "pass")
    failed = len(results) - passed
    notes = (
        f"确定性 {len(results)} 例（通过 {passed}）"
        + (f"；跳过退役套件 {retired}（本路径无 Jev / 不联网）" if retired else "")
        + ("；jev_enabled 参数被忽略（本路径没有 Jev）" if jev_enabled is not None else "")
        + ("；live 参数被忽略（本套件全部离线）" if live else "")
    )
    metrics_payload: dict[str, Any] = {
        **combined,
        "groups": group(combined),
        "per_suite": per_suite,
        "version": BENCHMARK_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "travelplan_commit": travelplan_commit(),
        "superharness_commit": superharness_commit(),
        "model": MODEL_LABEL,
        "scope": "agent_loop",
        "case_results": [
            {
                "case_id": result["case_id"],
                "suite": result["suite"],
                "status": result["status"],
                "failures": result["failures"],
            }
            for result in results
        ],
        "retired_suites": retired,
    }

    store.update_benchmark_run(
        resolved_id,
        {
            "status": "SUCCESS",
            "finished_at": _now(),
            "case_count": len(results),
            "passed": passed,
            "failed": failed,
            "metrics": metrics_payload,
            "notes": notes,
        },
    )

    if write_baseline:
        _write_baseline(
            {
                "benchmark_version": BENCHMARK_VERSION,
                "scope": "agent_loop",
                "fixture_version": FIXTURE_VERSION,
                "travelplan_commit": travelplan_commit(),
                "superharness_commit": superharness_commit(),
                "model": MODEL_LABEL,
                "suites": runnable,
                "case_count": len(results),
                "passed": passed,
                "failed": failed,
                "metrics": combined,
                "generated_at": _now(),
            },
            baselines_dir=baselines_dir,
        )

    written = _write_results(resolved_id, results, metrics_payload, results_dir=results_dir)
    if not written:
        store.update_benchmark_run(resolved_id, {"notes": notes + "；结果未落盘（目录不可写）"})

    return store.get_benchmark_run(resolved_id) or {}


def _write_baseline(payload: Mapping[str, Any], *, baselines_dir: Path | None = None) -> None:
    target = baselines_dir or BASELINES_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / BASELINE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    except OSError:  # 只读文件系统（容器）/ 权限问题：不改变本轮结论
        return


def _write_results(
    benchmark_run_id: str,
    results: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    *,
    results_dir: Path | None = None,
) -> bool:
    """把这一轮结果写进 `benchmark/results/<id>/`（逐例判定 + 聚合指标）。

    只写"能用来复盘"的内容：完整 plan 在库里与 `outputs/<run_id>/`，不在这里重复一份。
    写不进去就返回 False（调用方把这件事写进 notes），而不是让整轮评测失败。
    """

    target = (results_dir or RESULTS_DIR) / benchmark_run_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "cases.jsonl").write_text(
            "".join(json.dumps(result, ensure_ascii=False, default=str) + "\n" for result in results),
            encoding="utf-8",
        )
        (target / "summary.json").write_text(
            json.dumps(
                {
                    "benchmark_run_id": benchmark_run_id,
                    "generated_at": _now(),
                    "case_count": len(results),
                    "passed": sum(1 for result in results if result["status"] == "pass"),
                    "failed": sum(1 for result in results if result["status"] != "pass"),
                    "metrics": metrics,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def read_baseline(*, baselines_dir: Path | None = None) -> dict[str, Any] | None:
    """读本路径的基线；没有就返回 None（不编一份出来）。"""

    path = (baselines_dir or BASELINES_DIR) / BASELINE_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ======================================================================
# Evolution 的评测入口
# ======================================================================


def make_measure(
    store: TravelPlanStore | None = None,
    *,
    suites: Sequence[str] = ("basic", "hard"),
    limit: int | None = None,
) -> Callable[[Mapping[str, str]], dict[str, float] | None]:
    """给 Evolution 用的评测入口：``measure(overrides) -> 扁平指标``。

    Evolution 验证的是"候选配置改动在回归用例上的表现"，所以它用**临时库**跑：
    把几十条对照 run 灌进生产库会污染管理端的运行列表。临时目录交给操作系统回收。
    `store` 只用来对齐磁盘位置，不使用它的连接。
    """

    scratch_dir = Path(tempfile.mkdtemp(prefix="tp-evolution-"))
    scratch = TravelPlanStore(db_path=scratch_dir / "evolution.db")
    scratch.init_schema()

    def measure(overrides: Mapping[str, str]) -> dict[str, float] | None:
        env = {str(key): str(value) for key, value in (overrides or {}).items()}
        cases: list[dict[str, Any]] = []
        for suite in suites:
            if suite in DETERMINISTIC_SUITES:
                cases.extend(load_cases(suite, limit=limit))
        if not cases:
            return None
        with override_env(env):
            results = [run_case(case, store=scratch) for case in cases]
        metrics, _ = combine_suites(results, list(suites))
        return {name: value for name, value in metrics.items() if value is not None}

    return measure


# ======================================================================
# CLI
# ======================================================================


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark",
        description=f"TravelPlan Benchmark {BENCHMARK_VERSION}（Agent Loop 主路径，全部离线）",
    )
    parser.add_argument(
        "--suite", action="append", choices=list(DETERMINISTIC_SUITES), help="可重复；默认跑全部"
    )
    parser.add_argument("--limit", type=int, default=None, help="每个套件最多跑几个 case（冒烟用）")
    parser.add_argument("--db", default=None, help="跑评测用的库路径（默认临时库，不碰生产库）")
    parser.add_argument("--no-baseline", action="store_true", help="不更新 baselines/")
    parser.add_argument("--results-dir", default=None, help="结果目录（默认 benchmark/results）")
    parser.add_argument("--json", action="store_true", help="只打印汇总 JSON")
    parser.add_argument(
        "--compare-baseline", action="store_true", help="与 baselines/ 里的基线逐指标对比"
    )
    args = parser.parse_args(argv)

    store = (
        TravelPlanStore(db_path=Path(args.db))
        if args.db
        else TravelPlanStore(db_path=Path(tempfile.mkdtemp(prefix="tp-bench-db-")) / "bench.db")
    )
    store.init_schema()

    run = run_benchmark(
        store=store,
        suites=args.suite,
        limit=args.limit,
        write_baseline=not args.no_baseline,
        results_dir=Path(args.results_dir) if args.results_dir else None,
    )
    metrics = run.get("metrics") or {}
    if args.json:
        print(json.dumps(run, ensure_ascii=False, indent=2, default=str))
        return 0 if not run.get("failed") else 1

    print(f"benchmark_run_id={run.get('benchmark_run_id')}  status={run.get('status')}")
    print(f"确定性用例：{run.get('case_count')} 例，通过 {run.get('passed')}，失败 {run.get('failed')}")
    print(f"备注：{run.get('notes')}")
    print()
    for envelope in metrics.get("case_results") or []:
        mark = "PASS" if envelope.get("status") == "pass" else "FAIL"
        print(f"  [{mark}] {envelope.get('suite')}/{envelope.get('case_id')}")
        for failure in envelope.get("failures") or []:
            print(f"         - {failure}")
    print()
    print("指标（按维度）：")
    for name, rows in (metrics.get("groups") or {}).items():
        print(f"  {name}:")
        for key, value in rows.items():
            print(f"    {key} = {value}")
    baseline = read_baseline()
    if args.compare_baseline and baseline:
        comparison = compare_metrics(baseline.get("metrics") or {}, metrics)
        print()
        print(f"与基线对比（基线生成于 {baseline.get('generated_at')}）：")
        for key, delta in comparison["delta_pct"].items():
            print(f"  {key}: {comparison['baseline'].get(key)} → {comparison['current'].get(key)} ({delta}%)")
    return 1 if run.get("failed") else 0


if __name__ == "__main__":  # pragma: no cover - 由 `python -m benchmark` 覆盖
    sys.exit(main())
