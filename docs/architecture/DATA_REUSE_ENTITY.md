# 数据复用与实体

> 这份文档回答三个问题，全部以代码事实为准（不写估计值）：
> 1. **什么时候查、什么时候不查**——每类数据的取数时机与决策条件；
> 2. **查完存到哪、存了什么**——每张表的用途与字段；
> 3. **复用从哪读、读到的数据结构是什么**——双层取数 + 四层复用的具体路径。
>
> 涉及的源：社媒攻略（小红书/抖音/联网搜索）、高德（POI 搜索 / POI 详情 / 路线 / 地理编码）。
> 大交通（12306/途牛）、酒店、门票是同一套机制，本文只带过，重点讲社媒与高德。

---

## 1. 先建立一个心智模型：一次规划的两种阶段

一次「开始规划」之前，系统分两个阶段：

```
建会话（Planning Session）
   ├─ 先查第 ④ 层「城市知识库」（city_pois / city_evidences / city_cache_meta）
   │     ├─ 命中 → 攻略与 POI 直接用缓存（social / places 阶段标 CACHE 或 STALE_CACHE）
   │     │         交通与酒店**仍然现查**（实时数据不进缓存）
   │     └─ 未命中 → 跑下面的实时 Discovery，跑完回写城市知识库
   └─ 后台 Discovery（实时取数这一路）：transport / hotels / social 三线并行，
        随后串行跑 places（地点抽取依赖攻略结果）+ 动态偏好画像（用于住宿区域排序）
        └─ 结果打包进一个 PrefetchBundle，存进 planning_sessions.prefetch_json
用户点「开始规划」
   └─ 正式 Run：SuperHarness Agent Loop（app/agent_runner.py::execute_agent_run）
        ├─ Discovery 过户：_adopt_prefetch_sources + _materialize_city_evidence
        │    （复用的调用与攻略正文要在落 plan 之前写进本次 run 的 sources）
        └─ Agent 的 user prompt 里带一份**预取摘要**（前 N 条候选 + 用户对该点的选择），
           Agent 自己决定哪些直接采用、哪些用工具补查
```

- **Discovery 阶段**：只查"原始候选"（车次列表、酒店列表、攻略正文、POI 候选 + 详情），
  不做路线、不做门票、不做深度打分——那些要等用户选完（MUST/WANT/REJECT、酒店、节奏）才能决定。
- **正式 Run 阶段**：由 Agent 按需补查、比选、算路线与门票、排程、交卷；
  预取只是它的**起点**，不是必须逐项消费的清单。
- 两者都从**同一个 `ProviderHub` 接口**取数，所以"复用"是跨阶段的，而不是各自为政。

> 旧固定流程（12 步图）曾把预取按"每个节点先问能不能复用"的方式消费；
> 那套节点级判断随流程退役，本文中标注 `node_*` 的段落都属于**旧实现**，
> 保留下来只用于读历史 run 与理解设计意图。Agent 路径怎么用预取见 §6 末尾。

---

## 2. 四层复用机制（最关键的抽象）

系统有**四层**复用，粒度、生效范围、存哪、活多久各不相同。理解它们，就等于理解了
"复用从哪读"。

| 层 | 是什么 | 粒度 | 范围 | 存在哪 | 活多久 | 命中时记录什么 |
| --- | --- | --- | --- | --- | --- | --- |
| ① ProviderHub 内存缓存 | `hub._cache` 字典 | 单次 Provider 调用 | 同一个 Hub 实例（进程内存） | 内存 | 按 `kind` 的 TTL（见下） | `cached=true`，`fetched_at` 保留首次真实时间 |
| ② CallLedger 账本 | 每个 run 一个，`ledger.fetch/remember/seed` | 一个"查询键" | 同一次 run 内 | 内存（随 run 结束丢弃） | 本次 run | `cache_hit` / `provider_called` / `prefetch_reused` |
| ③ PrefetchBundle | Discovery 打包的结果，`dump()→json` | 一整条线（交通/攻略/地点） | 跨阶段（Discovery→Agent 规划上下文），按 Session 隔离 | `planning_sessions.prefetch_json` | 直到会话过期（默认 45 分钟） | Agent 在 user prompt 里看到摘要（前 N 条），自行决定采用或补查 |
| ④ 城市知识库 | `app/city_cache.py` 读写的 `city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta` | 目的地级（高德 POI + 攻略提及 + 攻略正文 + 检索词） | **跨会话、跨用户**，按城市分片 | 上述四张表 | 攻略 15 天 / POI 15 天（可配），过期先回旧值再后台刷新；写入时清理超期行 | `stage.status = CACHE`／`STALE_CACHE`、`prefetch.city_cache.*`、来源 `sources.status = CACHED` |
| ⑤ Canonical Place 实体层 | `app/places.py` 读写的 `canonical_places` / `place_aliases` / `place_provider_refs` / `place_relations` / `place_evidence_links` / `poi_query_cache` | **一个现实地点**（名字 / 别名 / 各 Provider 的 ID / 主点子点关系） | **跨会话、跨用户**，按城市分片 | 上述六张表 | 随 ④ 一起重建；查询缓存按 `CITY_CACHE_POI_TTL_DAYS` 过期 | `places.resolver.*`（复用词、合并 / 折叠 / 丢弃计数）、`place_resolver_traces` 表（每次判断的依据） |

**TTL（`app/providers.py` 的 `CACHE_TTL_SECONDS`，单位秒）**：

| kind | TTL | kind | TTL |
| --- | --- | --- | --- |
| `poi` / `geocode` | 24 小时 | `flight` | 5 分钟 |
| `social` / `web` | 6 小时 | `train` | 2 分钟 |
| `hotel` | 10 分钟 | `route` | 15 分钟 |
| `ticket` | 30 分钟 | | |

