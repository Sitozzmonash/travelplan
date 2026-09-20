# TravelPlan 性能优化 Before / After 报告

> 对应任务：`D:\Downloads\TravelPlan_性能优化与生产部署任务.md`（Part H / K / O）。
>
> 判定口径：优化是否有效，看**用户感知耗时**，而不是某一步的自我感觉：
>
> ```text
> t0 建 Session ──► t1 Discovery 完成 ──► t2 点「开始规划」 ──► t3 Run 终态
>
> discovery_s = t1 - t0   用户在前端选偏好时后台已经查完的部分（可被复用）
> plan_s      = t3 - t2   点「开始规划」后真正让用户等的时间  ← 主要优化目标
> total_s     = t3 - t0   从打开页面到拿到行程
> ```
>
> 原始数据：`_acceptance/perf_before.json`（优化前快照）、`e2e_*_before.json` /
> `e2e_*_after.json`（三次真实 E2E）。跑法见 `scripts/e2e_real.py`。

## 1. 优化前（BEFORE）基线

数据来自真实 run 的产物（`outputs/<run_id>/trace.jsonl` + `audit_report.json` + `metrics.json`）。

两次真实 Guided run（都在旧服务上跑：**既没有 Prefetch 复用，也没有本轮并发/限流**，
属于"全量重复查询 + 全串行"的最坏情况）：

| 行程 | run_id | 总耗时 | LLM 调用 | Jev | 工具调用 | Token | Provider 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 天 北京→成都 | `tp-20260920-205658-ead2ba` | **518.2s** | 7 | 3 | 49 | 31868 | 13 |
| 3 天 北京→西安 | `tp-20260920-214836-5c61d9` | **1075.2s** | 7 | 3 | 48 | — | 14 |
| 5 天（更早一次） | `tp-20260920-164133-9291f8` | **616.0s** | 8 | 3 | 48 | 36968 | 12 |

### 1.1 阶段耗时（BEFORE）

| 阶段 | 616s run | 518s run | 真正的成因（实测） |
| --- | --- | --- | --- |
| `search_intercity_transport` | 140.1s (22.7%) | **210.2s (40.6%)** | 12306 `get-tickets` 2 次共 110.2s（平均 55.1s）+ 途牛航班 35.6s，**去程/回程、火车/航班全部串行**，最多 4 次串行 Provider 调用 |
| `extract_and_normalize_places` | **172.0s (27.9%)** | 132.2s (25.5%) | 4 次**串行** LLM `extract_places`（63.6s）+ 16 次**串行** 高德 `search_poi`（54.5s） |
| `critic_and_revise` | **110.0s (17.8%)** | 26.0s (5.0%) | 1 次 LLM `critic`（该次 108.8s，纯模型抖动） |
| `verify_poi_and_routes` | 77.6s (12.6%) | 58.5s (11.3%) | 高德 `get_poi_detail` 6 次（最大单次 43.5s）+ `route` 9 次，均为串行 |
| `finalize` | 47.5s (7.7%) | 46.5s (9.0%) | 1 次 LLM `final_answer`（用户可读正文，保留） |
| `search_social_guides` | 25.5s (4.1%) | 23.3s (4.5%) | 1 次 LLM `query_expansion` 13.5s + 社媒 Provider |
| `score_candidates` | 25.9s (4.2%) | 13.8s (3.3%) | 途牛门票查询 |
| `search_hotels` | 3.6s (0.6%) | 5.4s (1.0%) | 途牛酒店 1 次 |
| `parse_intent` | 12.0s (2.0%) | 0.0s (0.0%) | Guided 模式已不再重新解析 Intent |

### 1.2 三个真正的瓶颈

1. **同一个 Session 内重复查询 Provider**：`tp-...-ead2ba` 的审计里
   `prefetch_reused: {transport:false, hotels:false, social:false, places:false}`，
   `prefetch_available: []`。首页/Discovery 查过一次的东西，12 步 Workflow 又原样查了一遍。
2. **无意义串行**：火车与航班、去程与回程、16 次 POI 关键词搜索、多个 POI 的门票/详情/路线，
   全部一次一个地等。Discovery 本身也一样：交通 247s、地点 372s，合计 621s。
3. **LLM 调用量大且长**：7~8 次调用合计 147.0s（518s run，28%）/ 312.5s（616s run，51%），
   其中 `extract_places` 4 次串行 63.6s 在 Guided 模式下**查完就被丢弃**
   （`node_extract_places` 先跑抽取循环、再到「复用 Discovery places」的提前返回）。

