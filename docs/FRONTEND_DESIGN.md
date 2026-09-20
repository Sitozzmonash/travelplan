# TravelPlan 前端设计说明（直接给 v0）

> 目标：请根据本文件直接生成一个可运行的 TravelPlan Web 前端。  
> 产品：中国国内 AI 旅行规划。  
> 前端只负责交互与展示；真实机票、高铁、酒店、门票、攻略、路线和价格全部由 Python 后端提供。  
> 第一版请优先做**高质量完整页面与组件状态**，接口暂时可以用 mock service 封装，方便后续连接 FastAPI。

---

# 1. 技术要求

使用 v0 默认推荐技术栈：

```text
Next.js
TypeScript
Tailwind CSS
shadcn/ui
Lucide Icons
```

要求：

- Desktop + Mobile Responsive
- 组件化
- TypeScript 类型完整
- 不在组件里散落大量 mock 数据
- 建立统一 `types`
- 建立统一 `lib/api.ts`
- API 可从 mock 切换到真实 FastAPI
- 不把任何 API Secret 写入前端
- 不直接调用途牛、TikHub、高德 Web Service Secret
- 中文界面

---

# 2. 产品定位

这不是普通“AI 聊天机器人”。

前端要让用户明显感觉它在做：

```text
真实搜索
→ 比价
→ 攻略研究
→ 地点验证
→ 路线规划
→ 时间检查
→ 预算检查
→ 输出可执行行程
```

核心卖点在 UI 上必须能看见：

1. **实时价格**
2. **数据来源**
3. **为什么推荐**
4. **时间是否可行**
5. **预算是否超标**
6. **路线是否顺路**
7. **攻略可信度 / 广告风险**

不要只做一个大聊天框。

---

# 3. 视觉方向

关键词：

```text
现代
轻量
旅行感
专业
可信
不花哨
信息密度适中
```

避免：

- 游戏化
- 过度渐变
- 大量玻璃拟态
- 夸张 AI 科技蓝
- 整页都是聊天气泡
- 旅游网站式密集广告卡片

建议：

- 大面积浅色背景
- 白色内容卡片
- 清晰分区
- 小面积品牌色
- 使用照片作为旅行氛围辅助
- 数据和行程仍是视觉主角

品牌暂时叫：

```text
TravelPlan
```

中文副标题：

```text
用真实信息，规划真正能走的行程
```

---

# 4. 整体信息架构

第一版至少包含：

```text
/
├─ 首页 / 创建旅行
├─ /plan/[id]  行程工作台
└─ 可用 Drawer / Dialog
   ├─ 交通详情
   ├─ 酒店详情
   ├─ 地点详情
   ├─ Evidence / 来源详情
   └─ Audit / 规划依据
```

不需要登录系统。

---

# 5. 首页

## 5.1 顶部导航

左：

```text
TravelPlan
```

右：

```text
新建行程
关于项目（可选）
GitHub（可选）
```

不要做复杂菜单。

## 5.2 Hero

标题：

```text
说出你想怎么旅行
剩下的交给 TravelPlan
```

副标题：

```text
比较机票、高铁、酒店与门票，结合小红书/抖音攻略和真实路线，
自动生成预算合理、时间可行的国内旅行计划。
```

## 5.3 主输入框

视觉上是首页中心。

Placeholder：

```text
例如：10月1日从北京去成都玩5天，两个人预算6000，喜欢美食、拍照和历史文化
```

支持多行。

按钮：

```text
开始规划
```

下方快捷示例：

```text
北京 → 成都 5天
上海 → 杭州 周末
广州 → 重庆 4天美食
长春 → 成都 国庆
```

## 5.4 可选结构化条件

输入框下方提供“小而轻”的高级条件，不抢主输入：

- 出发地
- 目的地
- 日期
- 天数
- 人数
- 总预算
- 偏好 Chips：
  - 美食
  - 拍照
  - 自然
  - 历史
  - 亲子
  - 购物
  - 夜生活
  - 轻松