> 注意 ① 是**每 Hub 一个**：Discovery 用的 Hub 和正式 Run 用的 Hub 是两个实例，
> 所以 ① 的缓存**不跨阶段共享**。跨阶段复用靠 ③（PrefetchBundle），同阶段去重靠 ②（账本），
> **跨会话/跨用户复用靠 ④（城市知识库）**。

### 2.1 第 ④ 层：城市知识库（`app/city_cache.py`）

前三层都局限在"这一次会话 / 这一次 run"。第 ④ 层解决的是"**下一个人再去成都**"：
同一个目的地，攻略正文、攻略里提到的地点、高德 POI（名称/坐标/营业时间/地址）都是半静态数据，
几周才变一次，没有理由每个会话都重打一遍社媒 + 高德。

**边界（很硬）**：只有攻略与 POI 进这一层。**车次/航班/酒店价/门票价永远不进**——它们是实时数据，
每个会话都必须真实查（`_prefetch_with_city_cache` 命中缓存时仍然现查交通与酒店）。

**接口**（`app/city_cache.py`，前端不直调；`app/sessions.py` 是唯一调用方）：

| 函数 | 语义 |
| --- | --- |
| `normalize_city(city)` | 只做空白归一，**不猜行政别名**（避免把不同城市错合并） |
| `read_candidates(city, *, store, now, allow_stale, evidence_limit)` | 返回 `CityCacheHit \| None`。**没有 POI 行就不算命中**（防止一次空 Provider 响应被缓存）；POI 在 `city_cache_poi_ttl_days` 内、且攻略（提及 + 正文 + 检索词）全部在 `city_cache_guide_ttl_days` 内才算新鲜，否则 `stale=True`；`stale` 且未传 `allow_stale` 时返回 `None`。`evidence_limit` 默认取 `EVIDENCE_MAX`，防止一次把整座城市的正文塞进 `prefetch_json` |
| `write_candidates(city, places, evidences, *, store, updated_at, social_queries, social_served_queries)` | upsert 四张表后**顺手清理过期行**，返回写入的 POI 数。`mentions` 由 `evidence.place_mentions` 按名称键匹配到 POI 后生成（`snippet` 取正文前 500 字）；正文按**内容键** `url + title` 落 `city_evidences` |
| `refresh_in_background(city, *, refresh)` | 同城刷新请求**合并**（进程内集合去重）+ 2 线程池异步执行；无回调时安全地什么都不做 |

**`updated_at` 取所有行的最小值**——不能把一条刚补写的数据伪装成整座城市都刚刷新。

**为什么要清理过期行**：新鲜度的口径是"**所有**行都在 TTL 内"，只增不删的话，一条再也搜不到的
旧攻略会让这座城市**永远 stale**，于是每个新会话都触发一次后台全量刷新，TTL 名存实亡。
`prune_city_cache()` 只删各自 TTL 之外的行（攻略 15 天 / POI 15 天），所以"先用旧数据兜底、
再后台刷新"的语义不受影响。

**`sessions.run_discovery` 里的读写路径**（`app/sessions.py`）：

```
建会话 → read_candidates(destination, allow_stale=True)
  ├─ 命中 → _prefetch_with_city_cache()：
  │         places  ← city_pois
  │         evidences / queries ← city_evidences + city_cache_meta   ★ 正文与检索词也复用
  │         交通与酒店照旧现查；stage.status = CACHE 或 STALE_CACHE
  │        └─ 若 stale → refresh_in_background(refresh=refresh_city_cache)
  └─ 未命中 → discovery.prefetch() 全量实时跑 → write_candidates() 回写
              → prefetch.city_cache = {"source": "live", ...}
              （回写失败只记一条 degradation，不把这次 live Discovery 判成失败）
```

**跨会话复用的正文要重建 run 级 provenance**（`app/workflow.py::_materialize_city_evidence`，
Agent 路径在**拉起循环之前**调用）：
`sources.source_id` 与 `evidence.evidence_id` 都是**全局主键**，同会话 Discovery→Run 能沿用原 id
（会话与 run 一一对应，不会互相顶掉），跨会话不成立 —— 沿用原 id 会把上一次 run 的 sources 行
`INSERT OR REPLACE` 成本次 run 的，上一轮证据链当场断掉。所以复用时**重新分配** `{run_id}-cache-{n}`，
并在 `sources` 里如实写 `status=CACHED`、原始 `fetched_at`、`normalized_json={origin: city_cache, ...}`。

**谁来把这一层填上：预热**。上面两条路径都只在"有用户真的来查"时才写 —— 第一个去成都的人
仍然要全价付掉社媒检索与模型抽取。`scripts/preheat_cities.py` 把这一步挪到离线
（本地 / CI 跑，**不要放 Render**：512Mi 实例上会跟健康检查抢资源）：
每座城市内部跑一次**预热 Agent**（`app/preheat_agent.py`，工具集 `app/preheat_tools.py`），
由它决定搜哪些攻略、登记哪些地点，最终仍写同一批表
（`app/places.py` 实体归一化 + `app/city_cache.py` 的城市缓存），
所以"预热出来的"与"用户自然形成的"是同一种数据，读路径一行都不用改。
它还会把"新鲜但没有社媒证据"（典型是 tikhub 额度耗尽 / 限流）的城市标成**降级**并打印原因；
补社媒要显式加 `--retry-degraded`（默认不重跑，避免社媒不可用时白烧网页与高德的配额）。
批调度（跑哪些城、串行、每天限量、退出码）仍在脚本里。见
[主规划：Agent Loop](AGENT_LOOP.md) §8 与 [部署说明](../operations/DEPLOYMENT.md) §9。

