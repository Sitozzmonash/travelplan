# 11 用户旅程与 Planning Session

一句话：说明"引导式（Guided）"这条新入口怎么从三步向导走到一次正式 run，以及 Planning Session 的状态、Prefetch、结构化偏好与 MUST/WANT/REJECT 到底改变了什么。

代码位置：`app/sessions.py`（会话）、`app/discovery.py`（取数）、`app/selection.py`（用户选择层）、`app/workflow.py`（节点消费）、`app/api.py`（HTTP）。

## 1. 两条入口，一个最终引擎

```text
Quick（保留）   一句话自然语言 → parse_intent（模型/规则） → 12 步 Workflow
Guided（新增）   三步向导 → Planning Session → 12 步 Workflow（复用已查候选）
```

两条入口最终都汇聚到 `app/workflow.py:execute_travel_run`，**没有第二套 Planner**。Guided 由 `app/sessions.py:start_run` 直接调用 `execute_travel_run(intent=..., prefetch=..., source="guided", source_session_id=...)`；Quick 由 `app/agent.py:run_travel` 调用（不传 `intent`/`prefetch`，走原逻辑）。

分开的是"规划前的准备"，不是规划算法本身。

## 2. 三步向导 → 正式 run

```text
Step 1 基础信息  出发地 / 目的地 / 出发日期（默认今天）/ 天数 / 人数 / 预算(可选)
Step 2 探索确认  推荐住宿区域 + 攻略抽取出的地点与美食 → 必去 / 想去 / 不感兴趣 / 不管
Step 3 偏好      交通 + 酒店 + 节奏在同一页选完 → 「开始规划」
```

向导从 6 步并到 3 步（`frontend/components/guided/wizard-progress.tsx` 的 `STEP_META`）：交通 / 酒店 / 节奏原来各占一页，每页只问一两件事，用户要点三次「继续」才走到开始；它们都是"选个偏好"而不是"填信息"，合成一页不增加任何必填项。原来的 Step 6「确认」页也去掉了 —— 桌面端右侧摘要卡与移动端顶部一行摘要本来就在实时复述，再让用户确认一遍是多余的。

后端时序与 HTTP 的对应：

```text
Step 1 「继续」  → POST /api/v1/planning-sessions                （建会话 + 后台立刻 Prefetch）
Step 2 「继续」  → PATCH /api/v1/planning-sessions/{id}          （写 POI 选择）
Step 3 「开始规划」→ PATCH .../{id} 之后 POST .../{id}/start      （先把这一页改过的偏好写回，再创建正式 run_id）
```

Step 3 的顺序不能反：用户可能在这一页直接点「开始规划」、从没点过「继续」，那些交通 / 酒店 / 节奏就还没进过 PATCH。写回失败时不会静默开跑 —— 前端会重试并在仍失败时明确报错。

基础信息一确认就立刻在后台 Prefetch，而不是等用户把后面全选完 —— 用户在 Step 2~3 点选的这段时间里，交通 / 酒店 / 攻略已经在查了。如果这座城市**预热过**（见 [15_城市知识库攻略正文复用.md](15_城市知识库攻略正文复用.md) §9），攻略那条线（Discovery 最贵的一段）直接命中城市知识库，只剩机酒火走实时查询。

## 3. Session 状态机与 TTL

Session 状态与 Run 状态**刻意分开命名**（`app/sessions.py`）：

```text
COLLECTING ──► DISCOVERING ──► READY ──► STARTING ──►（正式 run）
     │              │            │
     └──────────────┴────────────┴──► CANCELLED / EXPIRED
```

| 状态 | 含义 |
| --- | --- |
| `COLLECTING` | 已建会话，用户还在选；未提交 Discovery（或 `DISCOVERY_ENABLED=false`） |
| `DISCOVERING` | 后台 Prefetch 正在跑 |
| `READY` | Prefetch 结束（含 PARTIAL/FAILED），用户可以点「开始规划」 |
| `STARTING` | 已创建正式 run_id，run 在跑 |
| `CANCELLED` | 用户主动取消（`DELETE`） |
| `EXPIRED` | 超过 TTL 未进入 run |

- TTL = `max(5, PLANNING_SESSION_TTL_MINUTES)` 分钟，默认 **45**（`app/config.py`）。
- **过期判定不依赖后台定时任务**：每次读会话（`sessions.get_session`）顺手调用 `store.expire_planning_sessions(now=...)`，把 `expires_at < now` 且非 CANCELLED/STARTING 的会话标成 `EXPIRED`。
- 过期会话**不删除**：管理端仍要能看到"这个用户开了会话但没走到规划"。`prefetch_json` 里的机酒候选不再复用（避免用陈旧价格排行程）；但**跨会话的「城市知识库」不受会话过期影响**——`city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta` 有自己独立的 TTL（攻略 15 天 / POI 15 天），见 [13_数据流与复用机制.md](13_数据流与复用机制.md) §2.1。

