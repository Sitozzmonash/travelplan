# 开发指南

一句话：怎么把项目跑起来、怎么加一个新东西、提交前必须过哪些检查。

## 1. 环境与启动

要求：Python 3.11+（实测 3.11.11）、Node 18+（实测 24.7.0）。Windows 控制台默认 GBK，
跑脚本时建议带 `PYTHONIOENCODING=utf-8`（输出里有 `¥`）。

```bash
# 1) 依赖
pip install -e "./super_harness[mcp]"
pip install -r requirements.txt

# 2) 途牛 CLI（tuniu_travel 插件通过 subprocess 调它）
npm install -g tuniu-cli          # 装好确认 `tuniu --help` 可用

# 3) 12306 MCP 由 SuperHarness 按需拉起（`npx -y 12306-mcp`），首次会自动下载

# 4) 前端
cd frontend && npm install && cd ..
```

`super_harness` 是 git submodule，**fresh clone 必须初始化**：

```bash
git submodule update --init --recursive
```

`.env` 放在项目根目录（已被 `.gitignore` 忽略），变量清单见 [配置说明](CONFIG.md)。
`.env` 由 SuperHarness 的配置层按**当前工作目录**加载，所以 CLI 要在项目根目录运行。

### 三种跑法

```bash
# CLI（一次完整规划，走 Agent Loop）
python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"

# API（前端与管理端的后端）
uvicorn app.api:api --reload            # http://localhost:8000

# 前端（用户端 http://localhost:3000，管理端 http://localhost:3000/admin）
cd frontend && npm run dev
```

`main.py --route agent` 走的是 SuperHarness Agent Loop 的自由问答分支（返回 messages state，
不是 RunResult），与主规划不是同一条路。

管理端需要后端配了 `TRAVELPLAN_ADMIN_TOKEN`。本地把它写进 `frontend/.env.local`，
打开 `/admin` 会自动填入（服务端路由 `/admin/default-token` 下发，不进浏览器包），不用手输；
Token 最终存在 localStorage（键 `travelplan_admin_token`）。

## 2. 测试

```bash
python -m pytest -q                        # 全部离线
python -m pytest tests/test_planner.py -q  # 领域算法最快的一组
python -m pytest tests/test_agent_runner.py -q   # 主规划路径（Agent Loop）
```

**测试必须离线**。`tests/conftest.py` 会在整个会话期间把第三方密钥从环境里摘掉
（`JEV_API_KEY` / `TYPESAFE_API_KEY` / `MODEL_API_KEY` / `AMAP_API_KEY` / `TIKHUB_API_TOKEN` /
`TUNIU_API_KEY` / `TAVILY_API_KEY` / `TRAVELPLAN_ADMIN_TOKEN` / `DATABASE_URL`）。
其中 `DATABASE_URL` 最关键：不摘掉的话，默认构造的 `TravelPlanStore()` 会去连真 Neon 并在那里
建表，测试既依赖网络、又会**污染线上库**（测试一律用 `tmp_path` 的 SQLite）。

为什么必须这么做：`app.providers` 会经 super_harness 间接执行 `load_dotenv()`，开发机的
`.env` 会被自动加载 —— 不摘掉的话，一次"忘了注入假件"的 run 就会真的去打第三方接口，
测试变得不可重复。需要验证"配置存在时的行为"的用例，自己用 `monkeypatch.setenv` 设值。

离线假件在 `tests/fakes.py`；Benchmark 自带一份（`benchmark/fixtures/world.py`）——
`benchmark/` 与 `tests/` 一样被 `.dockerignore` 排除，评测资产不能依赖测试资产的可用性。
两边的**技巧**是同源的（脚本模型 + 假 Hub + 真的 Agent Loop），避免"单测一套假件、
评测另一套"导致结论无法归因。

主规划路径（Agent Loop）相关的测试文件：

| 文件 | 守什么 |
| --- | --- |
| `tests/test_agent_runner.py` | 交卷 / 未交卷兜底 / 空 days / 超时与步数截断 / 时间兜底 / 模型未配置 |
| `tests/test_travel_tools.py` | 工具参数与返回信封、截断、失败不抛 |
| `tests/test_agent_trace.py` | 每步落 `run_stages.facts_json` / `trace_spans` / `current_stage` 契约 |
| `tests/test_model_fallback.py` | 主 → bk1 → bk2 降级链 |
| `tests/test_preheat_agent.py` | 城市预热 Agent 的入参、失败即不落库、护栏 |

> 评测套件（`benchmark/`）评的是**主规划 Agent Loop 路径**（12 例离线确定性用例），
> 见 [Benchmark](../quality/BENCHMARK.md)。本路径没有 Jev，也没有联网套件。

