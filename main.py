"""本地 CLI（START.md §8.1 / PRD §29）。

    python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"

它和 FastAPI 走同一个入口 `app.agent.run_travel` —— CLI 与 Web 不各写一套逻辑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agent import PROJECT_ID, Route, create_travel_app, run_travel  # noqa: E402
from app.workflow import DEFAULT_OUTPUT_DIR  # noqa: E402

EXAMPLE = "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="TravelPlan 本地 CLI：真实数据源 + 固定 12 步规划流程。",
    )
    parser.add_argument("-m", "--message", required=True, help=f'自然语言需求，例如 "{EXAMPLE}"')
    parser.add_argument("--user-id", default="anonymous", help="长期记忆归属（默认 anonymous）")
    parser.add_argument("--thread-id", default=None, help="会话 ID；同 ID 复用 Checkpointer 状态")
    parser.add_argument("--project-id", default=PROJECT_ID, help=f"记忆项目隔离（默认 {PROJECT_ID}）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="产物目录（默认 outputs/）")
    parser.add_argument(
        "--route",
        default=Route.WORKFLOW,
        choices=[Route.WORKFLOW, Route.AGENT],
        help=f"{Route.WORKFLOW}=固定规划流程（默认）；{Route.AGENT}=Agent Loop 自由问答",
    )
    parser.add_argument("--debug", action="store_true", help="打印更多中间信息")
    return parser


def _configure_console() -> None:
    """把标准输出/错误切成 UTF-8。

    Windows 控制台默认是 GBK（cp936），而报告里有 ¥ 和可能来自数据源的任意字符；
    一旦编不出来 print 就抛 UnicodeEncodeError —— 进程带着 traceback 收场，用户看到的是
    "崩了"，但这次 run 其实已经成功落库、产物也已经写盘。errors="replace" 保证再遇到
    编不出的字符最多显示成 ?，不会把一次成功的规划报成失败。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 某些被包装的流不允许重配置，那就保持原样
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)

    if args.route == Route.AGENT:
        return _run_agent(args)

    app = create_travel_app(output_dir=args.output_dir, debug=args.debug)
    result = run_travel(
        args.message,
        user_id=args.user_id,
        thread_id=args.thread_id,
        project_id=args.project_id,
        route=Route.WORKFLOW,
        output_dir=args.output_dir,
        debug=args.debug,
        app=app,
    )
    return _report(result, args)


def _run_agent(args: argparse.Namespace) -> int:
    """Agent Loop 分支：返回的是 LangGraph messages state，不是 plan。"""
    app = create_travel_app(output_dir=args.output_dir, debug=args.debug)
    payload = {"messages": [{"role": "user", "content": args.message}]}
    config = {"configurable": {"thread_id": args.thread_id}} if args.thread_id else None
    from superharness import HarnessContext

    state = app.invoke(
        payload,
        config=config,
        context=HarnessContext(user_id=args.user_id, project_id=args.project_id),
        route=Route.AGENT,
    )
    messages = state.get("messages") if isinstance(state, dict) else None
    print(messages[-1].content if messages else state)
    return 0


def _report(result, args: argparse.Namespace) -> int:
    if result.status == "needs_clarification":
        print(f"需要补充信息：{result.clarification}")
        print(f"run_id: {result.run_id}")
        return 2

    if not result.ok or result.plan is None:
        print(f"规划失败：{result.error or '未知原因'}")
        print(f"run_id: {result.run_id}")
        return 1

    plan = result.plan
    print(f"run_id      : {plan.run_id}")
    print(f"状态        : {result.status}")
    print(f"行程        : {len(plan.days)} 天，{sum(len(day.items) for day in plan.days)} 个安排")
    budget = plan.budget
    if budget.budget_total is not None:
        print(
            f"预算        : 预计 ¥{budget.projected_total:,.0f} / 预算 ¥{budget.budget_total:,.0f}"
            f"（结余 ¥{(budget.remaining or 0):,.0f}，{budget.status}）"
        )
    else:
        print(f"预算        : 预计 ¥{budget.projected_total:,.0f}（用户未给预算）")

    if plan.transport and plan.transport.selected is not None:
        selected = plan.transport.selected
        label = getattr(selected, "label", "") or getattr(selected, "flight_no", "")
        print(f"大交通      : {label}")
        print(f"选择理由    : {plan.transport.selection_reason}")

    if plan.hotel and plan.hotel.selected is not None:
        print(f"住宿        : {plan.hotel.selected.name}")

    unresolved = [warning for warning in plan.warnings if not warning.resolved]
    if unresolved:
        print(f"未解决告警  : {len(unresolved)} 条")
        for warning in unresolved[:5]:
            print(f"  - [D{warning.day_index}] {warning.code}: {warning.reason}")

    if result.degradations:
        print(f"降级记录    : {len(result.degradations)} 条")
        for note in result.degradations[:5]:
            print(f"  - {note}")

    for filename, path in result.outputs.items():
        print(f"产物 {filename:18s}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
