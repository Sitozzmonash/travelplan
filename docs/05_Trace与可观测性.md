# 05 Trace 与可观测性

一句话：说明一次 run 产生了哪些 trace、怎么命名、落在哪，以及管理端每个面板的数据从哪来。

## 1. 三个状态机

```text
Run     （run_progress.status） ：RUNNING → SUCCESS / DEGRADED / FAILED / CANCELLED
Span    （trace_spans.status）  ：RUNNING → SUCCESS / WARNING / FAILED / REUSED / SKIPPED
Session （planning_sessions）   ：COLLECTING → DISCOVERING → READY → STARTING →（正式 run）
                                 └──────────────┴────────────┴──► CANCELLED / EXPIRED
```

- `RUNNING → SUCCESS|DEGRADED|FAILED|CANCELLED` 是**唯一**对外契约（前端轮询、管理端筛选用它）。
- `runs.status` 保留历史小写值（`completed` / `failed` / `needs_clarification`）只为兼容旧接口；
  管理端一律读 `run_progress.status`。
- `DEGRADED` = 产出了行程，但过程中有降级（模型/Provider/Jev 有失败）。
- `REUSED` 用于**复用了 Discovery 结果、本次没有真的出网**的 tool span（`app/workflow.py:4910`）；
  `SKIPPED` 用于 planner 主动跳过的节点（`app/workflow.py:438`，例如用户已排除或超过上限）。
- Session 状态与 Run 状态**刻意分开命名**，属于"正式 run 之前"的阶段，见
  [11_用户旅程与PlanningSession.md](11_用户旅程与PlanningSession.md)。

定义位置：`app/observability.py`（`RunStatus`、`StageStatus`、`SpanKind`、`span_id`）、
`app/sessions.py`（`SESSION_*`）。

## 2. Span 分类与命名

`SpanKind`（`app/observability.py`）共 11 类，覆盖任务要求：

```text
workflow / llm / jev / provider / tool / mcp / planner / budget / feasibility / critic / store
```

命名规则（`observability.span_id`）：

| 类型 | span_id | 例子 |
| --- | --- | --- |
| workflow 节点 | `{run_id}:{stage_id}` | `tp-xxx:search_hotels` |
| 其他组件 | `{run_id}:{component}:{name}[:{suffix}]` | `tp-xxx:jev:plan_choice`、`tp-xxx:tool:search_poi:src-1` |

`{suffix}` 是可选的（`app/observability.py:69-77`）：同一 run 下同一组件的同名 span 会重复出现
（例如同名 tool 被调用 16 次、多个 LLM tag），靠它区分实例，否则后写的会把先写的顶掉；
tool span 用的 suffix 是 `source_id`，provider / mcp 分组 span 不带 suffix。

层级（parent_span_id）在真实的 run 里长这样：

```text
Run SUCCESS
├─ parse_intent SUCCESS                     （workflow）
│  └─ parse_intent SUCCESS                  （llm）
├─ search_intercity_transport WARNING       （workflow）
│  ├─ railway_12306 SUCCESS                 （mcp）
│  │  └─ railway_12306_get-tickets SUCCESS  （tool）
│  └─ tuniu SUCCESS                         （provider）
│     └─ tuniu_search_flights TIMEOUT       （tool）
├─ build_initial_plan SUCCESS               （workflow）
│  ├─ build_candidates SUCCESS              （planner）
│  ├─ plan_choice SUCCESS                   （jev）
│  └─ hard_constraint_recheck SUCCESS        （planner）
├─ check_budget SUCCESS                     （budget）
├─ check_feasibility SUCCESS                （feasibility）
├─ critic_and_revise WARNING                （workflow）
│  ├─ rule_critic SUCCESS                   （critic）
│  ├─ tradeoff SUCCESS                      （jev）
│  ├─ quality_gate SUCCESS                  （jev）
│  └─ critic SUCCESS                        （llm）
└─ finalize SUCCESS                         （workflow）
   ├─ final_answer SUCCESS                  （llm）
   └─ persist SUCCESS                       （store）
```