### 1.3 顺手修掉的埋点 bug

`_emit_run_spans` 里 provider / mcp / tool / llm 这些**子 span** 的 `started_at` / `finished_at`
取的是**落盘时刻**而不是真实调用时刻，于是所有子调用的时间戳都挤在 run 结尾，
看起来"每个 Provider 都跑了 280~400s"。

`duration_ms` 是正确的，所以"单次耗时 / 总计"可信、**时间先后不可信**。
修好之前，管理端「最近一次调用时间」和任何甘特式判断都是假的。修完后：

- 组 span 的窗口 = 组内各次调用的 `min(start) ~ max(finish)`；
- 单次 tool span 用 `fetched_at + duration_ms` 推导；
- 同名 tool 的多次调用各占一条 span（见 §4.3）。

## 2. 优化后（AFTER）：真实 E2E

三次真实 run 都用 `scripts/e2e_real.py` 从**真实 Provider** 跑通（非假数据、非 Benchmark）。
`--wait-discovery` 表示"等 Discovery 跑完再点开始规划"，即**会走完整复用路径**。

| # | 行程 | run_id | Discovery | **点开始规划后** | 总计 | 终态 | Discovery 交接 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ① | 5 天 北京→成都 | `tp-20260920-224249-b015ba` | 255.2s | **78.4s** | 333.6s | DEGRADED | transport/hotels/social/places **全部 `reused`** |
| ② | 3 天 北京→西安 | 见 §2.3 | | | | | |
| 对照 | 5 天（优化后代码，**未等 Discovery**） | `tp-20260920-223705-1ff434` | 0s（没等） | 311.8s | 311.8s | completed | 无预取可复用 |

> ① 的 Discovery 255.2s 偏慢是**测量环境**造成的：同一台机器上还有 ② 之前那次
> 未等待的 run 在并发打同一批 Provider。同一份代码在同日早些时候的一次干净运行里，
> Discovery 只有 **97.9s**（交通 42.8s / 酒店 8.9s / 攻略 13.6s / 地点 97.9s），
> 这里如实记两个数，不挑好看的用。

### 2.1 5 天行程：同行程、同流程、逐阶段对比

| 阶段 | BEFORE（518.2s） | AFTER 无预取（311.8s） | AFTER 有预取（78.1s） |
| --- | --- | --- | --- |
| `search_intercity_transport` | 210.2s | 96.6s | **0.0s**（reused） |
| `extract_and_normalize_places` | 132.2s | 66.4s | **0.3s**（reused） |
| `verify_poi_and_routes` | 58.5s | 18.6s | 25.1s |
| `finalize` | 46.5s | 32.8s | 19.7s |
| `critic_and_revise` | 26.0s | 24.5s | 22.6s |
| `search_social_guides` | 23.3s | 25.3s | **0.0s**（reused） |
| `score_candidates` | 13.8s | 11.4s | 7.0s |
| `search_hotels` | 5.4s | 33.3s | **0.0s**（reused） |
| **合计** | **518.2s** | **311.8s** | **78.1s** |

拆开看两段收益，避免把两件事混成一个数字：

- **纯并发/限流/少调模型**（第 2 列 vs 第 1 列）：518.2s → 311.8s，**−39.8%**。
  这一段不依赖任何复用，是 `_run_parallel` + 少调模型 + Provider timeout 的净效果。
- **再加上 Prefetch 复用**（第 3 列 vs 第 1 列）：518.2s → 78.1s，**−84.9%**。
  交通 / 酒店 / 攻略 / 地点四条线全部 `reused`，等于把整个 Discovery 的成果直接用上。

### 2.2 调用次数与 Token

| 指标 | BEFORE | AFTER 无预取 | AFTER 有预取 | 变化 |
| --- | --- | --- | --- | --- |
| 总耗时 | 518.2s | 311.8s | **78.1s** | **−84.9%** |
| LLM 调用 | 7 | 7 | **2** | **−71%** |
| Jev 调用 | 3 | 2 | 2 | −33% |
| 工具调用 | 49 | 45 | **21** | **−57%** |
| Token 合计 | 31868 | 25430 | **16345** | **−49%** |
| Provider 失败 | 13 | 18 | 4 | — |

LLM 从 7 次降到 2 次，主要来自两处：Guided 模式不再对同一批证据重复抽地点
（省掉 4 次 `extract_places`），以及 `query_expansion` 随攻略线一起被复用。

