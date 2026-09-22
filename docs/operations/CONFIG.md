# 配置说明

一句话：所有开关、阈值、Provider 优先级、Agent 护栏与降级、预算与演进开关都集中在 `app/config.py`，这里是逐项说明。

## 1. 两类配置，不要混

| 类 | 位置 | 谁读 | 例子 |
| --- | --- | --- | --- |
| 运行配置 `TravelPlanConfig` | `app/config.py` | 主规划（护栏 + 工具预算）、API、Evolution、Session/Discovery、预热 Agent | `MAX_AGENT_STEPS`、`AGENT_RUN_TIMEOUT_SECONDS`、`MAX_RUN_TOKENS`、`MODEL_PRICE_*`、`DISCOVERY_*`、`PLANNING_SESSION_TTL_MINUTES`、`BADCASE_ENABLED`、`EVOLUTION_ENABLED` |
| 行程阈值 `PlannerTuning` | `app/config.py` | `app/planner.py`、`app/selection.py` | `TP_MAX_ITEMS_PER_DAY`、`TP_MIN_CANDIDATE_SCORE`、`TP_*_WEIGHT`、`TP_MUST_PLACE_BONUS`、`TP_PACE_AUTO_*` |
| 运行时覆盖 `_RUNTIME_OVERRIDES` | `app/config.py` + `runtime_config` 表 | 管理端 `PATCH /api/v1/admin/config` | 白名单见 `EDITABLE_KEYS`；不重启即生效、落库、重启后仍在 |

Secret（API Key）**只在环境变量里**，不写进代码、不写进 Prompt、不进日志、不通过 API 回显。
模型接入还支持两套备用端点（`MODEL_*_bk1` / `MODEL_*_bk2`），它们同样是 Secret，
**不**进 `TravelPlanConfig`、**不**进 `EDITABLE_KEYS`（见 §2.1）。

## 2. 运行配置 `TravelPlanConfig`

读取方式：`current_config()`，每次调用都重新读环境变量（长驻进程里改环境不必重启）。

### 预算与成本护栏

| 环境变量 | 默认 | 作用 | 风险 |
| --- | --- | --- | --- |
| `MAX_RUN_TOKENS` | 无 | 单次 run 累计 token 上限 | **主规划路径真正生效的护栏**：`_TokenBudgetGuard` 在每次模型调用**前**检查，超了就停循环（已交卷的行程保留）。默认不设 = 不限 |
| `BUDGET_ENABLED` | `false` | 预算总开关 | **当前无消费点**：除管理端展示外全仓无人消费（包括旧流程），改它不改变任何行为 |
| `MAX_RUN_COST` | 无 | 单次 run 成本上限 | **当前无消费点**（只在 Admin 时间线的"运行配置"事件里展示） |
| `MAX_LLM_CALLS` | 无 | 单次 run 模型调用上限 | **当前无消费点** |
| `MAX_TOOL_CALLS` | 无 | 单次 run 工具调用上限 | **当前无消费点** |
| `MAX_RUN_SECONDS` | 无 | 单次 run 墙钟上限 | **当前无消费点**；Agent 路径的墙钟上限是 `AGENT_RUN_TIMEOUT_SECONDS`（见 §2.1） |

> 实际统计值写在 `run_metrics` 表与 `outputs/<run_id>/metrics.json` 里。接口拿不到真实价格回执时
> `cost` 为 `null`（不猜）；若维护者配了模型单价（见下），`cost` 会按用户填的单价估算，并标
> `cost_source="user_price"`。

### 模型单价（元 / 百万 token）

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `MODEL_PRICE_INPUT_PER_MILLION` | 无 | 输入 token 单价（元/百万） |
| `MODEL_PRICE_OUTPUT_PER_MILLION` | 无 | 输出 token 单价（元/百万） |
| `MODEL_PRICE_CACHED_PER_MILLION` | 无 | 缓存 token 单价（元/百万）；不配则回落到输入单价 |

只有**同时配了输入与输出单价**、且本次 run 拿得到 token 用量时才算 `cost`，并标 `cost_source="user_price"` ——
它是"用户填的估计值"，不是 Provider 回执。主规划路径的 metrics 由
`app/agent_runner.py::_metrics` 写出（`cost` / `cost_source` / `agent` 块），
管理端 `/api/v1/admin/runs/{id}` 的 `metrics` 一并回传；旧固定流程的
`app/workflow.py:persist_metrics` 会额外写 `cost_breakdown`（含各 token 数与单价快照），
Agent 路径没有这一段。
没配单价或拿不到 token → `cost=null`（不推算）。

