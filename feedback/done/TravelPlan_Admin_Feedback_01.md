# TravelPlan Admin Feedback

## 1. Dashboard 加载太慢

### 问题
现在管理后台 Dashboard 打开仍然需要十多秒，等待时间太长。主要原因是页面加载时需要临时读取和聚合 Runs、Plan、Bad Case、Provider 等多类数据，并且会产生多次 Neon 数据库往返。

### 建议优化
把常用聚合结果提前保存或做短时间缓存，减少每次打开页面时重新计算全部数据。

优先考虑：

- Run 完成后直接保存常用质量摘要
- Dashboard 聚合结果做 30～60 秒缓存
- 减少跨 Neon 的重复查询
- 能用一条聚合 SQL 完成的不要拆成多次请求

目标：

```text
Dashboard：2～3 秒内打开
Travel Quality：3 秒左右
```

---

## 2. Travel Quality 仍然使用固定权重

### 问题
现在质量评分虽然比以前完整，但还是使用固定权重。

例如不同用户的旅行重点完全不同：

```text
用户 A：主要为了吃、逛商圈
用户 B：带老人、少走路
用户 C：主要拍照和景点
```

如果三种用户都使用同一套固定质量权重，最终评分不能真正代表“这次旅行对这个用户来说好不好”。

### 建议优化
质量评分应该结合本次旅行的 `Dynamic Preference Profile` 动态调整。

例如：

```text
美食型用户
→ 美食质量 ↑
→ 商圈 / 酒店位置 ↑
→ 夜间便利 ↑

老人轻松型
→ 路线距离 ↑
→ 交通便利 ↑
→ 节奏舒适 ↑
→ 每天景点数量权重 ↓

景点型用户
→ 景点兴趣匹配 ↑
→ MUST 覆盖 ↑
→ 区域规划 ↑
```

Python 继续负责真实、时间、路线、营业时间等不能违反的问题；软质量分则根据用户本次偏好动态计算。

核心目标：

> 质量分不是“系统统一认为这趟旅行好不好”，而是“这趟旅行是否适合这个用户”。

---

## 3. Run Trace 轨迹还不够完整

### 问题
现在轨迹已经能展示：

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

方向是对的，但目前最重要的缺口是：

```text
模型实际收到了什么
模型实际返回了什么
```

现在很多 ASSISTANT 事件只能看到调用标签、模型、耗时、字符数等信息，还不能完整还原一次模型交互。

因此出现坏结果时，仍然很难快速判断：

- Prompt 给错了吗
- Context 注入错了吗
- Dynamic Preference 是否传给模型了
- 模型理解错了吗
- Tool Result 是否真的进入下一轮模型上下文

### 建议优化
每次 LLM 调用安全保存一份脱敏后的预览：

```text
system_preview
user_preview
context_preview
assistant_preview

prompt_version
prompt_hash

input_tokens
output_tokens
duration_ms
```

要求：

- API Key、Token、Cookie、Secret 必须脱敏
- 内容做长度限制，避免数据库过大
- 默认只显示摘要，点击后再展开
- 保留完整的真实执行顺序

前端建议同时提供两种视图：

```text
Workflow View
→ 按 12 个 Stage 看执行过程

Conversation View
→ System
→ User
→ Context
→ Assistant
→ Tool
→ Tool Result
→ Assistant
```

这样才能真正做到：

> 打开一个坏 Run，就能顺着轨迹看到它为什么最后做出了这个决定。
