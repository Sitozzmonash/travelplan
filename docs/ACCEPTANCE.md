# 验收报告（PRD §35 / §36）

> 本文件是**实测记录**，不是设计文档。每一行都能追溯到一次真实运行或一条可复现的命令；
> 没测到的项一律写「未验证」并给出原因，不写「应该没问题」。
>
> 核验日期：2026-09-18 20:00 ~ 2026-09-19 00:20（跨零点，产物时间戳以 run_id 为准）。
> 验收口径见 `docs/PRD.md` §35 / §36。

---

> ⚠️ **这是一次性验收的实测快照（2026-09-18 ~ 09-19），此后没有再更新。**
> 里面的 **run_id、产物字节数、`verify_run 32/32`、逐阶段耗时**都是当时的真实数据，仍然可以
> 用 `_acceptance/` 下的原始日志复现，是性能结论（`_acceptance/perf_report.md`）的唯一出处。
>
> 但下列**派生数字**已过期，引用时请注明"当时快照"：
>
> - `git submodule status` 的 `2c2c51a3` → 现为 `d013ee8`；本文写作时仓库"尚无提交"，现已有 27 个 commit。
> - "pytest Passed: 716" / `_acceptance/pytest_final.log` 的 964 passed → 当前
>   `pytest --collect-only` 收集 **996** 条（口径不同：前者含 parametrize 展开与当时的用例集）。
> - "next build 4 条路由" → 现为 13 个 `page.tsx`（含 `/guided` 与 10 个 `/admin/*`）。
> - `frontend/app/plan/[id]/loading.tsx` 已不存在（该目录只有 `page.tsx` 与 `error.tsx`）。
> - 所有 `app/planner.py:NNNN` 行锚点已系统性偏移 +20 ~ +130 行（如 `check_feasibility` 1931 → 2063）。
>
> **当前怎么跑的**见 [05](05_Trace与可观测性.md) / [06](06_BadCase机制.md) / [07](07_评测体系.md) /
> [12](12_Provider健康与观测.md)；**恢复命令**见文末"复现方式"。

---

# §35 验收标准

## A. 框架

| # | 标准 | 结论 | 证据 |
|---|---|---|---|
| A1 | `super_harness` 是 Git Submodule | ✅ | `.gitmodules` 声明 `super_harness` → `https://github.com/Sitozzmonash/super_harness.git`；`git submodule status` = `2c2c51a3... super_harness (heads/main)`，无 `+`/`-` 前缀即与记录 commit 一致 |
| A2 | TravelPlan 未复制 SuperHarness Runtime | ✅ | `app/` 内不存在 `HybridRunner` / `WorkflowSpec` / `build_sequence_workflow` 的定义，只 import：`app/agent.py` 用 `create_harness_app`、`app/providers.py` 用 `PluginLoader`/`MCPLoader`、`app/llm.py` 用 `create_model` |
| A3 | Plugin Auto Discovery 生效 | ✅ | 实测 `PluginToolSet().plugin_names` = `['amap_cn','mediacrawler_social','secops_kit','tikhub_social','tuniu_travel']` —— 5 个 manifest 全部由 `super_harness/superharness/capabilities/plugins` 自动扫描得到（含本项目用不到的 `secops_kit`，说明扫描确实是通配而不是按名单）。TravelPlan 只声明要用哪 4 个（`PLUGIN_TOOLS`），限定后 `plugin_names` = 同 4 个，不 import 未声明的 Plugin |
| A4 | MCP Router 生效 | ✅ | 12306 只注册 spec（`default_mcp_servers()`），装配期不拉起 npx；实跑 audit 的 `transport.provider_status` = `{去程火车: OK, 去程航班: OK, 回程火车: UNAVAILABLE, 回程航班: OK}` —— 按需握手成功，且**不可用时如实记状态而不是编车次**（回程火车 UNAVAILABLE 的原因见 §36.7-12：10-05 超出 12306 预售期）。同批「广州→重庆」那次去程两个源都 TIMEOUT，见 §36.7-14 |
| A5 | SuperHarness 日志能看到 Router / Tool / LLM / Trace | ⚠️ 部分 | Router ✅ `[WORKFLOW] selected/name/status`；Tool ✅ `[TOOL] name/input/output/time/status`；LLM ✅ `[LLM] model/decision/time/tokens`（token 为模型回报的真实用量）。**Trace 未产出**，原因见 §36.7-6 |

`[TOOL]` / `[LLM]` 的接入方式：`app/providers.py::ProviderHub._record` 与
`app/llm.py::LLM.invoke` 把每次调用上报给 `HybridRunner.emit`（即
`ObservabilityMiddleware.emit` → 原生 `EventBus`），日志由 SuperHarness 的
`ConsoleRenderer` 渲染。TravelPlan 不自己生成日志格式。

## B. Provider

见 §36.3 实测表（每行的状态、条数、延迟均为本次真实调用结果）。

## C. 规划

样本：`tp-20260918-222825-4bb0bb`（北京→成都，10-01 起 5 天，2 人，预算 6000）——
**这是 §36.7-11 / -13 / -14 全部修完之后重跑的 run**，早前那份
`tp-20260918-185628-cb151c` 已经作废（它的 D0 被排成游玩日、末日没有返程段）。
代码位置为可复现锚点，运行值为该 run 的真实产物。

| 标准 | 结论 | 证据 |
|---|---|---|
| 价格不是 LLM 编的 | ✅ | 价格只来自 Provider 返回值或具名常量：`price_type` 由 `app/planner.py:1252-1341` 按「该类别是否有真实报价」判定，item 价格由 `planner.py:2283-2396` 从 `hub` 结果写入，模型输出不参与算价。实测 `breakdown_price_type` = `{交通:realtime, 住宿:realtime, 门票:unknown, 市内交通:estimated, 餐饮估算:estimated, 其他:unknown}` —— 拿不到真实价的类别如实标 `unknown`，不填数字 |
| 每个实时价格有 fetched_at | ✅ | 49 条 source 全部带非空 `fetched_at`（`all('fetched_at' in x ...) == 49/49`），例：`src-001 provider=12306 fetched_at=2026-09-18T14:30:08.252549Z`；audit `provider_calls[].fetched_at` 同源 |
| 第一日考虑到达时间 | ✅ | 去程 MU6649 10-01 21:30 起飞、10-02 00:15 落地 → D0 排成**在途日**（只有 1 项 transport，note 原文「第 2 天才抵达目的地，本日不安排游玩点 —— 不是漏排，是那天人还在路上」）；`check_feasibility` stage 原文「去程第 2 天（2026-10-02）抵达；抵达链最早 02:20 可开始，行程从 08:30 起排」—— 抵达链与起排时刻分开写（§36.7-11） |
| 最后一日考虑返程 | ✅ | stage 原文「末日最晚结束时间：17:30」（`departure_deadline_minutes`，`planner.py:1646`）；末日 D4 的市内安排止于 15:15，随后是 `d4-inbound JD5612 20:30→23:00` —— **返程段在行程里**（§36.7-13） |
| 城内路线基于高德 | ✅ | 逐段调高德 `route`（`app/providers.py:1254`）；本 run `verify_poi_and_routes` stage 原文「高德路线 7 段成功 / 0 段失败，整体状态 OK」。取不到路段的**不编造耗时**：按直距×绕行估算并逐个发 `ROUTE_UNVERIFIED`（本 run 15 条） |
| 有 Budget Check | ✅ | `budget` 块：`budget_total=6000 / known_real_cost=7226 / estimated_cost=1620 / projected_total=8846 / remaining=-2846 / status=over_budget`，并给 3 条 `optimization_suggestions`（如「去程改 G321 direct ¥918 可省约 ¥818」）与 5 条 `price_notes` |
| 有 Feasibility Check | ✅ | `check_feasibility`（`planner.py:1931`）+ `apply_feasibility_pass`（`planner.py:2091`）；本 run 产出 22 条告警（`ROUTE_UNVERIFIED×15 / OPENING_TIME_CONFLICT×3 / MEAL_TIME_ODDITY×2 / HOTEL_CHECKIN_EARLY / OUTBOUND_MULTI_DAY`），**自动修订 0 个、遗留 3 个 error** —— 见 §36.7-15 |
| 有 Trust / Ad Risk | ✅ | `trust_score`（`planner.py:697`）、`ad_risk`（`planner.py:886`），落到 item 的 `trust_score` / `ad_risk` 字段；ad_risk 只对有证据的地点计分（避免"零证据也重罚"）。本 run `evidence_summary` = `{places_verified: 123, low_trust_filtered: 104, sources_used: 49}` —— 104 个低可信候选被挡在行程外 |
| 有路线折返检查 | ✅ | `detect_backtracking`（`planner.py:1106`），阈值 `BACKTRACK_SAME_PLACE_METERS=200` / `BACKTRACK_LEG_METERS=3000` / `BACKTRACK_TOTAL_METERS=15000`，见 `planner.py:1034` 节标题「六、地理聚类 / 顺路 / 折返（PRD §21、§10.3）」。本 run 0 条 `BACKTRACKING`；同日「广州→重庆」两次 run 分别检出 3 条（`tp-20260918-232247-ebca73`，已作废，仅作 §36.7-14 的回归证据）与 1 条（`tp-20260918-235342-3b2407`，§36.4 采用的那次） |
| 有 Critic 修订 | ✅ 机制在 / 本 run 未自动改排 | `revise_day`（`planner.py:1957`）按时间与营业时间冲突改排；本 run critic 段产出 18 条 `REVISION` 决策，但**全部是 `ROUTE_UNVERIFIED → CONFIDENCE_REDUCED`**（例：「双子塔夜景打卡点 的上一段路线未经高德核实（估算），时间仅供占位，出发前需确认」）—— 靠改开始时间解决不了，所以只降置信度并提示出行前确认，没有假装修好。3 条营业时间 error 也未被自动修订，见 §36.7-15 |

