# 目前缺陷（复盘清单）

> 用途：把本轮复盘发现的问题**按编号**记下来，后面集中一起改。每条含：**现状 / 为什么算缺陷 / 影响 / 方向（含字段级方案）**。
> 编号规则：`A`=数据复用与存储（核心），`B`=产品交互，`C`=性能与观测，`D`=其它，`E`=决策与规划质量（固定权重→动态偏好，方案见 `docs/14_决策与规划质量优化.md`），`F`=UI 与前端体验（方案见 `docs/TravelPlan_UI_优化方案.md`）。
> 结论均来自对代码的事实核对，不含猜测。

---

## ⚠️ 核对状态（截至 commit `e0a0cae`）—— 读任何一条之前先看这张表

本文是**改造前**写的台账。下面每条里的"现状"描述的是**当时**的代码，很多已经修好了。
下面的状态表才是当前事实；带 ✅ 的条目，其正文只应作为"当初为什么这么改"的历史记录来读。

| 条目 | 状态 | 证据 |
| --- | --- | --- |
| A1 无跨 session / 跨用户目的地级缓存 | ✅ 已修 | `app/city_cache.py`（`read_candidates` / `write_candidates` / `refresh_in_background`）；旧行清理由 `write_candidates` 按 TTL 调用 `TravelPlanStore.prune_city_cache`（`app/store.py:1137`）——**它不在 `city_cache.py` 里**；**四张表** `city_pois` / `city_poi_mentions` / `city_evidences`（攻略正文）/ `city_cache_meta`（检索词）；读路径 `app/sessions.py` 的 `read_candidates(...)` |
| A2 `places` 主键阻止共享 | 🟡 部分 | 已按方向"共享表与 `places` 分离"落地；但"`places` 行加 `source='city_cache'` 标识位"**未做**（改由 `sources.status=CACHED` + `bundle.city_cache` 表达来源） |
| A3 `prefetch_json` 不是城市知识库 | ✅ 已修（方向调整为另建表） | 半静态部分（**含攻略正文与检索词**）已落 `city_evidences` / `city_cache_meta`，不再只活在会话 JSON 里；`prefetch_json` 仍整包序列化本次会话的候选（这是会话状态，不是知识库）。跨会话复用见 `workflow._materialize_city_evidence` |
| A4 ProviderHub 缓存不落库、TTL 形同虚设 | 🟡 部分 | 半静态部分已由 `city_cache` 落库；Hub 内存缓存 `self._cache` 仍在（`app/providers.py`） |
| A5 未按实时/半静态分层 | ✅ 已修 | `app/city_cache.py` 只缓存攻略（正文/提及/检索词）与 POI；机酒火价格与时刻表永不进 |
| A6 无过期重算 / 后台刷新 | 🟡 部分 | **惰性刷新已修**：`refresh_in_background` + `allow_stale`（stale-while-revalidate），并用 `prune_city_cache` 清掉 TTL 外的旧行（否则一座城市过了 TTL 会永远 stale）；TTL 进 config（`app/config.py:202-203`，攻略 15 天 / POI 15 天）。**主动填充已补一半**：`scripts/preheat_cities.py` 能对城市清单做离线预热（本地/CI 跑，写同一个 Neon），但它**是手动脚本，不是调度器** —— 全仓仍无 cron / apscheduler / BackgroundTasks，正文里"按城市+类别轮询的定时刷新"仍未做 |
| A7 相邻点路线缓存（待决策） | ⬜ 维持"先不上" | 无路线缓存表 |
| B1 主页默认一句话、POI 流藏次级 | ✅ 已修 | `frontend/app/page.tsx:269-276` 默认进 Guided；:338 Quick 降为副链接 |
| B2 POI 候选每次现抽 | ✅ 已修 | `app/sessions.py:317` 先读城市缓存 |
| B3 缺"数据新鲜度"维度 | ✅ 已修 | `frontend/components/guided/discovery-research.tsx:63-68` |
| C1 模型时延第一瓶颈，只有硬上限没策略 | 🟡 部分 | 已加按用途 LLM 超时 + 并发闸门；未做"换更快模型 / 合并 critic 与质量门"（`quality_gate` 仍独立） |
| C2 单源 90s 长尾 + TikHub 配额耗尽 | 🟡 部分 | 单源预算已成 config（默认仍 90s）；社媒配额由 A1 缓存缓解 |
| D2 共享表写入并发一致性 | ✅ 已修 | `ON CONFLICT ... WHERE excluded.updated_at >= ...`，四张共享表都带（`app/store.py:1004` city_pois / `:1031` city_poi_mentions / `:1086` city_evidences / `:1121` city_cache_meta） |
| E1 决策靠固定 Python 权重 | ✅ 已修 | `app/decision/profile.py` + `PreferenceProfile` + `PREFERENCE_PROFILE_PROMPT` |
| E2 硬规则/软决策无分界 | ✅ 已修 | **硬规则分散在四处，不要只认 `hard_constraint_violations()`**：(1) `planner.hard_constraint_violations()`（`app/planner.py:3656`）**只管构造性不变量**——天数对齐 / 时间不倒置 / 不超当日窗口 / 同点不跨天重复 / 在途日不排游玩点，源码里明确声明**不查**返程与营业时间；(2) REJECT 硬排除 + MUST 强优先且不偷丢，在 `app/selection.py` 的 `apply_user_place_preferences`；(3) 返程赶得上、营业时间冲突、时间物理冲突，在 `planner.check_feasibility`（`app/planner.py:2063`）；(4) 超预算 → REJECT，在预算引擎（`app/planner.py` §23） |
| E3 候选未按池拆分 | ✅ 已修 | `discovery.POOL_KEYS = attraction/food/experience`（注意：**三池**，住宿区域是独立产物） |
| E4 缺"先选住宿区域→再选酒店" | ✅ 已修 | `discovery.extract_hotel_areas()`；`workflow._preferred_area` + `STAGE_HOTEL_AREA_SELECTED` |
| E5 景点全局排名不做区域聚类 | ✅ 已修 | `planner.cluster_places()`；`day.area` |
| E6 美食不看"当天此刻人在哪" | 🟡 部分 | 已按"当天位置 + 时间窗"从**已有候选**里插餐（`planner._insert_meals`）；**没有**以当前位置为圆心重新向高德检索附近餐厅 |
| E7 缺 Top-K 比选层、Jev 在关键路径 | ✅ 已修 | `MAX_PLAN_CANDIDATES = 5`；Jev → LLM Ranking → 动态权重 Top-1 三级降级 |
| E8 路线查询时机过早 | ⬜ 仍开 | 路线仍在 `node_verify_poi_and_routes` 内查（早于 `node_build_plan`）；已有缓解：深验只覆盖可能入选的点 |
| F1 首页缺旅行感 | ✅ 已修 | `frontend/app/page.tsx:261-347` |
| F2 默认入口颠倒 | ✅ 已修 | 同 B1 |
| F3 Guided 是普通表单、住宿策略只是 Radio | ✅ 已修（形态不同） | 卡片形态已做；但**选项集合与本文建议不一致**，真实枚举是 `value/location/rating/comfort/transit/auto`（没有"商圈美食优先/景点居中/安静舒适"） |
| F4 Discovery 期间用户干等 | ✅ 已修 | `frontend/components/guided/discovery-research.tsx:21-28` 逐项打勾 |
| F5 缺「探索确认页」 | ✅ 已修（形态变更） | 不是独立路由，落成向导第 2 步「探索确认」（向导 2026-09-21 由 6 步并成 3 步，见 `docs/11` §2）；`[选择春熙路]/[让我决定]` 按钮**有意未做**（后端无对应 PATCH 字段）；**美食品类勾选与实现相反**（实现是逐店标 MUST/WANT/REJECT，没有菜系字段） |
| F6 进度页看不到中间决策 | ✅ 已修 | `workflow` 六个阶段码 + `frontend/components/planning-progress.tsx` |
| F7 结果是"AI 报告"不是地图+时间线 | ✅ 已修 | `frontend/components/plan-workspace.tsx`（地图 / 时间线 / 酒店 / 美食 / 预算） |
| F8 缺 IA 与优先级 | 🟡 部分 | P1 全部交付；P0 的住宿策略选项、`[选择]/[让我决定]`、美食品类形态不同或未做；P2 的图片、拖拽替换未做 |

