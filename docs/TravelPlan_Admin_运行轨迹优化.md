# TravelPlan 管理后台：运行轨迹与可观测性优化

## 1. 当前问题

现在 TravelPlan 管理后台已经能看到 Run、Provider、Bad Case、Benchmark、Evolution 等信息，但 `Run Detail` 的可观测性仍然偏“工程数据”，不够像一个真正可读、可诊断的 Agent 运行轨迹。

当前主要问题：

- Trace 更像 JSON / 树结构，阅读成本高
- 看不到完整的“系统 → 用户 → 上下文 → 助手 → 工具 → Provider → 结果”过程
- 很难快速回答：系统提示词是什么、用户输入了什么、Prefetch / Context 注入了什么、模型每一轮做了什么、调用了哪些工具、哪个 Provider 最慢或失败、哪一步发生 fallback / timeout、最终结果为什么变差
- 模型调用、工具调用、Provider 调用之间缺少统一时间轴
- 性能问题和质量问题需要在多个页面之间来回找

目标不是增加更多原始日志，而是把现有 Trace 变成一个**人可以直接读懂的 Agent Execution Timeline**。

## 2. 优化目标

在：

```text
/admin/runs/[run_id]
```

新增一个真正的“轨迹”视图。Run Detail 顶部建议改成：

```text
概览 | 轨迹 | 性能 | Provider | Bad Case
```

其中“轨迹”作为主要诊断入口。

## 3. 轨迹视图整体结构

### 3.1 顶部：时间轴概览

参考 Agent 调试工具的轨迹展示方式，按时间顺序展示不同类型事件：

```text
模型       ■   ■       ■     ■
工具         ■ ■ ■       ■ ■
Provider     ■ ■ ■       ■ ■
错误                      !
```

用颜色或图标区分：

```text
系统
用户
上下文
助手
工具
Provider
错误 / 降级
```

悬停或点击显示：名称、Stage、开始时间、耗时、状态、Token / Cost、Provider 名称。

目标是一眼看出：哪里调用最密集、哪里耗时最长、哪里失败、哪里发生 fallback。

## 4. 下方：详细事件时间线

按照真实执行顺序展示，例如：

```text
系统
初始系统提示词

用户
北京去成都玩3天，主要想吃东西...

上下文
加载 PlanningSession / PrefetchBundle
交通候选 18
酒店候选 42
Evidence 26
POI 34

助手
生成 Dynamic Preference Profile
food_density: high
commercial_area: high
nightlife: medium

工具
TikHub · search_xiaohongshu
12.4s
SUCCESS
返回 18 条

工具
Amap · search_poi
2.1s
SUCCESS
返回 12 个 POI

Provider
Tuniu Hotel
8.2s
TIMEOUT
→ fallback

助手
生成候选行程 Top-K
3 个方案

系统
Python Hard Validation
PASS
```

默认展示摘要，点击后展开完整详情。

## 5. Trace Event 类型

统一定义：

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

### SYSTEM
记录 System Prompt、Runtime 配置、模型配置、Workflow Stage、关键策略参数。

### USER
记录用户原始输入、Guided Mode 最终确认信息、MUST / WANT / REJECT。

### CONTEXT
记录 PlanningSession、PrefetchBundle、Discovery 结果、Memory / RAG、Dynamic Preference Profile、上一 Stage 输出。默认只显示摘要，不直接展开大块 JSON。

### ASSISTANT
记录 Intent Parse、Query Expansion、Place Extraction、Dynamic Preference Generation、Ranking、Critic、Finalize。每次模型调用展示模型、Prompt 类型、输入/输出 Token、耗时、状态、输出摘要。

### TOOL
记录 Tool 名称、Args、Result 摘要、Duration、Cache Hit、Prefetch Reused、Fallback。

例如：

```text
amap.search_poi
query = 成都 火锅
cache = false
prefetch_reused = false
duration = 1.8s
```

### PROVIDER
记录 TikHub、Tuniu、12306、Amap、Tavily、MediaCrawler 等底层数据源状态，重点区分 SUCCESS、EMPTY、TIMEOUT、AUTH ERROR、FALLBACK、RETRY。

### VALIDATION
记录 Python 最终硬校验，例如 Route、Opening Hours、Return Deadline、Budget Hard Limit、REJECT、MUST。

### ERROR
统一展示 Exception、Timeout、Provider Failure、Degradation、Fallback、Bad Case Trigger。

## 6. 每条 Trace Event 建议字段

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

不要让不同模块各自定义一套 Trace 格式。

## 7. 前端交互

轨迹页支持：

```text
全部
只看模型
只看工具
只看 Provider
只看错误
只看某个 Workflow Stage
```

同时支持关键词搜索，例如：

```text
hotel
TikHub
TIMEOUT
Prefetch
春熙路
```

快速定位相关事件。

## 8. 默认展示原则

不要默认展开大量 JSON。默认只显示：

```text
类型
标题
摘要
状态
耗时
```

点击“展开详情”后再看 Input、Output、Args、Raw Result、Metadata。

技术细节保留，但不影响正常阅读。

## 9. 敏感信息

必须继续脱敏：

```text
API Key
Token
Authorization
Cookie
Password
Secret
```

System Prompt 可以展示，但 Secret 必须先 scrub。Provider Raw Response 也必须经过脱敏和长度限制。

## 10. 和性能页面联动

轨迹页负责回答：

> 这次 Run 到底发生了什么？

性能页负责回答：

> 哪里最慢？

两者应该互相跳转。例如性能页面里的 `search_social_guides 12.4s`，点击后直接定位轨迹里的 TikHub search、Web search、Fallback、Place extraction；反过来点击某次 Tool Call，也能查看性能详情。

## 11. 和 Bad Case 联动

如果某个 Run 产生：

```text
HOTEL_AREA_MISMATCH
FOOD_LOW_QUALITY
PREFERENCE_IGNORED
ROUTE_BACKTRACKING
```

轨迹中要标记对应节点，例如：

```text
助手
选择住宿区域：天府广场

⚠ Bad Case
用户明确“美食 / 商圈优先”，
但最终区域 food_density 较低。
```

点击可以直接跳 Bad Case 详情。这样才能真正回答“为什么这次结果不好”。

## 12. Run Detail 推荐最终结构

```text
Run Detail

[概览]
用户请求
状态
目的地
耗时
成本
质量
Bad Case

[轨迹]
完整 Agent Execution Timeline

[性能]
Stage / LLM / Tool / Provider latency

[Provider]
调用次数
成功率
Fallback
错误

[Bad Case]
本次 Run 触发的问题
```

## 13. 实现原则

不要重新建立一套独立日志系统。优先复用当前：

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

统一转换成 `TraceEvent` 给前端展示：

```text
现有 Observability 数据
↓
Trace Event Adapter
↓
统一 Timeline API
↓
前端轨迹页面
```

## 14. 最终目标

当前管理端更多是在回答：

```text
系统调用了什么？
```

优化后应该能回答：

```text
用户说了什么
↓
系统拿到了什么上下文
↓
模型怎么理解
↓
调用了什么工具
↓
真实数据返回什么
↓
为什么做这个决定
↓
哪里失败 / fallback
↓
最终为什么得到这个结果
```

核心目标：

> **把 Trace 从“工程日志”升级成“可阅读、可定位、可诊断的 Agent 运行轨迹”。**
