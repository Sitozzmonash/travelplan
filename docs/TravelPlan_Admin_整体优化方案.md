# TravelPlan 管理后台整体优化方案

## 1. 当前问题

TravelPlan 管理后台目前已经具备较完整的工程能力，包括：

- Dashboard
- Runs
- Planning Sessions
- Bad Cases
- Provider Health
- Benchmark
- Evolution
- Config
- Trace / Provider Call / LLM Call 等运行数据

但现在整体更像一个 **Agent 工程监控台**，还不是一个真正高效的 **TravelPlan 产品质量控制台**。

当前后台更擅长回答：

```text
系统调用了多少次 LLM？
用了多少 Token？
Provider 有没有失败？
某个 Run 调了哪些工具？
```

但不够擅长回答更重要的问题：

```text
为什么这次酒店选得不好？
为什么餐厅很差？
为什么路线不顺？
为什么没有体现用户偏好？
最近最常出现的问题是什么？
哪个模块现在最值得优先修？
```

因此下一步不应该继续堆更多日志和指标，而应该优化：

```text
信息架构
+ Dashboard
+ 产品质量诊断
+ Bad Case 聚类
+ Planning Funnel
+ Run Detail
+ Trace Timeline
```

目标是让后台同时具备：

```text
系统观测
+
产品质量诊断
+
问题定位
+
评测闭环
```

---

# 2. 推荐的信息架构

建议导航改成：

```text
总览
├─ Dashboard

产品质量
├─ Travel Quality
├─ Planning Funnel
└─ Bad Cases

运行观测
├─ Runs
├─ Planning Sessions
└─ Provider Health

评测优化
├─ Benchmark
└─ Evolution

系统
└─ Config
```

重点是新增两个核心页面：

```text
Travel Quality
Planning Funnel
```

并把 `Bad Cases` 从单纯列表升级成真正的“问题中心”。

---

# 3. Dashboard 重构

## 3.1 当前问题

现在 Dashboard 有很多累计指标，例如：

```text
运行总数
运行中
降级完成
失败
总 Token
LLM 调用
Jev 调用
工具调用
Provider 失败
Bad Case
Benchmark
Evolution
```

这些数据不是没用，而是**首屏优先级不对**。

例如：

```text
总 Token = 2,000,000
总工具调用 = 18,000
```

很难直接指导下一步该修什么。

---

## 3.2 建议首屏只展示真正关键指标

建议：

```text
成功率
降级率
失败率
P95 规划耗时
平均成本 / Run
未处理 Bad Case
异常 Provider 数
```

例如：

```text
成功率           93.4%  ↑2.1%
降级率            5.8%  ↓1.2%
失败率            0.8%
P95               78s   ↓19%
平均成本          ¥0.42
未处理 Bad Case    12
异常 Provider       1
```

---

## 3.3 增加时间范围

所有主要指标都应该支持：

```text
最近 1h
最近 24h
最近 7d
最近 30d
```

并展示趋势：

```text
成功率      93% ↑3%
P95         68s ↓21%
降级率       8% ↑2%
平均成本   ¥0.38 ↓5%
```

不要只显示历史累计总数。

---

## 3.4 增加“当前需要关注”

Dashboard 最重要的部分应该是：

```text
当前需要关注
```

例如：

```text
⚠ TikHub 最近失败率 32%
⚠ 3 个正式 Run 超过 120 秒
⚠ 酒店质量 Bad Case 过去 7 天出现 18 次
⚠ 美食相关 Bad Case 本周增长 40%
⚠ 4 个 Run 忽略了用户住宿偏好
```

目标是让管理员一打开后台就知道：

> 现在最应该修什么。

---

# 4. 新增 Travel Quality 页面

这是最应该新增的产品质量页面。

它解决：

> 系统虽然成功生成了行程，但生成得好不好？

建议结构：

```text
Travel Quality

├─ Preference Match
├─ Hotel Quality
├─ Food Quality
├─ Itinerary Quality
├─ Route Quality
└─ Hard Validation
```

---

## 4.1 Preference Match

检查：

```text
用户说了什么
↓
Dynamic Preference Profile 是什么
↓
最终计划是否体现出来
```

例如：

```text
用户：
喜欢美食 / 商圈 / 热闹

Dynamic Preference：
food_density      HIGH
commercial_area   HIGH
nightlife         HIGH

最终住宿：
春熙路

结果：
Preference Match = GOOD
```

如果用户说：

```text
商圈 / 美食优先
```

最后却住到偏远区域，就应该产生：

```text
PREFERENCE_MISMATCH
```

---

## 4.2 Hotel Quality

重点看：

