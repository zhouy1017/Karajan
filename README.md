# Karajan

面向个人的多来源 Agent 代码交付平台：Commander 理解需求，用户确认计划，平台按 Rulebook 和服务配额组织实现、测试、审查并交付 PR。

## 产品需求与实施规划

- [PRD v1](docs/prd/karajan-v1.md)：用户故事、功能需求、可观察验收和首版边界；[GitHub 父任务 #1](https://github.com/zhouy1017/Karajan/issues/1)。
- [M0–M4 路线图](docs/planning/roadmap.md)：M0 探针顺序、阶段出口与后续拆票时机。
- [GitHub Issues](https://github.com/zhouy1017/Karajan/issues)：任务状态与依赖关系；[跟踪约定](docs/agents/issue-tracker.md)。

PRD 与 8 个 M0 Issues 已发布，原生子任务及阻塞依赖已核对。首个可开始任务是 [M0-01：契约与资格报告 #2](https://github.com/zhouy1017/Karajan/issues/2)。尚未开始平台实现或真实账户资格测试。

## 完整架构设计

从 [Karajan 完整架构设计 v1](docs/architecture/README.md) 开始阅读。它是当前设计入口，覆盖：

- 模块职责、数据模型、任务状态、崩溃恢复和取消。
- 多来源执行配置、Rulebook 矩阵、共享配额池、预算与换源。
- 订阅与 API 执行、隔离、跨 Agent 上下文、候选验证和 PR 交付。
- Web/执行器接口、配置示例、技术组合、实施阶段和验收矩阵。

状态：2026-09-05 已完成审阅并由用户确定为 v1 设计基线；下一阶段是 M0 接口资格探针。尚未实现平台或完成真实账户/执行器资格测试。审阅结果见 [决定记录](docs/architecture/06-review-and-decisions.md)，术语见 [CONTEXT.md](CONTEXT.md)。

## 报告

- [重度 Toil 类框架：底座选型与 Bernstein-first 设计报告](outputs/toil-like-heavy-framework-report.md)

报告比较了 Toil、Bernstein、Claim Plane、Agent Workspace Fabric 等项目。后续设计已按用户目标收敛到 PR，并依据接口核查将 Bernstein 调整为须通过资格验收的复用候选；当前状态所有权以完整架构为准。

## 前期设计材料

- [Karajan 第一版设计草案](outputs/karajan-design-blueprint.md)：已确认的个人单机、Web 工作台、计划确认后自动交付 PR 的产品范围，以及模块职责、状态语义和分阶段验收建议。
- [领域术语](CONTEXT.md)：需求、计划、任务、运行、执行尝试、候选变更与交付的统一含义。
- [多来源路由与配额设计](outputs/karajan-routing-and-quota-design.md)：订阅与 API 混合接入、Commander 协作、Rulebook 矩阵、配额分配和跨服务换源。