**观测**：`prefetch.city_cache` 里带 `source` ∈ `live` / `city_cache`、`updated_at`、`stale`，
以及复用的条数（`evidences` / `mentions` / `social_queries`）；会话视图（`GET /api/v1/planning-sessions/{id}`）
把它们作为 `source` / `city_cache` / `updated_at` 透出，前端据此渲染「地点信息来自 X 天前整理的攻略库」。
复用事实还会写进本次 run 的 `degradations`（"攻略正文复用城市知识库缓存（N 条，最早抓取于 …）"）。
只读入口是 `GET /api/v1/city-cache/{city}`，管理端强制刷新是 `POST /api/v1/admin/city-cache/{city}/refresh`（202）。

**表结构**（`app/store.py`，upsert 用 `WHERE excluded.updated_at >= 现有 updated_at`，新者胜）：

| 表 | 主键 | 关键字段 |
| --- | --- | --- |
| `city_pois` | `(city, place_id)` | `name, normalized_name, category, lng, lat, address, business_area, district, opening_hours, amap_verified, evidence_count, updated_at` |
| `city_poi_mentions` | `(city, source_url, raw_name)` | `place_id, raw_name, source_type, provider, snippet, tone, published_at, updated_at` |
| `city_evidences` | `(city, evidence_key)` | **攻略正文**：`source_type, provider, source_url, title, author, published_at, fetched_at, text, place_mentions_json, specific_dishes_json, raw_metrics_json, updated_at`。`evidence_key` = `sha1(url + title)` 的内容键，**不是** run 级 `evidence_id`（后者内嵌 session_id，跨会话必然不同） |
| `city_cache_meta` | `city` | `social_queries_json, social_served_queries_json, updated_at`，用来省掉一次 `query_expansion` 模型调用 |

前三张表都有 `(city, …, updated_at)` 索引。

> 内容键为什么带上标题：只按 URL 去重会**丢证据**——搜索引擎对多个 query 会返回同一个 URL，
> 标题与正文却不同。带上标题后，"同一篇文章重抓"仍落同一行，"同 URL 的不同条目"各占一行。

**升级说明（旧库上线的行为）**：`city_evidences` / `city_cache_meta` 是新增表，老库里没有行。
老库命中缓存时 `hit.evidences` 为空，正式 run 会退回"全量重搜攻略"的旧行为（不会报错），
直到这座城市被刷新一次（stale 触发的后台刷新或 `POST /api/v1/admin/city-cache/{city}/refresh`）
把正文写进去 —— 也就是说**最多一个攻略 TTL（15 天）内自动补齐**，不需要手工迁移。

**公开只读接口不外发正文**：`GET /api/v1/city-cache/{city}` 不需要鉴权，因此只返回候选与
攻略的**元信息 + 字符数**（`CityCacheHit.payload(include_evidence_text=False)`）；
正文只在会话 Discovery 与正式 run 内部使用。

### 2.2 第 ⑤ 层：Canonical Place 实体层（`app/places.py`）

第 ④ 层解决"下一个人再去成都"，但它和它之前的实现都把**高德 `poi_id` 当成地点身份**，
于是同一座城市里"杜甫草堂"会有 5 行（博物馆 / 售票处 / 地铁站 / 大雅堂 / 南邻），
20 行候选只对应 4 个真实景点，而停车场 / 地铁站 / 酒店被当成景点列给了用户。

第 ⑤ 层的定位是：**`poi_id` 降级成证据，身份由 TravelPlan 自己维护**。

```text
LLM / Agent        决定需要什么信息
Reuse & Entity 层  判断已有数据能不能用、这个名字/这条 POI 是谁   ← 本层
ProviderHub        只有确实缺数据时才访问外部 Provider
Python Validator   最终硬校验
```

**解析顺序（任何一次 `search_poi` 之前都要先走一遍）**：

```text
检索词 → normalize → 别名索引（place_aliases）
                     ├─ 命中 → 直接用已有实体（**不调用高德**，action=alias_hit）
                     └─ 未命中 → 查询缓存 poi_query_cache
                                  ├─ 命中且未过期 → 取历史候选（**不调用高德**）
                                  └─ 未命中 → 才调 ProviderHub → 高德
高德结果 → 地理围栏（不属于目的地城市的直接剔）
        → 已见过的 poi_id → 归位到已知实体（不做相似度判断）
        → 其余按多信号聚类（城市 / 名字键 / 坐标 / 地址 / 商圈 / 类别）
        → 子设施挂到母体上，不作为并列候选
        → 回写实体层 + 查询缓存 + 留痕
```

**判定信号（方案 §9 / §11）**：

| 强度 | 条件 | 结果 |
| --- | --- | --- |
| 强 | 同一 `poi_id` 见过；或名字键完全相同且坐标未冲突；或名字高度相似 + 坐标 150m 内 | 合并 |
| 中 | 名字相似 + 地址一致；或名字相似 + 坐标 150m 内 + 同类；或名字相似 + 坐标 500m 内 + 行政区/商圈一致 | 合并 |
| 冲突 | 城市不同；坐标相距 1.5km 以上；**连锁品牌的不同分店** | **绝不合并**（宁可留两个实体） |

三条不变量：
1. **漏合并比错合并安全** —— 拿不准就留两个实体（错并是静默丢地点，用户看不到任何解释）；
2. **查询缓存不直接决定实体** —— 缓存只给历史候选，命中后仍按 `provider_refs` 映射回实体，
   映射不到就当未命中；
3. **子设施不是候选** —— 停车场 / 地铁站 / 售票处 / 服务中心 / 酒店，以及地址桩（`地名地址信息`）
   与住宅小区（`商务住宅`），一律不进候选；前几类能挂到母体上的挂上去（`place_relations`），
   挂不上的如实记进 `places.resolver.dropped`。

