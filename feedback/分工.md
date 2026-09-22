# 分工（feedback 里的活，两个角色 A / B）

> **怎么用**：用户会直接说「你是 A」或「你是 B」。只读自己那一节 + 「共享契约与红线」就能独立开工。
> 两条线**可以并行**：A 只碰 `app/**` 与 `scripts/**`，B 只碰 `frontend/**` —— **文件零重叠**，所以不需要跟对方协调。
> 统一验证 / 构建 / 部署由用户做。**不要 `git add` 对方的文件、不要 commit、不要 push。**

来源：`feedback/` 下全部 6 份（含两份非本轮写的：`TravelPlan_Agentic_Reuse_Dedup_改造方案.md`、`TravelPlan_Admin_Feedback_01.md`）。

---

## 角色总览

| 角色 | 定位（一句话） | 负责 |
| --- | --- | --- |
| **A** | 后端·地点实体底座与 agentic 执行：让"同一个地点"有唯一身份，让复用与工具调用在服务端成立 | Resolver + Canonical Place、cache-first、Trace/token 补字段、仪表板聚合、脏数据重建 |
| **B** | 前端·把两页向导做干净 + 把管理台的信息补齐 | 向导砍页与删字段、排版、管理台 token/质量分/Trace 双视图、Dashboard 提速 |

**为什么这么切**：A 是"数据对不对"，B 是"用户与运营看不看得见"。两者耦合只有一个地方——**API 返回哪些字段**（见契约 1），定了就能并行。

---

## 共享契约与红线

### 文件所有权（谁都不许越界）

| 路径 | 归属 | 规则 |
| --- | --- | --- |
| `app/**`、`scripts/**` | **A** | A 独占。B 一行都不改 |
| `frontend/**` | **B** | B 独占。A 一行都不改 |
| `docs/**`、`feedback/**` | 只读 | 双方都可以改**自己那条线对应的**文档；不确定就不改，交给用户 |

**A 改 `app/api.py` 时**：只**追加** B 需要的字段/端点，不改已有字段语义。
**B 需要新字段**：先在 `frontend/types/` 里自己加，再按契约 1 找 A 补后端 —— 不要自己在后端加。

### 契约 1：B 需要的服务端字段（由 A 产出）

B 本轮**必须依赖**这几个字段（没有就只能显示"—"）：

| 字段 | 用途 | 现状 |
| --- | --- | --- |
| 窗口内 `avg_tokens_per_run` | 管理台 KPI「平均每个规划的 token」 | ❌ 需 A 新增（`admin_analytics` 里 token 零命中） |
| `total_tokens`（历史累计） | 管理台「累计用量」 | ✅ 已有（`/admin/overview`） |
| `quality_score` | KPI「行程质量分」 | ✅ 已有（`admin_analytics.py:901`） |
| llm span 的 `input/output/cached tokens` | Run Trace 与 llm 调用列表 | ❌ 需 A 在 `LLMResult.to_audit()` 带上 `usage` |
| llm 的 `system/user/assistant preview` + `prompt_hash` | Trace 双视图 | ❌ 需 A 新增（脱敏 + 截断） |

### 红线（与仓库既有约定一致，两个角色都适用）

1. **事实不得由模型改写**：坐标 / 地址 / 营业时间 / 价格一律来自 Provider，LLM 只做筛选与归类。
2. **不许静默降级**：任何"没做到"都要如实写进 `degradations` / `status` / `error`，宁可空榜也不编。
3. **不许空榜**：新增的 LLM/工具链路必须有确定性兜底（LLM 不可用时退回现在的行为）。
4. **不破坏既有测试语义**：`tests/test_workflow.py::test_the_model_is_only_consulted_at_the_documented_sites` 会被 agentic 化打破 —— **改它之前必须先说明**（见 A3）。
5. **仓库可能被另一个会话并发写**：动手前 `git status` 看清自己的工作区，别把别人的改动当自己的。