```text
住宿区域是否符合用户偏好
酒店是否位于选定住宿区域
到商圈距离
到美食区域距离
到地铁距离
到主要景点平均距离
价格 / 评分
攻略推荐度
```

示例：

```text
住宿区域：春熙路
酒店：XX酒店

商圈距离：350m
地铁：280m
高质量餐厅：17 家 / 1.5km
主要景点平均通勤：26min
评分：4.7
价格：¥420

Hotel Quality：GOOD
```

---

## 4.3 Food Quality

不要只判断有没有餐厅。

应该监控：

```text
Meal Slot 覆盖率
真实餐厅比例
Evidence 覆盖率
具体菜品覆盖
平均绕路距离
营业时间冲突
重复餐饮类型
广告风险
```

例如：

```text
6 个午餐/晚餐槽位
5 个真实餐厅
Evidence 覆盖：83%
平均绕路：620m
营业时间冲突：0
```

---

## 4.4 Itinerary Quality

检查：

```text
每天景点数量
区域集中度
节奏
首末日利用
自由时间
MUST 覆盖
用户兴趣覆盖
```

例如：

```text
Day 1：武侯区
Day 2：熊猫基地 + 春熙路
Day 3：宽窄巷子

平均每天 2.7 个核心点
MUST 覆盖：100%
区域聚类：GOOD
```

---

## 4.5 Route Quality

检查：

```text
平均每天移动距离
平均单段通勤
最长单段通勤
明显折返
未验证路线
高德 Route 覆盖率
```

---

## 4.6 Hard Validation

只看真正不能违反的问题：

```text
时间冲突
营业时间冲突
返程赶不上
REJECT 出现
MUST 无故丢失
硬预算超限
地点 / 酒店真实性
```

---

# 5. 新增 Planning Funnel

新的用户主流程会越来越依赖 Guided Mode，因此必须能看到用户在哪一步流失。

建议：

```text
Session Created
↓
Discovery Started
↓
Discovery Ready
↓
用户完成偏好
↓
用户完成 POI 选择
↓
用户确认
↓
Run Started
↓
Plan Success
```

后台展示：

```text
100 Session 创建
↓
96 Discovery 启动
↓
92 Discovery 完成
↓
84 完成偏好选择
↓
79 完成 POI 选择
↓
73 点击开始规划
↓
69 成功生成
```

同时标出：

```text
最大流失：
探索确认 → 开始规划
-8.2%
```

还可以按：

```text
目的地
设备
Quick / Guided
日期
```

做简单筛选。

---

# 6. Runs 页面简化

## 6.1 当前问题

当前列表展示的信息过多：

```text
Run
状态
来源
原始请求
时间
耗时
Token
LLM / Jev / Tool
Provider 失败
Bad Case
成本
```

一行太密，运营人员很难快速扫描。

---

## 6.2 默认列表建议

只展示：

```text
状态
用户请求 / 目的地
来源
耗时
质量
问题
成本
开始时间
```

例如：

```text
SUCCESS | 北京→成都 3天 | Guided | 61s | 82 | 1 Warning | ¥0.31
```

点击 Run 后再看：

```text
Token
LLM
Tool
Provider
Trace
```

技术指标下沉到详情，不要全部挤在列表。

---

# 7. Planning Sessions 优化

当前 Planning Session 页面方向是对的，但有一个实际问题：

现在搜索存在“当前页过滤”的兜底逻辑。

例如数据库有：

```text
500 个 Session
```

当前只拿：

```text
20 条
```

搜索一个第 200 条的 Session 时，前端当前页过滤可能会错误显示：

```text
没有找到
```

建议后端正式支持：

```text
GET /admin/planning-sessions?q=xxx
```

搜索范围必须是全库，而不是当前分页。

---

## Session Detail 建议重点展示

不要只展示原始 JSON。

建议按用户旅程组织：

```text
基础信息
↓
用户偏好
↓
Dynamic Preference Profile
↓
Discovery
↓
Prefetch
↓
用户 POI 选择
↓
确认
↓
正式 Run
```

让管理员能看清：

> 用户到底选了什么，最后系统有没有遵守。

---

# 8. Provider Health 简化

## 8.1 当前问题

Provider 卡片一次展示很多：

```text
最近调用
最近成功
最近失败
调用次数
成功率
失败率
Timeout
Fallback
平均延迟
P95
来源
工具
最近错误
```

信息完整，但扫描效率低。

---

## 8.2 建议卡片默认只展示

例如：

```text
TikHub                     DEGRADED

成功率       72%
P95          12.4s
Timeout      8
Fallback     4

最近错误：
TIMEOUT
```

点击详情后再展示：

