# TravelPlan 开发启动说明（START）

> 本文件是开发 Agent 的**第一入口**。  
> 开始写代码前，必须先完整阅读本文件，再按顺序阅读 `docs/PRD.md` 与 `docs/FRONTEND_DESIGN.md`。  
> 项目时间基准：2026-09-18。  
> 项目用途：个人学习 / 作品项目，不商用。

---

## 1. 先明确：这个项目是什么

TravelPlan 是一个面向**中国国内旅行**的智能旅行规划 Agent。

它不是“让 LLM 凭空写一份攻略”，而是：

```text
用户自然语言需求
→ 查询真实机票 / 高铁 / 酒店 / 景点门票 / 邮轮 / 旅游产品
→ 搜索小红书 / 抖音等真实攻略
→ 高德验证地点与计算路线
→ 过滤广告和低可信内容
→ 比较价格与时间
→ 检查预算与时间可行性
→ 生成每天行程
→ 输出来源、实时价格、查询时间和完整决策记录
```

核心原则：

> Evidence First, LLM Second.  
> 没有真实数据支撑的价格、车次、航班、营业时间、路线，不允许模型自行编造。

---

## 2. 必须使用 SuperHarness

TravelPlan **必须基于现有 SuperHarness 开发**，不要另起 Agent Framework。

仓库：

- TravelPlan：`Sitozzmonash/travelplan`
- SuperHarness：`Sitozzmonash/super_harness`

本地目标结构：

```text
travelplan/
├─ super_harness/                # Git submodule，独立 Git 仓库
│  └─ superharness/
│     └─ capabilities/
│        ├─ plugins/
│        │  ├─ tuniu_travel/
│        │  ├─ amap_cn/
│        │  ├─ tikhub_social/
│        │  └─ mediacrawler_social/
│        └─ mcp/
│           └─ railway_12306.py
│
├─ app/
│  ├─ __init__.py
│  ├─ agent.py
│  ├─ api.py
│  ├─ workflow.py
│  ├─ models.py
│  ├─ planner.py
│  ├─ store.py
│  └─ prompts.py
│
├─ frontend/
├─ tests/
├─ docs/
│  ├─ START.md
│  ├─ PRD.md
│  └─ FRONTEND_DESIGN.md
├─ outputs/
├─ main.py
├─ .env
├─ requirements.txt
└─ README.md
```

### 2.1 不允许重复实现 SuperHarness 已经有的能力

SuperHarness 当前已经提供：

- `create_harness_agent`
- `create_harness_app`
- Tool Auto Discovery
- Plugin Auto Discovery + Router + Lazy Load
- MCP Server Router + MCP Tool Router
- BM25 / LLM / Hybrid Capability Router
- Workflow
- Memory
- External RAG 接口
- Retrieval Fusion
- Subagent
- Retry
- Checkpoint
- Observability
- EventBus
- Trace
- Tool / LLM / Router 日志
- Tavily `web_search`
- `http_fetch`

因此 TravelPlan **不要再创建**：

```text
自制 Router
自制 Plugin Loader
自制 MCP Client
自制 Memory Runtime
自制通用 RAG Runtime
自制 Retry 框架
自制 Trace 框架
第二套 Agent Runtime
第二套 Tool Registry
```

TravelPlan 只负责“旅行领域逻辑”。

---

## 3. Git 关系

`super_harness/` 必须作为 `travelplan` 的 Git Submodule，而不是普通复制目录。

目标：

```text
travelplan/.git
travelplan/super_harness/.git  → submodule 独立仓库
```

### 第一次初始化

如果 submodule 尚未加入：

```powershell
cd D:\Documents\MyWorkSpace\travelplan
git submodule add https://github.com/Sitozzmonash/super_harness.git super_harness
git add .gitmodules super_harness
git commit -m "add super_harness submodule"
```

如果仓库已经记录 submodule：

```powershell
git submodule update --init --recursive
```

### 提交规则

修改通用 Plugin / MCP：

```powershell
cd super_harness
git add .
git commit -m "add travel capabilities"
git push
```

然后回 TravelPlan 更新 submodule commit 指针：

```powershell
cd ..
git add super_harness
git commit -m "update super_harness"
git push
```

TravelPlan 的 `app/`、`frontend/`、`docs/`、测试等只提交到 `travelplan` 仓库。

---

## 4. 数据源规则（不可擅自扩张）

### 4.1 机票 / 酒店 / 门票 / 邮轮 / 旅游产品

**只用途牛。**

对应 SuperHarness Plugin：

```text
tuniu_travel
```

V1 只查询，不允许：

```text
下单
支付
取消订单
自动购票
抢票
```

即使途牛 CLI 本身存在下单工具，`tuniu_travel` Plugin 也不能暴露这些写操作给 Agent。

### 4.2 火车 / 高铁

```text
12306 MCP     → 主数据源
途牛 Train    → fallback
```

### 4.3 小红书 / 抖音攻略