**当前仍然开着的三条**：**E8**（路线查询时机）、**C1**（模型时延的解题策略）、**A7**（相邻点路线缓存，待决策）。
`F5`/`F3`/`F8` 属"做了但形态与本文建议不同"，需要修订的是**本文的期望**，不是代码。
`A2`/`A4` 的剩余部分（`places.source` 标识位、Hub 内存缓存）是**有意的取舍**，不再作为待办：
前者由 `sources.status=CACHED` 覆盖，后者是"同一 Hub 实例内的单次调用去重"这一层语义。

---

## A. 数据复用与存储（本轮核心问题）

### A1.【核心】没有跨 session / 跨用户的「目的地级」静态数据缓存

**现状**
数据复用只有三层，且都局限在窄范围：

| 层 | 范围 | 生命周期 |
| --- | --- | --- |
| `ProviderHub._cache`（内存） | 单个 Hub 实例 | 进程重启即失 |
| `CallLedger`（账本） | 单次 run | run 结束即失 |
| `PrefetchBundle`（`planning_sessions.prefetch_json`） | 单个 session | 会话过期（45 分钟）即失 |

会话 A（北京→成都）查过的小红书攻略、抽取的地点、高德 POI，会话 B（武汉→成都）
**一点都复用不到**——即便目的地都是成都。

