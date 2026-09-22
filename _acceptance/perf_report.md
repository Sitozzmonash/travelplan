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


---

# 第二轮优化：消灭长尾（本轮）

> 第一轮（§1~§6）已经把"串行 + 重复查询"解决掉了：Guided 路径全复用后 plan 阶段从
> 518s 降到 78s。本轮针对的是**剩下的长尾**——第一轮自己列出的三个瓶颈（§5）：
> 门票查询打满预算 185.3s、POI 详情重复 23 次、LLM 无按用途预算。
>
> 判定口径与第一轮一致：看用户感知耗时（`plan_s` = 点「开始规划」到终态），
> 不看某一步的自我感觉。原始数据：`_acceptance/e2e_*_after_perf.json`，
> 跑法 `python scripts/e2e_real.py`（真实 Provider，非假数据）。

## 7. 本轮实测（真实 E2E，全部真实 Provider）

| 行程 | run_id | Discovery | **点开始规划后** | 总计 | 终态 | 交接 |
| --- | --- | --- | --- | --- | --- | --- |
| 5 天 北京→成都 | `tp-20260920-234208-8d33af` | 208.5s | **123.4s** | 331.8s | SUCCESS | 四线复用 |
| 5 天 北京→成都（同行程再跑一次） | `tp-20260921-000318-794c95` | 184.4s | **78.2s** | 262.6s | DEGRADED | 四线复用 |
| 3 天 北京→西安 | `tp-20260920-235614-943308` | 181.6s | **60.2s** | 241.8s | DEGRADED | 四线复用 |
| 3 天 北京→西安（上一轮同行程） | `tp-20260920-224653-f4e091` | — | 262.0s | 426.2s | completed | 四线复用 |
| 5 天 北京→成都（**不等 Discovery**） | `tp-20260921-000506-4e855d` | 0s | **367.9s** | 367.9s | DEGRADED | 无预取可复用 |

最后一行是"用户马上就点开始规划"的路径：正式 run 拿不到 prefetch，必须自己把
交通/酒店/攻略/地点全查一遍，而且此时 Discovery 还在后台**并发**抢同一批 Provider。

**这一行没有达到 `180~300s` 的目标，原因不在代码而在模型侧**：该次 run 的单次模型调用
实测 `query_expansion 30.4s` / `extract_places:b0 54.7s` / `critic 66.8s` /
`final_answer 42.7s`，其中一批抽取在 120s 预算上打满超时。上一轮同一条路径测到
311.8s 时，模型单次是 10~45s。把模型时延按当期实测重新摊一遍，本轮在
Provider 侧是**变快**的：交通 96.6s → 63.2s、verify 18.6s → 11.1s、门票单次 185.3s → ≤15s。

**3 天行程 262.0s → 60.2s（−77%）**，而且这**不是**靠减少验证换来的：
同一次 run 里 `get_poi_detail` 20 次、`route` 14 次、`search_poi` 16 次全部照查，
只是不再重复查 Discovery 刚查过的同一批。

### 7.1 5 天行程逐阶段（对比第一轮的同行程）

| 阶段 | 第一轮（78.1s） | 本轮（123.4s） | 说明 |
| --- | --- | --- | --- |
| `critic_and_revise` | 22.6s | **61.6s** | 纯模型时延抖动（同一条 prompt） |
| `finalize` | 19.7s | 31.2s | 同上 |
| `verify_poi_and_routes` | 25.1s | 13.4s | 不再重复查 Discovery 查过的 POI 详情 |
| `score_candidates` | 7.0s | 15.1s | 门票 3 次，单次上限 15s（见 §8.1） |
| 交通 / 酒店 / 攻略 / 抽取 | 0s（复用） | 0s（复用） | 不变 |

本轮 5 天比第一轮的 78.1s 高，**差额全部在两次模型调用的时延上**（61.6+31.2=92.8s vs
22.6+19.7=42.3s），不是代码变慢：确定性 Benchmark 的质量指标两轮**逐位相同**（§9）。
模型时延这一项本轮做了硬上限（§8.4），但上限只能防"无限等"，不能把慢调用变快。

### 7.2 3 天行程逐阶段（同一行程，对比上一轮）

| 阶段 | 上一轮 262.0s | 本轮 60.2s |
| --- | --- | --- |
| `score_candidates` | **185.4s**（1 次门票 TIMEOUT 185.3s） | **15.1s**（3 次，最大 15.0s） |
| `critic_and_revise` | 21.5s | 10.4s |
| `finalize` | 27.5s | 15.7s |
| `verify_poi_and_routes` | 25.2s | 14.6s |
| 其余 | 复用，≈0s | 复用，≈0s |
| **合计** | **262.0s** | **60.2s** |