## D. Provenance

样本同上（`tp-20260918-222825-4bb0bb`）。审计链：`item.evidence_ids → evidence → source_id → source.fetched_at`。

| 标准 | 结论 | 证据 |
|---|---|---|
| 最终 Place 能定位 Evidence | ✅ | item 带 `evidence_ids`；`evidence_summary.places_verified=123`；verifier 逐 item 反查通过（「每个地点都能定位到 evidence/source（16 个地点）」—— 16 是**进了行程**的地点数，123 是通过验证的候选数） |
| Evidence 能定位 Source | ✅ | `evidence_summary.sources_used=49`，全部可回指 `plan.sources[].source_id` 与 `audit.provider_calls[].source_id` |
| REJECT 有理由 | ✅ | 32 条；例：`{"entity_id":"3U8896 川航 ¥1420","status":"REJECT","reason_codes":["TRANSPORT_ALTERNATIVE","OUTBOUND"],"reason_text":"去程未选中：综合分 53.7 高于 MU6649 东航 ¥1327","scores":{"价格":14.2,"门到门时长":39.5,"日期一致":0.0,"首末天影响":0.0,"偏好":-0.0}}` —— 带分数分解，且 `scores` 里含 §36.7-10 新增的「日期一致」分项 |
| PASS 有理由 | ✅（本实现记作 `KEEP`） | 136 条 `KEEP`，例：`{"entity_id":"B0FFG1LBBY","reason_codes":["representative","merged:4"],"reason_text":"作为「海合安成都极地海洋公园-海底世界」的代表点，合并了 4 条重复候选"}`；决策词表同时列了 `KEEP` 与 `PASS`，本项目统一用 `KEEP` |
| NOT_USED 有理由 | ✅ | 10 条；例：`{"entity_id":"...-src-016-w0","status":"NOT_USED","reason_codes":["NO_ITINERARY_REFERENCE"],"reason_text":"没有被任何 itinerary item 采用（未通过筛选或与行程路线不匹配）","scores":{"provider":"tavily","source_type":"web"}}` |
| audit_report 完整 | ✅ | 11 个顶层块：`run_id / query / generated_at / stages(12) / decisions(199) / provider_calls(49) / llm_calls(8) / timeline(12) / artifacts(3) / degradations(1) / evidence_summary`；决策分布 `KEEP 136 / REJECT 32 / REVISION 18 / NOT_USED 10 / SELECT 3` |

每一条决策都带 `timestamp`，`degradations` 逐条写清降级原因（本 run 只有 1 条：
「去程车站/机场到酒店的真实接驳时间未取得，门到门时长按具名常量估算」；
8 次 LLM 调用全部 `status=OK`，本次没有超时降级）。
`scripts/verify_run.py` 是这套检查的可复现实现（**32 项**，含跨午夜抵达、回程落地、
去程/住宿缺失必须明说、交通日期一致性四条；分母对四个 case 恒定，见 §36.4）。

## E. Interface

| # | 标准 | 结论 | 证据 |
|---|---|---|---|
| E1 | CLI 能运行 | ✅ | `python main.py -m "..." --user-id acceptance`，见 §36.4 |
| E2 | FastAPI 能运行 | ✅ | `GET /api/v1/health` → `{"status":"ok",...}`；`GET /api/v1/plans/{run_id}` → 200 / 177,911 B |
| E3 | Frontend 能创建计划 | ⏳ 待人工 | 后端 `POST /api/v1/plans` 同步返回，前端已把该接口超时放宽到 20 分钟；**需用户在浏览器点一次**（一次真实规划 8~20 分钟） |
| E4~E9 | Frontend 能展示日程 / 价格 / 来源 / Budget / Warning | ✅（服务端渲染实测） | 对已落库 run 请求 `GET /plan/<run_id>`，SSR HTML（307,500 字符 / 357,561 字节）中：真实航班号 `MU6649`×24、`CZ1471`×16、酒店名×30、`¥`×173、`实时`×47、`2026-09-18`×240、`证据`×214、`来源`×38、`ROUTE_UNVERIFIED`×60、`估算`×121、`未知`×2、`已连接规划服务`×2；交互链路的 DOM 级实测见 §36.6 |

---

# §36 最终验收报告

## 36.1 环境

```text
OS:                    Windows 10.0.26200 (x64)
Python:                3.11.11  (D:\miniconda3\python.exe)
Node:                  v24.7.0  (npm 11.5.1)
SuperHarness commit:   2c2c51a3b3d2370ce00eb5d17be3224ad3db40ec  (2026-09-12, submodule super_harness)
TravelPlan commit:     尚无提交（main 分支还没有任何 commit，工作区全部未提交）
Tuniu CLI version:     tuniu-cli 1.1.4  (C:\Users\Sito\AppData\Roaming\npm\tuniu.CMD)
12306-mcp version:     npx -y 12306-mcp（未固定版本；启动打印 "12306 MCP Server running on stdio @Joooook"）
```

关键依赖：

```text
superharness 0.1.0    langchain 1.4.0    langchain-core 1.6.2    langgraph 1.2.11
fastapi 0.115.6       uvicorn 0.34.0     httpx 0.28.1            pydantic 2.13.5
next 16.3.5           react 19.2.8       tailwindcss 4           typescript 5
```

模型：`kimi-k3`（`MODEL_NAME`）。环境变量是否配置由 `GET /api/v1/health` 报告，
该接口只回答「配了没配」，不回显任何 Secret 值。

## 36.2 自动测试

