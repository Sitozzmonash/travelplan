# 数据源与工具

一句话：说明真实数据从哪来、需要什么配置、失败时表现成什么样。

主规划路径里，Agent 只通过**旅行工具**访问这些能力；工具定义与展示名见
[主规划：Agent Loop](AGENT_LOOP.md) §3。

## 1. 全景

| 能力 | 类型 | 代码位置 | 用途 | 主 / 备 |
| --- | --- | --- | --- | --- |
| `railway_12306` | MCP (stdio) | `super_harness/superharness/capabilities/mcp/railway_12306.py` | 火车/高铁车次与余票 | 主 |
| `tuniu_travel` | Plugin | `…/capabilities/plugins/tuniu_travel/` | 机票 / 酒店 / 门票 / 火车备选 / 邮轮 / 节假日（后两项**业务链路不调用**，仅 `scripts/provider_smoke.py` 与 `tests/test_tuniu_travel.py` 在用） | 备（火车）/ 唯一（机票、酒店、门票） |
| `amap_cn` | Plugin | `…/capabilities/plugins/amap_cn/` | POI 检索与详情、地理编码、路线与耗时 | 唯一 |
| `tikhub_social` | Plugin | `…/capabilities/plugins/tikhub_social/` | 小红书 / 抖音攻略（只走免费额度） | 主 |
| `mediacrawler_social` | Plugin | `…/capabilities/plugins/mediacrawler_social/` | 攻略兜底（本地克隆 + 已登录会话） | 备 |
| LLM | HTTP | `app/llm.py`（通用调用）+ `app/prompts.py`（prompt） | **Discovery** 的检索词扩写与地点抽取；旧固定流程的意图解析 / Critic / 成文 | — |
| 主规划 Agent | LangChain / SuperHarness | `app/agent_runner.py` + `app/prompts.py` | 自己在循环里调上面这些能力，最后交出一份行程 | — |

`app/providers.py` 的 `ProviderHub` 是业务代码获取能力的**唯一入口**：统一信封、主备、缓存、
审计账本（`audit_entries()`）。`app/travel_tools.py` 把它包成 Agent 能调的工具，
业务代码不直接碰任何第三方 SDK。
**唯一例外**是 `scripts/provider_smoke.py`（探活脚本，按工具名直调 Plugin，用于区分"配置错"与"瞬时故障"）。

> 边界说明：`super_harness/superharness/capabilities/plugins/` 下真实有 6 个 manifest —— 上面 5 个
> 加上本项目不使用的 `secops_kit`；`capabilities/tools/http_fetch.py` 与 `capabilities/mcp/xiaohongshu.py`
> 也未被 `ProviderHub` 使用（MCP 只注册 `railway_12306`）。

## 2. 逐个说明

### railway_12306（MCP，主源）

- 启动：`npx -y 12306-mcp`（可用 `RAILWAY_12306_COMMAND` 覆盖），走 stdio，由 SuperHarness 的
  MCP 装载器按需拉起，不常驻。
- 依赖：Node ≥ 18 与网络。**容器部署必须装 Node**（见 `Dockerfile`）。
- 失败表现：`UNAVAILABLE` / `TIMEOUT` / `INVALID_RESPONSE`；此时大交通自动落到途牛。
- 已知现象：12306 偶尔返回早一天的车次；另外有预售期（约 15 天），超出预售期的日期必然查不到。
  这两件事都属数据源事实，plan 里如实体现（Agent 的 prompt 要求把不确定与未获取到的写进 warnings）。

### tuniu_travel（Plugin）

- 依赖：`TUNIU_API_KEY` + `tuniu` CLI（`npm i -g tuniu-cli`；Windows 下自动解析 `tuniu.cmd`）。
  可用 `TUNIU_COMMAND` 覆盖命令。
- 未配置 `TUNIU_API_KEY` 时**不会启动子进程**，直接返回 `AUTH_ERROR`。
- 超时/非零退出码映射为 `TIMEOUT` / `UNAVAILABLE`。
- **只导出查询类工具**，下单/支付类工具刻意不暴露（V1 只读）。

### amap_cn（Plugin）

- 依赖：`AMAP_API_KEY`（只从环境变量读）。缺 Key 时**不发请求**，直接 `AUTH_ERROR`。
- 用途：`search_poi` / `poi_detail` / `geocode` / `route`。
- 路线失败时**绝不假装路线是真的**：planner 用**具名常量**做粗估（`DETOUR_FACTOR`、`SPEED_KMH`），
  并把 `TravelLeg.verified=False`；旧流程据此下调置信度，Agent 路径则由 prompt 要求把这条
  写进 item 的理由与 plan 的 warnings。