Discovery 自己还有一个状态（`session["discovery_status"]`）：`PENDING / RUNNING / READY / PARTIAL / FAILED`。`PARTIAL` 表示四条取数线里有降级或报错，但用户仍可继续。

## 4. Prefetch：Discovery 的唯一实现与逐阶段状态

`app/discovery.py` 是"取真实候选"的**唯一实现**，Workflow 节点与 Session Prefetch 共用它：

| 函数 | 作用 |
| --- | --- |
| `fetch_transport_candidates(hub, intent)` | 去程/回程大交通候选（不做比选，比选留给策略层） |
| `fetch_hotel_candidates(hub, intent, pages=...)` | 酒店候选；`pages>1` 翻多页扩大候选池 |
| `discover_social_evidence(hub, llm, intent)` | 攻略证据：小红书 → 抖音（小红书空时）→ 网页 |
| `extract_place_candidates(hub, llm, intent, evidences, ...)` | 攻略地名 + 关键词 → 高德 POI 候选（含去重） |
| `prefetch(hub, llm, intent, *, hotel_pages, workers)` | 取数入口：`transport / hotels / social` 三线并行，随后串行做 `places`；返回原始结果 + 逐阶段状态 |

`prefetch` 用 `ThreadPoolExecutor(max_workers=discovery_workers)` 并行跑 `transport / hotels / social`，随后串行做 `places`（地点抽取依赖攻略结果）。每条线单独计时，返回**逐阶段**状态：

```text
stages[stage] = {status, started_at, finished_at, duration_ms, result_count, degraded, error}
stage ∈ {transport, hotels, social, places}

# 实时取数（discovery.prefetch）
status ∈ {RUNNING, OK, EMPTY, FAILED}   # 单条线崩溃只记 FAILED + error，不带走整个 Discovery
# 城市知识库命中（sessions._prefetch_with_city_cache，此时根本不调 discovery.prefetch）
status ∈ {CACHE, STALE_CACHE}           # 这两个阶段没有 started_at/finished_at，多一个 updated_at
```

`discovery.prefetch` 只覆盖"实时取数"这一路。**命中城市知识库时它整个不被调用**：
攻略正文、攻略提及、POI 与检索词分别来自 `city_evidences` / `city_poi_mentions` /
`city_pois` / `city_cache_meta`，当中只有交通与酒店照旧现查
（见 [13](13_数据流与复用机制.md) §2.1）。复用的正文会在跑图前被
`workflow._materialize_city_evidence` 换成本次 run 自己的 `sources` / `evidence` 行
（`sources.status = CACHED`），所以证据链不会因为"来自上一次会话"而断掉。

一条线失败只让 `discovery_status=PARTIAL`，**绝不让用户卡在加载页**：社交/酒店/交通任一失败，POI 页仍可用。

Prefetch 结果通过 `discovery.PrefetchBundle` 在"会话 JSON ↔ 类型化对象"之间搬运（`dump()` / `load()` / `reused`）。为什么要类型化：会话把候选存成 JSON，正式 run 再还原成同一批 Pydantic 模型；若复用的是一个半成品字典，Provider 归一化出来的字段会悄悄丢一半，最后表现为"引导式跑的行程比一句话跑的差"。

## 5. 结构化偏好如何映射到 Planner

`app/sessions.py:intent_from_basic(basic, preferences)` 把用户点选的结构化字段直接构造成 `TripIntent`（**不经过模型**），并置 `source="guided"`。`node_parse_intent` 在引导式下**直接采用**这个结构化意图（`state["intent_override"]`），不再让模型猜一遍用户刚点过的按钮。

取值归一化集中在 `app/selection.py`，认不出就退回 `auto`：

| 字段 | 取值 | 中文标签 |
| --- | --- | --- |
| `transport_mode` | `train / flight / any / auto` | 高铁 / 飞机 / 都可以 / 帮我选 |
| `transport_priority` | `value / fastest / cheapest / comfort / auto` | 性价比优先 / 时间最短 / 价格最低 / 舒适优先 / 帮我选 |
| `transport_constraints` | `few_transfers / no_early / no_red_eye / any_time`（多选；`any_time` 与其它互斥） | 少换乘 / 不要太早 / 不要红眼 / 时间都可以 |
| `hotel_priority` | `value / location / rating / comfort / transit / auto` | 性价比优先 / 位置优先 / 评分优先 / 舒适优先 / 交通方便 / 帮我选 |
| `pace` | `relaxed / balanced / packed / auto` | 轻松一点 / 正常 / 多玩一些 / 帮我安排 |

