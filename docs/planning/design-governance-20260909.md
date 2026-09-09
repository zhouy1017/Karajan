# Commander Workbench 设计治理与承接矩阵

修订：2026-09-09。本文是本轮设计变更的活动治理索引；真实 GitHub 状态、编号、正文和关系以远端为准。发布清单由本轮协调者另行维护，本文件不虚构新 Issue 编号，也不改写既有发布快照。

## 活动设计与唯一入口

唯一产品首演是：打开受管仓库，选择 Commander 模型和来源，进入高级 Commander 会话，持续对话后查看并编辑任务角色、指定模型来源和依赖；用户可以一键接受建议或修改，随后批准同一版本；两个无依赖任务由便宜模型真实并行编码；组合候选再独立运行 checks、review、diff 和 PR 交付。

新的产品行为规格是 [Commander Workbench PRD](../prd/commander-workbench.md)（2026-09-09 revision，由设计作者维护）。本治理文档负责文档层级、旧范围承接和执行队列；PRD 负责用户行为、状态和界面细节。旧 [v1 PRD](../prd/karajan-v1.md)、架构验收和原 Issue 正文仍是必需范围基线，未被本轮静默删除。

2026-09-09 r2 的核心日常入口是 Commander Hub：主会话集中呈现 Commander 的初步拆分与分配、用户调整确认、运行任务缩略卡片和最终汇报。Tasks / Agents 看板、计划、任务和交付页提供更多细节及操作，返回 Hub 保留同一 Run 状态。资格、规则、预算和容量编辑仍按需展开。可信程序调度承担 admission、授权、幂等、恢复和证据收据；卡片直接读取状态，不能靠 Commander 轮询浪费主模型调用。Commander 汇总检查及独立审查结果，但不能覆盖其证据和交付 gate。

用户显式指定的单一模型或来源必须原样进入批准版本，若资格、能力或额度不满足就显示具体状态；严格指定单一 Profile 时程序不得静默替换。只有用户明确启用并批准的替代集合，才允许按范围自动创建新 Attempt，且不重复批准；合法有限的保守 unknown 模式可按当前策略准入。能力额度、隔离、认证和计量仍是硬约束。既有消费授权不扩大；现金 API、订阅外余额和现金后备在本轮继续暂停。

模型分工保持：Luna 处理简单 UI、DTO 和边界明确的适配；Terra 处理生命周期、权限、预算、隔离和跨模块接线；Astra 负责协调、关键判断以及独立 Standards / Spec 审查，不编写产品代码、测试或 CI。

## 文档分类与承接

| 类别 | 权威内容 | 读取时机 | 维护规则 |
|---|---|---|---|
| active spec | `README.md`、`CONTEXT.md`、`docs/prd/`、`docs/architecture/`、有效 ADR 和本治理索引 | 设计、实现、验收行为时 | 新行为写入 PRD；治理只保留入口、边界和映射 |
| current schedule | [`business-first.md`](business-first.md) | 选择下一项工作或恢复队列时 | 只保留 P1–P4 当前主线；状态不从本地文本推断远端 |
| implementation evidence | `docs/implementation/*.md`、[`requirement-coverage.md`](../implementation/requirement-coverage.md)、[`issue-management-audit.md`](../implementation/issue-management-audit.md) | 评估已有实现、候选或失败时 | 证据按原 AC 和 C/U/P/S/G 分层；历史 passed 不升级新候选 |
| historical snapshots / research | `docs/planning/*publication*.json`、`v1/`、`m0/`、dated handoffs/reviews、`delivery-goals/`、已发布 issue Markdown、`outputs/` 原始调研/草案、`examples/` 固定证据 | 追溯原范围、候选、失败和关系时 | 保留原字节和摘要；新增修订文档链接承接，不在快照内回填当前安排 |

活动规范与证据发生冲突时，先判断是不是“行为变更”或“事实更新”：行为变更由 PRD revision 和治理映射记录；事实更新追加到实现证据并绑定候选 commit；两者都不能把旧快照的状态当成当前 GitHub 状态。

## M0–M4 / DG 到 P1–P4

P 阶段是当前执行顺序，M0–M4 和 DG 是历史范围及原 AC 的责任索引。映射表示承接关系，不表示取代、完成或自动关闭。

