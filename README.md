# Karajan

面向个人的多来源 Agent 代码交付平台：Commander 理解需求，用户确认计划，平台按 Rulebook 和服务配额组织实现、测试、审查并交付 PR。

## 产品需求与实施规划

- [PRD v1](docs/prd/karajan-v1.md)：用户故事、功能需求、可观察验收和首版边界；[GitHub 父任务 #1](https://github.com/zhouy1017/Karajan/issues/1)。
- [M0–M4 路线图](docs/planning/roadmap.md)：M0 探针顺序、阶段出口与后续拆票时机。
- [GitHub Issues](https://github.com/zhouy1017/Karajan/issues)：任务状态与依赖关系；[跟踪约定](docs/agents/issue-tracker.md)。

PRD、8 个 M0 Issues、20 个 M1–M4 实现任务和独立 CI 任务已发布。M0 契约、执行恢复、预算和运行探针已分批实现，本地项目工作台已可登记仓库与预览、保存配置；真实账户资格尚未通过。后续任务见 [完整任务清单](docs/planning/v1-backlog.md) 与 [需求覆盖审计](docs/implementation/requirement-coverage.md)；GitHub 实际编号见 [发布记录](docs/planning/v1/github-publication.json)。

本轮不进行现金 API 调用，相关真实验收保持 `not_run`；本地进程、假 provider 和协议回放继续推进。开发验证方式见 [测试与合并质量门](docs/implementation/testing-gates.md)。

## 本地工作台

[启动说明与实际验证](docs/implementation/m1-local-workbench.md) 包含依赖安装、构建和本机运行方式。服务启动后使用本地文件中的一次性访问码登录；项目状态、配置预览和版本化保存使用实际 SQLite。[需求与计划页面](docs/implementation/m1-run-workbench.md) 可保存需求、审阅并确认既有计划、决定指定 Commander 交接。当前尚不能从页面派发真实模型任务。

[交付协调协议](docs/implementation/m1-delivery.md) 已提供本地可运行验证：固定候选推送、同一 PR 身份恢复、当前提交 CI 与暂停/取消。示例使用本地 Git 和明确的 PR 替身，生产交付仍等待当前候选权威、真实凭据与执行资格接线。

## 完整架构设计

从 [Karajan 完整架构设计 v1](docs/architecture/README.md) 开始阅读。它是当前设计入口，覆盖：

- 模块职责、数据模型、任务状态、崩溃恢复和取消。
- 多来源执行配置、Rulebook 矩阵、共享配额池、预算与换源。
- 订阅与 API 执行、隔离、跨 Agent 上下文、候选验证和 PR 交付。
- Web/执行器接口、配置示例、技术组合、实施阶段和验收矩阵。

状态：2026-09-05 已完成审阅并由用户确定为 v1 设计基线，现已进入 M0 接口探针实现。设计确认不代表真实账户/执行器资格通过。审阅结果见 [决定记录](docs/architecture/06-review-and-decisions.md)，术语见 [CONTEXT.md](CONTEXT.md)。

## 报告

- [重度 Toil 类框架：底座选型与 Bernstein-first 设计报告](outputs/toil-like-heavy-framework-report.md)

报告比较了 Toil、Bernstein、Claim Plane、Agent Workspace Fabric 等项目。后续设计已按用户目标收敛到 PR，并依据接口核查将 Bernstein 调整为须通过资格验收的复用候选；当前状态所有权以完整架构为准。

## 前期设计材料

- [Karajan 第一版设计草案](outputs/karajan-design-blueprint.md)：已确认的个人单机、Web 工作台、计划确认后自动交付 PR 的产品范围，以及模块职责、状态语义和分阶段验收建议。
- [领域术语](CONTEXT.md)：需求、计划、任务、运行、执行尝试、候选变更与交付的统一含义。
- [多来源路由与配额设计](outputs/karajan-routing-and-quota-design.md)：订阅与 API 混合接入、Commander 协作、Rulebook 矩阵、配额分配和跨服务换源。
