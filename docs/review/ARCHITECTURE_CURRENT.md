# 当前架构（不是理想架构）

> 本文记录 **TravelPlan 当前代码实际上怎么运行**，所有路径与函数名均可追溯到
> 真实文件。未实现的能力一律写「当前未实现」，不画理想图。
> 对应代码：`main.py`、`app/*.py`、`frontend/`、`super_harness/`（submodule，只读分析）。

## 1. 一句话概括

TravelPlan 有**两条 Runtime 路径**：

- **固定 12 步规划流程**（主路径，CLI 与 FastAPI 默认）：`run_travel()` → `HybridRunner.invoke(route=WORKFLOW)` → `TravelWorkflowRunnable` → `execute_travel_run()` → 一张 LangGraph `StateGraph`。决定「做什么」的是代码，LLM 只做自然语言 → 结构化输入、以及最后把结果讲成人话。
- **SuperHarness Agent Loop**（`route="agent"`）：处理自由追问，才真正走 Router / Plugin / MCP / Memory 全链路。

两条路径**不是同一套 Runtime 内部实现**：固定流程复用 SuperHarness 的装配（Router 打分、Checkpointer、Observability 上报口），但其编排由 TravelPlan 自己的 LangGraph 完成；Agent Loop 才是 SuperHarness 的原生 Agent 运行时。见 §4、§7-B。

## 2. 目录职责

每个条目：职责 / 主要入口 / 主要依赖 / 不应该负责什么。

### main.py（149 行）

- **职责**：CLI 入口。解析参数，调 `run_travel()`，把 `RunResult` 落成终端输出。
- **入口**：`main.py` 里的 `--message/--user-id/--route/--output-dir/--debug` 解析。
- **依赖**：`app.agent.run_travel`。
- **不该负责**：任何业务逻辑、任何 Provider/LLM 调用。它只是「把命令行连到 run_travel」。

### app/agent.py（288 行）

- **职责**：装配层，只把 SuperHarness 装起来，不写业务算法。
- **入口与关键符号**：
  - `TravelWorkflowRunnable`（`app/agent.py:116`）：把固定流程包成 HybridRunner 能调的 runnable，只做输入形状适配（messages → query）与结果透传。
  - `travel_workflow_spec()`（`app/agent.py:159`）：`WorkflowSpec(name, description, runnable)`，`description` 供 Workflow Router 用 BM25 打分。
  - `create_travel_app()`（`app/agent.py:206`）：调 `create_harness_app(workflows=[spec], system_prompt=…, mcp_servers=default_mcp_servers(), model=…, checkpointer=…)`。
  - `_Emitter`（`app/agent.py:172`）：把 `HybridRunner.emit` 延后回填给先构造好的 runnable。
  - `run_travel()`（`app/agent.py:251`）：CLI 与 FastAPI 共用的唯一业务入口。
- **依赖**：`superharness`（`HarnessContext` / `create_harness_app` / `create_dev_checkpointer` / `WorkflowSpec`）、`app.workflow`、`app.prompts`、`app.providers.default_mcp_servers`。
- **不该负责**：规划算法、Provider 归一化、买卖任何数据。

### app/workflow.py（2748 行）

- **职责**：固定 12 步规划流程本身 —— LangGraph 节点、状态、预算/可行性/诚实性告警、最终产物组装。
- **入口**：`build_travel_graph()`（`app/workflow.py:2550`）、`execute_travel_run()`（`app/workflow.py:2626`）、`plan_payload()`（`app/workflow.py:2722`）。
- **12 个节点**（`build_travel_graph`，见下表）。
- **依赖**：`app.planner`、`app.providers.ProviderHub`、`app.llm.LLM`、`app.store`、`app.models`。
- **不该负责**：外部数据的具体抓取（那是 ProviderHub）、模型调用细节（那是 LLM wrapper）、整齐的对象建模（那是 models.py）。

