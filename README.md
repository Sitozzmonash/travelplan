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
→ 生成每天行程
→ 输出「来源 / 实时价格 / 查询时间 / 完整决策记录」
```

核心红线：

- **没有真实数据支撑的价格、车次、航班、营业时间、路线，不允许模型编造。**
- Tool 失败必须如实记为 `status=unavailable` + `provider` + `reason` + `fallback`，**不允许假数据兜底**。
- Secret 只从环境变量读，不写入代码 / Prompt / 日志 / 输出文件。
- V1 只读：不实现下单、支付、购票、抢票。
- TikHub 只允许免费额度（`FREE_ONLY` 是常量），额度不可确认时直接 fallback，不消耗现金余额。

---

## 1. 代码边界

| 位置 | 属于谁 | 内容 |
| --- | --- | --- |
| `super_harness/` | SuperHarness（独立仓库，submodule） | Harness 运行时 + 5 个通用能力：`tuniu_travel`、`railway_12306`(MCP)、`amap_cn`、`tikhub_social`、`mediacrawler_social` |
| `app/` | TravelPlan | 只做旅行领域逻辑：模型、Store、Planner、固定 Workflow、装配、API |
| `frontend/` | TravelPlan | 展示层，不含业务判断，不持有任何 Provider Secret |

**没有自制的 Router / Plugin Loader / MCP Client / Memory / RAG / Retry / Trace** —— 这些一律复用 SuperHarness。

## 2. 目录

```text
travelplan/
├─ super_harness/          # Git submodule
│  └─ superharness/capabilities/
│     ├─ plugins/{tuniu_travel,amap_cn,tikhub_social,mediacrawler_social}
│     └─ mcp/railway_12306.py
├─ app/
│  ├─ agent.py             # 装配：system prompt / HarnessContext / create_harness_app / 12306 MCP / Workflow
│  ├─ workflow.py          # 固定 12 步规划流程（LangGraph）
│  ├─ models.py            # TripIntent / FlightOption / TrainOption / HotelOption / Place / …
│  ├─ planner.py           # 去重、Trust、Ad Risk、比价、预算、时间与路线约束、行程生成
│  ├─ store.py             # SQLite 证据库：run / source / evidence / place / decision / plan
│  ├─ providers.py         # ProviderHub：把 5 个能力统一成「带 provenance 的调用」
│  ├─ prompts.py           # 5 个 LLM 提示词
│  └─ api.py               # FastAPI（`app.api:api`）
├─ frontend/               # Next.js 16 + React 19 + Tailwind 4
├─ tests/                  # pytest
├─ outputs/<run_id>/       # plan.json / plan.md / audit_report.json
└─ main.py                 # CLI
```

## 3. 环境准备

要求：Python 3.11+、Node 18+（本项目实测 Python 3.11.11 / Node 24.7.0）。

```bash
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

> ⚠️ **重要：仓库当前不完整，fresh clone 跑不起来。** 本仓库把 `super_harness` 子模块
> 钉在 `2c2c51a3`，但 §1 表格里那 5 个 capability（`tuniu_travel` / `railway_12306` /
> `amap_cn` / `tikhub_social` / `mediacrawler_social`）**不在那个 commit 里** ——
> 它们目前以未提交文件的形式存在于 `super_harness/` 的工作区（`superharness/capabilities/plugins/*`
> 与 `superharness/capabilities/mcp/railway_12306.py`，外加 `mcp/__init__.py` 的注册）。
> 因此 `git clone` 后执行 `git submodule update --init` 只会拿到没有 capability 的空壳，
> 项目无法启动。
>
> 想让全新 clone 可用，需要先把这些 capability commit 进你的 `super_harness` fork、
> 再把本仓库的子模块指针顶到那个新 commit。在此之前，只有保有这份本地工作区的机器能跑，
> 或手动把上述文件补回 `super_harness/`。

### `.env`（项目根目录，已被 `.gitignore` 忽略）

```ini
MODEL_NAME=...
MODEL_BASE_URL=...
MODEL_API_KEY=...
TAVILY_API_KEY=...        # SuperHarness 自带 web_search
AMAP_API_KEY=...          # 高德 Web Service（后端 Secret，绝不进前端）
TIKHUB_API_TOKEN=...      # 小红书 / 抖音，只走免费额度
TUNIU_API_KEY=...         # 机票 / 酒店 / 火车 / 门票
```

可选覆盖：`RAILWAY_12306_COMMAND`、`TUNIU_COMMAND`、`MEDIACRAWLER_DIR`、`TRAVELPLAN_OUTPUT_DIR`、`TRAVELPLAN_CORS_ORIGINS`。

> `TRAVELPLAN_LLM_TIMEOUT_SECONDS`（默认 180）是单次模型调用的墙钟上限。推理模型偏慢时
> 可以调大；调大只是给模型更多时间，调小会让更多调用降级到规则路径 —— 无论哪种，
> 降级都会如实写进 `audit_report.json` 的 `llm_calls` 与 `degradations`。

