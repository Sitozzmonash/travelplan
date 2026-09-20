"""Benchmark v0.1 运行器（接管任务 §9）。

    python -m benchmark.runner --suite basic
    python -m benchmark.runner --jev on --limit 5
    python -m benchmark.runner --live          # 额外跑 Live Smoke（会真的打第三方）

两条口径必须记住：

1. **确定性套件离线**。basic / hard / badcase_regression / provider_failure / jev_decision
   全部注入假件，不联网、不消耗额度、可重复；`live_smoke` 是唯一联网的套件，
   它的结果单独统计，**不进**确定性指标（否则"今天接口抖了"会被读成"代码退化了"）。
2. **Jev OFF / ON 必须用同一批 case 跑两遍**。OFF 基线不是"关掉开关"，而是真的走
   "未配置 Jev"的生产分支（`JEV_ENABLED=false` 且不注入替身），否则基线证明不了任何事。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.config import current_config, override_env
from app.store import TravelPlanStore
from app.version import fixture_version, model_name, superharness_commit, travelplan_commit
from app.workflow import execute_travel_run
from benchmark.fixtures.world import FIXTURE_VERSION, build_world
from benchmark.judges import data_integrity_checks, judge, summarize_failures
from benchmark.metrics import (
    ALL_METRICS,
    RunFacts,
    compute_metrics,
    group,
    jev_comparison,
    summarize_cases,
)

BENCHMARK_VERSION = "v0.1"

#: 确定性套件（离线、可重复）。
DETERMINISTIC_SUITES: tuple[str, ...] = (
    "basic",
    "hard",
    "badcase_regression",
    "provider_failure",
    "jev_decision",
)

#: 联网套件，单独统计。
LIVE_SUITE = "live_smoke"

SUITES: tuple[str, ...] = (*DETERMINISTIC_SUITES, LIVE_SUITE)

CASES_DIR = Path(__file__).resolve().parent / "cases"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


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
        for key in ("case_id", "suite", "query"):
            if not case.get(key):
                raise ValueError(f"{path.name}:{number} 缺少必填字段 {key}")
        cases.append(case)
    if limit is not None:
        cases = cases[: max(1, limit)]
    return cases


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_case(
    case: dict[str, Any],
    *,
    store: TravelPlanStore,
    jev_enabled: bool = True,
    live: bool = False,
) -> dict[str, Any]:
    """跑一个 case，返回判定结果 + 该 case 的指标。单个 case 失败不影响其他 case。"""

    run_id = f"bm-{case['case_id']}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(tempfile.mkdtemp(prefix="tp-bench-"))
    world = build_world(case, store=store, run_id=run_id, live=live, jev_enabled=jev_enabled)
    env = {
        "JEV_ENABLED": "true" if jev_enabled else "false",
        # 基准里的 Jev 一定是注入的假件（或明确关闭），绝不能因为环境里有 Key 就真的打出去。
        "JEV_MAX_CALLS_PER_RUN": "5",
    }

    try:
        with override_env(env):
            result = execute_travel_run(
                str(case["query"]),
                store=store,
                hub=world.hub,
                llm=world.llm,
                jev=world.jev,
                run_id=run_id,
                output_dir=output_dir,
                # 标注来源：管理端据此把评测用例运行从真实运行列表里过滤掉，
                # Provider Health 也能区分"这是评测打的"还是"真实用户打的"。
                source="benchmark",
            )
    except Exception as exc:  # noqa: BLE001 —— 一个 case 崩了不能带走整个套件
        return {
            "case_id": case["case_id"],
            "suite": case.get("suite") or "",
            "title": case.get("title") or "",
            "status": "fail",
            "failures": [f"{type(exc).__name__}: {exc}"],
            "metrics": {name: None for name in ALL_METRICS},
            "detail": {"run_id": run_id, "injected": world.injected, "error": repr(exc)},
        }
    finally:
        world.close()

    audit = result.audit or {}
    badcases, _ = store.list_badcases(run_id=run_id, limit=200)
    facts = RunFacts(
        plan=result.plan,
        spans=store.get_trace_spans(run_id),
        metrics=store.get_run_metrics(run_id) or {},
        evidence=store.get_evidence(run_id),
        places=store.get_places(run_id),
        badcases=badcases,
        status=result.status,
    )
    metrics = compute_metrics(facts)
    observation = {
        "status": result.status,
        "plan": result.plan,
        "badcase_categories": [item["category"] for item in badcases],
        "plan_choice": audit.get("plan_choice") or {},
        "quality_gate": audit.get("quality_gate") or {},
    }
    status, failures = judge(case, observation)
    return {
        "case_id": case["case_id"],
        "suite": case.get("suite") or "",
        "title": case.get("title") or "",
        "status": status,
        "failures": failures,
        "metrics": metrics,
        "detail": {
            "run_id": run_id,
            "run_status": result.status,
            "degradations": list(result.degradations)[:5],
            "injected": world.injected,
            "badcase_categories": observation["badcase_categories"],
            "plan_choice": {
                "selected": (observation["plan_choice"] or {}).get("selected"),
                "fallback": (observation["plan_choice"] or {}).get("fallback"),
                "reason": (observation["plan_choice"] or {}).get("reason"),
            },
            "quality_gate": {
                "decision": (observation["quality_gate"] or {}).get("decision"),
                "signals": (observation["quality_gate"] or {}).get("signals"),
            },
            "data_integrity": data_integrity_checks(result.plan)[:10],
        },
    }


def run_suites(
    cases: Sequence[dict[str, Any]],
    *,
    store: TravelPlanStore,
    jev_enabled: bool,
    live: bool,
) -> list[dict[str, Any]]:
    return [
        run_case(case, store=store, jev_enabled=jev_enabled, live=live) for case in cases
    ]


def _combine_suites(
    results: Sequence[dict[str, Any]], suites: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把逐 case 结果按套件聚合，再按套件等权合成总指标。

    等权（而不是按 case 数加权）是有意的：basic 有 12 个 case、jev_decision 只有 4 个，
    按 case 数加权会让"基础用例多"直接盖过"软决策表现"。每个套件先算自己的均值，
    再对套件求均值，这样任何一个维度的退化都不会被数量稀释。
    """

    per_suite: dict[str, Any] = {}
    for suite in suites:
        subset = [result for result in results if result["suite"] == suite]
        if not subset:
            continue
        summary = summarize_cases(subset)
        summary["failures"] = summarize_failures(subset)
        per_suite[suite] = summary

    combined: dict[str, float | None] = {}
    for name in ALL_METRICS:
        values = [
            float(summary["metrics"][name])
            for summary in per_suite.values()
            if isinstance(summary["metrics"].get(name), (int, float))
        ]
        combined[name] = round(sum(values) / len(values), 4) if values else None
    return combined, per_suite