| 节点（函数） | 做什么 | 位置 |
|---|---|---|
| `parse_intent` | 解析城市/日期/天数/人数/预算/偏好；唯一条件分支：没目的地就 END | `app/workflow.py:898` |
| `search_intercity_transport` | 去/回机票+火车，比价选优 | `:977` |
| `search_hotels` | 酒店检索与择优 | `:1132` |
| `search_social_guides` | 小红书/抖音/网页攻略 | `:1224` |
| `extract_and_normalize_places` | 从攻略抽地点，去重归一 | `:1358` |
| `verify_poi_and_routes` | 高德核对 POI、算市内路线 | `:1487` |
| `score_candidates` | Trust / Ad Risk 打分 | `:1638` |
| `build_initial_plan` | 生成逐日行程 | `:1758` |
| `check_budget` | 预算数学与口径标注 | `:1863` |
| `check_feasibility` | 时间/营业时间/抵达日/返程可行性 + 诚实告警 | `:1910` |
| `critic_and_revise` | Critic 决策（KEEP/REJECT/REVISION） | `:2086` |
| `finalize` | 组装 plan.json / plan.md / audit_report.json | `:2369` |

### app/providers.py（1727 行）

- **职责**：TravelPlan 访问所有外部数据的唯一入口 `ProviderHub`，把 SuperHarness 的 Plugin/Tool/MCP 能力统一成「带 provenance 的调用」（每次调用产出 `ProviderCall`，含 status/fetched_at/error/fallback 链）。
- **入口与关键符号**：`ProviderHub`（`:578`）、`PluginToolSet`（`:423`）、`LocalToolSet`（`:485`）、`MCPServerHandle`（`:505`）、`_AsyncRuntime`（`:255`）、`ProviderCall`（`:292`）、`PLUGIN_TOOLS`（`:547`）。
- **依赖**：`superharness` 的 `PluginLoader` / `ToolLoader` / `MCPLoader` / `MCPServerSpec`，`app.models`。
- **不该负责**：旅行业务语义（比如「回程跨天」这类判断在 workflow/planner）。ProviderHub 只返回「数据 + provenance」。

### app/llm.py（417 行）

- **职责**：对 `superharness` 的 `create_model` 做 TravelPlan 侧的薄封装：timeout、JSON 宽松解析、规则兜底、usage 统计、audit 留痕、观测上报。
- **入口与关键符号**：`LLM`（`:172`）、`LLMResult`（`:60`）、`invoke`（`:274`）、`invoke_json`（`:361`）、`_parse_json_loose`（`:383`）。
- **依赖**：`superharness` 的 `create_model`。
- **不该负责**：Prompt 文案（在 `app/prompts.py`）；也不该替代 SuperHarness 的 Model Middleware（见 §7-D 的职责重叠分析）。

### app/planner.py（3159 行）

- **职责**：纯旅行领域算法 —— 去重、Trust/Ad Risk、比价与门到门、预算、时间窗与营业时间约束、折返、行程生成与修订。
- **入口（节标题/函数名可 grep）**：`trust_score`、`ad_risk`、`detect_backtracking`、`apply_feasibility_pass`、`revise_day`、`arrival_day_index` / `arrival_day_start_minutes` / `arrival_ready_minutes` / `departure_deadline_minutes`、`_trim_to_window`。
- **依赖**：`app.models`、`dataclasses` 与常量。**不 import ProviderHub、不 import LLM**（保持纯函数、可单测）。
- **不该负责**：任何 I/O。planner 视 I/O 为外部输入。

### app/models.py（1250 行）

- **职责**：所有领域对象与 Pydantic/dataclass 定义（`TripIntent`、`FlightOption`、`TrainOption`、`HotelOption`、`Place`、`Evidence`、`Decision`、`TripPlan`、`FeasibilityIssue`、`utcnow()` 等）。
- **不该负责**：算法、I/O。

### app/store.py（536 行）

- **职责**：SQLite 证据库。表：`runs / trip_requests / sources / evidence / places / place_evidence / decisions / plans / plan_items`（`app/store.py:47-155`）。
- **不该负责**：业务判断；它只是「把对象写进去、按 run_id 读出来」。

### app/api.py（285 行）

- **职责**：FastAPI 接口（`app.api:api`）。
- **端点**：`GET /api/v1/health`（`:126`）、`POST /api/v1/plans`（`:161`，同步返回，最长 20 分钟）、`GET /api/v1/plans/{run_id}`（`:202`）、`GET /api/v1/plans/{run_id}/audit`（`:218`）、`POST /api/v1/plans/{run_id}/revise`（`:238`，v0 占位）、`GET /api/v1/plans/{run_id}/artifacts/{filename}`（`:271`）。
- **不该负责**：规划逻辑（委托 `run_travel`）、前端渲染。