`auto` 是**确定性推导**，不是随机（规则定义在 `app/selection.py`；三条 `auto` 规则与结构化策略都已接入正式打分）：

- **pace auto**（`selection.resolve_pace`，guided 的 `node_parse_intent` 会调用 `resolve_pace_with_reason` 并写进决策链）：按"候选数 / 可玩天数"的密度判定 —— `per_day ≤ 3.0` → `relaxed`，`per_day ≥ 4.5` → `packed`，否则 `balanced`；首/末日可用时间少时按 1.15 倍折算（阈值来自 `PlannerTuning.pace_auto_*`）。**这条已生效**。
- **交通 auto/any**：使用 `PlannerTuning` 的一组基础权重（价格/门到门时长/舒适/偏好各为 1.0），固定规则是"日期正确 → 不明显浪费首末日 → 综合门到门时间与价格 → 再看舒适度"。
- **酒店 auto**：基础权重为"位置/交通不能明显差 + 评分达到合理水平 + 再综合价格"。

> **这些按钮确实生效**：`selection.transport_weights()` / `hotel_weights()` 是**真正的消费点**
> ——`workflow._transport_score` 与 `workflow._hotel_score` 都调用它们（`app/workflow.py:1196`、
> `:1368`，`hotel_weights` 的标签还用于 `:1462` 的策略说明），权重来自 `app/config.py` 的
> `PlannerTuning`（`transport_price_weight` / `hotel_rating_weight` 等，可用 `TP_*` 环境变量覆盖；
> 其中 10 个 `TP_*` 键在 `EDITABLE_KEYS` 里，可由管理端运行时改）。
> 也就是说**交通方式 / 排序优先级 / 附加约束（少换乘、不要太早、不要红眼）与酒店优先级都会改变
> 最终选中的车次与酒店**，不只是被记录。
>
> 此外用户补充的酒店条件在 `selection.hotel_candidates_for()` 里是**硬过滤**（价格上限 / 评分下限 /
> 房型关键词），过滤后为空时回落原列表并说明原因（住宿是行程必需项，不能交白卷）。

## 6. MUST / WANT / REJECT 的执行语义

用户在 Step 2 对每个 POI 的选择，最终变成 `intent.place_selections`（`place_id → MUST/WANT/REJECT`）。`app/selection.py:apply_user_place_preferences` 把它作用到候选集合：

```text
REJECT   硬排除 —— 直接移出候选，绝不进入最终行程（DecisionStatus.REJECT，reason_codes=[USER_REJECT, HARD_EXCLUDE]）
MUST     强优先 —— 加 must_place_bonus（默认 60），但不越过时间/营业/预算等硬约束
WANT     加分   —— 加 want_place_bonus（默认 25），Planner 优先放入
NEUTRAL  不改分 —— 交给 Trust / AdRisk / 路线 / 多样性逻辑
```

落地位置：

- `node_extract_places`（引导式复用 Discovery 候选时）先做一次 REJECT 硬排除，并写一条 `planner.user_place_selection` 子 span；
- `node_score_candidates` 再对**所有入口**（Quick 也可能带着 `place_selections`）跑一遍 `apply_user_place_preferences` + `preference_bonus`，所以 REJECT 不可能进入 `build_initial_plan`。

排不下时的取证（`app/badcase.py` 用）：

- `selection.must_place_shortfall(...)`：找出标了 MUST 但最终没进行程的点；
- `selection.rejected_place_intruders(...)`：找出最终行程里仍出现、但被标记 REJECT 的点（正常必须是 0）。

Jev 不能覆盖用户硬选择：REJECT 的点在 Jev 之前就已被移出候选池。

## 7. 会话 → Run 的衔接与幂等

`app/sessions.py:start_run`：