**为什么算缺陷**
机酒火是实时数据（价格/时刻秒级可变），每 session 查可以容忍；但**攻略正文、攻略里提及的地点、
高德 POI 的名称/坐标/营业时间/地址**是半静态的，几周才变一次。为这些数据每次开新 session
都重打一遍社媒 + 高德，是纯浪费，也是当前 Discovery 慢（180~325s）和 TikHub 频繁限流的根源之一。

**影响**
- 时间：每个去成都的用户都要重新付一遍攻略抓取（含 LLM 抽取）+ 高德 POI/详情；
- 配额：TikHub 免费额度被同一城市的重复查询耗尽（实测多次 `RATE_LIMIT`/`FREE_CREDIT_EXHAUSTED`）；
- 一致性差：两次 session 抽出来的"成都游玩集合"因社媒波动而不同。

**方向（字段级，供后续实现）**

新增两张**目的地级共享表**（跨用户、跨 session，按城市分片）：

```sql
-- 半静态：高德 POI 数据（按 city + place_id 共享）
CREATE TABLE IF NOT EXISTS city_pois (
    city            TEXT NOT NULL,          -- 目的地（建议用高德 region 口径，如"成都市"）
    place_id        TEXT NOT NULL,          -- 高德全局 POI id
    name            TEXT,
    normalized_name TEXT,
    category        TEXT,                   -- attraction/food/shopping/hotel/transfer/other
    lng             REAL, lat REAL,
    address         TEXT, business_area TEXT, district TEXT,
    opening_hours   TEXT, phone TEXT, rating REAL,
    amap_verified   INTEGER NOT NULL DEFAULT 0,
    trust_score     REAL, ad_risk REAL,     -- 半静态信号（可重算；缓存起来省一次计算）
    evidence_count  INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,          -- 高德数据最后刷新时间
    PRIMARY KEY (city, place_id)
);

-- 半静态：攻略 → 地点 的提及（供 trust/ad_risk 重算 + 前端"来自 N 篇攻略"文案）
CREATE TABLE IF NOT EXISTS city_poi_mentions (
    city          TEXT NOT NULL,
    place_id      TEXT NOT NULL,            -- 高德核实过才有；未核实则用 raw_name 兜底
    raw_name      TEXT NOT NULL,            -- 攻略里的原始写法（未核实前的地点名）
    source_type   TEXT NOT NULL,            -- xhs/douyin/web
    provider      TEXT,
    source_url    TEXT,
    snippet       TEXT,                     -- 提及该地点的原文片段（quote，供 tone/可信度判断）
    tone          TEXT,                     -- recommend/warn/neutral
    published_at  TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (city, source_url, raw_name)  -- 同一篇攻略对同一地点只记一次
);
```