### frontend/（Next.js 16 App Router + React 19 + Tailwind 4）

- **职责**：纯展示层。读 FastAPI 返回的 `plan.json`，渲染日程/地图/预算/来源抽屉/可行性告警。持有 zero Provider Secret。
- **关键**：`frontend/app/plan/[id]/page.tsx`、`frontend/components/*`、`frontend/lib/api.ts`。含 mock 模式（`frontend/lib/mock/*`，由 `NEXT_PUBLIC_USE_MOCK_API` 控制）。
- **不该负责**：业务判断、自己算价格、自己生成计划、直接调途牛/高德/TikHub。

### super_harness/（Git submodule，指针 `2c2c51a3`）

- **职责**：SuperHarness 运行时与通用能力。TravelPlan 复用：`create_harness_app`（HybridRunner）、`WorkflowSpec`、`PluginLoader` / `ToolLoader` / `MCPLoader`、`create_model`、Observability（`EventBus` / `ConsoleRenderer` / `TraceCollector` / `ObservabilityMiddleware`）。
- **TravelPlan 业务代码绝不能**：复制它的 Runtime、或往里塞旅行业务判断。

## 3. 当前真实调用链

### 3.1 固定 12 步规划流程（主路径）

```text
CLI (main.py) 或 FastAPI (app/api.py)
↓  run_travel(query, route=Route.WORKFLOW)
HybridRunner.invoke(payload, route=…, context=HarnessContext)        # superharness
↓  WorkflowRouter 命中 travelplan_workflow
TravelWorkflowRunnable.invoke / ainvoke (app/agent.py:133,147)
↓  extract_query + asyncio.to_thread
execute_travel_run(query, …)          (app/workflow.py:2626)
↓
LangGraph StateGraph(TravelState)     (app/workflow.py:2550)
  12 个节点串行（唯一分支在 parse_intent）
↓
ProviderHub       # 外部数据 + provenance（app/providers.py:578）
LLM               # 结构化 + 讲人话（app/llm.py:172）
planner           # 纯算法（app/planner.py）
↓
Store.save_*      # 落 SQLite（app/store.py）
node_finalize     # 写 outputs/<run_id>/{plan.json, plan.md, audit_report.json}
↓ 返回 RunResult → CLI 打印 / API 返回 JSON
```

### 3.2 Agent Loop（`route="agent"`）

```text
CLI --route agent
↓
HybridRunner (SuperHarness 原生 Agent Loop)
↓
Capability Router → Tool / Skill / MCP / Memory / Retry / Checkpointer
↓
ObservabilityMiddleware：before_agent 建 trace_id，wrap_model_call / wrap_tool_call 打点
```

### 3.3 两条路径不是完全相同的 Runtime

- 固定流程：编排是 TravelPlan 自己的 `build_travel_graph()`（LangGraph `StateGraph`），但**挂在 HybridRunner 下**，复用了它的 Router 评分、Checkpointer（thread_id）、以及 `emit` 上报口。
- Agent Loop：编排是 SuperHarness 的 LangGraph Agent，TravelPlan 只提供 `TRAVEL_AGENT_SYSTEM_PROMPT` 与 MCP 清单。
- 直接后果：固定流程**没有 trace_id**（`ObservabilityMiddleware.before_agent` 只在 Agent 生命周期里建 trace_id），所以原生 `TraceCollector` 不给它生成 span 树 —— 见 §7-B。

## 4. SuperHarness / TravelPlan 边界表