## 3. 加新东西的步骤

### 加一个旅行工具（让 Agent 能用）

1. 能力本身属于 SuperHarness：在 `super_harness/superharness/capabilities/` 下加 plugin
   或 MCP server（本仓不自制 Tool/MCP 装载器）；
2. 在 `app/providers.py` 的 `ProviderHub` 里加一个方法，返回 `ProviderResult`，
   **必须**经过 `self._record(...)` 把调用写进审计账本（失败的调用也要写）；
3. 缺 Key / 缺二进制 / 超时都要返回 `AUTH_ERROR` / `UNAVAILABLE` / `TIMEOUT` 信封，
   不抛异常、不编数据；
4. 在 `app/travel_tools.py::build_travel_tools` 里包一个 `@tool`：description 写清
   "什么时候用 / 什么时候不用 / 参数 / 返回什么"，返回 `_run(...)` 截断后的 JSON；
5. 在 `app/travel_tools.py::TOOL_DISPLAY_NAMES` 里补中文步骤文案
   （前端进度、admin 步骤视图都读它，漏了会显示「在调用 <工具名>」）；
6. 加测试（参考 `tests/test_travel_tools.py` 的写法，用假 Hub / 假 HTTP / 假 subprocess）。

注意工具可见性：`_AlwaysVisibleTools` 会把 `build_travel_tools` 返回的工具按名字补回模型
可见列表，所以新工具只要进这个列表就能被调用（不需要额外注册）。

### 改主规划行为

主规划的"算法"就是 `app/prompts.py::TRAVEL_PLANNER_SYSTEM_PROMPT`：

1. 改 prompt；
2. **同时改 `TRAVEL_PLANNER_PROMPT_VERSION`**（版本号会进 `trace_spans`、`audit_report.json`
   与 `run_metrics`，否则"改前改后哪一版更好"无法对比）；
3. 跑 `pytest tests/test_agent_runner.py`，并至少实跑几个真实需求看产物。

如果改的是"Python 兜底"（目前只有时间冲突验证，`app/agent_runner.py::_apply_time_pass`），
算法落在 `app/planner.py`，保持纯函数（无网络、无模型、无随机）。

### 加一个 workflow 节点 / 新阶段

**不要走这条路**：主规划不再是固定图，阶段由 Agent 实际调用的工具决定。
"这次 run 在做什么"由 `app/agent_trace.py` 从工具/模型调用实时推导，不靠预先声明的节点。

### 想改数据存储（SQLite / Postgres）

- 表结构与读写方法 → `app/store.py`（**一份 SQL**同时服务 SQLite 与 Postgres）；
- 方言差异（占位符 `?`→`%s`、`INSERT OR REPLACE`→`ON CONFLICT DO UPDATE`、`PRAGMA`→
  `information_schema`、连接池、连接串脱敏）→ `app/db.py`（**唯一改动点**，别在 store 的方法里写
  `if postgres:`）；
- 后端选择 → 环境变量 `DATABASE_URL`：不设走本地 SQLite（`TRAVELPLAN_DB_PATH`），设了走
  Postgres；显式传 `db_path` 时一律 SQLite。连不上 Postgres 会**抛错而不是回退**。详见
  [配置说明](CONFIG.md) §4.1 与 `tests/test_store_postgres.py`。

### 加一个 BadCase 规则

见 [Bad Case](../quality/BAD_CASE.md) 第 6 节。

### 加一个 Benchmark case

见 [Benchmark](../quality/BENCHMARK.md) 与 `benchmark/README.md`。
注意：评测的是主规划 Agent Loop 路径，跑的是脚本模型 + 假 Provider（离线）。

### 加一个可被 Evolution 验证的阈值

1. 放进 `app/config.py:PlannerTuning`（含 `from_env` 读取与默认值）；
2. 在 `app/planner.py` 的调用点用 `tuning().xxx` 而不是模块常量；
3. 在 `app/evolution.py:LEVERS` 里登记一条候选改动（`overrides` 用环境变量名）；
4. 跑 `python -m benchmark` 确认度量能反映它（注意脚本模型按剧本走，它只验证接线与判据）。

### 想改用户旅程 / 会话 / 缓存

- 会话状态机、TTL、create/patch/start、事件时间线 → `app/sessions.py`；
- 后台取数（交通/酒店/攻略/地点）→ `app/discovery.py`（**唯一实现**）；
- 用户点选如何被解释（策略/节奏/POI 分类）→ `app/selection.py`；
- 跨会话攻略 + POI 缓存 → `app/city_cache.py`；地点身份 → `app/places.py`；
- HTTP 形状 → `app/api.py` 的 `planning-sessions` 系列。
- ⚠️ 不要为 Web 再复制一份取数逻辑；也不要把"未确认的 UI 点击"写进正式 Run。