**读路径（复用）**：建 session 后先按 `city` 查这两张表，直接拼出"成都游玩集合"给用户勾选；
只有「没命中 / 已过期」的才去查小红书 + 高德，查到后 `UPDATE ... updated_at` 或 upsert 回表。

**写路径**：Discovery 抓到的攻略与高德 POI，在写入当次 session 的同时（或后台任务）同步进
这两张共享表，供下一个 session 读。

### A2. `places` 表主键设计天然阻止了共享

**现状** `places` 主键是 `(run_id, place_id)`（`app/store.py`）。同一个高德 POI，
每个 run 各存一份；`place_evidence` 也按 run 隔离。

**为什么算缺陷** 这个设计是"为了同一 run 内证据链干净"，但它从存储层上就**排除了跨 run 共享
POI 的可能**——A1 的共享表必须与它分离（`places` 继续管单 run 的行程内引用，共享表管"城市知识库"）。

**方向** 不动 `places`（它仍是 run 内引用），共享走 A1 的两张表；`places` 行增加一个
`source="city_cache"` 标识位，说明这个候选是从共享表拿出来的，便于审计。

### A3. `prefetch_json` 是"会话缓存包"，不是"城市知识库"

**现状** Discovery 结果整体序列化进 `planning_sessions.prefetch_json`，仅该 session 的 run
通过 `PrefetchBundle.load()` 读。会话过期（默认 45 分钟）即失效。

**为什么算缺陷** 它解决的是"同一用户当次往返 Discovery→Run"的复用；解决不了"下次再来成都"
的复用。方向是把里面**半静态的那部分**（evidences、places、social_queries）的副产品落进 A1
的两张共享表，prefetch_json 只保留实时部分与当次会话状态。

### A4. ProviderHub 缓存是进程内存、不落库，TTL 形同虚设

**现状** `ProviderHub._cache` 是实例内存字典，TTL 定义了 `poi 24h`、`social 6h` 等
（`app/providers.py` `CACHE_TTL_SECONDS`），但**不持久化**——服务重启就全丢；且
Discovery 与 Run 用两个 Hub 实例，缓存互相看不见。

**为什么算缺陷** 定义了 24h/6h 的 TTL，实际生效窗口是"进程存活期内"。落库的 A1 表才配得上
这种"跨请求、跨天"的时效语义。

**方向** 半静态 TTL 的落点从"Hub 内存字典"迁移到 A1 表 + 一个刷新调度（见 A6）。

### A5. 数据没有按"实时 / 半静态"分层，一律每 session 查

**现状** Discovery 四条线（交通/酒店/攻略/地点）无论变不变，每次新 session 都全量调 Provider。

**方向** 明确分层（用户判断正确，落到代码）：

| 数据 | 时效 | 策略 |
| --- | --- | --- |
| 车次 / 航班 / 酒店价 / 门票价 | 实时 | 每 session 查，不共享 |
| 攻略正文、提及地点、高德 POI（名/坐标/营业时间/地址）、相邻点路线耗时（可选） | 半静态 | 目的地级共享 + TTL 刷新 |

### A6. 没有"过期重算 / 后台刷新"的更新机制

**现状** 所有缓存只有"取数时比较时间戳"的惰性 TTL，没有过期重算，更没有按天/按半月的后台
刷新调度。

**方向**
- 惰性刷新（先做）：`city_pois`/`city_poi_mentions` 命中时查 `updated_at`，超过阈值
  （攻略 15 天、高德 POI 15 天，做成 config）就先回吐**旧数据兜底**、再后台异步补刷新，
  **绝不因刷新失败删掉旧数据导致空榜**（真实优先、降级披露）。
