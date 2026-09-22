# 主规划：SuperHarness Agent Loop

一句话：主规划不再是固定 12 步 LangGraph，而是一个 Agent Loop —— Agent 自己在循环里决定查什么、
自己控制预算与时间，最后交卷；Python 只保留**时间冲突兜底**。

- 入口：`app/agent_runner.py::execute_agent_run`（`app/agent.py::TravelWorkflowRunnable` 调它，
  CLI / FastAPI / Guided 的 `start_run` 都汇聚到这里）。
- 装配：`app/agent.py::create_travel_app` 把本业务的 runnable 与 Agent Loop 一起挂到
  SuperHarness 的 `HybridRunner` 下（route 常量仍是 `Route.WORKFLOW`，属于对外契约，不为改名而改名）。
- 旧的固定 12 步图（`app/workflow.py::execute_travel_run`）**已退役**，代码里还在（产物写出、
  审计、Bad Case 等 helper 被复用），但不再是规划引擎。

## 1. 一次 run 的流程

```text
① create_run(run_id, source)                        （store）
② build_step_reporter(store, run_id)                观测桥，见 §5
③ ProviderHub（自建或注入）+ build_travel_tools(hub)  11 个旅行工具，见 §3
④ Discovery 过户（prefetch 非空时）
     _adopt_prefetch_sources + _materialize_city_evidence
⑤ create_harness_agent(
       system_prompt = TRAVEL_PLANNER_SYSTEM_PROMPT@版本,
       tools         = 11 个旅行工具 + submit_final_plan,
       middleware    = [StepReporter, ModelFallback(主/bk1/bk2), _AlwaysVisibleTools, _TokenBudgetGuard] )
⑥ agent.ainvoke(payload)    payload = 用户原话 + 已确认 intent + Discovery 预取摘要
     循环：Agent 自己选工具 → 看结果 → 再选 …（顺序不固定，步数受 MAX_AGENT_STEPS 限）
     收尾：调用 submit_final_plan 交卷
⑦ 取行程：holder 里的 SubmittedPlan；没交卷则退一步解析最后一条 AI 消息里的 JSON；
     再拿不到 → status=FAILED（不编造 plan）
⑧ Python 唯一兜底：_apply_time_pass（时间冲突验证与修订）
⑨ 复用既有产物管线：save_plan / _render_markdown / _build_audit /
     _detect_and_save_badcases / _write_artifacts / _write_run_artifacts / finish_run
```

### 1.1 输入分成两半，这是 prompt 版本号有意义的前提

| 位置 | 内容 | 代码 |
| --- | --- | --- |
| system prompt（静态、带版本） | "怎么规划"：取数纪律、三条硬约束（用户偏好 / 预算 / 时间与合理性）、交卷方式、交卷前自查 | `app/prompts.py::TRAVEL_PLANNER_SYSTEM_PROMPT` |
| user prompt（每 run 不同） | 用户原话、前端已确认的结构化 intent（含 MUST / WANT / REJECT 清单）、Discovery 预取摘要 | `app/agent_runner.py::_user_prompt` / `_prefetch_digest` |

预取摘要只给"前 N 条 + 关键字段 + 用户对每个点的选择"，**不给原始 payload**（预取里可能有几十个
POI 与几 KB 攻略正文，全量灌进 prompt 会让第一次模型调用就吃满上下文）。候选的 `place_id` /
`source_id` 原样保留，Agent 之后可以按 id 指回真实证据。

## 2. 谁负责什么（Agent Loop 版的边界）

```text
真实价格/时刻/车次/航班/酒店/门票/POI/路线/攻略 → Provider / Tool / MCP（app/providers.py + super_harness）
"这次旅行怎么排"（顺序、取舍、预算与节奏的权衡） → Agent 自己（system prompt 就是算法）
时间冲突验证（返程赶不上、时间重叠）             → Python（app/planner.py::apply_feasibility_pass）
产物 / 落库 / 审计 / Bad Case / Benchmark / Evolution → Python（复用 app/workflow.py 的管线）
```

推论：

- 行程里的每个数字都必须来自一次工具返回；Agent 不许凭记忆补价格或时刻（写死在 prompt 里）；
- **预算不校验**：预算是 prompt 的职责，Python 不拦截超预算的行程（只在 plan 里如实标 `over_budget`）；
- **MUST / REJECT / 禁忌不校验**：同样写进 prompt 由 Agent 自己保证，Python 不做硬排除；
- **营业时间不校验**：`_apply_time_pass` 传入 `places=None`，planner 会跳过营业时间相关判断 ——
  检查不出来 ≠ 没问题，所以 prompt 要求 Agent 自己保证，并把不确定的写进 `risk_note` / `warnings`；
- **不落 places / evidence 表**：取数由 Agent 在循环里完成，Python 只看到它交回来的 plan，
  所以本次 run 的 `places` / `evidences` 如实为空（跨会话的攻略正文仍会经城市知识库落地）。