> `.env` 由 SuperHarness 的配置层按**当前工作目录**加载，所以 CLI 请在项目根目录运行。

## 4. 运行

### 4.1 CLI

```bash
python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"
```

参数：`--user-id` / `--thread-id` / `--project-id`（默认 `travelplan`）/ `--output-dir` / `--route` / `--debug`。

`--route agent` 走 SuperHarness Agent Loop，用于自由追问（「这个行程哪里不合理」），返回的是对话消息而不是 plan。

退出码：`0` 成功，`1` 失败，`2` 需要用户补充信息（没识别出目的地城市）。

### 4.2 FastAPI

```bash
uvicorn app.api:api --reload
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 只报「配了没配」，不返回值 |
| POST | `/api/v1/plans` | `{"message": "..."}` → `{run_id, status, plan}` |
| GET | `/api/v1/plans/{run_id}` | 计划（含 `evidence[]` 与 `evidence_summary`） |
| GET | `/api/v1/plans/{run_id}/audit` | 规划依据（stages / decisions / provider_calls / timeline） |
| POST | `/api/v1/plans/{run_id}/revise` | V1 只登记请求，不重新规划（见 §7） |
| GET | `/api/v1/plans/{run_id}/artifacts/{filename}` | 下载三件产物 |

### 4.3 前端

```bash
cd frontend && npm run dev      # http://localhost:3000
```

默认 `NEXT_PUBLIC_USE_MOCK_API=true`，用内置演示数据；接真实后端：

```ini
NEXT_PUBLIC_USE_MOCK_API=false
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

## 5. Workflow（PRD §25）

固定 12 步，唯一分支是「没识别出目的地就直接停下并追问」，不做自由发挥：

```text
parse_intent → search_intercity_transport → search_hotels → search_social_guides
→ extract_and_normalize_places → verify_poi_and_routes → score_candidates
→ build_initial_plan → check_budget → check_feasibility → critic_and_revise → finalize
```

模型只在 5 个地方被调用：意图解析、检索词扩展、地点抽取、Critic、成文。
**所有数字（价格、时刻、时长、预算、可信度）都由代码计算并原样写入产物，不经过模型改写。**

## 6. Provider 与 fallback

| 需求 | 主 | 备 |
| --- | --- | --- |
| 火车 / 高铁 | 12306 MCP | 途牛 train |
| 机票 / 酒店 / 门票 | 途牛 | — |
| 小红书 / 抖音 | TikHub（免费额度） | MediaCrawler |
| POI / 市内路线 | 高德 | — |
| 通用网页 | SuperHarness Tavily `web_search` | — |

降级一律如实记录在 `audit_report.json` 的 `provider_calls[].status` 与 `degradations[]` 里。

## 7. 已知限制

- `POST /revise` 是 V1 预留接口：只做参数校验与登记，返回 `queued` 并明确告知「页面上的行程不会变化」，**不会假装修好了**。真正的修改需要重跑完整流程。
- MediaCrawler 是 fallback，需要本地 `data/vendors/MediaCrawler` 可用；目录不存在时返回 `UNAVAILABLE` 并给出说明。
- TikHub 免费额度耗尽时小红书/抖音会降级，此时 Trust 的「多来源证据」分项会偏低，Critic 置信度相应下调。
- 门到门时长里的机场/车站接驳，在未取得高德真实路线时按具名常量估算，并在 `selection_reason` 里注明。

## 8. 测试

```bash
# 单元 / 集成测试（716 条，不联网）
pytest -q
```

Windows 控制台默认 GBK，脚本输出里有 `¥`，**必须**带 `PYTHONIOENCODING=utf-8`，
否则 `UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'`。

验收用的三个脚本（都只读已有产物 / 只打真实 Provider，不碰业务库以外的东西）：

```bash
# 一次 run 的 32 项验收检查（provenance / 预算 / 时间线 / 产物 / 诚实性）
PYTHONIOENCODING=utf-8 python scripts/verify_run.py <run_id>
PYTHONIOENCODING=utf-8 python scripts/verify_run.py --latest bj_cd   # 从 _acceptance/bj_cd.log 取 run_id

# 生成 PRD §36.4 需要的 E2E 结果字段
PYTHONIOENCODING=utf-8 python scripts/case_summary.py <run_id>
PYTHONIOENCODING=utf-8 python scripts/case_summary.py --all

# Provider 探活（§36.3 的表）+ 降级链路验证（§36.5 的三项）
PYTHONIOENCODING=utf-8 python scripts/provider_smoke.py
PYTHONIOENCODING=utf-8 python scripts/provider_smoke.py --with-fallback
```

四个验收 case 的串行重跑（自带单实例锁，日志落 `_acceptance/<name>.log`，
一轮约 70 分钟；**不要并发跑**，会互相抢 Provider 配额、把限流误报成故障）：

```bash
bash _acceptance_runs.sh
```

---

验收标准见 `docs/PRD.md` §35，验收报告见 `docs/ACCEPTANCE.md`。
开发约定见 `docs/START.md`、`docs/PRD.md`、`docs/FRONTEND_DESIGN.md`。
