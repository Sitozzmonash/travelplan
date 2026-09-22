# Benchmark 评测体系

一句话：用一批固定的、可复现的用例，把"这次改动让 Agent 出的行程变好还是变坏"变成数字。

完整说明（case 格式、判分口径、指标限制、怎么加 case）在
[`benchmark/README.md`](../../benchmark/README.md)，本篇只讲清"体系长什么样、数字怎么读、
跟 BadCase / Evolution 怎么接"。

> **当前覆盖范围**：`benchmark/` 评测的是**主规划路径** ——
> `app/agent_runner.py::execute_agent_run`（Agent 在循环里调工具、用 `submit_final_plan` 交卷）。
> 旧版套件（固定 12 步 + Jev，33 例 + 5 例 Live Smoke、六维指标、Jev OFF/ON 基线）已整体
> 删除：它建立在与主路径不相容的流程上。**本路径没有 Jev，因此没有 jev 维度**。

## 1. 构成

```text
12 例确定性 Core Case（离线、可复现、不消耗第三方额度、约 40 秒）
├─ basic               3  正常出计划 / 预算逼近上限 / 用户 REJECT 不进行程
├─ hard                4  时间冲突被修订 / 撞步数上限仍保留行程 / 工具返回空不编造 / 当天往返
├─ provider_failure    2  火车源不可用 / 酒店源返回空（都不许拿"没查到"当借口编数据）
└─ badcase_regression  3  已确认的失败类别：REJECT 侵入、回程填成去程车次、无来源价格
```

**没有 Live Smoke**：真实链路的抽检不放在这里 —— 第三方抖动会被读成"代码退化了"。
真实链路的探活见 `scripts/provider_smoke.py`。

**Bad Case 一旦确认，就永久加入 `badcase_regression.jsonl`** —— 这是"不重复犯同一个错"的机制。

旧套件名 `jev_decision` / `live_smoke` 在 `benchmark.runner.SUITES` 里保留为**已退役**：
管理端复选框传进来会被显式跳过并在 notes 说明；只勾退役套件时直接报错
（不产出"0 例 100% 通过"这种假结论）。

## 2. 指标（五维，全部由代码从既有事实算出）

| 维度 | 关心什么 | 代表指标 |
| --- | --- | --- |
| hard_constraints | 行程本身对不对 | `hard_constraint_violation_rate`、`days_nonempty_rate`、`reject_intrusion_rate`、`unresolved_time_conflict_rate`、`time_conflict_resolved_rate`、`budget_overflow_rate` |
| plan_quality | 行程像不像样 | `avg_items_per_day`、`category_diversity`、`preference_coverage` |
| evidence | 数字有没有依据 | `grounded_number_rate`、`unsupported_number_rate`、`source_traceable_rate` |
| provider | 数据源的表现 | `provider_success_rate`、`provider_failure_calls`、`injected_failure_hit_rate` |
| performance | 代价多大 | `duration_ms`、`llm_calls`、`tool_calls`、`total_tokens`、`input_tokens`、`output_tokens` |

**没有 jev 维度**：这条路径上没有 Jev，不造一个恒为 0 的假维度来凑数。管理端的
"部分维度没有指标"提示会把缺失显式标出来。

三条读法上的约定：

1. **`null` ≠ 0**。`null` 是"这次没有可测对象"，`0` 是"确实是零"。混起来会把"一条用例都
   没跑"读成"通过率 100%"。
2. **`passed` 是"结果符合预期"，不是"run 成功"**。有的用例的期望就是"如实失败"
   （工具返回空时不许编造行程）：那种用例 run 是 failed、judge 判 pass。
3. **套件等权**：套件内先平均，再对套件求均值，避免"用例多的套件"盖过"用例少的套件"。

最值钱的一条断言是 `grounded_number_rate`：假 Hub 每返回一条数据就记进事实账本，判分时
把行程里的价格 / 班次 / place_id / 路程耗时 / source_id 逐个拿去对 —— 不在账本里就是编造。
它把"Agent 有没有编价格"从观点变成了可证伪的事实。

## 3. 基线（只有一份）

`benchmark/baselines/agent_loop.json` 记：`benchmark_version / scope / fixture_version /
travelplan_commit / superharness_commit / model / suites / case_count / passed / failed /
metrics / generated_at`。

```bash
python -m benchmark                      # 跑全部套件并更新基线
python -m benchmark --compare-baseline   # 与基线逐指标对比
python -m benchmark --no-baseline        # 只出结果，不动基线
```

* **CLI 默认写基线、管理端触发默认不写**：管理端跑的一轮评测不该覆盖仓库里的基线。
* 改了 `benchmark/fixtures/world.py` 的世界数据 / 注入语义 / 剧本形状 → 必须改
  `FIXTURE_VERSION` 并重跑基线。
* **性能类指标会漂移**（`duration_ms` 尤其明显，同一台机器两次跑差 20% 是常态）：
  看趋势，不要拿单次的个位数差异当结论。

