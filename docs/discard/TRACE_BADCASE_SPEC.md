# Trace / Bad Case 规格（下一阶段设计，本轮不实现）

> 目标：为「调试、回归、Benchmark、模块 Credit Assignment、未来 RSI」建立统一的数据链路。
> 本文件只做**设计**，不写执行代码。所有「当前」都是指 2026-09-20 的代码状态。

## 1. 现状与缺口（一句话）

当前只有**两层**：TravelPlan 自己的 `audit_report.json`（业务审计，见 `app/workflow.py:_build_audit`）与
SuperHarness 原生 Observability（Agent Loop 才有 trace_id，固定流程没有，见 `ARCHITECTURE_CURRENT.md` §7-B）。
**没有**统一的 Trace 树、**没有** Bad Case 结构化记录、**没有** Benchmark。

## 2. Trace / Audit / Bad Case 分工

| 层 | 回答的问题 | 现状 | 未来归属 |
|---|---|---|---|
| **Trace** | 这次 Run **发生了什么**（谁调了谁、花了多久、结果状态） | 固定流程缺 trace_id；audit 里有 provider_calls / timeline / llm_calls 的平铺记录 | `runs/<run_id>/trace.json` |
| **Audit** | **为什么做这个决定**（为什么选这趟车、为什么 KEEP/REJECT、降级原因） | 已有：`decisions[]` + `degradations[]` + `evidence_summary` | 保持，作为 Trace 的「理由层」 |
| **Bad Case** | **哪里不好、为什么不好、哪个模块负责** | 无 | `runs/<run_id>/badcases.json` + 汇总池 |

三者关系：Trace 是骨架，Audit 是理由，Bad Case 是「对前两者 + 最终 plan 的判读」，并且**必须指向产生它的 stage/module**。

## 3. Trace Schema（设计）

```json
{
  "trace_id": "",
  "run_id": "",
  "parent_span_id": null,
  "span_id": "",
  "component": "",
  "operation": "",
  "status": "",
  "started_at": "",
  "finished_at": "",
  "duration_ms": 0,
  "input_summary": {},
  "output_summary": {},
  "error": null,
  "metadata": {}
}
```

- `component` 取值（第一阶段）：`workflow / llm / provider / tool / mcp / planner / budget / feasibility / critic / store`。
- `operation` 建议直接复用现有节点名与动作名：
  - workflow：12 节点名（`parse_intent`、`search_intercity_transport`、`search_hotels`、`search_social_guides`、`extract_and_normalize_places`、`verify_poi_and_routes`、`score_candidates`、`build_initial_plan`、`check_budget`、`check_feasibility`、`critic_and_revise`、`finalize`）。
  - llm：`llm.parse_intent`、`llm.extract_places`、`llm.critic` …（按 `app/llm.py` 的 `tag`）。
  - provider：`provider.12306`、`provider.tuniu.train`、`provider.amap.route`、`provider.tavily.web` …。
- `metadata` 放运行上下文：model 名、config 版本、provider 快照、`thread_id`。

### 落地约束

- 固定流程当前能产出「平铺事实」：`audit_report.json` 的 `provider_calls`（含 `fetched_at`/`status`/`duration_ms`）与 `llm_calls` 已有 span 的雏形，**第一阶段可直接由这些反推一版 trace.json**，不必先改 Runtime。
- 真正的树形结构（父子 span 正确嵌套）要等 SuperHarness 侧支持「固定 subprocess/workflow 也获得 trace_id」（见 ARCHITECTURE §7-B），在那之前 trace 可以是**按时间序的平铺 span 列表 + 显式 parent_span_id**。

## 4. Bad Case Schema（设计）

```json
{
  "badcase_id": "",
  "run_id": "",
  "trace_id": "",
  "query": "",
  "category": "",
  "severity": "",
  "symptom": "",
  "expected": "",
  "actual": "",
  "root_cause": {
    "component": "",
    "module": "",
    "stage": "",
    "confidence": ""
  },
  "evidence": [],
  "trace_refs": [],
  "candidate_fixes": [],
  "status": "open",
  "introduced_in": "",
  "fixed_in": null
}
```

