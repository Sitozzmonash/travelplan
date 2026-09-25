# Agent Loop 收敛：已完成项与后续优化清单（2026-09-25）

> 状态：P0 子集已实施并上线（commit `d435abe`/`643f8de`/`3c39be2`，Render Live）。
> 本文记录**尚未实施**的 5 项优化——问题描述 + 方案，供下一轮排期。来源：
> 用户提供的《TravelPlan_AgentLoop_超时与收敛优化方案.md》+ 2026-09-25 评审（评审意见
> 含对原方案的修正，已吸收进下文）。

## 背景：已落地的 P0（本文件的前提）

1. **交卷即停**：`submit_final_plan` 通过校验后立即干净结束（`_SubmissionEndGuard` 放
   middleware 最外层），不再有交卷后的多余"在思考行程"；校验失败仍可让 agent 修一轮。
   语义决策：**交卷成功是终态，优先于预算/超时截断**。
2. **max_llm_calls / max_tool_calls 真正生效**（`_CallBudgetGuard`，默认 None=不启用）。
3. **单次 LLM 超时**接入（`request_timeout=llm_timeout_seconds`，默认 180）。
4. **`_prefetch_digest` 补坐标/地址/营业时间/区划**，减少对已预热地点的重复查询。
5. **search_trains 缓存完整 ProviderResult**、key 去 top_n 按需切片。
6. **prompt 收敛约束 v3**：同目标最多 2 次 / 不许改用户日期 / 事实够就交卷。
7. 控制流异常改继承 `(ModelError, RuntimeError)`：修掉 ModelRetryMiddleware 对控制流
   异常白重试的隐患（实测 +2.9s、llm_calls 虚增）。

---

## 未做项 1：Batch POI Detail / Batch Routes（减步数，治本）

### 问题描述

Agent 每查一个景点详情或一段路线 = "模型想一次 → 调一次工具"一个往返。一天 5 个景点
就是 5 次 `poi_detail` + 4 次 `route` ≈ 9 个往返，每个往返都烧一次 LLM（单轮实测
4~68s）。**每步一次 LLM 往返 × 步数 = 总耗时**，这是 600 秒超时的根因（40 步 × 平均
15s ≈ 600s）。单个工具的超时上限再调也是治标。

### 优化方案

新增两个批处理工具，替换逐点调用：

```text
get_place_details(place_ids=[...])   # 一次查多个地点的详情
get_routes([{from:A,to:B},{from:B,to:C},...])   # 一次算多段路线
```

内部：Cache First → 只查缺失 → Python 受控并发 → 一次返回。原则：
**LLM 决定 WHAT，Python Tool 决定 HOW efficiently**。一天 5 点 9 段的场景步数从 ~9 降到 1~2。

注意：新增工具 = 契约改动，`TRAVEL_PLANNER_SYSTEM_PROMPT` 自查清单 + benchmark 的
`expect_*` 断言 + 前端步骤文案（`TOOL_DISPLAY_NAMES`）要同步；建议保留单点工具做兜底
（模型可能只传一个）。

### 优先级

P1。做完交卷即停后步数已降一截，先看线上 `llm_calls` 分布再决定要不要做。

---

## 未做项 2：Deadline 分阶段工具收窄（60/80/90%）

### 问题描述

`_AlwaysVisibleTools` 每轮把全部旅行工具暴露给模型（能力筛选后的补回），副作用是
agent 在任何时候都能重新进入 Research——排完 Day 3 仍可重搜小红书/酒店/POI，循环容易
扩大。600 秒全程工具权限一样，没有"越接近截止越该收手"的渐进压力。

### 优化方案

按剩余时间收窄**可见工具列表**（不是 phase 状态机，避免退化成半固定 workflow）：

```text
60% deadline（360s）：剔除 research 类（search_xiaohongshu/search_douyin/web_search/
   search_poi/search_hotels），只留 plan 类（poi_detail/route/search_scenic_tickets/
   check_budget/check_feasibility/submit_final_plan）
80% deadline（480s）：只留 check_budget/check_feasibility/submit_final_plan
   （允许极少量关键 route/detail）
90% deadline（540s）：FINALIZE_ONLY，至少留 60s 交卷
```

实现：一个 middleware 持有 `started_at`，在每轮工具可见列表组装时按剩余时间过滤
（与 `_AlwaysVisibleTools` 的补回逻辑配合）；配置化 `{60, 80, 90}` 阈值。

