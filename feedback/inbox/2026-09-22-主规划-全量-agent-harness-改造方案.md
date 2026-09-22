# 主规划全量 Agent Harness 改造方案（2026-09-22 对齐）

> 状态：进行中（inbox，后续有新决策/注意事项续写在本文件）
> 本方案 **取代** 早期「只 agent 化 extract_places 入库决策 + critic 补证据」的方向（见
> feedback/done/2026-09-21-入库应由-LLM-调用工具完成.md）。那次只动两个节点；
> 这次是**主流程整体 agent 化**。

---

## 1. 目标（一句话）

页面 1-2-3 填完（前端给出 ROI：预算/天数/节奏/偏好）→ 后端不再跑固定 12 步图，
而是拉起 SuperHarness Agent Loop：agent 自己在循环里调工具（查机票/查酒店/查高德/
查途牛/12306/小红书…），自己控制预算、时间、合理性，最终 plan 也是 agent 生成的。
全程 trace（每步 token、总 token、每步调了什么工具）进 admin。

## 2. 已对齐的决策（用户逐条确认）

### 2.1 架构
- **固定 12 步 workflow 退役**（app/workflow.py 主路径不再作为规划引擎）。
- **主规划 = SuperHarness Agent Loop**；工具全部在 agent 循环里由 agent 按需调用，
  顺序不固定。
- **预算 / 时间 / 合理性 / 前端 ROI** 全部写进 system prompt，由 agent 自己控制。
- **REJECT/MUST 等用户硬约束不做 Python 校验**，一律注入 prompt（违反视为失败重试）。
- **Python 兜底只做一件事：时间冲突验证**（返程赶不上、营业时间排不下等），不校验预算。
- **discovery 预取保留**，作为 agent 上下文（避免 agent 每次重新搜一遍，省钱省时）。
- **plan 产出管线保留**：plan.json / plan.md / audit / badcases / outputs/ 落库这套
  「存 + 展示 + 审计」复用现有代码；agent 只负责按固定 schema 吐结构化 plan。
- **Jev 砍掉**（方案选择 / trade-off / 质量门由 agent 的 prompt 承担，不留第二套决策源）。

### 2.2 工具 / 能力
- 复用 super_harness 已有能力（Agent Loop / Tool Calling / Capability Router / Plugin /
  MCP / Retry / Trace / Memory / Observability），**不重复造轮子**；没有的能力往
  super_harness 里加，不旁路。
- 旅行工具（机票/酒店/高德/途牛/12306/小红书…）在 super_harness 里以 tool/plugin 形式
  注册，描述写清楚（agent 选工具靠它，前端展示「在查机票」也靠它）。
- **工具返回必须截断/摘要**（如只留 top 5 + 关键字段），否则 agent 多次调用上下文必爆。

### 2.3 兜底 / 防失控
- **LLM 兜底 = 配置两个额外 LLM API**（模型级 failover：主 LLM 挂了按顺序切兜底）。
- 配置里要有：`max_steps`（agent 最大循环步数）、超时、`max_run_tokens`（已有）。
- LLM 全挂 → 如实报失败，不允许编数据 / 空榜。

### 2.4 前端 / Admin
- **前端实时展示 agent 当前步骤**（「在查机票」「在查酒店」…），按 agent 实际调的工具
  动态展示，不是固定顺序；机制 = run 进度里写当前步骤字段，前端轮询（1-2s），不上 SSE。
- **admin 兼容要到位**：agent 步骤流、每步 token、总 token、prompt 版本都要能显示；
  现有 Trace 视图要改造/扩展成 agent 步骤视图，对接不能漏、不能没兼容好。
- 现有 token 逐次记录基建已有（app/admin_timeline.py:526 解析 input/output/cached/
  total_tokens），复用扩展即可，不重造。

### 2.5 Prompt / 测试 / 文档
- **prompt 单独文件管理**（所有 LLM 相关都走 super_harness）；**prompt 版本进 trace**，
  admin 可见（prompt 是唯一算法，改 prompt 要能对比）。
- **测试 / benchmark 重立**：老 33 例确定性套件建立在 12 步图上，随旧流程作废；
  新策略 = mock 工具返回 + 固定输入 + 断言 agent 行为（预算内 / 无 REJECT 点 /
  时间无冲突 / plan 里每个数字指回真实工具调用）。
- **文档清理**：大改后所有不相干文档改或删（docs/、README、feedback 旧方向标注）。
- **项目结构**：目前结构还行，允许适当改动。