### 2.1 时间兜底具体做什么

`_apply_time_pass`（`app/agent_runner.py`）把 Agent 交上来的 `days` 交给
`planner.apply_feasibility_pass`，喂进去的证据只有两样：Agent 写在 item 上的路段耗时
（`_routes_from_plan`）与 transport 上的去回程时刻。

- 去程抵达日按 `arrival_day_index` / `arrival_day_start_minutes` 计算（跨午夜抵达也算得对）；
- 末日的可用窗口由 `departure_deadline_minutes`（返程发车/起飞前留缓冲）决定；
- 修订结果替换 `plan.days`，问题清单并入 `plan.warnings`；
- 仍有 `severity=error` 且未解决的冲突 → 记一条降级说明（不编造"修好了"）。

## 3. 工具集（`app/travel_tools.py::build_travel_tools(hub)`）

11 个工具，全部是 `ProviderHub` 方法的薄封装；返回值统一是**截断后的 JSON 字符串**
（默认 `top_n=5`，且有硬上限），信封语义：`status` / `total` / `shown` / `truncated` / `items`。

| 工具 | 用途 | 前端步骤文案 |
| --- | --- | --- |
| `search_trains` | 城际火车（车次、时刻、历时、席别余票、票价区间） | 在查火车票 |
| `search_flights` | 机票（航班号、起降、含税价） | 在查机票 |
| `search_hotels` | 酒店列表（起价、评分、坐标） | 在查酒店 |
| `search_scenic_tickets` | 按景区名查各渠道门票报价 | 在查景区门票 |
| `geocode` | 地址 → 坐标 | 在定位地址 |
| `search_poi` | 按关键词搜真实 POI | 在搜周边地点 |
| `poi_detail` | POI 详情（营业时间 / 评分 / 消费） | 在查地点详情 |
| `route` | 两点之间的真实距离与耗时 | 在算路上时间 |
| `search_xiaohongshu` | 小红书攻略（观点，不是事实） | 在翻小红书攻略 |
| `search_douyin` | 抖音攻略 | 在翻抖音攻略 |
| `web_search` | 官网公告 / 预约 / 临时闭馆核实（**不能**当价格来源） | 在搜网页资料 |

`TOOL_DISPLAY_NAMES`（同文件）是"工具名 → 中文步骤文案"的**唯一一份**映射，
`app/agent_trace.py` 只做再导出，新增工具时不会漏改展示名。

两条装配期的接线（不是绕过 Router，是必要的接线，理由写在 `agent_runner` 模块 docstring）：

1. **`_AlwaysVisibleTools`**：SuperHarness 的能力筛选（`settings.tool_top_k` 默认 3）用会话里
   最后一条 HumanMessage 打分，而 Agent Loop 里那条消息从头到尾不变 —— 不补回工具的话，
   这一趟固定只能看见 3 个工具。该 middleware 在筛选**之后**把本业务工具按名字补回可见列表；
2. **`web_search` 不传进 `tools=`**：它与 SuperHarness 自动发现的同名内置工具会触发
   `ValueError`（不允许静默覆盖）。内置那个够用，且 `hub.web_search` 转发的正是同一个工具。

## 4. 交卷机制

- 终结工具 `submit_final_plan`（`SUBMIT_TOOL_NAME`），参数 schema = `SubmittedPlan`（扁平的
  逐日 / 大交通 / 住宿 / 预算 / 来源字段）。工具闭包把校验通过的行程记进 holder。
- 参数不合法 → 工具返回 `{"status": "INVALID", "hint": ...}`，让 Agent 能看到错在哪并重试。
- 没调用终结工具 → 退一步解析最后一条 **AI 消息**里的 JSON（工具结果里的 JSON 不算，
  把候选列表当行程是编造的一种）。
- 两条路都拿不到，或 `days` 为空 → `status=FAILED` + 明确 error。**绝不编造 plan**。
- 拿到了行程但循环被截断（超时 / 步数 / token）→ 保留这份行程，并如实追加一条降级说明。

## 5. 护栏与降级

| 护栏 | 配置 | 行为 |
| --- | --- | --- |
| 最大步数 | `MAX_AGENT_STEPS`（默认 40）→ LangGraph `recursion_limit` | 超限抛 `GraphRecursionError` → 没交卷就 FAILED，交了卷就标降级 |
| 墙钟上限 | `AGENT_RUN_TIMEOUT_SECONDS`（默认 600） | 两层超时：内层 `asyncio.wait_for` 发起取消，外层守护线程 + join 预算；到点放弃等待 |
| 成本护栏 | `MAX_RUN_TOKENS` → `_TokenBudgetGuard` | 在**每次模型调用前**检查累计 token，超了抛 `_TokenBudgetExceeded`；已交卷的行程保留 |
| 模型降级 | `.env` 的 `MODEL_*` + `MODEL_*_bk1` / `_bk2` | `app/agent.py::build_model_with_fallbacks` 返回 `ModelFallbackMiddleware(主 → bk1 → bk2)`：抛错即降级，不在同一条坏路上重试；整套配不全的备用被跳过 |
| 取数降级 | Provider 层 | 工具失败也返回信封（`status != OK`），Agent 应换源 / 换词重试；重试仍失败就如实写进 warnings，不填空白 |

