# TravelPlan 产品需求文档（PRD）

> 版本：V1.0  
> 日期：2026-09-18  
> 面向：中国国内旅行  
> 项目性质：个人学习 / 作品项目，不商用  
> Agent Runtime：SuperHarness  
> 主仓库：`Sitozzmonash/travelplan`  
> Runtime 仓库：`Sitozzmonash/super_harness`（Git Submodule）

---

> ⚠️ **这是设计期文档（成书 2026-09-18），不是当前实现的说明书。**
> **代码注释里有 182 处 `PRD §x` 引用它，所以不要删。** 但下列内容已与实现不符，
> 读到时请以代码 + 当前文档为准（入口见 [docs/README](../README.md)）：
>
> - **全文没有 Guided / Planning Session / Agent Loop**——PRD 成书早于引导式入口与本次
>   Agent 化改造：Guided 现在是 Web 默认入口（见 [用户旅程](USER_JOURNEY.md)），
>   主规划已从"固定 12 步图"换成 SuperHarness Agent Loop
>   （见 [主规划：Agent Loop](../architecture/AGENT_LOOP.md)）。因此 **§25 的 12 步节点名不再是
>   规划的实现顺序**（节点名仍在 `app/workflow.py` 里，那是退役流程的代码）。
> - `§6 仓库结构` 的 `app/` 只列 8 个文件，实际有 30 个模块 + `app/decision/` 3 个；
>   `§28 输出文件` 写每次 run 3 件产物，实际 **6 件**（`app/api.py` 白名单）。
> - `§27 日志与可观测性` 的 `[TRAVEL]/[COMPARE]/[TRUST]` 之类的日志前缀在 `app/` 下 **0 命中**；
>   实际观测走 span/stage/步骤上报（`app/agent_trace.py` + `outputs/<run_id>/trace.jsonl`）。
> - `§10.1` 的途牛 Tool 名（`search_flights`/`search_hotels`…）实际全部带 `tuniu_` 前缀且有过改名；
>   `§13` 的 `get_social_detail` 工具**不存在**（详情路由因与 `FREE_ONLY` 冲突被刻意移除，实际是 `get_tikhub_quota`）。
> - `§14` 是 `mediacrawler_social`，**不是**部署契约章节。早期文档里把 health 契约字段写成
>   "PRD §14"是**误引用**（实际契约见 [部署说明](../operations/DEPLOYMENT.md) §2.4）。
>
> 仍然准确且被当作契约的部分：`§16` 核心数据模型（字段级 provenance）、
> `§18`/`§19` 的 Trust / Ad Risk 权重（当前只在旧固定流程里消费）、`§37` 外部接口事实参考。

---

# 1. 产品定义

TravelPlan 是一个基于真实旅行数据的国内旅行规划 Agent。

用户输入一段自然语言，例如：

```text
10月1日从长春去成都玩5天，两个人，预算6000，
喜欢美食和拍照，希望交通不要太折腾。
```

系统需要自动完成：

```text
查机票/高铁
→ 比时间和价格
→ 查酒店
→ 查景点门票
→ 搜小红书/抖音攻略
→ 高德验证地点
→ 过滤广告和低可信内容
→ 计算市内交通
→ 检查营业时间
→ 生成每日行程
→ 判断预算
→ 判断时间是否可行
→ 必要时修订
→ 输出真实价格、来源、查询时间和完整决策链
```

产品核心不是“生成文字”，而是：

> **真实检索 + 结构化比较 + 约束规划 + 可追溯推荐。**

---

# 2. 产品目标

V1 必须实现：

1. 支持中国大陆范围内城市旅行规划。
2. 支持单城市和跨城市出行。
3. 能查询真实机票。
4. 能查询真实火车 / 高铁信息。
5. 能查询真实酒店。
6. 能查询景点门票。
7. 支持途牛邮轮、度假 / 跟团 / 自助游等产品查询。
8. 能从小红书 / 抖音获取真实攻略信息。
9. 能通过高德验证 POI 和路线。
10. 能对餐厅 / 景点候选做可信度与广告风险判断。
11. 能用真实交通时间检查行程可行性。
12. 能计算预算。
13. 能输出可直接给前端消费的结构化 JSON。
14. 能输出人类可读 Markdown。
15. 能输出完整审计报告。
16. 同时保留 CLI 与 Web 前端。
17. 所有 Agent 能力基于 SuperHarness。