| 能力 | 当前实现位置 | 理想归属 | 当前是否合理 | 说明 |
|---|---|---|---|---|
| Capability Router | SuperHarness（HybridRunner 内） | SuperHarness | 合理 | 固定流程也借它做 route 打分 |
| Plugin Loader | SuperHarness `PluginLoader` | SuperHarness | 合理 | TravelPlan 只包了 `PluginToolSet` 建索引 |
| MCP Loader | SuperHarness `MCPLoader` | SuperHarness | 合理 | TravelPlan 只包了懒连接句柄 `MCPServerHandle` |
| LLM Model Factory | SuperHarness `create_model` | SuperHarness | 合理 | TravelPlan 的 `LLM` 只是外层 wrapper |
| Trace | SuperHarness Observability | SuperHarness | 合理 | 但固定流程拿不到 trace_id，见 §7-B |
| ProviderHub | TravelPlan | 待 Review | 见 §5 | 外部数据唯一入口 + provenance + fallback |
| PluginToolSet | TravelPlan | 待 Review | 见 §5 | 工具名 → (plugin, tool) 索引 |
| LocalToolSet | TravelPlan | 待 Review | 见 §5 | 目前只用 web_search |
| MCPServerHandle | TravelPlan | 待 Review | 见 §5 | MCP 懒连接 + 常驻 loop |
| `_AsyncRuntime` | TravelPlan | 待 Review | 见 §5 | 守护线程 + 常驻事件循环 |
| `app.llm.LLM` | TravelPlan | 待 Review | 见 §5 | timeout/JSON/fallback/audit wrapper |
| Travel Planner | TravelPlan | TravelPlan | 合理 | 领域核心 |
| Budget | TravelPlan | TravelPlan | 合理 | 领域 |
| Feasibility | TravelPlan | TravelPlan | 合理 | 领域 |
| Evidence Store | TravelPlan | TravelPlan | 合理 | SQLite 证据库 |

## 5. 重点解释 6 个对象

> 结论原则：**不下「必须删除」的结论**，只讲「为什么存在 / 复用了什么 / 自己扛了什么 / 是否开始接近 Runtime / 迁移要抽什么」。

### 5.1 ProviderHub（app/providers.py:578）

1. **为什么需要**：所有节点都要拿外部数据，且每条数据必须带 `fetched_at` + 状态 + 回退链。若各节点各写一套，审计口径必然碎掉。
2. **复用了 SuperHarness 的什么**：`PluginLoader` / `ToolLoader` / `MCPLoader`、`MCPServerSpec`（`create_model` 是 LLM 层的事，不在 ProviderHub 里）。
3. **自己承担**：`PLUGIN_TOOLS` 声明、结果归一化（`ProviderCall` / `ProviderResult`）、缓存（同 run 内 `_cache_key`）、回退链组织（12306 fail → tuniu）、`_record` 落库、`_emit_tool` 上报。
4. **是否接近 Runtime**：**部分接近**。它有「call → record → emit → 落库」一条自建行链路，和 SuperHarness 的 `wrap_tool_call` 中间件职责有重叠。
5. **若要迁回 SuperHarness**：需要抽「tool invoke 的 provenance 信封」这个通用概念 —— 即 `ProviderCall(status/fetched_at/error/chain)` 与回退链，应是 Runtime 级概念，而不是旅行领域概念。
6. **留在 TravelPlan 的理由**：回退优先级、工具名白名单（`PLUGIN_TOOLS`）、业务层面「火车→航班」语义是旅行而定。可拆：通用信封上移，业务选择留下。

### 5.2 PluginToolSet（app/providers.py:423）

1. **为什么需要**：`PluginLoader.load_tools(manifest)` 需要 manifest，而调用方只想要 `tuniu_search_flights` 这个工具名。
2. **复用**：`PluginLoader`（懒加载语义不变）。
3. **自己承担**：工具名 → (plugin, tool) 索引、`only=` 白名单过滤、按需 import。
4. **是否接近 Runtime**：否，只是薄索引层。
5. **迁移**：若 SuperHarness 提供 `loader.get_tool_by_name()`，这层可消失。
6. **保留理由**：目前 SuperHarness 的 Loader 没有按名查工具的能力，这层是贴着缺口补的。

### 5.3 LocalToolSet（app/providers.py:485）

1. **为什么需要**：`web_search`（Tavily）走 SuperHarness 的 Local Tool 目录。
2. **复用**：`ToolLoader`。
3. **自己承担**：一次加载、按名查。
4. **接近 Runtime**：否。
5./6. 同上，价值极低、几乎可以并进 PluginToolSet 的抽象，但当前独立存在只为「Plugin 目录 vs Tool 目录」的区别。