模型一条都没配全 → 直接 FAILED，错误文案点名要配哪些变量。

## 6. 观测：每一步都留痕

`app/agent_trace.py::build_step_reporter(store, run_id)` 是 Agent Loop 与前端 / admin 之间的
**唯一观测桥**（不新建表、不改表结构）：

```text
工具调用 → run_stages（stage_id = 中文展示名，
                      facts_json = {tool, status, duration_ms, started, finished, seq,
                                    stage_key, query, note, error}）
         + trace_spans（component=tool）
         + run_progress.current_stage  ← 前端轮询的就是它
模型调用 → trace_spans（component=llm，attributes 带 input/output/cached/**total** tokens）
         + run_stages（stage_id =「在思考行程」，模型思考期间前端不停留在上一个工具名）
```

同一个工具的第 N 次调用共享同一行 stage（最后一次覆盖 facts），**逐次调用的完整事实在
trace_spans**（span_id 带序号，不会被顶掉）。prompt 版本写进一条
`component=workflow / name=prompt_version` 的 span（含工具清单与 system prompt 字符数），
同时进 `audit["agent"]` 与 `run_metrics.agent.prompt_version`。

观测失败绝不影响规划：所有落库调用都过保护，异常只记 debug 日志。

## 7. 产物与落库（与旧管线同一套代码）

| 落点 | 内容 |
| --- | --- |
| `data/travelplan.db`（或 Neon） | run / plan / plan_items / decisions / run_stages / trace_spans / run_progress / run_metrics / badcases / provider_calls… |
| `outputs/<run_id>/` | plan.json、plan.md、audit_report.json、trace.jsonl、metrics.json、badcases.json |
| stdout（SuperHarness） | 结构化运行日志（模型、工具调用、决策） |

`audit_report.json` 里 Agent 专属的一段：

```json
"agent": {
  "prompt_version": "travel-planner-v1",
  "plan_origin": "submit_final_plan | message_json",
  "max_steps": 40,
  "timeout_seconds": 600.0,
  "truncation": null
}
```

`run_metrics` 里 `jev_calls` 恒为 0（Agent 路径没有 Jev，方案选择由 prompt 承担），
并带 `agent.prompt_version` / `agent.degraded`。

有降级时：`runs.status` 仍是 `completed`（兼容旧接口），只把 `run_progress.status` 提升为
`DEGRADED`。

## 8. 城市知识库预热：批调度归代码，每城内部归 Agent

`scripts/preheat_cities.py` 仍管"跑哪些城、串行、每天限量、跳过判定、退出码"；
每座城市内部改由 `app/preheat_agent.py::preheat_city` 里的预热 Agent 决策：

```text
search_guides(keyword, platform)  小红书 / 抖音 / 网页
search_poi / poi_detail           到高德核实地点
save_city_evidence(evidence_id)   登记攻略条目（正文由系统按 id 取回）
save_city_poi(place_id, query)    登记 POI
```

工具集见 `app/preheat_tools.py::build_preheat_tools`；Agent 只能"选"，不能"造"
（正文与 POI 一律由 `RecordingHub` 从 Provider 原样取回）。失败即不落库，因为没有 POI 的城市
本来就不算缓存命中。system prompt 版本 `CITY_PREHEAT_PROMPT_VERSION`，护栏是
`MAX_PREHEAT_AGENT_STEPS` / `PREHEAT_AGENT_TIMEOUT_SECONDS`。

读路径不变：`sessions` 命中城市知识库时交通 / 酒店照旧现查，攻略与 POI 直接用缓存。
见 [数据复用与实体](DATA_REUSE_ENTITY.md) 与 [部署说明](../operations/DEPLOYMENT.md) §9。

## 9. 已经不存在的东西（避免照着旧文档找）

- 固定 12 步节点（`parse_intent` … `finalize`）不再是主路径的实现顺序；
- Jev 决策层（方案选择 / Trade-off / 质量门）不在主路径上，Agent 路径不会产生 `jev` span；
- Top-K 候选行程 + Python 硬约束复核 / `LLM Ranking` 兜底不在主路径上；
- `app/selection.py` 的"策略→权重"与 MUST / WANT / REJECT 候选硬排除**不在主规划路径上**
  （规划交给 prompt）；它仍在 **Guided 侧**使用（偏好归一、节奏推导、推荐分类），
  其结论以结构化事实注入 user prompt。
