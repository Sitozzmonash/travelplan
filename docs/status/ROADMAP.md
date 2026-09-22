# 路线图（未完成事项）

本页只记录仍未关闭、且已由代码或反馈确认的事项；已完成的复盘与方案保留在
[archive](../archive/) 或 [feedback](../../feedback/)。优先级不是发布日期承诺。

> 已关闭（2026-09）：**评测套件已迁到主路径**。`benchmark/` 现在评的是
> `app/agent_runner.py::execute_agent_run`：12 例离线确定性用例、五维指标、单份基线，
> 覆盖预算/**REJECT 不进行程**/时间冲突被修订/**每个数字能指回工具返回**/降级如实。
> 原 Six 维与 Jev OFF/ON 基线随旧固定流程一起废弃。见
> [Benchmark](../quality/BENCHMARK.md)。

## P1：旧流程与 Jev 代码清理

主规划已切到 Agent Loop，但 `app/workflow.py::execute_travel_run`（固定 12 步图）与
`app/decision/`（Jev 客户端与软决策）仍在代码里，只作为产物写出与审计 helper 的复用来源。
清理前要先确认产物管线（plan.json / audit / badcases / metrics）的 helper 归属，
不能把可复用的那部分一起删掉。

## P2：主路径的可观测性补强

- **Provider 调用账本**：Agent 路径当前不写 `provider_calls` 表，管理端 Provider Health
  看不到主路径的真实调用（只有 `audit_report.json` 里有）。需要把 ProviderHub 的调用账本
  在 run 结束时落库；
- **预算 / MUST / REJECT 只有 prompt 约束**：需要决定是否给"明确超预算""REJECT 点出现"
  这类硬违反加 Python 兜底或至少更醒目的产物标记；
- **prompt 可对比**：prompt 是唯一算法，但还没有"两份 prompt 在同一批用例上对比"的入口。

## P3：数据质量与运营补强

- 建立 Resolver Benchmark、错并/漏并 Bad Case 与低置信度人工审计入口；
- 设计确认后再做 alias 学习，不能把低置信度自动合并；
- TikHub 可用后重跑城市预热，补齐社媒证据；
- Dashboard 在 run 完成时持久化质量摘要，避免冷加载反复解析 `plan_json`。

来源：[A 线未完成事项](../../feedback/inbox/2026-09-22-A线未完成事项.md)、
[开发复盘](../../feedback/done/2026-09-development-review.md)。