管理端 `/admin/benchmark` 展示五个维度、逐 case 结果（按套件分组）。它的"基线对比"一节
读的是历史 Jev 的 `off.json` / `on.json`，本路径下这两个文件不存在，因此会如实显示
"这次运行没有基线对照"——本地用 `--compare-baseline` 看趋势。

## 4. 与 BadCase / Evolution 的关系

```text
Run → 规则检测 → Bad Case(pending)
                     │
                     ├─► 人工确认后：把触发场景写进 badcase_regression.jsonl（永久回归）
                     │
                     └─► Evolution：聚类 → 候选改动 → 调 benchmark.runner.make_measure()
                                    在同一批用例上跑「改动前 vs 改动后」→ ACCEPT / REJECT
```

`make_measure(store, suites=("basic","hard"))` 返回 `measure(overrides) -> 指标`，在
**临时库**里跑，不污染生产库。Evolution 只看两边都有的指标名，所以本套件的指标名与
`app/evolution.METRIC_DIRECTIONS` 刻意保持交集。判定规则见 [EVOLUTION.md](EVOLUTION.md)。

## 5. 什么时候必须跑

| 改动 | 必须跑 |
| --- | --- |
| `app/agent_runner.py`（Agent Loop、交卷、时间兜底、护栏） | `python -m pytest tests/test_agent_runner.py tests/test_benchmark.py` + `python -m benchmark --compare-baseline` |
| `app/travel_tools.py`（工具信封 / 截断 / 失败语义） | `pytest tests/test_travel_tools.py` + `python -m benchmark`（用例覆盖了空/失败两种取数结果） |
| `app/prompts.py` 的主规划 prompt | benchmark 覆盖不到 prompt 文本质量（脚本模型按剧本走），需人工实跑若干真实需求，并升 `TRAVEL_PLANNER_PROMPT_VERSION` |
| `app/planner.py`（`_apply_time_pass` 走的可行性检查） | `python -m benchmark --suite hard`（时间冲突被修订就是靠它） |
| `app/providers.py` | `pytest tests/test_providers.py` + `python scripts/provider_smoke.py`（探活更贴近真实链路） |
| `benchmark/cases/**`、`benchmark/fixtures/**` | `python -m pytest tests/test_benchmark.py -q` + `python -m benchmark`（重跑基线） |
| 只改文档/前端 | 不需要，但 `pytest` 与前端 typecheck/lint/build 仍要过 |

## 6. 与单测的分工

Benchmark 只回答"固定用例上的结果有没有退化"；Agent Loop 的**机制**由离线单测逐个钉住：

| 文件 | 守什么 |
| --- | --- |
| `tests/test_agent_runner.py` | 交卷 / 未交卷兜底 / 空 days / 超时与步数截断 / 时间兜底 / 模型未配置 |
| `tests/test_benchmark.py` | 套件本身：12 例跑通、逐例判分符合预期、判卷器能抓到坏行为、管理端接口真的接上了 |
| `tests/test_travel_tools.py` | 工具参数与返回信封、截断、失败不抛 |
| `tests/test_agent_trace.py` | 每一步落 `run_stages.facts_json` / `trace_spans` / `current_stage` 契约 |
| `tests/test_model_fallback.py` | 主 → bk1 → bk2 降级链的装配与触发 |
| `tests/test_preheat_agent.py` | 预热 Agent 的入参、失败即不落库、护栏 |
| `tests/test_guided_journey.py` | Session create-patch-start 幂等 / 取消后 start=409 / `intent_from_basic` 不调模型 |
| `tests/test_admin_observability.py` | Admin Session 与 Provider Health 的聚合、脱敏与鉴权 |

跑法：`python -m pytest -q`（全量离线）。

## 7. 已知未覆盖的部分（诚实说明）

- **测不了"行程好不好玩"**：只验证客观事实（有没有编造、有没有违反硬约束、时间排不排得下）。
  `category_diversity` / `preference_coverage` 是**代理量**，只能横向比较。
- **测不了真实 Provider 的行为**：假 Hub 的世界是常量，不会超时、不会返回脏数据、不会变价。
- **测不了 Prompt 文本质量**：脚本模型按剧本走，不会因为 prompt 写得好而变聪明；
  本套件验证的是接线与判据，不是模型能力。
- **测不了营业时间**：Agent 路径不落 `places` 表，`_apply_time_pass` 以 `places=None` 运行，
  `OPENING_TIME_CONFLICT` / `LAST_ENTRY_MISSED` 这条检查根本不会触发 ——
  "没有报营业时间冲突"不等于"营业时间排得对"。
- **测不了预算算术**：只看 Agent 自己填的汇总与用户预算的关系，不核对 breakdown 的加法。
- **只有 12 例**：覆盖的是最容易出错、能确定性复现的几类问题；它不是验收测试，
  替代不了人工看一份行程。
- 没有 LLM Judge（那会引入不可复现性）；真实 Provider 抽检见 `scripts/provider_smoke.py`。
