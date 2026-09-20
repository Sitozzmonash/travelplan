# TravelPlan 性能优化 Before / After 报告

> 对应任务：`D:\Downloads\TravelPlan_性能优化与生产部署任务.md`（Part H / K / O）。
>
> 判定口径：优化是否有效，看**用户感知耗时**，而不是某一步的自我感觉：
>
> ```text
> t0 建 Session ──► t1 Discovery 完成 ──► t2 点「开始规划」 ──► t3 Run 终态
>
> discovery_s = t1 - t0   用户在前端选偏好时后台已经查完的部分（可被复用）
> plan_s      = t3 - t2   点「开始规划」后真正让用户等的时间  ← 主要优化目标
> total_s     = t3 - t0   从打开页面到拿到行程
> ```

## 1. 优化前（BEFORE）基线

数据来自真实 run 的产物（`outputs/<run_id>/trace.jsonl` + `audit_report.json` + `metrics.json`），
机器可读快照见 `_acceptance/perf_before.json`。

两次真实 Guided run（都**完全没有复用** Discovery 结果，`prefetch_reused` 全为 false，所以是"全量重复查询"的最坏情况）：

| run_id | 总耗时 | LLM 调用 | Jev | 工具调用 | Token | Provider 失败 |
| --- | --- | --- | --- | --- | --- | --- |
| `tp-20260920-164133-9291f8` | **616.0s** | 8 | 3 | 48 | 36968 | 12 |
| `tp-20260920-205658-ead2ba` | **518.2s** | 7 | 3 | 49 | 31868 | 13 |

### 1.1 阶段耗时（BEFORE）

| 阶段 | 616s run | 518s run | 真正的成因（实测） |
| --- | --- | --- | --- |
| `search_intercity_transport` | 140.1s (22.7%) | **210.2s (40.6%)** | 12306 `get-tickets` 2 次共 110.2s（平均 55.1s）+ 途牛航班 35.6s，**去程/回程、火车/航班全部串行**，最多 4 次串行 Provider 调用 |
| `extract_and_normalize_places` | **172.0s (27.9%)** | 132.2s (25.5%) | 4 次**串行** LLM `extract_places`（63.6s）+ 16 次**串行** 高德 `search_poi`（54.5s） |
| `critic_and_revise` | **110.0s (17.8%)** | 26.0s (5.0%) | 1 次 LLM `critic`（该次 108.8s，纯模型抖动） |
| `verify_poi_and_routes` | 77.6s (12.6%) | 58.5s (11.3%) | 高德 `get_poi_detail` 6 次（最大单次 43.5s）+ `route` 9 次，均为串行 |
| `finalize` | 47.5s (7.7%) | 46.5s (9.0%) | 1 次 LLM `final_answer`（用户可读正文，保留） |
| `search_social_guides` | 25.5s (4.1%) | 23.3s (4.5%) | 1 次 LLM `query_expansion` 13.5s + 社媒 Provider |
| `score_candidates` | 25.9s (4.2%) | 13.8s (2.7%) | 途牛门票查询 |
| `search_hotels` | 3.6s (0.6%) | 5.4s (1.0%) | 途牛酒店 1 次 |
| `parse_intent` | 12.0s (2.0%) | 0.0s (0.0%) | Guided 模式已不再重新解析 Intent |

### 1.2 三个真正的瓶颈（都有实测证据）

1. **同一个 Session 内重复查询 Provider**：`tp-...-ead2ba` 的审计里
   `prefetch_available: []`、`prefetch_reused: {transport:false, hotels:false, social:false, places:false}`。
   首页/Discovery 查过一次的东西，12 步 Workflow 又原样查了一遍。
2. **无意义串行**：火车与航班、去程与回程、16 次 POI 关键词搜索、多个 POI 的门票/详情/路线，
   全部一次一个地等。
3. **LLM 调用量大且长**：7~8 次调用合计 147.0s（518s run，28%）/ 312.5s（616s run，51%），
   其中 `extract_places` 4 次串行 63.6s 在 Guided 模式下**查完就被丢弃**
   （`node_extract_places` 先跑抽取循环、再到「复用 Discovery places」的提前返回）。

### 1.3 另一个自查发现的埋点 bug

`_emit_run_spans` 里 provider / mcp / tool / llm 这些子 span 的 `started_at` / `finished_at`
取的是**落盘时刻**而不是真实调用时刻，于是所有子调用的时间戳都挤在 run 结尾，
看起来"每个 Provider 都跑了 280~400s"。`duration_ms` 是正确的，
所以**单次耗时与总计可信、时间先后不可信**。修好它之前，
Perf 页面上的"最近一次调用时间"和甘特图都不可信。

## 2. 优化后（AFTER）

见下方 §3，由 `scripts/e2e_real.py` 真实跑出后再填。

## 3. 结论

待补。