**接口**（`app/discovery.py` 的 `open_resolver` / `resolve_pois` 是唯一入口）：

| 函数 | 语义 |
| --- | --- |
| `PlaceResolver.plan_queries(terms)` | 把检索词分成"要查"和"已能复用"两组；`resolve_query` 单测用 |
| `PlaceResolver.ingest(records)` | 地理围栏 → 引用复用 → 聚类 → 子设施收敛；返回 `IngestOutcome`（`places / dropped / folded / notes`） |
| `PlaceResolver.record_query(term, …)` | 登记"这个词查到了什么"（存的是 **canonical id**，不是 Provider ID） |
| `PlaceResolver.needs_detail(poi_id)` | 实体层已有地址 + 营业时间时返回 False —— 不再重复打高德查详情 |
| `PlaceResolver.flush()` | 落库实体 / 别名 / 引用 / 关系 / 查询缓存 / 留痕；`store=None` 时纯内存、`persisted=False` |

`discovery.resolve_pois(resolver, results, terms, extra_places)` 是 Discovery 与（旧流程的）
正式 run **共用**的那一处收敛实现（"全收 + 按 id 去重"已被它取代）。**绝不空榜**，但兜底分两级：
先退回"过完围栏、且不是附属设施 / 地址桩"的原始候选，实在一条都不剩才退回完全未过滤的
原始候选 —— 并且两种情况都把这件事写进 `degradations`。

**地理围栏只认"能证明属于别处"**：高德的 `cityname` 是**地级行政区**，查"婺源"它回
`上饶市`、查"大理"它回 `大理白族自治州`，与用户输入都不相等。按相等比较会把整座城市的
POI 全判成越界（线上真实发生过：这两座城市的候选被清空，兜底又把未过滤的原始候选写进了
缓存）。所以判定顺序是"城市相关 → 行政区（`adname`，如"婺源县""大理市"）相关 → 两者
都拿不到就放行 → 都有值却都不相关才拒收"，其中"相关"容许 `大理` ⊂ `大理白族` 这类前缀
关系（`places.city_related`）。

**可观测**：`PlaceCandidates.resolver`（→ `stages.places.resolver` → 会话 `discovery_json`）
带复用词、合并 / 折叠 / 丢弃计数与明细；`place_resolver_traces` 表逐条留痕，
只读入口 `GET /api/v1/admin/places/resolver-traces?run_id=…|session_id=…|city=…`，
实体底座概览 `GET /api/v1/admin/city-cache/{city}/entities`。

**脏数据必须显式清理**：`prune_city_cache` 只删各自 TTL 之外的行，清不掉"还新鲜但内容是错的"
那些（去重键是 `(city, place_id)`，一次新的预热只会再加一批子设施、顶不掉旧的）。
`scripts/rebuild_city_cache.py` 做的是**整城清空 + 重建**，并在重建后自检
"候选里还有没有附属设施 / 提及表有没有建起来"。顺序必须是**先修代码再清库重建**。

---

## 3. 数据都存到哪：表结构总览

正式 Run 的产物全部落 SQLite（本地）或 Postgres/Neon（线上）。Schema 在 `app/store.py`。

**与"数据从哪来"直接相关的 4 张表 + 1 个 JSON 列：**

| 表 | 主键 | 一句话用途 | 关键字段 |
| --- | --- | --- | --- |
| `sources` | `source_id` | **每次 Provider 调用**的原始记录（审计） | `run_id, provider, source_type, source_url, query_json, fetched_at, raw_json, normalized_json, status` |
| `evidence` | `evidence_id` | 归一化后的**攻略/网页证据** | `run_id, source_id(→sources), source_type, provider, source_url, title, author, published_at, fetched_at, text, place_mentions_json, specific_dishes_json, raw_metrics_json` |
| `places` | `(run_id, place_id)` | 高德查回来的**地点候选** | `name, normalized_name, type, lat, lng, address, city, district, opening_hours, amap_verified, aliases_json` |
| `place_evidence` | `(run_id, place_id, evidence_id)` | Place ↔ Evidence 多对多 | 让"行程项 → 地点 → 证据 → 来源"这条链走得通 |
| `planning_sessions.prefetch_json` | — | **Discovery 整包结果**（就是 ③PrefetchBundle） | 见 §6 |

> `places` 的主键**带 run_id**是有意的：`place_id` 是高德全球唯一 ID，若只用它做主键，
> 第二次 run 走到同一个 POI 时 `INSERT OR REPLACE` 会把上一 run 的行顶掉、把两个 run 的证据挂到同一行。

**其余表**（本文会顺带提到）：

