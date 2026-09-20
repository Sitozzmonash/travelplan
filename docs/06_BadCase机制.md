# 06 BadCase 机制

一句话：用规则把"这次 run 确实出了问题"变成结构化记录，供人复核、聚类、回归。

代码：`app/badcase.py`（检测）+ `app/store.py`（落库）+ `app/api.py`（管理端接口）。
**Bad Case 检测不调用任何模型** —— 让模型判断"这次是不是坏了"会产生一批无法复现的记录，
把回归集污染掉。

## 1. 生命周期

```text
Run 结束（finalize）
  → 规则检测（app/badcase.py:detect_badcases）
  → 写 badcases 表（analysis_status=pending）+ outputs/<run_id>/badcases.json
  → 计数进 run_metrics.badcase_count
  → 管理端 Bad Cases 页复核 / PATCH 状态
  → Evolution 只处理 analysis_status=pending 的记录（见 08）
  → 确认过的失败场景固化进 benchmark/cases/badcase_regression.jsonl（永久回归）
```

`badcase_id` 是 `(run_id, category, symptom)` 的稳定哈希（`bc-{run_id}-{sha1[:10]}`），
因此同一次 run 反复检测会命中同一行、重跑同一条 run 不会把记录越积越多。

## 2. 字段

| 字段 | 含义 |
| --- | --- |
| `badcase_id` | 稳定主键（见上） |
| `run_id` | 出问题的 run（外键到 `runs`） |
| `category` | 类别（见下表） |
| `severity` | `low` / `medium` / `high` |
| `symptom` | 症状摘要（聚类指纹的来源） |
| `expected` | 期望是什么 |
| `actual` | 实际是什么 |
| `suspected_root_cause` | 疑似根因（规则给的推测） |
| `root_cause_status` | `suspected` / `verified` / `rejected`（默认 `suspected`，只由人或 Evolution 改） |
| `detected_by` | `rule` / `human` / `benchmark` / `llm_judge`（当前实现只产出 `rule`） |
| `trace_refs` | 相关 span id 列表（可直接在 trace 里定位） |
| `analysis_status` | `pending` / `analyzed` |
| `fixed_status` | `unfixed` / `fixed` / `wontfix` |
| `introduced_in` | 引入版本（`app/version.py:travelplan_commit()`） |
| `fixed_in` | 修复版本（人工填写） |
| `created_at` | 产生时间 |

## 3. 全部类别与触发条件

### Provider / 降级

| category | severity | 触发条件 |
| --- | --- | --- |
| `provider_failure` | 关键 Provider（12306 / tuniu / amap）high，其余 medium | 任一 Provider 调用 `status != OK`（每次 (provider, tool) 一条） |
| `degradation` | medium | 本次 run 有任何降级说明（模型/Provider/Jev 不可用），整体记一条 |

### 硬约束 / 数据完整性

| category | severity | 触发条件 |
| --- | --- | --- |
| `hard_constraint` | high | `plan.warnings` 里存在 `severity=error 且未解决` 的项（每条一条）；或 plan 为空 |
| `budget` | medium | `budget.status == over_budget` |
| `budget_math` | high | `breakdown` 各项之和与 `projected_total` 相差 ≥ 1 元 |
| `missing_disclosure` | high | `transport.selected` 或 `hotel.selected` 为空（没披露交通/住宿） |
| `hallucinated_price` | high | `price_type=realtime` 但 `source_ids` 为空（实时价没有来源） |
| `evidence_gap` | low | 有安排项没有任何 evidence 关联（**聚合为一条**，示例名写在 `actual`） |
| `route_unverified` | low | 相邻两点路线未核实且为估算值（**聚合为一条**） |

> 低严重度的逐项问题聚合成一条是有意的：一次 5 天行程有十几个安排，逐项各记一条会让
> 单次 run 产出几十条（实测 61 条），管理端翻不动、Evolution 也只能看到一堆单例簇。

### Jev（7 类，任务明确要求）

| category | severity | 触发条件 |
| --- | --- | --- |
| `jev_timeout` | medium | Jev 调用返回 `TIMEOUT` |
| `jev_quota` | medium | `BUDGET_EXCEEDED` / `HTTP_429` / `HTTP_402` / `FREE_CREDIT_EXHAUSTED` |
| `jev_invalid_response` | medium | 返回缺少有效 choice/confidence |
| `jev_low_confidence` | low | 置信度低于 `JEV_MIN_CONFIDENCE` |
| `jev_wrong_choice` | high | Jev 选中的方案**未通过 Python 硬约束复核**，被退回（`plan_choice.rejected`） |
| `jev_unnecessary_replan` | medium | 没有任何硬问题时 Jev 却要求 REPLAN |
| `jev_missed_replan` | high | 存在硬问题时 Jev 却判定 KEEP |