1. 读会话并做过期判定；已 `CANCELLED/EXPIRED` → 返回 `session_closed`（API 409）；
2. 若 `run_id` 已存在 → **幂等**返回同一个 `run_id`（重复点「开始规划」不会产生第二条 run）；
3. 由 `intent_from_basic` 构造 `TripIntent`，把 `poi_selections` 写进 `intent.place_selections`；
4. `PrefetchBundle.load(session["prefetch"])` 还原候选（`load()` 单块坏了只丢那一块）；
5. `store.create_run(run_id, source="guided", source_session_id=session_id)` —— **只有走到这里才创建正式 run_id**；
6. 会话转 `STARTING`，追加 `session_confirmed` 与 `run_started` 事件；
7. 后台线程执行 `_start_job` → `execute_travel_run(...)`，把 `intent` / `prefetch` / `source` / `source_session_id` 一起交给同一个 12 步流程。

`store.create_run` 的 `source` / `source_session_id` 落在 `runs` 表，管理端据此从 Run 反查 Session。`runs.source` 现有四个真实取值：`guided`（本路径）、`cli`（`main.py:70`）、`benchmark`（`benchmark/runner.py:120`）与默认的 `quick`（Quick Web `/plans`）。管理端的 Benchmark 过滤（`include_benchmark=false`）就是按 `runs.source <> 'benchmark'` 生效的，详见 [12_Provider健康与观测.md](12_Provider健康与观测.md)。

## 8. 为什么"改基础信息必须重建会话"

`app/sessions.py:PATCHABLE_FIELDS` 白名单**不含** `origin` / `destination` / `start_date` / `end_date` / `days`。`patch_session` 只改偏好与 POI：

```text
transport_mode / transport_priority / transport_constraints / hotel_priority /
hotel_max_price_per_night / hotel_min_rating / hotel_min_star / hotel_room_type /
hotel_allow_change / pace / budget_total / poi_selections
```

原因：基础信息决定 Discovery 的查询口径（查哪个城市的交通/酒店/攻略/POI）。若允许在 PATCH 里把目的地从成都改成重庆，而不重跑 Discovery，就会**拿着成都的攻略去排重庆的行程**。所以前端要改基础信息必须重新 `POST` 一个会话，由新会话重新 Prefetch。

偏好变化只影响排序、不影响原始数据，因此 `patch_session` **不重跑 Discovery**，只追加一条 `user_preferences_updated` 事件。

## 9. 会话事件时间线

事件存在 `planning_sessions.events_json`（会话 JSON 里的事件列表），管理端详情页默认折叠。`app/sessions.py:_event` 实际产出：

```text
session_created
discovery_queued                                  # 已提交后台 Discovery
transport_prefetch_started / hotel_prefetch_started /
social_discovery_started / place_extraction_started   # 四条线一次性写入（含 places）
transport_prefetch_finished / hotel_prefetch_finished /
social_discovery_finished / place_extraction_finished
discovery_finished                                # 汇总（候选数 + 总耗时）
discovery_failed                                  # Discovery 整体异常
discovery_grace_waited                            # 等 Discovery 的 grace period 结束
user_preferences_updated                          # PATCH 改过哪些键
session_confirmed / run_started                   # start_run
session_cancelled                                 # DELETE
session_expired                                   # 读会话时触发过期判定所补写
```

## 10. Session API

