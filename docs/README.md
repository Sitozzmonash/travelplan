# TravelPlan 文档索引

一句话：**这个目录回答"想改什么该动哪个文件"，并把每个机制讲清楚到能自己动手改。**

产品范围：中国国内旅行规划。技术形态：两条入口（引导式 Planning Session / 一句话）+ 固定 12 步
的 Python 流程 + 真实数据源 + 7 处 LLM 调用 + 3 处 Jev 软决策。

> **只有 `01`~`15` 是"当前实现的说明书"，以代码为准。**
> `PRD.md` / `START.md` / `FRONTEND_DESIGN.md` / `ACCEPTANCE.md` / `TravelPlan_UI_优化方案.md` /
> `TravelPlan_Admin_整体优化方案.md` / `目前缺陷.md` 是设计稿与台账，文首都标了状态横幅；
> `docs/discard/` 是已归档、不再维护的旧文档。

## 想改什么 → 去哪

| 你想要的 | 去哪 | 具体入口 |
| --- | --- | --- |
| 改 Prompt | `app/prompts.py` | 9 个提示词常量（其中 `EXTRACT_PLACES_PROMPT` 已无生产引用，只剩测试 mock 还在用）；模型在 7 处被调用：意图解析、检索词扩写、地点抽取、动态偏好画像、候选方案软排序、Critic、成文，另有 1 处批超时按单篇重试（复用 `EXTRACT_PLACES_BATCH_PROMPT`，`tag=extract_places_retry`） |
| 改 Config / 阈值 | `app/config.py` | `TravelPlanConfig`（Jev / 预算 / 模型单价 / Discovery / 城市缓存 / BadCase / Benchmark / Evolution 开关）、`PlannerTuning`（`TP_*` 行程阈值）、`EDITABLE_KEYS`（管理端可运行时改的白名单，含 10 个 `TP_*`） |
| 改 Workflow 步骤 | `app/workflow.py` | `build_travel_graph()` 里的 12 个节点与边；`_instrument_node` 负责进度与 span；`_reuse_result` 复用 Prefetch |
| 改行程质量算法 | `app/planner.py` | Trust / Ad Risk / 候选打分 / 聚类 / 排程 / 预算 / 可行性 / Critic / 质量度量 / Top-K 候选 |
| 改动态偏好与软决策 | `app/decision/profile.py`（画像）、`app/decision/planner_decision.py`（方案选择 / Trade-off / 质量门 / LLM Ranking 兜底） | 画像目前只被 `hotel_area` / `hotel` / `attraction` 三组权重消费，见 [14](14_决策与规划质量优化.md) 文首状态表 |
| 改用户旅程 / 会话 | `app/sessions.py` + `app/discovery.py` | 状态机、TTL、create/patch/start、事件时间线；取数的唯一实现（Prefetch 与 Workflow 共用） |
| 改跨会话"城市知识库" | `app/city_cache.py` + `app/store.py`（`city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta`） | 攻略与高德 POI 的半静态缓存，TTL 按 `CITY_CACHE_*_TTL_DAYS`；见 [13](13_数据流与复用机制.md) §2.1 |
| 改用户选择（策略/POI） | `app/selection.py` | 策略→权重（`transport_weights` / `hotel_weights`）、酒店硬过滤、`resolve_pace`、`apply_user_place_preferences`（MUST/WANT/REJECT） |
| 改 Jev 用法 | `app/decision/jev.py`（调用层）、`app/decision/planner_decision.py`（三个决策节点） | 想改"要不要问 Jev / 问什么"改后者；想改"怎么调 Jev"改前者 |
| 改 Provider 策略 | `app/providers.py` + `super_harness/superharness/capabilities/` | `ProviderHub` 管主备、缓存、审计账本；具体能力在 submodule 的 plugins/mcp 里 |
| 改数据库后端 / 方言 | `app/db.py` | `resolve_backend`（未设 `DATABASE_URL` → SQLite，否则 Postgres）+ SQLite→Postgres 方言翻译 |
| 看 Trace / 观测 | `app/observability.py`、`app/workflow.py`、`data/travelplan.db` | 见 [05](05_Trace与可观测性.md)；管理端 `/admin/runs/{id}` |
| 看 Provider 健康 | `provider_calls` 表 + `app/api.py:_provider_status` | 见 [12](12_Provider健康与观测.md)；管理端 `/admin/providers` |
| 新增 Benchmark | `benchmark/cases/*.jsonl` + `benchmark/README.md` | 加一行 JSON 即可；确定性用例不许联网；改了 cases 或 `FIXTURE_VERSION` 要重跑两份基线 |
| 新增 BadCase 规则 | `app/badcase.py` | 加一条规则函数并接进 `detect_badcases()`；类别常量在模块顶部 |
| 触发 Evolution | 管理端 `/admin/evolution` 或 `POST /api/v1/admin/evolution/runs` | 需要 `EVOLUTION_ENABLED=true`，否则 409 |
| 部署 | [09_部署说明.md](09_部署说明.md) | 固定架构：前端托管（Vercel 或 EdgeOne Pages）+ Render 常驻容器 + Neon PostgreSQL；含免费层实测与验收脚本 |

## 当前实现的说明书（01~15）