示例里写的是**真实 tool 名**：`PROVIDER_STAGE_MAP`（`app/workflow.py:4694-4706`）只认抽象名
`search_trains` / `search_flights` / `search_hotels`，它们对应的真实 tool 名分别是
`railway_12306_get-tickets`、`tuniu_search_flights`、`tuniu_search_hotels`。
provider / mcp 分组 span 命名是 `{run}:provider:{provider}`（12306 走 MCP，即 `{run}:mcp:{server}`），
**没有 suffix**，而 tool span 是 `{run}:tool:{tool}[:{source_id}]`。

LLM span 按 `LLM_STAGE_MAP`（`app/workflow.py:4712-4718`）挂在**对应节点**下，不是都挂在 `finalize` 下：
`parse_intent` → `{run}:parse_intent`、`critic` → `{run}:critic_and_revise`、
`final_answer` → `{run}:finalize`。

实现要点：

- workflow span 由 `execute_travel_run.progress_hook` 写（节点边界，同时更新 `run_stages`）；
- 其余 span 由 `_record_subspan` / `record_span` 写；
- provider / tool / mcp / llm span 在 `finalize` 之后由 `_emit_run_spans()` 从 ProviderHub 与
  LLM 的**调用账本**统一转录 —— 不要求每个调用点都记得埋点，也就不会漏；
- Bad Case 的 `trace_refs` 多数是真实 span_id（workflow 节点 `{run}:{stage_id}`、Jev 决策
  `{run}:jev:{name}`），管理端按 span_id 精确匹配就能跳转；但 `provider_failure` 写的是
  `{run}:provider:{provider}:{tool}`（`app/badcase.py:164`），真实 span 只有分组
  `{run}:provider:{provider}` 与 tool `{run}:tool:{tool}[:{source_id}]` 两种，所以这类 ref
  永远匹配不上、会显示 `missing`（`app/api.py:936-943` 只做精确匹配，不做前缀回退）。
- 引导式复用 Discovery 候选时，`node_extract_places` 会写一条 `planner` 子 span
  `{run_id}:planner:user_place_selection`（attributes: reused/kept/must/want/rejected），
  让"用户选择到底排除了几个点"在 trace 里可查。

## 3. 产物（`outputs/<run_id>/`）

只有**产出了 plan 的 run** 才会建这个目录（要求澄清或失败的 run 不建，避免出现"空产物目录"）。

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `plan.json` | 结构化行程（intent / transport / hotel / days / budget / sources / decisions） | `workflow._write_artifacts` |
| `plan.md` | 人读的行程说明（模型成文 + 代码生成的明细，失败则全代码生成） | 同上 |
| `audit_report.json` | 这次查了什么、谁给的、信不信、用了没有（含 provider_calls / **cached_evidence** / llm_calls / jev_calls / timeline） | `workflow._build_audit` |
| `trace.jsonl` | 全部 span，一行一个 JSON | `workflow._write_run_artifacts` |
| `metrics.json` | run 级汇总（与 `run_metrics` 表同源） | 同上 |
| `badcases.json` | 规则检测出的 Bad Case（含按严重度的 summary） | 同上 |

`audit_report.json` 的 `artifacts[]` 是产物索引，新增的三件会被自动补进去。
下载接口 `GET /api/v1/plans/{run_id}/artifacts/{filename}` 用白名单，只允许这六个文件名。

**`cached_evidence` 段**（跨会话复用攻略正文时才有）：数组，每项是
`{source_id, evidence_id, provider, source_type, source_url, title, fetched_at,
city_cache_updated_at, chars}`。它**不并入 `provider_calls`**：复用的正文本次并没有出网，
并进去会让"数据源调用次数"与 Provider Health 看起来凭空多了几次调用。
对应的 `sources` 行状态是 `CACHED`（`app/workflow.py:STATUS_CACHED`），
`normalized_json` 里带 `{origin: "city_cache", city_cache_updated_at: ...}`。

## 4. 数据库侧

- `trace_spans`：`span_id / run_id / parent_span_id / component / name / status / started_at /
  finished_at / attributes_json / error`。完整 trace 走 JSONL，数据库负责查询、筛选、聚合与后台展示。
