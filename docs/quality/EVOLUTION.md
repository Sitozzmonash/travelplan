# Evolution 进化机制

一句话：把待分析的 Bad Case 变成**经过评测验证**的经验，而不是"看起来应该改改"。

代码：`app/evolution.py`；触发入口：管理端 `/admin/evolution` 或
`POST /api/v1/admin/evolution/runs`（需要 `EVOLUTION_ENABLED=true`，否则 409）。

> **评测入口评的是主路径**：第 ⑤ 步的 `measure` 由 `benchmark.runner.make_measure` 提供，
> 它在**临时库**里跑 12 例离线确定性用例（脚本模型 + 假 Provider），评的是
> `app/agent_runner.py::execute_agent_run`。见 [Benchmark](BENCHMARK.md)。
> 两点限制要知道：脚本模型不会因为 prompt 变好而变聪明，所以"改动前 vs 改动后"只对
> **接线、判据与可测的配置项**成立；另外 Agent 路径不产生 `jev_*` 系列 Bad Case，
> 表里那几条 Jev 杠杆在主路径上永远不会命中。

## 1. 完整流程

```text
① 取 analysis_status=pending 的 Bad Case      （已分析过的不重复处理）
      │
② 聚类：按 (category, 症状指纹) 归并
      │      症状指纹 = symptom 去掉具体对象名/序号后的首段，避免"同一模式被拆成一堆单例簇"
      │
③ 失败模式与根因假设
      │      failure_pattern = category + 代表性 symptom + 规模
      │      suspected_root_cause = 簇内最高频的 Bad Case 根因描述
      │
④ 候选改动（LEVERS）
      │      每个类别 → {受影响模块, 建议改动, 理由, 可执行的环境变量覆盖}
      │      没有配置杠杆的类别 → 明确判为"需要人工改代码"，不硬凑一个无效改动
      │
⑤ 验证：调 measure(overrides) 跑「改动前 vs 改动后」
      │      基线只跑一遍（同一批用例）；候选改动各跑一遍
      │      任一指标回退 → REJECT；没有任何改善 → REJECT；有改善且无回退 → ACCEPT
      │      拿不到可信评测 → REJECT（并写明原因）
      │
⑥ 落 Experience（每条簇一条）
      │
⑦ 把本轮的 Bad Case 标记为 analysis_status=analyzed

输出：evolution_runs 表（状态/结论/步骤/前后指标）+ experiences 表
```

## 2. 候选改动的判定规则

- 参与比较的指标与方向在 `app/evolution.py:METRIC_DIRECTIONS` 里显式列出 ——
  只有列进去的指标才算"改善/回退"，避免"改了一个数字、某个无关指标漂了"被当成改进。
- `REGRESSION_TOLERANCE = 1e-9`：任何超过噪声的回退都算回退。
- 结论有四种：`ACCEPT`（建议采纳）/ `REJECT`（不采纳，附原因）/ `ROLLBACK`
  （回退已采纳的改动；当前实现把它作为保留结论，尚未自动产生）/ `NOOP`
  （没有 `analysis_status=pending` 的 Bad Case 时写入，本轮不处理任何记录，`app/evolution.py:319`）。

## 3. LEVERS（候选改动表）

`app/evolution.py:LEVERS` 的每一项都是**可执行**的：`overrides` 直接对应
`app.config.override_env()` 能接受的键，写进环境变量就能复现同一份对照。