用户侧（无需鉴权，未进入正式 Run 前不属于 Run）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/planning-sessions` | 建会话并后台 Prefetch（202）；`origin`/`destination` 必填 |
| GET | `/api/v1/planning-sessions/{id}` | `session_view`：basic_intent / source / preferences(+labels) / poi_selections / 交通·酒店·POI 候选(**按 `poi_pools` 分池 + `place_categories`**) / 推荐住宿区域 `hotel_areas` / 动态偏好 `profile` / evidence_summary / 逐阶段 discovery / degradations / events / capabilities |
| PATCH | `/api/v1/planning-sessions/{id}` | 改偏好与 POI（白名单字段） |
| DELETE | `/api/v1/planning-sessions/{id}` | 用户主动取消 → `CANCELLED` |
| POST | `/api/v1/planning-sessions/{id}/start` | 创建正式 run（202）；取消/过期 → 409 |
| GET | `/api/v1/city-cache/{city}` | 只读目的地级「城市知识库」（攻略提及 + 高德 POI；带 `updated_at` / `source` / `stale`） |

`session_view` 的 `capabilities` 当前是 6 个键：

```jsonc
{
  "hotel_star_filter": false,        // 数据源不返回星级，星级筛选只记录不排序
  "hotel_star_note": "...",
  "hotel_max_price_filter": true,    // 每晚价格上限：硬过滤
  "hotel_rating_filter": true,       // 评分下限：硬过滤
  "hotel_allow_change": false,       // 全程只订一家酒店，中途换酒店未支持
  "hotel_allow_change_note": "..."
}
```

`updated_at` 在城市知识库命中时取自**缓存新鲜度**（不是会话的更新时间），前端据此渲染「地点信息来自 X 天前整理的攻略库」。管理端只读接口见 [05_Trace与可观测性.md](05_Trace与可观测性.md)。

## 11. 已知边界（诚实说明）

- **酒店星级**：后端没有星级字段（途牛不返回），`capabilities.hotel_star_filter=false`；`hotel_min_star` 会记录进会话与 `TripIntent`，但不参与过滤或排序。品质/预算请用「评分下限」与「每晚价格上限」表达（这两个是**真生效的硬过滤**）。
- **`hotel_allow_change`**：已记录并进入 `TripIntent`，但当前产品全程只选一家酒店（`_select_hotel` 返回单个 selected），该字段不影响排程（`capabilities.hotel_allow_change=false` 如实置灰）。
- **酒店价格/评分/房型过滤**：`hotel_max_price_per_night` / `hotel_min_rating` / `hotel_room_type` 在 `selection.hotel_candidates_for()` 里作为**硬过滤**生效；全部候选被滤掉时回落原列表并附说明（住宿是行程必需项）。
- **交通/酒店策略权重已接入**：见第 5 节。
- **Bad Case 类别 `discovery_stale` / `guided_intent_mismatch`**：两者都**已有规则产出**（`app/badcase.py`），见 [06_BadCase机制.md](06_BadCase机制.md)。

## Discovery → 正式 Run 的数据交接（grace period）

用户**不必**等 Discovery 全部跑完才能开始规划；系统按下面这套规则交接，任何一种情况都不丢数据、不卡住用户：

```text
用户点「开始规划」
→ ① 立即收集当前已经完成的 Prefetch（transport / hotel / social evidence / places）
→ ② 对仍在 RUNNING 的线给一个很短的 grace period（默认 3 秒，`DISCOVERY_GRACE_SECONDS` 可配）
→ ③ grace 期间完成的线继续合并进这次 Run
→ ④ 到点仍未完成的线不再阻塞用户，直接开始
→ ⑤ 正式 12 步流程对缺失的部分自己补查
→ ⑥ 已经 Prefetch 成功的部分必须复用，不重复打 Provider
```

三种输入对应三种结果，都有测试钉住：

| Discovery 状态 | 结果 |
| --- | --- |
| 全部完成 | 四条线全部 `reused`，本次 run 不再调用对应 Provider |
| 部分完成 | 完成的部分 `reused`，没完成的部分由正式 run 补查（`fallback_query`） |
| 全未完成 / 超时 | 全部 `fallback_query`（补查仍无结果时为 `unavailable`），行程照常产出 |

### 每个阶段都会记录交接结论

`outputs/<run_id>/audit_report.json` 与 `run_metrics` 所在的同一次 run 里，
`user_journey.discovery` 记录四个阶段各自的交接结论。**每个阶段是一个对象，不是一个字符串**：

```jsonc
{
  "transport": {
    "label": "交通",
    "handoff": "reused | fallback_query | unavailable",   // 三选一的结论
    "prefetch_available": true,      // Discovery 里有这条线的结果
    "queried_in_run": false,         // 本次 run 自己打没打对应的工具
    "resolved": true,                // 最终是否得到了可用结果
    "status": "OK",                  // 若来自 Discovery，带上它的 status
    "result_count": 18, "duration_ms": 4210, "degraded": false
  },
  "hotels": { /* 同上 */ },
  "social": { /* 同上 */ },
  "places": { /* 同上 */ }
}
```

判定口径（`app/workflow.py:_user_journey_summary`，全部由证据推出，不是标记位）：

- `reused`：Discovery 有该线结果，且本次 run 的调用账本里**没有**对应的查询工具；
- `fallback_query`：本次 run 自己查了，并且查到了可用结果；
- `unavailable`：本次 run 自己查了，但仍然没有可用结果（如实暴露，不粉饰）。

`prefetch_stages` 保留原始分阶段状态（status / duration_ms / result_count / degraded），
`grace_waited_ms` 记录开始规划时实际等待了多久（0 = Discovery 已完成，没有等待）。

### Trace 里怎么看

正式 run 的 `outputs/<run_id>/trace.jsonl` 与 `trace_spans` 表里会多一条 `discovery_handoff` span
（挂在 `finalize` 下），属性就是四个阶段的交接结论 + `grace_waited_ms`；复制自 Discovery 的
Provider 调用在 Trace 里状态为 `REUSED`、在 audit 的 `provider_calls` 里带
「Discovery 阶段已查询，本次 run 复用（未重复调用）」的 note。
