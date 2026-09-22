# Benchmark（Agent Loop 主路径）

一句话：**用同一批固定用例，把"这次改动让 Agent 出的行程变好还是变坏"变成可复现的数字。**

它评测的对象只有一条路径：`app/agent_runner.py::execute_agent_run` —— Agent 在循环里自己调
`app/travel_tools.py::build_travel_tools(hub)` 的工具，最后用 `submit_final_plan` 交卷。

> 旧版 `benchmark/`（固定 12 步 + Jev）已整体删除：它测的是 `app/workflow.py` 那条流程，
> 与新路径不相容。本目录是重建的最小版本，**只在被替换掉的维度上取舍，不假装覆盖全部**。

## 怎么跑

```bash
python -m benchmark                    # 全部套件（12 例，离线，约 40 秒）
python -m benchmark --suite basic      # 只跑一个套件
python -m benchmark --limit 1          # 每个套件最多 1 例（冒烟）
python -m benchmark --no-baseline      # 只出结果，不动 baselines/
python -m benchmark --compare-baseline # 与 baselines/agent_loop.json 逐指标对比
python -m benchmark --json             # 只打印汇总 JSON（给脚本用）
```

* 结果落 `benchmark/results/<benchmark_run_id>/{summary.json,cases.jsonl}`（已被 gitignore）；
* 基线落 `benchmark/baselines/agent_loop.json`（**CLI 默认写、管理端默认不写**：
  管理端触发的一轮评测不该覆盖仓库里的基线）；
* 默认用**临时库**跑，不碰 `data/travelplan.db`；要留证据就传 `--db path/to/bench.db`。

管理端也可以触发：`POST /api/v1/admin/benchmark/runs`（需要管理员 Token），
它调的就是 `benchmark.runner.run_benchmark`。管理端那条路径会写评测 run 到当前库
（`source="benchmark"`，`/admin/runs` 默认把它们过滤掉）。

## 为什么全部离线

假 ProviderHub 返回一个**固定的世界**（车次 / 航班 / 酒店 / 门票 / POI / 路线，见
`fixtures/world.py` 的常量），脚本模型按预设剧本发出工具调用与交卷参数。
不联网、不花任何第三方额度、可重复 —— 一次真实规划要串几十个 HTTP、数据每天都在变，
那样的"评测"测的是天气，不是代码。

本套件**没有** live 套件。真实链路的抽检不放在这里：第三方抖动会被读成"代码退化了"。

## 目录

```text
benchmark/
├─ cases/            四个套件的用例（jsonl，见下）
├─ fixtures/world.py 固定世界 + 假 Hub + 脚本模型 + 一次 case 的装配
├─ judges.py         判卷器（确定性，不用模型判分）
├─ metrics.py        指标计算（纯函数，可单测）
├─ runner.py         运行器 / CLI / Evolution 的 measure 入口
├─ baselines/        本路径的基线（agent_loop.json）
└─ results/          逐次结果（gitignore）
```

## 为什么用例用 jsonl

用例是**数据**不是代码：jsonl 一行一例，加用例不必改 Python、review 时一行就是一条用例，
格式错误由 `runner.load_cases` 直接抛错（静默跳过坏 case 会让指标悄悄失真）。
需要"原始剧本"的极端场景（评测判卷器本身能不能抓到坏行为）走 Python 常量，
因为那类剧本没法用声明式的 `tool_calls + submit` 表达。

## 用例格式（`cases/<suite>.jsonl`，一行一个 JSON）

```json
{
  "case_id": "basic-bj-cd-3d",
  "suite": "basic",
  "title": "北京→成都 3 天：正常出计划",
  "query": "10月1日从北京去成都玩3天，两个人，预算6000，喜欢美食和拍照",
  "fixtures": {"failing": [], "empty": [], "env": {"MAX_AGENT_STEPS": "16"}},
  "intent": {"destination": ["成都"], "place_selections": {"B0FF2": "REJECT"}},
  "tool_calls": [["search_trains", {"origin": "北京", "destination": "成都", "depart_date": "2026-10-01"}]],
  "submit": {"...": "submit_final_plan 的参数（见 app/agent_runner.SubmittedPlan）"},
  "tail": [{"tool": "search_poi", "args": {"keywords": "火锅", "region": "成都"}}],
  "repeat_last_step": false,
  "expect": {"expect_status": "completed", "min_days": 3, "no_reject_intrusion": true}
}
```

