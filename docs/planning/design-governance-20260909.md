# Commander Workbench 设计治理与承接矩阵

修订：2026-09-16 r9。本文继续作为活动治理索引，承接原工作台、r6 网关/自定义 Workflow、r7 对话创作/图表确认/真实部署、r8 获授权角色调度/自适应拆分，以及 r9 同一会话多维用量/实际 provider 归属。真实 GitHub 状态、编号、正文和关系以远端为准；不改写既有发布快照。

## 活动设计与唯一入口

当前主路径是：项目内对话提出 Workflow 要求 → Designer 编写配置 → 同源图表修改和确认 → 可信部署加载核对后生效 → 以具体输入、初始计划及调度范围批准 Run → 获授权角色按任务拆分/绑定/扩图/派发 → 按配置交付报告、补丁或 PR。已部署模板可直接复用，不强制再次调用 Commander。默认代码模板保留两个 Worker 并行、组合候选、独立 checks/review 与 PR 的历史最小验收；两任务不是产品默认数量或上限。

活动行为规格是 [Commander Workbench PRD](../prd/commander-workbench.md)（2026-09-16 r9，继承原 Hub、网关、设计部署及角色调度能力，新增用量统计待实现）。本治理文档负责文档层级和范围承接；领取顺序维护于 [business-first](business-first.md)，完整分类见 [文档导航](../README.md)。PRD 负责行为与界面，原验收和 Issue 保留，本次行为变更由 PRD 1.7、ADR 0005–0009 和下方增量承接。

## r9 会话多维用量与实际 provider 的承接

2026-09-16 用户新增要求：在项目同一个 Commander 对话中，统计并查看每个角色 Agent、每个任务、每个模型的 token 消耗，以及实际底层路由 provider 与对应用量。会话汇总包括全部 Run 和无 Run 的对话/创作调用；角色与 Agent 实例分开，请求模型与实际路由分开，未知及统计覆盖缺口可见。

| 新增范围 | 规格与验收 | 承接与完成条件 |
|---|---|---|
| 会话摘要、多维分组与明细 | FR28、UX-AC25/27、[UA-AC01–05/07](../architecture/12-conversation-usage-accounting.md) | 复用会话身份、调用/资源账本，补齐角色/Agent/任务归属、token 口径、覆盖去重、全部 Run/pre-Run 聚合及 Hub 查看；C/U/P 验证恢复和无模型刷新 |
| 实际 provider 与用量关联 | FR29、UX-AC26/27、UA-AC02–06、[ADR 0009](../adr/0009-conversation-usage-accounting.md) | 复用网关/兼容适配器，采集可关联实际发送及 usage，保留未知/估算、内部重试和迟到核对；真实来源/计量精度按固定部署另取 S 证据 |

2026-09-16 用户进一步要求审核全量设计、集中修订并标记已拆分 Issue；开发责任、接口/验收覆盖与远端发布读回统一见 [就绪清单](r9-development-readiness-20260916/README.md)。按调用归属与采集→聚合核对→工作台承接，当前准备规格和任务，不启动产品实现。已发布 #159–#162、#174–#178 等快照及原验收不变，已有预算账本、网关目录或 r8 控制面通过不代表 r9 已完成。以下 r6–r8 段落中的“本轮”及发布安排属于各次修订背景，远端现状须另行读回。

## r8 获授权角色调度与自适应规模的承接

由 Workflow 中明确获授 SchedulerGrant 的 Commander 或自定义角色按实际任务决定拆分、角色/模型绑定、依赖、扩图及派发规模。可信服务校验 SchedulingDecision 后提交 TaskGraphRevision，范围内自动生效，不逐个子任务再次审批；更换定义或扩大原授权才请求用户确认。原定义、授权与已启动 Attempt 冻结，运行图修订和决策可追溯，动态 ExpansionSet 封口后才判断汇合完整，不能靠删图抹掉既有必需义务。

Karajan 不内置前后端分工，也不内置 Agent、角色、任务数或并发人数上限。机器/上游实际容量、共享资源及用户明确预算/策略约束实际运行时机；容量不足就完整排队，恢复后继续，不截断业务图或降低目标。状态更新、队列唤醒与事实核对不反复调用模型，业务拆分/调整才需要模型判断。

| 新增范围 | 规格与验收 | 承接与完成条件 |
|---|---|---|
| 获授权角色实际调度 | FR26、UX-AC23、[RS-AC](../architecture/11-role-directed-scheduling.md)、[ADR 0008](../adr/0008-role-directed-scheduling.md) | 新增调度授权、命令身份/版本、范围内自动扩图/绑定/派发、超范围确认及幂等恢复；不能只做自然语言建议或逐子任务人工代操作 |
| 自适应业务拆分与规模 | FR27、UX-AC24、RS-AC | 用适合 1、3、7 项工作的真实输入验证内容驱动拆分，无内置人数/任务上限；缩减容量后完整排队并恢复，预算/硬约束、扩展封口与原质量义务仍成立，样本数不成为上限 |