```text
pytest:   D:/miniconda3/python.exe -m pytest -q
Passed:   716
Failed:   0
Skipped:  0
用时:      28.94s   （2026-09-19 00:22，最后一处代码改动之后重跑确认；
                     比上一版多 5 条，来自 TestMissingCandidatesAreDisclosed。
                     1 条 warning 来自 starlette/anyio 的 deprecated alias，
                     非本项目代码）
```

覆盖（PRD §34.1 要求项 → 对应测试文件）：

| PRD §34.1 要求 | 覆盖位置 |
|---|---|
| TripIntent null 归一化 | `tests/test_models.py` |
| Tuniu CLI 参数构造 / 输出解析 | `tests/test_tuniu_travel.py` |
| 12306 fallback | `tests/test_railway_12306.py` |
| AMap route 解析 | `tests/test_amap_cn.py` |
| TikHub Free Only | `tests/test_tikhub_social.py` |
| MediaCrawler unavailable | `tests/test_mediacrawler_social.py` |
| Place normalize / Trust / Ad Risk | `tests/test_planner.py` |
| Budget / Feasibility / 时间冲突修订 | `tests/test_planner.py`、`tests/test_workflow.py` |
| 跨午夜/跨天去程的抵达日语义（§36.7-11） | `tests/test_planner.py::TestCrossMidnightArrival` |
| 抵达日起排时刻不早于正常起床时间（§36.7-11） | `tests/test_planner.py::TestArrivalDayStart`（含端到端：红眼抵达日不含 08:30 前的任何行程） |
| 返程段不会被末日的窗口裁剪吞掉（§36.7-13） | `tests/test_planner.py::TestReturnLegSurvivesTrimming`（含端到端：末日必须出现 inbound 段、没有空白日程卡） |
| 营业时间跨零点（`08:00-00:30`）（§36.7-13） | `tests/test_planner.py::TestParseOpeningHours` 中 4 条：跨零点、凌晨班、`24:00`、普通区间回归 |
| 回程缺席 / 跨天必须明说（§36.7-13） | `tests/test_workflow.py::TestInboundSpanIssues` |
| 去程 / 住宿一个候选都没有时必须明说（§36.7-14） | `tests/test_workflow.py::TestMissingCandidatesAreDisclosed`（5 条）+ `::test_empty_data_sources_degrade_truthfully`（端到端：三个源全空时 `plan.warnings` 必须同时含 `OUTBOUND_UNAVAILABLE` / `INBOUND_UNAVAILABLE` / `HOTEL_UNAVAILABLE`） |
| 交通发车日与行程日期一致性 + 不符必须明说 | `tests/test_workflow.py::TestTransportScore`、`::TestSelectTransport`、`::TestOffDateIssues`、`::TestExecuteTravelRunEndToEnd::test_an_off_date_outbound_reaches_plan_warnings`（端到端：issue → plan.warnings） |
| Mock E2E（不联网跑完整流程） | `tests/test_workflow.py` |
| Provider 归一化约定 | `tests/test_providers.py` |
| LLM 超时兜底 / 观测上报 | `tests/test_llm.py` |
| 装配边界（workflow / MCP / emit） | `tests/test_agent_api.py` |
| 落库与幂等 | `tests/test_store.py` |

前端：`npx tsc --noEmit` 无输出；`next build` 成功（exit 0，4 条路由）；`npx eslint .` 无输出。

## 36.3 Provider Smoke

`scripts/provider_smoke.py`（口径：状态与条数一律取 `ProviderCall` 里记录的值，
和真实 run 走同一条代码路径，不另写探活逻辑）。

**主表：2026-09-19 00:12 的一次完整运行**

| Provider | Tool | 状态 | 返回条数 | 延迟 | 备注 |
|---|---|---|---:|---:|---|
| 12306 | train | OK | 14 | 76.4s | **主源 UNAVAILABLE、由途牛兜底**：`12306/railway_12306_get-tickets=UNAVAILABLE → tuniu/tuniu_search_trains=OK`，`provider=tuniu`。12306-mcp 的 node 进程直接崩了（`ExceptionGroup: unhandled errors in a TaskGroup`，栈落在 `12306-mcp/build/index.js:34`），见 §36.7-9 |
| tuniu | flight | OK | 10 | 2.6s | |
| tuniu | hotel | OK | 8 | 7.1s | |
| tuniu | ticket | OK | 48 | 7.2s | |
| tuniu | cruise | TIMEOUT | 0 | 30.3s | 途牛 CLI 退出码 105：请求超时（30s）。**V1 不用邮轮**，只是把能力探一遍 |
| tuniu | holiday | OK | 10 | 1.7s | 同上，V1 不用跟团游 |
| amap | poi | OK | 5 | 3.2s | |
| amap | route | OK | 1 | 4.5s | |
| amap | geocode | OK | 2 | 2.1s | |
| tikhub | quota | OK | 0 | 0.7s | 额度查询本身通；`free_credit = 0.0`（§36.7-3） |
| tikhub | xhs | UNAVAILABLE | 0 | 0.6s | `tikhub/search_xiaohongshu=RATE_LIMIT`（HTTP 429）→ `mediacrawler/search_xhs_via_mediacrawler=UNAVAILABLE` |
| mediacrawler | xhs | UNAVAILABLE | 0 | 0.0s | 未安装：`data/vendors/MediaCrawler` 不存在（§36.7-2） |
| tavily | web | OK | 19 | 4.2s | 本轮唯一的攻略证据来源 |

**同一批工具的间歇性（如实记录，不做挑选）**：8 分钟前（00:04）的第一次完整运行里，
`12306 train` 是 TIMEOUT（210.4s，途牛兜底也 TIMEOUT）、`tuniu flight` 与 `tuniu cruise`
是 `ConnectionError（code=101）无法连接到服务器`、`tuniu ticket` 是 CLI 崩溃
（退出码 `3221226505`，libuv `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`）。
00:09 单独重试 `--only tuniu`，**5 个 tool 全部 OK**（flight 10 / hotel 8 / ticket 48 /
cruise 10 / holiday 10）。所以这些是瞬时故障，不是配置错误；上面主表也不是"挑好看的一次"，
就是最后一次完整运行。这正是 §36.7-7「每个城市对只有 1 次真实运行样本」的由来。

**每一条失败都没有被假数据兜底**：状态、错误原文、回退链全部落在 `ProviderCall` 里，
真实 run 的 `audit_report.json` 同样如实记 `status=unavailable/timeout` + `reason`。

## 36.4 E2E Case

四个 case 串行执行（`bash _acceptance_runs.sh`，带单实例锁；并发跑会互相抢 Provider
配额，把限流误报成故障）。产物落 `outputs/<run_id>/{plan.json,plan.md,audit_report.json}`，
日志落 `_acceptance/<name>.log`。下面的结果块由 `scripts/case_summary.py` 从产物直接生成，
不是手抄。

**代码版本说明（重要）**：`bj_cd / sh_hz / cc_cd` 三个 case 跑在 §36.7-14 修复**之前**的
代码上，`gz_cq` 是修完之后重跑的（`tp-20260918-235342-3b2407`，旧的那次
`tp-20260918-232247-ebca73` 保留作为 §36.7-14 的回归证据）。那次改动是**纯增量**：
只在「去程一个候选都没有」/「住宿一个候选都没有」时新增 warning，而这三个 case
**去程和住宿都选中了**，所以新代码不可能改变它们的输出——这一点由
`verify_run.py` 复查确认：三个 case 在新代码下仍是 32/32。

### Case 1：北京 → 成都（bj_cd）