- 节奏：
  - 轻松
  - 平衡
  - 特种兵

自然语言输入仍是主入口。

---

# 6. 规划过程页 / Loading 状态

点击“开始规划”后，不要只显示 spinner。

显示真实 Agent 工作步骤：

```text
✓ 理解旅行需求
✓ 查询机票 / 高铁
✓ 比较交通时间和价格
✓ 查询酒店
● 搜索小红书 / 抖音攻略
○ 高德验证地点
○ 计算市内路线
○ 检查预算
○ 检查时间可行性
○ 生成最终行程
```

每一步状态：

```text
waiting
running
success
warning
error
```

右侧或下方展示轻量事实：

```text
找到 14 个交通方案
找到 26 家酒店
读取 38 条攻略证据
验证 17 个 POI
```

注意：

- 不假装后端真的流式返回这些数据；
- mock 版用合理模拟；
- 接 FastAPI 后由真实状态驱动。

---

# 7. 行程工作台 `/plan/[id]`

这是核心页面。

Desktop：

```text
┌────────────────────────────────────────────────────────────┐
│ Header：成都 5 天 · 北京出发 · 2人 · ¥6000                │
├────────────────────────────────────────────────────────────┤
│ Summary Cards                                               │
├───────────────────────┬────────────────────────────────────┤
│ 左：行程时间线        │ 右：地图 / 当前 Day 路线           │
│                       │                                    │
│ Day 1                 │                                    │
│ Day 2                 │                                    │
│ Day 3                 │                                    │
├───────────────────────┴────────────────────────────────────┤
│ 交通 / 酒店 / 预算 / 来源                                   │
└────────────────────────────────────────────────────────────┘
```

Mobile：

```text
顶部摘要
↓
Day Tabs
↓
行程卡片
↓
地图折叠卡
↓
预算
↓
交通 / 酒店
```

不要在手机上强行左右分栏。

---

# 8. 顶部 Summary

显示：

```text
成都 · 5天4晚
2026/10/01 - 2026/10/05
北京出发
2 人
```

状态 Badge：

```text
时间可行
预算内
路线已验证
```

若有问题：

```text
1 个时间冲突已自动修正
部分实时价格可能变化
```

---

# 9. Summary Cards

四张核心卡：

### 交通

```text
去程：CAxxxx
¥760 / 人
北京 08:05 → 成都 11:20
```

或高铁。

### 酒店

```text
成都春熙路 ××酒店
4 晚
¥1,680
```

### 预算

```text
预计总计 ¥5,180
预算 ¥6,000
剩余 ¥820
```

区分：

```text
实时价格
估算费用
```

### 可信度

```text
使用 42 条来源
17 个地点已验证
4 个低可信候选被过滤
```

---

# 10. Day Navigation

使用 Tabs / horizontal scroll：

```text
Day 1
Day 2
Day 3
Day 4
Day 5
```

Tab 下方可显示主要区域：

```text
Day 2 · 武侯祠 / 锦里 / 玉林
```

---

# 11. Itinerary 时间线

每个 Item：

```text
09:00
武侯祠
历史文化 · 建议 2h

为什么推荐：
高可信、多来源重复推荐，与锦里步行衔接

Trust 91
Ad Risk 12

门票：¥xx
来源：高德 + 小红书 + 抖音

↓ 步行 8 分钟

11:15
锦里
...
```

Item Type：

- attraction
- food
- hotel
- transport
- activity
- free_time

不同类型通过 icon 区分，不要使用太多颜色。

---

# 12. 行程 Item 卡片

必须包含：

### 主信息

- 名称
- 时间
- 类别
- 预计停留
- 区域

### 实时信息

有真实数据时：

```text
¥xxx
查询于 14:32
```

必须显示：

```text
价格可能变化
```