```text
完整工具
调用来源
最近 200 次调用
Raw Error
延迟分布
```

---

# 9. Bad Cases 升级成“问题中心”

## 9.1 当前问题

现在更像：

```text
一条 Bad Case
一条 Bad Case
一条 Bad Case
```

当数据多以后很难知道：

> 这些其实是不是同一个问题？

---

## 9.2 应该自动聚类

例如：

```text
酒店区域选择不合理

Severity：HIGH
过去 7 天：18 次
影响 Run：14
趋势：↑80%

主要目的地：
成都 7
重庆 4
杭州 3

关联模块：
hotel_area_selection

最近一次：
18分钟前
```

点击后再展开相关 Bad Cases / Runs。

---

## 9.3 推荐增加字段

```text
issue_group
occurrence_count
affected_runs
first_seen
last_seen
trend
related_module
related_provider
```

目标：

```text
100 条 Bad Case
↓
聚合成 8 个真正需要解决的问题
```

---

# 10. Jev 在后台降级

按照新的 TravelPlan 设计：

```text
Jev = Optional Enhancement
```

不应该继续作为后台核心健康指标。

建议主指标改成：

```text
Dynamic Preference Profile 成功率
LLM Soft Ranking 成功率
Soft Ranking fallback 次数
Dynamic Weight fallback 次数
Hard Validation reject 次数
```

Jev 相关信息移动到：

```text
Advanced / Experimental
```

Benchmark 中可以继续保留 Jev 对比，但不能让系统质量依赖 Jev。

---

# 11. Run Detail 重构

建议：

```text
Run Detail

概览 | 轨迹 | 性能 | Provider | Bad Case
```

---

## 11.1 概览

第一屏直接回答：

```text
用户要什么？
系统最后给了什么？
质量怎么样？
哪里有问题？
```

展示：

```text
用户请求
TripIntent
Dynamic Preference Profile
住宿区域
酒店
核心景点
餐厅
预算
状态
Quality
Warnings
Bad Cases
```

---

# 12. 新增 Agent Execution Timeline

当前 Trace 偏工程结构，需要升级成人能直接阅读的运行轨迹。

目标参考：

```text
系统
用户
上下文
助手
工具
Provider
Validation
错误
```

按真实执行顺序展示。

---

## 12.1 顶部轨道概览

例如：

```text
模型        ■     ■        ■      ■
工具          ■ ■ ■        ■ ■
Provider      ■ ■ ■        ■ ■
错误                         !
```

一眼看：

```text
什么时候调用模型
什么时候调用工具
哪里最慢
哪里失败
哪里发生 fallback
```

点击一个节点定位下方详细事件。

---

# 13. 详细轨迹时间线

示例：

```text
系统
初始系统提示词

用户
北京去成都玩3天，主要想吃东西...

上下文
PlanningSession
PrefetchBundle
交通候选 18
酒店候选 42
Evidence 26
POI 34

助手
生成 Dynamic Preference Profile
food_density = high
commercial_area = high
nightlife = medium

工具
TikHub · search_xiaohongshu
SUCCESS
12.4s
返回 18 条

工具
Amap · search_poi
SUCCESS
2.1s
返回 12 个 POI

Provider
Tuniu Hotel
TIMEOUT
8.2s
→ fallback

助手
生成 Top-K Plan
3 个候选

Validation
Python Hard Validation
PASS

助手
Finalize
SUCCESS
```

默认只展示摘要，点击后看完整 Input / Output。

---

# 14. Trace Event 统一类型

建议统一：

```text
SYSTEM
USER
CONTEXT
ASSISTANT
TOOL
PROVIDER
VALIDATION
ERROR
```

---

## SYSTEM

展示：

```text
System Prompt
Runtime 配置
Workflow Stage
模型配置
关键策略
```

---

## USER

展示：

```text
用户原始输入
Guided 结构化选择
MUST / WANT / REJECT
```

---

## CONTEXT

展示：

```text
PlanningSession
PrefetchBundle
Discovery
Memory / RAG
Dynamic Preference Profile
上一 Stage 输出
```

---

## ASSISTANT

展示模型行为：

```text
Intent Parse
Query Expansion
Place Extraction
Dynamic Preference Generation
Soft Ranking
Critic
Finalize
```

每次模型调用至少显示：

```text
模型
Tag
Input Token
Output Token
耗时
状态
输出摘要
```

---

## TOOL

展示：

```text
Tool 名称
Args
Result 摘要
Duration
Cache Hit
Prefetch Reused
Fallback
```

例如：

```text
amap.search_poi

query = 成都 火锅
duration = 1.8s
cache_hit = false
prefetch_reused = false
status = SUCCESS
```