### tikhub_social（Plugin，攻略主源）

- 依赖：`TIKHUB_API_TOKEN`。模块内 `FREE_ONLY` 是编译期常量：只允许免费额度。
- 先查额度（`free_credit`），额度不足直接 `FREE_CREDIT_EXHAUSTED`，**不动现金余额**；
  402 → 同样判为额度耗尽。
- 标注计费的端点不会被重试（避免重复计费）。

### mediacrawler_social（Plugin，攻略兜底）

- 依赖：本地克隆目录（默认 `data/vendors/MediaCrawler`，可用 `MEDIACRAWLER_DIR` 覆盖）。
- 目录不存在、未登录、触发风控、超时 → 一律返回空结果 + `UNAVAILABLE` / `TIMEOUT`，
  **绝不编造攻略内容**。

### LLM（`app/llm.py`）

- 配置：`MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY`；额外两套备用
  `MODEL_*_bk1` / `MODEL_*_bk2` 只在**主规划 Agent** 的降级链里生效（见下）。
- 单次超时基线 `TRAVELPLAN_LLM_TIMEOUT_SECONDS`（默认 180 秒）。
  **但它不是唯一预算**：`llm_timeout_for_tag()`（`app/config.py`）按**用途**给更紧的预算 ——
  `extract_places` 120s（按**整批**给，不是按单篇）、`extract_places_retry` 45s、
  `critic` 90s、`final_answer` 120s，其余 tag 用基线 180s。这四个可分别用
  `LLM_TIMEOUT_EXTRACT_SECONDS` / `LLM_TIMEOUT_EXTRACT_RETRY_SECONDS` / `LLM_TIMEOUT_CRITIC_SECONDS` /
  `LLM_TIMEOUT_FINAL_SECONDS` 覆盖。
  基线超时有**两个**入口，别混淆：`TRAVELPLAN_LLM_TIMEOUT_SECONDS` 由 `app/llm.py` 直接读
  （调用方没显式传 timeout 时生效），`LLM_TIMEOUT_SECONDS` 进 `app/config.py` 的
  `llm_timeout_seconds`，作为"按用途查表查不到"时的默认值。两者默认都是 180s。
  预算打满只是**如实降级**（`status=TIMEOUT` 落 Trace），不编造内容。
- **当前真正会发生的调用点**（`app/discovery.py`）：
  - `query_expansion`：把意图扩写成中文旅行攻略检索词；
  - `extract_places` / `extract_places_retry`：从攻略正文批量抽地点，批超时后按单篇重试一次。
  旧固定 12 步图（`app/workflow.py`，已退役）里还有 `parse_intent` / `critic` / `final_answer` /
  `preference_profile` / `llm_ranking` 这几个 tag，只在直接调用旧流程时才会出现。
- 主规划 Agent 的模型调用**不走 `app/llm.py`**：它由 LangChain / SuperHarness 直接发起，
  逐次 token 由 `app/agent_trace.py` 记进 `trace_spans`（component=llm）。
- 主模型失败时的降级链：`.env` 的 `MODEL_*` + `MODEL_*_bk1` + `MODEL_*_bk2`，由
  `app/agent.py::build_model_with_fallbacks` 装成 `ModelFallbackMiddleware`（抛错即降级）；
  某一套三项（name / base_url / key）配不全就整套跳过，不半配一套。
- 失败表现：`LLMResult.status != OK` → 该处降级到确定性的代码路径，并写入 audit 的
  `llm_calls` 与 `degradations`。**任何一处失败都不会让整次规划失败。**

## 3. 降级统一语义

`PROVIDER_STATUSES`（`app/providers.py`）**只有下面 8 个**：

| 状态 | 含义 | 谁会产生 |
| --- | --- | --- |
| `OK` | 成功 | 全部 |
| `EMPTY` | 调用成功但没有数据 | 高德、途牛、TikHub |
| `UNAVAILABLE` | 服务不可用 / 未配置 / 依赖缺失 | 全部 |
| `TIMEOUT` | 超时 | 全部 |
| `AUTH_ERROR` | 缺 Key 或 Key 无效（**未发出请求**） | 途牛、高德、TikHub |
| `RATE_LIMIT` | 被限流 | 高德、TikHub |
| `FREE_CREDIT_EXHAUSTED` | 免费额度耗尽（不动现金） | TikHub |
| `INVALID_RESPONSE` | 返回结构不符合契约 | 12306 MCP、Plugin 工具 |