| 表 | 用途 |
| --- | --- |
| `runs` / `run_metrics` | run 状态 + 总量（llm_calls / jev_calls / tool_calls / provider_failures / tokens / duration_ms） |
| `run_progress` / `run_stages` | 前端轮询用的阶段进度 |
| `plans` / `plan_items` | 最终行程（`plan_json` + `plan_md` 全文，及逐条 `plan_items`；路线耗时、营业时间等最终落到这里） |
| `decisions` | 每个节点的取舍决策（选哪趟车、排除哪个点、为什么） |
| `provider_calls` | **观测账本**：谁在几点调了哪个 Provider、成不成、多快。与 `sources` 的分工是：`sources` 是"用了什么证据"，`provider_calls` 是"调了几次、多快"；且它能记录**还没有 run** 的 Discovery 调用（用 `session_id`） |
| `trace_spans` | 结构化 Trace：`component` ∈ workflow/provider/mcp/tool/llm/jev/planner/store …，`attributes_json` 里带复用标记（§7） |
| `planning_sessions` | 引导式会话（含 `prefetch_json`） |
| `city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta` | **目的地级共享表**（第 ④ 层复用的落点）：跨用户、跨 session，按城市分片；与 `places` 的分工是"`places` 管单 run 的行程内引用，城市表管'这座城市有什么、攻略写过什么'" |
| `canonical_places` | **跨 run 的现实地点实体**（第 ⑤ 层的落点）。与 run 级 `places` 刻意分成两张表：那是"某一次 run 的证据快照"（主键带 `run_id`，永不跨 run 复用），这是"现实里那个地点"。把实体身份塞进 `places` 等于让一次 run 的去重结论改写所有历史 run 的证据链 |
| `place_aliases` | 别名索引，主键 `(city, normalized_alias)` —— 一座城市里一个归一化别名只能指向一个实体（解析名字时的第一站） |
| `place_provider_refs` | `高德 poi_id → canonical_place_id` 的映射，主键 `(provider, provider_place_id)`。**高德 ID 只是证据，不是身份**：一个实体可以挂多个高德 POI（主 POI + 子 POI + 旧 ID） |
| `place_relations` | 主点 / 子点关系（`entrance` / `inside` / `child`）：`熊猫基地` 与 `熊猫基地南门` 是两个实体 + 一条边，不是粗暴合成一个 |
| `place_evidence_links` | 证据 → 实体的挂载（用城市级 `evidence_key`，跨会话稳定）。表名带 `_links` 是为了不与 run 级 `place_evidence` 撞名 |
| `poi_query_cache` | `"这个 query 以前查到了什么"`（**不是**"这个 query 是谁"）。命中只提供历史候选，实体仍要过一遍 Resolver |
| `place_resolver_traces` | 每次"复用 / 新建 / 合并 / 折叠 / 丢弃"的依据，用来回答"这次为什么没有重新查高德" |

---

## 4. 社媒攻略：什么时候查、什么时候不查

### 4.1 检索词怎么来

1. Discovery 阶段，`discovery.discover_social_evidence` 调 **一次** LLM
   （`RESEARCH_QUERY_EXPANSION_PROMPT`，`tag=query_expansion`）把意图扩写成 8–16 条检索词；
   模型不可用时回退到规则兜底词（`_fallback_queries`：`"{目的地} 攻略"`、`"{目的地} 必去"`，
   外加 `"{目的地} {偏好词}"`，总数截到 `SOCIAL_QUERY_LIMIT + WEB_QUERY_LIMIT`）。
2. 这批查询词**存进 PrefetchBundle**（`social_queries` / `social_served_queries`），
   后续阶段**不再问模型一遍**（旧固定流程按差集只补搜没搜过的 query）；同时也落进
   `city_cache_meta`，所以**下一个会话**命中城市知识库时同样不必重问。

### 4.2 取数链（每条检索词）

```
search_xiaohongshu(kw)
  ├─ TikHub（主源，tikhub_social 插件，search_xiaohongshu）
  │     └─ OK 有内容 → 用
  │     └─ 状态 ∈ {UNAVAILABLE,TIMEOUT,AUTH_ERROR,RATE_LIMIT,FREE_CREDIT_EXHAUSTED,INVALID_RESPONSE}
  │           → fallback 到 MediaCrawler（search_xhs_via_mediacrawler）
  │     └─ EMPTY / 其它 → 这就是结论，不换源（避免把噪声混进来）
  └─ 小红书整条线没拿到任何证据 → 兜底一次抖音（`search_douyin("{城市} 旅游")`）
```

联网搜索是**独立一条**：

```
web_search(q) → Tavily（super_harness 的 web_search 工具）→ Evidence(source_type="web", provider="tavily")
```

- 数量上限：社媒最多 `SOCIAL_QUERY_LIMIT=3` 个 query，联网最多 `WEB_QUERY_LIMIT=2` 个，证据总量封顶 `EVIDENCE_MAX=40`。
- 每条调用都过 ② 账本 `ledger.fetch`：**同一次 run 内同一个 query 不会打第二次**。

### 4.3 社媒返回 → Evidence → 存哪

返回条目经 `_evidences_from_social` 归一成 `Evidence`（title + text + author + published_at
+ `raw_metrics`{likes/comments/...}）。存两处：

1. **正式 Run**：`store.save_evidence(run_id, evidence, source_id)` → `evidence` 表；
   调用记录同时落 `sources` 表和 `provider_calls` 表。
2. **Discovery**：证据**只放在内存的 PrefetchBundle**里（此时还没有 run_id），
   调用记录写 `provider_calls` 表并用 `session_id` 关联。

### 4.4 复用：正式 Run 怎么读

**Agent 路径（当前主路径）**：预取的证据以**摘要**形式进入 Agent 的 user prompt
（`app/agent_runner.py::_prefetch_digest` 给出前 N 条的标题/来源/时间等关键字段），
Agent 自己决定是否直接用、要不要用 `search_xiaohongshu` / `search_douyin` / `web_search`
再补。跨会话命中城市知识库时，正文会先被 `_materialize_city_evidence` 换成本次 run 的
source_id / evidence_id（见 §2.1），否则证据链会指向上一轮 run。

**旧固定流程（已退役）**：`node_search_social` 的开头就是复用判断，优先级从高到低：

1. `bundle.evidences` 非空 → **直接返回这包证据**，一个 Provider 都不打
   （游记正文不会因为用户改偏好而变）。**命中城市知识库时走的就是这一条**：
   `_prefetch_with_city_cache` 把 `city_evidences` 里的正文装进 bundle；
2. `bundle.evidences` 为空、但 `bundle.social_queries` 在 → 复用检索词（**不再调
   query_expansion**），然后用 `bundle.missing_social_queries(queries)` 算出差集，
   **只补搜 Discovery 没搜过的那些 query**（已搜过的不再打）；
