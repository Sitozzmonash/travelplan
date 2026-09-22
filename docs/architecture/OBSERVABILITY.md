# Trace 与可观测性

一句话：说明一次 run 产生了哪些事实、怎么命名、落在哪，以及管理端每个面板的数据从哪来。

主路径（Agent Loop）的观测重点与旧固定流程不同：**没有固定阶段码，步骤由 Agent 实际调用的
工具决定**。见 [主规划：Agent Loop](AGENT_LOOP.md) §6。

## 1. 三个状态机

```text
Run     （run_progress.status） ：RUNNING → SUCCESS / DEGRADED / FAILED / CANCELLED
Span    （trace_spans.status）  ：RUNNING → SUCCESS / WARNING / FAILED
                                  （旧固定流程的 run 还会出现 REUSED / SKIPPED）
Session （planning_sessions）   ：COLLECTING → DISCOVERING → READY → STARTING →（正式 run）
                                 └──────────────┴────────────┴──► CANCELLED / EXPIRED
```

- `RUNNING → SUCCESS|DEGRADED|FAILED|CANCELLED` 是**唯一**对外契约（前端轮询、管理端筛选用它）。
- `runs.status` 保留历史小写值（`completed` / `failed` / `needs_clarification`）只为兼容旧接口；
  管理端一律读 `run_progress.status`。
- `DEGRADED` = 产出了行程，但过程中有降级（模型/Provider 失败、循环被截断、时间冲突未解决）。
  Agent 路径的写法是：`runs.status` 仍 `completed`，另把 `run_progress.status` 提升为 `DEGRADED`。
- 旧固定流程会另外产出 `REUSED`（复用了 Discovery 结果、本次没有真的出网）与 `SKIPPED`
  （主动跳过的阶段，如用户已排除的点）两种 span 状态；Agent 路径不产生这两种状态。
- Session 状态与 Run 状态**刻意分开命名**，属于"正式 run 之前"的阶段，见
  [用户旅程](../product/USER_JOURNEY.md)。

定义位置：`app/observability.py`（`RunStatus`、`StageStatus`、`SpanKind`、`span_id`）、
`app/sessions.py`（`SESSION_*`）。

## 2. Agent 路径怎么被观测

```text
工具调用 → run_stages（stage_id = 中文展示名，如「在查酒店」；
                      facts_json = {tool, status, duration_ms, started, finished,
                                    seq, stage_key, query, note, error}）
         + trace_spans（component=tool，name = 工具名，attributes 带 tool/status/duration_ms/
                        seq/stage_key/query/note）
         + run_progress.current_stage  ← 前端轮询的就是它
模型调用 → trace_spans（component=llm，attributes 带 input_tokens / output_tokens /
                      cached_tokens / total_tokens / cumulative_*）
         + run_stages（stage_id =「在思考行程」）
一次性   → trace_spans（component=workflow，name=prompt_version，
                      attributes 带 prompt_version / system_prompt_chars / tools / agent）
```

约定：

- 同一个工具的第 N 次调用**共享同一行 stage**（主键 `run_id + stage_id`），最后一次覆盖 facts；
  **逐次调用的完整事实在 `trace_spans`**（span_id 带序号 `<run>:tool:<name>:2`，不会被顶掉）。
- `stage_id` 用中文展示名而不是工具名，是因为 `store.update_stage` 会把 `stage_id` 同时写进
  `run_progress.current_stage`，而前端要的就是「在查酒店」这类文案；机器可读的工具身份在
  `facts_json.tool` / `facts_json.stage_key`，工具调用的 span `name` 就是工具名。
- 展示名映射只有一份：`app/travel_tools.py::TOOL_DISPLAY_NAMES`；`app/agent_trace.py` 只做
  再导出，未命中时显示「在调用 <工具名>」（不猜一个像是有意义的文案）。
- 模型 span 的 token 键名与 `app/admin_timeline.py` 读的一致；`llm.finished` 事件里没有
  `total_tokens`，`app/agent_trace.py` 按 `input + output` 自己算。
- 观测失败绝不影响规划：所有落库调用都过保护，异常只记 debug 日志；工具/模型本身抛的异常
  原样向上抛（上报吞掉的只能是自己的错误）。

`SpanKind`（`app/observability.py`）共 11 类，是管理端分组展示的依据：

```text
workflow / llm / jev / provider / tool / mcp / planner / budget / feasibility / critic / store
```