**已知缺口：TikHub 的 `SAFE_MODE_BLOCK`。** TikHub Plugin 在"无法证明这次调用不会先扣现金
余额"时会返回 `SAFE_MODE_BLOCK`
（`super_harness/superharness/capabilities/plugins/tikhub_social/tools/search.py`），
但它不在 `PROVIDER_STATUSES` 里，经 `app/providers.py::_status_of()` 会被归一成
`EMPTY`；而 `EMPTY` 不在 `FALLBACK_STATUSES`。后果是这一路
**既不会显示为 `SAFE_MODE_BLOCK`、也不会 fallback 到 MediaCrawler** —— 观测上看起来像
"TikHub 搜了但没搜到"，与真实原因（安全模式拒发请求）不符。此处如实记录，尚未修复。

`INVALID_RESPONSE` 的产生方不止 12306 MCP：Plugin 工具同样会产
（工具返回无法解析为空 payload、途牛航班、TikHub 搜索）。

三条铁律：

1. **失败也要留痕**。"查了但没查到"与"根本没查"是两件事：失败的调用同样写进
   调用账本（`provider_calls` 表 + `audit_report.json:provider_calls`，`status != OK` + note）。
2. **绝不用假数据兜底**。没有真实价格/时刻/路线时，宁可缺失、宁可告警，
   也不生成一个"看起来合理"的数字。Agent 侧同理：工具返回 `status != OK` 或 `items` 为空时，
   prompt 要求换源 / 换关键词 / 换日期重试，重试仍拿不到就标 unknown。
3. **凭据不进账本**。调用参数里的 `api_key/token/authorization/secret` 在存储边界
   （`store._scrub_query`）被替换成 `***`，任何接口不回显明文。

## 4. Provider 调用账本与健康观测

`app/providers.py:ProviderHub` 的调用账本（`audit_entries()`）之外，还有一张**观测用**的
SQLite 表 `provider_calls`（`app/store.py`），它不带 `runs` 外键。原因：引导式旅程的
Discovery 调用发生在 Planning Session 里，那时还没有正式 run；写 `sources` 会撞外键或污染
运行列表。

```text
sources        = 这次 run 用了什么证据（业务，带外键）
provider_calls = 谁在什么时候调了哪个数据源、成不成、多快（观测，无外键）
```

写入方两处（都在 `provider_calls` **表**）：

| 写入方 | `source_type` | 场景 |
| --- | --- | --- |
| `app/sessions.py:_record_provider_calls` | `discovery` | 会话 Prefetch（无 run_id） |
| `app/workflow.py:_record_provider_calls`（旧固定流程的 finalize） | `run` / `benchmark` | 直接跑旧流程时才有 |

> **已知缺口**：主规划 Agent 路径**不**把 Provider 调用写进 `provider_calls` 表 ——
> 它的调用只出现在 `outputs/<run_id>/audit_report.json` 的 `provider_calls` 段
> （由 `_build_audit` 从 ProviderHub 的调用账本实时读取）与 `run_metrics.tool_calls`。
> 因此管理端 Provider Health 页看不到 agent 路径的真实调用，只有 Discovery 与（若跑过）
> 旧流程的。此处如实记录，尚未修复。

`sources` 表还有第三种来源：**城市知识库复用的攻略正文**。跨会话复用正文时本次并没有出网，
所以这些行由 `app/agent_runner.py` 复用的 `_materialize_city_evidence` helper 写成
`status="CACHED"`（`STATUS_CACHED`，**不属于**上面的 `PROVIDER_STATUSES` 词表），
`fetched_at` 保留**原始抓取时间**，`normalized_json` 带
`{origin: "city_cache", city_cache_updated_at: ...}`，并按 `{run_id}-cache-{n}` 重新分配 source_id
（`sources.source_id` / `evidence.evidence_id` 都是全局主键，沿用上一次会话的 id 会把上一轮 run
的归属改掉）。它们**不写进 `provider_calls`**，因此不会污染 Provider Health 的调用统计。

`GET /api/v1/admin/providers` 用 `store.provider_call_stats(limit_per_provider=200)` 聚合每
Provider **最近 N 次真实调用**，给出 status / 成功率 / 失败率 / timeout / fallback / 平均与 P95
延迟 / tools / sources / configured；**没有历史 = UNKNOWN 而不是失败**，且不主动打第三方接口
（不做假探活）。完整口径见 [可观测性](OBSERVABILITY.md)。

`source_type` 实际有三种取值 —— `discovery`（会话 Prefetch）、`run`（旧流程的正式 run，含 CLI 的
`source="cli"`）、`benchmark`（Benchmark 用例），因此能回答"某个 Provider 在 Discovery 阶段
大量超时"这类问题；**当前正式 run 的主路径不写这张表**（见上面的已知缺口）。