```text
TikHub         → 主
MediaCrawler   → fallback
```

但项目有硬性约束：

> 第三方 API 只能使用免费额度，不允许产生现金付费。

TikHub 必须启用 `FREE_ONLY` 保护：

1. 调用用户信息接口检查账户；
2. 如果账户存在现金余额，在无法证明只扣免费额度时，默认拒绝发起收费接口请求；
3. 若 `free_credit <= 0`，直接 fallback；
4. 若接口返回 402 / 不接受免费额度，直接 fallback；
5. 不实现充值、不自动购买额度、不自动切换到现金余额。

### 4.4 POI / 地点验证 / 市内路线

**只用高德 Web Service。**

用途：

- POI 搜索
- POI 详情
- 地理编码
- 经纬度
- 驾车路线
- 公交路线
- 步行路线
- 距离
- 耗时
- 可返回时使用费用字段

### 4.5 通用网页搜索

SuperHarness 已经存在 `web_search`，后端是 Tavily，读取：

```env
TAVILY_API_KEY=
```

因此 TravelPlan 不要再创建 Tavily Plugin。

用途仅为补充：

- 官网信息
- 临时事实核验
- 攻略网页补充
- 已知链接外部信息

不能拿 Tavily 代替机票、酒店、高铁、高德等结构化数据源。

---

## 5. 已准备的 `.env`

项目根目录已有：

```env
MODEL_NAME=
MODEL_BASE_URL=
MODEL_API_KEY=
TAVILY_API_KEY=
AMAP_API_KEY=
TIKHUB_API_TOKEN=
TUNIU_API_KEY=
```

规则：

- Secret 只能从环境变量读取。
- 不允许把 Key 写入代码、Prompt、Skill、日志、输出文件、测试快照。
- `TIKHUB_API_TOKEN` 只允许免费额度模式。
- 12306 MCP 不需要 Token。
- MediaCrawler 不需要 API Token。

---

## 6. SuperHarness 只新增 5 个通用能力

### Plugin

```text
tuniu_travel
amap_cn
tikhub_social
mediacrawler_social
```

### MCP wrapper

```text
railway_12306.py
```

不要把 TravelPlan 的：

- 广告判断
- Trust Score
- 预算计算
- 时间可行性
- 行程规划
- Evidence 决策
- Travel Critic

放进 SuperHarness。

这些属于 `travelplan/app/`。

---

## 7. TravelPlan 最小业务结构

### `app/agent.py`

只负责装配：

- system prompt
- `HarnessContext`
- `create_harness_app`
- 12306 MCP ServerSpec
- Travel Workflow
- 必要的 RAG Retriever
- Checkpointer（开发态可用）

不要在这里写业务算法。

### `app/workflow.py`

固定旅行规划主流程：

```text
parse
→ transport
→ stay
→ research
→ poi_verify
→ plan
→ feasibility
→ critic
→ finalize
```

复杂自由问题仍然允许 SuperHarness Agent Loop 处理。

### `app/models.py`

保存 TravelPlan 自己的数据模型：

- TripIntent
- FlightOption
- TrainOption
- HotelOption
- Place
- RouteOption
- Evidence
- Decision
- ItineraryItem
- ItineraryDay
- TripPlan
- BudgetSummary
- FeasibilityIssue

### `app/planner.py`

项目真正核心：

- 去重
- Trust Score
- Ad Risk
- 价格比较
- 预算计算
- 时间约束
- 路线约束
- 地理聚类
- 候选排序
- 行程生成/修正

### `app/store.py`

只负责 TravelPlan 自己的业务证据：

- run
- source
- evidence
- normalized result
- decision
- final plan

使用 SQLite。

注意：这不是重新实现 SuperHarness Memory。

### `app/api.py`

FastAPI，对前端暴露 TravelPlan API。

必须与 CLI 走**同一套 Agent / Workflow / Planner**，禁止 CLI 与 Web 各写一套逻辑。

### `main.py`

本地 CLI：

```powershell
python main.py -m "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照"
```

---

## 8. 输出必须保留两种入口

### 8.1 本地 Python CLI

必须可独立运行，即使没有 frontend：

```powershell
python main.py -m "..."
```

### 8.2 Web API + Frontend

后端：

```powershell
uvicorn app.api:api --reload
```

前端：

```powershell
cd frontend
npm install
npm run dev
```

Frontend 只是展示层，所有真实数据和 Key 都在 Python 后端。

---

## 9. 每次运行必须输出

```text
outputs/<run_id>/
├─ plan.json
├─ plan.md
└─ audit_report.json
```

至少包含：

- 用户原始输入
- 解析后的 TripIntent
- 每个 Tool / Provider 的查询条件
- 查询时间 `fetched_at`
- 原始结果数量
- 归一化结果
- 实时价格
- 来源
- KEEP / REJECT
- SELECT / PASS
- 原因
- 时间可行性检查
- 预算检查
- 最终每天行程
- 最终使用了哪些证据
- 哪些证据没有使用以及原因

