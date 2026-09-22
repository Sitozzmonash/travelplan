# 用户旅程

TravelPlan 有两个入口，最终都跑同一个执行体：SuperHarness Agent Loop
（`app/agent_runner.py::execute_agent_run`，见 [主规划：Agent Loop](../architecture/AGENT_LOOP.md)）。

## Guided（Web 默认）

```text
首页「基础信息」         出发地 / 目的地 / 日期 / 天数 / 人数 / 预算（唯一入口）
  │  提交 → 跳 /guided?…（基础信息经 URL 带过去，向导里不再重复填一遍）
  ▼
创建 Planning Session    POST /api/v1/planning-sessions
  │  discovery_enabled 且目的地非空 → 后台立刻 Prefetch：
  │    交通 / 酒店 / 攻略 / POI（先查城市知识库，命中就只现查交通与酒店）
  │  前端轮询会话直到 READY / PARTIAL，第 1 页直接展示 POI 候选
  ▼
第 1 步「探索确认」      住宿区域、景点/美食候选，用户标 必去(MUST) / 想去(WANT) /
  │                     不感兴趣(REJECT)；PATCH 只重排序，不重新取数
  ▼
第 2 步「偏好」          交通 / 酒店 / 节奏一页选完
  │  「开始规划」→ PATCH 偏好 → POST /api/v1/planning-sessions/{id}/start
  ▼
正式 Run                start_run()：此刻才 create_run(source="guided")
  │                     execute_agent_run(intent=已确认的结构化事实, prefetch=本次会话的候选)
  │                     Agent 循环：自己调工具、自己控制预算与时间 → submit_final_plan
  ▼
结果页 /plan/{run_id}    轮询 GET /api/v1/plans/{run_id}/status，
                         读 run_progress.current_stage 显示「在查机票」「在查酒店」…
                         结束后展示行程、预算、来源与规划依据
```

会话状态、TTL 和 API 形状由 `app/sessions.py` 与 `app/api.py` 定义；取数只在 `app/discovery.py`
实现。用户点选如何影响结果由 `app/selection.py` 解释（归一、节奏推导、推荐分类），
其结论与其他已确认字段一起作为**结构化事实**注入主规划的 user prompt —— 规划本身由 Agent 承担，
Python 不再对候选做 MUST / REJECT 硬排除（见 [主规划：Agent Loop](../architecture/AGENT_LOOP.md) §2）。

会话事件时间线（存在 `planning_sessions.events_json`，管理端默认折叠）：

```text
session_created
discovery_queued
transport_prefetch_started / hotel_prefetch_started / social_discovery_started /
place_extraction_started
transport_prefetch_finished / hotel_prefetch_finished /
social_discovery_finished / place_extraction_finished
discovery_finished / discovery_failed
user_preferences_updated
session_confirmed / run_started
session_cancelled / session_expired
```

## Quick（CLI / 兼容入口）

用户用一句自然语言描述旅行需求：`POST /api/v1/plans {message, background:true}` 或
`python main.py -m "…"`。没有会话与预取，Agent 直接从零开始查。

## 用户可获得的结果

- 真实数据来源、查询时间和可用性说明；
- 逐日安排、住宿、交通、预算与可行性判断；
- 可追溯的运行审计（Agent 每一步调了什么、每次模型调用的 token）、实时阶段进度与 Bad Case；
- 无法获得真实数据时的明确降级信息，而不是补造数据。

数据结构与复用机制见 [数据复用与实体](../architecture/DATA_REUSE_ENTITY.md)；
观测口径见 [Trace 与可观测性](../architecture/OBSERVABILITY.md)。