- 定时刷新（后做）：`qoder_cron`/后台任务按城市+类别轮询，把"每个工作日刷新成都美食/景点"做成任务。

### A7. 路线虽然"设计上不查"，但同目的地相邻点路线也是半静态，可一并纳入缓存（待决策）

**现状** 路线只在正式 Run 查（Discovery 一个不查），且只做同 run 内去重。

**方向** 相邻 POI 对的路线耗时（`RouteOption.duration_minutes`）变化慢，理论上能像 POI 一样
进共享表；但它的配对依赖"候选集合 + 用户所选酒店"，命中率存疑。**先不上**，记为待评估项，
等 A1 落地后用真实命中率数据再决定。

---

## B. 产品交互

### B1. 主页默认是"一句话一次性规划"，POI 反馈流藏在次级入口

**现状** 主页 `/` 是 `TripSearch → createPlan(一句话) → 直接跑完全流程`，没有 POI 反馈；
完整的"目的地/天数 → POI 列表勾选 → 再规划"在 `/guided`（当时的六步向导，第 4 步"想去哪里"
已有美食/游玩分类卡片 + 必去/想去/不去 + 都随便）。

**为什么算缺陷** 用户预期的主路径是"填目的地→给 POI 列表→我选→再规划"，而当前落地页把它
反过来了：默认一句话直出，交互式选择只是页脚一个次级链接。用户大概率没走进 `/guided`。

**方向**
- 把 `/guided` 提升为主路径（主页默认先建 session 进向导），一句话模式降级为"高级/快捷入口"；
- 或在主页输入框上给出两个显式 tab：`一句话` / `逐步选`。

### B2. POI 候选每次现抽，不查库

**现状** `/guided` 的 POI 列表 = 当次 Discovery 从小红书+高德**现抽**的结果；换一个人来成都
又从头抽一遍。

**方向** POI 列表先查 A1 的 `city_pois` + `city_poi_mentions` 拼"成都游玩集合"（这个几乎 0 秒
就能给用户），当次抓取的增量再回写。用户勾选体验从"等 2~5 分钟"变成"秒出"。

### B3. POI 分类 / 排序口径要能解释"这些是从库里拿的历史结果"

**现状** POI 卡片展示 trust/ad_risk/amap_verified，但没有"数据新鲜度"维度。

**方向** 卡片或列表顶部加一行"地点信息来自 X 天前整理的成都攻略库，已按最新抓取增量更新"，
把 `updated_at` 直接暴露给用户，避免"半静态缓存"被误读成实时。

---

## C. 性能与观测遗留（上一轮已确认，待一起改）

### C1. 模型时延是当前第一瓶颈，只有硬上限没有解题策略

**现状** 实测单次调用 `query_expansion 30.4s` / `extract_places 54.7s`（一批）/ `critic 10~67s`
/ `final_answer 15~43s`；无预取路径 367.9s 里模型占约 300s。已有按用途的硬上限 + 批超时按单篇重试，
但**上限不能把慢调用变快**。

**方向** 两条路二选一（或并用）：换更快的模型 / 在保证质量门不降的前提下减少调用次数
（如把 critic 与质量门合并为一次判断）。需要产品口径。

### C2. 12306 / 途牛火车单源 90s 长尾 + TikHub 配额耗尽

**现状** 两条火车线都出现过打满 90s 预算；小红书侧本轮多次 `RATE_LIMIT`/`FREE_CREDIT_EXHAUSTED`。

**方向** 火车：下调单源预算（会把"慢但成功"判成超时，属取舍，已做成 config）；社媒：A1 落地后
同一城市的重复查询消失，配额压力随之下大幅缓解。

---

## D. 其它

### D1. 上一轮已修复的问题不再列（存档对照）