- `run_metrics`：run 级汇总，字段为
  `duration_ms / input_tokens / output_tokens / cached_tokens / total_tokens / llm_calls /
  jev_calls / tool_calls / provider_failures / badcase_count / cost`。
  **`cost` 在没有模型单价或拿不到 token 时是 `null`**，不猜；配了 `MODEL_PRICE_*` 且拿到 token 时
  按用户单价估算，并标 `cost_source="user_price"`（token 同理：拿不到就 `null` 而不是 0）。
- `run_stages` / `run_progress`：轮询用的真实阶段状态。
- `provider_calls`：Provider 调用账本（观测，**无 runs 外键**），供 Provider Health 聚合；见
  [12_Provider健康与观测.md](12_Provider健康与观测.md)。
- `planning_sessions`：引导式会话（含 `discovery_json` / `prefetch_json` / `events_json`），
  与 `runs.source_session_id` 关联；见 [11](11_用户旅程与PlanningSession.md)。
- `runtime_config`：管理端可运行时修改的非 Secret 配置覆盖（见 [02](02_配置说明.md) §3.5）。

## 5. 管理端怎么消费

| 面板 | 接口 | 数据来源 |
| --- | --- | --- |
| Dashboard 摘要 | `GET /api/v1/admin/overview` | runs + run_metrics 聚合 + badcases 计数 + Jev 健康 |
| 运行列表 | `GET /api/v1/admin/runs?status=&q=&include_benchmark=&limit=&offset=` | `list_runs`（含 total）；`q` 按 run_id / 原始需求搜索；`include_benchmark` 默认 false |
| 运行详情 | `GET /api/v1/admin/runs/{run_id}` | `{run, metrics, user_journey, stages, progress, trace, decisions, provider_calls, jev_calls, llm_calls, badcases}` |
| 用户旅程上下文 | 同上 | `user_journey`（source / source_session_id / 策略 / MUST·WANT·REJECT 计数 / prefetch_reused / discovery）默认折叠 |
| 阶段明细 | `GET /api/v1/admin/runs/{run_id}/stages` | 每阶段 steps / facts / 该阶段的 tool_calls 与 llm_calls / tokens |
| Trace 树 | 运行详情 | `trace[]`，前端按 `parent_span_id` 复原层级 |
| Jev Calls | 运行详情 | `jev_calls[]`（从 component=jev 的 span 还原，含 decision_type / input_summary / confidence / latency / fallback reason / quota） |
| LLM 调用 | 运行详情 | `llm_calls[]`（从 component=llm 的 span 还原） |
| Token / 成本 | 运行详情 | `metrics`（含 `cost` / `cost_source` / `cost_breakdown`） |
| Jev 健康 | `GET /api/v1/admin/jev/health` | 最近 20 个 run 的 jev span 汇总（quota 缺失时是 `unknown`） |
| Planning Sessions | `GET /api/v1/admin/planning-sessions?status=&destination=&has_run=&q=&limit=&offset=` | `planning_sessions` 列表（每行带 Discovery 状态 / MUST·WANT·REJECT 计数 / run_id） |
| Session 详情 | `GET /api/v1/admin/planning-sessions/{session_id}` | 信封 `{session, basic_info, preferences, preference_labels, discovery, prefetch_summary, poi_selections, events, run_link, decision（profile / hotel_areas / poi_pools）}` |
| Provider Health | `GET /api/v1/admin/providers?limit_per_provider=` | `provider_calls` 聚合（每 Provider 一张卡 + `summary`；无历史 = UNKNOWN） |
| Provider 明细 | `GET /api/v1/admin/providers/{provider}?limit=` | 最近 N 次调用（含 source_type / session_id / run_id，query 已脱敏） |

Dashboard 默认只显示总量；LLM / Jev / Tool / Token / 用户旅程 / Session 事件明细都**默认折叠**。
Provider Health 只读 `provider_calls`，不做实时探活（见 [12](12_Provider健康与观测.md)）。

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