---

# 3. 非目标

V1 不做：

- 自动下机票订单
- 自动下酒店订单
- 自动买火车票
- 抢票
- 支付
- 退款
- 自动取消订单
- 商业化
- 海外旅行
- 海外地图
- 海外酒店/航班 Provider
- 旅行社后台
- 社交分享社区
- 用户支付体系

第三方 API 不允许产生现金付费。

---

# 4. 用户场景

## 4.1 基础旅行规划

```text
我10月1日从北京去成都，5天，2个人，
预算6000，喜欢美食、拍照、历史文化。
```

输出至少包括：

- 去程交通候选
- 回程交通候选
- 选择理由
- 酒店候选及价格
- 每天行程
- 每段交通时间
- 景点门票
- 餐厅推荐
- 预算汇总
- 信息来源

## 4.2 低预算

```text
上海去南京玩3天，两个人预算2000。
```

系统需要主动：

- 优先高铁而非明显更贵航班；
- 控制酒店成本；
- 避免收费高但价值低的景点；
- 明确预算余量。

## 4.3 时间冲突

如果：

```text
航班 12:10 落地
预计 13:00 出机场
机场→酒店 70 分钟
酒店入住 20 分钟
```

则系统不得安排：

```text
14:00 景点入场
```

必须检测并修订。

## 4.4 攻略可信度

如果某餐厅：

- 仅出现于一条明显商业探店笔记；
- 大量模板化夸张词；
- 没有具体菜品；
- 缺少其他来源；

则应降低 Trust、提高 Ad Risk，不应因为“网红”就进入最终计划。

---

# 5. 总体架构

```text
Frontend / CLI
      │
      ↓
TravelPlan app
      │
      ├── workflow.py       固定旅行规划流程
      ├── planner.py        旅行领域算法
      ├── models.py         结构化数据
      ├── store.py          Evidence / Decision / Plan
      │
      ↓
SuperHarness
      │
      ├── Capability Router
      ├── Plugin Router
      ├── MCP Router
      ├── Memory
      ├── RAG / Retrieval
      ├── Retry
      ├── Observability
      └── Trace
      │
      ├── tuniu_travel Plugin
      ├── amap_cn Plugin
      ├── tikhub_social Plugin
      ├── mediacrawler_social Plugin
      ├── railway_12306 MCP
      ├── web_search (Tavily，框架已有)
      └── http_fetch (框架已有)
```

---

# 6. 仓库结构

```text
travelplan/
├─ super_harness/
│  └─ superharness/
│     └─ capabilities/
│        ├─ plugins/
│        │  ├─ tuniu_travel/
│        │  ├─ amap_cn/
│        │  ├─ tikhub_social/
│        │  └─ mediacrawler_social/
│        └─ mcp/
│           └─ railway_12306.py
├─ app/
│  ├─ __init__.py
│  ├─ agent.py
│  ├─ api.py
│  ├─ workflow.py
│  ├─ models.py
│  ├─ planner.py
│  ├─ store.py
│  └─ prompts.py
├─ frontend/
├─ tests/
├─ docs/
├─ outputs/
├─ main.py
├─ .env
├─ requirements.txt
└─ README.md
```

`super_harness/` 是 Git Submodule，里面的修改单独提交至 `super_harness` 仓库。

---

# 7. SuperHarness 复用边界

必须直接复用：

- `create_harness_agent`
- `create_harness_app`
- `HarnessContext`
- `WorkflowSpec`
- `CapabilityRouter`
- `PluginLoader`
- `MCPLoader`
- `MCPServerSpec`
- `RetrievalManager`
- `MemoryManager`
- `ObservabilityMiddleware`
- `TraceCollector`
- Tool Retry / Model Retry
- Tool Auto Discovery
- Plugin Lazy Loading
- `web_search`
- `http_fetch`

TravelPlan 不重复实现这些基础设施。

---

# 8. 环境变量

根目录 `.env`：

```env
MODEL_NAME=
MODEL_BASE_URL=
MODEL_API_KEY=

TAVILY_API_KEY=

AMAP_API_KEY=
TIKHUB_API_TOKEN=
TUNIU_API_KEY=
```

说明：