| 类别 | 影响模块 | 候选改动 | 覆盖 |
| --- | --- | --- | --- |
| `hard_constraint` | `app/planner.py` / `app/config.py` | 下调每天停留点上限 | `TP_MAX_ITEMS_PER_DAY=3` |
| `budget` | `app/planner.py` | 提高候选入选门槛，减少高价项进行程 | `TP_MIN_CANDIDATE_SCORE=4` |
| `jev_timeout` | `app/config.py` | 提高 Jev 超时预算 | `JEV_TIMEOUT_MS=2500` |
| `jev_quota` | `app/config.py` | 调低单次 run 的 Jev 调用上限 | `JEV_MAX_CALLS_PER_RUN=3` |
| `jev_low_confidence` | `app/config.py` | 下调置信度阈值以观察是否值得采纳 | `JEV_MIN_CONFIDENCE=0.6` |
| `jev_wrong_choice` / `jev_unnecessary_replan` | `app/config.py` | 提高置信度阈值，只采纳高置信决策 | `JEV_MIN_CONFIDENCE=0.85` |
| `budget_math` / `evidence_gap` / `hallucinated_price` / `jev_invalid_response` / `jev_missed_replan` | 对应模块 | **无配置杠杆**（代码缺陷） | 判为"需要人工改代码" |
| `provider_failure` / `missing_disclosure` / `route_unverified` / `degradation` | `app/providers.py` 等 | 需要换主备或换查询参数 | 判为"需要人工确认/改代码" |
| `provider_degraded` | `app/config.py` | 按降级项排查 | **永不命中的死杠杆**：它不是 `app/badcase.py` 的类别（那里的降级类别叫 `degradation`），没有任何 Bad Case 会命中它 |

只有"配置旋钮真的能影响该失败模式"的类别才有 `overrides`；其余类别**明确拒绝**，
而不是凑一个改了也没用的改动 —— 后者会让人误以为系统"自己变好了"。
`provider_degraded` 是这 17 条里唯一**连不上任何 Bad Case 类别**的一条：类别名与
`app/badcase.py`（`degradation`）不一致，因此永远不会被匹配到。

## 4. Experience 结构

| 字段 | 含义 |
| --- | --- |
| `experience_id` | 稳定主键（`exp-{evolution_run_id}-{sha1(cluster_key)[:10]}`） |
| `source_badcases` | 这条经验来自哪些 Bad Case |
| `failure_pattern` | 失败模式（类别 + 代表性症状 + 规模） |
| `verified_root_cause` | 根因。规则推测 + 评测对比结论（两者写在一起，便于分辨哪部分被验证过） |
| `affected_module` | 该改哪个文件 |
| `recommended_change` | 建议的改动 |
| `before_metrics` / `after_metrics` | 评测前后的指标 |
| `decision` | `ACCEPT` / `REJECT` / `ROLLBACK` |
| `created_at` | 生成时间 |

## 5. 明确不做的事

- **不自动改代码**：候选改动只是建议，落地由人决定。
- **不自动上线**：没有"改完直接生效"的路径。
- **不重复处理已分析的 Bad Case**：只取 `analysis_status=pending`；想重跑就先把它们改回
  `pending`（管理端 PATCH 即可）。
- **不凭感觉给 ACCEPT**：没有评测入口（`measure` 为 None）或评测返回空 → 一律 REJECT 并写明原因。
- **不伪造指标**：拿不到就写 `null`，不推算。

## 6. 怎么用

```text
1) 先积累 Bad Case：跑几次真实规划，或用 benchmark 的 badcase_regression 套件
2) 打开开关：EVOLUTION_ENABLED=true（默认关闭，避免在长驻服务里被误触）
3) 管理端 /admin/evolution 点"触发 Evolution"，或：
   curl -X POST -H "Authorization: Bearer $TRAVELPLAN_ADMIN_TOKEN" \
        -H 'Content-Type: application/json' -d '{"limit": 50}' \
        http://localhost:8000/api/v1/admin/evolution/runs
4) 看 /admin/evolution/runs/{id}：失败模式、候选改动、前后指标、结论
5) 对 ACCEPT 的建议：人改代码 → 跑 pytest 与 benchmark → 把触发场景加进 badcase_regression
```

评测入口由 API 层接到 Benchmark（`app/api.py:_evolution_measure` → `benchmark.runner.make_measure`）；
没有安装 `benchmark/` 时仍然会做聚类与建议，只是结论一定是 REJECT（并说明"评测入口不可用"）。