def _read_baseline(mode: str, *, baselines_dir: Path | None = None) -> dict[str, Any] | None:
    path = (baselines_dir or BASELINES_DIR) / f"jev_{mode}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_baseline(mode: str, payload: dict[str, Any], *, baselines_dir: Path | None = None) -> None:
    target = baselines_dir or BASELINES_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / f"jev_{mode}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def run_benchmark(
    *,
    store: TravelPlanStore,
    suites: Sequence[str] | None = None,
    jev_enabled: bool | None = None,
    limit: int | None = None,
    live: bool = False,
    benchmark_run_id: str | None = None,
    write_baseline: bool = True,
    results_dir: Path | None = None,
    baselines_dir: Path | None = None,
) -> dict[str, Any]:
    """跑一轮 Benchmark：逐 case 执行 → 判定 → 聚合 → 落库 → 写基线。

    返回落库后的 benchmark run（含 metrics 与 passed/failed）。
    """

    selected = [suite for suite in (suites or DETERMINISTIC_SUITES)]
    if live and LIVE_SUITE not in selected:
        selected.append(LIVE_SUITE)
    unknown = [suite for suite in selected if suite not in SUITES]
    if unknown:
        raise ValueError(f"未知套件：{unknown}；可用：{list(SUITES)}")

    resolved_jev = current_config().jev_enabled if jev_enabled is None else jev_enabled
    # Jev 关闭时不跑 jev_decision 套件：这批用例的期望本来就是"Jev 会做出某个决策"，
    # 关掉它跑出来的必然全是 fail —— 那不是产品退化，是套件前提不成立。
    # 需要 Jev 的单个用例用 `requires_jev: true` 标记（坏例回归里也有这类）。
    if not resolved_jev:
        selected = [suite for suite in selected if suite != "jev_decision"]
    resolved_id = benchmark_run_id or f"bm-{_now().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"

    cases: list[dict[str, Any]] = []
    skipped: list[str] = []
    for suite in selected:
        suite_cases = load_cases(suite, limit=limit)
        if suite == LIVE_SUITE:
            for case in suite_cases:
                case["live"] = True
        for case in suite_cases:
            if case.get("requires_jev") and not resolved_jev:
                skipped.append(str(case["case_id"]))
                continue
            cases.append(case)

    store.save_benchmark_run(
        {
            "benchmark_run_id": resolved_id,
            "suite": ",".join(selected),
            "version": BENCHMARK_VERSION,
            "travelplan_commit": travelplan_commit(),
            "superharness_commit": superharness_commit(),
            "model": model_name(),
            "fixture_version": FIXTURE_VERSION,
            "config_json": current_config().public_dict(),
            "jev_enabled": 1 if resolved_jev else 0,
            "live": 1 if live else 0,
            "status": "RUNNING",
            "started_at": _now(),
            "case_count": len(cases),
        }
    )

    results: list[dict[str, Any]] = []
    for case in cases:
        results.append(
            run_case(
                case,
                store=store,
                jev_enabled=resolved_jev,
                live=bool(case.get("live")),
            )
        )
        store.save_benchmark_case_result(
            {
                "benchmark_run_id": resolved_id,
                "case_id": result_case_id(case),
                "suite": case.get("suite") or "",
                "title": case.get("title") or "",
                "status": results[-1]["status"],
                "metrics": results[-1]["metrics"],
                "detail": results[-1]["detail"],
            }
        )

    # Live Smoke 单独统计：它的波动来自第三方，不能混进确定性指标。
    deterministic = [result for result in results if result["suite"] != LIVE_SUITE]
    combined, per_suite = _combine_suites(deterministic, [s for s in selected if s != LIVE_SUITE])
    live_summary = summarize_cases([r for r in results if r["suite"] == LIVE_SUITE])

    baseline_compare = None
    off = _read_baseline("off", baselines_dir=baselines_dir)
    on = _read_baseline("on", baselines_dir=baselines_dir)
    if off and on:
        baseline_compare = {
            **jev_comparison(off.get("metrics") or {}, on.get("metrics") or {}),
            "off_generated_at": off.get("generated_at"),
            "on_generated_at": on.get("generated_at"),
        }

    passed = sum(1 for result in deterministic if result["status"] == "pass")
    failed = len(deterministic) - passed
    metrics_payload: dict[str, Any] = {
        **combined,
        "groups": group(combined),
        "per_suite": per_suite,
        "live_smoke": {
            "case_count": live_summary["case_count"],
            "passed": live_summary["passed"],
            "failed": live_summary["failed"],
            "note": "Live Smoke 联网，结果单独统计，不参与确定性指标",
        },
        "baseline_compare": baseline_compare,
        "version": BENCHMARK_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "travelplan_commit": travelplan_commit(),
        "superharness_commit": superharness_commit(),
        "model": model_name(),
        "jev_enabled": resolved_jev,
        "case_results": [
            {"case_id": result["case_id"], "status": result["status"], "failures": result["failures"]}
            for result in deterministic
        ],
        "skipped": skipped,
        "skipped_note": (
            "这些用例需要 Jev（requires_jev），本轮 Jev 关闭，未计入通过率"
            if skipped
            else None
        ),
    }

    store.update_benchmark_run(
        resolved_id,
        {
            "status": "SUCCESS",
            "finished_at": _now(),
            "case_count": len(deterministic),
            "passed": passed,
            "failed": failed,
            "metrics": metrics_payload,
            "notes": (
                f"确定性 {len(deterministic)} 例（通过 {passed}）；"
                f"Live Smoke {live_summary['case_count']} 例（通过 {live_summary['passed']}）"
                + (f"；跳过 {len(skipped)} 例（需要 Jev）" if skipped else "")
            ),
        },
    )

    if write_baseline and not live:
        _write_baseline(
            "on" if resolved_jev else "off",
            {
                "benchmark_version": BENCHMARK_VERSION,
                "travelplan_commit": travelplan_commit(),
                "superharness_commit": superharness_commit(),
                "model": model_name(),
                "fixture_version": FIXTURE_VERSION,
                "config": current_config().public_dict(),
                "jev_enabled": resolved_jev,
                "suites": [s for s in selected if s != LIVE_SUITE],
                "case_count": len(deterministic),
                "passed": passed,
                "failed": failed,
                "metrics": combined,
                "generated_at": _now(),
            },
            baselines_dir=baselines_dir,
        )

    _write_results(resolved_id, results, metrics_payload, results_dir=results_dir)
    return store.get_benchmark_run(resolved_id) or {}


