# Sonus：流程问题、优化建议与实施计划

日期：2026-09-22

## 范围与边界

本次按用户授权实施下文第 1、2、3 项；第 4～7 项只记录，不实施。保留仓库已有改动，不新增基础设施、不改数据库结构、不操作生产数据、不提交、不部署、不启停服务。验证使用离线假 Provider、临时 SQLite 和前端静态/隔离逻辑检查，不消耗真实模型或第三方接口。

## 当前端到端流程

引导入口：基础信息 → PlanningSession → 后台 Discovery 与用户选择并行 → 确认偏好 → 正式 Run（结构化 intent + 已完成预取复用）。快捷入口：自然语言 → 解析意图 → 同一规划引擎。

正式引擎有 12 个固定节点：parse_intent → search_intercity_transport → search_hotels → search_social_guides → extract_and_normalize_places → verify_poi_and_routes → score_candidates → build_initial_plan → check_budget → check_feasibility → critic_and_revise → finalize。

项目已经具备 Provider 受控并发、往返交通并查、城市缓存、预取溯源搬运、批量地点抽取、实体与查询复用、排除用户拒绝地点、限量详细核验、相邻路线查询、Critic/Jev 并行及最终预算重算。这些不是本次需要从零新增的能力。

## 问题与优化建议

### 1. 会话一致性与前端确认（本次实施，优先级高）

问题：app/sessions.py 的 Discovery 长期持有旧 session 字典，部分/最终回写通过 app/store.py 的整行替换，能覆盖刚提交的 preferences、取消状态、STARTING 和 run_id。start_run 是先读后创建，顺序幂等不等于并发幂等；grace wait 期间取消也未重检。前端 guided-wizard.tsx 的 syncNow 吞掉 PATCH 失败，handleFinish 随后调用的 handleStart 仍读取旧 render 的 pendingPatch：可能同步失败仍开始，也可能用旧失败请求覆盖新偏好。防抖 POI 请求还有在途覆盖风险。

方案：使用现有表的短事务串行化单会话修改，读取最新行后仅更新变化字段；用户更新、Discovery 发布、取消、过期和 grace 事件统一遵守状态边界。正式启动把状态占用与 run 创建放在同一事务，只有赢家提交后台任务。前端串行化同会话 PATCH，取消未发出的防抖，最终明确提交最新完整草稿，成功才 START；使用同步 ref 防重复点击，不把 React setState 当锁。

### 2. 降级不得越过硬过滤（本次实施，优先级高）

问题：app/discovery.py 的 resolve_pois 在全量候选被 resolver 拒绝时会把 raw_places 全部恢复，可能重新引入异地地点或附属设施。prefetch 只在 social.evidences 非空时调用 extract_place_candidates，挡住了该函数已有的纯关键词兜底。

方案：仅允许未被硬规则排除的干净候选降级保留；全被过滤则如实返回空集和说明。社交为空或失败仍运行目的地关键词候选生成，但不伪造社交证据。

### 3. 预取依赖与缓存首屏（本次实施，优先级高）

问题：冷启动先等待 transport/hotels/social 全部结束才抽地点，实际上地点只依赖 social；缓存路径串行查询交通和酒店，查完才发布已缓存 POI。缓存路径未返回 hotel_areas、poi_pools、profile，使 UI 数据契约不完整；实时阶段计时也混成总耗时。

方案：将冷预取改为 transport、hotels、social→places 的依赖结构，画像独立进行，单 worker 不死锁。每个阶段完成即可发布快照，并序列化快照回调，防止晚写的旧快照抹去已完成分支。缓存路径先发布静态候选和来源时间，再并发查交通/酒店、分别计时与增量发布；使用确定性画像基线和现有区域/候选池聚合，保持字段契约，不因缓存命中额外要求 LLM 凭据。

### 4. 完整可行性核验前置（仅记录）

候选比较前的 hard_constraint_violations 只覆盖构建约束，开放时间、返程截止等较晚检查。建议先对候选做确定性可行性检查，再让 Jev 在合法方案中比较，减少选中后反复改排。本次不调整 Planner/Jev。

### 5. 最终文案移出关键路径（仅记录）