```text
输入:            10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照
run_id:          tp-20260918-222825-4bb0bb
transport:       MU6649  ¥1327.0
inbound:         JD5612  ¥1410.0  抵达 2026-10-05T23:00:00
hotel:           维也纳国际酒店·成都春熙路太古里店  ¥438.0/晚
day_count:       5
itinerary:       20 个真实地点 / 0 段留白
real_cost:       7226.0
estimated_cost:  1620.0
projected_total: 8846.0（预算 6000.0，结余 -2846.0，status=over_budget）
budget:          {'交通': 5474.0, '住宿': 1752.0, '门票': 0.0, '餐饮估算': 1200.0, '市内交通': 420.0, '其他': 0.0}
realtime 价格:   4 条；价格未知的地点 16 个
warnings:        22 条 ['HOTEL_CHECKIN_EARLY', 'MEAL_TIME_ODDITY×2', 'OPENING_TIME_CONFLICT×3', 'OUTBOUND_MULTI_DAY', 'ROUTE_UNVERIFIED×15']
sources:         49 条，decisions 199 条
verify_run:      32/32
```

红眼去程 MU6649 10-01 21:30 起飞 → 10-02 00:15 落地，D0 被如实排成在途日（§36.7-11）；
末日 D4 带 `JD5612 20:30→23:00` 返程段（§36.7-13）。预算超支 ¥2,846，系统标
`over_budget`、不削价编造（§36.7-5）。3 条 error 级营业时间冲突未自动改排（§36.7-15 a/b/c）。

### Case 2：上海 → 杭州（sh_hz）

```text
输入:            10月2日从上海去杭州玩3天，两个人，预算3000，喜欢自然和拍照
run_id:          tp-20260918-223526-00317d
transport:       D203  ¥33.0~153.0（分席别）
inbound:         （未选中）—— 行程末尾没有回家那一段，见 warnings 的 INBOUND_UNAVAILABLE
hotel:           全季酒店（杭州西溪海港城店）  ¥414.0/晚
day_count:       3
itinerary:       16 个真实地点 / 0 段留白
real_cost:       894.0
estimated_cost:  1056.0
projected_total: 1950.0（预算 3000.0，结余 1050.0，status=within_budget）
budget:          {'交通': 66.0, '住宿': 828.0, '门票': 0.0, '餐饮估算': 720.0, '市内交通': 336.0, '其他': 0.0}
realtime 价格:   3 条；价格未知的地点 13 个
warnings:        21 条 ['DAY_OVERLOADED×3', 'HOTEL_CHECKIN_EARLY', 'INBOUND_UNAVAILABLE', 'OPENING_TIME_CONFLICT×2', 'OUTBOUND_OFF_DATE', 'ROUTE_UNVERIFIED×13']
sources:         48 条，decisions 169 条
verify_run:      32/32
```

这个 case 同时踩中两条已修缺陷：12306 返回的车次全是 09-30 / 10-01 的，没有一条是
约定的 10-02（§36.7-10，`OUTBOUND_OFF_DATE` 如实报出、没有偷偷改日期），
以及一个回程候选都没有（§36.7-13，`INBOUND_UNAVAILABLE`）。

### Case 3：长春 → 成都（cc_cd）

```text
输入:            10月1日从长春去成都玩6天，两个人，预算8000，喜欢美食和历史文化
run_id:          tp-20260918-230052-9819f6
transport:       K548  ¥328.0~879.0（分席别）
inbound:         K546  ¥328.0~918.0（分席别）  抵达 2026-10-08T06:45:00
hotel:           美豪酒店（成都春熙路太古里店）  ¥469.0/晚
day_count:       6
itinerary:       11 个真实地点 / 1 段留白
real_cost:       3657.0
estimated_cost:  1632.0
projected_total: 5289.0（预算 8000.0，结余 2711.0，status=within_budget）
budget:          {'交通': 1312.0, '住宿': 2345.0, '门票': 0.0, '餐饮估算': 1440.0, '市内交通': 192.0, '其他': 0.0}
realtime 价格:   3 条；价格未知的地点 8 个
warnings:        12 条 ['INBOUND_MULTI_DAY', 'MEAL_TIME_ODDITY×2', 'OPENING_TIME_CONFLICT×2', 'OUTBOUND_MULTI_DAY', 'ROUTE_UNVERIFIED×6']
sources:         49 条，decisions 172 条
verify_run:      32/32
```

去程 K548 10-01 22:57 开 → 10-03 21:35 到（46h38m），吃掉整整两天，
`OUTBOUND_MULTI_DAY` 如实说明"看起来 6 天、实际能玩的少得多"；回程唯一候选是
K546 46h47m（10-06 07:58 → 10-08 06:45），`INBOUND_MULTI_DAY` 写明"跨 3 天、
要 10-08 才到家"（§36.7-15 d/e）。那 1 段留白是末日窗口排不下任何停留点时的如实交代，
不是空卡片（§36.7-13）。

### Case 4：广州 → 重庆（gz_cq，§36.7-14 修完后重跑）

```text
输入:            10月3日从广州去重庆玩4天，三个人，预算7000，喜欢美食和夜景
run_id:          tp-20260918-235342-3b2407
transport:       JD5227  ¥640.0
inbound:         G2965  ¥574.0~1941.0（分席别）  抵达 2026-10-06T21:19:00
hotel:           桔子酒店（重庆西站新桥医院店）  ¥537.0/晚
day_count:       4
itinerary:       19 个真实地点 / 0 段留白
real_cost:       6864.0
estimated_cost:  2070.0
projected_total: 8934.0（预算 7000.0，结余 -1934.0，status=over_budget）
budget:          {'交通': 3642.0, '住宿': 3222.0, '门票': 0.0, '餐饮估算': 1440.0, '市内交通': 630.0, '其他': 0.0}
realtime 价格:   4 条；价格未知的地点 15 个
warnings:        21 条 ['BACKTRACKING', 'HOTEL_CHECKIN_EARLY', 'MEAL_TIME_ODDITY×2', 'OPENING_TIME_CONFLICT×2', 'ROUTE_UNVERIFIED×15']
sources:         54 条，decisions 195 条
verify_run:      32/32
provider_status: {'去程火车': 'OK', '去程航班': 'OK', '回程火车': 'OK', '回程航班': 'UNAVAILABLE'}
degradations:    2 条（5 段市内路线高德 DEGRADED；去程接驳时间未取得、按具名常量估算）
```

**必须如实说明的一点**：这次重跑**没有触发** `OUTBOUND_UNAVAILABLE` /
`HOTEL_UNAVAILABLE` —— 因为这一回 Provider 成功了（途牛火车 30 条、航班 10 条、
酒店 7 条都返回了），去程和住宿都选中了。上一次（`tp-20260918-232247-ebca73`）
是 12306 与途牛航班同时 TIMEOUT、途牛酒店 UNAVAILABLE 才暴露出缺陷。
所以这个修复的真机证据来自**旧 run 在新检查下得 30/32、两个 FAIL 正好是这两项**，
加上端到端单测 `test_empty_data_sources_degrade_truthfully`（三个源全空时
`plan.warnings` 必须同时含三个 UNAVAILABLE 码）。没有为了让告警出现而人为制造失败。

### PRD §36.4 验证清单（四个 case 逐项）

