# Benchmark v0.1

一句话：**用同一批固定用例，把"这次改动到底让行程变好还是变坏"变成可复现的数字。**

它取代不了人工验收，但能回答三个问题：硬约束有没有被违反、行程质量有没有退化、
Jev 的接入到底是净收益还是净成本。

## 怎么跑

```bash
# 全部确定性套件（离线、不联网、约 2~4 分钟）
python -m benchmark.runner

# 只跑一个套件 / 只跑几个用例（冒烟）
python -m benchmark.runner --suite basic
python -m benchmark.runner --suite jev_decision --limit 2

# 关掉 Jev 跑（生成/更新 Jev OFF 基线）
python -m benchmark.runner --jev off

# 额外跑 Live Smoke（联网，会真的打 12306 / 途乐 / 高德 / TikHub）
python -m benchmark.runner --live
```

默认用**临时库**跑，不碰生产库；要留证据就显式传 `--db path/to/bench.db`。
结果落 `benchmark/results/<benchmark_run_id>/{summary.json,cases.jsonl}`；
最新的 Jev OFF / ON 指标落 `benchmark/baselines/jev_{off,on}.json`。

管理端也可以触发：`POST /api/v1/admin/benchmark/runs`（需要管理员 Token）。

## 套件构成（33 例确定性 + 5 例 Live Smoke）

| 套件 | 例数 | 来源 | 是否联网 |
| --- | --- | --- | --- |
| `basic` | 12 | 人工典型需求（国内城市，含预算/人数/偏好差异） | 否 |
| `hard` | 6 | 边界条件（当天往返、跨天卧铺、末日返程、无日期、无预算、1 天） | 否 |
| `badcase_regression` | 6 | 已确认的真实失败类别，一条一个 | 否 |
| `provider_failure` | 4 | 受控注入（失败 / 返回空） | 否 |
| `jev_decision` | 4→5 | 关键软决策与 fallback | 否 |
| `live_smoke` | 5 | 真实链路抽检 | **是** |

> **Bad Case 一旦确认，就永久加入回归集**（`badcase_regression.jsonl`）。
> 这是"不重复犯同一个错"的唯一机制：修完一个 Bad Case，就把触发它的场景固化成一个 case。

## Case 格式

`benchmark/cases/<suite>.jsonl`，一行一个 JSON：

```json
{
  "case_id": "basic-bj-cd-5d",
  "suite": "basic",
  "title": "北京→成都 5 天（美食+拍照，有预算）",
  "query": "10月1日从北京去成都玩5天，两个人，预算6000，喜欢美食和拍照",
  "live": false,
  "fixtures": {
    "failing": ["search_trains"],
    "empty": ["search_hotels"],
    "llm_unavailable": false,
    "poi_spread": 0.25,
    "jev_script": {"plan_choice": {"choice": "A"}}
  },
  "expect": {
    "hard_constraints_ok": true,
    "min_items": 4,
    "expect_badcase_categories": ["provider_failure"],
    "expect_no_badcase_categories": ["jev_wrong_choice"],
    "expect_jev_choice": "A",
    "expect_jev_fallback": false,
    "expect_replan": false,
    "allow_failed": false
  }
}
```

`expect` 的每一项都是可选项，缺省即不检查；判定规则见 `benchmark/judges.py`。
`fixtures` 的语义：

- `failing` / `empty`：对应 `FakeHub` 的注入方法名（失败 / 返回空）；
- `llm_unavailable`：模型整个不可用，验证"Evidence First, LLM Second"；
- `poi_spread`：地点之间的经纬度间距（度）。默认 0.01°（≈1.1km）会让所有点落进
  同一个地理簇，于是 Top-K 只排得出一份拓扑；要测 Jev 的"选方案"必须把它放大
  （0.25° ≈ 28km，模拟"城市里相距很远的几个区"）；
- `jev_script`：按决策标签脚本化假 Jev（`plan_choice` / `tradeoff` / `quality_gate`）。

## 指标（与接管任务 §9 的清单逐字一致）

六个维度，全部由代码从**已发生的事实**（plan / trace span / run_metrics / decisions /
badcases）算出，没有任何模型打分：