3. 都没有 → 走完整的 4.1 + 4.2 流程。

读到的数据结构就是 `Evidence` 模型（字段同 §3 的 evidence 表）。证据在后续只被
**读**——用于地点抽取（§5）与 Trust / Ad Risk 打分（§6）。

---

## 5. 高德数据：POI 搜索 / 详情 / 路线

### 5.1 搜索关键词怎么来（地点推荐的第一批原料）

`node_extract_places`（正式 Run）/ `discovery.extract_place_candidates`（Discovery）里，
POI 检索词 = 三份来源**合并去重**：

1. **证据自带的 `place_mentions`**：攻略 Provider 如果能直接给出地名，零成本直接用；
2. **LLM 抽取**：对没有 mention 且正文 ≥40 字的证据，用批量抽取（`EXTRACT_PLACES_BATCH_PROMPT`）
   抽 `{name, category, tone, quote, ...}`；多篇合成一批（`EXTRACT_BATCH_SIZE`）、批间受控并发，
   批超时按单篇重试一次（`discovery.extract_places_from_evidences`，`tag=extract_places_retry`）；
3. **关键词兜底**：`DEFAULT_POI_KEYWORDS`（景点/博物馆/公园/步行街/古镇）+ 用户偏好映射
   （"拍照"→观景台、"亲子"→乐园…），保证必去景点不会因为攻略没写就缺席。

上限：guidance 抽取最多 `EXTRACT_EVIDENCE_LIMIT=4` 条、每篇正文截 `EXTRACT_TEXT_CHARS=1200` 字；
原始候选去重后截 `DISCOVERY_MAX_PLACES=20`；兜底关键词最多 `POI_QUERY_LIMIT=8`。

### 5.2 关键词 → Place（`hub.search_poi`）

```
search_poi(keyword, region) → amap_cn 插件 search_poi → Place.from_poi(...)
```

返回的 `Place` 已带 `place_id`（高德唯一）、坐标、行政区、`amap_verified=True`。
**每个候选都必须在高德查得到**——查不到就不进候选，绝不用模型编一个地点。

### 5.3 去重与详情核实

1. `planner.dedupe_places` 按名称相似度做并查集去重（归一化在 Python 里做，不调模型）。
2. **Discovery**：先把去重后的候选截到 `USER_VISIBLE_POI_LIMIT=20`（用户可见上限），
   再取前 `DISCOVERY_POI_VERIFY_LIMIT=20` 个调 `hub.poi_detail(poi_id)`
   补营业时间/地址/商圈/行政区（`_apply_poi_detail`）——**这是两个独立上限**，不是一个。
3. **正式 Run —— 旧固定流程**（`node_verify_poi_and_routes`）：
   - `_deep_verify_places` 只挑「MUST/WANT（无条件）+ 高分 NEUTRAL（到 `PLANNER_POI_LIMIT=15`）」，
     **REJECT 直接出局**（用户排除的点一个 Provider 都不花）；
   - 补详情只补「缺营业时间 **且 `amap_verified`** **且 Discovery 没查过这个 poi_id 的**」（判据是过户账本
     `_adopted_poi_detail_ids`，不是"营业时间为空"——高德对部分 POI 本来就不返回营业时间），
     且单次 run 最多 `POI_DETAIL_LIMIT=6` 个；
   - 详情调用受 `POI_VERIFY_MAX_CONCURRENCY=4` 限制，且过 `_bounded_provider_call` 兜底超时。

   **Agent 路径**：没有"深度候选"这一步 —— Agent 看到预取摘要里的候选后，自己决定给哪些点调
   `poi_detail` / `route`；上限由它自己的步数预算与 prompt 里的取数纪律约束。

### 5.4 路线（`hub.route`）

- **只有正式 Run 查路线，Discovery 一个都不查**（这是设计边界：路线的配对依赖
  "用户选了哪些点 + 选了哪家酒店"，此时才知道）。
- 旧固定流程：`order_by_proximity(deep_places, hotel_coords)` 排序 → 只查**相邻一段**，
  **不做 N×N 全连接**；直线距离 > `ROUTE_MAX_LEG_METERS=60km` 的腿判"非相邻"跳过；
  总用量 ≤ `MAX_ROUTE_LOOKUPS=14`，并发 ≤ `ROUTE_MAX_CONCURRENCY=4`；
  同 run 内同一对坐标靠账本只打一次。
- Agent 路径：Agent 自己按需调 `geocode` / `route`，prompt 要求"排一天行程之前先算清
  上一站到下一站的路上时间"，同一对坐标的重复调用由 ProviderHub 的内存缓存兜住。
- 结果 `RouteOption.duration_minutes` 用于路上时间与时间可行性，最终随行程写进
  `plans.plan_json` / `plan_items`（没有单独的 routes 表；调用记录在 sources / provider_calls / trace）。

### 5.5 复用：正式 Run 怎么"不再重查高德"

**Agent 路径**：预取摘要里带 `place_id` / 名称 / 类型 / 区域 / `amap_verified` 与用户对该点的选择，
Agent 可以直接采用（不再 `search_poi`），也可以换关键词补查或给某个点调 `poi_detail`。

**旧固定流程**：`node_extract_places` 的复用判断是

1. `bundle.places` 非空 → **直接用 Discovery 查好的候选**，套一遍用户的
   MUST/WANT/REJECT（`apply_user_place_preferences`），**一次高德 `search_poi` 都不打**。
2. 详情复用在 5.3（按 poi_id 判"是否查过"）。
3. 路线不跨阶段复用（Discovery 没查过），只在同 run 内靠账本去重。

---

## 6. PrefetchBundle：跨阶段复用的载体

