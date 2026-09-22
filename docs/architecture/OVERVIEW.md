# 项目架构总览

一句话：这份文档说明 TravelPlan 由哪几层组成、每层的边界在哪、一次请求的数据怎么流动。

## 1. 顶层目录

```text
travelplan/
├─ app/                     业务代码（Python）
│  ├─ api.py                FastAPI 层：HTTP 形状 ↔ RunResult（ASGI 变量名 api）
│  ├─ agent.py              装配：把 super_harness 的 runtime 与本业务规划入口接起来
│  ├─ agent_runner.py       主规划入口：SuperHarness Agent Loop（execute_agent_run）
│  ├─ agent_trace.py        观测桥：Agent 每步（工具/模型）→ run_stages + trace_spans
│  ├─ travel_tools.py       旅行工具集：ProviderHub 方法 → langchain Tool（11 个）
│  ├─ prompts.py            Prompt 常量：主规划 / 城市预热 system prompt + 版本号，及其它调用点
│  ├─ sessions.py           Planning Session：引导式旅程的"规划前会话"状态机 + Prefetch
│  ├─ discovery.py          取数逻辑的**唯一实现**（Session Prefetch 与其调用方共用）
│  ├─ city_cache.py         跨会话「城市知识库」：攻略 + 高德 POI 的半静态缓存（按城市分片）
│  ├─ places.py             Canonical Place 实体层（跨会话地点身份）
│  ├─ recommendations.py    Guided 探索页的推荐数据组装
│  ├─ selection.py          用户选择层：策略→权重、Pace 推导、MUST/WANT/REJECT 解释
│  ├─ planner.py            旅行领域算法（纯函数、无网络、无随机）：排程 / 预算 / 可行性
│  ├─ providers.py          ProviderHub：能力调用、主备、缓存、审计账本
│  ├─ llm.py                通用 LLM 调用与降级（按用途超时、并发闸门）
│  ├─ models.py             Pydantic 领域模型
│  ├─ store.py              证据库（SQLite / Postgres 双后端）
│  ├─ db.py                 后端选择与 SQLite→Postgres 方言翻译
│  ├─ observability.py      Run/Span 状态与 span 分类
│  ├─ badcase.py            Bad Case 规则引擎
│  ├─ evolution.py          Evolution：Bad Case → 经验
│  ├─ preheat_agent.py      城市预热 Agent（一座城市内部由 Agent 决策）
│  ├─ preheat_tools.py      预热工具集（search_guides / save_city_* 等）
│  ├─ admin_timeline.py     Admin 时间线与步骤视图的数据组装
│  ├─ admin_analytics.py    Admin 概览/质量聚合
│  ├─ config.py             集中配置与可调阈值
│  ├─ redact.py             全仓唯一一份"哪些字符串不能出现在输出里"
│  ├─ version.py            版本信息（Trace / Baseline 都要记是哪一版跑的）
│  ├─ workflow.py           **旧固定 12 步图（已退役）**；产物写出 / 审计 / BadCase 等 helper 仍被复用
│  └─ decision/             Jev 客户端与软决策（**旧路径遗留**，主规划不再使用）
├─ benchmark/               评测套件 → 见 [Benchmark](../quality/BENCHMARK.md)
├─ frontend/                Next.js App Router：用户端 + 管理端
├─ super_harness/           git submodule：Runtime / Tool / MCP / Model / Trace
├─ tests/                   pytest（全部离线）
├─ docs/                    本文档
├─ main.py                  本地 CLI
├─ Dockerfile               API 容器镜像（含 Node，供 12306 MCP）
└─ requirements.txt
```

## 2. 分层与边界