`fixtures` 的语义：

* `failing`：这些工具返回 `UNAVAILABLE` 且无 items（主源不可用）；
* `empty`：这些工具返回 `EMPTY` 且无 items（查到了但没有）。**两种都会在调用账本上
  留下非 OK 的状态** —— "查了但没拿到"和"查到了"必须分得清；
* `env`：本次 run 的环境覆盖（例如把 `MAX_AGENT_STEPS` 调小来测截断）；
* `usage` / `delay_seconds`：每轮模型调用的 token 用量 / 延迟（测成本护栏与超时）。

`submit` 为 `null` 时模型不交卷（用于"拿不到数据就该如实失败"的用例）。
`repeat_last_step: true` 时剧本末尾**不加**收尾的文本步骤 —— 专门用来构造"撞上步数上限"。

## 套件（12 例确定性）

| 套件 | 例数 | 覆盖 |
| --- | --- | --- |
| `basic` | 3 | 正常出计划、预算逼近上限、REJECT 不进行程 |
| `hard` | 4 | 时间冲突被修订、撞步数上限仍保留行程、工具返回空不编造、当天往返不排住宿 |
| `provider_failure` | 2 | 火车源不可用、酒店源返回空（都不许拿"没查到"当借口编数据） |
| `badcase_regression` | 3 | 已确认的失败类别：REJECT 侵入、回程填成去程车次、无来源价格 |

旧套件名里的 `jev_decision` 与 `live_smoke` 保留在 `SUITES` 里但**已退役**：管理端复选框
传进来时会被显式跳过并在 notes 里说明；只勾退役套件时直接报错（不产出"0 例 100% 通过"）。

## 判分口径

判分只回答"客观事实对不对"，**不用模型打分**（那会引入不可复现性）。

每个 case 的 `expect` 全部可选，缺省不检查；常用的几项：

| 断言 | 口径 |
| --- | --- |
| `expect_status` | run 的 status（`completed` / `failed`），默认 `completed` |
| `expect_no_plan` | 拿不到数据时必须**没有** plan（不编造） |
| `min_days` / `min_items` | 行程非空：天数与安排项的下限 |
| `require_grounded_numbers` | 行程里的每个价格 / 班次 / place_id / 路程耗时 / source_id 必须能在**本次取数真的返回过的值**里找到（见下） |
| `no_reject_intrusion` | 用户 REJECT 的 place_id 一个都不许出现在行程里 |
| `max_unresolved_time_conflicts` | `plan.warnings` 里未解决的 error 级时间问题数量上限（通常 0） |
| `expect_time_conflict_code` | 必须出现某个时间问题（例如 `TRANSFER_TIME_SHORTFALL`）且已被修订 |
| `expect_budget_within` | `projected_total ≤ budget_total` |
| `expect_degradation_contains` / `forbid_degradations` | 降级说明是否与事实一致 |
| `expect_injected` / `expect_failed_calls` | 故障注入真的生效了吗（防止用例空转） |
| `expect_hotel_absent` / `expect_transport_absent` | "没查到"的部分不许被填成具体的值 |
| `expect_inbound_transport_no` / `expect_place_ids` | 回程班次、必去地点是否真的进了行程 |
| `expect_missing_artifacts` | 失败时不许留下 `plan.json` 这类"伪造成品" |

**"pass" 不等于"run 成功"**：有的用例的期望就是"如实失败"（工具返回空时不许编造行程）。
那种用例 run 是 `failed`、judge 判 `pass`。`case_count / passed` 统计的是"结果符合预期"。

### 数字对账（`grounding`）怎么算

假 Hub 每返回一条数据就把它记进**事实账本**（价格 / 历时 / 路程秒数 / place_id / 店名 /
班次 / source_id）。判分时把行程里的对应字段逐个拿去对：不在账本里就是无来源的数字。
免费（`price = 0`）不检查 —— 编造的价格从来不是 0。

这是本套件里最值钱的一条断言：它把"Agent 有没有编价格/班次"从观点变成了可证伪的事实。

### 时间冲突

`app/agent_runner._apply_time_pass` 是 Agent 路径上**唯一**保留的 Python 校验。判分读
`plan.warnings`：`severity="error"` 且 `resolved=False` 的算"未解决"（这正是
`execute_agent_run` 自己用来决定要不要记降级的口径）。

## 指标（五个维度，不给总分）