def result_case_id(case: Mapping[str, Any]) -> str:
    return str(case.get("case_id") or "")


def _write_results(
    benchmark_run_id: str,
    results: Sequence[dict[str, Any]],
    metrics: Mapping[str, Any],
    *,
    results_dir: Path | None = None,
) -> None:
    """把这一轮的结果写到 benchmark/results/<id>/。

    只写"能用来复盘"的内容：逐 case 的判定与失败原因、聚合指标。完整 plan 都在
    `outputs/<run_id>/` 与数据库里，不在这里重复一份（否则仓库会被产物撑爆）。
    """

    target = (results_dir or RESULTS_DIR) / benchmark_run_id
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
                "failures": summarize_failures(results),
                "metrics": metrics,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def make_measure(
    store: TravelPlanStore | None = None,
    *,
    suites: Sequence[str] = ("hard", "basic"),
    limit: int | None = None,
) -> Callable[[Mapping[str, str]], dict[str, float] | None]:
    """给 Evolution 用的评测入口：``measure(overrides) -> 扁平指标``。

    它开一个**临时库**跑：Evolution 的验证是"候选改动在回归用例上的表现"，
    不该把几十条对照 run 灌进生产库，污染管理端的运行列表。

    同一个 measure 复用同一份临时库（而不是每次调用新建）：一次 Evolution 会调用它
    2×cluster 次，反复建库既慢、在 Windows 上还会因为文件句柄没释放而删不掉临时目录
    （真实踩到过）。临时目录交给操作系统回收。
    调用方传进来的 store 只用来对齐磁盘位置，不使用它的连接。
    """

    scratch_dir = Path(tempfile.mkdtemp(prefix="tp-evolution-"))
    scratch = TravelPlanStore(db_path=scratch_dir / "evolution.db")

    def measure(overrides: Mapping[str, str]) -> dict[str, float] | None:
        env = {"JEV_ENABLED": "true", **{str(key): str(value) for key, value in overrides.items()}}
        cases: list[dict[str, Any]] = []
        for suite in suites:
            cases.extend(load_cases(suite, limit=limit))
        if not cases:
            return None
        with override_env(env):
            results = run_suites(cases, store=scratch, jev_enabled=True, live=False)
        metrics, _ = _combine_suites(results, list(suites))
        return {name: value for name, value in metrics.items() if value is not None}

    return measure


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m benchmark.runner", description="TravelPlan Benchmark v0.1")
    parser.add_argument("--suite", action="append", choices=list(SUITES), help="可重复；默认跑全部确定性套件")
    parser.add_argument("--jev", choices=["on", "off"], default="on", help="是否启用 Jev（决定写哪份 baseline）")
    parser.add_argument("--limit", type=int, default=None, help="每个套件最多跑几个 case（冒烟用）")
    parser.add_argument("--live", action="store_true", help="额外跑 Live Smoke（联网，会消耗真实额度）")
    parser.add_argument("--db", default=None, help="跑评测用的库路径（默认临时库，不碰生产库）")
    parser.add_argument("--no-baseline", action="store_true", help="不更新 baselines/")
    args = parser.parse_args(argv)

    import os

    from app.store import default_db_path

    # 默认用临时库：跑评测不该往生产库里灌几十条 run。要留证据就显式传 --db。
    db_path = Path(args.db) if args.db else Path(tempfile.mkdtemp(prefix="tp-bench-db-")) / "bench.db"
    store = TravelPlanStore(db_path=db_path)
    print(f"[benchmark] db={db_path if args.db else '(临时库)'} jev={args.jev} live={args.live}")

    run = run_benchmark(
        store=store,
        suites=args.suite,
        jev_enabled=args.jev == "on",
        limit=args.limit,
        live=args.live,
        write_baseline=not args.no_baseline,
    )
    metrics = run.get("metrics") or {}
    live_smoke = metrics.get("live_smoke") or {}
    live_count = int(live_smoke.get("case_count") or 0)
    # 确定性例数与 Live 例数必须分开报：只跑 `--suite live_smoke --live` 时确定性例数是 0，
    # 只打一行 "cases=0 passed=0" 会让人以为什么都没跑（实际跑了一条 8 分钟的真实链路）。
    print(
        f"[benchmark] {run['benchmark_run_id']} status={run['status']} "
        f"确定性 {run.get('case_count')} 例（通过 {run.get('passed')}，失败 {run.get('failed')}）"
        + (
            f"｜Live Smoke {live_count} 例（通过 {live_smoke.get('passed')}，失败 {live_smoke.get('failed')}）"
            if live_count
            else ""
        )
    )
    for dimension, values in (metrics.get("groups") or {}).items():
        shown = "，".join(
            f"{key}={value if value is not None else 'n/a'}" for key, value in list(values.items())[:4]
        )
        print(f"  {dimension:18s} {shown}")
    baseline = metrics.get("baseline_compare")
    if baseline:
        print(
            f"  Jev OFF→ON: quality_delta={baseline.get('quality_delta_with_jev')} "
            f"latency_delta_ms={baseline.get('latency_delta_with_jev')}"
        )
    if metrics.get("skipped"):
        print(f"  跳过（需要 Jev，本轮 Jev 关闭）：{'、'.join(metrics['skipped'])}")
    print(f"[benchmark] 生产库默认位置（本次未写入）：{default_db_path()}  env={os.environ.get('TRAVELPLAN_DB_PATH') or '(未设置)'}")
    failures = int(run.get("failed") or 0) + int(live_smoke.get("failed") or 0)
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
