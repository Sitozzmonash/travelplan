# 05 Trace 与可观测性

一句话：说明一次 run 产生了哪些 trace、怎么命名、落在哪，以及管理端每个面板的数据从哪来。

## 1. 两个状态机

```text
Run  （run_progress.status）：RUNNING → SUCCESS / DEGRADED / FAILED / CANCELLED
Span （trace_spans.status） ：RUNNING → SUCCESS / WARNING / FAILED
```

- `RUNNING → SUCCESS|DEGRADED|FAILED|CANCELLED` 是**唯一**对外契约（前端轮询、管理端筛选用它）。
- `runs.status` 保留历史小写值（`completed` / `failed` / `needs_clarification`）只为兼容旧接口；
  管理端一律读 `run_progress.status`。
- `DEGRADED` = 产出了行程，但过程中有降级（模型/Provider/Jev 有失败）。

定义位置：`app/observability.py`（`RunStatus`、`StageStatus`、`SpanKind`、`span_id`）。

## 2. Span 分类与命名

`SpanKind`（`app/observability.py`）共 11 类，覆盖任务要求：

```text
workflow / llm / jev / provider / tool / mcp / planner / budget / feasibility / critic / store
```

命名规则（`observability.span_id`）：

| 类型 | span_id | 例子 |
| --- | --- | --- |
| workflow 节点 | `{run_id}:{stage_id}` | `tp-xxx:search_hotels` |
| 其他组件 | `{run_id}:{component}:{name}` | `tp-xxx:jev:plan_choice`、`tp-xxx:tool:search_poi` |

层级（parent_span_id）在真实的 run 里长这样：

```text
Run SUCCESS
├─ parse_intent SUCCESS                     （workflow）
├─ search_intercity_transport WARNING       （workflow）
│  ├─ railway_12306 SUCCESS                 （mcp）
│  │  └─ get-tickets SUCCESS                （tool）
│  └─ tuniu SUCCESS                         （provider）
│     └─ search_flights TIMEOUT             （tool）
├─ build_initial_plan SUCCESS               （workflow）
│  ├─ build_candidates SUCCESS              （planner）
│  ├─ plan_choice SUCCESS                   （jev）
│  └─ hard_constraint_recheck SUCCESS        （planner）
├─ check_budget SUCCESS                     （budget）
├─ check_feasibility SUCCESS                （feasibility）
├─ critic_and_revise WARNING                （workflow）
│  ├─ rule_critic SUCCESS                   （critic）
│  ├─ tradeoff SUCCESS                      （jev）
│  └─ quality_gate SUCCESS                  （jev）
└─ finalize SUCCESS                         （workflow）
   ├─ parse_intent / critic / final_answer …（llm，挂在对应节点下）
   └─ persist SUCCESS                       （store）
```

实现要点：

- workflow span 由 `execute_travel_run.progress_hook` 写（节点边界，同时更新 `run_stages`）；
- 其余 span 由 `_record_subspan` / `record_span` 写；
- provider / tool / mcp / llm span 在 `finalize` 之后由 `_emit_run_spans()` 从 ProviderHub 与
  LLM 的**调用账本**统一转录 —— 不要求每个调用点都记得埋点，也就不会漏；
- tool span 的 id 与 Bad Case 的 `trace_refs` 完全一致，所以从一条 Bad Case 能直接跳到那条 span。

## 3. 产物（`outputs/<run_id>/`）

只有**产出了 plan 的 run** 才会建这个目录（要求澄清或失败的 run 不建，避免出现"空产物目录"）。

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `plan.json` | 结构化行程（intent / transport / hotel / days / budget / sources / decisions） | `workflow._write_artifacts` |
| `plan.md` | 人读的行程说明（模型成文 + 代码生成的明细，失败则全代码生成） | 同上 |
| `audit_report.json` | 这次查了什么、谁给的、信不信、用了没有（含 provider_calls / llm_calls / jev_calls / timeline） | `workflow._build_audit` |
| `trace.jsonl` | 全部 span，一行一个 JSON | `workflow._write_run_artifacts` |
| `metrics.json` | run 级汇总（与 `run_metrics` 表同源） | 同上 |
| `badcases.json` | 规则检测出的 Bad Case（含按严重度的 summary） | 同上 |

`audit_report.json` 的 `artifacts[]` 是产物索引，新增的三件会被自动补进去。
下载接口 `GET /api/v1/plans/{run_id}/artifacts/{filename}` 用白名单，只允许这六个文件名。

## 4. 数据库侧

- `trace_spans`：`span_id / run_id / parent_span_id / component / name / status / started_at /
  finished_at / attributes_json / error`。完整 trace 走 JSONL，数据库负责查询、筛选、聚合与后台展示。
- `run_metrics`：run 级汇总，字段为
  `duration_ms / input_tokens / output_tokens / cached_tokens / total_tokens / llm_calls /
  jev_calls / tool_calls / provider_failures / badcase_count / cost`。
  **`cost` 拿不到可核实的价格回执时是 `null`**，不猜；token 同理（拿不到就 `null` 而不是 0）。
- `run_stages` / `run_progress`：轮询用的真实阶段状态。

## 5. 管理端怎么消费

| 面板 | 接口 | 数据来源 |
| --- | --- | --- |
| Dashboard 摘要 | `GET /api/v1/admin/overview` | runs + run_metrics 聚合 + badcases 计数 + Jev 健康 |
| 运行列表 | `GET /api/v1/admin/runs?status=&limit=&offset=` | `list_runs`（含 total） |
| 运行详情 | `GET /api/v1/admin/runs/{run_id}` | `{run, metrics, progress, trace, decisions, provider_calls, jev_calls, llm_calls, badcases}` |
| Trace 树 | 同上 | `trace[]`，前端按 `parent_span_id` 复原层级 |
| Jev Calls | 同上 | `jev_calls[]`（从 component=jev 的 span 还原，含 decision_type / input_summary / confidence / latency / fallback reason / quota） |
| LLM 调用 | 同上 | `llm_calls[]`（从 component=llm 的 span 还原） |
| Token 明细 | 同上 | `metrics` |
| Jev 健康 | `GET /api/v1/admin/jev/health` | 最近 20 个 run 的 jev span 汇总（quota 缺失时是 `unknown`） |
| Provider 统计 | `GET /api/v1/admin/providers` | 最近 50 个 run 的 tool span 汇总 |

Dashboard 默认只显示总量；LLM / Jev / Tool / Token 明细在运行详情里**默认折叠**。

## 6. 两条工程约定

1. **观测失败绝不影响规划**。`progress_hook`、`record_span`、`_write_run_artifacts`、Bad Case 落库
   全部包在 try/except 里：写库失败只是一条诊断记录缺失，不会把一次成功的规划打成失败。
2. **Trace 里没有 Secret**。审计账本只存 query/status/时长/条数；Prompt 全文与 API Key 都不进
   trace 与 audit（`app/llm.py:LLMResult.to_audit` 是白名单）。

## 7. 怎么排查一次"变慢/变差"

```text
1) 管理端 Dashboard：是整体变慢，还是某些 run 变慢？
2) 该 run 的 Trace 树：找 status=WARNING/FAILED 的 span，看 component
      provider/mcp/tool → 第三方或网络
      llm              → 模型侧（看 attributes.duration_ms 与 status）
      jev              → Jev 超时/低置信/fallback（看 fallback_reason）
      planner          → 候选数量、硬约束复核结果
3) 该 run 的 Bad Cases：规则已经把它们归类了（category + severity + trace_refs）
4) 需要跨 run 对比时：benchmark/results/<id>/summary.json 与 baselines/
```