## 8. 本轮做了什么

### 8.1 门票长尾：按 Tool 给预算（最大的单项收益）

`tuniu_search_scenic_tickets` 与途牛酒店/火车共用一份 90s 预算，而它只是"按景区名查渠道价"，
正常 2~6s 返回。上一轮 3 天行程里 1 次挂死就打满 185.3s（占该次墙钟 70.7%）。
现在 `app/config.py` 支持**按 `(provider, tool)` 覆盖预算**（`TICKET_TIMEOUT_SECONDS=15`），
本轮实测门票 3 次共 24.0s、单次最大 15.0s。

### 8.2 交通对冲：主源慢时提前用备胎，但绝不混排

12306 是火车主源（一手数据），但实测单次能打到 90s 预算上限。现在主源与途牛火车
**并发**发出，按单源不变量择一采用：

- 主源成功 → 用主源，途牛结果标记 `discarded` 留在账本里（不多算一次有效调用）；
- 主源 EMPTY → 这是"当天没车"的结论，不回退；
- 主源超时/失败，**或**超过 `TRANSPORT_HEDGE_WAIT_SECONDS=25` 且备胎已有非空结果
  → 提前采用备胎，不再傻等主源把预算耗完。

实测 3 天行程里铁路这一路**没有出现 12306 调用**（对冲到途牛 2 次共 6.6s），
Discovery 的交通线从 177.1s（两次 90s 超时）降到 46s 量级。Discovery 与正式 run
走的是同一套对冲逻辑。

### 8.3 复用补齐：同一件事不做第二遍

| 位置 | 上一版 | 本轮 |
| --- | --- | --- |
| 交通复用 | `outbound or inbound` 一刀切（**只跑完去程 → 回程被静默置空**） | 逐方向判定，缺哪个查哪个 |
| POI 详情 | 按"营业时间是否为空"判断查过没有 → 高德不返回营业时间的点每次重查 | 按"账本里有没有这次调用"判断 |
| 检索词 | Discovery 算过的 query 正式 run 再问一次模型（13.5s） | 复用 Discovery 的检索词；只补搜没搜过的 query |
| 已搜过的 query | 全量重搜一遍 | 按 query 粒度算差集，已搜过的不再搜 |
| REJECT 地点 | 进深度验证池，只被"超出上限"挡下 | 直接出局，绝不为用户排除的点花 Provider 预算 |
| POI 搜索词 | `raw_names` 与兜底关键词可能重名 → 同一词查两遍高德 | 合并去重后再发 |

### 8.4 模型调用：并发闸门 + 按用途预算 + 结构化裁剪

- `LLM_MAX_CONCURRENCY` 以前是**死配置**（实际硬编码 `max_workers=2`）；现在装在
  `LLM` 客户端的唯一收口点上，所有调用都过这道闸门。
- 模型预算按用途给：抽取 120s / critic 90s / 成文 120s，且都**不超过**调用方给的硬上限
  （`min(cap, per_tag)`）。打满只是如实降级为 `TIMEOUT` 并落 Trace，不编造内容。
- 上一版把 digest 用 `json.dumps(...)[:24000]` **从中间切断**，模型收到的是残缺 JSON、
  必然解析失败。现在是**按结构**逐级瘦身（先压短理由 → 再减每天的条目 → 最后减天数），
  每一步都仍然是合法 JSON，并如实记录砍掉了多少。

### 8.5 抽取：多篇合成一批 + 批间并发 + 超时按单篇重试

`extract_places` 从"每篇一次模型往返"改成"多篇一批"，批间受控并发。批大小默认 **2**
而不是 4：实测 4 篇一批时整批在 120s 预算上打满超时，4 条证据的地点一起丢
（`extract_places:b0 TIMEOUT`，见 3 天那次的 audit）；批小一半、两批并发，墙钟仍≈
最慢那一批，而"一次超时只损失 2 篇"。

更进一步：批超时**不该连带丢掉批里所有证据**——批内各篇本来就是互相独立的。所以整批
失败时按单篇**再重试一次**（预算 45s，`LLM_TIMEOUT_EXTRACT_RETRY_SECONDS`），
把"一次慢调用损失 2 篇"降级成"最坏情况下每篇各自决定成败"，并留下降级说明。
实测 5 天那次 `extract_places:b1` 打满 120s 后，重试把两篇都救了回来。

正式 run 与 Discovery **共用同一个实现**（`discovery.extract_places_from_evidences`），
批大小/并发/归属/重试规则不会两处漂移。

### 8.6 配置集中

