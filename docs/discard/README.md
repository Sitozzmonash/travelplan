# docs/discard — 已归档、不再维护的文档

这里放的是**曾经有用、现在不再作为事实来源**的文档。它们被移出 `docs/` 主目录，是因为
它们对当前代码的描述已经失效，留着会误导读者（"当前架构"其实不是当前，"下一阶段设计"其实
早过了那个阶段）。

**归档 ≠ 删除。** 里面的历史分析、论证与一次性工作单仍然可以查；只是**判断"现在怎么跑的"
请一律以代码 + `docs/01`~`docs/15` 为准**。

| 文件 | 原来的用途 | 为什么归档 | 现在应该看哪里 |
| --- | --- | --- | --- |
| `ARCHITECTURE_CURRENT.md` | 标题自称"当前架构"的时点快照（停在 commit `45bf2f2`） | 文中 9 处 `（N 行）` 全部过期，`app/` 只覆盖 9/24 个模块，表清单只有 9 张（现 25 张）；`§8` 说"Bad Case / Benchmark / RSI 当前未实现"，而三者都已落地。**"当前"二字已经不成立**——文件名保留只为不破坏引用，别再把它当"当前" | `docs/01_项目架构.md`（目录/分层/数据流）+ `docs/03_项目流程图.md` |
| `分工.md` | 四角色（A/B/C/D）并行开工派工单 | 一次性工作单：A/B/C/D 的交付除 E8 外全部落地并 commit；文首"git 当前是干净的、只有 4 个未跟踪 docs"的前提已失效。**仍保留**：`§通用红线` 里"不 commit / 不重启服务，由用户统一做"的约定仍被 `docs/15` 引用 | `docs/13_数据流与复用机制.md` + `docs/14_决策与规划质量优化.md` + `docs/目前缺陷.md` |

### 已删除的归档（2026-09-21 清理）

以下三份已从本目录**删除**，不再保留原文——它们描述的是从未存在、也不会存在的系统：

| 文件 | 为什么可以删 |
| --- | --- |
| `BENCHMARK_SPEC.md` | 设计稿与最终实现几乎零重合：case 格式（每例一个 JSON vs 实际 `*.jsonl`）、规模（≥20/≥20/≥10 vs 实际 33 例）、指标名、baseline 文件名、UX LLM Judge 全部未被采纳。现状看 `docs/07_评测体系.md` + `benchmark/README.md` |
| `TRACE_BADCASE_SPEC.md` | 同上：产物是 `outputs/<run_id>/trace.jsonl` 而不是 `runs/<run_id>/trace.json`，`component` 是 11 类不是 10 类，severity 没有 `critical`，25 类 badcase 分类几乎不与实现重合。现状看 `docs/05_Trace与可观测性.md` + `docs/06_BadCase机制.md` |
| `ITERATION_LOG.md` | 只有 Iteration 000 + 空模板，001 至今空白；文中 11 条问题全部转引自 `docs/ACCEPTANCE.md` §36.7 与 `ARCHITECTURE_CURRENT.md` §7，本身不承载独有事实 |

需要原文时用 `git show HEAD:docs/discard/<文件名>` 取回，或 `git checkout HEAD -- docs/discard/` 整目录恢复。

## 还在 `docs/` 里的"设计稿"

以下几份同样是设计期产物，但**仍保留在 `docs/`**，因为它们被代码注释当作章节锚点引用
（`PRD §x` 176 处、`FRONTEND_DESIGN` 43 处、`START.md §x` 13 处；口径 = `app/` + `tests/` +
`scripts/` + `benchmark/` + `main.py` + `frontend/`）或承载一次性实测记录：

- `PRD.md` / `START.md` / `FRONTEND_DESIGN.md`：设计期文档，**代码注释引用的章节锚点，不要删**。
- `ACCEPTANCE.md`：2026-09-18 的一次性验收实测记录（含 4 个 run_id 与产物数字快照）。
- `TravelPlan_UI_优化方案.md`：改造设计稿 + P0/P1/P2 排期，文首已标注与实现的差异。
- `14_决策与规划质量优化.md`：改造设计稿，但逐节实现状态表已随代码同步，因此同时出现在
  `docs/README.md` 的"当前实现的说明书（01~15）"里——**它不是过时文档，别当纯设计稿读**。
- `TravelPlan_Admin_整体优化方案.md`：管理后台设计稿，已由 commit `437aca9` 实现，现在是
  "管理台为什么长这样"的规格说明。
- `目前缺陷.md`：缺陷台账，逐条标注了"已修 / 仍开"。

这些文档里凡是写"当前 / 目前"的段落，读之前请先看它开头的状态横幅。