### 2.6 本轮补充拍板（2026-09-22）
- **迁移策略 = 一刀切**：固定 12 步图直接删，不保留并行开关（用户明确）。
- **预热 agent 化程度 = 批调度仍由代码控制**：代码按批决定跑哪些城、串/并行；每城内部
  「搜攻略 / 入库 / 查高德」的决策交给 agent 调工具。
- **reviewer / 产品经理 = 干活方式**（多 agent 分工：并行实现 + reviewer 只读审查 +
  PM 架构审查），不写进产品代码；复用 super_harness 已有的 `review-agent` skill。
- **兜底模型已配好**：env 里 `MODEL_NAME/BASE_URL/API_KEY` 各有 `_bk1/_bk2` 两套备用。
- **交付标准**：代码完成且验证没问题 → 推 git + 检查部署更新。

## 4. 目标架构（spine）

```text
POST /api/v1/plans (background=true)  →  202
  → execute_agent_run()                    # 新入口，替换 execute_travel_run
      1. 组装上下文：intent + PrefetchBundle(discovery) + 用户偏好/预算 → system prompt
      2. create_harness_agent(
             system_prompt = TRAVEL_PLANNER_SYSTEM_PROMPT@v?,
             tools         = [travel tools（机票/酒店/高德/门票/攻略）],
             model         = 主模型,
             middleware    = [Observability, StepReporter, ModelFallback(主,bk1,bk2)] )
      3. ainvoke(input, config={"recursion_limit": MAX_STEPS}) + asyncio 超时
      4. agent 输出结构化 TripPlan
         → check_feasibility（唯一 Python 兜底：时间冲突）
         → 复用 finalize 管线（plan.json / plan.md / audit / badcases / 落库）
      5. 落 run_metrics（token 汇总：input+output）
  → 前端轮询 GET /plans/{id}/status（读 run_stages + trace_spans）
```

## 5. 实施阶段

- **P1 后端 agent 核心**：旅行工具注册（包装 ProviderHub）、LLM failover（主+bk1+bk2）、
  agent loop 入口、max_steps/超时配置。
- **P2 保留层接入**：`check_feasibility`（时间冲突）接在 agent 产出之后；复用 finalize
  落库/成文/审计管线；`app/places.py` 实体层原位保留。
- **P3 prompt 外置 + 版本**：system prompt 独立文件、带版本号、版本进 trace。
- **P4 观测桥接 + admin**：agent 每步（工具中文名/状态/耗时/token）→ `run_stages.facts_json`
  + `trace_spans`；admin timeline 增加 agent 步骤视图。
- **P5 前端**：结果页按 agent 实际调用的工具动态展示步骤（轮询 1-2s）。
- **P6 预热 agent 化**：批调度仍归代码（每批哪些城、串/并行），每城内部「搜攻略/入库/查高德」
  交给 agent 决策。
- **P7 测试重立 + 文档清理**：老 33 例/Jev 套件作废；新测试 = mock 工具 + 断言 agent 行为；
  删/改过时文档。
- **P8 验证 + 交付**：实跑验证 → 推 git → 检查部署更新。

## 6. 调研关键结论（2026-09-22）

- **无需新表**：`run_stages.facts_json`（工具名/状态/耗时）+ `trace_spans.attributes_json`
  （逐次 token）已足够承载 agent 每步；`run_metrics` 只做整 run 汇总。
- **旅行工具已就绪**：super_harness plugins 里有 tuniu_travel（机票/酒店/火车/门票/邮轮/度假）、
  amap_cn（geocode/search_poi/poi_detail/route）、tikhub_social、mediacrawler_social，
  另有 railway_12306 MCP。缺的是把它们接到主流程 + 结果截断。
- **super_harness 无多模型 failover**：`create_model`（agent.py:34）只读单个 MODEL_*；
  需接 langchain `ModelFallbackMiddleware`；`ModelRetryMiddleware`/`ToolRetryMiddleware` 已有。
- **`llm.finished` 事件无 total_tokens**（只有 input/output/cached）→ 汇总自行累加。
- **循环上限**：super_harness 未内建 max steps/超时 → `config={"recursion_limit":N}`
  或 `ToolCallLimitMiddleware` + `asyncio.wait_for`。
- **Provider 层可包 tool**：`app/providers.py` ProviderHub 方法全阻塞 HTTP、带缓存与降级链，
  `_truncate_payload:442` 已有截断雏形。
