# TravelPlan 文档索引

一句话：**这个目录回答"想改什么该动哪个文件"，并把每个机制讲清楚到能自己动手改。**

产品范围：中国国内旅行规划。技术形态：两条入口（引导式 Planning Session / 一句话）+ 固定 12 步的 Python 流程 + 真实数据源 + 少量 LLM/Jev 软决策。

## 想改什么 → 去哪

| 你想要的 | 去哪 | 具体入口 |
| --- | --- | --- |
| 改 Prompt | `app/prompts.py` | 5 个提示词常量；模型只在意图解析、检索词扩写、地点抽取、Critic、成文这几处被调用 |
| 改 Config / 阈值 | `app/config.py` | `TravelPlanConfig`（Jev / 预算 / 模型单价 / Discovery / BadCase / Benchmark / Evolution 开关）、`PlannerTuning`（`TP_*` 行程阈值）、`EDITABLE_KEYS`（管理端可运行时改的白名单） |
| 改 Workflow 步骤 | `app/workflow.py` | `build_travel_graph()` 里的 12 个节点与边；`_instrument_node` 负责进度与 span；`_reuse_result` 复用 Prefetch |
| 改行程质量算法 | `app/planner.py` | Trust / Ad Risk / 候选打分 / 聚类 / 排程 / 预算 / 可行性 / Critic / 质量度量 / Top-K 候选 |
| 改用户旅程 / 会话 | `app/sessions.py` + `app/discovery.py` | 状态机、TTL、create/patch/start、事件时间线；取数的唯一实现（Prefetch 与 Workflow 共用） |
| 改用户选择（策略/POI） | `app/selection.py` | 策略→权重、`resolve_pace`、`apply_user_place_preferences`（MUST/WANT/REJECT） |
| 改 Jev 用法 | `app/decision/jev.py`（调用层）、`app/decision/planner_decision.py`（三个决策节点） | 想改"要不要问 Jev / 问什么"改后者；想改"怎么调 Jev"改前者 |
| 改 Provider 策略 | `app/providers.py` + `super_harness/superharness/capabilities/` | `ProviderHub` 管主备、缓存、审计账本；具体能力在 submodule 的 plugins/mcp 里 |
| 看 Trace / 观测 | `app/observability.py`、`app/workflow.py`、`data/travelplan.db` | 见 [05_Trace与可观测性.md](05_Trace与可观测性.md)；管理端 `/admin/runs/{id}` |
| 看 Provider 健康 | `provider_calls` 表 + `app/api.py:_provider_status` | 见 [12_Provider健康与观测.md](12_Provider健康与观测.md)；管理端 `/admin/providers` |
| 新增 Benchmark | `benchmark/cases/*.jsonl` + `benchmark/README.md` | 加一行 JSON 即可；确定性用例不许联网；改了 cases 或 `FIXTURE_VERSION` 要重跑两份基线 |
| 新增 BadCase 规则 | `app/badcase.py` | 加一条规则函数并接进 `detect_badcases()`；类别常量在模块顶部 |
| 触发 Evolution | 管理端 `/admin/evolution` 或 `POST /api/v1/admin/evolution/runs` | 需要 `EVOLUTION_ENABLED=true`，否则 409 |
| 部署 | [09_部署说明.md](09_部署说明.md) | Vercel 只能承载前端；API 需要常驻容器（原因与替代方案都在那篇） |

## 文档清单

| 文件 | 用途 |
| --- | --- |
| [01_项目架构.md](01_项目架构.md) | 目录、分层、数据流（Quick 与 Guided 两条入口），以及为什么这样分层 |
| [02_配置说明.md](02_配置说明.md) | 所有环境变量与阈值：默认值、作用、风险；运行时覆盖与模型单价 |
| [03_项目流程图.md](03_项目流程图.md) | Guided Pre-Planning 与 Final Planning Workflow 的分开流程图、Session 状态机、12 步时序 |
| [04_数据源与工具.md](04_数据源与工具.md) | 五个能力 + LLM + Jev 的用途、依赖、降级语义、隐私边界、Provider 调用账本 |
| [05_Trace与可观测性.md](05_Trace与可观测性.md) | span 命名、状态机、产物清单、指标字段、管理端消费方式 |
| [06_BadCase机制.md](06_BadCase机制.md) | 字段表、全部类别与触发条件（含用户旅程 8 类）、人工处理流程 |
| [07_评测体系.md](07_评测体系.md) | Benchmark v0.1：套件、指标、基线、怎么加 case；与 Guided Journey 测试的关系 |
| [08_进化机制.md](08_进化机制.md) | Evolution：聚类 → 候选改动 → 评测对比 → Experience，以及不做什么 |
| [09_部署说明.md](09_部署说明.md) | Vercel 能力边界（附官方限制）、容器部署、环境变量、同域名方案 |
| [10_开发与修改指南.md](10_开发与修改指南.md) | 本地跑起来、测试约定、新增能力的步骤、提交前检查清单 |
| [11_用户旅程与PlanningSession.md](11_用户旅程与PlanningSession.md) | Guided 六步向导 → 正式 run：会话状态机、Prefetch、结构化偏好、MUST/WANT/REJECT |
| [12_Provider健康与观测.md](12_Provider健康与观测.md) | `provider_calls` 调用账本、Provider Health 统计口径与阈值、UNKNOWN 语义、脱敏 |

## 设计期参考文档（历史留存，不是当前实现的说明书）

这些是动手写代码之前的设计稿，**部分内容已经与实现不同**（实现以代码为准）：

- [PRD.md](PRD.md)、[START.md](START.md)、[FRONTEND_DESIGN.md](FRONTEND_DESIGN.md)
- [ACCEPTANCE.md](ACCEPTANCE.md)：早期验收记录
- [review/](review/)：架构现状、Benchmark 规格、Trace/BadCase 规格、迭代日志

代码注释里有大量 `PRD §x` / `START.md §x` 的引用，指向的就是这些文档，因此它们保留不删。
**判断"现在到底怎么跑的"请以代码和本目录 01~12 为准。**