命名规则（`observability.span_id`）：

| 类型 | span_id | 例子 |
| --- | --- | --- |
| workflow 节点 / 具名步骤 | `{run_id}:{stage_id}` | `tp-xxx:parse_intent`（旧流程）、`tp-xxx:agent_prompt` |
| 其他组件 | `{run_id}:{component}:{name}[:{suffix}]` | `tp-xxx:tool:search_poi:2` |

`{suffix}` 是可选的：同一 run 下同一组件的同名 span 会重复出现（同名 tool 被调用十几次、
多个 LLM tag），靠它区分实例，否则后写的会把先写的顶掉。
`jev` / `mcp` / `provider` 这些 component 只会出现在**旧固定流程的 run** 上
（agent 路径不产生 Jev 调用；provider / mcp 层的调用事实按 §4 的说明呈现）。

## 3. 产物（`outputs/<run_id>/`）

只有**产出了 plan 的 run** 才会建这个目录（失败的 run 会写一份失败产物，见下）。

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `plan.json` | 结构化行程（intent / transport / hotel / days / budget / sources / decisions） | `workflow._write_artifacts`（agent 路径复用） |
| `plan.md` | 人读的行程说明 | 同上 |
| `audit_report.json` | 这次查了什么、谁给的、信不信、用了没有（含 provider_calls / cached_evidence / llm_calls / **agent** / timeline） | `workflow._build_audit` |
| `trace.jsonl` | 全部 span，一行一个 JSON | `workflow._write_run_artifacts` |
| `metrics.json` | run 级汇总（与 `run_metrics` 表同源，含 `agent` 块） | 同上 |
| `badcases.json` | 规则检测出的 Bad Case（含按严重度的 summary） | 同上 |

`audit_report.json` 的 `artifacts[]` 是产物索引，新增的三件会被自动补进去。
下载接口 `GET /api/v1/plans/{run_id}/artifacts/{filename}` 用白名单，只允许这六个文件名。
失败的 run 由 `agent_runner._write_failure_artifacts` 写一份最小产物（保留已经发生的 trace 与
metrics），同样不含编造的行程。

`audit_report.json` 的 `provider_calls` 段来自 ProviderHub 的**调用账本**（本次可真出网的
事实），`cached_evidence` 段是跨会话复用城市知识库正文的记录（**不并入 provider_calls**，
避免"数据源调用次数"凭空变多）；对应的 `sources` 行状态是 `CACHED`，
`normalized_json` 里带 `{origin: "city_cache", city_cache_updated_at: ...}`。

## 4. 数据库侧

- `trace_spans`：`span_id / run_id / parent_span_id / component / name / status / started_at /
  finished_at / attributes_json / error`。完整 trace 走 JSONL，数据库负责查询、筛选、聚合与后台展示。
- `run_stages`：`stage_id / status / message / started_at / finished_at / facts_json`。
  Agent 路径里 stage_id 是中文展示名；旧流程里是节点名。
- `run_progress`：`status / current_stage / message / started_at / updated_at / finished_at / error`，
  **前端轮询的字段就是 `current_stage`**。
- `run_metrics`：run 级汇总，字段为
  `duration_ms / input_tokens / output_tokens / cached_tokens / total_tokens / llm_calls /
  jev_calls / tool_calls / provider_failures / badcase_count / cost / cost_source / agent`。
  - `jev_calls` 在 Agent 路径恒为 0（没有 Jev）；
  - `cost` 在没有模型单价或拿不到 token 时是 `null`，不猜；配了 `MODEL_PRICE_*` 且拿到 token
    时按用户单价估算，并标 `cost_source="user_price"`；
  - `agent` = `{prompt_version, degraded, plan_origin}`（`plan_origin` 见 §5 的交卷方式）。
- `provider_calls`：Provider 调用账本（观测，**无 runs 外键**），供 Provider Health 聚合；
  注意主路径当前不写这张表，见 [数据源与工具](PROVIDERS_TOOLS.md) §4 的已知缺口。
- `planning_sessions`：引导式会话（含 `discovery_json` / `prefetch_json` / `events_json`），
  与 `runs.source_session_id` 关联。
- `runtime_config`：管理端可运行时修改的非 Secret 配置覆盖（见 [配置说明](../operations/CONFIG.md) §3.5）。

## 5. 管理端怎么消费