finalize 的模型文案阻塞结构化行程持久化与完成状态。建议先保存可使用的结构化结果和确定性文本，再可选异步润色，明确两种完成状态。现有降级逻辑不等于没有完成态保护；本次不改工作流。

### 6. 恢复体验与修改承诺（仅记录）

向导刷新会重新创建会话；部分「重新整理」只重置轮询，不能真正重试已 settled 的 Discovery。修改行程 UI 承诺重排，但后端只排队记录，不能让本地锁定样式冒充已执行。建议补恢复协议、真实重试入口和实际 revision 能力，或收窄文案。本次不扩大 UI 功能。

### 7. 有界任务接纳与持久审计（仅记录）

_RUN_EXECUTOR 工作者有上限但队列无界；启动时 sweep 全部 RUNNING 不适合多实例；部分审计接口依赖容器输出文件。建议任务接纳限流/背压、明确单实例边界或租约机制、持久化审计出口。管理端默认 token 便利路由也应具有生产保护。本次不改部署、鉴权或存储设施。

## 实施计划

1. 在本文件集中记录分析、范围、实施与验证，不另建计划文件。
2. 修改 app/discovery.py：锁住硬过滤边界、恢复空社交关键词路线、移除无关阶段屏障、保护阶段快照；以 Event 同步测试证明地点先于被阻塞交通完成，覆盖失败与 workers=1。
3. 修改 frontend/components/guided/guided-wizard.tsx 及必要草稿转换：最终全量 PATCH → START、显式失败中止、PATCH 队列与重复提交保护；保留现有两步布局。读取安装版 Next 文档后修改，增加不依赖网络/浏览器服务的回归测试。
4. 修改 app/store.py、app/sessions.py：事务性会话更新与启动，状态保护，Discovery 只发布自己的字段，缓存立即发布与实时交通酒店并行。增加取消/开始/偏好与 Discovery 交错及真正双线程启动回归。
5. 合并审查三条线，离线运行相关 pytest 回归、前端类型检查和逻辑测试。临时文件在显式临时目录创建并清理，不保留测试缓存/字节码，不连接生产库。检查最终 diff 与已有用户改动。
6. 在下节补充实际结果与验证局限，不用静态推测冒充生产实测。

## 验收标准

- Discovery 部分与最终写回不会覆盖最新用户偏好，不会复活 CANCELLED/EXPIRED，不会清掉 STARTING/run_id。
- 并发 START 只有一个 run 与一次后台提交；等待期间取消不得启动；STARTING 后草稿不可再改变已提交意图。
- 前端最新 PATCH 失败绝不 START，旧失败请求不覆盖新草稿，在途 POI 完成后才提交最终草稿。
- 全部 POI 被地理/设施硬规则拒绝时保持空集；空社交仍执行关键词发现。
- 地点分支不等待无关交通/酒店；缓存候选在实时调用结束前可用，完成字段不会被较旧部分快照抹除。
- 缓存与冷启动都携带 profile、hotel_areas、poi_pools，保持来源/查询/溯源契约。

## 实施与验证结果

状态：第 1、2、3 项已实现并完成下述离线验证。第 4～7 项未实施。未提交、未部署、未启动/停止服务，未操作生产数据。

### 实际改动