| ENV | 用途 | 必须 |
|---|---|---:|
| MODEL_NAME | LLM 模型 | 是 |
| MODEL_BASE_URL | OpenAI-compatible Base URL | 是 |
| MODEL_API_KEY | LLM API Key | 是 |
| TAVILY_API_KEY | SuperHarness `web_search` | 建议 |
| AMAP_API_KEY | 高德 Web Service | 是 |
| TIKHUB_API_TOKEN | 小红书/抖音 | 是，但仅免费额度 |
| TUNIU_API_KEY | 途牛 CLI | 是 |

不新增 Secret 到源码。

---

# 9. Provider 与优先级

| 场景 | 主 Provider | fallback |
|---|---|---|
| 火车 / 高铁 | 12306 MCP | 途牛 Train |
| 机票 | 途牛 | 无 |
| 酒店 | 途牛 | 无 |
| 景点门票 | 途牛 | 无 |
| 邮轮 | 途牛 | 无 |
| 度假/旅游产品 | 途牛 | 无 |
| 小红书攻略 | TikHub 免费额度 | MediaCrawler |
| 抖音攻略 | TikHub 免费额度 | MediaCrawler |
| POI | 高德 | 无 |
| 路线 | 高德 | 无 |
| 通用网页 | SuperHarness Tavily | `http_fetch` 已知 URL |

Provider 失败时不得伪造结果。

---

# 10. 通用能力设计

## 10.1 `tuniu_travel` Plugin

位置：

```text
super_harness/superharness/capabilities/plugins/tuniu_travel/
├─ plugin.toml
└─ tools/
   ├─ flight.py
   ├─ hotel.py
   ├─ train.py
   ├─ ticket.py
   ├─ cruise.py
   └─ holiday.py
```

Plugin 描述必须覆盖：

```text
国内机票、酒店、火车票、景点门票、邮轮、度假旅游产品查询
```

### 认证

```env
TUNIU_API_KEY=
```

途牛 CLI 读取该环境变量。

安装：

```powershell
npm install -g tuniu-cli@latest
```

自检：

```powershell
tuniu list
tuniu list flight
tuniu list hotel
```

### V1 暴露 Tool

#### `search_flights`

底层：

```text
tuniu call flight searchLowestPriceFlight
```

输入：

```json
{
  "departure_city": "北京",
  "arrival_city": "成都",
  "departure_date": "2026-10-01"
}
```

输出必须归一化为：

```json
{
  "provider": "tuniu",
  "fetched_at": "...",
  "items": [
    {
      "flight_no": "...",
      "departure_airport": "...",
      "arrival_airport": "...",
      "departure_time": "...",
      "arrival_time": "...",
      "duration_minutes": 0,
      "price": 0,
      "currency": "CNY",
      "raw": {}
    }
  ]
}
```

#### `search_hotels`

底层：

```text
tuniu call hotel tuniu_hotel_search
```

至少支持：

- city
- check_in
- check_out
- keyword
- price range

输出：

- hotel_id
- name
- address
- rating（若 Provider 返回）
- price
- price_unit
- room info（若返回）
- source
- fetched_at

#### `search_tuniu_trains`

只作为 12306 fallback。

底层：

```text
tuniu call train searchLowestPriceTrain
```

#### `search_attraction_tickets`

底层：

```text
tuniu call ticket query_cheapest_tickets
```

#### `search_cruises`

底层：

```text
tuniu call cruise searchCruiseList
```

#### `search_tour_products`

底层：

```text
tuniu call holiday searchHolidayList
```

注意：途牛度假产品列表中的部分价格属于“起价”，最终 UI 必须显示：

```text
¥xxx 起
```

不能当成最终确定价。

### V1 禁止暴露

任何：

```text
saveOrder
bookTrain
create_order
cancelOrder
saveCruiseOrder
saveHolidayOrder
```

即使 CLI 可用，也不要进入 `TOOLS`。

---

# 11. `railway_12306` MCP

位置：

```text
super_harness/superharness/capabilities/mcp/railway_12306.py
```

使用 SuperHarness 现有：

```python
MCPServerSpec
MCPLoader
```

不要自己写 MCP Runtime。

推荐 stdio：

```text
command: npx
args: ["-y", "12306-mcp"]
```

Windows 若 `npx` 无法直接解析，可兼容 `npx.cmd`。

最低要求：

```text
Node.js >= 18
```

12306 MCP 应覆盖：