---

## A · 后端：地点实体底座与 agentic 执行

依据：`feedback/TravelPlan_Agentic_Reuse_Dedup_改造方案.md`（主）、`feedback/2026-09-21-探索确认-候选地点重复与误分类.md`、`feedback/2026-09-21-入库应由-LLM-调用工具完成.md`、`feedback/TravelPlan_Admin_Feedback_01.md` §1–§3。

### P0 —— 先把"同一个地点只有一个身份"做出来

| # | 任务 | 出处 |
| --- | --- | --- |
| A1 | 建 **Canonical Place** 实体：`places` / `place_aliases` / `place_provider_refs` / `place_evidence` / `poi_query_cache`（字段见方案 §7） | 方案 §6–§7 |
| A2 | **Entity Resolver**：按强 / 中 / 冲突信号判断"是不是同一个地点"（方案 §9） | 方案 §9 |
| A3 | **所有 `search_poi` 前强制先过 Resolver**；高德不再当去重依据 | 方案 §15/§16、P0-5/6 |
| A4 | **Tool 调用强制 Prefetch / Cache First**（A8 的预热结果必须先被读到） | 方案 §17、P0-6 |
| A5 | 修 `city_poi_mentions` 恒为 0：抽取出的地名没写回 `evidence.place_mentions`（0/213 有值） | 重复与误分类 §二 |
| A6 | 检索加**地理围栏**：成都缓存里混进了乐山大佛 | 重复与误分类 §三 |

### P1 —— 复用与可观测

| # | 任务 | 出处 |
| --- | --- | --- |
| A7 | Cross-session **Query Cache**（`poi_query_cache`），且缓存**不直接决定实体** | 方案 §13/§14、P1-8 |
| A8 | 景区 **parent / child** 关系：子设施（地铁站/停车场/售票处/游客中心）不再当并列候选 | 方案 §10、P1-9 |
| A9 | 连锁门店 / 分店特殊规则 | 方案 §11、P1-10 |
| A10 | Evidence → Canonical Place 重新挂载 | 方案 P1-11 |
| A11 | **Resolver Trace**：每次归并/丢弃都要留痕（谁被并进谁、为什么） | 方案 §21、P1-12 |
| A12 | **llm span 补 token**：`LLMResult.to_audit()`（`app/llm.py:91`）漏掉了 `usage` —— run 级总量在 `run_metrics` 里有，但每个调用没有 | 用户提问 |
| A13 | Trace 补**脱敏预览**：`system/user/context/assistant preview` + `prompt_version/prompt_hash`，截断 + Secret 脱敏 | Admin Feedback §3 |
| A14 | 仪表板聚合：**平均每个规划的 token**（口径：窗口内、分母为"有 token 快照的运行"）；`总 token` 保持历史累计 | `2026-09-21-仪表板指标-质量分与token消耗.md` |
| A15 | Dashboard 提速：Run 完成即存**质量摘要**、聚合结果 **30~60s 缓存**、能用一条 SQL 别拆多次 | Admin Feedback §1 |
| A16 | 质量分**动态化**：按本次 `Preference Profile` 调权重（美食型/老人型/景点型） | Admin Feedback §2 |

### P2 —— 收尾

| # | 任务 | 出处 |
| --- | --- | --- |
| A17 | 低置信度 match 人工审计、自动学习 alias、Resolver Benchmark、错并/漏并 Bad Case | 方案 §20 P2 |
| A18 | **脏数据重建**：Neon 里 20 城 × 20 条 = 400 条全带子设施扎堆 + 误分类。**必须显式处理**（过期不清、`allow_stale` 照读、不同 `place_id` 覆盖不掉）。顺序：**先修代码再清库重建**，用 `scripts/preheat_cities.py --cities ...` 逐城替换 | 重复与误分类 §六 |

### A 的注意事项