| 文件 | 改动 |
| --- | --- |
| app/store.py | 短事务先锁定会话，再读最新值，只保存变化字段；会话占用、Run 和进度创建同事务；失败回滚；新增路径显式关闭 SQLite 连接。 |
| app/sessions.py | 用户 PATCH 合并最新字段，STARTING 后冻结；取消/过期不复活；Discovery 只发布自己的字段和追加事件；失败保留已完成候选；grace 只读，锁内重新判定可否开始；队列拒绝则如实标记 Run 失败。 |
| app/sessions.py（缓存路径） | 先发布静态候选、原始来源时间与聚合字段，再并发补交通/酒店；分别计时并累计发布。缓存路径不初始化 LLM，画像明确标为均衡基线。 |
| app/discovery.py | 不再复活硬过滤候选；空/失败社交仍走关键词路线；transport、hotels、social→places 与画像独立；单 worker 可完成；回调串行、快照隔离；提供实时/缓存共用的确定性 aggregate_candidate_metadata。 |
| frontend/components/guided/guided-wizard.tsx | 同会话 PATCH 串行，最终锁住编辑、取消防抖、提交完整最新草稿，成功才 START；同步 ref 防双击；旧会话响应不污染重建会话；START 响应丢失时恢复已有 Run。 |
| frontend/components/guided/draft.ts | 完整草稿转换，酒店清空显式传 null、POI 清空传空映射，不让被省略的字段残留旧值。 |
| tests/test_session_consistency_regressions.py | 新增 24 项：真实 SQLite 并发启动/修改、晚到 Discovery、grace 期间取消/过期/删除、冻结输入、事务回滚、提交失败、缓存首批可见及交接、Postgres 包装层提交/回滚协议。 |
| tests/test_discovery_dependency_regressions.py | 新增 19 项：全量硬拒绝、干净降级、空/失败社交、被阻塞机酒前地点就绪、单 worker、调用次数对照、快照单调与隔离、聚合降级。 |
| frontend/tests/guided-sync.test.cjs | 新增 10 项：加载实际 TSX、草稿转换及 HTTP 序列化，在内存 hook/网络替身中验证旧闭包、在途请求、重复点击、字段清空、重建隔离和 START 响应丢失恢复。 |

### 实际验证

- 后端定向回归：**236 passed，1 warning**，约 114 秒。覆盖两个新增回归文件，以及 test_guided_journey、test_discovery_handoff、test_city_cache_reuse、test_city_cache、test_places_pipeline、test_places_resolver、test_preference_profile、test_store、test_store_postgres。
- 前端：`node --test frontend/tests/guided-sync.test.cjs`，**10/10 通过**。
- 前端全项目类型检查：`node frontend/node_modules/typescript/bin/tsc --noEmit --incremental false --project frontend/tsconfig.json`，退出码 0。
- 两个修改过的向导 TS/TSX 文件执行 ESLint（`--no-cache`），退出码 0。
- 修改文件执行 `git diff --check`，无空白错误。仅有 Git 的 LF/CRLF 换行提示，未修改 Git 配置。
- 核对工作区，无本轮临时测试目录残留；未新增 pytest 缓存或 Python 字节码。原有 `_deploy_check.py` 未触碰。

后端执行前在独立 Python 进程内移除 DATABASE_URL 和第三方密钥、禁用 dotenv、把默认库和 pytest basetemp 指向随后清理的 D 盘临时目录；禁外部 socket 连接，仅允许 Windows asyncio 自身 socketpair。使用 `python -B`、`PYTHONDONTWRITEBYTECODE=1` 和 `-p no:cacheprovider`。未直接使用项目默认数据库。唯一 warning 是已安装 Starlette 引用 AnyIO 旧别名的弃用提示，不是本次用例失败。

首次扩展验证中，过严的 socket 拦截也挡住了 Windows asyncio 的内部 socketpair，导致两项 TestClient 测试无法启动；只对内部 socketpair 放行后，API 用例及最终完整定向回归均通过。未为此放开外网。

### 验证边界与剩余限制

本轮不是全仓测试、生产性能测试或浏览器端到端验收。前端验证的是实际组件/请求逻辑与类型、lint，没有启动服务、构建或截图；未实连 PostgreSQL/Neon，Postgres 覆盖为真实方言包装 + 假驱动的 SQL/事务协议检查，数据库并发行为实际运行的是 SQLite。

本地最近的 metrics 样本存在大量 Provider 失败，不适合作为健康线上基准，不据此承诺百分比加速。本轮用 Event 阻塞测试证明依赖顺序、首批结果可见性，以及正常路径 Provider/LLM 调用次数不增加。

事务消除了并发重复启动和状态覆盖，但没有引入持久任务队列；进程若恰在事务提交后、后台提交前退出，仍需第 7 项的任务恢复机制。取消或 STARTING 会阻止迟到 Discovery 修改草稿，但不会强制中断已发出的第三方请求。没有清洗历史城市缓存或生产库；修复的是新发现结果的降级边界。缓存画像采用明确的 fallback 基线，正式规划仍按最终用户偏好生成画像。