r8 已有 #174–#178 的四个控制面切片验收合入；实际角色模型、物理并行及完整 Run 尚须后续任务。#159–#162、PR172/173 仍按原范围核验；两任务原票通过不自动证明全部 r8 能力，新设计不关闭旧验收。

## r7 对话设计与真实部署的承接

用户通过 Workflow Designer 对话提出要求，由 Agent 撰写配置文件，流程图/表格从同一候选编译生成；文字或字段修改产生新文件 revision，用户确认当前版本后，可信部署器物化 pending 包，调度器加载读回核对，再条件切换 active 并返回部署回执。表格/文件修改直接进入保存与编译，无须额外调用模型。API/表单是辅助工具；完整拖拽画布可后置，同源图表和真实部署不可后置为仅静态设计。

| 新增范围 | 规格与验收 | 承接与完成条件 |
|---|---|---|
| 文字创作配置及图表迭代 | FR24、UX-AC19/20、[WD-AC](../architecture/10-conversational-workflow-deployment.md) | 复用项目会话/草稿身份；新增 Designer 调用、真实文件、校验、图表与 diff，不能只验收手写配置/静态卡片 |
| 实际配置部署及复合运行 | FR25、UX-AC21/22、[ADR 0007](../adr/0007-conversational-workflow-deployment.md) | 新增目标绑定、精确确认、部署 intent/回执、配置物化与调度器加载；一次授权可组合部署并运行，各阶段结果分开记录 |

新增切片应先完成上述闭环，再与 P1–P4 复用模块整合；部署对象是 Karajan Workflow，不是 CLIProxyAPI 服务、应用网站或自动定时任务。配置导出、模板登记和模型宣称成功均不是部署验收。r6–r8 控制面已有 #174–#178 承接，剩余产品集成纳入当前就绪清单；既有候选与原 AC 不自动扩大。

## r6 网关与可配置流程的承接

| 设计变化 | 活动规格与验收 | 既有范围与实施影响 |
|---|---|---|
| 外置 CLIProxyAPI 统一模型接入 | FR21、UX-AC15；[网关契约 GW-AC01–05](../architecture/08-provider-gateway.md)、[ADR 0005](../adr/0005-external-model-gateway.md) | 新连接/模型绑定接入 P1 来源选择及 P2/P3/P4 调用层；旧官方/Go/DeepSeek 证据保留，不转为网关资格；新来源映射/请求变换需独立验收 |
| 自定义职责与 Workflow | FR22/23、UX-AC16–18；[Workflow 契约 WF-AC01–11](../architecture/09-configurable-workflows.md)、[ADR 0006](../adr/0006-configurable-roles-and-workflows.md) | P2 提案/批准解析角色与流程版本；P3 按当前有效运行图调度，r8 允许范围内自动扩图；P4 按目标产物判完成，Hub 展示自定义步骤，三角色仅为模板 |
| 多种完成目标 | report/patch/pr；PR 保留原 checks/独立 Review/授权与远端 gate | 新报告流程不要求 PR；不能据此关闭原 #1/#14/#17/#162 的 PR 验收或重解释旧 delivery=none |

P1–P4 是工程与验收承接顺序，非固定运行时 Workflow。先准备连接/绑定和角色/流程编译契约，再接已有会话、批准、执行和交付。现有 #172/#173 候选可复用，但其旧 AC 不自动覆盖 r6；新增范围需要独立的实现切片和证据。现已有 r8 控制面任务与证据；未覆盖推理/实际执行/UI 范围在就绪清单独立拆票。旧 native blocked-by、编号及原始验收保留，新增实现依赖不得冒充父范围最终验收已通过。

2026-09-09 r2 确立的 Commander Hub 继续作为日常入口：主会话集中呈现初始拆分、用户授权、当前运行图和最终汇报。r8 的获授权调度角色在原范围内直接提交实际扩图/派发，不要求用户再次逐子任务确认。Tasks / Agents 看板、计划、任务和交付页提供更多细节及操作，返回 Hub 保留同一 Run 状态。资格、规则、预算和容量编辑仍按需展开。可信程序负责调度命令校验与提交、admission、幂等、恢复和证据收据；卡片直接读取状态，不能靠 Commander 轮询浪费主模型调用。角色汇总检查及独立审查结果，但不能覆盖其证据和交付 gate。

用户显式指定的单一模型或来源必须原样进入批准版本，若资格、能力或额度不满足就显示具体状态；严格指定单一 Profile 时程序不得静默替换。只有用户明确启用并批准的替代集合，才允许按范围自动创建新 Attempt，且不重复批准；合法有限的保守 unknown 模式可按当前策略准入。能力额度、隔离、认证和计量仍是硬约束。既有消费授权不扩大；现金 API、订阅外余额和现金后备在本轮继续暂停。

开发本仓库的 Agent 分工保持：Luna 处理简单 UI、DTO 和边界明确的适配；Terra 处理生命周期、权限、预算、隔离和跨模块接线；Astra 负责协调、关键判断以及独立 Standards / Spec 审查，不编写产品代码、测试或 CI。这是开发协作约定，不是 Karajan 用户可配置角色或模型的产品限制。

## 文档分类与承接