- 车票搜索
- 直达
- 中转
- 出发时间
- 到达时间
- 历时
- 席别
- 票价
- 余票
- 经停站（能力可用时）

TravelPlan 策略：

```text
调用 12306
→ 成功且有可用结果 → 使用
→ MCP 不可用 / 查询异常 → 调用途牛 Train
```

fallback 必须写入 audit：

```json
{
  "primary": "12306",
  "fallback": "tuniu",
  "reason": "..."
}
```

---

# 12. `amap_cn` Plugin

位置：

```text
super_harness/superharness/capabilities/plugins/amap_cn/
```

认证：

```env
AMAP_API_KEY=
```

高德 Web Service Key 只在 Python 后端使用，不允许传给浏览器。

### Tools

#### `search_poi`

高德 POI 2.0。

支持：

- keywords
- region
- types
- page
- page_size

输出至少：

- poi_id
- name
- address
- type
- longitude
- latitude
- city
- district

#### `get_poi_detail`

通过 POI ID 获取详情。

若高德返回：

- business area
- tel
- opening_hours
- photos
- rating

则保留；若不返回不能补造。

#### `geocode`

名称 / 地址 → 坐标。

#### `route`

统一接口：

```python
route(origin, destination, mode, city=None)
```

`mode`：

```text
driving
transit
walking
```

返回：

```json
{
  "distance_meters": 0,
  "duration_seconds": 0,
  "estimated_cost": null,
  "mode": "transit",
  "provider": "amap",
  "fetched_at": "..."
}
```

路线规划必须以高德结果作为时间可行性主要事实来源。

---

# 13. `tikhub_social` Plugin

位置：

```text
super_harness/superharness/capabilities/plugins/tikhub_social/
```

认证：

```env
TIKHUB_API_TOKEN=
```

Base：

```text
https://api.tikhub.io
```

Header：

```text
Authorization: Bearer <token>
```

### V1 Tools

```text
search_xiaohongshu
search_douyin
get_social_detail（若对应接口稳定且免费额度允许）
```

### 小红书搜索

必须支持至少：

- keyword
- page
- sort
- time filter

对旅行场景建议自动扩展 query：

```text
成都 美食
成都 本地人 美食
成都 避坑
成都 拍照
成都 三天攻略
```

### TikHub 免费模式

这是 V1 硬约束。

TikHub 官方是按请求计费平台，但可通过签到获得 `free_credit`。

项目必须实现：

```text
FREE_ONLY = True
```

请求前：

1. 调 `get_user_info`。
2. 读取：
   - `balance`
   - `free_credit`
3. 若 `free_credit <= 0` → 不调用付费内容接口。
4. 若 `balance > 0` 且无法保证先扣 free_credit → SAFE MODE 下拒绝调用。
5. 若接口返回 402 → fallback MediaCrawler。
6. 不提供充值工具。
7. 不允许自动关闭 FREE_ONLY。
8. 日志不得输出 Token。

建议开发/验收账户：

```text
balance = 0
free_credit > 0
```

这样最容易保证不会消耗现金余额。

---

# 14. `mediacrawler_social` Plugin

定位：

> 开发 / 免费 fallback，不是商业生产 Provider。

来源项目：

```text
NanmiCoder/MediaCrawler
```

支持：

- 小红书
- 抖音
- B站
- 微博
- 知乎等

TravelPlan V1 主要用：

```text
xhs
dy
```

不把 MediaCrawler 源码提交到 SuperHarness。

推荐本地准备：

```text
travelplan/data/vendors/MediaCrawler/
```

该目录加入 `.gitignore`。

Plugin 负责：

- 检查本地 MediaCrawler 是否存在；
- 调用 CLI / Python 入口；
- 读取 JSON / JSONL 结果；
- 归一化为 TravelContent；
- 未登录或风控失败时明确返回 unavailable。

不允许在 fallback 失败后伪造社交攻略。

---

# 15. Tavily

SuperHarness 当前已有：

```text
superharness/capabilities/tools/web_search.py
```

后端就是 Tavily，使用：

```env
TAVILY_API_KEY=
```

因此 TravelPlan 不再新增 Plugin。

用途：

- 搜索景区官网
- 查临时关闭公告
- 查询节假日政策
- 查询攻略网页补充
- 交叉验证明显冲突的信息

不能作为实时机票、酒店、火车票替代数据源。