| 当前阶段 | 用户可见目标 | 主要历史承接 | 独立验收门 |
|---|---|---|---|
| P1 开仓库会话 | 选仓库后进入 Commander Hub，会话、任务缩略卡片及展开的 Tasks / Agents 详情可恢复 | M1-01/#11、M1-02/#12、M1-06/#16、#153、#93 | C/U：可信仓库、planning intent、计划状态和批准 revision 可复核；无批准不写代码 |
| P2 显式分工 | 持续对话后编辑角色、模型来源、依赖和任务边界；用户一键接受建议或修改后批准同一版本 | M1-02/#12、M1-05/#15、#93；复用 #112、#142、#146、#147、#153 | C/U/P/S：服务端编译真实输入，严格单一来源不被替换，已批准替代集合可按范围自动新 Attempt；过期/篡改/无资格版本拒绝，有限 unknown 仅按策略准入；原批准和预算/权限证据保持绑定 |
| P3 最小并行整合 | 两个无依赖任务真实并行编码，默认最多两个 writer，固定顺序组合候选并重新验证 | M1-03/#13、M2-01/#17；DG 业务接线与 #153 后续子任务 | C/P/S：独立工作区、依赖闭合、两个真实执行记录、冲突和组合候选可核对；P3 实现切片可独立验收，不因此关闭 #17 或满足其完整交付 gate |
| P4 checks 独立 review 交付 | 组合候选运行必需 checks；独立 Reviewer 读取同一候选和证据；展示 diff、当前 head、PR 与限制 | M1-03/#13、M1-04/#14、M1-06/#16、DG review/delivery、#93/#95 | C/U/P/S/G：当前候选的 checks、不同上下文 review、diff/head/PR 身份和失败/不确定 gate 逐项绑定；真实交付与原 #14 条件仍需完整满足 |

旧阶段依赖继续作为父票验收 gate 保存。例如 #17 的 #14/#15 依赖是其完整组合交付条件；P3 新切片只承接并行组合候选和 checks 的可独立实现，不能通过缩短实现依赖来宣称 #17 完成。任何新 native blocked-by 必须表达真实执行依赖；“稍后阶段”本身不是依赖。

## 新工作台实现切片

完整范围和验收只维护在以下发布正文；本表负责承接，避免重复正文产生不同口径。真实编号和关系见[本轮发布清单](design-publication-20260909.json)。Parent/Related 是范围归属，不自动成为实现的 blocked-by；现有父票的完整验收 gate 继续保留。

| 切片 | 原生父范围 | 实现顺序 | 验收正文 |
|---|---|---|---|
| [P1 #159](https://github.com/zhouy1017/Karajan/issues/159) 工作台入口与详情 | #16 | 基于已有认证及 Project/Run 读模型，不等 #153 整链或 #16 父票关闭 | [P1](commander-workbench-20260909/DG-UX-01-workbench-shell.md) |
| [P2 #160](https://github.com/zhouy1017/Karajan/issues/160) 会话分工与批准 | #12 | P1；具体 Planning/transport 接线按真实能力验收 | [P2](commander-workbench-20260909/DG-UX-02-dispatch-edit-approval.md) |
| [P3 #161](https://github.com/zhouy1017/Karajan/issues/161) 两任务真实并行 | #17 | P2 及所需执行/候选原语；不等 #14 整票关闭 | [P3](commander-workbench-20260909/DG-FLOW-03-parallel-candidate.md) |
| [P4 #162](https://github.com/zhouy1017/Karajan/issues/162) 独立审查与 PR | #14 | P3 及所需 checks/Reviewer/Delivery 接线 | [P4](commander-workbench-20260909/DG-DELIVERY-04-checks-review-pr.md) |

r2 新增 UX-AC11（Hub 闭环）：#159 承接主会话、缩略卡片及详情往返；#160 承接 Hub 内初步分配、调整及同版确认；#161 承接并行状态/阻塞回到 Hub；#162 承接 Commander 最终汇报和同候选证据入口。原验收正文与发布快照保留，远端 Issue 追加 r2 行为约束；原型不构成任一票的实现完成。

## 未覆盖范围

本轮设计治理没有完成真实来源资格、通用多来源接入、完整容量/预算产品、自动换源、恢复/升级/保留、任意第三方通道、现金/API 后备或完整 v1 出口。Go 的固定实测、既有离线协议、候选 Checks/Reviewer 原语和历史失败仍按实现证据记录，不能升级为 P1–P4 的真实全链通过。

P1–P4 也没有授予新的账户消费、合并、发布或外部消息权限；真实计划、真实编码、PR、CI、Review 和用户合入必须分别按 Issue 跟踪流程核对。新子票的远端编号、原生关系和当前状态以[本轮发布清单](design-publication-20260909.json)与 GitHub 读回为准。

运行操作文档（如 runtimes/ 下 README、docs/implementation/ 下启动说明）按其固定实现版本使用；它们不是新的产品排期。历史输出中的 Bernstein-first 指底座选型过程，不覆盖本次 UI 参考与 Commander 委派决定。