### 路线

```text
距上一地点 2.1km
地铁 / 步行约 18 分钟
```

### 推荐理由

1~2 行。

### Evidence

显示 compact badge：

```text
8 条证据
Trust 91
广告风险 12
```

点击打开 Evidence Drawer。

---

# 13. Evidence Drawer

这是产品差异化关键页面。

标题：

```text
为什么推荐「××火锅」
```

显示：

### 综合判断

```text
Trust Score 91 / 100
Ad Risk 12 / 100
```

### 推荐原因

```text
✓ 多个平台独立出现
✓ 多名普通用户具体提到“毛肚、黄喉”
✓ 推荐时间跨度较长
✓ 高德 POI 信息一致
```

### 风险

```text
部分内容包含探店营销措辞
但占比低，且存在大量独立用户证据
```

### Sources

卡片：

```text
小红书
标题...
作者...
发布时间...
查看来源

抖音
...

高德
POI 验证
```

不要显示完整长原文，只显示摘要。

---

# 14. 地图区域

V0 页面阶段**不要要求真实地图 SDK Key**。

先做一个高度还原的“Map Panel”组件：

- 浅色地图背景 placeholder
- 路线 polyline
- 编号 Pin：
  - 1
  - 2
  - 3
- 当前选中地点高亮
- 顶部 Day 区域名
- 底部：
  - 总移动时间
  - 总距离

组件接口预留：

```ts
type MapPoint = {
  id: string
  name: string
  lat: number
  lng: number
  order: number
}
```

未来接高德 JS SDK 时不要重写整个页面。

注意：

```text
AMAP_API_KEY 是后端 Web Service Secret，
不要直接放 frontend。
```

如果未来需要高德 JS SDK，使用独立前端 Key / 安全配置。

---

# 15. 交通比较

点击交通 Summary 打开 Drawer/Dialog。

分：

```text
推荐
飞机
高铁
```

每个方案：

```text
CAxxxx
08:05 - 11:20
飞行 3h15m
¥760

机场往返预计：
+ 110 分钟

门到门预计：
5h05m
```

高铁：

```text
Gxx
07:20 - 14:10
6h50m
¥662
```

显示“为什么推荐当前方案”。

不要只突出最低价。

---

# 16. 酒店比较

列表卡：

```text
酒店名
区域
价格/晚
总价
距当天主要区域
Trust / Provider
```

Filter 视觉可以预留：

```text
价格
区域
评分
距主要行程
```

V1 不需要真的在前端重新规划，只发送修改请求给后端。

---

# 17. Budget 页面 / 区块

使用简洁 stacked bar 或分类列表。

分类：

```text
大交通
住宿
门票
餐饮估算
市内交通估算
其他
```

明确区分：

### 真实价格

```text
机票
火车
酒店
门票
```

### 估算

```text
餐饮
部分市内费用
```

示例：

```text
预算            ¥6000
已知实时费用    ¥3980
估算费用        ¥1120
预计总计        ¥5100
剩余            ¥900
```

如果超预算：

```text
预计超出 ¥630
```

同时展示：

```text
可优化：
- 酒店换至 ¥320/晚选项，可省约 ¥480
- 去程改高铁，可省约 ¥210
```

这些建议必须来自后端。

---

# 18. Feasibility / Warning

页面上需要有统一 Warning 区。

示例：

```text
已自动修正 1 个时间冲突

原计划：
14:00 武侯祠

问题：
航班落地 + 机场到酒店后，最早约 14:20 才能出发

调整：
武侯祠改为 15:00
```

这会很好地体现 Agent 不是只“写攻略”。

---

# 19. Hotel Card

最终选中酒店：

```text
酒店名称
位置
入住 / 离店
4 晚
¥420 / 晚
¥1680 总价
查询时间
```

提供：

```text
查看其他候选
```

不做“立即预订”。

---

# 20. Source / Provenance

