# TravelPlan

TravelPlan 是面向中国国内旅行的可追溯规划系统：先检索真实交通、酒店、门票、攻略和地点数据，
再按预算、时间、偏好与可行性生成行程。它不会用模型补造价格、车次、营业时间或路线；
数据不足会明确披露降级原因。

## 核心能力

- **主规划是一个 Agent Loop**：SuperHarness 的 Agent 在循环里自己决定查什么
  （火车 / 机票 / 酒店 / 门票 / 高德 POI 与路线 / 小红书 / 抖音 / 网页），
  自己控制预算、时间与合理性，最后调 `submit_final_plan` 交卷；
- Guided 三步旅程（基础信息 → 探索确认 → 偏好并开始规划）与 Quick 一句话入口，
  两条入口共用同一个执行体；
- Python 只保留**时间冲突兜底**（返程赶不上、时间重叠）；其余约束写进 system prompt，
  prompt 带版本号并写进 trace / audit / metrics，便于"哪一版更好"的对比；
- 真实的来源、查询时间、每一步的工具与 token、Trace、Bad Case 与 Benchmark；
- 城市级半静态攻略 / POI 复用（城市知识库，可离线预热到同一张表），
  实时机酒火数据不跨会话缓存；
- 用户端与受 Token 保护的管理后台。

## 架构一句话

`app/api.py` 只做 HTTP 翻译 → `app/agent.py` 装配 → **`app/agent_runner.py::execute_agent_run`
跑 Agent Loop**（工具来自 `app/travel_tools.py`，prompt 来自 `app/prompts.py`）→
`app/planner.py` 做最后的时间可行性兜底 → 复用 `app/workflow.py` 的产物与落库管线。
详见 [主规划：Agent Loop](docs/architecture/AGENT_LOOP.md) 与 [架构总览](docs/architecture/OVERVIEW.md)。

## 快速开始

要求 Python 3.11+、Node 18+。

```bash
git submodule update --init --recursive
pip install -e "./super_harness[mcp]"
pip install -r requirements.txt
npm install -g tuniu-cli          # 途牛 CLI（tuniu_travel 插件通过 subprocess 调它）
cd frontend && npm install && cd ..

# 后端
uvicorn app.api:api --reload      # http://localhost:8000

# 另开一个终端运行前端
cd frontend && npm run dev        # 用户端 :3000，管理端 :3000/admin
```

命令行跑一次规划：

```bash
python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"
```

测试（全部离线）：

```bash
python -m pytest -q
```

## 配置

本地配置写在项目根目录 `.env`（已被 `.gitignore` 忽略）；Secret 只能从环境变量读取。
完整变量清单见 [配置说明](docs/operations/CONFIG.md)。

最少要配的模型三件套（缺任意一件 Agent 规划无法开始）：

```dotenv
MODEL_NAME=...
MODEL_BASE_URL=...
MODEL_API_KEY=...
```

数据源 Key（按需要配；缺 Key 时对应能力如实报 `AUTH_ERROR`，不会编数据）：

```dotenv
AMAP_API_KEY=...        # 高德：POI / 详情 / 路线
TUNIU_API_KEY=...       # 途牛：机票 / 酒店 / 门票 / 火车备选
TIKHUB_API_TOKEN=...    # 小红书 / 抖音（只走免费额度）
TAVILY_API_KEY=...      # 网页检索
```

### 模型降级链（主 → bk1 → bk2）

主模型限流 / 欠费 / 宕机时按顺序切换备用端点，**抛错即降级**（不在同一条坏路上重试）。
一套必须三项齐全，缺一项就整套跳过：

```dotenv
MODEL_NAME_bk1=...  MODEL_BASE_URL_bk1=...  MODEL_API_KEY_bk1=...
MODEL_NAME_bk2=...  MODEL_BASE_URL_bk2=...  MODEL_API_KEY_bk2=...
```

一个都没配全 → 本次 run 直接 FAILED，并明确告诉你缺什么。

### Agent 护栏

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MAX_AGENT_STEPS` | `40` | Agent 循环最大步数。超限时：没交卷就 FAILED，交了卷就保留行程并标降级 |
| `AGENT_RUN_TIMEOUT_SECONDS` | `600` | 一次 Agent 规划的墙钟上限（秒），超时同样保留已交卷的行程 |
| `MAX_RUN_TOKENS` | 不设 | 累计 token 上限，在每次模型调用前检查；超了就停循环 |

这两个护栏与预算限制都能在管理端 Config 页运行时改，无需重启（白名单见 `EDITABLE_KEYS`）。

### 部署相关的两个变量

```dotenv
DATABASE_URL=postgresql://...   # 设了走 Postgres/Neon；不设走本地 SQLite
TRAVELPLAN_ADMIN_TOKEN=...      # 不配则所有 /api/v1/admin/** 返回 503
```

部署架构与验证方式见 [部署说明](docs/operations/DEPLOYMENT.md)。

## 文档入口

从 [docs/README.md](docs/README.md) 按产品、架构、质量、运维和路线图进入。
历史材料位于 `docs/archive/`；问题、缺陷和修复记录位于 `feedback/`。