Discovery 结束（或每完成一条线）就落一次盘，存进 `planning_sessions.prefetch_json`
（JSON 文本，不是多张表）。`PrefetchBundle.dump()` 序列化、`load()` 反序列化。
字段（`app/discovery.py`）：

| 字段 | 类型 | 谁用 | 说明 |
| --- | --- | --- | --- |
| `outbound` / `inbound` | `list[TrainOption/FlightOption]` | 大交通 | 逐方向复用（旧流程：只跑去程时回程**会补查**） |
| `hotels` | `list[HotelOption]` | 住宿 | |
| `evidences` | `list[Evidence]` | 攻略 | 摘要进 Agent prompt；旧流程非空则整包复用（§4.4） |
| `places` | `list[Place]` | 地点候选 | 摘要进 Agent prompt；旧流程非空则整包复用（§5.5） |
| `social_queries` | `list[str]` | 攻略检索词 | 复用检索词，跳过 query_expansion |
| `social_served_queries` | `list[str]` | 攻略检索词 | 算差集，只补搜没搜过的 |
| `provider_calls` | `list[dict]` | 过户 | Discovery 期间的调用账本（→ sources） |
| `discovery` | `dict[stage→status]` | 判定"某条线跑完没" | RUNNING/PENDING 的线不视为可复用 |
| `hotel_areas` | `list[dict]` | 住宿区域推荐 | 攻略推出来的候选住宿区域（含 `key` / `name` / `reason` / `tags` / `fit_score`） |
| `poi_pools` | `dict[str, list]` | 探索页候选 | 按池拆分的候选：`attraction` / `food` / `experience` |
| `profile` | `dict` | 住宿区域排序 / 会话视图 | Dynamic Preference Profile（五组动态权重 + `travel_style` + `pace`） |
| `city_cache` | `dict` | 前端新鲜度 | 第 ④ 层的新鲜度：`source` ∈ `live`/`city_cache`、`updated_at`、`stale` |

> `hotel_areas` / `poi_pools` / `profile` / `city_cache` 都是**跨版本 JSON 字段**，
> `dump()` / `load()` 必须逐个显式保留；不在 `known` 集合里的新字段由 `extras` 原样过户，
> 不得在读写过程中丢掉。

**跨阶段调用的"过户"**：`_adopt_prefetch_sources` 把 Discovery 的 `provider_calls`
以**原 `source_id`** 写进正式 Run 的 `sources` 表（这样 Evidence 引用的 source 不会断链）。
Agent 路径同样要过户：`execute_agent_run` 在拉起循环之前调用它，
所以本次 run 的 `sources` 里能看出"这条数据是复用的"。

---

## 7. "什么时候查 / 什么时候不查"一键决策表

> 下表的"复用条件"描述的是**旧固定流程**的逐节点判断。Agent 路径下，"是否复用"由 Agent
> 根据 prompt 里的预取摘要决定：它可以直接采用摘要里的候选，也可以用工具再查一遍 ——
> 因此这张表在 Agent 路径上应读作"预取里有什么可用"，而不是"代码保证不会重查"。
> 城市知识库那一行（跨会话复用）两条路径完全一致。

| 数据 | Discovery 查吗 | Run 复用条件 | Run 不查的条件 | 不查时读什么 |
| --- | --- | --- | --- | --- |
| 攻略 + 高德 POI（跨会话） | 仅在第 ④ 层**未命中/无缓存**时查 | — | `read_candidates()` 命中城市知识库 | `city_pois`（POI）+ `city_evidences`（正文）+ `city_cache_meta`（检索词）（`stale` 时同时后台刷新） |
| 车次/航班 | ✅ | 对应方向 `bundle.outbound/inbound` 非空 | 方向被复用 | bundle 里那批 option |
| 酒店 | ✅ | `bundle.hotels` 非空 | 复用 | bundle.hotels |
| 攻略证据 | ✅ | `bundle.evidences` 非空 | 复用 | bundle.evidences |
| 检索词 | ✅（1 次 LLM） | `bundle.social_queries` 非空 | 复用检索词 | bundle.social_queries + served 差集 |
| POI 搜索 | ✅ | `bundle.places` 非空 | 复用 | bundle.places |
| POI 详情 | ✅（前 20） | — | 该 poi_id 已被 Discovery 查过 **或** 已有营业时间 **或** 非深度候选 | 就地用 place 字段 |
| 路线 | ❌（设计边界） | — | 同 run 内同一对坐标已查过（账本） | `ledger._values` |
| 门票 | ❌ | — | 非入选候选 / 超过 `TICKET_LOOKUP_LIMIT=3` | — |

**同一 run 内去重**（账本 ②）覆盖大多数 Provider：`query_key(provider, tool, origin, destination, date, query, page, extra)`
一旦 `remember` 过，后续同键直接 `cache_hit`，不再出网。

**但账本不是全覆盖**，以下三处不走账本 `fetch`（因此不受同 run 去重保护）：

- **门票**（`node_verify_poi_and_routes` 里的门票查询）：只按 `TICKET_LOOKUP_LIMIT` 限次，不做键去重；
- **酒店候选为空时的联网兜底**：走的是"补一次搜索"，不是可记忆的查询键；
- **车站 ↔ 酒店接驳的 `geocode` / `route`**：经 `_bounded_provider_call` 只留下超时/降级标记，不做账本去重。

---

## 8. Trace / 审计里怎么看"这次是复用还是新查"

**Agent 路径**：审计依据是 `outputs/<run_id>/audit_report.json` 的 `provider_calls` 段
（ProviderHub 的调用账本，逐条记录这次真的出网了哪一次）与 `plan.sources[]`；
复用 Discovery 的调用会以原 source_id 出现在 `sources` 里。