---

# 16. 核心数据模型

以下为业务层逻辑模型，具体字段可扩充，但核心 provenance 字段不能删。

## 16.1 `TripIntent`

```python
destination: list[str]
origin: str | None
start_date: date | None
end_date: date | None
days: int
travelers: int
budget_total: float | None
preferences: list[str]
pace: str
transport_preferences: list[str]
hotel_preferences: list[str]
constraints: list[str]
```

必须容忍 LLM 返回 null：

```text
preferences=null → []
pace=null → balanced
travelers=null → 1
days 字符串 → int
```

## 16.2 Provider 共同字段

所有实时结果必须包含：

```python
provider: str
source_id: str | None
source_url: str | None
fetched_at: datetime
raw: dict
```

## 16.3 FlightOption

- flight_no
- origin
- destination
- departure_airport
- arrival_airport
- departure_at
- arrival_at
- duration_minutes
- price
- currency
- provider
- fetched_at

## 16.4 TrainOption

- train_no
- origin_station
- destination_station
- departure_at
- arrival_at
- duration_minutes
- seats
- price_range
- provider
- fetched_at

## 16.5 HotelOption

- hotel_id
- name
- address
- lat/lng
- check_in
- check_out
- room_type
- price_per_night
- total_price
- rating
- cancellation_policy（Provider 有才填）
- provider
- fetched_at

## 16.6 Place

- place_id
- name
- normalized_name
- aliases
- type
- lat/lng
- address
- city
- district
- opening_hours
- expected_duration_minutes

## 16.7 Evidence

```python
id
source_type
provider
source_url
title
author
published_at
fetched_at
text
place_mentions
specific_dishes
raw_metrics
```

## 16.8 Decision

```python
entity_id
status: KEEP | REJECT | SELECT | PASS | USED | NOT_USED
agent_or_stage
reason_codes
reason_text
scores
timestamp
```

---

# 17. Evidence Store

使用 TravelPlan 自己的 SQLite：

```text
data/travelplan.db
```

它只存旅行领域 Evidence 和 run，不与 SuperHarness Memory 混淆。

建议表：

```text
runs
trip_requests
sources
evidence
places
place_evidence
decisions
plans
plan_items
```

最低要求：

### `runs`

- run_id
- user_id
- created_at
- status
- original_query

### `sources`

- source_id
- run_id
- provider
- source_type
- source_url
- fetched_at
- raw_json / normalized_json

### `decisions`

- run_id
- stage
- entity_id
- decision
- reason_codes
- reason
- scores
- created_at

---

# 18. Trust Score

目标：

> 判断“这条攻略 / 这个地点为什么值得信”。

建议 0~100：

```text
25 多来源独立证据
20 评价/出现规模
20 具体体验细节
15 跨时间持续出现
10 本地人 / 重复访问线索
10 POI/官方信息一致
```

不要机械使用单一评分。

---

# 19. Ad Risk

0~100。

高风险：

- 大量类似文案
- 明确团购 / 商单
- 强营销关键词
- 没有具体体验
- 同期达人集中推荐
- 只有高情绪词
- 没有缺点
- 重复模板化素材

降低风险：

- 普通用户自然提及
- 明确具体菜品
- 同时描述优缺点
- “第二次/第三次来”
- “住附近经常吃”
- 多平台跨时间重复出现

最终筛选不是：

```text
Ad Risk 高 → 一定删除
```

而是结合：

```text
Trust + Ad Risk + 用户偏好 + 路线适配
```

---

# 20. POI 归一化

解决：

```text
宽窄巷子
宽窄巷子景区
成都宽窄巷子
```

归为同一 Place。

优先：

1. 高德 POI ID
2. 坐标距离
3. 标准化名称
4. 地址
5. Alias

不得仅靠字符串完全相等。

---

# 21. 行程规划算法

Planner 不应该纯 LLM。

## 21.1 基本流程

```text
候选地点
→ 高德坐标
→ 地理聚类
→ 每天主要区域
→ 时间窗
→ 路线耗时
→ 用户偏好
→ Trust / Ad Risk
→ 预算
→ 初始计划
→ feasibility
→ critic
→ 修订
```

## 21.2 每天规划原则