| 文件 | 用途 |
| --- | --- |
| [01_项目架构.md](01_项目架构.md) | 目录、分层、数据流（Quick 与 Guided 两条入口），以及为什么这样分层 |
| [02_配置说明.md](02_配置说明.md) | 所有环境变量与阈值：默认值、作用、风险；运行时覆盖与模型单价 |
| [03_项目流程图.md](03_项目流程图.md) | Guided Pre-Planning 与 Final Planning Workflow 的分开流程图、Session 状态机、12 步时序 |
| [04_数据源与工具.md](04_数据源与工具.md) | 各能力 + LLM + Jev 的用途、依赖、降级语义、隐私边界、Provider 调用账本 |
| [05_Trace与可观测性.md](05_Trace与可观测性.md) | span 命名、状态机、产物清单、指标字段、管理端消费方式 |
| [06_BadCase机制.md](06_BadCase机制.md) | 字段表、全部类别与触发条件（含用户旅程 8 类）、人工处理流程 |
| [07_评测体系.md](07_评测体系.md) | Benchmark：套件（33 例确定性 + 5 例 Live Smoke）、指标、基线、怎么加 case |
| [08_进化机制.md](08_进化机制.md) | Evolution：聚类 → 候选改动 → 评测对比 → Experience，以及不做什么 |
| [09_部署说明.md](09_部署说明.md) | 固定架构、Vercel / EdgeOne 能力边界与官方限制、环境变量、免费层实测、验收脚本 |
| [10_开发与修改指南.md](10_开发与修改指南.md) | 本地跑起来、测试约定、新增能力的步骤、提交前检查清单 |
| [11_用户旅程与PlanningSession.md](11_用户旅程与PlanningSession.md) | Guided 三步向导 → 正式 run：会话状态机、Prefetch、结构化偏好、MUST/WANT/REJECT |
| [12_Provider健康与观测.md](12_Provider健康与观测.md) | `provider_calls` 调用账本、Provider Health 统计口径与阈值、UNKNOWN 语义、脱敏 |
| [13_数据流与复用机制.md](13_数据流与复用机制.md) | 四层复用（Hub 缓存 / 调用账本 / PrefetchBundle / 城市知识库）、社媒与高德数据什么时候查、存哪、怎么复用 |
| [14_决策与规划质量优化.md](14_决策与规划质量优化.md) | "LLM 定偏好、Python 保执行"的改造设计稿 + **逐节实现状态表**（哪些已落地、哪些没有） |
| [15_城市知识库攻略正文复用.md](15_城市知识库攻略正文复用.md) | 一次完整的"问题 → 根因 → 方案 → 实现 → 验证"记录：城市知识库如何做到跨会话复用**攻略正文与检索词**（原来只复用了 POI），含 run 级 provenance 重建与仍不复用的部分 |

## 设计稿与台账（不是当前实现的说明书）

这些文档的读者是"想知道当初为什么这么设计"或"想看还剩什么问题"，**不是"现在怎么跑的"**。
每份都在开头标注了自己的性质与失效范围：

| 文件 | 性质 | 注意 |
| --- | --- | --- |
| [PRD.md](PRD.md) | 产品需求文档（设计期） | **代码注释里 176 处 `PRD §x` 指的是它，不要删**。成书早于 Guided，全文没有 Planning Session；`§6`/`§28` 的 app/ 文件数与产物数已过期 |
| [START.md](START.md) | 开发启动说明 / 开工工单 | 13 处代码注释引用（`START.md §x`）。`§7` 的 `app/` 结构、`§11` 的 9 步流程、`§9` 的 3 件产物已过期（现 12 步 / 6 件）；文首"`agent.py` 没有 checkpointer"的结论与代码相反（`app/agent.py` 确实给它配了 checkpointer） |
| [FRONTEND_DESIGN.md](FRONTEND_DESIGN.md) | 前端 v0 设计说明 | 43 处代码注释引用。Hero 文案、结果页布局、`source_url` 展示、修改计划端点这几处已被实现推翻 |
| [ACCEPTANCE.md](ACCEPTANCE.md) | 2026-09-18 的一次性验收**实测记录** | 4 个 run_id 与产物数字快照仍可复现；文首的 commit 数 / `page.tsx` 数 / 测试条数 / `planner.py` 行锚点已过期。原始日志在 `_acceptance/` |
| [TravelPlan_UI_优化方案.md](TravelPlan_UI_优化方案.md) | UI 改造设计稿 + P0/P1/P2 排期 | P1 全部交付；P0 的住宿策略选项、`[选择春熙路]`、美食品类勾选与实现形态不同；见文首核对结果 |
| [TravelPlan_Admin_整体优化方案.md](TravelPlan_Admin_整体优化方案.md) | 管理后台整体优化设计稿（Dashboard / Travel Quality 页 / 信息架构 / Agent 执行时间线） | **已实现**（commit `437aca9`：后端 `app/admin_analytics.py` + `app/admin_timeline.py`，前端 `frontend/app/admin/**`），代码里 3 处主动回指本文。读它了解"管理台为什么长这样"，看现状仍以 `docs/05`/`docs/12` 为准 |
| [目前缺陷.md](目前缺陷.md) | 缺陷台账（A/B/C/D/E/F 六类） | 每条已标注**已修（附证据）/ 仍开**。仍开的主要是 E8（路线查询时机）、C1（模型时延策略）、A7（相邻点路线缓存，待决策） |

## 已归档文档

[docs/discard/](discard/) 里是**曾经有用、现在不再作为事实来源**的文档（"当前架构"快照、
已完成的派工单）。其中未被采纳的 Benchmark / Trace 设计稿与停更的迭代日志已于 2026-09-21 删除
（理由见 [discard/README.md](discard/README.md)），需要原文可 `git show HEAD:docs/discard/<文件名>`。
归档 ≠ 删除，但判断现状请以代码 + `01`~`15` 为准。清单与归档理由见
[docs/discard/README.md](discard/README.md)。
