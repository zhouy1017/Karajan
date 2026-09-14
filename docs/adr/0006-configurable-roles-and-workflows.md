---
status: accepted
---

# 角色职责与 Workflow 采用版本化定义

2026-09-14 用户明确要求可自定义角色分工、职责与工作流。Karajan 使用可保存、可编辑的 RoleDefinition 与 WorkflowDefinition；现有 Commander、Worker、Reviewer 及两 Worker 到 PR 的协作方式作为内置模板，不是引擎唯一拓扑。

职责说明、输入输出和完成标准属于角色定义；工具权限、写区、预算和来源属于批准后计算的授权；步骤依赖、条件、并行汇合和有限返工属于 Workflow。改名或修改提示词不能扩大授权。引擎执行已批准图和注册的步骤类型，不能按角色字符串推导写权限或强制所有 Run 走同一条链。

Run 固定部署 Workflow、初始授权及已启动 Attempt 的角色/来源/权限；r8 [ADR 0008](0008-role-directed-scheduling.md) 允许获授权角色在范围内有效拆分、增图与派发，以运行图修订记录，无须逐子任务人批；超范围才形成新的批准。只读报告、补丁与 PR 可以有不同完成目标；PR 仍须通过当前候选的检查与独立审查，不能通过删节点或改名绕过。Commander Hub 保留统一用户入口，可信协调器保留状态权威。

首版以对话 Agent 生成声明式配置，流程图/表格协助确认和修改，由可信部署器实际加载生效；该 r7 交互与部署决定见 [ADR 0007](0007-conversational-workflow-deployment.md)。通用拖拽画布与任意脚本引擎不作前置。P1–P4 仍是开发/验收承接顺序，不是运行时固定流程。角色/图语义与兼容迁移见 [Workflow 契约](../architecture/09-configurable-workflows.md)。本 ADR 接受设计，未宣称实现或原 Issue 验收完成。