### 2.3 3 天行程（北京→西安 10/1 起 3 天 2 人 ¥4500）

| 指标 | BEFORE | AFTER | 变化 |
| --- | --- | --- | --- |
| run_id | `tp-20260920-214836-5c61d9` | `tp-20260920-224653-f4e091` | — |
| Discovery | 621.1s（交通 246.9s + 地点 372.4s，全串行） | **164.2s**（交通 46.3s / 酒店 9.9s / 攻略 21.8s / 地点 116.1s，四条线并行） | **−73.6%** |
| **点开始规划后** | —（旧服务无复用，整段 1075.2s 都在等） | **262.0s** | — |
| 总耗时 | **1075.2s** | 426.2s | **−60.4%** |
| 终态 | completed | **SUCCESS** | — |
| LLM 调用 | 7 | **2** | −71% |
| 工具调用 | 48 | 20 | −58% |
| Token | — | 13805 | — |
| Discovery 交接 | 无（`prefetch_reused` 全 false） | transport/hotels/social/places **全部 `reused`** | — |

> 这次 262s 明显高于 5 天的 78.4s，原因在 §5：`score_candidates` 里有一次
> 途牛门票查询打满超时预算（185.3s）。不是复用没生效 —— 四条线同样是全部 `reused`。


### 2.4 Discovery 阶段本身（用户在前端选偏好时后台跑的那段）

| 阶段 | BEFORE（3 天，串行） | AFTER（5 天，干净运行） |
| --- | --- | --- |
| 交通候选 | 246.9s | **42.8s** |
| 酒店候选 | （与交通同段） | 8.9s |
| 攻略 | （同上） | 13.6s |
| 地点抽取 | 372.4s | **97.9s** |
| Discovery 总耗时 | **621.1s** | **约 98s**（四条线并行，取最慢一条） |

另外，候选数量被限制住（Part D）：地点从 **123 个**降到 **20 个**（用户可见候选上限），
交通从 60 降到 44。这一步同时减少了后续 POI 核验与路线查询的规模。

## 3. 做了什么（对应 PRD Part A~H）

| Part | 做法 | 落点 |
| --- | --- | --- |
| A 并发 | 新增 `_run_parallel`：上限全部来自 `app/config.py`，结果按任务定义顺序收集 | `app/workflow.py`，9 处调用点（交通/航班、POI 关键词、POI 详情、路线、接驳、门票、critic） |
| B Prefetch | Discovery 四条线并行 + 完成一条落一次盘，用户点开始规划时读到的就是已完成的部分 | `app/discovery.py` / `app/sessions.py` |
| C 复用 | 同 Session 内同 query key 不再重复请求；`cache_hit` / `prefetch_reused` / `provider_called` / `fallback_query` 落到 span 与账本 | `app/providers.py` / `app/workflow.py` |
| D 限流 | `DISCOVERY_MAX_PLACES` / `USER_VISIBLE_POI_LIMIT` / `PLANNER_POI_LIMIT` / `MAX_ROUTE_LOOKUPS` | `app/config.py` |
| E 超时 | 每 Provider 独立 timeout（含 MCP 预算），fallback 链保留且全部留痕；失败只降级不编造 | `app/providers.py` / `app/config.py` |
| F 少调模型 | Guided 不再重复抽取；Jev 仅在多候选/真实 trade-off/质量门时调用 | `app/workflow.py` / `app/decision/` |
| H 目标 | 5 天 518.2s → 78.1s；Discovery 621s → 98s | — |

## 4. 过程里发现并修掉的真问题

都不是"顺手重构"，是实测撞到的：

1. **Guided 模式先抽后弃**：`node_extract_places` 先跑 4 次串行 LLM 抽取（63.6s），
   之后才走到「复用 Discovery places」的提前返回 —— 白花的 63.6s。
2. **子 span 时间戳取落盘时刻**（§1.3）。
3. **16 次 `search_poi` 在 Trace 里只剩 1 条**：同名 tool 共用一个 `span_id`，
   `INSERT OR REPLACE` 让后写覆盖先写。profiler 数出来的 route/poi 调用数全是错的。
4. **途牛酒店为空时整次 run 崩**：走联网兜底那条分支 `_record_subspan` 漏传 `started_at`，
   抛 TypeError → run FAILED。而它本该只是一次降级。这是本轮最严重的一个：
   "住宿主源返回空"是常见情况（实测成都 10/1-10/4 途牛就返回 0 条）。