### 优先级

P1。交卷即停已让"超时没交卷"大幅减少，收益被稀释；且"可见列表动态变化"与现固定暴露
机制是两套逻辑，测试成本中等。

---

## 未做项 3：No-progress 检测（空转检测）

### 问题描述

agent 可能连续多轮"查了但没拿到新东西"（同批候选反复查、检索词一直在失败重试），
600 秒预算被空转烧掉。目前没有"这一轮相比上一轮有没有实质进展"的判断。

### 优化方案

每轮记录**确定性事实增量**（不要 facts_hash，哈希抖动会误判）：

```text
新增 canonical place 数 / 新增 route 数 / 新增 price 数 / 新增 evidence 数
```

连续 2 轮增量为 0 → 注入一条系统提示：

```text
NO_PROGRESS：连续两轮没有获得新的有效事实。禁止继续广泛搜索；
请使用现有数据完成行程，缺失信息标 unknown/warning，然后进入 Finalize。
```

**关键语义（评审修正）**：no-progress 在"信息已完备"场景是正常状态（本来就该交卷了），
所以它触发的动作是**引导交卷**（prompt 提示用现有数据 submit），不是报错、不是继续。
计数必须用确定性的字段增量，避免 hash 抖动误伤正常流程。

### 优先级

P1。最难做对的一项；prompt 的"事实够就交卷"已先顶替了部分语义，若线上"超时且步数打满"
仍常见再做。

---

## 未做项 4：Timeout 保留草稿为 DEGRADED（Incremental Draft）

### 问题描述

agent 跑到 599 秒没交卷 = 整单 FAILED，之前查的交通/酒店/Day1 全白费（评审实测过
`tp-20260924-062247` 这类 run 里交通、酒店、多天行程其实早已查完）。"跑很久但没交卷"
不应该等于"什么都没做"。

### 优化方案（评审修正：先做轻量版，不做 checkpoint 管线）

第一版不做"关键节点存草稿"的中间产物管线（太重），改为：

```text
超时/截断时，若 agent 已调过 submit_final_plan（holder 里有一份 plan，即使 Python
校验带 warning）→ 落成 DEGRADED 而不是 FAILED，缺失字段标 unknown/warning。
```

即"至少保住 agent 交过的卷"，从"交卷后的二次校验失败"到"未交卷"之间没有落差。
完整版（交通定→存一版、Day1 完→存一版）留到 P2，需要：中间草稿数据结构、部分计划的
可行性校验、前端 DEGRADED 完整展示形态（哪些能看哪些是空）。

### 优先级

P2。改动最大（动管线结构 + 前端）；交卷即停后正常路径不再超时，超时只剩"真没交卷"
的异常场景，频率低。

---

## 未做项 5：Cancellation Token（同步代码降级为"快速失败 + 防写"）

### 问题描述

外层超时后请求虽然能返回，但底层同步 Tool / 线程理论上可能继续跑一会儿、继续写状态
（僵尸线程把页面写成"正在查酒店"）。MD 原案要"真取消"。

### 优化方案（评审修正：真取消不可行，降级）

项目是**同步 Provider + asyncio.wait_for + 守护线程 join 预算**（已实现两层超时，
`app/agent_runner.py::_run_agent_loop`），同步代码无法安全杀线程。降级为：

1. 超时/用户取消/预算触发 → `RunControl.cancelled` 置位；
2. Provider 包装层每次调用前检查标志，发现取消快速返回失败（不强制中断）；
3. run 已 FAILED/DEGRADED/COMPLETED 后，reporter 禁止再写状态（僵尸线程防写）。

### 优先级

P2。现有双层超时已保证"请求按时返回、不挂死"；底层线程多跑一会儿目前只写临时产物、
无实际危害。真取消在同步架构下不做。

---

## 附：与本次无关的历史测试失败（已上线代码）

`tests/test_agent_trace.py::test_model_call_records_scrubbed_200_character_previews` 与
`tests/test_discovery_handoff.py::test_agent_results_are_not_reported_as_unavailable_discovery_handoff`
在 HEAD 上失败（`workflow.py:811 SimpleNamespace.transport_mode`），由 2026-09-24 的
`2005be4`（agent_trace/workflow/api 改动）引入且已上线。属历史遗留，待确认是否真 bug。