- `root_cause.component` 限定为 §3 的 component 之一（再加 `data` 表示数据源本身、`config` 表示阈值/配置、`prompt`）。
- `trace_refs`：指向同一 run 里的 span_id / source_id / decision entity_id，保证「判读 → 事实」可回追。
- `evidence`：支撑 symptom 的原始摘录（warnings 条目、plan 片段、价格对比）。

## 5. Bad Case 分类（第一阶段全集）

```text
intent_error         router_error         tool_selection_error
provider_error       provider_timeout     fallback_error
normalization_error  retrieval_error      evidence_quality
ad_filter_error      poi_error            price_error
route_error          budget_error         feasibility_error
opening_hours_error  planning_diversity   planning_quality
backtracking         critic_miss          hallucination
frontend_display     performance
```

前五个侧重「数据/链路」，`*.error` 侧重「业务正确性」，`planning_*` / `backtracking` / `critic_miss` 侧重「计划质量」，`hallucination` 是最高红线（编造价格/车次/时间）。

## 6. Severity

| 级别 | 定义 | 现有实例子 |
|---|---|---|
| `critical` | 编造价格/车次、严重日期错误、硬时间上不可能 | 曾有 `OUTBOUND_OFF_DATE` 未明说 → 已修；`hallucinated_price > 0` |
| `high` | 没有去程却生成完整旅行、没有住宿却显示预算正常、明确营业时间硬冲突 | `OUTBOUND_UNAVAILABLE` / `HOTEL_UNAVAILABLE` 未明说（已修）；`OPENING_TIME_CONFLICT` error 级 |
| `medium` | 连续 5 家餐厅、明显折返、节奏差 | bj_cd D2 连续餐馆、`BACKTRACKING` |
| `low` | 文案或展示口径问题 | 跨 3 天车次 `end_time` 标 `06:45(次日)`（真实日期在 reason/warning，是显示口径不精确） |

## 7. Bad Case 来源

```text
自动规则发现   —— 用 benchmark 硬约束（§BENCHMARK_SPEC.md）自动判 FAIL 的，天然是 Bad Case
Benchmark Judge —— 同一数据集跑分，得分劣化的条目
用户人工反馈   —— 用户点「换一个」「不想去」等动作、或口头反馈
开发者 Review  —— 人工读产物（如本轮 ACCEPTANCE §36.7 的复盘）
LLM Judge      —— 仅限软指标（clarity/readability/usefulness），见下
```

**硬约束红线**：`LLM Judge 不能独自判断硬事实正确性`。价格对不对、日期对不对、营业时间对不对，只能由规则/代码判（数据在 source 里可查）。LLM Judge 只能评「且仅评」软质量。

## 8. Run 目录建议（未来统一）

```text
runs/
└─ <run_id>/
   ├─ trace.json
   ├─ plan.json
   ├─ audit_report.json
   ├─ metrics.json
   └─ badcases.json
```

- **本轮不改**现有 `outputs/<run_id>/`（`{plan.json, plan.md, audit_report.json}`）。
- 迁移时把这些产物识别为同一 run 的多视图，`trace.json / metrics.json / badcases.json` 为新增。

## 9. 面向 RSI 的要求（设计目标，本轮不建）

Trace 最终要能定位到：

```text
哪个 stage / 哪个 Provider / 哪个 Tool / 哪个 Planner Rule / 哪个 Prompt / 哪个 Config / 哪个代码版本
```

未来闭环：

```text
Failure
→ Credit Assignment（badcase.root_cause 归因到 module）
→ 确定修改模块（planner / prompt / provider adapter / router / config / workflow）
→ Candidate Patch
→ 同一 Benchmark 数据集跑分（metrics_candidate.json vs metrics_baseline.json）
→ Accept / Reject / Rollback
```

**硬约束**：RSI 不允许「Agent 改完代码后自己说变好了」。接受/拒绝只能依据固定 Benchmark 与 Regression 的确定性结果。