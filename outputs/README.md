# 历史选型、访谈与设计草案

分类核对：2026-09-14。本目录三份原始材料就地归档并保留原字节、固定外部版本和历史讨论；不再作为活动产品规范或开发派发入口。

| 原始材料 | 时点与独有内容 | 当前承接 |
|---|---|---|
| [Toil 类框架研究](toil-like-heavy-framework-report.md) | 2026-09-04；固定 Toil/Bernstein 版本的选型分析与 Bernstein-first 建议 | [架构来源](../docs/architecture/sources.md)保留选型来源；旧建议不是当前采用或执行资格 |
| [第一版产品草案](karajan-design-blueprint.md) | 2026-09-05；早期访谈、需求到 PR 的交互与模块讨论 | [产品 PRD](../docs/prd/karajan-v1.md)、[工作台 PRD](../docs/prd/commander-workbench.md)、[架构导航](../docs/architecture/README.md) |
| [路由与配额草案](karajan-routing-and-quota-design.md) | 2026-09-05；多来源、Commander、Rulebook 和 ai7-harness 参考分析 | [路由与配额](../docs/architecture/02-routing-and-quota.md)、[外置网关](../docs/architecture/08-provider-gateway.md) |

原文的“当前依据”“已确认”“下一步”、默认值、角色和阶段安排均按各自日期理解；其中指向活动文档的链接可能已随仓库演进，不能用今天链接的内容倒推当年的规格。当前 r8 要求对话 Agent 生成真实配置、同源图表确认及实际部署，运行中由授权角色决定任务拆分和并行规模；自定义 Workflow 可交付 report/patch/pr，早期“从需求到 PR”草案不再限制所有流程。

新设计只在活动 PRD、架构、ADR 与[治理](../docs/planning/design-governance-20260909.md)维护；选择任务看[当前业务顺序](../docs/planning/business-first.md)。这些原始材料因包含独有调查与决策过程而保留，不复制为第二套活动规范，也不凭研究报告认定产品实现、真实来源或隔离已通过。