以下已修并 commit，仅存档、不在本清单内：交接标签按方法名匹配（`search_trains` 等）导致
"补查了却报 reused"、复用结果 provider 写死 12306、profiler 把 Jev 算成 0.0s、
`--no-wait-discovery` 静默失效、门面查询长尾（按 Tool 预算）、POI 详情重复查、REJECT 点仍被深度验证。
详见 `_acceptance/perf_report.md` §10。

### D2.（待办）共享表写入的并发与一致性

**方向细节** 多 session 同时写 `city_pois` 时用 `INSERT ... ON CONFLICT (city, place_id) DO UPDATE`
并按 `fetched_at` 取新者；刷新用"先读旧值兜底、再异步写新值"，避免一个慢 session 挡住所有人的读。

---

## E. 决策与规划质量（固定权重 → 动态偏好，方案见 docs/14_决策与规划质量优化.md）

> 这一类不是"查得慢 / 复用不到"，而是**决策本身做错了**：Python 用固定权重在替用户定义
> "什么是好旅行"。改造目标一句话：**LLM 决定「这个用户会喜欢什么」，Python 只负责「这个
> 方案能不能真的走」**。完整设计见 `docs/14_决策与规划质量优化.md`，这里按可实现单元拆条。

### E1.【核心】旅行决策靠固定 Python 权重，用户差异几乎不起作用

**现状** 酒店分数 ≈ 价格+评分+位置+偏好的固定权重（`app/workflow.py` 打分）；景点 ≈ Trust −
AdRisk + 偏好 + 路线分。权重是写死的，同一个目的地、不同的人，得到的排序逻辑基本一致。

**为什么算缺陷** 数学上"合理"、体验一般：酒店便宜评分高却离商圈远；景点排名高但来回跑；
用户说"主要想吃"仍按固定景点权重排；不同用户规则没差别。本质是 Python 越俎代庖替用户定义
"什么是好旅行"。

**影响** 结果同质化，个性化偏好只在权重里占一个很小的固定份额。

**方向** 见 docs/14 §1–§4：LLM 在用户输入 / Guided 完成后生成 **Dynamic Preference Profile**
（hotel_area / hotel / attraction / food / pace 五组动态权重 + `travel_style` + 每日 POI 目标），
Python 只用它计算候选 Top-K，软取舍交给 LLM。

### E2. Python 的"硬规则"与"软决策"没有分界

**现状** Python 现在既做真实性/可行性验证、又直接决定取舍（选哪家酒店、一天塞几个景点、
绕不绕路吃火锅），两者混在一起。

**方向** 见 docs/14 §3 明确划界。Python 只保留硬规则：地点/酒店真实存在、价格不伪造、路线不
伪造、时间不物理冲突、营业时间不冲突、返程赶得上、REJECT 不出现、MUST 无原因不丢、用户
"预算绝对不能超"时不超。其余（住哪、贵100值不值、一天2个还是4个、绕不绕路）一律软决策。

### E3. 候选没有按"池"拆分，景点/美食/住宿区域/体验混为普通 POI

**现状** 所有地点统一当 POI，走同一套搜索→去重→打分→排序管道。

**方向** 见 docs/14 §7–§8：拆成**住宿区域池 / 景点池 / 美食池 / 体验池**；并明确高德只负责真实
性验证（存在、坐标、类型、地址、营业时间、路线、附近），"值不值得去"由攻略 Evidence 负责
（是否真推荐、推荐什么、是否广告、是否本地人常去、是否长期有人提）。

### E4. 缺"先选住宿区域 → 再选酒店 → 酒店成为旅行中心"的流程

**现状** 从全部酒店里直接打分挑一家，酒店只是"晚上睡一觉的地点"。

**方向** 见 docs/14 §5、§9–§11：
- Guided Mode 增加住宿策略单选（商圈/美食优先、景点居中、交通方便、性价比、安静舒适、帮我选）；
- 先判断最适合住哪个区域（攻略推荐度、美食/商圈密度、夜间便利、地铁便利、到机场车站便利、
  核心景点覆盖、用户偏好），用动态权重 Top-K + LLM Soft Ranking 选区域，再在区域内挑酒店；