### 5.4 MCPServerHandle（app/providers.py:505）

1. **为什么需要**：12306 MCP 是 stdio + npx 拉起，不能每次调用都起一个新进程；而且 `MCPLoader` 缓存 `MultiServerMCPClient`，其内部 loop 必须常驻。
2. **复用**：`MCPLoader`、`MCPServerSpec`。
3. **自己承担**：懒连接（`tools()` 第一次才 connect）、`spec` 纯声明。
4. **接近 Runtime**：否，是「连接句柄」。
5. **迁移**：若 SuperHarness 提供「按名拿 MCP Server 且自带常驻 loop」，本类可消失。
6. **保留理由**：SuperHarness 的 MCPLoader 不管理 loop 生命周期。

### 5.5 _AsyncRuntime（app/providers.py:255）

1. **为什么需要**：`MCPLoader` 缓存的 `MultiServerMCPClient`（anyio task group / stdio 子进程 / httpx client）绑定在创建它的 loop 上；`asyncio.run` 每次建新 loop，第二次就 "attached to a different loop"。
2. **复用**：没有，这是 TravelPlan 自己补的一层。
3. **自己承担**：一个守护线程 + 常驻事件循环，`run_coroutine_threadsafe` 送协程。
4. **是否接近 Runtime**：**是**。这已经是一个 run-loop 管理问题，理论上属于 SuperHarness 的 MCP 客户端层。
5. **迁移**：把「MCP 调用共用一个长期 loop」作为 SuperHarness `MCPLoader` 的内建语义。
6. **保留理由**：在 SuperHarness 提供该语义之前，这是让 12306 MCP 稳定工作的最低成本方案（代码有详细注释说明原因）。

### 5.6 app.llm.LLM（app/llm.py:172）

1. **为什么需要**：固定流程要「JSON 结构化输出 + 失败有规则兜底 + 每次调用进 audit」。
2. **复用**：`create_model`（模型本身）。
3. **自己承担**：timeout（`TRAVELPLAN_LLM_TIMEOUT_SECONDS`）、`_parse_json_loose` 宽松解析、`LLMResult.degraded_reason` 降级说明、usage 提取、audit 留痕、`_emit` 上报。
4. **是否接近 Runtime**：部分。timeout/usage/emit 与 SuperHarness 的 Model Middleware（`wrap_model_call`）职责重叠（见 §7-D）。
5. **迁移**：若 SuperHarness 的 Model Middleware 支持「JSON 解析失败 → 返回降级结果」这一业务语义，重叠部分可上移。
6. **保留理由**：JSON 宽松解析与「有数据兜底」是旅行业务的确定性需求，不是所有业务都要。

## 6. 当前复杂度

| 文件 | 行数 | 判断 |
|---|---|---|
| app/workflow.py | 2748 | 偏重：12 节点 + 所有告警函数（`_transit_span_issues` / `_off_date_issues` / `_inbound_span_issues` / `_hotel_issues` / `_select_transport` / `_select_hotel` / `_transport_score`…）+ 产物组装叠加在一个文件 |
| app/planner.py | 3159 | 偏重：领域算法最大的一块，但已按节标题（聚类/折返/Trust/比价/时间窗）组织，仍可再按「时间约束」与「空间约束」拆 |
| app/providers.py | 1727 | 中：Hub + 3 个 ToolSet/Handle + 归一化函数，职责尚清晰 |
| app/models.py | 1250 | 中：纯定义，长度大但低复杂度 |
| app/llm.py | 417 | 轻：可接受 |

- **合理领域复杂度**：`planner.py` 的时间窗/营业时间/折返/比价，`workflow.py` 里各节点的诚实性告警（这些都是真实业务规则）。
- **可能职责过重**：`workflow.py` 同时扛「编排」「比选评分函数（_transport_score/_hotel_score）」「告警函数」「产物组装」。**比选/告警是纯函数，可以整体下沉到 planner.py 或单独 `app/scoring.py`，让 workflow.py 只留编排**。
- **不建议**：为拆而拆的 `Manager / Factory / RepositoryFactory / StrategyFactory` 多层抽象。拆的判据是「调用链是否更易读」。

## 7. 当前已知架构问题

### A. SuperHarness capability 尚未提交（影响可复现性）