| 项 | bj_cd | sh_hz | cc_cd | gz_cq | 判定依据 |
|---|---|---|---|---|---|
| 所有实时价格有 source | ✅ | ✅ | ✅ | ✅ | `verify_run` 逐条比对 `price_type=realtime` 的 item → `source_ids` → `plan.sources`（4 / 3 / 3 / 4 条） |
| 所有实时价格有 fetched_at | ✅ | ✅ | ✅ | ✅ | 同上，每条 source 都带 ISO8601 `fetched_at`，缺一条即 FAIL |
| 所有 itinerary place 有 evidence | ✅ | ✅ | ✅ | ✅ | 16 / 13 / 8 / 15 个地点全部能定位到 evidence 或 source |
| 时间无硬冲突 | ✅ | ✅ | ✅ | ✅ | 同一天内无区间重叠（营业时间冲突是另一类，见 §36.7-15） |
| 预算计算正确 | ✅ | ✅ | ✅ | ✅ | 分项之和 == `projected_total`；`known_real_cost + estimated_cost == projected_total`；`remaining == 预算 - projected_total` |
| plan.json 生成 | ✅ | ✅ | ✅ | ✅ | `outputs/<run_id>/plan.json` |
| plan.md 生成 | ✅ | ✅ | ✅ | ✅ | 32,161 / 16,943 / 12,602 / 34,044 字节 |
| audit_report.json 生成 | ✅ | ✅ | ✅ | ✅ | 含 stages / decisions / provider_calls / timeline / degradations |

`scripts/verify_run.py` 把这 8 项扩成 **32 项**可复现检查（分母对四个 case 恒定，
见 §36.7-14），四个 case 全部 **32/32**：

```bash
PYTHONIOENCODING=utf-8 D:/miniconda3/python.exe scripts/verify_run.py <run_id>
PYTHONIOENCODING=utf-8 D:/miniconda3/python.exe scripts/case_summary.py --all
```

## 36.5 Fallback

`scripts/provider_smoke.py --with-fallback`（2026-09-19 00:16 运行）。
三条都是**真实调用链**，不是断言"代码里写了 fallback"。

| # | PRD 要求 | 结论 | 实测 |
|---|---|---|---|
| 1 | 人工让 12306 MCP 不可用，确认走途牛 | ✅ | 不需要人工制造——12306-mcp 的 node 进程**自己就崩了**（`ExceptionGroup: unhandled errors in a TaskGroup`，栈在 `12306-mcp/build/index.js:34`）。调用链 `['12306/railway_12306_get-tickets=UNAVAILABLE', 'tuniu/tuniu_search_trains=OK']`，整体 `status=OK`、拿到 14 条车次，`provider` 如实记 `tuniu`。脚本判定 **PASS：回退到了途牛** |
| 2 | TikHub `free_credit=0` / mock 402，确认走 MediaCrawler | ⚠️ 半通过 | 额度查询 `status=OK`、`free_credit = 0.0`；小红书调用链 `['tikhub/search_xiaohongshu=RATE_LIMIT', 'mediacrawler/search_xhs_via_mediacrawler=UNAVAILABLE']` —— **确实走到了 MediaCrawler**，但 MediaCrawler 未安装（`data/vendors/MediaCrawler` 不存在，§36.7-2），所以拿不到内容，如实记 `UNAVAILABLE`、**不编造攻略**。"回退链路正确"已验证；"MediaCrawler 真能抓到"未验证（抓取路径本身由注入式单测覆盖，见 §36.2） |
| 3 | AMap 异常时不允许模型编路线 | ✅ | 注入式：把 `ProviderHub._plugin_call` 里 `provider == "amap"` 的调用换成返回 `status=UNAVAILABLE` 的 `ProviderCall`，再走**同一条** `hub.route(...)` 代码路径 → `status=UNAVAILABLE`、`items=0`。脚本判定 **PASS：没有编造路线，交由 planner 记 ROUTE_UNVERIFIED**。真机对照：gz_cq 那次 5 段市内路线高德 DEGRADED，`audit.degradations` 原文「这些路段在行程里标为未核实，Critic 会相应下调置信度；没有用估算值冒充实测」，产物里对应 15 条 `ROUTE_UNVERIFIED` |

跑 §36.5 第 3 项时暴露并修掉了脚本自身的一个 bug：注入的假 `ProviderCall`
少传必填的 `fetched_at`，`TypeError` 直接把 smoke 打断了（`scripts/provider_smoke.py:176`）。
已补 `fetched_at=utcnow()`。**这是验收工具的缺陷，不是主流程的**——主流程构造
`ProviderCall` 的地方全都带 `fetched_at`，否则 §36.4 的「所有实时价格有 fetched_at」
四个 case 不可能全过。

## 36.6 前端

| 项 | 结论 | 说明 |
|---|---|---|
| Desktop | ⏳ 待人工 | SSR 已确认渲染出内容；视觉需在浏览器确认 |
| Mobile | ⏳ 待人工 | 同上 |
| Loading | ✅ | `frontend/app/plan/[id]/loading.tsx` 提供路由级骨架（Qoder 内置浏览器里实测可见该骨架文案） |
| Empty | ⏳ 待人工 | |
| Partial Data | ✅ | 实测 run 里 4 类价格有 `realtime`/`estimated`/`unknown` 三种口径，页面按口径分别标注 |
| Error | ✅ | `frontend/app/plan/[id]/error.tsx` + `ErrorState`，`describeApiError` 区分 network/timeout/not_found/invalid/server |
| Real-time Price Timestamp | ✅ | SSR HTML 中 `实时`×47、`2026-09-18`×240 |
| Evidence Drawer | ✅ 打开 / ⏳ 关闭与显示 | DOM 实测：点「查看 N 个来源」后挂载出 8,553 字符的来源面板（来源名、`查询于 10:58`、「为什么推荐：候选得分 16（…）+6 / +2」的逐项算分）；但面板节点的计算样式停在 `opacity:0`（`data-starting-style`），**没有真正显示出来**，见下方复核 |
| Budget Summary | ✅ | SSR HTML 中 `预算`×22、`结余`×1 |
| Feasibility Warning | ✅ | SSR HTML 中 `ROUTE_UNVERIFIED`×60 |

上表的字符计数取自 `tp-20260918-185628-cb151c`（§35 C 已作废的那份 run）。
2026-09-19 00:20 用 §36.4 采用的 gz_cq run 复测了一次 SSR，结论一致：

```text
GET http://127.0.0.1:8000/api/v1/plans/tp-20260918-235342-3b2407   → 200 / 201,410 B
GET http://127.0.0.1:3000/plan/tp-20260918-235342-3b2407           → 310,091 字符
  JD5227×34  G2965×28  桔子酒店×44  解放碑×30  洪崖洞×19
  ROUTE_UNVERIFIED×45  实时×64  超支×3  来源×31  证据×208
```

真实航班号、酒店名、地点名、超支标记、未核实路线告警都进了首屏 HTML（不依赖客户端
JS 才出现）。**视觉与交互仍需人工在浏览器确认**，尤其是下面这条已知问题。

复核（2026-09-18 22:33 重测，服务均为 live 模式；样本是上面那份已作废的 run，
DOM 层面的结论仍然成立，但计数与 gz_cq 那次不同）：

```text
GET http://127.0.0.1:3000/plan/tp-20260918-185628-cb151c
  → 200, 357,561 bytes（307,500 字符）, 0.38s
  第 1 天 ×8    MU6649 ×24    维也纳国际酒店 ×30    证据 ×214    ROUTE_UNVERIFIED ×60
GET http://127.0.0.1:8000/api/v1/plans/tp-20260918-185628-cb151c
  → 200, 177,911 bytes, 0.03s
```

补充：DOM 级交互复核（2026-09-18 22:30，同一个 run，页面已在浏览器里 hydrate 完成）

截图仍然拿不到（`NATIVE_BROWSER_VIEWPORT_UNAVAILABLE viewport=531x623, visible=false`），
所以换一条不需要像素的路：用 CDP 在该页上执行脚本，读它**交互后的 DOM 状态**。
这条路的结论只覆盖"点了之后 DOM 有没有变"，不覆盖"看起来对不对"。