- 酒店确定后以其为 Base 找附近早餐/夜宵/高档餐厅/商圈/地铁/夜游点。

### E5. 景点全局排名硬塞，不做区域聚类

**现状** 景点逐个全局排名后按天硬塞，容易一天跨几个区来回跑。

**方向** 见 docs/14 §12：先按区域聚类（如武侯祠/锦里、熊猫基地+春熙路、宽窄巷子/人民公园），
每天主打一个区域、2~4 个核心点，少跨城、不折返、留吃饭与自由时间；具体数量由
Dynamic Preference Profile 的 `pace` 决定。

### E6. 美食与景点共用一套 POI 排序，不考虑"当天此刻人在哪"

**现状** 餐厅当普通 POI，按全局评分塞进当天，可能出现"17:30 人在春熙路、晚餐安排在 10km 外"
的情况。

**方向** 见 docs/14 §13：午餐/晚餐单独规划——当天景点骨架 → 此刻人在哪 → 附近真实餐厅 →
攻略反复推荐 → 具体菜品 → 营业与否 → 绕路多少 → 选这顿。目的地的"最高分餐厅"不能再无视距离
直接塞进行程。

### E7. Top-K + LLM Soft Ranking 的"比选层"缺失，Jev 在关键路径上

**现状** Python 打分后直接取 Top-1 定案；Jev 决策要么参与要么整个卡住，属于关键依赖。

**方向** 见 docs/14 §15–§17：Python 可行性校验通过后生成 Plan A（美食/商圈最佳）/ B（最顺路）/
C（更轻松）多个备选，由 LLM 按 Profile 软选；LLM 不得改真实数据（价格/营业时间/路线/POI），
只做软选择。Jev 降级为增强能力：可用→参与 Top-K 判断，不可用→普通 LLM Ranking，再失败→
动态权重 Top-1。

### E8. 路线查询时机过早（与 A7 / C2 联动）

**现状** 路线在候选生成阶段就大范围查询，最终很多路线根本不会进行程。

**方向** 见 docs/14 §14：等酒店/景点/餐厅真正定下来后，才按"酒店→景点A→午餐→景点B→晚餐→
酒店"的实际链查真实 Route，从源头减少无用路线查询（A7 的半静态路线缓存可与此步合并评估）。

---

## F. UI 与前端体验（方案见 docs/TravelPlan_UI_优化方案.md）

> 独立成类：B 是"产品流程"问题，F 是"前端呈现/体验"问题。两者有交集（F2 与 B1 同源、
> F3 与 E4 住宿策略联动），排期时合并处理。完整设计见 `docs/TravelPlan_UI_优化方案.md`。

### F1.【核心】缺少旅行感，首页更像"AI 输入框/表单工具"而非旅行产品

**现状** 首页 = 一句话输入 + 高级条件 + 开始规划 + 几张说明卡片，无目的地视觉、地图、
住宿区域、美食、景点等旅行元素。

**为什么算缺陷** 用户第一眼没有"我在规划一次旅行"的代入感，且 Trace/Trust/Evidence 等
工程信息反客为主。

**方向** 见 UI §1、§2、§5：首页重构为「你想去哪里？（出发地/目的地/日期/天数）+ 开始规划」，
下方保留热门目的地卡片、最近规划、"美食/亲子/拍照/轻松度假"等旅行方式入口；一句话速规划
收为次级链接。目标体验对齐"Airbnb/Trip.com 信息质感 + 小红书发现感 + AI 动态规划"。

### F2. 默认入口颠倒：一句话直出为主、Guided 为次（与 B1 同源）

**现状** 同 B1：主页默认 `TripSearch → createPlan` 一句话直出，向导只是页脚链接。

**方向** 见 UI §3：默认进 **Guided Mode**，Quick Mode 降为"我已经想好了，直接一句话规划"。
与 B1 合并排期。

### F3. Guided Mode 是普通表单，住宿偏好只是 Radio

**现状** 当时的多步向导偏表单；「住宿策略」还没有独立、产品化的呈现。