SuperHarness 原生 Observability 继续负责：

- Router
- LLM
- Tool input
- Tool output
- token
- cache
- latency
- trace

不要重造。

---

## 10. 核心执行规则

### 10.1 价格

机票、酒店、高铁、门票等：

- 必须来自实时 Tool 返回；
- 必须记录 provider；
- 必须记录 `fetched_at`；
- 禁止模型估价格；
- 最终 UI 必须标注“价格可能变化”。

### 10.2 时间

不能只检查“景点几点开门”。

必须检查：

```text
航班/火车抵达
+ 出站/取行李 buffer
+ 市内交通
+ 酒店入住
+ 景点游玩时间
+ 下一段交通
+ 餐厅时间
+ 闭园/闭店
```

### 10.3 路线

不能让 LLM 自己猜“顺路”。

必须以高德返回的真实距离 / 时间作为主要依据。

### 10.4 来源

最终 itinerary item 必须能追溯：

```text
ItineraryItem
→ Place
→ Evidence
→ Source
```

---

## 11. 开发顺序

严格按以下顺序，不要一上来写前端或大规模 UI：

### Phase 0：环境

- submodule
- `pip install -e`
- Node / npm
- tuniu CLI
- 12306 MCP
- `.env`
- pytest baseline

### Phase 1：SuperHarness 通用能力

先实现并单测：

1. `tuniu_travel`
2. `railway_12306`
3. `amap_cn`
4. `tikhub_social`
5. `mediacrawler_social`

每个能力先独立跑通，再接 TravelPlan。

### Phase 2：TravelPlan 数据模型 + Store

先定义 Schema 和 provenance。

### Phase 3：Planner

先用 mock Tool 数据跑：

- 时间检查
- 预算
- 地理顺路
- Trust
- Ad Risk

### Phase 4：Workflow

把真实 Tool 接进来。

### Phase 5：CLI E2E

必须先跑通完整 CLI。

### Phase 6：FastAPI

复用同一业务入口。

### Phase 7：Frontend

按 `FRONTEND_DESIGN.md` 实现。

### Phase 8：真实验收

至少执行：

```text
北京 → 成都
上海 → 杭州
长春 → 成都
广州 → 重庆
```

覆盖：

- 飞机
- 高铁
- 酒店
- 景点
- 小红书/抖音攻略
- 高德路线
- 时间冲突修正
- 超预算修正
- fallback

---

## 12. 开发 Agent 禁止事项

不要：

- 重新设计 SuperHarness
- 复制 SuperHarness 到 TravelPlan 业务代码中
- 把所有逻辑塞 `main.py`
- 创建十几个无意义 service/repository/manager 层
- 让 LLM 估实时价格
- 把 Key 写进代码
- 自动调用途牛下单接口
- 自动购票
- 自动充值 TikHub
- 在 TikHub 免费额度不可确认时消耗现金余额
- 对 Tool 失败进行“假数据兜底”
- Tool 查不到就让 LLM 编一个结果
- 为了测试而伪造“真实查询成功”

Tool 失败必须如实记录：

```text
status=unavailable
provider=...
reason=...
fallback=...
```

---

## 13. Definition of Done

开发完成至少满足：

- [ ] TravelPlan 使用 SuperHarness，而非自建 Runtime
- [ ] SuperHarness 5 个通用能力已独立测试
- [ ] 12306 主、途牛 Train fallback
- [ ] 机票/酒店/门票/邮轮/旅游产品只用途牛
- [ ] TikHub 免费模式 + MediaCrawler fallback
- [ ] 高德负责 POI / 路线
- [ ] CLI 可完整跑
- [ ] FastAPI 可完整跑
- [ ] Frontend 可完整展示
- [ ] 真实价格带查询时间
- [ ] 所有推荐可追溯
- [ ] PASS / REJECT 有原因
- [ ] 时间可行性不是由 LLM 猜
- [ ] 路线使用高德真实数据
- [ ] 有 `plan.json`
- [ ] 有 `plan.md`
- [ ] 有 `audit_report.json`
- [ ] pytest 通过
- [ ] README 有完整运行说明
- [ ] PRD 的验收表全部填写

完成前不要把项目描述为“已完成”。

---

## 14. 阅读顺序

开发 Agent 必须按顺序阅读：

```text
1. docs/START.md
2. docs/PRD.md
3. super_harness/README.md
4. super_harness/examples/production/ex15_full_production.py
5. super_harness/examples/production/ex16_xiaohongshu_mcp.py
6. super_harness/examples/production/ex17_secops_plugin.py
7. docs/FRONTEND_DESIGN.md
```

开始编码前，先根据以上文件输出一份**10~20 行实施计划**，确认将复用哪些 SuperHarness 能力、哪些代码写入 SuperHarness、哪些代码只属于 TravelPlan，然后再编码。