- **可原位保留**：`app/places.py`（不依赖 workflow）、`planner.check_feasibility`/`apply_feasibility_pass`。
- **必删**：`app/decision/`（Jev 全套）、`app/selection.py` 的硬排除逻辑（改 prompt）。

## 7. 待确认

- 「前端 ROI」：代码里查无此词（全仓 grep 只命中本文档）。当前向导实为 **2 步**
  （explore + preferences，`frontend/.../wizard-progress.tsx STEP_META`）。用户口中的
  「页面123 + ROI」按「前端收集的全部输入（基础信息 + 偏好 + 预算）」理解，待确认。

## 9. 实施进展（2026-09-22 晚）

**已完成（P1–P7 主体）**
- 新入口 `app/agent_runner.py::execute_agent_run`（`app/agent.py` 已切过去）；agent loop +
  `submit_final_plan` 交卷 + 时间冲突兜底 + 复用落库/成文/审计/badcase/metrics。
- 工具：`app/travel_tools.py::build_travel_tools(hub)`（11 个），`app/agent_trace.py` 步骤上报。
- LLM failover：`app/config.py` 读 `MODEL_*_bk1/_bk2`，`ModelFallbackMiddleware` 注入。
- 护栏：`MAX_AGENT_STEPS`(40) / `AGENT_RUN_TIMEOUT_SECONDS`(600) / `MAX_RUN_TOKENS`（均进 `EDITABLE_KEYS`）。
- Prompt 外置：`TRAVEL_PLANNER_SYSTEM_PROMPT` + `TRAVEL_PLANNER_PROMPT_VERSION`，版本进 trace/audit。
- admin 有 agent 步骤视图；前端运行中动态展示 `progress.current_stage` + `stages`（轮询 1.2s）。
- 预热：`app/preheat_agent.py` + `app/preheat_tools.py`，批调度仍归 `scripts/preheat_cities.py`。
- **已删除**：`app/decision/`（Jev 全套）、12 节点 LangGraph、`benchmark/` 整套 + `tests/test_benchmark.py`；
  `app/decision/profile.py` → `app/profile.py`。`app/selection.py` 仍在用（discovery/sessions/recommendations），保留。
- `pytest tests/ -q` → **1173 passed**（已亲测复核）。

**遗留 / 待决策**
- [ ] **benchmark 已整体删除**（旧套件全建立在 12 步 + Jev 上，改造成本高于价值）。是否要按
      agent 路径重建一个「mock 工具 + 断言 plan 质量」的最小基准？还是先不建？
- [ ] **Jev 尸体**未清：`app/config.py` 的 `jev_*` 配置、`app/badcase.py` 的 7 个 `jev_*` 类别 +
      `_jev_cases`、admin 六维指标里的 `jev` 维度与 `/admin/jev/health` 接口、前端对应展示。
      （不可达但不干净；清理会牵动 admin/前端，待确认是否本轮清。）
- [ ] 预热 agent 的每步观测没进 `run_stages/trace_spans`（预热不建 run 行）；admin 看不到预热逐步。
- [ ] `scripts/rebuild_city_cache.py` 与会话惰性后台刷新仍走旧的固定路径（`sessions.refresh_city_cache`）。
- [ ] 预热单城耗时未实测（agent 每步一次模型往返，15 城可能逼近 CI 的 90 分钟上限）。
- [ ] **真实 LLM 端到端未由我跑过**（全量测试是离线 fake 模型；前端 e2e 用真实后端但没跑真 LLM）。

## 10. 待定 / 后续续写

（下面是占位，聊到就补，不阻塞开工）
- [ ] DB schema 迁移方案：agent 步骤流落库（新表 or 扩 trace_spans/run_progress）、
      prompt 版本字段、LLM failover 事件记录。
- [ ] 上线/迁移顺序：一刀切 vs 新旧并行（新 agent 路跑通验证质量再切默认，最后删 12 步图）。
- [ ] 新 benchmark 具体形态与验收标准。
- [ ] 前端改动边界确认（页面 1-2-3 主体不动，动「开始规划后」展示 + admin Trace 视图）。
- [ ] 预算超支时 plan 展示策略（只标注不校验？）。

## 4. 用户补充注意事项（原话要点）

- 「admin 展示哪些，对接补上，或者没兼容好，这个你要注意」→ 见 2.4。
- 「一定要动 super_harness 已经有的不要重复造轮子，没有的可以往里面加」→ 见 2.2。
- 「agent 失败/LLM 挂了：我会配置两个额外的 llm api 作为兜底 llm」→ 见 2.3。