| 动作 | DOM 观测 | 结论 |
|---|---|---|
| 读首屏 | `innerText` 8,784 字符，`scrollHeight` 10,934，`MU6649` 出现、`¥`×54、`button`×72 | ✅ 真实计划已 hydrate，不是骨架屏 |
| 点 `Day 4` | 该按钮 `aria-current` 由 `false` → `true`，类名从 `border-border bg-card` → `border-primary/40 bg-accent`，正文标题变为 `Day 4 行程` + 当日 5 个地点 | ✅ 日切换生效（状态变化可观测） |
| 点 `#budget` 锚点 | `location.hash` = `#budget` | ✅ 锚点导航生效 |
| 点 `查看 1 个来源` | `[role=dialog]` 0 → 2，其中「来源与查询条件」面板内容 8,553 字符：来源名、`查询于 10:57`、`为什么推荐：候选得分 16（Trust 8×0.55 − AdRisk 0×0.45 + 偏好 1 项（12）+ 路线适配 0%（0））` | ✅ 点击→数据渲染链路通 |
| 点 `依据` | 「规划依据」面板内容 26,450 字符：`本次规划共调用 48 个数据源，其中 11 个为降级返回`，逐条列降级原因 | ✅ 同上 |
| 点面板内的 `Close` | 面板内容**没有卸载**，`[role=dialog]` 仍是 2 | ⏳ 未验证（合成事件点不动；不用真鼠标不算数） |

关于"打开"这一行的 ⏳：两个面板节点的计算样式都是 `opacity: 0`，
类名带 `transition duration-200 data-starting-style:opacity-0` —— 也就是**入场动画卡在起始帧**。
这与截图失败的根因是同一个：这个内置浏览器 `visibilityState=hidden`、不给可见 surface，
页面不渲染，CSS transition 也就不推进。同一时刻 curl 拿到的是完整 357 KB 页面，
所以「内容挂载了但 opacity 停在 0」是**宿主环境不渲染**，不是页面坏了 ——
换个真实浏览器再看一眼就能定论，因此没有把「看起来对不对」记成已验证。

> 「✅」只代表服务端返回的 HTML 里确实有这些信息；交互（展开证据抽屉、切 tab）
> 需要人工在浏览器里点一次。


## 36.7 已知问题