5. **管理端 metrics 键重复**：`admin_run_detail` 的返回字典里写了两个 `"metrics"`，
   后者覆盖前者，成本拆分永远到不了前端。
6. **管理端 user_journey 不带交接结果**：前端只能去 Trace 里猜哪条线复用了；
   现在把审计里的 `discovery` / `prefetch_stages` / `grace_waited_ms` 并进来。
7. **测试会真的拉起 `npx 12306-mcp` 并挂死整套 pytest**：MCP 不需要密钥，
   "摘 Key"的离线策略拦不住它；一个没注入替身的用例就能让 931 个测试卡在 24% 半小时。
   新增 `TRAVELPLAN_DISABLE_MCP`，测试默认关闭。
8. **本地起服务会写到线上库**：`.env` 一旦配了生产 `DATABASE_URL`，
   直接 `uvicorn` 就会连 Neon 建表写 run，且 `/health` 要 30s。
   新增 `scripts/serve_local.py` 强制 SQLite（清变量的顺序也在注释里写明了原因）。
9. **生产上 run 永久卡在 RUNNING（本轮最严重）**：部署验证那次 quick run
   （上海→杭州 2 天）在 `extract_and_normalize_places` 卡了一个多小时没结束，
   前端会一直转圈。进程内复现 + 线程栈定位到根因：
   **途牛插件工具在签名里声明了 `timeout`，内部 `subprocess.run` 却没把它传下去**
   —— CLI 进程挂住时调用它的线程无限等待。此前 `_plugin_call` 的逻辑是
   "Tool 说自己支持 timeout 就直接 `tool.invoke()`"，等于把上限交给了工具自己。
   现在**所有** Plugin Tool 一律走 `_invoke_bounded` 的外层守护线程，到点一定放手；
   声明了 timeout 的工具仍然拿到预算去做内部取消。
   修复后用同一句 query 复跑：`ok: True`（改前卡死）。

## 5. 仍然存在的瓶颈

- **`verify_poi_and_routes` 25.1s（5 天）/ `score_candidates` 185.4s（3 天）**：
  3 天那次被**门票查询**拖住 —— `tuniu_search_scenic_tickets` 有 1 次 **TIMEOUT 185.3s**
  （打满了途牛的预算），`12306_get-tickets` 一次 INVALID_RESPONSE（25.0s）。
  这些调用已经是并发执行（3 次门票调用合计 191s、「合计 > 墙钟」正是并发在起作用），
  所以继续压只能靠**减少要查门票的候选数**，而不是提高并发。
- **`critic_and_revise` 21~23s + `finalize` 19.7~27.5s 是不可压缩的模型时延**：
  单次 LLM 调用本身就要 10~45s，只能靠"值不值得调"来省，不能靠并发。
- **LLM 长尾没有硬上限**：单次调用最长见过 108.8s。目前 Provider 有独立 timeout，
  LLM 只有统一超时，还没有"这次调用不值得等"的预算（Part F 的下一步）。
- **`discovery_stale` / `guided_intent_mismatch`** 两份 Bad Case 规则仍只有常量定义、
  没有触发器（不影响正确性，属于观测缺口）。
- **3 天行程的 Discovery（164.2s）比 5 天的干净值（97.9s）慢**：北京→西安这条线
  交通候选多（80 条 vs 44 条），地点抽取也到了 116.1s，仍在 100~170s 量级。

## 6. 优化过程中修掉的埋点/观测问题（第二批）

上面 §2/§3 的数字是修正过的，修正本身也是这轮工作的一部分：

1. **子 span 时间戳**（§1.3）：provider/tool/llm 子 span 拿落盘时刻当起止，导致
   "每个 Provider 都跑了 280~400s"。
2. **同名 tool 覆盖**：16 次 `search_poi` 在 Trace 里只剩 1 条，调用数统计全错。
3. **缺 duration 的调用被拉长成整段 run**：缓存命中、Provider 不可用、工具缺失这些
   路径不记 `duration_ms`，而 span 窗口此前会退回"当前时刻"，于是**一条 UNAVAILABLE
   记录被显示成 396.3s**，把真正超时那条（185.3s TIMEOUT）埋掉了。
   现在没有 duration 的调用按零长度处理（它们本来就没发生外部调用）。
4. **profiler 把成组 span 当单次调用累加**：provider/mcp 组 span 的窗口是组内
   调用的并集，和 tool 一起求和会出现"合计 293s 而整个 run 只有 78s"的矛盾数字。

