# 用户端 UI

用户端是 Next.js 前端（`frontend/`）。首页收集基础信息并作为 Guided 的唯一入口；
Quick 输入保留为兼容入口。

## 页面职责

- 首页（基础信息 + Quick 输入）：`frontend/app/page.tsx`；
- Guided 两步向导（探索确认 → 偏好）：`frontend/app/guided/` + `frontend/components/guided/`；
- 规划进行页（轮询阶段进度）：`frontend/app/plan/[id]/` +
  `frontend/components/plan-pending.tsx` + `frontend/components/planning-progress.tsx`；
- 结果工作台（地图、时间线、住宿、交通、预算、来源、规划依据）：
  `frontend/components/plan-workspace.tsx` 及其子组件。

## 进度怎么显示（与 Agent Loop 的契约）

规划是 Agent 循环，**步骤顺序不固定**，所以前端不画"12 步进度条"，而是：

- 大字标题取 `GET /api/v1/plans/{id}/status` 的 `progress.current_stage` ——
  这是后端按 Agent 实际调用的工具写下的中文文案（「在查机票」「在查酒店」「在思考行程」…）；
- 下方步骤列表来自 `run_stages`（同一个工具的第 N 次调用共享一行，最新状态覆盖）；
- 轮询间隔 1~2 秒，不用 SSE。

文案映射的唯一来源在后端（`app/travel_tools.py::TOOL_DISPLAY_NAMES`）；前端不自己维护工具名表。

## 边界

UI 只呈现与收集选择，不复制 Provider 调用、规划或业务判定。用户选择通过 Planning Session API
写入；正式 run 才是可审计的规划产物。

产品流程见 [USER_JOURNEY.md](USER_JOURNEY.md)；管理端见 [ADMIN.md](ADMIN.md)。
