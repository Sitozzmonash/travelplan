# Benchmark 规格（下一阶段设计，本轮不实现）

> 一句话区分：**Acceptance ≠ Benchmark**。
> 现有 `scripts/verify_run.py`（32 项）验证的是**真实性 / Provenance / 预算数学 / 输出完整 / 失败明说**。
> 一个 32/32 的 case 仍可能出现：连续 5 家餐厅、夜景排上午、营业时间硬冲突、明显折返、大量 `ROUTE_UNVERIFIED`。
> 因此需要真正衡量「旅途计划质量」的 Benchmark。本文件只设计，不写执行代码。

## 1. 设计原则

1. **不先做单一魔法总分**。分维度打分：Hard Constraint / Plan Quality / Evidence / Provider / Performance / UX。
2. **硬门槛先行**：硬约束任何一条触发即 FAIL，通过后才比较软质量。
3. **确定性优先**：能由代码/规则判的，不让 LLM 判。
4. **可比较**：同一数据集 + 同一版本上下文 → baseline vs candidate 两份 metrics。
5. **别用实时票价变化判断 regression**：实时数据单独跑 Live 集。

## 2. 数据集四类

### A. Basic（≥20 个 Case）

覆盖维度：城市差异、2~7 天、1~4 人、有/无预算、不同偏好（美食/拍照/自然/历史文化/夜景）、不同交通偏好。目的：整体可用性与覆盖面。

### B. Hard（≥20 个 Case）

覆盖：

```text
低预算 / 时间很紧 / 红眼航班 / 跨午夜 / 超长火车 /
节假日（预售期） / 早班返程 / 晚班抵达 / 多偏好冲突 /
酒店离景点远 / 复杂营业时间
```

目的：压出 `OPENING_TIME_CONFLICT`、`INBOUND_MULTI_DAY`、抵达日在途等边界。

### C. Provider Failure（≥10 个 Case）

```text
12306 unavailable / Tuniu timeout / TikHub free_credit=0 /
MediaCrawler unavailable / AMap route unavailable / LLM timeout
```

验证三件事：**fallback 正确、honesty（都如实记 UNAVAILABLE + reason + 链）、degradation 不伪造**。
（已有 `--with-fallback` 注入式验证作为雏形，见 `scripts/provider_smoke.py`。）

### D. Bad Case Regression（历史真实失败永久加入，≥ 下列全部）

```text
OUTBOUND_OFF_DATE（发车日与约定日不符却不明说）
跨午夜抵达日错误（抵达日之前被排成游玩日）
凌晨安排景点（红眼抵达日 03:15 打卡）
回程交通被裁掉（末日窗口切片吞掉 inbound 段）
没有去程却继续排（OUTBOUND_UNAVAILABLE 缺失）
没有住宿却预算正常（HOTEL_UNAVAILABLE 缺失）
营业时间跨零点解析（00:30 被读成开门）
连续餐厅（bj_cd D2：5 家馆子 08:30→14:43）
夜景排上午（双子塔夜景打卡点排在 09:25）
```

这些对应 `docs/ACCEPTANCE.md` §36.7 已定位并修复/未修复的条目，**每条都能在 `tests/` 找到或补一个硬断言**。

## 3. 指标

### 3.1 Hard Constraint（代码评分，任何 >0 即 FAIL）

```text
date_consistency_rate          日期一致性
hard_time_conflict_rate        硬时间冲突（同日内区间重叠）
opening_hours_violation_rate   营业时间冲突
missing_transport_disclosure_rate   去程/回程缺失却不明说
missing_hotel_disclosure_rate       住宿缺失却不明说
budget_math_error_rate         预算数学错误
hallucinated_price_rate        编造价格（查不到 source 或对不上 fetched_at 的值）
hallucinated_route_rate        编造路线（没高德数据却给了具体耗时）
source_coverage_rate           实时价格必须有 source
```

硬门槛（示例，阈值随第一版评测定稿）：

```text
hallucinated_price > 0  → FAIL
hard_time_conflict > 0  → FAIL
critical date error > 0 → FAIL
```

### 3.2 Plan Quality（规则 + LLM Judge）

```text
category_diversity          餐饮/景点/活动多样性
consecutive_same_type_count 连续同类上限（如连续餐馆数）
backtracking_score          折返统计（阈值已在 planner.py: BACKTRACK_*）
daily_load_balance          每天负荷均衡
meal_time_quality           用餐时间合理性（LUNCH/DINNER 窗口）
pace_match                  节奏与天数/人数匹配
preference_coverage         偏好覆盖（用户说美食有没有美食）
first_day_quality           抵达日在途/半日在途质量
last_day_quality            末日返程质量
route_efficiency            路线顺路度
```

### 3.3 Evidence

```text
evidence_coverage       有证据的地点占比
multi_source_ratio      多来源地点占比（Trust 的「多来源」分项）
poi_verified_ratio      高德核实率
trust_score_distribution
ad_risk_distribution
unsupported_recommendation_rate   无证据却强推的比例
```

### 3.4 Provider

```text
provider_success_rate
fallback_success_rate
timeout_rate
empty_rate
invalid_response_rate
route_verified_rate
```

（这些字段已经能从 `audit_report.json` 的 `provider_calls[]` 直接统计，不必新增埋点。）

### 3.5 Performance

```text
end_to_end_latency
llm_latency
provider_latency
tool_call_count
llm_call_count
input_tokens / output_tokens / cached_tokens
```

token 与 latency 已在 `llm_calls` / `provider_calls` 里留存，可直接聚合。

### 3.6 User-facing Quality（仅 LLM Judge）

```text
clarity / usefulness / readability / explanation_quality
```

**软指标不能覆盖硬约束**：硬约束 FAIL 时，软指标分数不参与决策。

## 4. 评分原则

```text
Hard Constraint  Plan Quality  Evidence  Provider  Performance  UX
```

- 第一版不做魔法总分，按维度并列留档。
- 设硬门槛：`hallucinated_price > 0`、`hard_time_conflict > 0`、critical 级日期错误 → 直接 FAIL。
- 硬门槛通过后，才比较 Plan Quality / Evidence / UX 等软维度。

## 5. Baseline / Candidate 比较

```text
Baseline Commit  → Benchmark 数据集 → metrics_baseline.json
Candidate Commit → 同一数据集     → metrics_candidate.json
Compare          → Accept / Reject
```

每次必须记录上下文：

```text
TravelPlan commit / SuperHarness commit / model / config / provider snapshot / benchmark version
```

## 6. 实时数据特殊处理

```text
Deterministic / Mock：硬规则、Planner、Feasibility、Regression 用 mock 数据（确定性）
Live：                 Provider 真实表现、实时数据质量、Latency、Fallback 用真实调用
```

**不要用实时票价变化判断代码 regression**。价格是 Provider 决定的，跑 Live 集时价格相关的硬约束应改为「是否有 source / fetched_at」而非「具体数值是否符合预期」。

## 7. RSI 扩展（设计目标，本轮不建）

```text
Bad Case Pool
→ Failure Cluster（按 root_cause.module 聚合）
→ Root Cause / Credit Assignment
→ module: planner / prompt / provider adapter / router / config / workflow
→ Candidate Patch
→ Benchmark
→ Accept / Reject
```

**必须写明**：RSI 不允许 Agent 修改代码后自己说「变好了」。接受/拒绝只能依据固定 Benchmark 与 Regression 的确定性结果。