### 2.1 主规划 Agent Loop（护栏与模型降级）

这一组决定"Agent 能跑几步、能跑多久、能烧多少 token、主模型挂了怎么办"。
它们在管理端 Config 页可运行时修改（`EDITABLE_KEYS` 里有后两项）。

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `MAX_AGENT_STEPS` | `40` | Agent 循环最大步数（传给 LangGraph 的 `recursion_limit`，同时覆盖模型调用与工具调用）。超限时：没交卷就 FAILED，交了卷就保留行程并标降级 |
| `AGENT_RUN_TIMEOUT_SECONDS` | `600` | 一次 Agent 规划的墙钟上限（秒）。两层超时：内层发起取消，外层守护线程 + join 预算；到点放弃等待 |
| `MAX_RUN_TOKENS` | 无 | 累计 token 上限；`_TokenBudgetGuard` 在每次模型调用**前**检查，超了就停循环（已交卷的行程保留）。默认不设 = 不限 |
| `MAX_PREHEAT_AGENT_STEPS` | `36` | 城市预热 Agent 的最大步数（预热是"逐条入库"，工具调用本来就多） |
| `PREHEAT_AGENT_TIMEOUT_SECONDS` | `360` | 一次城市预热的墙钟上限；超时按"如实失败 + 不写缓存"处理，坏城市尽快让位 |

模型降级链**不走 `TravelPlanConfig`**（那三个值是 Secret）：`.env` 里主模型之外的
`MODEL_NAME_bk1` / `MODEL_BASE_URL_bk1` / `MODEL_API_KEY_bk1`（以及 `_bk2`）由
`app/config.py::model_endpoints()` 读取，`app/agent.py::build_model_with_fallbacks` 装成
`ModelFallbackMiddleware`。规则：

- 一套三项（name / base_url / key）**缺任意一项就整套跳过** —— 半套配置只会在运行时 401；
- 主模型缺失时 bk1 顶上（"主不可用"包含"主没配"）；
- 一个都没配全 → 直接 FAILED，错误文案点名要配哪些变量；
- 触发时机是**抛错即降级**（不是同一个模型重试 N 次再换）。

### 2.2 Jev（旧固定流程遗留）

主规划已切到 Agent Loop，主路径不再调用 Jev；下列配置只影响旧固定流程
（`app/workflow.py` + `app/decision/`）与历史 run 复核，改它们不会改变主路径行为。

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `JEV_ENABLED` | `true` | 关掉后所有 Jev 调用返回 `SKIPPED`，流程走确定性路径 |
| `JEV_PLANNER_ENABLED` | `true` | Planner 是否使用 Jev 的三处软决策 |
| `JEV_TIMEOUT_MS` | `1500` | 单次调用超时；超时即 fallback |
| `JEV_MIN_CONFIDENCE` | `0.70` | 低于该置信度的结论视为 `LOW_CONFIDENCE`，不采纳 |
| `JEV_MAX_CALLS_PER_RUN` | `5` | 单次 run 的调用上限，超过返回 `BUDGET_EXCEEDED` |
| `JEV_FALLBACK_ENABLED` | `true` | 失败时是否降级到确定性路径（关掉会让主流程失败，不建议） |
| `JEV_MODEL` | `jev-latest` | 模型名 |
| `JEV_BASE_URL` | TypeSafe System One 端点 | 接口地址 |
| `JEV_API_KEY` / `TYPESAFE_API_KEY` | 无 | Secret；两者取其一即可，**不回显** |

### Bad Case / Benchmark / Evolution

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `BADCASE_ENABLED` | `true` | 关掉后 run 结束不做规则检测（不写 badcases 表与 badcases.json） |
| `BENCHMARK_ENABLED` | `true` | 保留开关（API 里 Benchmark 端点始终可用，套件不存在时返回 503） |
| `EVOLUTION_ENABLED` | `false` | Evolution 默认关闭；开启后管理端才能触发（否则 409） |