---

## PROVIDER

展示底层数据源：

```text
TikHub
Tuniu
12306
Amap
Tavily
MediaCrawler
```

重点标记：

```text
SUCCESS
EMPTY
TIMEOUT
AUTH ERROR
FALLBACK
RETRY
```

---

## VALIDATION

展示真正的硬校验：

```text
Route
Opening Hours
Return Deadline
Hard Budget
REJECT
MUST
```

---

## ERROR

统一展示：

```text
Exception
Timeout
Provider Failure
Degradation
Fallback
Bad Case Trigger
```

---

# 15. Trace Event 数据结构

建议统一为：

```text
event_id
run_id
round_index
event_type
stage

title
summary

input_preview
output_preview

status

started_at
finished_at
duration_ms

parent_event_id

model
provider
tool

tokens_in
tokens_out
cost

cache_hit
prefetch_reused
fallback

metadata_json
```

不要让：

```text
LLM
Tool
Provider
Workflow
```

各自定义不同 Trace 结构。

---

# 16. Trace 页面交互

支持：

```text
全部
模型
工具
Provider
Validation
错误
```

以及：

```text
Workflow Stage Filter
Status Filter
搜索
```

例如搜索：

```text
hotel
TikHub
TIMEOUT
Prefetch
春熙路
```

可以快速定位相关节点。

---

# 17. Trace 默认展示原则

默认不要铺满 JSON。

只展示：

```text
类型
标题
摘要
状态
耗时
```

点击：

```text
展开详情
```

才显示：

```text
Input
Output
Args
Raw Result
Metadata
```

---

# 18. Trace 与性能联动

轨迹页回答：

> 这次 Run 发生了什么？

性能页回答：

> 哪里最慢？

两者应该互相跳转。

例如性能页：

```text
search_social_guides
12.4s
```

点击后直接定位轨迹：

```text
TikHub
Web Search
Fallback
Place Extraction
```

轨迹里的 Tool Call 也可以跳到对应性能详情。

---

# 19. Trace 与 Bad Case 联动

如果 Run 产生：

```text
HOTEL_AREA_MISMATCH
FOOD_LOW_QUALITY
PREFERENCE_IGNORED
ROUTE_BACKTRACKING
```

轨迹里直接标记对应节点。

例如：

```text
助手
选择住宿区域：天府广场

⚠ Bad Case
用户明确“美食 / 商圈优先”，
但最终区域 food_density 较低。
```

点击：

```text
查看 Bad Case
```

这样才能回答：

> 为什么结果不好？

---

# 20. 敏感信息保护

轨迹里可能出现：

```text
System Prompt
Tool Args
Raw Response
HTTP Error
```

必须统一脱敏：

```text
API Key
Token
Authorization
Cookie
Password
Secret
```

Raw Result 同时做：

```text
长度限制
结构化截断
```

---

# 21. 实现原则

不要重新建立另一套日志系统。

优先复用现在已经存在的：

```text
TraceCollector
Provider Calls
LLM Calls
Workflow Stage
Decision
PlanningSession
Prefetch
Bad Case
```

增加一层：

```text
现有 Observability
↓
Trace Event Adapter
↓
统一 Timeline API
↓
管理端轨迹页
```

避免重复记录同一份数据。

---

# 22. 推荐开发优先级

## P0

先做：

```text
1. 重构 Dashboard 首屏
2. 增加时间范围和趋势
3. 增加“当前需要关注”
4. Runs 列表简化
5. Planning Session 搜索改为后端全局搜索
6. Run Detail 新增轨迹 Timeline
```

## P1

然后做：

```text
7. Travel Quality 页面
8. Planning Funnel
9. Bad Case 聚类
10. Provider Health 简化
11. Trace / Performance / Bad Case 联动
```

## P2

最后：

```text
12. 更完整质量趋势
13. 目的地维度分析
14. 成本趋势
15. 自动生成问题摘要
16. 自动给出关联模块建议
```

---

# 23. 最终目标

现在后台更接近：

```text
大量运行数据
+
工程指标
+
Trace
```

最终应该变成：

```text
现在系统健康吗？
↓
现在最严重的问题是什么？
↓
哪些用户受影响？
↓
哪类旅行质量下降？
↓
打开一个 Run
↓
看到用户需求和最终结果
↓
沿轨迹定位到具体决策 / Tool / Provider
↓
找到对应 Bad Case
↓
Benchmark 验证修复
↓
Evolution / 人工优化
```

核心目标：

> **把 TravelPlan Admin 从“Agent 工程监控台”，升级成“产品质量 + Agent 可观测性 + 问题诊断”的完整控制台。**