```text
1. /api/v1/plans/{id}/revise 是 v0 占位：只做请求校验与登记，不重新规划，
   返回 queued。PRD §30 允许 v0 预留，但用户点了「修改行程」不会真的改。

2. MediaCrawler 未安装：vendor 目录不存在，check_mediacrawler_available 返回
   不可用。因此「TikHub 无额度 → MediaCrawler 兜底」这条链路的真实抓取
   跑不通（抓取路径本身由 §36.5 的注入式测试覆盖）。

3. TikHub 免费额度为 0（free_credit=0.0）→ 社交证据为空，本次仅有 Tavily 网页
   证据。所有地点因此是「高德 POI + 0 条攻略证据」，页面按实际标注
   「只有 0 条攻略证据支撑，建议自行确认」。

4. 模型是推理模型（kimi-k3），单次调用耗时波动大（实测 parse_intent 49s、
   抽地点 75s / 115.8s）。默认墙钟上限已从 120s 提到 180s，并可用
   TRAVELPLAN_LLM_TIMEOUT_SECONDS 覆盖；仍有调用超时降级。超时不会伪造结果：
   audit_report.json 的 llm_calls 与 degradations 会如实记下哪一次没等到。

5. 预算可能超：实测「北京→成都 5 天 2 人 预算 6000」给出 ¥9,222
   （over_budget），系统如实标记超支、不做削价式编造，但 V1 没有
   「按预算反推更省的方案」这一步。

6. SuperHarness 原生 Trace 树没有产出（§35 A5 只达成一半）：
   固定规划流程走的是 HybridRunner 的 workflow 分支，不经过 Agent Loop，
   而 trace_id 由 ObservabilityMiddleware.before_agent 在 Agent 生命周期里创建
   → 该分支根本没有 trace_id，TraceCollector 会跳过所有无 trace_id 的事件。
   现状是 Router / Tool / LLM / latency / token 都能在日志里看到，
   但「一次 run 的 span 树」看不到。替代物是本项目自己的
   audit_report.json（timeline + stages + provider_calls + llm_calls），
   它对固定流程更贴切，但那是业务审计，不是原生 Trace。

7. 每个城市对只有 1 次真实运行样本：Provider 返回随时间变化，本报告的数字
   是 2026-09-18 当晚的快照，不是稳定值。

8. 历史遗留：早于状态守卫修复的若干 run 在 runs 表里停在 running
   （进程早已退出）。已在本轮验收前统一标为 failed，不做数据删除。

9. 12306 主源会整段时间不可用。实测 2026-09-18 20:55 连续 3 次调用全部失败：

       Error: get station name js file failed.
         at getStations (.../12306-mcp/build/index.js:1194)
       → 12306 status=UNAVAILABLE，途牛兜底 OK（分别返回 14 / 30 / 30 条）

   同一晚早前 12306 对「北京→成都 10-01」是能返回 10 条车次的，所以这是
   间歇性不可用，不是永久坏掉。这条链路恰好是 §36.5 第 1 条降级路径的
   真实复现：主源不可用时回退途牛，两条调用都留在 audit 里。

10. 12306 会返回**发车日与查询日不符**的车次（已定位并修复）。
    实测查「上海→杭州 date=2026-10-02」，10 条车次的 start_date 是
    2026-09-30（3 条）与 2026-10-01（7 条）——没有一条是 10-02 的，
    而 run 的 intent.start_date 正是 2026-10-02。比选当时只看价格与时长，
    于是选中了 D203（10-01 08:16 开），整份行程实际排在了 10-01，
    计划里没有任何地方提到这件事。

    修法（三选一里选了唯一诚实的那个）：

    - ~~把车次日期改成 10-02~~ → 编造数据，不做；
    - ~~默默采用、什么都不说~~ → 用户拿到一份日期对不上的行程，不做；
    - **保留真实车次 + 明说日期不符** ← 采用。

    具体改动：
    - `app/planner.py` 新增 `departure_day_offset()`（发车日与基准日差几天）；
    - `app/workflow.py` 比选新增"日期一致"分项，`TRANSPORT_OFF_DATE_PENALTY=200`
      **每差一天**——刻意重于价格(权重1/¥100)、门到门时长(1/10min)、偏好(40)，
      因为"贵一点"是偏好问题而"差一天"是这趟车根本不在那天；
    - 没有一天对得上时，`transport.selection_reason` 明写
      「该方案的发车日是 2026-09-30，与行程约定的 2026-10-01 相差 -1 天 ——
      N 个候选里没有一个落在约定日期，这是数据源返回的日期，我们没有替它改」；
    - `plan.warnings` 记 `OUTBOUND_OFF_DATE` / `INBOUND_OFF_DATE`（severity=warning）；
    - `scripts/verify_run.py` 新增一条检查「不符时必须明说」——它检查的是
      **说了没有**，不是日期对不对：日期由数据源决定，我们只负责不隐瞒。

    回归证据：旧代码跑出的 `tp-20260918-194929-d152f4` 在加了这项检查后是
    25/27（当时的检查总数是 27），两个 FAIL 正好是「跨午夜去程丢失」和本条——
    说明检查不是摆设。
    配套单测 13 条（`TestTransportScore` / `TestSelectTransport` / `TestOffDateIssues`）。

11. 跨午夜 / 跨天去程会被排成"人还在路上就已经在逛景点"（已定位并修复）。
    这是本轮验收里最严重的一个缺陷，而且 4 个 case 里有 3 个中招：

    | case | 去程 | 旧代码排出来的样子 |
    |---|---|---|
    | 北京→成都 | MU6649 10-01 21:30 起飞 → 10-02 00:15 落地 | D0（10-01）排成 08:30~16:11 的完整游玩日，`transport.items = []`，去程段整段丢失 |
    | 长春→成都 | K548 10-01 22:57 开 → 10-03 21:35 到（46h38m） | D0 从 22:40 开始一路排到次日 06:50 |
    | 上海→杭州 | D203 10-01 08:16 开（见 §36.7-10），而行程出发日是 10-02 | 抵达链套在出发日上 |

    根因有两处：
    - `planner.build_initial_plan` 用 `_arrives_on(outbound, 0)`（与第 0 天**精确同日**匹配）
      判断去程属于哪一天，只要抵达跨了午夜就匹配不上，去程段被静默丢弃；
    - `workflow.node_check_feasibility` 把 `first_day_start` 当成一个**裸时刻**
      传给 `apply_feasibility_pass`，后者固定套在 `index == 0` 上——于是"抵达后最早
      几点能出门"被算在了出发日，而不是真正的抵达日。

    修法：
    - `planner.arrival_day_index()` / `arrival_beyond_trip()`：抵达日按**真实日期**
      算索引并夹在行程内，超出行程则由上层告警；
    - 抵达日之前的天数标为"在途日"（`capacity = 0`），排一个 `free_time` 段
      「在途（交通日）」，day note 写明
      「第 N 天才抵达目的地，本日不安排游玩点 —— 不是漏排，是那天人还在路上」；
    - `MAX_DAY_MINUTES = 24*60` 硬顶 + 深夜抵达只留 90 分钟
      （`LATE_ARRIVAL_WINDOW_MINUTES`）够"出站 → 到酒店 → 入住"，不越过 24:00；
    - `apply_feasibility_pass` 增加 `first_day_index`，让抵达约束落在正确的那天；
    - 新增告警 `OUTBOUND_MULTI_DAY`（warning）/ `ARRIVAL_AFTER_TRIP_END`（error）；
    - 跨天车次不再写 `end_time`（避免 `00:15 < 21:30` 被误判成"结束早于开始"），
      真实起止时刻写进 `reason`；
    - `scripts/verify_run.py` 新增「去程交通段出现在行程里」与「抵达日之前没有
      游玩点」两项检查。

    修完上面这轮之后，**同样的红眼 case 又暴露出第二个缺陷**（这次是复盘新版
    `bj_cd` 输出时看出来的，不是旧 run 遗留）：

    | 抵达日实际排出来的样子（修复后、但还没做下面这一步时） |
    |---|
    | `d1-hotel-checkin 02:20` → `activity 双流…夜景打卡 03:15–04:45` → `food 05:06` → `food 06:24` |

    根因：`arrival_ready_minutes()` 回答的是「几点**能**出门」，这是事实；但
    `build_initial_plan` 与 `node_check_feasibility` 把它直接当成了当天的**排程起点**。
    「物理上能开始」≠「应当从那时开始」——凌晨 3 点打卡、5 点吃晚饭，那时旅客在睡觉，
    景点和餐馆也没开门。

    修法：新增 `planner.arrival_day_start_minutes()`，对落在正常起床时刻之前的抵达
    取 `max(抵达链, DAY_START_MINUTES = 08:30)`，只改**排程起点**；原始抵达时刻
    （`00:15`）仍如实写在 D0 的在途说明与 transport 理由里，不因为取整就把事实抹掉。
    `node_check_feasibility` 的阶段说明也分开写「抵达链最早 02:20 可开始，行程从
    08:30 起排」。

    回归证据：17 条单测（`TestCrossMidnightArrival` 8 条 + `TestArrivalDayStart` 9 条，
    后者含一条端到端断言"红眼抵达日不含 08:30 前的任何行程"）、698 条全量通过；
    两个新检查在旧代码的 run 上如期报红
    （`bj_cd` 旧 run 25/26、`cc_cd` 旧 run 24/26）。

12. 12306 有预售期，返程日期一旦超出就必然查不到票。实测 2026-09-18 查
    「北京→成都」：10-01 有 10 条、10-02 有 10 条，10-03 / 10-04 / 10-05 / 10-06
    全部为 0 条。今天 09-18，10-02 是第 14 天、10-03 是第 15 天，与"预售 15 天
    （含当天）"吻合。

    影响（**只限 12306 这一个源**，别读成"所有源都查不到"）：本批 4 个 case 的行程
    都在 10-03~10-06 之间结束，12306 对这些日期返回 0 条或 INVALID_RESPONSE，
    audit 里如实记 `回程火车=UNAVAILABLE/EMPTY`，**没有编造车次**。

    但途牛不受这个预售期限制：`gz_cq` 重跑（`tp-20260918-235342-3b2407`）里
    12306 查 10-06 重庆→广州是 `INVALID_RESPONSE`，同一段途牛 `tuniu_search_trains`
    返回 OK，选中 G2965（¥574~1941 分席别，10-06 21:19 抵达），另有 G2941 /
    G3729 / G3749 / G3727 四个候选。所以"回程火车查不到"是**主源预售期**造成的，
    兜底源能补上——这正是 §36.5 第 1 条降级链路存在的意义。

13. 行程里"没有回家的那一段"（已定位并修复）—— 最容易被放过的一个缺陷，
    因为它在页面上**看不出任何异常**。

    | case | 回程方案 | 旧代码的日程里 |
    |---|---|---|
    | 广州→重庆 | CA4341，10-06 16:55 起飞 | 末日只有退房 + 2 个景点，**没有返程段** |
    | 长春→成都 | K546，10-06 07:58 开 → 10-08 06:45 到（46h47m） | 末日**整天为空**（0 项），返程段消失，10-07/10-08 在车上也没人提 |
    | 上海→杭州 | 一个候选都没有 | 3 天行程照常排，末尾不说"没有回程" |

    根因在 `planner._trim_to_window`：掐当天时间窗时执行
    `dropped.extend(working_items[first:]); working_items = working_items[:first]`
    的**整段切片**。`overflow` 的筛选条件里写了 `type != "transport"`
    （交通段不参与"超窗"判定），但那只保护它不被当成**触发者**，
    保护不了它**被连带切掉** —— 而返程段固定 append 在当天最后一位，
    于是"第一个装不下的景点"把后面的返程段一起带走了。末日窗口比起床时刻还早时
    （K546 07:58 开 → deadline 06:43），切片从 index 0 开始，整天被清空。

    修法：
    - `_trim_to_window` 改为**只丢停留点、交通段一律保留**：逐项判断而不是整段切片；
    - `build_initial_plan` 增加兜底：任何一天都不能是空 items，排不下就如实写
      「当天可用时间窗排不下任何一个符合门槛的安排（抵达太晚或末日要赶返程），
      留作机动 —— 不编造行程」，且**不补一个假的开始时刻**；
    - 新增 `INBOUND_UNAVAILABLE`（没有回程候选）/ `INBOUND_MULTI_DAY`
      （回程到家晚于行程最后一天，跨 N 个自然日）两条 warning —— 原来的检查只看去程段，
      `OUTBOUND_MULTI_DAY` 也没有回程对应项；
    - `scripts/verify_run.py` 新增 3 项：回程段必须出现在行程里、每天必须有安排、
      回程跨天必须明说。这 3 项在（未修复的）`cc_cd` run 上如期报红
      （27/30），`gz_cq` 报 1 项红（28/30）。

    顺带修掉的同批缺陷：`parse_opening_hours("周一至周日 08:00-00:30")`
    按 min/max 取时刻，把 00:30 读成"开门"、08:00 读成"关门"，
    于是 `CLOSING_TIME_OVERRUN` 报出「德庄火锅停留到 13:43，超过闭园时间 08:00」
    这种完全错误的 error 级结论。现在按"开门-关门"区间解析，
    **关门早于开门即视为跨零点**（00:30 → 次日 1470），并在 note 里标注"营业时间跨零点"。

    回归证据：13 条单测（`TestReturnLegSurvivesTrimming` 4 条 +
    `TestParseOpeningHours` 新增 4 条 + `TestInboundSpanIssues` 5 条）、711 条全量通过。

14. 「没有去程」「没有住宿」只写进 audit 降级记录，不进 plan.warnings（已定位并修复）。
    这是 §36.7-13 修完回程之后暴露出来的**同一个不对称**——回程缺席会告警，
    去程和住宿缺席不会。

    真机复现（2026-09-18，广州→重庆 `tp-20260918-232247-ebca73`）：
    12306 与途牛航班同时 TIMEOUT、途牛酒店 UNAVAILABLE，于是 4 天行程里
    **既没有去程也没有住宿**，而页面上看不出任何异常：

    | 事实 | 页面上的样子 |
    |---|---|
    | 没有任何去程方案 | D0 08:30 就在重庆逛景点（按「人已经在目的地」排） |
    | 没有任何住宿候选 | `budget.breakdown.住宿 = ¥0`，`projected_total` 仍显示「预算内」 |
    | 两处都只有 audit 记录 | 前端把它汇总成「有 2 个数据源为降级返回」这种笼统计数 |

    `plan.warnings` 一条都没提。用户看不出这趟行程没法去、也没地方睡。

    修法（沿用 §36.7-10 / -13 的原则：**不补数据，只把空缺说出来**）：
    - `_transit_span_issues` 原先的合并守卫 `outbound is None or arrival_at is None`
      拆开：`outbound is None` 时新增 `OUTBOUND_UNAVAILABLE`（warning），
      理由写明「行程按『人已经在 {目的地}』排：第 1 天不受抵达时间约束，
      请自行安排前往 {目的地} 的交通 —— 系统不替这次空缺编一趟车」；
    - 新增 `_hotel_issues`：`hotel_plan.selected is None` 且行程 > 1 天时给
      `HOTEL_UNAVAILABLE`（warning），写明缺几晚、「住宿费用没有计入预算
      （也没有用估算价代替）」；当天往返（nights ≤ 0）不报，那不是缺陷；
    - `node_check_feasibility` 里与 `inbound_issues` 同样的方式并入 issues / errors 计数；
    - `scripts/verify_run.py` 新增 `check_missing_candidates_disclosed`（2 项检查）——
      同样只检查**说了没有**，不检查数据源该不该失败。

    顺带修掉验收工具自身的一个问题：`check_arrival_grounding` 原先在没有
    `transport.selected` 时**整段跳过**两项检查，导致四个 case 的分母不一致
    （gz_cq 28 项、其余 30 项）。现在改为无条件记一条并写明「不适用（不当成通过）」，
    **分母对四个 case 恒定为 32 项**。

    回归证据：新增 5 条单测（`TestMissingCandidatesAreDisclosed`），
    并把 `test_empty_data_sources_degrade_truthfully` 加强为「光有 audit.degradations
    不够，三个 UNAVAILABLE 码必须同时出现在 `plan.warnings`」；716 条全量通过。
    两条新检查**不是摆设**：未修复的代码跑出的 gz_cq run 得 30/32，
    两个 FAIL 正好是这两项；bj_cd / sh_hz / cc_cd 都是 32/32。

15. 已定位但**本轮不修**的规划质量问题（如实列出，不代表这些是可接受的输出）。
    a / b / c 三条是同一个根因：`apply_feasibility_pass` 只会**报**冲突、不会为了避开
    营业时间而重排，critic 段的 18 条 REVISION 又全部是
    `ROUTE_UNVERIFIED → CONFIDENCE_REDUCED`（改开始时间解决不了"路线未经核实"），
    所以 error 级冲突会一路留到产物里。

    | # | 现象（均为实跑产物，非构造） | 为什么没修 |
    |---|---|---|
    | a | `bj_cd` D2（2026-10-03）**整日 5 个连续餐馆** 08:30→14:43，一个景点都没有，其中 2 家还没到营业时间（`悦百味 09:30 开门`排在 08:30、`新蓉庭 11:00 开门`排在 09:48） | 用户偏好是「美食」，选点阶段按偏好打分把餐馆排到了前面，日程装配只管时间窗不塞得下、不管**类型多样性**；要修得引入"同类连续上限"与重排，属于规划策略改动，不在本轮验收范围 |
    | b | `bj_cd` D1 把 `双子塔夜景打卡点`（营业时间 19:00-03:00）排在 **09:25** | 同上，`OPENING_TIME_CONFLICT` 报成 error 但没有自动改排 |
    | c | `bj_cd` 有 **3 条 error 级**未解决冲突、`cc_cd` 有 **2 条**，而 `runs.status` 仍是 `completed` | `status` 表达的是"流程跑完了"，不是"计划没有瑕疵"；把 error 计入 status 会让所有含未核实路线的 run 都变成 failed，反而丢掉可用产物。**但这一点必须在页面上更醒目**，目前只在「时间与可行性检查」里逐条列出 |
    | d | `cc_cd` 回程**只有 1 个候选**，是 K546 成都西→长春 46 小时 47 分（10-06 07:58 → 10-08 06:45） | 12306 预售期（§36.7-12）+ 途牛航班 TIMEOUT，确实只有这一趟；系统如实选中并给出 `INBOUND_MULTI_DAY`，**没有为了好看去编一趟高铁** |
    | e | 同上那趟 46h47m 的车，日程卡 `end_time` 显示 **`06:45(次日)`**，而真实抵达是**第 3 天** | `minutes_to_clock` 对 ≥1440 分钟一律加 `(次日)` 后缀，不区分 +2 天 / +3 天。真实日期没有丢：同一项的 `reason` 写「10-06 07:58 出发 → 10-08 06:45 抵达，历时 46 小时 47 分，跨天」，`INBOUND_MULTI_DAY` 也写「回程跨 3 天…要 2026-10-08 才到家」。是**显示口径不精确**，不是编造，但会误导只扫时间列的用户 |
```