- 清库只清 `city_pois` / `city_poi_mentions` / `city_evidences` / `city_cache_meta`；**run 级 `places` 表不动**（那是历史 run 的证据链）。
- 重建**不依赖 TikHub**（现在这份缓存本来就是网页来源），但别把 `city_evidences` 一起删了再等社媒恢复 —— Tavily 现在可用。
- A3 会把"模型只在 6 个调用点被问"那条测试打破，**先跟用户确认怎么改**（改断言 vs 把 agent 化范围框住）。

---

## B · 前端：两页向导 + 管理台信息补齐

依据：`feedback/2026-09-21-引导式流程-首页与基础信息重叠等.md`、`feedback/2026-09-21-仪表板指标-质量分与token消耗.md`、`feedback/TravelPlan_Admin_Feedback_01.md`。

### B1 —— 向导从 3 页砍到 2 页（最高优先）

| # | 任务 | 出处 |
| --- | --- | --- |
| B1.1 | 删掉向导第 1 页「基础信息」。首页已有 出发地/目的地/日期/天数/人数，**6 项里 5 项重复** | 引导式流程 §一 |
| B1.2 | **预算**（第 1 页唯一不重叠项）挪到首页；「按返程日期」模式也要给落点 | 引导式流程 §一 |
| B1.3 | 首页「开始规划」直接进**探索确认**（POI 列表），选完再到偏好页 | 引导式流程 §一 |
| B1.4 | 删酒店「**最低星级**」+「**接受换酒店吗？**」，并清掉配套死代码：`starFilterAvailable` / `allowChangeAvailable` / `dropStarFilter()` / `dropAllowChange()` / `hotelDetailLabels()` 的 `includeStar` / `includeAllowChange` 分支；**摘要卡里的对应行也要去掉** | 引导式流程 §二 |
| B1.5 | 排版整齐：偏好页三块间距（现在 `gap-7/6/5` 不统一）、酒店删字段后栅格会落单项、基础信息页三种控件高度不一（删页后自然消失） | 引导式流程 §三 |

### B2 —— 管理台

| # | 任务 | 出处 |
| --- | --- | --- |
| B2.1 | KPI 行加「**平均每个规划的 token**」；「总 token」保留在「累计用量」折叠卡（历史量，别和窗口 KPI 同行） | 仪表板指标 §一/§三 |
| B2.2 | Dashboard 打开提速到 **2~3 秒**、Travel Quality **3 秒左右**（前端侧：少发请求、别重复拉、展开才加载） | Admin Feedback §1 |
| B2.3 | 质量分按用户偏好**动态**呈现（数值由 A 提供，B 只负责展示与解释口径） | Admin Feedback §2 |
| B2.4 | Run Trace **双视图**：`Workflow View`（按 12 Stage）/ `Conversation View`（System→User→Context→Assistant→Tool→Tool Result）；预览默认摘要、点击展开 | Admin Feedback §3 |

### B 的注意事项

- B 需要的新字段先在 `frontend/types/` 自己加，**等 A 补后端**（契约 1）；在 A 补齐前用"—"占位，不要在前端假造数字。
- B1.4 删字段时，**别把"前端不发了"当成"后端不认了"** —— 后端 `capabilities` 与 PATCH 白名单要不要跟着收，属于 A 的范围，跨线问题先问用户。
- 改完要能证明"用户看到的少了两页"，不是只删代码：构建通过 + 真在浏览器里走一遍。

---

## 验收与交付

- **各自自测**：A 跑后端相关测试 + 至少一条端到端；B 跑 `npm run typecheck && npm run lint && npm run build`，并在浏览器里走一遍新流程。
- **统一验证**：由用户做（全量测试、部署、真机验收）。
- **不许 commit / push**：改完留在工作区，由用户统一提交。
- **写清"没做到什么"**：任何降级、占位、未验证项都要在交付说明里点出来，别让用户以为"完成 = 全对"。