`JEV_CATEGORIES` 常量列出了这七类，管理端据此分组。

### 用户旅程（8 类，`app/badcase.py:JOURNEY_CATEGORIES` / `_journey_cases`）

| category | severity | 触发条件 |
| --- | --- | --- |
| `rejected_poi_in_plan` | **high** | 最终行程里出现了用户标 `REJECT` 的 place_id（`journey.rejected_in_plan` 非空） |
| `must_poi_missing` | **high** | 用户标 `MUST` 的点没有进入行程（`journey.must_missing` 非空）；`actual` 会带上 `day.notes` 里"为什么没排上"的原话 |
| `prefetch_failed` | medium | Discovery 四条线里任一 `status=FAILED` |
| `prefetch_not_reused` | medium | Discovery 已查到的线里，正式 run 又自己查了一遍（账本里出现交通/酒店/社交/搜索工具） |
| `discovery_empty` | high | Discovery 四条线的 `result_count` 全为 0 |
| `user_preference_ignored` | medium | 用户选了「必去/想去」但没有 `must_missing`/`rejected_in_plan` 时，若一个都没落进行程 |
| `discovery_stale` | — | **只定义类别，当前没有规则产出** |
| `guided_intent_mismatch` | — | **只定义类别，当前没有规则产出** |

三条口径（很重要）：

1. **这些规则只在 `source=guided` 的 run 上判定**（`_journey_cases` 开头 `journey.get("source") != "guided"` 直接返回）。一句话规划（quick）本来就没有前置选择，拿它去报"用户偏好被忽略"是误报 —— 误报会让 Bad Case 表失去可信度。
2. `prefetch_reused` **是用证据推出来的，不是标记位**：由 `app/workflow.py:_user_journey_summary` 比对"Prefetch 已有的线"与"本次 run 账本里实际调用的工具"，两边都发生 = 没复用。
3. `must_poi_missing` 的原因取自 `plan.days[].notes` 的原话（`_must_missing_reasons`），不是规则自己编一个理由。

`journey` 上下文由 `app/workflow.py:_user_journey_summary` 提供，字段包括
`source / source_session_id / transport_mode / transport_priority / hotel_priority / pace /
place_selections / prefetch_available / prefetch_reused / prefetch_status / discovery /
rejected_in_plan / must_missing`。`BadCaseContext.journey` 承接这份事实。

## 4. 关于 Jev 三类"判断错误"的取证方式

它们不是"猜 Jev 想什么"，而是拿**代码判定的事实**与 Jev 的结论对照：

- `jev_wrong_choice`：`choose_plan()` 在采用 Jev 的选择前会跑
  `planner.hard_constraint_violations()`；不通过就把 `rejected` 写进 `plan_choice`。
- `jev_missed_replan` / `jev_unnecessary_replan`：质量门把**由代码算出的硬问题清单**
  作为输入发给 Jev（`_hard_issue_texts`），再比对它的 KEEP/REPLAN 结论。

也就是说：Jev 是在**知道**硬问题的前提下做出的判断，所以"漏判"是可归因的。

## 5. 人工处理（管理端）

```text
GET   /api/v1/admin/badcases?analysis_status=pending&category=&severity=&run_id=&limit=&offset=
GET   /api/v1/admin/badcases/{badcase_id}        # 含 trace_refs 展开成真实 span
PATCH /api/v1/admin/badcases/{badcase_id}
      body: {analysis_status?, fixed_status?, root_cause_status?, suspected_root_cause?,
             severity?, introduced_in?, fixed_in?}
```

- PATCH 有**字段白名单**（`app/api.py:BADCASE_PATCH_FIELDS`），不能改 `run_id` / `category` /
  `created_at` / `badcase_id`；
- 枚举值在服务端校验（`analysis_status` / `fixed_status` / `root_cause_status` / `severity`），
  前端下拉框只是便利；
- 典型流程：`suspected → verified`（确认根因）→ 修代码 → `fixed_status=fixed` + `fixed_in=<commit>`，
  并把这个场景加进 `benchmark/cases/badcase_regression.jsonl`。

## 6. 怎么加一条规则

1. 在 `app/badcase.py` 顶部加 `CATEGORY_*` 常量（Jev 相关加进 `JEV_CATEGORIES`）；
2. 写一个 `_xxx_cases(ctx) -> list[dict]` 函数，用 `_case(...)` 构造，**只写事实**：
   `expected`/`actual` 都要能指回 `plan` / `metrics` / span / 账本里的字段；
3. 接进 `detect_badcases()` 的组合列表；
4. 加测试（构造 `BadCaseContext` 即可，不需要跑真实 run）；如果是"确认过的真实故障"，
   同时往 `benchmark/cases/badcase_regression.jsonl` 加一条 case。