### 想加一个 Provider 观测

- 新数据源接入后，它的调用会经 `ProviderHub.audit_entries()` 自动出现在本次 run 的
  `audit_report.json` 里；
- `provider_calls` **表**的写入方是 Discovery（`app/sessions.py`）与旧流程的 finalize；
  主规划 Agent 路径当前不写（见 [数据源与工具](../architecture/PROVIDERS_TOOLS.md) §4）；
- 页面按 `provider_calls.provider` 动态枚举，**不需要写死 Provider 清单**（只有中文标签
  `app/api.py:PROVIDER_LABELS` 需要时可补）；
- 统计口径与阈值改动在 `app/api.py:_provider_status` / `PROVIDER_SAMPLE_FOR_STATUS`；
  详见 [可观测性](../architecture/OBSERVABILITY.md)。

## 4. 提交前检查清单

```bash
python -m pytest -q                                   # 全绿
python -m benchmark --limit 1                         # 改了 agent_runner / 工具 / 用例才需要
cd frontend && npx tsc --noEmit && npx eslint . && npm run build
```

- [ ] `pytest` 全绿（不许 skip 掉失败的用例来"变绿"）
- [ ] 前端 typecheck / lint / build 全绿
- [ ] 改了 prompt → 同时改了 `TRAVEL_PLANNER_PROMPT_VERSION`，并实跑验证
- [ ] 改了 `app/agent_runner.py` / `app/planner.py` / `app/travel_tools.py` → 跑过 benchmark 并对比 baseline
- [ ] 新增了失败场景 → 加进 `benchmark/cases/badcase_regression.jsonl`
- [ ] 改了 `benchmark/cases` 或 `FIXTURE_VERSION` → 重跑基线（`python -m benchmark`）
- [ ] 没有把 Secret 写进代码 / 文档 / 测试 / 产物
- [ ] 文档与代码一致（改了什么就改对应那篇）

## 5. 代码风格约定

- 中文注释，解释**为什么**（尤其是"以前这么写会出什么问题"），不解释代码字面在做什么；
- 不用 emoji；不留注释掉的死代码；不留 `TODO` 当交付（要么做要么写进文档的"已知未覆盖"）;
- 一个模块只做一件事；跨层依赖只能自上而下（HTTP → 规划/准备 → 领域/能力 → 存储）；
- 领域算法保持纯函数：`app/planner.py` 里不许出现网络调用、模型调用、随机数；
- 降级路径必须显式：失败要么抛（可定位的错误），要么返回带 `status` 的结果并留痕，
  不允许"静默返回空"。

## 6. 排查入口速查

```text
模型行为不对        → app/prompts.py（主规划 = TRAVEL_PLANNER_SYSTEM_PROMPT + 版本号）
阈值/开关不对        → app/config.py（可运行时改的项看 EDITABLE_KEYS）
Agent 步数/超时不对  → MAX_AGENT_STEPS / AGENT_RUN_TIMEOUT_SECONDS / MAX_RUN_TOKENS
模型总是切备用       → app/agent.py::build_model_with_fallbacks，看 .env 的 MODEL_*_bk1/_bk2
Agent 调不到某个工具 → app/travel_tools.py::build_travel_tools 与 TOOL_DISPLAY_NAMES
行程排得不对        → app/planner.py（排程 / 预算 / 可行性）
时间冲突没兜住       → app/agent_runner.py::_apply_time_pass
用户点选不生效      → app/selection.py + app/agent_runner.py::_user_prompt（注入的结构化事实）
引导式会话/取数不对  → app/sessions.py（状态机、TTL）+ app/discovery.py（四条取数线）
数据源返回不对      → app/providers.py，再看 super_harness 的对应能力
"哪个 Provider 不稳" → 管理端 /admin/providers（provider_calls 聚合，无历史=UNKNOWN）
"用户为什么没走到 Run"→ 管理端 /admin/sessions（Discovery 分阶段状态 + 事件时间线）
看不到 run 的过程    → 管理端 /admin/runs/{id}（Agent 步骤轨迹 / Trace / LLM 调用 / Bad Cases）
某类失败反复出现     → 管理端 /admin/badcases 按 category 过滤 → /admin/evolution
"这次改动有没有变差" → python -m benchmark --compare-baseline（12 例离线确定性用例）
```