- 一个主要区域优先；
- 避免反复横跨城市；
- 午餐晚餐处在合理时间；
- 打卡拍照点考虑白天/夜景；
- 景点停留时长合理；
- 留交通缓冲；
- 最后一天必须考虑返程；
- 第一天必须考虑到达时间；
- 不要一天安排过满。

---

# 22. 时间可行性引擎

必须由代码判断，不由模型自由判断。

公式示意：

```text
previous_end
+ transfer_buffer
+ route_duration
+ arrival_buffer
<= next_start
```

需要考虑：

### 飞机

- 提前到机场安全 buffer
- 落地到离开机场 buffer
- 取行李
- 机场到酒店/景点

### 火车

- 提前到站 buffer
- 出站 buffer
- 火车站到下一地点

### 酒店

- check-in 时间
- 寄存行李（仅在有证据或标记为假设时）
- check-out

### 景点 / 餐厅

- opening hours
- expected stay
- last entry（如果能查到）
- meal time

### 高德

真实路线 duration 必须进入约束。

---

# 23. 预算引擎

预算分：

```text
交通
住宿
门票
餐饮估算
市内交通
其他
```

其中：

- 机票 / 火车 / 酒店 / 门票必须是真实实时数据；
- 餐饮若没有可靠价格，可标记为“估算”，必须与真实价格字段区分；
- 不允许把估算写成实时价格。

输出：

```json
{
  "budget_total": 6000,
  "known_real_cost": 4200,
  "estimated_cost": 900,
  "projected_total": 5100,
  "remaining": 900,
  "status": "within_budget"
}
```

---

# 24. 交通方案比较

不能只选最低价。

比较：

```text
价格
总耗时
出发时间
到达时间
机场/车站额外交通
是否影响第一天/最后一天行程
用户偏好
```

例如：

```text
飞机 ¥500，飞行 2h，但机场往返 3h
高铁 ¥450，4h，车站离酒店近
```

最终不应简单认为飞机一定更快。

---

# 25. Workflow

推荐固定 Workflow：

```text
parse_intent
↓
search_intercity_transport
↓
search_hotels
↓
search_social_guides
↓
extract_and_normalize_places
↓
verify_poi_and_routes
↓
score_candidates
↓
build_initial_plan
↓
check_budget
↓
check_feasibility
↓
critic_and_revise
↓
finalize
```

使用 SuperHarness：

```python
create_harness_app(workflows=[...])
```

不要自己实现 Workflow Router。

---

# 26. LLM 职责

LLM 适合：

- 意图解析
- Query Expansion
- 从攻略文本提地点 / 菜品 / 用户体验
- 解释推荐理由
- Critic 文本分析
- 最终自然语言表达

LLM 不适合：

- 生成机票价格
- 生成火车余票
- 猜酒店价格
- 猜 POI 坐标
- 猜路线时间
- 猜营业时间
- 做精确预算加法
- 做硬时间约束判断

这些必须由 Tool / Python 算法完成。

---

# 27. 日志与可观测性

SuperHarness 原生日志继续负责：

```text
RUN
ROUTER
RAG
LLM
TOOL
TOKEN
CACHE
TIMING
TRACE
```

TravelPlan Audit 负责业务决策。

示例：

```text
[TRAVEL] run=xxx stage=transport provider=12306 query=北京→成都 date=...
[TRAVEL] train_results=18
[TRAVEL] flight_results=12

[COMPARE] G89 price=840 duration=447m
[COMPARE] CAxxxx price=760 duration=185m airport_extra=150m

[DECISION] SELECT transport=CAxxxx reason="total door-to-door time lower"

[RESEARCH] source=tikhub platform=xhs query="成都 本地人 美食"
[RESEARCH] results=20

[TRUST] place="xxx火锅" trust=87 ad_risk=18 KEEP
[TRUST] place="xxx网红店" trust=42 ad_risk=82 REJECT

[FEASIBILITY] item="武侯祠 14:00" FAIL
[FEASIBILITY] reason="hotel arrival earliest=14:20"
[REVISION] start_time 14:00 → 15:00
```

---

# 28. 输出文件

每次 Run：

```text
outputs/<run_id>/
├─ plan.json
├─ plan.md
└─ audit_report.json
```

## `plan.json`

给前端。

必须包括：

```text
run_id
query
intent
transport
hotel
days
budget
warnings
sources
generated_at
```

每个 itinerary item：

