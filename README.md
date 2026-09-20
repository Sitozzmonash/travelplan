# TravelPlan

中国国内旅行规划 Agent。基于 [SuperHarness](https://github.com/Sitozzmonash/super_harness)（git submodule）构建，**Evidence First, LLM Second**。

它不"让 LLM 凭空写一份攻略"，而是：

```text
用户自然语言需求
→ 查真实机票 / 高铁 / 酒店 / 景点门票
→ 搜小红书 / 抖音真实攻略
→ 高德验证地点与计算路线
→ 过滤广告与低可信内容
→ 比较价格与门到门时间
→ 检查预算与时间可行性
→ 生成每天行程（多份候选里由 Jev 选整体更好的那份）
→ 输出「来源 / 实时价格 / 查询时间 / 完整决策记录 / Bad Case」
```

Web 端默认走**引导式（Guided）**：先填基础信息 → 系统在后台把真实交通/酒店/攻略/POI 查好 →
用户用极少的选择表达偏好（交通/酒店策略、必去/想去/不感兴趣、节奏）→ 点「开始规划」才创建正式
run，并复用已查到的候选。CLI 与老用户仍可走**一句话（Quick）**入口。两条入口最终是同一个 12 步引擎。

核心红线：

- **没有真实数据支撑的价格、车次、航班、营业时间、路线，不允许模型编造。**
- Tool 失败必须如实记为 `status` + `provider` + `reason` + `fallback`，**不允许假数据兜底**。
- 排不下的点**不排**（例如闭园后的参观），而不是"报个错但仍写进行程"。
- Secret 只从环境变量读，不写入代码 / Prompt / 日志 / 输出文件 / API 响应。
- V1 只读：不实现下单、支付、购票、抢票。
- TikHub 只允许免费额度（`FREE_ONLY` 是常量），额度不可确认时直接 fallback，不消耗现金余额。

**文档入口：[`docs/README.md`](docs/README.md)**（"想改 X 该动哪"的定位表 + 全部机制说明）。

---

## 1. 代码边界

| 位置 | 属于谁 | 内容 |
| --- | --- | --- |
| `super_harness/` | SuperHarness（独立仓库，submodule） | Harness 运行时 + 5 个能力：`tuniu_travel`、`railway_12306`(MCP)、`amap_cn`、`tikhub_social`、`mediacrawler_social` |
| `app/` | TravelPlan | 旅行领域逻辑：模型、Store、Planner、固定 Workflow、引导式 Session/Discovery、用户选择层、Jev 决策、BadCase、Evolution、API |
| `benchmark/` | TravelPlan | 离线评测套件（33 例确定性用例 + 5 例 Live Smoke + Jev OFF/ON 基线） |
| `frontend/` | TravelPlan | 用户端 + 管理端，不含业务判断，不持有任何 Provider Secret |
| `docs/` | TravelPlan | 架构/配置/流程/数据源/Trace/BadCase/评测/进化/部署/开发指南 + 用户旅程与 Provider 健康 |

**没有自制的 Router / Plugin Loader / MCP Client / Memory / RAG / Retry / Trace** —— 这些一律复用 SuperHarness。

## 2. 目录

```text
travelplan/
├─ super_harness/          # Git submodule（含 5 个能力插件）
├─ app/
│  ├─ api.py               # FastAPI（`app.api:api`）：用户接口 + 管理接口
│  ├─ agent.py             # 装配：system prompt / HarnessContext / 12306 MCP / Workflow
│  ├─ sessions.py          # Planning Session：引导式旅程的会话状态机 + 后台 Prefetch
│  ├─ discovery.py         # 取数逻辑唯一实现（交通/酒店/攻略/地点），Session 与 Workflow 共用
│  ├─ selection.py         # 用户选择层：策略→权重、Pace auto、MUST/WANT/REJECT
│  ├─ workflow.py          # 固定 12 步规划流程（LangGraph）+ 产物写出 + Prefetch 复用
│  ├─ planner.py           # 去重、Trust、Ad Risk、打分、聚类、排程、预算、约束、质量度量、Top-K
│  ├─ store.py             # SQLite：run/source/evidence/place/decision/plan/trace/badcase/benchmark/evolution + planning_sessions/provider_calls/runtime_config
│  ├─ providers.py         # ProviderHub：5 个能力统一成「带 provenance 的调用」
│  ├─ llm.py               # 模型调用与降级
│  ├─ prompts.py           # 5 个提示词
│  ├─ badcase.py           # Bad Case 规则引擎（含用户旅程 8 类）
│  ├─ evolution.py         # Evolution：Bad Case → 经验
│  ├─ observability.py     # Run/Span 状态与 span 分类
│  ├─ config.py            # 集中配置 + 可运行时覆盖的 EDITABLE_KEYS
│  ├─ version.py           # 版本信息（Trace / Baseline 要记是哪一版跑的）
│  └─ decision/            # Jev 适配层 + 三处软决策（方案选择 / Trade-off / 质量门）
├─ benchmark/              # 评测套件（cases / fixtures / baselines / results）
├─ frontend/               # Next.js 16 + React 19 + Tailwind 4（用户端 + /admin 管理端）
├─ tests/                  # pytest（全部离线）
├─ docs/                   # 文档（见 docs/README.md）
├─ outputs/<run_id>/       # plan.json / plan.md / audit_report.json / trace.jsonl / metrics.json / badcases.json
├─ Dockerfile              # API 容器镜像（含 Node，供 12306 MCP）
└─ main.py                 # CLI
```

## 3. 环境准备

要求：Python 3.11+、Node 18+（实测 Python 3.11.11 / Node 24.7.0）。

```bash
# 0) 子模块（fresh clone 必须做）
git submodule update --init --recursive

# 1) SuperHarness 本体（含 MCP 依赖）
pip install -e "./super_harness[mcp]"
pip install -r requirements.txt

# 2) 途牛 CLI（tuniu_travel Plugin 通过 subprocess 调它）
npm install -g tuniu-cli      # 装好确认 `tuniu --help` 可用

# 3) 12306 MCP（由 SuperHarness 的 MCPLoader 按需拉起，不需要常驻）
#    默认命令是 `npx -y 12306-mcp`，首次运行会自动下载。

# 4) 前端
cd frontend && npm install && cd ..
```

### `.env`（项目根目录，已被 `.gitignore` 忽略）

```ini
MODEL_NAME=...
MODEL_BASE_URL=...
MODEL_API_KEY=...
TAVILY_API_KEY=...        # SuperHarness 自带 web_search
AMAP_API_KEY=...          # 高德 Web Service（后端 Secret，绝不进前端）
TIKHUB_API_TOKEN=...      # 小红书 / 抖音，只走免费额度
TUNIU_API_KEY=...         # 机票 / 酒店 / 火车 / 门票
JEV_API_KEY=...           # 三处软决策（未配置则自动降级，不影响主流程）
TRAVELPLAN_ADMIN_TOKEN=...  # 管理端 Bearer Token（不配则 /api/v1/admin/** 一律 503）
```

可选覆盖：`RAILWAY_12306_COMMAND`、`TUNIU_COMMAND`、`MEDIACRAWLER_DIR`、`TRAVELPLAN_DB_PATH`、
`TRAVELPLAN_OUTPUT_DIR`、`TRAVELPLAN_CORS_ORIGINS`、`TRAVELPLAN_LLM_TIMEOUT_SECONDS`、
`JEV_*`、`TP_*`、`MODEL_PRICE_*`、`DISCOVERY_*`、`PLANNING_SESSION_TTL_MINUTES`、
`BADCASE_ENABLED`、`EVOLUTION_ENABLED`。完整清单见
[`docs/02_配置说明.md`](docs/02_配置说明.md)（含默认值与风险）。管理端非 Secret 项也可在
`/admin/config` 页面运行时修改（落库、重启后仍在）。

## 4. 运行

### 4.1 CLI

```bash
python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"
```

参数：`--user-id` / `--thread-id` / `--project-id`（默认 `travelplan`）/ `--output-dir` / `--route` / `--debug`。
`--route agent` 走 SuperHarness Agent Loop（自由追问），返回对话消息而不是 plan。
退出码：`0` 成功，`1` 失败，`2` 需要用户补充信息（没识别出目的地城市）。

### 4.2 FastAPI

```bash
uvicorn app.api:api --reload
```

用户侧：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 只报「配了没配」与库是否可用，不返回值 |
| POST | `/api/v1/plans` | `{"message": "...", "background": true}` → 202 `{run_id, status}` |
| GET | `/api/v1/plans/{run_id}/status` | 真实阶段进度（`RUNNING/SUCCESS/DEGRADED/FAILED/CANCELLED`） |
| GET | `/api/v1/plans/{run_id}` | 计划（含 `evidence[]` 与 `evidence_summary`） |
| GET | `/api/v1/plans/{run_id}/audit` | 规划依据（stages / decisions / provider_calls / llm_calls / jev_calls / timeline） |
| POST | `/api/v1/plans/{run_id}/revise` | V1 只登记请求，不重新规划（见 §6） |
| GET | `/api/v1/plans/{run_id}/artifacts/{filename}` | 下载六件产物（白名单） |
| POST | `/api/v1/planning-sessions` | 引导式：建会话并后台 Prefetch（202 `{session_id, status, discovery_status}`） |
| GET | `/api/v1/planning-sessions/{id}` | 引导式：会话视图（偏好 / POI 候选 / 逐阶段 discovery / degradations / events） |
| PATCH | `/api/v1/planning-sessions/{id}` | 引导式：改偏好与 POI（白名单字段，只重排序、不重取数） |
| DELETE | `/api/v1/planning-sessions/{id}` | 引导式：取消会话 |
| POST | `/api/v1/planning-sessions/{id}/start` | 引导式：创建正式 run（202 `{run_id, status}`；取消/过期 → 409） |

管理侧（全部需要 `Authorization: Bearer $TRAVELPLAN_ADMIN_TOKEN`）：

```text
GET   /api/v1/admin/overview              摘要（含 Jev 健康、BadCase 计数、最新 Benchmark）
GET   /api/v1/admin/runs                  运行列表（status 过滤 + q 搜索 + include_benchmark 默认 false + total）
GET   /api/v1/admin/runs/{run_id}         Trace / decisions / provider / jev_calls / llm_calls / badcases
                                          + user_journey（来源会话 / 策略 / MUST·WANT·REJECT / prefetch_reused）
GET   /api/v1/admin/runs/{run_id}/stages  阶段明细（steps / facts / 该阶段 tool_calls 与 llm_calls / tokens）
GET   /api/v1/admin/planning-sessions     Session 列表（status / destination / has_run / q 筛选 + total）
GET   /api/v1/admin/planning-sessions/{id} Session 详情信封
                                          {session, basic_info, preferences, discovery, prefetch_summary, poi_selections, events, run_link}
GET   /api/v1/admin/providers             Provider Health（per-provider 卡片 + summary；无历史 = UNKNOWN）
GET   /api/v1/admin/providers/{provider}  某 Provider 最近调用明细（source_type / session_id / run_id；query 已脱敏）
GET   /api/v1/admin/badcases              列表 + facets
GET   /api/v1/admin/badcases/{id}        详情（trace_refs 展开成真实 span）
PATCH /api/v1/admin/badcases/{id}        更新 analysis/fixed/root_cause 状态
GET   /api/v1/admin/benchmark/runs        Benchmark 列表
GET   /api/v1/admin/benchmark/runs/{id}   六维指标 + 逐 case 结果 + Jev OFF/ON 对照
POST  /api/v1/admin/benchmark/runs        触发一轮 Benchmark（202 + 轮询）
GET   /api/v1/admin/config                配置 + Planner 阈值 + 杠杆表 + 可运行时改的白名单（Secret 只报配没配）
PATCH /api/v1/admin/config                改非 Secret 的运行时配置（立即生效、落库、重启后仍在）
GET   /api/v1/admin/jev/health            Jev 状态/延迟/额度(or unknown)/fallback 计数
GET   /api/v1/admin/evolution             待分析 BadCase + 历史 run + 可用杠杆
GET   /api/v1/admin/evolution/runs/{id}   某轮演进的经验与来源 BadCase
POST  /api/v1/admin/evolution/runs        触发 Evolution（需 EVOLUTION_ENABLED=true）
```

### 4.3 前端

```bash
cd frontend && npm run dev      # 用户端 http://localhost:3000，管理端 /admin
```

默认走真实后端：

```ini
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_USE_MOCK_API=false
```

管理端在页面里填 `TRAVELPLAN_ADMIN_TOKEN`（存 localStorage，键 `travelplan_admin_token`）：
没有 Token 时页面只显示 Token 输入框，拿不到任何 Trace / Config / Evolution 数据。
`NEXT_PUBLIC_USE_MOCK_API=true` 可切到内置演示数据（不连后端）。

## 5. Workflow

两条入口，**同一个 12 步最终引擎**：

```text
Guided（Web 默认）：基础信息 → Planning Session → 后台 Discovery/Prefetch → 选偏好/POI → 确认 → 正式 Run
Quick（保留）    ：一句自然语言 → intent parse → 直接完整 Workflow
```

Guided 在正式 run 里**复用** Prefetch：② 交通 / ③ 酒店 / ④ 攻略 / ⑤ 地点 四个节点直接消费
Discovery 已查到的候选（`app/workflow.py:_reuse_result` 把候选包成与 `ProviderResult` 同形），
不再重复打 Provider；偏好只重新排序。两条入口都汇聚到 `execute_travel_run`，没有第二套 Planner。
详见 [`docs/11_用户旅程与PlanningSession.md`](docs/11_用户旅程与PlanningSession.md)。

固定 12 步，唯一分支是「没识别出目的地就直接停下并追问」：

```text
parse_intent → search_intercity_transport → search_hotels → search_social_guides
→ extract_and_normalize_places → verify_poi_and_routes → score_candidates
→ build_initial_plan → check_budget → check_feasibility → critic_and_revise → finalize
```

模型只在 5 处被调用：意图解析、检索词扩展、地点抽取、Critic、成文。
**所有数字（价格、时刻、时长、预算、可信度）都由代码计算并原样写入产物，不经过模型改写。**

Jev（软决策）只在 3 处参与：`build_initial_plan` 的 Top-K 方案选择、`critic_and_revise` 的
Trade-off 与质量门。任何失败都只是降级，行程照常产出。详见
[`docs/03_项目流程图.md`](docs/03_项目流程图.md) 与 [`docs/04_数据源与工具.md`](docs/04_数据源与工具.md)。

## 6. Provider 与 fallback

| 需求 | 主 | 备 |
| --- | --- | --- |
| 火车 / 高铁 | 12306 MCP | 途牛 train |
| 机票 / 酒店 / 门票 | 途牛 | — |
| 小红书 / 抖音 | TikHub（免费额度） | MediaCrawler |
| POI / 市内路线 | 高德 | — |
| 通用网页 | SuperHarness Tavily `web_search` | — |

降级一律如实记录在 `audit_report.json` 的 `provider_calls[].status` 与 `degradations[]` 里，
并由 `app/badcase.py` 转成可复核的 Bad Case。

每次调用还会写进**观测账本** `provider_calls`（无 runs 外键，因此能记录还没正式 run 的
Discovery 调用）。管理端 `/admin/providers` 用真实调用聚合出每个 Provider 的状态 / 成功率 /
P95 延迟 / fallback；**没有调用历史显示 UNKNOWN 而不是失败，也不主动打第三方接口（不做假探活）**。
详见 [`docs/12_Provider健康与观测.md`](docs/12_Provider健康与观测.md)。

## 7. 已知限制

- `POST /revise` 是 V1 预留接口：只做参数校验与登记，返回 `queued` 并明确告知「页面上的行程不会变化」，**不会假装修好了**。真正的修改需要重跑完整流程。
- MediaCrawler 是 fallback，需要本地 `data/vendors/MediaCrawler` 可用；目录不存在时返回 `UNAVAILABLE` 并给出说明。
- TikHub 免费额度耗尽时小红书/抖音会降级，此时 Trust 的「多来源证据」分项会偏低，Critic 置信度相应下调。
- 门到门时长里的机场/车站接驳，在未取得高德真实路线时按具名常量估算，并在 `selection_reason` 里注明。
- 没有出发日期时查不到大交通：系统会如实告警并留下 Bad Case（`missing_disclosure`），**不猜日期**。
- 后端不能部署到 Vercel Serverless：见 [`docs/09_部署说明.md`](docs/09_部署说明.md)（附 Vercel 官方限制数字与实测记录）。

## 8. 测试与评测

```bash
# 单元 / 集成测试（离线，869 条，约 1.5 分钟）
python -m pytest -q

# 引导式旅程专项（Selection / Discovery / Session；管理端 Session 与 Provider Health）
python -m pytest tests/test_guided_journey.py tests/test_admin_observability.py -q

# Benchmark：33 例确定性用例 + 5 例 Live Smoke（离线，可复现）
python -m benchmark.runner --jev on            # 生成/更新 Jev ON 基线
python -m benchmark.runner --jev off           # 生成/更新 Jev OFF 基线
python -m benchmark.runner --suite basic --limit 4   # 冒烟

# 前端
cd frontend && npx tsc --noEmit && npx eslint . && npm run build
```

改了 `benchmark/cases` 或 `FIXTURE_VERSION` 时，**两份基线都要重跑**（只更新一边，对比就是在拿两个世界比）。

测试**必须离线**：`tests/conftest.py` 会摘掉第三方密钥，避免单测真的去打第三方接口
（开发机 `.env` 会被 super_harness 自动加载）。详见
[`docs/10_开发与修改指南.md`](docs/10_开发与修改指南.md)。

Windows 控制台默认 GBK，脚本输出里有 `¥`，**必须**带 `PYTHONIOENCODING=utf-8`，
否则 `UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'`。

验收用的三个脚本（只读已有产物 / 只打真实 Provider，不碰业务库以外的东西）：

```bash
PYTHONIOENCODING=utf-8 python scripts/verify_run.py <run_id>     # 一次 run 的 32 项验收检查
PYTHONIOENCODING=utf-8 python scripts/case_summary.py --all      # PRD §36.4 需要的 E2E 字段
PYTHONIOENCODING=utf-8 python scripts/provider_smoke.py          # Provider 探活 + 降级链路
```

四个验收 case 的串行重跑（自带单实例锁，日志落 `_acceptance/<name>.log`，一轮约 70 分钟；
**不要并发跑**，会互相抢 Provider 配额、把限流误报成故障）：`bash _acceptance_runs.sh`

---

验收标准见 `docs/PRD.md` §35，历史验收记录见 `docs/ACCEPTANCE.md`。
当前实现与修改指南见 [`docs/README.md`](docs/README.md)。