所有关键实时数据支持查看来源。

显示 Badge：

```text
途牛
12306
高德
小红书
抖音
Tavily
```

点击可查看：

```text
Provider
Fetched At
Source URL（如果有）
查询条件
```

API Token 永不显示。

---

# 21. Audit Drawer

高级用户入口，放在页面右上角：

```text
规划依据
```

内容不是工程 Trace 全量，而是产品化版：

```text
交通
查到 14 个候选 → 过滤 6 个 → 比较 8 个 → 选择 CAxxxx

酒店
查到 26 家 → 区域筛选 → 价格筛选 → 选择 ××酒店

攻略
读取 38 条 → 提取 23 个地点 → 高德验证 17 个 → 最终使用 11 个

路线
检测 1 次明显折返 → 已修正

时间
检测 1 个冲突 → 已修正
```

可下载：

```text
plan.json
plan.md
audit_report.json
```

按钮视觉先实现，真实下载接口后接。

---

# 22. 用户修改计划

V1 UI 预留简单动作：

```text
换一个
不想去
锁定这个地点
这天轻松一点
降低预算
```

点击后可以显示确认 Dialog：

```text
将重新规划 Day 2，已锁定地点不会变化。
```

后端 API 后续可接：

```text
POST /plans/{id}/revise
POST /plans/{id}/items/{item_id}/replace
POST /plans/{id}/items/{item_id}/lock
```

第一版 v0 可以用 mock。

---

# 23. API Adapter

前端创建：

```text
lib/api.ts
```

不要直接在页面组件 fetch。

接口：

```ts
createPlan(message)
getPlan(id)
getAudit(id)
```

数据模型至少：

```ts
TripPlan
TripSummary
TransportOption
HotelOption
ItineraryDay
ItineraryItem
BudgetSummary
Evidence
Source
Warning
```

后端地址：

```env
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

这不是 Secret。

---

# 24. Backend API 假定

### `POST /api/v1/plans`

Request：

```json
{
  "message": "10月1日北京去成都5天，两个人预算6000，喜欢美食和拍照"
}
```

### `GET /api/v1/plans/:id`

返回完整 `TripPlan`。

### `GET /api/v1/plans/:id/audit`

返回 Audit。

### `GET /api/v1/health`

健康检查。

---

# 25. Mock Data

请创建一套高质量 mock：

```text
北京 → 成都
5天4晚
2人
预算 ¥6000
偏好：美食 / 拍照 / 历史
```

Mock 至少包含：

- 1 个最终交通方案
- 2 个交通备选
- 1 个最终酒店
- 3 个酒店备选
- 5 天 itinerary
- 每天 3~5 个 item
- 高德 route 数据
- 6~10 条 Evidence
- 预算
- 1 个被自动修正的时间冲突
- 1 个因 Ad Risk 高而被过滤的地点

不要只做空壳 UI。

---

# 26. 页面状态

每个主页面必须考虑：

### Loading

有 Skeleton。

### Empty

例如：

```text
暂时没有找到符合条件的酒店
```

### Partial

例如：

```text
小红书实时数据暂时不可用，已使用其他可用来源完成规划。
```

### Error

例如：

```text
高德路线暂时不可用，因此本次无法确认该段真实通勤时间。
```

不要写成：

```text
暂无数据
```

却不给用户解释。

---

# 27. Mobile

手机端是重要目标。

建议底部 sticky：

```text
行程
地图
预算
依据
```

Day tabs 横向滚动。

Item Card 单列。

地图高度约：

```text
280~360px
```

避免小屏幕显示巨大表格。

交通比较在 mobile 用 Sheet。

---

# 28. Desktop

建议最大宽：

```text
1440px
```

Plan workspace：

```text
左 55~60%
右 40~45%
```

地图右侧 sticky。

顶部 Summary 保持可扫描性。

---

# 29. 图标建议

使用 Lucide：

```text
Plane
Train
Hotel
MapPin
Utensils
Camera
Landmark
Ticket
Wallet
Clock
Route
ShieldCheck
TriangleAlert
ExternalLink
Search
Sparkles
```

不要用 Emoji 做正式主图标。

---

# 30. 文案规范

不要使用：

```text
AI 为你打造梦幻旅程
开启奇妙之旅
探索无限可能
```

更专业：

```text
已比较 14 个交通方案
已验证 17 个地点
1 个时间冲突已修正
价格查询于 14:32
该餐厅存在较高广告风险
```

让用户感到“这是一个可靠工具”，不是营销落地页。

---

# 31. 色彩

不要锁死具体 Hex，也不要过度使用品牌色。

建议语义：

```text
Primary      操作与选中
Success      可行 / 预算内 / 已验证
Warning      时间风险 / 数据不完整
Danger       超预算 / 不可行
Muted        来源 / 次要信息
```

整体保持低饱和、干净。

---

# 32. 图片

首页可以有少量高质量中国城市旅行图片。

Plan Workspace 不要铺大量图片。

地点卡可选 1 张缩略图，但：

- 没有真实图片时用 placeholder；
- 不要自动使用无法确认来源的图片；
- 图片不是信息主角。

---

# 33. 生成代码要求

请 v0 输出完整可运行前端，而不是截图。

要求：

```text
frontend/
├─ app/
├─ components/
├─ lib/
├─ types/
├─ public/
├─ package.json
└─ ...
```

建议组件：

```text
TripSearch
PlanningProgress
TripSummary
DayTabs
ItineraryTimeline
ItineraryCard
TravelMap
TransportCompare
HotelCompare
BudgetCard
EvidenceDrawer
AuditDrawer
FeasibilityAlert
SourceBadge
PriceBadge
```

避免一个 1000+ 行页面文件。

---

# 34. 与后端集成原则

当前先以 mock API 开发 UI。

但结构必须做到：

```text
mockData
   ↓
