# 管理后台

管理端位于 `frontend/app/admin/`，后端接口位于 `app/api.py` 的 `/api/v1/admin/**`。
生产环境必须配置 `TRAVELPLAN_ADMIN_TOKEN`；不配置时管理接口不可用（一律 503）。

## 主要页面

| 页面 | 路由 | 看什么 |
| --- | --- | --- |
| Overview / Dashboard | `/admin` | 运行摘要、质量、Bad Case、Provider 与 Benchmark 信号 |
| Runs | `/admin/runs`、`/admin/runs/{id}` | 单次 run 的 **Agent 步骤轨迹**（每步工具 / 状态 / 耗时 / token）、Trace、LLM 调用、决策、Bad Case、用户旅程 |
| Sessions | `/admin/sessions` | Guided 会话、Discovery 分阶段状态与事件时间线 |
| Providers | `/admin/providers` | 数据源健康与调用账本（只读 `provider_calls`，不做实时探活） |
| Bad Cases | `/admin/badcases` | 规则检测出的失败记录与人工复核 |
| Benchmark | `/admin/benchmark` | 评测套件结果与基线对比 |
| Evolution | `/admin/evolution` | Bad Case → 经验（默认关闭，需 `EVOLUTION_ENABLED=true`） |
| Travel Quality / Funnel | `/admin/travel-quality`、`/admin/funnel` | 质量聚合与用户旅程漏斗 |
| Config | `/admin/config` | 可运行时调整的非 Secret 配置 |

## 与 Agent Loop 相关的三处口径

1. **Agent 步骤视图**：`GET /api/v1/admin/runs/{run_id}/timeline` 给出事件时间轴 + `agent` 块
   （按真实发生顺序排列的步骤与每步 token、整 run token 汇总、prompt 版本 / 交卷方式 /
   截断原因与上限）。固定 12 步的阶段码在 Agent 路径里**不存在**，事件按新口径归属；
   旧 run 仍按原来的规则转录。
2. **Jev 面板**：`jev_calls` 与 `/api/v1/admin/jev/health` 只对**旧固定流程的 run** 有内容；
   Agent 路径不产生 Jev 调用，面板为空是正常现象，不是故障。
3. **Provider Health**：只读 `provider_calls` 表，而主路径当前不写这张表 ——
   想知道 Agent 路径真实调了哪些数据源，看该 run 的
   `outputs/<run_id>/audit_report.json` 的 `provider_calls` 段。

指标口径、Trace 和 Provider 健康见 [OBSERVABILITY.md](../architecture/OBSERVABILITY.md)；
质量规则见 [BAD_CASE.md](../quality/BAD_CASE.md) 与 [BENCHMARK.md](../quality/BENCHMARK.md)。