**方向** 见 UI §6：拆为 Step1 基础信息 / Step2 大交通偏好 / Step3 住宿策略 / Step4 旅行兴趣 /
Step5 节奏 / Step6 确认；住宿策略做成**卡片**（商圈·美食优先 / 景点居中 / 交通方便 / 性价比 /
安静舒适 / 帮我选择），与 E4 的住宿区域选择共用同一份偏好数据。

### F4. Discovery 期间用户干等，看不到"系统在研究什么"

**现状** 前端只有 loading/skeleton + 通用文案，用户感知是"在等一个 LLM 回答"。

**方向** 见 UI §7：后台开始查交通/酒店/攻略/POI/住宿区域的同时，前端**逐步打勾**——"✓ 找到 18
个航班/高铁候选 → ✓ 42 家酒店 → ✓ 读 26 篇攻略 → ✓ 34 个景点体验 → ✓ 48 个美食候选 → ✓ 4 个
住宿区域"，让"研究感"可见。

### F5.【核心】缺「探索确认页」（Discovery 与正式 Run 之间的桥梁）

**现状** Discovery 就绪后直接进正式规划，用户没有机会确认住宿区域 / 景点 / 美食偏好。

**方向** 见 UI §4、§8，新增探索确认页：
- **推荐住宿区域**：列 3 个候选区域（春熙路/太古里、天府广场、宽窄巷子……）带"攻略推荐度 +
  适合你"星级，[选择春熙路] / [让我决定]；
- **景点选择**：系统发现、用户只需 MUST / WANT / REJECT（不手输 POI 名）；
- **美食偏好**：先选品类（火锅/串串/川菜/小吃/甜水面/网红甜品），具体餐厅后续按当天位置 +
  距离 + 攻略 + 营业时间动态决定。
与 E4（住宿区域选择）、E5、E6 深深联动——这一页就是 E 类决策的"人机确认面"。

### F6. 正式规划 UI 只显示"正在规划…"，AI 的中间决策不可见

**现状** 进度页=阶段进度条 + 通用文案，看不出走到哪一步、在比什么。

**方向** 见 UI §9：实时展示真实阶段——"已确定住宿区域：春熙路 / 正在比较附近酒店 24→6→3 /
正在安排每天区域 Day1 武侯区… / 正在匹配午餐晚餐 / 正在用高德检查路线 / 正在检查预算和返程
时间"，把 E 类决策过程映射成可读的进度叙事。

### F7. 最终行程是"AI 报告"，不是能拿着走的地图 + 时间线计划

**现状** 结果页偏报告式，工程字段（Trust/Ad Risk/Trace/Provider Call）成为主要视觉。

**方向** 见 UI §10–§13：
- 结果页改为 **地图 + 时间线 + 酒店 + 美食 + 预算**；Day 1/2/3 切换、地图同步切换路线；
- 地点卡片只显示图片/名称/推荐原因/距离/预计停留/营业时间/价格/具体推荐内容（如"距离当前
  景点 850m · 18:00 营业 · 人均 ¥90 · 回锅肉被多篇提及"）；
- Trace/Trust/Evidence 收进"查看依据"展开层或后台管理端，不作为普通用户主视觉。

### F8. 缺最终信息架构与开发优先级

**现状** 无明确的页面信息架构（IA）与排期。

**方向** 见 UI §14、§15，按优先级排期：
- **P0**：首页默认进 Guided Mode、Quick Mode 降级、新增住宿策略、新增探索确认页、展示推荐
  住宿区域、景点 MUST/WANT/REJECT、美食偏好选择；
- **P1**：规划过程实时展示 Discovery/Planning 进度、最终结果改 Day Timeline、加地图与每日路线、
  酒店/美食独立卡片；
- **P2**：目的地图片与视觉、Evidence 展开、行程拖动/替换 POI、地图与 Timeline 联动。

核心原则（贯穿全体 F 类）：> **不要让用户感觉自己在"使用一个 Agent"，而应该让用户感觉自己正在"规划一次旅行"。**