### 引导式旅程（Planning Session / Discovery）

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `DISCOVERY_ENABLED` | `true` | 关掉后建会话不进后台 Prefetch（会话停在 `COLLECTING`/`PENDING`） |
| `PLANNING_SESSION_TTL_MINUTES` | `45` | 会话 TTL（分钟），最小 5；过期即 `EXPIRED` |
| `DISCOVERY_PLACE_LIMIT` | `20` | Discovery 给用户的 POI 候选总数上限（避免一次丢 50 个） |
| `DISCOVERY_PLACE_PER_CATEGORY` | `5` | **当前无消费点（死配置）**：除 `app/config.py` 的读取与 `EDITABLE_KEYS` 登记外全仓无人消费，改它不改变任何行为 |
| `DISCOVERY_HOTEL_PAGES` | `3` | 酒店候选翻几页（途牛首页只给一页） |
| `DISCOVERY_WORKERS` | `4` | Discovery 并行取数线程数（交通/酒店/攻略/地点抽取四条线） |
| `DISCOVERY_GRACE_SECONDS` | `3.0` | 用户点「开始规划」时，若 Discovery 还在跑，最多再等这么久（秒） |

详见 [用户旅程](../product/USER_JOURNEY.md)。

### 跨会话城市知识缓存

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `CITY_CACHE_GUIDE_TTL_DAYS` | `7` | 城市攻略（提及 + **正文** + 检索词）的新鲜期（天） |
| `CITY_CACHE_POI_TTL_DAYS` | `15` | 城市 POI 基础信息的新鲜期（天） |

两个 TTL 都由 `app/city_cache.py` 读取：攻略文本变化快、POI 基础信息稳定得多，所以分开过期，
避免为了地址重查而频繁刷新攻略。两者都在 `EDITABLE_KEYS` 里，可运行时改；
复用细节见 [数据复用与实体](../architecture/DATA_REUSE_ENTITY.md)。

> 判定口径是"**所有**行都在 TTL 内才算新鲜"，并且每次写入会顺手删掉各自 TTL 之外的行
> （`store.prune_city_cache`）。不清理的话，一条再也搜不到的旧攻略会让这座城市永远 stale，
> 于是每个新会话都触发一次后台全量刷新。攻略正文的条数上限沿用 `EVIDENCE_MAX`。

### 性能（并发 / 取数上限 / 超时 / 对冲）

这一组决定"一次 run 打多少个 Provider 请求、并发几路、等多久放弃"。它们全部集中在
`app/config.py`，节点里**不再有任何写死的同类数字**——调参不需要改流程代码。

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `PROVIDER_MAX_CONCURRENCY` | `4` | 互不依赖的 Provider 查询并发上限（交通四路、POI 关键词等） |
| `POI_VERIFY_MAX_CONCURRENCY` | `4` | POI 详情并发上限 |
| `ROUTE_MAX_CONCURRENCY` | `4` | 路线查询并发上限 |
| `LLM_MAX_CONCURRENCY` | `2` | 独立模型调用并发上限（装在 `LLM` 客户端这一个收口点上） |
| `POI_QUERY_LIMIT` | `8` | 一次节点内最多拿几个关键词去搜 POI |
| `POI_PAGE_SIZE` | `10` | 单次 `search_poi` 翻几页 |
| `POI_DETAIL_LIMIT` | `6` | 最多给几个地点补详情（营业时间/地址） |
| `MAX_ROUTE_LOOKUPS` | `14` | 最多查几段市内路线（**不做** POI × POI 全连接） |
| `TICKET_LOOKUP_LIMIT` | `3` | 最多查几个景点的门票 |
| `DISCOVERY_MAX_PLACES` | `20` | 攻略里最多提取多少个原始地名送去核实 |
| `DISCOVERY_POI_VERIFY_LIMIT` | `20` | Discovery 最多核实多少个 POI |
| `USER_VISIBLE_POI_LIMIT` | `20` | 用户可见 POI 候选上限（历史别名 `DISCOVERY_PLACE_LIMIT`） |
| `PLANNER_POI_LIMIT` | `15` | 进入深度验证的候选上限（MUST/WANT 不占名额） |
| `ROUTE_MAX_LEG_METERS` | `60000` | 路线初筛阈值：直线超过它判定为非相邻路段，不查市内路线 |
| `EXTRACT_EVIDENCE_LIMIT` | `4` | 最多对几条攻略调用模型抽地点 |
| `EXTRACT_TEXT_CHARS` | `1200` | 单篇攻略送给模型的正文上限（字符） |
| `EXTRACT_BATCH_SIZE` | `2` | 一次模型调用合并几篇攻略（批内共享 prompt；批越小，一次超时损失的地点越少） |
| `EVIDENCE_MAX` | `40` | 一次 run 最多保留多少条攻略/网页证据 |
| `SOCIAL_QUERY_LIMIT` | `3` | 社媒检索最多用几个 query |
| `WEB_QUERY_LIMIT` | `2` | 联网搜索最多用几个 query |
| `TRANSPORT_HEDGE_ENABLED` | `true` | 12306 慢时是否并发提前发一次途牛火车当备胎 |
| `TRANSPORT_HEDGE_WAIT_SECONDS` | `25` | 对冲时最多先等主源多久；超过且备胎已有非空结果就提前采用备胎 |