| 类别 | 权威内容 | 读取时机 | 维护规则 |
|---|---|---|---|
| active spec / navigation | 根 README 与 `docs/README.md` 导航、`CONTEXT.md` 术语、活动 PRD/架构契约、有效 ADR 及本治理索引 | 设计、实现、验收行为时 | 新行为更新 PRD 与详细契约正文；总览负责解释；06/source 的历史记录及 examples 旧样例按自身日期/范围使用 |
| current schedule | [`business-first.md`](business-first.md) | 选择下一项工作或恢复队列时 | 保留 r8 直接依赖与 P1–P4 集成主线；状态不从本地文本推断远端 |
| implementation evidence / operations | [实现导航](../implementation/README.md)、`docs/implementation/*.md`、[`requirement-coverage.md`](../implementation/requirement-coverage.md)、[`issue-management-audit.md`](../implementation/issue-management-audit.md) | 评估已有实现、候选或失败，运行对应版本时 | 证据按原 AC 和 C/U/P/S/G 分层；历史 passed 不升级新候选；testing-gates 等维护规则按自身范围使用 |
| historical snapshots / research | `docs/planning/*publication*.json`、`v1/`、`m0/`、dated handoffs/reviews、`delivery-goals/`、已发布 issue Markdown、`outputs/` 原始调研/草案、`examples/` 固定证据 | 追溯原范围、候选、失败和关系时 | 保留原字节和摘要；新增修订文档链接承接，不在快照内回填当前安排 |

活动规范与证据发生冲突时，先判断是不是“行为变更”或“事实更新”：行为变更由 PRD revision 和治理映射记录；事实更新追加到实现证据并绑定候选 commit；两者都不能把旧快照的状态当成当前 GitHub 状态。

## M0–M4 / DG 到 P1–P4

P 阶段是既有工作台与默认 PR 模板的工程承接顺序，r8 直接依赖和领取顺序见 business-first。M0–M4 和 DG 是历史范围及原 AC 的责任索引。映射表示承接关系，不表示取代、完成或自动关闭；历史两个任务的样本不限制产品运行规模。

| 当前阶段 | 用户可见目标 | 主要历史承接 | 独立验收门 |
|---|---|---|---|
| P1 开仓库会话 | 选仓库后进入 Commander Hub，会话、任务缩略卡片及展开的 Tasks / Agents 详情可恢复 | M1-01/#11、M1-02/#12、M1-06/#16、#153、#93 | C/U：可信仓库、planning intent、计划状态和批准 revision 可复核；无批准不写代码 |
| P2 显式分工 | 持续对话后编辑角色、模型来源、依赖和任务边界；批准初始版本，r8 另明确调度授权及范围内自动修订 | M1-02/#12、M1-05/#15、#93；复用 #112、#142、#146、#147、#153 | C/U/P/S：原票仍核对真实输入、同版批准与严格来源；r8 调度另按 RS-AC，过期/篡改/无资格命令拒绝，有限 unknown 仅按策略准入，原预算/权限证据保持绑定 |
| P3 最小并行整合 | 原两任务样本真实并行编码、固定顺序组合候选并重新验证；当前规模由获授权角色决定，资源不足完整排队 | M1-03/#13、M2-01/#17；DG 业务接线与 #153 后续子任务 | C/P/S：独立工作区、依赖闭合、两个真实执行记录、冲突和组合候选可核对；原样本不是人数上限，r8 自适应规模另验，不因此关闭 #17 或满足其完整交付 gate |
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

r3 新增 UX-AC12/13：#159 承接一致的反馈图标、时间及新对话/新任务入口和会话保留；#160 承接新任务交给 Commander 的待确认计划；#161/#162 将真实 Worker/Reviewer 反馈接到相同状态源。图标动画只表达当前证据，不可代替运行心跳或真实模型资格，旧批准版本不因快捷新建而扩大。

r4 新增 UX-AC14：#159 承接项目分组侧栏、会话切换、空项目入口和快照恢复；#160 的新建/批准操作绑定目标项目，#161/#162 的事件与结果仍绑定原 Project/Run/Attempt。项目切换是导航行为，不构成执行授权或生命周期变化，原发布快照保持不变。

## 未覆盖范围

本轮设计治理没有完成真实来源资格、通用多来源接入、完整容量/预算产品、自动换源、恢复/升级/保留、任意第三方通道、现金/API 后备或完整 v1 出口。Go 的固定实测、既有离线协议、候选 Checks/Reviewer 原语和历史失败仍按实现证据记录，不能升级为 P1–P4 的真实全链通过。

P1–P4 也没有授予新的账户消费、合并、发布或外部消息权限；真实计划、真实编码、PR、CI、Review 和用户合入必须分别按 Issue 跟踪流程核对。新子票的远端编号、原生关系和当前状态以[本轮发布清单](design-publication-20260909.json)与 GitHub 读回为准。

运行操作文档（如 runtimes/ 下 README、docs/implementation/ 下启动说明）按其固定实现版本使用；它们不是新的产品排期。历史输出中的 Bernstein-first 指底座选型过程，不覆盖本次 UI 参考与 Commander 委派决定。