---

# 复现方式

```bash
# 1. 自动测试
D:/miniconda3/python.exe -m pytest -q

# 2. 单个 E2E case
D:/miniconda3/python.exe main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照" --user-id acceptance

# 3. 验收检查（provenance / 预算 / 时间线 / 产物）
D:/miniconda3/python.exe scripts/verify_run.py <run_id>
D:/miniconda3/python.exe scripts/verify_run.py --latest bj_cd

# 3b. 四个 case 串行重跑（自带单实例锁，日志落 _acceptance/<name>.log）
bash _acceptance_runs.sh

# 4. Provider smoke + fallback
D:/miniconda3/python.exe scripts/provider_smoke.py
D:/miniconda3/python.exe scripts/provider_smoke.py --with-fallback
# 4b. 只重试某一个 Provider（区分瞬时故障与配置错误，见 §36.3）
D:/miniconda3/python.exe scripts/provider_smoke.py --only tuniu --no-markdown
# 4c. 只跑 §36.5 的三条降级验证：--only 给一个匹配不到的子串，把探活行全部滤掉
D:/miniconda3/python.exe scripts/provider_smoke.py --only __fallback_only__ --with-fallback --no-markdown

# 5. 起服务（前端 live 模式需要 frontend/.env.local，见 frontend/.env.example）
D:/miniconda3/python.exe -m uvicorn app.api:api --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

> 注意：`_acceptance_runs.sh` 里的 python 调用必须带 `PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8`
> （脚本已设）。直接在 git-bash 里跑 `python scripts/case_summary.py` 会因 GBK 编码写不出 `¥` 而报
> `UnicodeEncodeError`。
