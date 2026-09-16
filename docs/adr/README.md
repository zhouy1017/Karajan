# 架构决定索引

当前基准：2026-09-16 r9。`accepted` 表示设计决定已接受，实际实现与真实资格在对应证据中验收。后续 ADR 可以收窄旧示例或替代旧接入建议，不能据此改写历史事实。

| ADR | 当前适用决定 | 后续承接 |
|---|---|---|
| [0001 唯一业务协调器](0001-single-coordinator.md) | Karajan 唯一持久提交 Run/Task/Attempt 业务状态 | 0008 明确业务调度由获授权角色决定，程序校验并机械执行；状态权威唯一不等于角色没有调度权 |
| [0002 固定 Profile 与原生资源账本](0002-profile-and-native-resource-ledger.md) | 每个 Attempt 固定来源/执行配置，保留原生资源单位与共享关系 | 0005 增加网关连接/模型绑定，0006 角色名字不带来资格或资源特权 |
| [0003 独立交付](0003-independent-delivery.md) | PR 必须绑定当前候选、有效 checks、独立审查和受管远端权限 | 0006 将其适用范围明确为 PR；报告和补丁有各自完成条件 |
| [0004 Commander Hub](0004-commander-workbench.md) | 项目内持久会话、任务缩略卡片和详情共享同一事实源 | 0006 将三角色首演作为模板，0007 加入对话配置与部署主路径 |
| [0005 外置 CLIProxyAPI](0005-external-model-gateway.md) | provider 认证/协议转换外置，调用授权、工具、账本与状态保留在 Karajan | 旧 CLI/API 适配器只作显式兼容；既有资格不自动迁移 |
| [0006 可配置角色与 Workflow](0006-configurable-roles-and-workflows.md) | 职责和拓扑采用版本定义，Run/Attempt 冻结批准材料 | 0007 定义其对话创作、图表确认与真实部署体验 |
| [0007 对话创作与部署](0007-conversational-workflow-deployment.md) | Agent 写配置，同源图表供审阅；可信程序准备、加载读回、条件激活并记录回执 | 当前活动交互与部署决定 |
| [0008 角色调度与自适应并行](0008-role-directed-scheduling.md) | 用户授权的 Workflow 角色决定任务拆分和调度；无内置 coding Agent 数量上限，资源不足排队 | 初始授权内动态图修订自动生效；旧两 writer 初值废止，原验收样例保留 |
| [0009 会话用量与实际 provider](0009-conversation-usage-accounting.md) | 同一 Commander 会话按角色/Agent、任务、模型与实际底层 provider 查看 token，可信归属与未知分开 | FR28/29、UX-AC25–27、UA-AC01–07 独立承接；复用账本，不将预算占用当实耗 |

具体行为见 [工作台 PRD](../prd/commander-workbench.md)，模块与流程见 [架构总览](../architecture/README.md)。ADR 记录决定理由，不维护随时变化的实现进度。