每个 Provider 一份超时预算（**不使用**一个全局值）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `TUNIU_TIMEOUT_SECONDS` | `90` | 途牛（机票/火车/酒店/门票） |
| `RAILWAY_12306_TIMEOUT_SECONDS` | `90` | 12306 MCP（含冷启动余量） |
| `AMAP_TIMEOUT_SECONDS` | `25` | 高德（超时按"未核实"处理，绝不用估算值冒充） |
| `TIKHUB_TIMEOUT_SECONDS` | `45` | TikHub（超时后走 MediaCrawler） |
| `MEDIACRAWLER_TIMEOUT_SECONDS` | `180` | MediaCrawler（拉子进程，可能卡在扫码登录） |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `30` | 联网搜索（Tavily） |
| `TICKET_TIMEOUT_SECONDS` | `15` | **按 Tool** 的更紧预算：门票只是"按景区名查渠道价"，不该共用途牛那份预算 |

模型调用按**用途**给预算（打满只是如实降级为 `TIMEOUT` 并落 Trace，不编造内容）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_TIMEOUT_SECONDS` | `180` | 默认预算（同时是硬上限） |
| `LLM_TIMEOUT_EXTRACT_SECONDS` | `120` | `extract_places`：一批里有多篇，按整批给 |
| `LLM_TIMEOUT_EXTRACT_RETRY_SECONDS` | `45` | 批抽取超时后按单篇重试一次的预算（救回证据，不无限延长等待） |
| `LLM_TIMEOUT_CRITIC_SECONDS` | `90` | `critic`：规则批评已先跑，模型批评等不到就降级 |
| `LLM_TIMEOUT_FINAL_SECONDS` | `120` | `final_answer`：用户直接读的正文，留足 |

能否在管理端运行时覆盖**以 `EDITABLE_KEYS` 为准**（见 §3.5），不必重启服务 —— 它不按前缀划分：
白名单里**包含 10 个 `TP_*`**（`TP_MIN_CANDIDATE_SCORE`、`TP_MAX_ITEMS_PER_DAY`、
`TP_DEFAULT_CLUSTER_KM`、`TP_AD_RISK_HIGH`、`TP_DAY_END_MINUTES`、`TP_HOTEL_PRICE_UNIT`、
`TP_HOTEL_RATING_WEIGHT`、`TP_HOTEL_LOCATION_WEIGHT`、`TP_MUST_PLACE_BONUS`、`TP_WANT_PLACE_BONUS`），
同时又**排除了** `JEV_MODEL` / `JEV_BASE_URL` / `BENCHMARK_ENABLED` / `DISCOVERY_WORKERS`
这些非 `TP_` 键。
调参前先跑 `python scripts/profile_run.py --latest` 看清时间花在哪一段。

## 3. 行程阈值 `PlannerTuning`

这些值原本是 `app/planner.py` 的模块常量，挪到配置里的目的是让 **Evolution 能提出一个
可执行、可度量的候选改动**（否则"候选改动"只是一句话，没法验证）。默认值与历史常量逐一
对齐，因此不设环境变量时行为不变。

| 环境变量 | 默认 | 作用 | 调大的后果 |
| --- | --- | --- | --- |
| `TP_MIN_CANDIDATE_SCORE` | `0.0` | 候选入选门槛 | 调大 → 入选更严、行程点更少 |
| `TP_MAX_ITEMS_PER_DAY` | `5` | 每天停留点数上限 | 调大 → 一天更满、更容易触发时间冲突 |
| `TP_DEFAULT_CLUSTER_KM` | `1.5` | 地理聚类半径 | 调大 → 更多点被当成"同一个区域"，折返可能增加 |
| `TP_AD_RISK_HIGH` | `70.0` | Critic 判"广告嫌疑高"的阈值 | 调小 → 更多项被建议人工确认 |
| `TP_DAY_END_MINUTES` | `1290`（21:30） | 每天结束时刻 | 调大 → 排得更晚，可能安排闭园后的参观 |

引导式旅程新增的阈值（默认值与历史行为对齐，不设就不改变结果）：

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `TP_TRANSPORT_PRICE_WEIGHT` | `1.0` | 交通比选的价格权重基准 |
| `TP_TRANSPORT_DURATION_WEIGHT` | `1.0` | 门到门时长权重基准 |
| `TP_TRANSPORT_COMFORT_WEIGHT` | `1.0` | 舒适度权重基准 |
| `TP_TRANSPORT_PREFERENCE_WEIGHT` | `1.0` | 偏好权重基准 |
| `TP_TRANSPORT_MODE_BONUS` | `30.0` | 命中"高铁/飞机"方式偏好的加分 |
| `TP_TRANSPORT_RED_EYE_PENALTY` | `30.0` | "不要红眼"约束的惩罚 |
| `TP_TRANSPORT_EARLY_PENALTY` | `20.0` | "不要太早"约束的惩罚 |
| `TP_TRANSPORT_TRANSFER_PENALTY` | `25.0` | "少换乘"约束的惩罚 |
| `TP_HOTEL_PRICE_UNIT` | `100.0` | 酒店价格换算单位（`100` 等价于历史 `每晚价格 / 100`） |
| `TP_HOTEL_RATING_WEIGHT` | `2.0` | 评分权重 |
| `TP_HOTEL_LOCATION_WEIGHT` | `0.0` | 位置项权重（`0` = 不把距离算进分，保持既有口径） |
| `TP_HOTEL_PREFERENCE_BONUS` | `20.0` | 命中住宿偏好的加分 |
| `TP_HOTEL_UNKNOWN_PRICE_PENALTY` | `30.0` | 酒店价格缺失时的惩罚 |
| `TP_HOTEL_TRANSIT_BONUS` | `30.0` | "交通方便"策略的加分 |
| `TP_HOTEL_LOCATION_RANGE_KM` | `5.0` | 位置项归一化尺度（5km 内线性衰减） |
| `TP_MUST_PLACE_BONUS` | `60.0` | POI 标「必去」的加分 |
| `TP_WANT_PLACE_BONUS` | `25.0` | POI 标「想去」的加分 |
| `TP_PACE_AUTO_RELAXED_MAX` | `3.0` | 节奏 auto：候选/天 ≤ 此值 → `relaxed` |
| `TP_PACE_AUTO_PACKED_MIN` | `4.5` | 节奏 auto：候选/天 ≥ 此值 → `packed` |
| `TP_PACE_AUTO_SHORT_DAY_BONUS` | `0.15` | 首/末日可用时间少时的折算系数 |

> **诚实说明**：`app/planner.py` 的 `TP_MIN_CANDIDATE_SCORE` / `TP_MAX_ITEMS_PER_DAY` /
> `TP_DEFAULT_CLUSTER_KM` / `TP_AD_RISK_HIGH` / `TP_DAY_END_MINUTES` 已接入 `planner.tuning()`。
> 交通/酒店策略权重（`transport_*` / `hotel_*` 里的 `*_WEIGHT` / `*_BONUS` / `*_PENALTY`）
> **同样已接入**：`app/selection.py` 的 `transport_weights` / `hotel_weights` 负责把它们汇总成
> 一组权重，`app/workflow.py:1196`（`_transport_score`）与 `:1368`（`_hotel_score`）都调用它们，
> `:1462` 还用 `hotel_weights` 的标签做策略说明。权重来自 `app/config.py` 的 `PlannerTuning`，
> 可用 `TP_*` 环境变量覆盖（其中 10 个 `TP_*` 键同时登记在 `EDITABLE_KEYS` 里，管理端可运行时改）。
> 见 [用户旅程](../product/USER_JOURNEY.md) 第 5 节。

读取方式：`current_tuning()` / `planner.tuning()`。Evolution 用
`app.config.override_env()` 临时覆盖，退出时精确还原（长驻进程里不会污染其他请求）。

## 3.5 运行时可编辑配置（管理端）

`app/config.py` 提供一层"运行时覆盖"，让维护者不重启服务就能调阈值：

- `EDITABLE_KEYS`：白名单 `{key: (类型, 最小值, 最大值)}`。只有白名单里的键可改，Secret
  （API Key / Admin Token）**永不在此列**。
- `validate_override(key, value)`：写入前校验类型与区间，非法抛 `ValueError`。校验必须在写入前做 ——
  把 `JEV_MIN_CONFIDENCE` 手滑写成 `8`（本意 `0.8`）会让所有 Jev 决策变成低置信度，而线上不会报错。
- `set_runtime_overrides(values)` / `runtime_overrides()`：整体替换 / 读取当前覆盖。
- 持久化：落 `runtime_config` 表（`store.set_runtime_config` / `list_runtime_config` /
  `delete_runtime_config`），重启后仍在。
- 生效：`PATCH /api/v1/admin/config`（body `{values, reset}`）写入后立即调用
  `set_runtime_overrides(...)`，本进程立刻生效。响应里带 `runtime_overrides` 与 `editable_spec`。
- `override_env()` 生效期间**环境变量优先**（`_source()` / `is_overriding()`）：Benchmark / Evolution
  的对照实验必须能跑在指定配置下，不能被线上覆盖值污染。

## 4. Provider / 部署相关环境变量

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY` | 无 | 模型接入（OpenAI 兼容）。另可配 `…_bk1` / `…_bk2` 两套备用（见 §2.1） |
| `TRAVELPLAN_LLM_TIMEOUT_SECONDS` | `180` | 单次模型调用墙钟上限 |
| `AMAP_API_KEY` | 无 | 高德 Web Service |
| `TIKHUB_API_TOKEN` | 无 | 小红书 / 抖音（只走免费额度） |
| `TUNIU_API_KEY` | 无 | 途牛（机票/酒店/火车/门票） |
| `TAVILY_API_KEY` | 无 | 通用网页检索（SuperHarness 自带） |
| `RAILWAY_12306_COMMAND` | `npx` | 12306 MCP 的**可执行文件**。只替换命令本身，`["-y", "12306-mcp"]` 是固定 args（`super_harness/.../capabilities/mcp/railway_12306.py`）；照"整串命令"填成 `npx -y 12306-mcp` 会把它当成一个可执行文件名去拉起，必然失败 |
| `TUNIU_COMMAND` | `tuniu` | 途牛 CLI 命令（Windows 下会自动找 `tuniu.cmd`） |
| `MEDIACRAWLER_DIR` | `data/vendors/MediaCrawler` | 本地 MediaCrawler 目录 |
| `TRAVELPLAN_DB_PATH` | `data/travelplan.db` | SQLite 位置（**未设 `DATABASE_URL` 时**才生效）。`Dockerfile` 里把它设成 `/data/travelplan.db`，但生产的数据落在 Neon（`DATABASE_URL`），容器本地盘只是临时产物目录 |
| `DATABASE_URL` | 无 | PostgreSQL（Neon）连接串，形如 `postgresql://...?sslmode=require`。**设了就用 Postgres**，未设则用本地 SQLite |
| `TRAVELPLAN_OUTPUT_DIR` | `outputs/` | 产物目录 |
| `TRAVELPLAN_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | 允许的前端来源（逗号分隔） |
| `TRAVELPLAN_ADMIN_TOKEN` | 无 | 管理端 Token。**不配则所有 `/api/v1/admin/**` 返回 503**（安全默认） |
| `TRAVELPLAN_COMMIT` / `SUPERHARNESS_COMMIT` | 无 | 覆盖版本号（部署环境没有 `.git` 时用） |
| `TRAVELPLAN_FIXTURE_VERSION` | `fixtures.v1` | Benchmark fixture 版本 |

前端（`frontend/`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | 后端地址（非 Secret） |
| `NEXT_PUBLIC_USE_MOCK_API` | `false` | `true` 用内置演示数据，不连后端 |
| `TRAVELPLAN_ADMIN_TOKEN` | 无 | **只给本地开发用**（`frontend/.env.local`）：配了就在打开 `/admin` 时自动填入，等价于免登录。它由同源服务端路由 `/admin/default-token` 下发，不进浏览器包；线上前端**不要**配这个变量 |

### 4.1 数据存储：SQLite（默认）还是 Postgres/Neon

- **不设 `DATABASE_URL`**：行为与从前**完全一致** —— 走 `TRAVELPLAN_DB_PATH`（默认
  `data/travelplan.db`）的本地 SQLite，不需要安装任何 Postgres 驱动。
- **设了 `DATABASE_URL`**：`TravelPlanStore()` 改用 Postgres。驱动是 `psycopg`（v3，
  `requirements.txt` 里的 `psycopg[binary]`），连接由 `app/db.py` 的进程内共享连接池管理
  （不再每次操作新建连接，避免消耗 Neon 连接数）。
- **显式传 `db_path` 时一律走 SQLite**：测试与 Benchmark 传临时库，即使开发机 `.env` 里
  有 Neon 连接串也不会把测试数据写到线上。
- **不会静默回退**：选了 Postgres 却连不上时会抛清晰异常（信息里带 `postgres` / `neon`），
  **绝不**悄悄退回本地 SQLite —— 否则线上会以为在写 Neon，实际写进容器临时盘。
- **自检**：`GET /api/v1/health` 的 `store.backend` 报 `sqlite` 或 `postgres`；Postgres 下另带
  脱敏后的 `store.target`（形如 `postgresql://***@host/db`）与 `store.host`，**不回显用户名/密码**。
- 两种后端的差异（占位符、`INSERT OR REPLACE`、`PRAGMA` → `information_schema`）全部集中在
  `app/db.py`，`app/store.py` 方法体只写一份 SQL。

> 管理端 Token 存在浏览器 `localStorage`（键 `travelplan_admin_token`），由运维人员在页面里填。
> 它**不能**放进 `NEXT_PUBLIC_*`：那样会被打进前端产物，等于公开。

## 5. Secret 清单（只列变量名）

`MODEL_API_KEY`（以及 `MODEL_API_KEY_bk1` / `MODEL_API_KEY_bk2`）、`AMAP_API_KEY`、
`TIKHUB_API_TOKEN`、`TUNIU_API_KEY`、`TAVILY_API_KEY`、`TRAVELPLAN_ADMIN_TOKEN`；
`JEV_API_KEY`（或 `TYPESAFE_API_KEY`）只在直接跑旧固定流程时才需要。

`.env` 已被 `.gitignore` 忽略。API 只报"配了没配"（`/api/v1/health`、`/api/v1/admin/config`
的 `secret_configured`），任何响应都不回显值。

## 6. 改配置的正确姿势

1. 先判断属于哪一类：**行为开关/护栏阈值 → 这里**；提示词 → `app/prompts.py`（主规划的算法）；
   领域算法 → `app/planner.py`；工具封装 → `app/travel_tools.py`；用户旅程/会话 →
   `app/sessions.py` + `app/discovery.py`；点选解释 → `app/selection.py`。
2. 加一个阈值时：写进 `TravelPlanConfig` 或 `PlannerTuning` 的字段 + `from_env()` 的读取，
   并给它一个有理由的默认值；不要散落成模块常量。
3. 如果这个阈值应该能被管理端**运行时修改**，同时登记进 `EDITABLE_KEYS`（`key → (类型, 最小, 最大)`）。
4. 如果这个阈值应该能被 Evolution 验证，就放进 `PlannerTuning` 并在
   `app/evolution.py:LEVERS` 里登记一条候选改动（评测入口是 `benchmark.runner.make_measure`，
   在临时库里跑 12 例离线确定性用例）。
5. 改完跑 `python -m pytest -q`；改了 prompt 要同时升 `TRAVEL_PLANNER_PROMPT_VERSION` 并实跑验证；
   改了 `FIXTURE_VERSION` 或 `benchmark/cases` 时要重跑基线（`python -m benchmark`，
   见 [Benchmark](../quality/BENCHMARK.md)）。