| 面板 | 接口 | 数据来源 |
| --- | --- | --- |
| Dashboard 摘要 | `GET /api/v1/admin/overview` | runs + run_metrics 聚合 + badcases 计数 |
| 运行列表 | `GET /api/v1/admin/runs?status=&q=&include_benchmark=&limit=&offset=` | `list_runs`（含 total）；`q` 按 run_id / 原始需求搜索 |
| 运行详情 | `GET /api/v1/admin/runs/{run_id}` | `{run, metrics, user_journey, stages, progress, trace, decisions, provider_calls, jev_calls, llm_calls, badcases}` |
| Agent 步骤轨迹 | `GET /api/v1/admin/runs/{run_id}/timeline` | `app/admin_timeline.py`：事件时间轴 + `agent` 块（`steps` 按真实发生顺序、带每步 token；`tokens`；`meta` = prompt 版本 / 交卷方式 / truncation 与上限） |
| 阶段明细 | `GET /api/v1/admin/runs/{run_id}/stages` | 每阶段 steps / **facts**（工具名、状态、耗时）/ 该阶段的 tool_calls 与 llm_calls / tokens |
| Trace 树 | 运行详情 | `trace[]`，前端按 `parent_span_id` 复原层级 |
| LLM 调用 | 运行详情 | `llm_calls[]`（从 component=llm 的 span 还原，含每次 token） |
| Token / 成本 | 运行详情 | `metrics`（含 `cost` / `cost_source` / `agent`） |
| Jev Calls / Jev 健康 | 运行详情 / `GET /api/v1/admin/jev/health` | 从 component=jev 的 span 还原；**只对旧固定流程的 run 有内容**，Agent 路径为空 |
| Planning Sessions | `GET /api/v1/admin/planning-sessions?status=&destination=&has_run=&q=&limit=&offset=` | `planning_sessions` 列表（每行带 Discovery 状态 / MUST·WANT·REJECT 计数 / run_id） |
| Session 详情 | `GET /api/v1/admin/planning-sessions/{session_id}` | 信封 `{session, basic_info, preferences, preference_labels, discovery, prefetch_summary, poi_selections, events, run_link, decision}` |
| Provider Health | `GET /api/v1/admin/providers?limit_per_provider=` | `provider_calls` 聚合（每 Provider 一张卡 + `summary`；无历史 = UNKNOWN） |
| Provider 明细 | `GET /api/v1/admin/providers/{provider}?limit=` | 最近 N 次调用（含 source_type / session_id / run_id，query 已脱敏） |

Dashboard 默认只显示总量；LLM / Tool / Token / 用户旅程 / Session 事件明细都**默认折叠**。
Provider Health 只读 `provider_calls`，不做实时探活。

**交卷方式（`plan_origin`）**在详情里直接可读：`submit_final_plan`（正规交卷）与
`message_json`（未交卷、由最后一条回复里的 JSON 兜底解析）；后者一定伴随一条降级说明。

## 6. 两条工程约定

1. **观测失败绝不影响规划**。进度更新、span 写入、产物写出、Bad Case 落库全部包在保护里：
   写库失败只是一条诊断记录缺失，不会把一次成功的规划打成失败。
2. **Trace 里没有 Secret**。审计账本只存 query/status/时长/条数，且 preview 一律先脱敏再截断
   （`app/redact.py` 是全仓唯一一份"哪些字符串不能出现在输出里"）；Prompt 全文与 API Key 都不进
   trace 与 audit。

## 7. 怎么排查一次"变慢/变差"

```text
1) 管理端 Dashboard：是整体变慢，还是某些 run 变慢？
2) 该 run 的 Agent 步骤轨迹 / Trace 树：看哪一步最慢
      tool（component=tool）  → 第三方或网络；看 facts.error 与 duration_ms
      llm （component=llm）   → 模型侧；看每次 input/output tokens 与耗时
      workflow:prompt_version → 确认这次跑的是哪一版 prompt
   有没有 status=WARNING/FAILED 的 span，有没有 truncation（超时 / 步数 / token）
3) 该 run 的 Bad Cases：规则已经把它们归类了（category + severity + trace_refs）
4) 需要跨 run 对比时：benchmark/results/<id>/summary.json 与 baselines/
5) 想定位单次调用的真实数据源：outputs/<run_id>/audit_report.json 的 provider_calls 段
```