```text
id
type
place_id
name
lat
lng
start_time
end_time
duration_minutes
travel_from_previous
price
price_type: realtime | estimated | unknown
trust_score
ad_risk
reason
evidence_ids
source_ids
```

## `plan.md`

人类阅读版本。

## `audit_report.json`

必须能回答：

```text
搜了什么？
调用了哪个 Provider？
Tool 参数是什么？
Tool 什么时候返回？
有哪些候选？
为什么保留？
为什么拒绝？
为什么最终用了这个？
哪些 Source 没被采用？
时间冲突如何修复？
预算如何计算？
```

---

# 29. CLI

运行：

```powershell
python main.py -m "我想从北京去成都玩5天..."
```

可选参数建议：

```text
--user-id
--thread-id
--output-dir
--debug
```

默认：

```text
project_id = travelplan
```

---

# 30. FastAPI

`app/api.py` 复用同一 Engine。

最低 API：

```http
POST /api/v1/plans
GET  /api/v1/plans/{run_id}
GET  /api/v1/plans/{run_id}/audit
GET  /api/v1/health
```

建议：

### POST `/api/v1/plans`

```json
{
  "message": "10月1日北京去成都5天，两个人预算6000"
}
```

返回：

```json
{
  "run_id": "...",
  "status": "completed",
  "plan": {}
}
```

若后续生成时间较长，可以升级成：

```text
POST → run_id
GET status
SSE / websocket progress
```

V1 不强制做异步任务队列。

---

# 31. Frontend

前端不是第二套业务逻辑。

```text
frontend
↓ API
FastAPI
↓
TravelPlan Workflow
↓
SuperHarness
```

前端不得：

- 保存 Provider Secret
- 直接请求途牛
- 直接请求 TikHub
- 直接请求高德 Web Service Secret
- 自己计算最终价格
- 自己生成旅行计划

详细见：

```text
docs/product/UI.md
```

---

# 32. 错误与 fallback

统一返回 Provider 状态：

```text
OK
EMPTY
UNAVAILABLE
AUTH_ERROR
RATE_LIMIT
FREE_CREDIT_EXHAUSTED
TIMEOUT
INVALID_RESPONSE
```

规则：

### 12306

```text
UNAVAILABLE/TIMEOUT
→ Tuniu Train
```

### TikHub

```text
FREE_CREDIT_EXHAUSTED / 402 / SAFE_MODE_BLOCK
→ MediaCrawler
```

### 途牛

失败：

```text
→ 明确告诉 Planner 当前实时数据不可用
```

不允许自动换携程 / 飞猪 / 去哪儿。

### 高德

失败：

- 不允许 LLM 猜 route duration；
- 对相关行程标记 `route_unverified=true`；
- Critic 应降低计划置信度。

---

# 33. 缓存

实时数据不要长期缓存。

建议：

```text
机票          5~10 分钟
火车票        2~5 分钟
酒店          10 分钟
门票          30 分钟
POI 基础信息   24 小时
路线          15~30 分钟
社交攻略       6~24 小时
```

最终 `fetched_at` 永远保留真实获取时间。

---

# 34. 测试

## 34.1 Unit

必须覆盖：

- TripIntent null 归一化
- Tuniu CLI 参数构造
- Tuniu 输出解析
- 12306 fallback
- AMap route 解析
- TikHub Free Only
- MediaCrawler unavailable
- Place normalize
- Trust Score
- Ad Risk
- Budget
- Feasibility
- 时间冲突修订

## 34.2 Contract Test

每个 Provider 保存经过脱敏的 fixture。

验证 Provider 返回变化时 parser 能及时失败，而不是静默出错。

## 34.3 Mock E2E

不调用真实 API：

```text
用户输入
→ workflow
→ mock provider
→ final plan
```

必须稳定通过 CI。

## 34.4 Real Smoke

手动 / 本机运行：

```text
北京→成都
上海→杭州
长春→成都
广州→重庆
```

记录实时调用是否成功。

---

# 35. 验收标准

## A. 框架

- [ ] `super_harness` 是 Git Submodule
- [ ] TravelPlan 未复制 SuperHarness Runtime
- [ ] Plugin Auto Discovery 生效
- [ ] MCP Router 生效
- [ ] SuperHarness 日志能看到 Router / Tool / LLM / Trace

## B. Provider

