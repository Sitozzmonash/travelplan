# Iteration Log

> 长期追加文档，不是一次性报告。每次「定位问题 → 修改 → 评测 → 决策」追加一个 Iteration。
> 当前记录到 **Iteration 000 / Baseline**（2026-09-20），后续迭代从 001 起。

---

## Iteration 000 / Baseline

### Baseline

- TravelPlan commit: `45bf2f2`（`TravelPlan: travel planning agent (app/frontend/tests/docs)`）
- SuperHarness commit: `2c2c51a3b3d2370ce00eb5d17be3224ad3db40ec`
- Benchmark version: 未建立（`BENCHMARK_SPEC.md` 已设计、未实现）
- model: `kimi-k3`（`MODEL_NAME`）

### 问题（当前仍未解决，按真实来源登记）

来源列 = `docs/ACCEPTANCE.md` §36.7 条目（或 `docs/review/ARCHITECTURE_CURRENT.md` §7）。

| # | 问题 | severity | category | 来源 |
|---|---|---|---|---|
| 1 | 固定 Workflow 没有完整 SuperHarness Trace 树（缺 trace_id） | medium | 可观测性 | ACCEPTANCE §36.7-6；ARCHITECTURE §7-B |
| 2 | MediaCrawler 未安装，`TikHub 无额度 → MediaCrawler` 兜底链路的真实抓取跑不通（如实降级） | medium | provider_error / fallback_error | ACCEPTANCE §36.7-2 |
| 3 | TikHub `free_credit=0`，社交证据为空（本轮仅有 Tavily 网页证据） | low | evidence_quality | ACCEPTANCE §36.7-3 |
| 4 | 推理模型 `kimi-k3` 单次调用慢（实测 parse_intent 49s、抽地点 75s+/115.8s），时有超时降级 | medium | performance | ACCEPTANCE §36.7-4 |
| 5 | 超预算后不会主动重规划（如实标 `over_budget`、不削价编造，但无「反推更省方案」） | medium | budget_error（策略） | ACCEPTANCE §36.7-5 |
| 6 | Planner 可能连续安排过多同类地点（bj_cd D2：5 家餐馆 08:30→14:43，无景点） | medium | planning_diversity | ACCEPTANCE §36.7-15a |
| 7 | Opening Hours 冲突只报告、不自动修复（`apply_feasibility_pass` 不改排，critic 的 REVISION 全是置信度调整） | high | opening_hours_error | ACCEPTANCE §36.7-15a/b/c |
| 8 | 夜景地点可能排在错误时段（双子塔夜景打卡点 19:00-03:00 排在 09:25） | high | opening_hours_error | ACCEPTANCE §36.7-15b |
| 9 | `ROUTE_UNVERIFIED` 数量较多（实测 bj_cd 15 条/gz_cq 15 条，市内地段高德路线缺失时按直距估算） | low | evidence_quality | ACCEPTANCE §36.7-15；§35 C |
| 10 | 跨多日交通的 `end_time` 显示口径不精确：≥1440 分钟一律标 `(次日)`，不区分 +2/+3 天（真实日期未丢，在 reason 与 warning） | low | frontend_display | ACCEPTANCE §36.7-15e |
| 11 | SuperHarness 5 个 capability 尚未提交到 fork，submodule 指针停在 `2c2c51a3` → fresh clone 跑不起来 | high | 仓库可复现性 | ARCHITECTURE §7-A；README §3 |

### 说明

- #7/#8 虽被 `OPENING_TIME_CONFLICT`（error 级）当场报告、未静默，但 `runs.status` 仍是 `completed`——**"流程跑完" ≠ "计划没有瑕疵"**，这是 `status` 语义与 error 告警之间的空缺，未来需要更醒目地区分。
- #11 属于仓库/交付层面的未解决项，不在 `app/` 业务代码内。
- 以上均为「当前代码真实支持/真实出现」的问题，不含任何 PRD 里计划但未实现的能力。

### Bad Case

（暂无。下一阶段启用 `TRACE_BADCASE_SPEC.md` 后，将 #6/#7/#8/#9/#10 转成结构化 badcase 登记。）

### Root Cause

（暂无归因结论；#6/#7/#8 已初步定位到 planner 的「装配只有时间窗、不管类型多样性」与「feasibility 只报不改」，见 ACCEPTANCE §36.7-15。）

### Decision

`BASELINE`（不打 ACCEPT/REJECT/ROLLBACK；这是起始快照。）

### Notes

- 验收口径见 `docs/ACCEPTANCE.md`；架构分析见 `docs/review/ARCHITECTURE_CURRENT.md`。

---

## Iteration 001

（模板，供后续填充）

### Baseline
- TravelPlan commit:
- SuperHarness commit:
- Benchmark version:

### 问题
...

### Bad Case
- badcase_id:
- run_id:
- category:
- severity:

### Root Cause
...

### 修改模块
...

### 修改内容
...

### Benchmark Before
...

### Benchmark After
...

### Regression
...

### Decision
ACCEPT / REJECT / ROLLBACK

### Notes
...