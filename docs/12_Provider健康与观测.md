# 12 Provider 健康与观测

一句话：说明 Provider 调用账本 `provider_calls` 记什么、Provider Health 怎么算、为什么"没有历史"显示 UNKNOWN 而不是失败。

代码位置：`app/store.py`（表 + 聚合）、`app/sessions.py:_record_provider_calls`（Discovery 写入）、`app/workflow.py:_record_provider_calls`（正式 run 写入）、`app/api.py:_provider_status` / `admin_providers`（HTTP）。

## 1. 为什么要有独立的调用账本

新用户旅程里，**Discovery 的 Provider 调用发生在 Planning Session 里，那时还没有正式 run**。如果把这些调用写进 `sources`（`sources.run_id` 有指向 `runs` 的外键），要么撞外键、要么污染运行列表。所以新增一张**不带 runs 外键**的观测表：

```text
sources         = 这次 run 用了什么证据（业务，带外键，落进 plan.sources）
provider_calls  = 谁在什么时候调了哪个数据源、成不成、多快（观测）
```

Provider Health 只读 `provider_calls`，因此它既能统计正式 Run，也能统计还没有 run 的 Discovery。`GET /api/v1/admin/providers` 与 `/api/v1/admin/runs/{id}` 返回的 `provider_calls` 不是同一份：前者是观测账本，后者仍是 `store.list_sources(run_id)`（业务证据）。

## 2. 表结构与写入方

`provider_calls`（`app/store.py:SCHEMA`）：

```text
call_id(主键)  provider  tool  status  source_type  source_id
run_id  session_id  fetched_at  duration_ms  returned  error  fallback  query_json
索引： (provider, fetched_at) / (source_type, fetched_at)
```

写入方两处（都走 `store.save_provider_calls`，`INSERT OR REPLACE` 幂等）：

| 写入方 | `source_type` | `run_id` / `session_id` |
| --- | --- | --- |
| `app/sessions.py:_record_provider_calls` | `discovery` | 只带 `session_id` |
| `app/workflow.py:_record_provider_calls`（在 `node_finalize` 里调用） | `run` / `benchmark` | 带 `run_id`；`session_id` 列取 `state["source_session_id"]`（`app/workflow.py:4789`，列名见 `app/store.py:409-424`） |

两处都**顺手过滤凭据类键**（`api_key/token/authorization/secret`），并把 `store._scrub_query` 作为最后一道存储边界（见第 6 节）。写库失败只吞掉异常（观测不该反过来弄坏规划）。

> **城市知识库复用的攻略不计入这张表。** 跨会话复用正文时本次并没有出网，所以
> `workflow._materialize_city_evidence` 只往 `sources` 写 `status=CACHED` 的行，
> **不写** `provider_calls` —— 否则 Provider Health 的调用次数 / 成功率 / P95 延迟会被
> "凭空多出来的调用"污染，而它本来的用途正是回答"某 Provider 到底稳不稳"。
> 要看复用明细，走 `audit_report.json` 的 `cached_evidence` 段或管理端 Run Detail 的
> `sources` 列表（`GET /api/v1/admin/runs/{run_id}` 的 `provider_calls` 字段读的就是
> `store.list_sources`，因此 CACHED 行会出现在那里，与真调用区分得开）。

## 3. 统计口径

`store.provider_call_stats(limit_per_provider=200)` 按 Provider 聚合：

```text
每个 Provider 取**最近 N 次真实调用**（N=limit_per_provider，默认 200）
```

用固定样本量而不是时间窗，是为了让调用量差几十倍的 Provider 之间的成功率可比。每个 Provider 聚合出：

```text
calls / successes / failures / timeouts / auth_errors / rate_limited / empty /
fallback_count / last_call_at / last_success_at / last_failure_at /
avg_latency_ms / p95_latency_ms / tools / sources / last_error / last_status
```

- `failures` = `status != OK` 的调用数；只有 `status == OK` 算成功。
- `p95_latency_ms` 用 `store._percentile` 的**最近邻法**（不插值）：样本很少时插值等于在编数字。
- Provider 名称从数据**动态枚举**（`store.provider_names()`），页面不写死清单。

## 4. 状态阈值（`app/api.py`）

```text
PROVIDER_SAMPLE_FOR_STATUS      = 5     # 少于 5 次不下"健康"结论
PROVIDER_DEGRADED_FAILURE_RATE  = 0.2   # 失败率 ≥ 20% → DEGRADED
PROVIDER_UNAVAILABLE_FAILURE_RATE = 0.6 # 失败率 ≥ 60% → UNAVAILABLE
```

`_provider_status(stats)` 的判定顺序：

```text
calls == 0                        → UNKNOWN
末次 status ∈ {AUTH_ERROR, UNAVAILABLE} 且 calls ≥ 5：
        失败率 ≥ 60% → UNAVAILABLE，否则 DEGRADED
calls < 5                         → 末次 OK 则 UNKNOWN，否则 DEGRADED
失败率 ≥ 60% → UNAVAILABLE；≥ 20% → DEGRADED；否则 HEALTHY
```

状态取值：`HEALTHY / DEGRADED / UNAVAILABLE / UNKNOWN`。

## 5. UNKNOWN 的语义（重要）

**没有调用历史 ≠ 故障。** 两种情形都显示 `UNKNOWN`：

1. 该 Provider 在库里没有任何 `provider_calls` 记录；
2. 有记录但样本 < 5 且最近一次是 OK（样本太少不下健康结论）。

此外，**配置了但最近没被调用过的 Provider 也会出现在列表里**（`_provider_configured()` 枚举 12306 / tuniu / amap / tikhub / mediacrawler / tavily / jev），状态 `UNKNOWN`、各项计数为 0。否则运维会以为"这个数据源不存在"，而不是"这段时间没用到"。把"没有数据"画成红色会让人去查一个根本没坏的东西。