```text
hard_constraints  date_mismatch_rate / hard_time_conflict_rate / opening_hours_violation_rate
                  missing_transport_disclosure_rate / missing_hotel_disclosure_rate
                  budget_math_error_rate / hallucinated_price_rate / hallucinated_route_rate
                  missing_source_rate / hard_constraint_violation_rate
plan_quality      category_diversity / consecutive_same_type_count / backtracking_score
                  daily_load_balance / meal_time_quality / pace_match / preference_coverage
                  first_day_quality / last_day_quality / route_efficiency
evidence          evidence_coverage / multi_source_ratio / poi_verified_ratio
                  unsupported_recommendation_rate
provider          provider_success_rate / fallback_success_rate / timeout_rate
performance       end_to_end_latency / llm_latency / jev_latency / provider_latency
                  llm_calls / jev_calls / tool_calls / input_tokens / output_tokens
                  cached_tokens / total_tokens
jev               jev_success_rate / jev_fallback_rate / jev_timeout_rate
                  jev_low_confidence_rate / jev_plan_accept_rate / jev_replan_rate
                  quality_delta_with_jev / latency_delta_with_jev
```

算不出来的指标写 `null` 而不是 0：`0` 是"确实是零"，`null` 是"这次没有可测对象"。
把两者混起来，Jev OFF/ON 的对比就会得出错误结论。

`quality_delta_with_jev` / `latency_delta_with_jev` 是**套件级**对照量，只有同时存在
`baselines/jev_off.json` 与 `baselines/jev_on.json` 时才有值（见 `metrics.jev_comparison`）。

### 已知的指标口径限制（不要误读）

- `poi_verified_ratio` 用"行程项是否带 source 引用"作代理，不是真的去查高德 POI 校验标记；
- `opening_hours_violation_rate` 只在拿到营业时间数据时才可能非零，没有数据不等于"通过"；
- `hard_constraint_violation_rate` 的分母是"天数 + 5"，是个**相对量**，只用于版本间比较，
  不要当成"违规概率"；
- 软指标（plan_quality / evidence）只能横向比较，不能当绝对分。

## 基线（Baseline）

`baselines/jev_off.json` 与 `baselines/jev_on.json` 各记一份：

```text
benchmark_version / travelplan_commit / superharness_commit / model
fixture_version / config 快照 / suites / case_count / passed / failed / metrics / generated_at
```

- **Baseline A = Jev OFF**：不注入替身，走真实的"未配置/关闭"分支；
- **Baseline B = Jev ON**：注入脚本化的假 Jev，走完整的三个决策节点。

基线必须成对更新：只更新一边，`baseline_compare` 就是在拿不同版本的世界对比。
`fixture_version` 变了，两份基线都要重跑。

## Live Smoke

联网套件，单独统计（`metrics.live_smoke`），**不进**确定性指标 —— 否则"今天 12306 抖了"
会被读成"代码退化了"。它的用途只有一个：确认真实链路还通。

## 怎么加一个 case

1. 按上面的 schema 往对应套件的 `.jsonl` 末尾加一行（改完立刻 `python -m pytest tests/test_benchmark.py`，
   它会校验格式与数量）；
2. 如果这是一个刚修完的真实 Bad Case，加进 `badcase_regression.jsonl`；
3. 改了假件行为或注入语义时，**必须**同时改 `benchmark/fixtures/world.py:FIXTURE_VERSION`
   并重跑两份基线。

## 目录

```text
benchmark/
├─ cases/        五个确定性套件 + live_smoke.jsonl
├─ fixtures/     把 case 的注入参数翻译成 run 的依赖（复用 tests/fakes.py 的离线假件）
├─ baselines/    jev_off.json / jev_on.json
├─ results/      <benchmark_run_id>/summary.json + cases.jsonl
├─ judges.py     确定性判卷器
├─ metrics.py    指标计算（纯函数，可单测）
└─ runner.py     运行器与 CLI
```

`fixtures/world.py` 刻意复用 `tests/fakes.py`：同一次失败必须只有一种解释。
如果 Benchmark 另有一份假 Provider，"单测通过、评测失败"就会变成无法归因的分歧。

## 与 Evolution 的关系

`benchmark.runner.make_measure()` 把本套件包装成 `measure(overrides) -> 指标`，
供 `app/evolution.py` 验证候选改动（用临时库跑，不污染生产库）。
Evolution 的判定规则是"任一指标回退即 REJECT"，见 `app/evolution.py:METRIC_DIRECTIONS`。