```text
┌──────────────────────────────────────────────┐
│ 展示层  frontend/（用户端 + 管理端）           │  只做展示，不含业务判断，不持有任何 Secret
├──────────────────────────────────────────────┤
│ HTTP 层 app/api.py                            │  只做翻译：HTTP 形状 ↔ RunResult / 库里的事实
├──────────────────────────────────────────────┤
│ 准备层 app/sessions.py + app/discovery.py      │  引导式旅程：会话状态机 + 后台 Prefetch
├──────────────────────────────────────────────┤
│ 规划层 app/agent_runner.py + super_harness     │  Agent Loop：自己调工具、自己控制预算与时间
├──────────────────────────────────────────────┤
│ 领域层 app/planner.py + app/selection.py       │  纯函数：排程、预算、可行性、用户选择解释
├──────────────────────────────────────────────┤
│ 能力层 app/providers.py + app/travel_tools.py  │  真实价格/车次/路线/攻略从哪来、怎么给 Agent
├──────────────────────────────────────────────┤
│ 持久层 app/store.py                           │  run 的证据、决策、度量、BadCase、Session、Provider 账本
└──────────────────────────────────────────────┘
```

五条不能越界的规则：

1. **HTTP 层不许算东西**。`api.py` 不比较价格、不排行程；它只把 `RunResult` 与库里的行
   翻译成 JSON。任何"顺手算一下"都会让 CLI 与 Web 走两套逻辑。
2. **取数只有一份**。查交通/酒店/攻略/地点的实现在 `app/discovery.py`；规划路径也不另写
   Provider 访问，只经 `app/travel_tools.py` 包好的 `ProviderHub` 方法。
3. **规划算法只有一份**。`app/prompts.py::TRAVEL_PLANNER_SYSTEM_PROMPT` 就是主规划的算法；
   prompt 改动必须同时改 `TRAVEL_PLANNER_PROMPT_VERSION`，否则"哪一版更好"无法对比。
4. **领域层不许联网、不许调模型**。`planner.py` 里没有任何 HTTP 与 LLM 调用，所以它可以
   脱网单测；`selection.py` 是纯函数式的用户选择层，同样不联网。
5. **硬事实只能来自工具**。价格、时刻、车次、航班号、营业时间、路线都必须能指回一次真实
   工具调用；取不到就如实标 unknown，不许补编（这条写进 system prompt，也是审计的判定口径）。

## 3. 一次请求的数据流

两条入口汇聚到同一个执行体：

```text
Quick（一句话 / CLI）
用户（前端 / CLI / HTTP）
  → POST /api/v1/plans {message, background:true}   → 202 + run_id
  → api.create_plan()：建 run 行（status=RUNNING）→ 线程池执行 run_travel
  → app.agent.run_travel（唯一业务入口）
  → TravelWorkflowRunnable.invoke → app.agent_runner.execute_agent_run

Guided（引导式）
用户（前端向导）
  → POST /api/v1/planning-sessions        （建会话 + 后台 Discovery/Prefetch）
  → PATCH /api/v1/planning-sessions/{id}  （改偏好/POI，只重排序）
  → POST /api/v1/planning-sessions/{id}/start
  → app.sessions.start_run()：建 run 行（source=guided）→ 线程池执行 _start_job
  → app.agent_runner.execute_agent_run(intent=..., prefetch=..., source="guided")

执行体（两条入口共用）— SuperHarness Agent Loop
  → execute_agent_run
       ① create_run + 建 ProviderHub + build_step_reporter（观测桥）
       ② Discovery 过户（prefetch 非空时）：复用来的调用与证据写进本次 run 的 sources
       ③ create_harness_agent(tools = 11 个旅行工具 + submit_final_plan, 主/bk1/bk2 降级链)
       ④ Agent 循环：自己调工具、自己控制预算与时间 → submit_final_plan 交卷
       ⑤ Python 唯一兜底：_apply_time_pass（时间冲突验证）
       ⑥ 复用既有产物管线：plan.json / plan.md / audit_report.json / trace.jsonl /
          metrics.json / badcases.json，并落库
  → RunResult 落库并返回
用户侧轮询 GET /api/v1/plans/{run_id}/status（读 run_progress.current_stage 的中文步骤名）
用户侧读取 GET /api/v1/plans/{run_id}（plan + evidence）与 /audit（规划依据）
管理端读取 /api/v1/admin/runs/{run_id}（trace / stages / llm_calls / badcases / user_journey）
```

细节见 [主规划：Agent Loop](AGENT_LOOP.md)。

一次 run 的副作用清单（都可以事后查证）：