- 当前 TravelPlan submodule 指针：`2c2c51a3`（`git ls-tree HEAD super_harness` → `160000 commit 2c2c51a3…`）。
- 本地 `super_harness/` 工作区内有未提交的新增能力：`tuniu_travel`、`amap_cn`、`tikhub_social`、`mediacrawler_social`（`superharness/capabilities/plugins/*`）+ `railway_12306.py` + `superharness/capabilities/mcp/__init__.py` 的追加导出。
- **fresh clone 无法完整运行**：`git clone && git submodule update --init` 拿到的是 `2c2c51a3`，不含任何 capability。`packages_root()` 指向 `superharness` 包目录（`app/providers.py:412`），Plugin 必须物理存在于子模块内，所以这个缺口无法靠 TravelPlan 侧代码补。
- 已如实写进 `README.md` §3。

### B. 固定 Workflow 没有完整 Trace Tree

- 机制：`ObservabilityMiddleware.before_agent`（`super_harness/superharness/middleware.py:56`）里 `trace_id = uuid4().hex[:12]`，写到 agent state 的 `_obs_trace_id`；随后 `wrap_model_call` / `wrap_tool_call` 打点都带这个 trace_id。`TraceCollector.__call__`（`super_harness/superharness/observability/trace.py:29`）遇到 `event.trace_id` 为空直接跳过。
- 固定流程走 `TravelWorkflowRunnable → execute_travel_run → LangGraph`，**没有经过 Agent 生命周期**，因此没有 `before_agent` 建的 trace_id。Provider/LLM 事件虽然通过 `_Emitter → HybridRunner.emit → EventBus` 到达 Console（所以 `[TOOL]`/`[LLM]` 能看到），但 `TraceCollector` 因缺 trace_id 不建 span 树。
- 现有替代物是 TravelPlan 自己的 `audit_report.json`（timeline / stages / provider_calls / llm_calls），是**业务审计**而不是原生 Trace。

### C. ProviderHub 直接调用 Loader，不经过 Agent Capability Router

- 现状：`ProviderHub._plugin_call`（`app/providers.py:721`）直接 `self.plugins.get(tool_name)` → `BaseTool.invoke`，`fetch` 就是 `PluginToolSet`（`PluginLoader`）与 `MCPServerHandle`（`MCPLoader`）。
- 原因：固定流程是**确定性子流程**，它要的是「按名调这个具体工具并拿回 provenance」，而不是让 Agent Loop 去做 tool 选择。走 Capability Router 意味着把「选哪个工具」交给 Agent 决策，这在固定流程里既慢又不确定。
- 代价：SuperHarness 的 `wrap_tool_call` 中间件（打点/观测）没有作用到这些调用上，TravelPlan 得自己 `_record` + `_emit_tool` 补全这一层。

### D. LLM Wrapper 与 SuperHarness Model Middleware 的职责重叠

- `app/llm.py` 自己处理 timeout、JSON 宽松解析（`_parse_json_loose`）、规则兜底（`degraded_note`）、usage（`_usage_of`）、audit（`LLMResult.to_audit`）、上报（`LLM._emit`）。
- SuperHarness 的 `ObservabilityMiddleware.wrap_model_call` 也做模型调用的打点/usage/token 统计（`super_harness/superharness/middleware.py:103`）。
- 重叠点：**timeout、usage/token 统计、调用打点**。不重叠点：**JSON 宽松解析、旅行业务的规则兜底**（SuperHarness 不该知道「意图解析失败时该给什么默认值」）。
- 结论：重叠部分若 SuperHarness 的 Model Middleware 提供可注入的「结构化解析与降级」钩子，可上移；业务兜底应留在 TravelPlan。

## 8. 不在本轮的理想图（明确未实现）

- 完整 Span 树（Trace）对固定流程 —— **当前未实现**（§7-B）。
- Bad Case 结构化记录 / Benchmark 评测 / RSI 自改进 —— **当前未实现**，是下一阶段设计（见同目录 `TRACE_BADCASE_SPEC.md`、`BENCHMARK_SPEC.md`）。
- `runs/<run_id>/trace.json` 统一目录 —— **当前未实现**，现有产物在 `outputs/<run_id>/`。