## 6. 脱敏边界

凭据在**存储边界**脱敏，不只靠调用方过滤：

- `store._scrub_query(query)`：`_SENSITIVE_QUERY_KEYS = (api_key, apikey, key, token, access_token, authorization, secret, password)`，命中就把该键的值替换成 `***`，其余键原样保留（Provider Health 要能看到"查了什么"）；
- `save_provider_calls` 落库前一定过 `_scrub_query`；
- 写入方（sessions / workflow）在构造 `query` 时再过滤一层 `api_key/token/authorization/secret`；
- `GET /api/v1/admin/providers/{provider}` 的明细里，`query` 只保留解释"查了什么"的键值，**不回显任何密钥**（`tests/test_admin_observability.py` 断言明文 secret 不出现在响应里）。

`error` 字段截断到 400 字符。

## 7. API

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/api/v1/admin/providers?limit_per_provider=200` | `{items, summary, note}` |
| GET | `/api/v1/admin/providers/{provider}?limit=20` | `{provider, label, configured, status, status_reason, stats, calls, note}` |

`status_reason`（`app/api.py:1400-1402`，取值定义在 `:1243-1261`）给 `status` 一个具体说法：
`no_history` / `insufficient_sample` / `ok` / `elevated_failure_rate` / `high_failure_rate`。

`items[]` 每张卡：`provider / label / configured / status / status_reason / calls / successes / failures / timeouts / auth_errors / rate_limited / empty / fallback_count / last_call_at / last_success_at / last_failure_at / avg_latency_ms / p95_latency_ms / tools / sources / last_error / last_status / success_rate / failure_rate`（`status_reason` 见 `app/api.py:1323`）。

`summary` = `{providers, healthy, degraded, unavailable, unknown, needs_attention[]}`；`needs_attention` 列出 DEGRADED/UNAVAILABLE 的 Provider。`note` 说明统计口径与"不主动打第三方接口"。

Provider 名称的中文标签在 `app/api.py:PROVIDER_LABELS`，未列出的 Provider 原样显示（允许动态扩展）。明细接口按 `fetched_at` 倒序返回最近 `limit` 次调用，带 `source_type` / `session_id` / `run_id`。

> 前端 `/admin/providers` 的响应契约是 `{items, summary, note}`（后端已按此形状返回）。

## 8. 部署与"不做假探活"的取舍

Provider Health **只基于真实调用数据**，不做"实时探活"，也不为了刷新管理页去调第三方付费 API：

- 页面刷新不会产生任何 Provider 调用；
- `configured` 只反映环境变量是否配置（如 `AMAP_API_KEY`），不代表能连通；
- 想要主动连通性抽检，用 `scripts/provider_smoke.py` 或 Benchmark `--live`，而不是让管理页去探活。

在 Vercel / 容器上的含义：Provider Health 的两个接口都是**纯读库**（SQLite），不需要 Node / MCP / 子进程，也不需要出站网络。因此它们与其它管理接口一样，可以放在常驻容器里；而 Vercel Serverless 因为不保证进程常驻、`/tmp` 不共享，`provider_calls` 也写不进去（同 [09_部署说明.md](09_部署说明.md) 的结论）。换句话说，"不做假探活"让 Provider Health 成为**只依赖数据库**的观测页，部署形态上最轻。

## 9. 与 Planning Session / Bad Case 的关系

- Discovery 的调用（`source_type=discovery`）让人能区分"`tikhub` 在正式 Run 正常、但 Discovery 阶段大量超时"这类问题（任务书 §11）；
- 正式 run 结束时，`app/workflow.py:_user_journey_summary` 会**用调用账本反推** `prefetch_reused`：匹配的是**真实 Tool 名**（`app/workflow.py:5213-5232`），按线分组：
  - transport = `get-tickets` / `railway_12306_get-tickets` / `tuniu_search_trains` / `flight` / `tuniu_search_flights`
  - hotels = `hotel` / `tuniu_search_hotels`
  - social = `xiaohongshu` / `search_xiaohongshu` / `search_xhs_via_mediacrawler` / `douyin` / `search_douyin` / `search_douyin_via_mediacrawler` / `web_search`
  - places = `search_poi`

  某条线的名字里**任意一个**出现在本次 run 的账本里，就说明它自己又查了一遍 = 没复用；
  这是**用证据推出来的**，不是标记位（见 [06_BadCase机制.md](06_BadCase机制.md) 的 `prefetch_not_reused`）。
  `app/workflow.py:5207-5212` 的注释专门记下了教训：上一版这里写的是 `search_trains` / `search_flights` 这类
  **Hub 方法名**，三类名字里一个都匹配不上 —— 后果是"本次真的补查了交通"被审计写成"复用了 Discovery"，
  读的人据此以为预取生效，实际那次 run 白等了一遍 Provider。

> **benchmark / cli 已接通，过滤有效。** `benchmark/runner.py:120` 显式传 `source="benchmark"`，
> `main.py:70` 传 `source="cli"`，`app/workflow.py:5484` 的默认值是 `"quick"`，并由 `:5497-5501`
> 把它写进 `runs.source`。因此：
> - `provider_calls.source_type` 有 `discovery` / `run` / `benchmark` 三种；
> - `runs.source` 有 `quick` / `guided` / `cli` / `benchmark` 四种；
> - `GET /api/v1/admin/runs` 的 `include_benchmark=false` 过滤（`app/store.py:759-762` 与
>   `:794-795` 的 `COALESCE(r.source, 'quick') <> 'benchmark'`）**是有效的**，确实会把
>   Benchmark 用例运行排除在运行列表之外。