**旧固定流程**：`trace_spans`（`component="tool"`）每条子 span 的 `attributes_json` 里有这些布尔标记：

| 字段 | 含义 |
| --- | --- |
| `cache_hit` | 命中了同 run 内账本（②） |
| `prefetch_reused` | 复用了 Discovery 的结果（③） |
| `provider_called` | 本次真的发了 Provider 请求（`not (cache_hit or prefetch_reused)`） |
| `fallback_query` | 主源不可用后走的备用源 |
| `timeout` | 这次调用打满预算被判超时 |
| `hedge_discarded` | 对冲里被弃用的一次调用（真实发生，但不是结果来源） |

`provider_calls` 表和 `sources.raw_json`（截断到 6 万字符）有同样的信息量级，供 Provider
健康页 / 管理端 / 复盘用。`audit_report.json` 的 `provider_calls` 段把这些聚合起来。

---

## 9. POI 推荐是怎么生成的（从候选到行程）

Discovery 侧（两条路径共用，步骤 ①~④）把候选收敛成"用户能看的一屏"：

```
① 候选来源：证据 place_mentions ∪ LLM 抽取 ∪ 关键词兜底  ──► 关键词表
   （抽出的地名会写回 evidence.place_mentions —— city_poi_mentions 就是按它建行的）
② plan_queries：别名 / 查询缓存命中的词**不打高德**，候选直接从实体层取（§2.2）
③ search_poi（高德）──────────► Place（amap_verified=True，必须真实存在）
④ resolve_pois（实体层收敛）──► 地理围栏 + 合并重复 + 折叠子设施 + 丢弃非目的地
   （用户可见 ≤20，按"攻略提到过的在前"截断；Discovery 对缺详情的前 20 补详情）
⑤ 用户选择：MUST / WANT / REJECT（在探索页点选，作为结构化事实进 Agent prompt）
```

进入最终行程分两条路：

```
【Agent 路径（当前主路径）】
⑥ Agent 拿到预取摘要 + 用户选择 → 自己决定给哪些点调 poi_detail / route、要不要换词再搜
⑦ 排逐日行程 → 自查（时间算上路上时间、返程来得及、预算内、REJECT 不出现）→ submit_final_plan
⑧ Python 兜底：时间冲突验证（app/planner.py::apply_feasibility_pass）
⑨ 落库与成文：plan.json / plan.md / audit / badcases（数字一律照抄，标注"估算/未验证"）

【旧固定流程（已退役）】
⑥ _deep_verify_places：MUST/WANT + 高分 NEUTRAL（≤15）──► 补详情 + 相邻路线
⑦ score_candidates：trust_score（多来源证据覆盖）+ Ad Risk（六项分项）
⑧ build_plan：按候选分 + 偏好匹配 + 节奏，聚成每天行程（Cluster/Nearest 拓扑）
⑨ check_feasibility：营业时间 / 交通耗时逐项校验，修订或降级
⑩ finalize：写成 plan.json / plan.md
```

所以"POI 推荐"**不是模型凭空生成的**：候选必须能被高德查到，路线与营业时间只能用真实查到的、
查不到就按"未验证"披露（Agent 路径由 prompt 要求如实标注）。旧流程还会有
"证据支撑 + 广告风险 + 用户偏好"的候选打分与 MUST 加分 / REJECT 硬排除。

第 ④ 步的取代关系要说明白：`planner.dedupe_places` **仍是 run 内去重的实现**（有独立单测），
但正式流程不再在它之后再跑一次 —— 实体层已经做了更严的收敛（子设施、连锁分店、跨会话引用），
在它后面再跑一遍 run 内去重会把实体层刻意拆开的分店重新合并。

---

## 10. 一处刻意没有复用、以及为什么

**路线（route）没有跨阶段复用**：`app/discovery.py` 里一个 `hub.route` 都没有。
原因：路线的配对集合依赖"用户最终选了哪些点（MUST/WANT/REJECT）"和"最终选了哪家酒店"，
这两件事在 Discovery 时还不知道。提前查路线 = 猜测用户会怎么选，命中率没有保证，
反而多打一批高德。所以路线只在同 run 内去重（ProviderHub 内存缓存），不做跨阶段预取。
如果未来要把这块也前移，需要先在 Discovery 阶段确定"默认排序 + 默认酒店"作为预取假设，
再按命中率决定值不值得。

---

## 11. 现在**还没有**复用到的部分（如实列出）

攻略正文与检索词已经进城市知识库（§2.1），地点身份与跨会话查询已经进实体层（§2.2），
下面这些仍然是每次重查或不去重：

| 项 | 现状 | 为什么先不做 |
| --- | --- | --- |
| 路线（route） | 只在同 run 内去重（ProviderHub 内存缓存），不跨阶段预取 | 见 §10：配对集合依赖用户最终选择 |
| 门票（ticket） | 旧流程只按 `TICKET_LOOKUP_LIMIT` 限次、不走去重账本；Agent 路径由 Agent 按需调用 | 价格实时、且只查可能入选的点，重复率低 |
| POI 详情 | 实体层带地址 + 营业时间的点已经**不再补查**（`PlaceResolver.needs_detail`，§2.2）；缺其中任一项的仍可补查一次 | 只按"两个字段都齐"才算够，宁可多查一条也不让行程缺营业时间 |
| 原始响应 `raw_json` | 城市缓存只存归一化后的字段，不存 Provider 原始报文 | 体积大；`sources.raw_json` 本身也截断到 6 万字符，存两份没有收益 |
| 车站/机场 ↔ 酒店接驳 | 旧流程走 `_bounded_provider_call`，只留超时标记，不去重；Agent 路径由 Agent 自己调 `geocode` / `route` | 它是"路线"的一种，理由同上 |