---
status: accepted
---

# Karajan 拥有唯一业务协调器

用户要求跨订阅/API 的强制 Rulebook、配额预留与明确交付权限。2026-09-05 审阅 Q1 已确认：Karajan 唯一拥有 Run/Task/Attempt 业务状态，执行器只执行已绑定 Attempt 并报告物理事实。Bernstein 当前可核查的扩展点未证明覆盖所有派发、续接和 fallback。

相比“Bernstein 全任务图＋外置策略”，这需要实现有限的业务依赖推进，但不会让两套系统同时决定重试、模型与终态。先接具体 CLI/API adapter，Bernstein 满足同一受控执行接口后复用。此决定取代前期草案的状态所有权建议。

依据见 [来源](../architecture/sources.md#bernstein)，契约见 [状态设计](../architecture/01-control-and-state.md)。本 ADR 记录 2026-09-05 的设计决定；后续协调实现与底座验收分别按 [实现记录](../implementation/README.md) 核对，不沿用原审阅时的未实现状态作为实时结论。

2026-09-09 的 [Commander 工作台设计](../prd/commander-workbench.md) 是本决定在用户界面上的指针：一个持久主 Commander 负责判断拆分、委派、升级建议和验收，可信协调器负责机械调度与状态提交。该设计补充入口和可观察交互，不改变本 ADR 的唯一业务协调器所有权，也不把 Bernstein 或其他模型变成第二个主控。

2026-09-14 [ADR 0005](0005-external-model-gateway.md) 将 provider 转换交给外置网关，[ADR 0006](0006-configurable-roles-and-workflows.md) 将职责与运行流程改为可配置版本。两者均不拥有第二套 Run 状态；本 ADR 的单协调器不意味着固定三角色、固定步骤图或每个 Workflow 必须调用规划模型。

同日 r8 [ADR 0008](0008-role-directed-scheduling.md) 明确：业务拆分、分工、依赖和派发由 Workflow 中获授权角色作决定，可信协调器验证并执行这些有效命令。它唯一拥有状态提交，不垄断业务调度判断，也不预设 coding Agent 数量。角色在委派工作范围内持有有效提交权，不因名称或共享数据库取得额外权限。