```text
hard_constraints  hard_constraint_violation_rate / days_nonempty_rate / reject_intrusion_rate
                  unresolved_time_conflict_rate / time_conflict_resolved_rate / budget_overflow_rate
plan_quality      avg_items_per_day / category_diversity / preference_coverage
evidence          grounded_number_rate / unsupported_number_rate / source_traceable_rate
provider          provider_success_rate / provider_failure_calls / injected_failure_hit_rate
performance       duration_ms / llm_calls / tool_calls / total_tokens / input_tokens / output_tokens
```

* **没有 jev 维度**：这条路径上没有 Jev，不造一个假维度来凑数。管理端会把缺失的维度
  显式标出来（"部分维度没有指标"），这比补一个恒为 0 的列诚实。
* 算不出来的指标写 `null` 而不是 `0`：`0` 是"确实是零"，`null` 是"这次没有可测对象"。
  把两者混起来，"通过率 100%"可能只是"一条用例都没跑"。
* 套件内先平均，再对套件**等权**平均：basic 有 3 例、provider_failure 有 2 例，
  按用例数加权会让"用例多的套件"盖过"用例少的套件"。

## 基线

`baselines/agent_loop.json`：`benchmark_version / fixture_version / commit / model /
suites / case_count / passed / metrics / generated_at`。

* 改了 `fixtures/world.py` 的世界数据、注入语义或剧本形状 → 必须改 `FIXTURE_VERSION` 并重跑基线；
* **性能类指标**（`duration_ms` / `llm_calls` / `*_tokens`）会随机器与随机性漂移，
  用 `--compare-baseline` 看趋势，不要拿单次的个位数差异当结论；
* 基线只有一份（没有 OFF/ON 对照）：`app/api.py::_benchmark_baselines` 读的是历史 Jev 的
  `off.json` / `on.json`，本路径下这两个文件不存在，所以管理端详情页的"基线对比"一节
  会如实显示"这次运行没有基线对照"。

## 这套基准**测不了**什么（诚实说明）

* **测不了"行程好不好玩"**。它只验证客观事实（有没有编造、有没有违反硬约束、
  时间排不排得下）。`category_diversity` / `preference_coverage` 是**代理量**，
  只能横向比较，不能当绝对分。
* **测不了真实 Provider 的行为**。假 Hub 的世界是常量：不会超时、不会返回脏数据、
  不会变价。第三方抖动、限流、字段漂移它都看不见。
* **测不了 Prompt 文本的质量**。脚本模型是按剧本走的，它不会因为 prompt 写得好而变聪明；
  换一个真模型，结论可能完全不同。**本套件验证的是接线与判据，不是模型能力**。
* **测不了营业时间**。Agent 路径不落 `places` 表，`_apply_time_pass` 以 `places=None` 运行，
  所以 `OPENING_TIME_CONFLICT` / `LAST_ENTRY_MISSED` 这条检查在本路径上根本不会触发
  —— "没有报营业时间冲突"不等于"营业时间排得对"（prompt 里要求 Agent 自己保证）。
* **测不了预算算术**。`budget_*` 只看 Agent 自己填的汇总与用户预算的关系，
  不会去核对 breakdown 的加法是否正确。
* **只有 12 例**。它覆盖的是最容易出错、且能确定性复现的几类问题；它不是验收测试，
  替代不了人工看一份行程。

## 加一个用例

1. 往对应套件的 `.jsonl` 末尾加一行（字段见上）；
2. 涉及"刚修完的真实 Bad Case"就加进 `badcase_regression.jsonl` ——
   **Bad Case 一旦确认，就永久进回归集**，这是"不重复犯同一个错"的唯一机制；
3. 跑 `python -m pytest tests/test_benchmark.py -q -p no:randomly`：
   它会校验格式、数量与逐例判分；
4. 若基线变了（新增/删除用例、世界数据变化），重跑 `python -m benchmark` 更新基线。

## 与 Evolution 的关系

`benchmark.runner.make_measure()` 把本套件包装成 `measure(overrides) -> 指标`，
供 `app/evolution.py` 验证候选配置改动（用临时库跑，不污染生产库）。
Evolution 的判定是"任一指标回退即 REJECT"，只看两边都有的指标名 —— 所以本套件的指标名
刻意与 `app/evolution.METRIC_DIRECTIONS` 保持交集（`hard_constraint_violation_rate` /
`provider_success_rate` / `category_diversity` / `preference_coverage`）。