第一轮说"节点不再使用写死数字"，但实际仍有 8 个常量被节点真实引用
（`SOCIAL_QUERY_LIMIT` / `MAX_EVIDENCE` / `EXTRACT_EVIDENCE_LIMIT` / `ROUTE_MAX_LEG_METERS` …）。
本轮把这些全部收进 `app/config.py`，并删掉同名"镜像常量"——镜像与 config 迟早漂移，
而"节点到底读哪一个"从代码上看不出来。新增键与文档见 `docs/operations/CONFIG.md`。

## 9. 回归验证（本轮）

| 项目 | 结果 |
| --- | --- |
| `pytest -q` | **944 passed**（含本轮新增：按 Tool 预算、按用途模型预算、结构化裁剪、逐方向复用） |
| `npm run typecheck` | 通过 |
| `npm run lint` | 通过 |
| `npm run build` | 通过（13 条路由全部生成） |
| Benchmark（33 例确定性，含 Provider Failure 4 例） | **33/33 通过，0 失败** |
| Benchmark 对照组（同一套用例，base=`7c9c223` vs 本轮） | 硬约束 / 质量 / 证据 / Provider / Jev **每一项指标逐位相同**；仅 fixture 时延 1810.7ms → 1751.5ms |

对照组的意义：确定性用例两轮**每一个质量指标都相同**，说明本轮只改"怎么取数、等多久"，
没有改"取到什么、怎么判断"。

## 10. 本轮修掉的观测/正确性问题

都不是顺手重构，是实测撞到的：

1. **交接状态按"Hub 方法名"匹配工具名**：`_user_journey_summary` 找的是 `search_trains`
   这类方法名，而账本里记的是 `railway_12306_get-tickets` / `tuniu_search_trains` /
   `tuniu_search_flights` —— 三类名字一个都匹配不上，于是**"本次真的补查了交通"会被
   审计写成"复用了 Discovery"**。写这个用例时才发现：只跑去程复用的场景下
   `search_trains` 明明被调用了，交接状态仍报 `reused`。
2. **复用结果的 provider 写死成 12306**：对冲后火车数据可能来自途牛，却仍被标成
   12306 的一手数据。现在以候选自带的 provider 为准。
3. **profiler 把 Jev 算成 0.0s**：Jev span 记的是 `latency_ms` 而 profiler 只读
   `duration_ms`，于是 3 次各 ~0.95s 的真实 Jev 调用被显示成 0s。已修正。
4. **`--no-wait-discovery` 是个静默失效的开关**：`scripts/e2e_real.py` 里轮询循环
   无条件执行，于是"不等 Discovery"其实一直在等——两条路径测出来是同一个数字。
   已修正为真的跳过等待（上面第 5 行那个 367.9s 才是真正的"无预取"路径）。
5. **缓存命中的并发竞态**：`search_terms` 里同名词在两个线程同时看到"未命中"就会
   真打两次高德（缓存的"查→未命中→调用"不是原子的）。除在节点层先去重外，
   `ProviderHub` 的计数器 / 缓存 / MCP 句柄创建也补上了锁（此前它已被 4 路并发使用）。

## 11. 仍然存在的瓶颈（按影响排序）

1. **模型时延是目前的第一瓶颈**，不是 Provider：本轮实测单次调用
   `query_expansion 30.4s` / `extract_places 54.7s`（一批）/ `critic 10~67s` /
   `final_answer 15~43s`。无预取路径的 367.9s 里模型占了约 300s。本轮已给每个用途
   装上硬上限（防"无限等"）与批超时重试（保质量），但**上限不能把慢调用变快**。
   继续压只有两条路：换更快的模型，或减少调用次数（后者会牺牲规划质量）。
2. **Provider 长尾**：12306 与途牛火车两条线都出现过打满 90s 预算
   （`RAILWAY_12306_TIMEOUT_SECONDS` / `TUNIU_TIMEOUT_SECONDS`）。对冲把"串行等两个源"
   变成了"等最慢的那个"，但**单源本身的慢**没办法靠并发解决。要继续压只能下调这两个
   预算，代价是把"慢但成功"的查询误判成超时——属于产品口径取舍，已做成可配置项。
3. **Discovery 的 180~325s 不直接计入 `plan_s`**，但用户若很快就点「开始规划」，
   没跑完的那部分会落到正式 run 里补查（3 天那次就是 PARTIAL → transport 74s）。
   这也是为什么"快速点击"的行程比"等 Discovery 跑完"的行程慢一倍。
4. **TikHub 配额**：本轮两次运行里小红书都是 `RATE_LIMIT` / `FREE_CREDIT_EXHAUSTED`，
   攻略证据主要靠联网搜索补。这不是性能问题，但会影响 Evidence 覆盖率。
5. **`search_hotels` 偶发 0 候选**：途牛对部分城市/日期返回 0 条，此时走联网兜底，
   已有降级披露，但候选池会明显变窄。
