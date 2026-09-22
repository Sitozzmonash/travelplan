# TravelPlan 文档

这里仅放当前业务项目的说明；一个主题只保留一个当前入口。设计稿、复盘和过期版本保留在
`archive/`，不作为实现事实来源。问题、缺陷、修复和待办只放在仓库根目录的
[`feedback/`](../feedback/)。

判断"现在到底怎么跑的"以**代码**为准；文档与代码不一致时，先改文档，确实代码有问题就记进
[反馈收件箱](../feedback/inbox/)。

## 产品

- [PRD](product/PRD.md)：设计期契约（开头列了已知过时项，勿当实现说明书）；
- [用户旅程](product/USER_JOURNEY.md)：Guided 与 Quick 的当前流程；
- [用户端 UI](product/UI.md)：前端页面职责与进度展示契约；
- [管理后台](product/ADMIN.md)：后台页面与三处 Agent 相关口径。

## 架构

- [总览](architecture/OVERVIEW.md)：分层、目录、一次请求的数据流、边界
- [主规划：Agent Loop](architecture/AGENT_LOOP.md)：主路径的流程、工具、护栏、交卷、观测、产物
- [数据复用与实体](architecture/DATA_REUSE_ENTITY.md)：城市知识库、实体层、预取与复用
- [数据源与工具](architecture/PROVIDERS_TOOLS.md)：Provider、工具集、降级语义、调用账本
- [Trace 与可观测性](architecture/OBSERVABILITY.md)：状态机、每步留痕、产物、管理端读什么

## 质量

- [Bad Case](quality/BAD_CASE.md)
- [Benchmark](quality/BENCHMARK.md)（含"当前覆盖的是已退役流程"的警告）
- [Evolution](quality/EVOLUTION.md)

## 运维与状态

- [开发](operations/DEVELOPMENT.md)
- [配置](operations/CONFIG.md)
- [部署](operations/DEPLOYMENT.md)
- [路线图（只列未完成）](status/ROADMAP.md)

## 历史与反馈

- [历史归档](archive/)：旧方案、复盘和迁移前快照；
- [反馈收件箱](../feedback/inbox/)：待处理问题、缺陷与建议；
- [已处理反馈](../feedback/done/)：已完成修复的记录。
