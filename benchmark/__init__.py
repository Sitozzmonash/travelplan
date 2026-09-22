"""TravelPlan Benchmark：面向 **Agent Loop 主路径** 的最小离线评测套件。

它评测的对象只有一个：
`app/agent_runner.py::execute_agent_run` —— Agent 自己在循环里调
`app/travel_tools.py::build_travel_tools(hub)` 的工具、最后用 `submit_final_plan` 交卷。

三条口径（改这个包之前先读）
--------------------------
1. **离线 + 确定性**。假 ProviderHub 返回固定的世界（车次/酒店/POI/路线），脚本模型按
   预设剧本发出工具调用与交卷参数。不联网、不花任何第三方额度、可重复。
   `benchmark/` 被 `.dockerignore` 排除，是开发期资产，不是服务运行时依赖。
2. **不给"总分"**。指标按维度分组（硬约束 / 行程质量 / 证据 / Provider / 性能），因为
   聚合分会掩盖"具体哪一维退化"。本路径没有 Jev，所以**没有 jev 维度** —— 不造一个
   假的维度来凑数（管理端会把缺失的维度显式标出来）。
3. **不对模型打分**。判分只回答"客观事实对不对"：天/项是否非空、行程里的数字能不能
   指回假工具真的返回过的东西、用户 REJECT 的点有没有混进来、时间冲突有没有被修订、
   降级说明是否与事实一致。

入口：
    python -m benchmark                  # CLI（见 benchmark/runner.py 的 --help）
    benchmark.runner.run_benchmark(...)  # 函数入口（管理端与 Evolution 都用它）
"""

from __future__ import annotations

__all__: list[str] = []
