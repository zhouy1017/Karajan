# Karajan 文档导航

维护日期：2026-09-16。活动产品基准：**Commander Workbench r9 / 产品 PRD 1.7**。继承 r8 授权角色调度，新增同一 Commander 会话内按角色/Agent、任务、模型及实际 provider 查看 token 用量；本地规格更新不表示实现或远端 Issue 已完成。

## 先读哪一份

1. [架构总览与流程图](architecture/README.md)：先理解用户入口、模块分工、外置网关和从设计到结果的完整过程。
2. [Commander Workbench](prd/commander-workbench.md)：用户如何对话创作配置、审阅修改、确认部署及观察执行。
3. [产品 PRD](prd/karajan-v1.md)：完整功能范围、FR01–29 与原 PRD 验收；UX 的完整正文只维护在工作台 PRD。
4. [领域术语](../CONTEXT.md)：Project、Conversation、WorkflowBundle、Deployment、Run、Task、Attempt 等对象的区别。
5. [ADR](adr/README.md)：理解决定及其后续承接；接受设计不等于已经实现。

## 当前详细设计

| 主题 | 唯一详细入口 |
|---|---|
| 完整开发接口、复用基线与分阶段出口 | [13 开发接线](architecture/13-development-integration-contract.md)、[Agent 就绪清单](planning/r9-development-readiness-20260916/README.md) |
| 同一 Commander 会话的角色/Agent、任务、模型与实际 provider 用量统计 | [12 会话用量与实际路由](architecture/12-conversation-usage-accounting.md) |
| 获授权角色决定拆分/调度、动态任务图、并行规模与资源背压 | [11 角色驱动的调度](architecture/11-role-directed-scheduling.md) |
| 对话设计、配置文件、图表确认、部署与恢复 | [10 对话式 Workflow 设计与部署](architecture/10-conversational-workflow-deployment.md) |
| 自定义角色、步骤语义、条件、并行、有限返工及产物目标 | [09 可定制角色与 Workflow](architecture/09-configurable-workflows.md) |
| 外置 CLIProxyAPI、模型绑定、调用账本与兼容路径 | [08 Provider 网关](architecture/08-provider-gateway.md) |
| 业务对象、唯一状态权威与生命周期 | [01 控制与状态](architecture/01-control-and-state.md) |
| Profile、Rulebook、共享池、预算与换源 | [02 路由与配额](architecture/02-routing-and-quota.md) |
| 受控执行、工作区、产物验证和独立 PR 交付 | [03 执行与交付](architecture/03-execution-and-delivery.md) |
| 用户命令、事件与工作台接口 | [04 API 与工作台](architecture/04-api-and-workbench.md) |
| Project/Conversation/Run、Hub 读模型、消息及恢复 | [07 工作台后端契约](architecture/07-commander-workbench-backend-contract.md) |
| 技术组合、实施切片、验收矩阵与运行环境 | [05 实施与验收](architecture/05-build-and-validation.md) |
| 用户决策沿革和带日期的外部依据 | [06 决策记录](architecture/06-review-and-decisions.md)、[来源记录](architecture/sources.md) |

以上分工用于避免重复规格：总览负责解释与导航，PRD 定义可观察行为，架构契约定义具体约束，ADR 解释为什么。修改行为时同步对应 FR/UX-AC 与详细契约；不能只在旧正文顶部追加一个新 revision 而留下相反要求。

## 规划、实现与历史分开阅读

| 类别与覆盖目录 | 使用方式 |
|---|---|
| [当前业务顺序](planning/business-first.md)、[设计治理](planning/design-governance-20260909.md) | 当前开发承接及范围映射；P1–P4 是工程顺序，运行引擎执行用户配置的流程 |
| [规划导航](planning/README.md) | 历史 M0–M4/DG、交接、复审和发布清单；原 Issue 要求继续验收，但历史排期不自动成为当前任务 |
| [实现导航](implementation/README.md)与 `implementation/*.md` | 固定版本的实现说明、诊断和证据；其中 testing-gates 等维护规则依其自身声明使用 |
| `../examples/**`、`../runtimes/**` | 原场景样例、实验事实与 runtime 操作说明；保留其输入、环境和适用范围，不改写成当前设计的资格 |
| [架构配置样例说明](architecture/examples/README.md) | 早期资源/Rulebook 示例及其 schema 范围，不是当前 WorkflowBundle 或生产配置 |
| [历史调研与草案](../outputs/README.md) | 选型、访谈和早期设计依据；当前实现应回到活动架构 |
| `planning/**/issue*.md`、发布 JSON 清单 | 原发布正文与身份/摘要记录；不直接修改已发布快照，新增行为通过新修订承接 |

## 本轮清理结果与后续维护

r9 新增 FR28/29、UX-AC25–27、UA-AC01–07 与 ADR 0009，同步会话用量入口、实际路由证据、token 计量/去重和恢复要求。全量审核补充架构 13，并已创建增量父票 #183 与 #184–#208；当前正文、原生关系及标签的读回状态见 [就绪清单](planning/r9-development-readiness-20260916/README.md)。尚待用户回答的规格不会标为最终就绪。以下保留 r8 文档整理范围。

r8 在已整理文档上同步更新活动正文：初始授权和部署定义固定，角色可在授权内动态增图、分工和派发；运行图修订与初始批准分开记录。旧“两 writer”和固定前后端例子不再定义默认调度，原验收规模与历史记录继续保留。当前行为由 11 与 FR26/27、UX-AC23/24 承接。

- 重写根 README 和架构总览，移除逐条堆积的旧实现进度及重复的 r4/r6/r7 产品介绍；历史实现细节继续在原证据文件查阅。
- 全文对齐两份 PRD 及架构 01–10：内置三角色与并行代码到 PR 是模板；对话 Designer 是配置创作入口；部署先加载核对再切换生效；报告/补丁按各自目标完成。
- 为 ADR、规划、实现、调研和旧配置样例建立分类导航。旧计划/报告只保留有追溯价值的原范围、来源与失败事实，移除其作为当前默认入口的地位。
- 保留原 FR/AC、ADR 编号、Issue 发布快照、证据路径与失败结果；文档清理不重写历史验收，也不删除尚未完成的产品要求。

过时文档的处理按内容决定：仍在使用的规范重写；有历史价值的材料明确标识日期、范围和活动继任入口；没有独有内容的重复说明合并删除。归档不表示需求取消，设计接受不表示功能完成。仓库协作规则统一维护在 [AGENTS.md](../AGENTS.md)，不分散到 Claude 专用文件。