lib/api.ts
   ↓
UI
```

后续只需要替换 `lib/api.ts`，不要大改组件。

Frontend 永远不负责：

```text
调用 LLM
运行 Agent
调用 MCP
调用 Tuniu CLI
调用 TikHub Secret API
计算 Trust
计算 Ad Risk
计算 Feasibility
决定最终 Plan
```

这些都在 Python。

---

# 35. 第一版最终体验

用户打开首页：

```text
输入：
“10月1日北京去成都5天，两个人预算6000，喜欢美食和拍照”
```

点击：

```text
开始规划
```

看到过程：

```text
正在查交通...
正在比较价格...
正在查酒店...
正在搜攻略...
正在验证路线...
正在检查预算...
```

进入 Plan：

```text
成都 5 天
预计总计 ¥5,180 / ¥6,000
时间可行
1 个冲突已修正

Day 1
机场 → 酒店 → 人民公园 → 宽窄巷子 → 火锅

Day 2
武侯祠 → 锦里 → 玉林
...
```

用户可以点：

```text
为什么推荐？
```

看到：

```text
Trust 91
Ad Risk 12

8 条独立证据
高德 POI 已验证
路线与当天区域匹配
```

这就是前端第一版需要完整表达出来的产品价值。

---

# 36. v0 最终要求

请直接：

1. 创建完整项目页面；
2. 使用高质量 Mock Data；
3. Desktop / Mobile 都做好；
4. 首页、规划 Loading、完整 Plan Workspace 都实现；
5. 实现 Evidence Drawer；
6. 实现 Transport Compare；
7. 实现 Hotel Compare；
8. 实现 Budget；
9. 实现 Feasibility Warning；
10. 实现 Audit Drawer；
11. `lib/api.ts` 预留 FastAPI；
12. 不需要实现真实后端；
13. 不需要任何 Secret；
14. 不需要登录；
15. 不要简化成聊天页面。
