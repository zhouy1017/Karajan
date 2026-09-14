# Karajan

Karajan 是面向个人、以仓库项目为上下文的多来源 Agent 工作流平台。用户用文字描述工作方式，由 Workflow Designer 编写角色与流程配置，借助流程图和职责表修改确认，再部署到 Karajan 引擎。流程可以产出报告、代码补丁或 PR；代码到 PR 是内置模板。

当前设计基准为 **2026-09-14 r8**：不同 provider 默认经外置 CLIProxyAPI 接入；角色和 Workflow 可定制、版本化复用；图表与实际执行配置同源。Workflow 中获授权的调度角色按实际工作决定拆分、分工、依赖与并行规模，Karajan 校验并执行这些决定，不预置 coding Agent 人数上限或前后端拆分方式。详见 [角色调度契约](docs/architecture/11-role-directed-scheduling.md)。

## 从这里开始

| 目的 | 入口 |
|---|---|
| 了解整个设计及架构、设计部署、运行三张图 | [架构总览](docs/architecture/README.md) |
| 查找当前设计、实现证据或历史材料 | [文档导航与维护规则](docs/README.md) |
| 理解用户如何对话、改图表、确认和观察结果 | [Commander Workbench PRD](docs/prd/commander-workbench.md) |
| 查看完整功能与验收范围 | [产品 PRD](docs/prd/karajan-v1.md) |
| 查术语和重要决定 | [领域术语](CONTEXT.md)、[ADR 索引](docs/adr/README.md) |
| 开发或恢复工作 | [当前业务顺序](docs/planning/business-first.md)、[Issue 跟踪流程](docs/agents/issue-tracker.md) |
| 本地启动和验证已有实现 | [实现文档导航](docs/implementation/README.md)、[启动说明](docs/implementation/m1-local-workbench.md)、[测试质量门](docs/implementation/testing-gates.md) |

## 设计与实现状态

r8 是目标设计。仓库已有项目/会话/草稿、计划与批准、路由/资源账本，以及部分执行、候选检查和审查基础；这些切片按各自实现记录和候选版本使用。**外置网关、通用 Workflow、Designer/部署、角色驱动动态图及并行执行闭环尚未完成产品验收。**

实现文档记录的是各自提交和环境下的事实，不能把早期“待接通”或某个场景的通过当作今天的全局状态。当前任务状态从 [GitHub Issues](https://github.com/zhouy1017/Karajan/issues) 和对应候选证据核对；本地会话检查点见 [PROGRESS.md](PROGRESS.md)。模型来源、隔离、真实并行、PR 创建、远端 CI 与合并分别验收。

历史 M0–M4/DG 计划、已发布 Issue 正文及实验记录通过[规划导航](docs/planning/README.md)与[历史调研导航](outputs/README.md)保留。它们承担历史范围与证据追溯，活动架构由上面的 r8 文档定义。