- [ ] 途牛 Flight 正常
- [ ] 途牛 Hotel 正常
- [ ] 途牛 Ticket 正常
- [ ] 途牛 Cruise 正常
- [ ] 途牛 Holiday 正常
- [ ] 12306 MCP 正常
- [ ] 12306 失败可 fallback Tuniu Train
- [ ] 高德 POI 正常
- [ ] 高德 Route 正常
- [ ] TikHub 免费模式正常
- [ ] TikHub 无免费额度时 MediaCrawler fallback

## C. 规划

- [ ] 价格不是 LLM 编的
- [ ] 每个实时价格有 fetched_at
- [ ] 第一日考虑到达时间
- [ ] 最后一日考虑返程
- [ ] 城内路线基于高德
- [ ] 有 Budget Check
- [ ] 有 Feasibility Check
- [ ] 有 Trust / Ad Risk
- [ ] 有路线折返检查
- [ ] 有 Critic 修订

## D. Provenance

- [ ] 最终 Place 能定位 Evidence
- [ ] Evidence 能定位 Source
- [ ] REJECT 有理由
- [ ] PASS 有理由
- [ ] NOT_USED 有理由
- [ ] audit_report 完整

## E. Interface

- [ ] CLI 能运行
- [ ] FastAPI 能运行
- [ ] Frontend 能创建计划
- [ ] Frontend 能展示日程
- [ ] Frontend 能展示价格
- [ ] Frontend 能展示来源
- [ ] Frontend 能展示 Budget
- [ ] Frontend 能展示 Warning

---

# 36. 最终验收报告模板

开发 Agent 完成后必须填写，不允许只写“测试通过”。

## 36.1 环境

```text
OS:
Python:
Node:
SuperHarness commit:
TravelPlan commit:
Tuniu CLI version:
12306-mcp version:
```

## 36.2 自动测试

```text
pytest:
Passed:
Failed:
Skipped:
```

## 36.3 Provider Smoke

| Provider | Tool | 状态 | 返回条数 | 延迟 | 备注 |
|---|---|---:|---:|---:|---|
| 12306 | train | | | | |
| Tuniu | flight | | | | |
| Tuniu | hotel | | | | |
| Tuniu | ticket | | | | |
| AMap | poi | | | | |
| AMap | route | | | | |
| TikHub | xhs | | | | |
| MediaCrawler | xhs | | | | |

## 36.4 E2E Case

输入：

```text
...
```

结果：

```text
run_id:
transport:
hotel:
day_count:
real_cost:
estimated_cost:
warnings:
```

验证：

- [ ] 所有实时价格有 source
- [ ] 所有实时价格有 fetched_at
- [ ] 所有 itinerary place 有 evidence
- [ ] 时间无硬冲突
- [ ] 预算计算正确
- [ ] plan.json 生成
- [ ] plan.md 生成
- [ ] audit_report.json 生成

## 36.5 Fallback

- [ ] 人工让 12306 MCP 不可用，确认走途牛
- [ ] TikHub free_credit=0 / mock 402，确认走 MediaCrawler
- [ ] AMap 异常时不允许模型编路线

## 36.6 前端

- [ ] Desktop
- [ ] Mobile
- [ ] Loading
- [ ] Empty
- [ ] Partial Data
- [ ] Error
- [ ] Real-time Price Timestamp
- [ ] Evidence Drawer
- [ ] Budget Summary
- [ ] Feasibility Warning

## 36.7 已知问题

必须真实列出：

```text
1.
2.
3.
```

---

# 37. 外部接口事实参考（核验日期：2026-09-18）

以下是实现时应优先查看的官方/项目文档：

- SuperHarness：`https://github.com/Sitozzmonash/super_harness`
- 途牛 CLI：`https://github.com/tuniucorp/tuniu-cli`
- 12306 MCP：`https://github.com/Joooook/12306-mcp`
- 高德 POI 2.0：`https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch`
- 高德路径规划 2.0：`https://lbs.amap.com/api/webservice/guide/api/newroute`
- TikHub：`https://docs.tikhub.io/`
- TikHub 小红书 Search Notes：`https://docs.tikhub.io/420136398e0`
- MediaCrawler：`https://github.com/NanmiCoder/MediaCrawler`

实现时不要根据本 PRD 猜第三方字段；实际调用前使用 Provider 最新 Schema / `help` / `list` 校验。