| 落点 | 内容 |
| --- | --- |
| `data/travelplan.db`（或 Neon） | 25 张表（以 `app/store.py` 的 `CREATE TABLE IF NOT EXISTS` 为准）：runs / trip_requests / sources / evidence / places / place_evidence / decisions / plans / plan_items / run_progress / run_stages / trace_spans / run_metrics / badcases / benchmark_runs / benchmark_case_results / evolution_runs / experiences / planning_sessions / city_pois / city_poi_mentions / city_evidences / city_cache_meta / provider_calls / runtime_config |
| `outputs/<run_id>/` | plan.json、plan.md、audit_report.json、trace.jsonl、metrics.json、badcases.json |
| stdout（SuperHarness） | 结构化运行日志（模型、工具调用、决策） |

`planning_sessions` 存引导式会话（含 `discovery_json` / `prefetch_json` / `events_json`），
`provider_calls` 是 Provider 调用账本（观测，无 runs 外键），`runtime_config` 是管理端可运行时
修改的非 Secret 配置；`city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta`
是目的地级共享的「城市知识库」（`app/city_cache.py` 写入并读取，**跨用户、跨会话**按城市分片复用，
含 POI、攻略提及、攻略正文与检索词）。这几类都不属于"一次 run 的证据"。

Agent 路径**不落** `places` / `evidence` 表：取数由 Agent 在循环里完成，Python 只看到它交回来的
plan。跨会话复用的攻略正文仍会经城市知识库落地并记 `sources.status=CACHED`。

## 4. 为什么主规划是 Agent Loop，而不是一张固定图

旧的固定 12 步图能回答"第 6 步哪些路线没核实"，代价是**每一步该查什么、查到什么程度**都被写死；
用户说"主要想吃""带老人别太累"这类要求只能在固定权重里找位置。改造后：

- **决定"做什么"的是 Agent**：它在循环里按需查火车/机票/酒店/门票/POI/路线/攻略，顺序不固定，
  查到什么程度由它自己判断；
- **Python 只保证"排得下"**：时间冲突验证与修订是唯一保留的兜底；
- **可观测性不降级**：每一步（工具名 / 状态 / 耗时 / 每次 token）写进 `run_stages.facts_json` 与
  `trace_spans`，前端轮询 `run_progress.current_stage` 显示中文步骤名，admin 有完整步骤视图；
- **可对比**：prompt 带版本号并写进 trace / audit / run_metrics，prompt 就是唯一算法。

代价与已知边界（如实列出）：

- **预算 / MUST / REJECT / 营业时间由 prompt 约束**，Python 不拦截 —— 违反时只能靠自查与
  Critic 式的 prompt 纪律，产物里可能留下 error 级告警（`plan.warnings` 会如实记录）；
- 循环被截断（超时 / 步数 / token）时会保留已交卷的行程并标降级；
- 旧的固定 12 步图与 Jev 决策层仍在代码里（`app/workflow.py`、`app/decision/`），
  但不在主路径上，仅作为产物写出与审计 helper 的复用来源。

## 5. 与 SuperHarness 的边界

| 谁 | 负责 |
| --- | --- |
| SuperHarness（submodule） | Runtime / Agent Loop（`create_harness_agent`）/ Tool 与 MCP 装载 / 模型接入 / 重试 / EventBus 与原生 Trace / 能力插件 |
| TravelPlan（本仓） | 旅行领域算法、主规划 prompt、旅行工具封装、会话与预取、证据库、BadCase/Evolution、评测、前端 |

本仓**不自制** Router / Plugin Loader / MCP Client / Memory / RAG / Retry，一律复用
SuperHarness；只在它之上补业务级的事实（步骤观测、prompt 版本、BadCase）。
唯一的"补丁"是经 SuperHarness 既有的 `middleware=` 注入口挂上本业务的三样东西：
步骤观测桥（`agent_trace`）、模型降级链（主 / bk1 / bk2）、工具可见性补全与成本护栏
（`_AlwaysVisibleTools` / `_TokenBudgetGuard`）。理由写在 `app/agent_runner.py` 模块 docstring。

详细的能力清单见 [PROVIDERS_TOOLS.md](PROVIDERS_TOOLS.md)。
