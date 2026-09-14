# 可定制角色与 Workflow 契约

修订：2026-09-14 r8。状态：**待实现、待验收的角色/流程契约。** 用户通过对话设计并真实部署 Workflow，其中获授权调度角色按实际任务决定拆分、依赖、角色绑定、优先级与派发。Karajan 机械校验、排队、持久化和执行，不内置前后端分工或 coding Agent 人数上限。三角色流程/P1–P4 保留模板、原验收与工程顺序语义；对话部署见 [10](10-conversational-workflow-deployment.md)，动态运行图见 [11](11-role-directed-scheduling.md)。本文件不声称代码已迁移。

本契约补充 [Commander Workbench PRD](../prd/commander-workbench.md)、[控制与状态](01-control-and-state.md)、[路由与配额](02-routing-and-quota.md) 和 [工作台后端契约](07-commander-workbench-backend-contract.md)。原代码交付模板、PR 验收和历史证据保留；新研究、文档或 patch 流程按自己的批准目标验收，不能借此宣布旧 PR 范围完成。

## 1. 用户可配置什么

用户可以新建角色，例如“资料研究员”“接口设计者”“文档编辑”“安全审查者”，设置职责、输入材料、输出契约、工具约束和停止条件；也可以修改主 Commander 的职责与分工方式。角色名称不限定可用模型品牌，模型和来源仍通过 Execution Profile 明确绑定。

用户可以配置固定步骤或授权角色在运行中展开子图，设置可委派范围、依赖/分支、汇合、人工决定、有限返工和产物，不必事先枚举全部子任务。拆分按实际代码或问题边界决定，不强制前端/后端二分。报告、文档和 patch 都是有效目标，不因缺少 PR 而卡在 finalizing。

主入口是 Workflow Designer 对话 Agent：根据文字撰写声明式配置文件，服务端从同一配置生成流程图、角色/步骤表与差异；用户用文字或图表定位后的字段编辑进行迭代，确认同版后真实部署。API 支撑这一闭环，表格和配置编辑辅助对话；完整拖拽画布可后续增加，流程图/表格预览属于当前必需范围。配置不接受自由 Python/JavaScript、shell 条件表达式或任意模块导入路径。需要新执行能力时由受信任代码注册并单独验收，不把代码藏进角色说明或 Workflow 字段。

## 2. 角色定义、身份与权限

`RoleDefinition` 是可版本化的工作职责约定；它不是权限主体，也不是一个正在运行的 Agent。

| 字段 | 语义 |
|---|---|
| `id`, `revision`, `digest`, `display_name` | 稳定角色身份、不可变发布版本、内容摘要与显示名称；改名不创建执行权限 |
| `responsibilities`, `stop_conditions` | 职责、判断范围、升级和交回条件；包括可定制的 Commander 工作方式 |
| `input_contract_ref`, `output_contract_ref` | 版本化、受支持的结构化输入/输出契约；包括必需材料、产物类型和完整性要求 |
| `instruction_template_ref` | 经版本化保存的角色说明模板；指令不能覆盖已批准范围或可信 gate |
| `required_capabilities` | 工作所需能力，例如只读研究、受限编辑、结构化输出；需要对应 Profile 的资格证据 |
| `tool_constraints` | 工具种类、路径、网络目的地及操作范围的声明式上限；不含真实凭据或任意可执行规则 |
| `independence_requirements` | 适用的非作者、新上下文、模型家族等要求；最终有效要求取项目与交付策略中更严格者 |
| 调度职责与授权要求 | 声明拆分、依赖、角色绑定、优先级、派发等能力；实际权力来自 SchedulerGrant，不来自提示或角色名 |

发布后的角色版本不可原地修改；编辑生成新 revision。模板发布不改变已批准 Run。删除使用中的角色只能退役，历史版本继续可读，以便解释既有 Attempt。

实际执行许可由可信程序计算：用户/项目授权、Run 批准范围、步骤要求、角色工具约束、执行器实测能力及当前资源政策共同生效，允许集合取交集。角色请求超出有效许可且使任务无法完成时，批准预览或准入返回具体缺口；不能默默授予权限，也不能把被删掉的必需能力当作任务已经可执行。角色定义限制工具使用，但只有 Authorization Envelope 能表达用户已授予的运行权限。

以下权限与资格独立于角色名称保存：

- Workflow/SchedulerGrant 定义有效提交身份，可明确授予重叠范围，竞争图变更经 CAS；同一 grant 或明确交接槽位仅有当前任期。Commander 或其他角色均可按授予委派，不设全 Run 单角色独占。用户选定主 Commander 的替换仍由用户决定，不把该要求扩大到其他角色的授权内委派。
- 用户批准初始目标、执行与调度授权；范围内 SchedulingDecision 经机械校验后生效，不逐子任务重复人批。超授权或显式人工节点才请求用户决定，模型不能取得用户身份。
- 可信协调器独占运行状态写入、资源校验、CAS、幂等、恢复和交付 gate；获权角色作业务调度决定，不构成竞争状态机。角色不能直接改库、签发 activation 或绕过硬门。
- Reviewer 的独立性由实际作者集合、Attempt、上下文及适用模型家族证据判定。把作者角色改名为“审查者”不能取得独立证据；审查任务需要修改候选时返回修复请求，由新 Attempt 产生新候选。
- 受保护额度访问和远端交付凭据由单独的可信授权控制；名为“Commander”或“Delivery”的角色不会自动取得它们。

调度身份不要求有名为 Commander 的步骤。用户可直接复用模板并批准初始计划/授权，静态流程不必额外调用规划模型。启用动态调度后，获权角色按任务和结果实际提交结构化决定，不仅提供建议。Designer 创作独立于目标 Run，只有另获 SchedulerGrant 后才能承担运行调度。

## 3. WorkflowDefinition 与编译

WorkflowDefinition 定义可复用职责、初始步骤/扩展点和调度规则；初始 PlanRevision 绑定需求、输入与初始授权。运行图通过不可变 TaskGraphRevision 演进，记录 SchedulingDecision、父版本和有效授权，不要求全部节点预先存在或每个图版本再人批。执行仍沿 Project/Conversation/Run/Task/Attempt 归属。

| 对象 | 最小内容 |
|---|---|
| `WorkflowDefinition` | id/revision/digest、输入契约、初始步骤/扩展点、可用角色/执行种类、调度/委派边界、依赖/条件、有限返工及产物政策；可引用用户明确的数量/预算政策，无内置人数默认值 |
| `WorkflowStep` | 稳定 `step_id`、`execution_kind_ref`、可空 `role_definition_ref`、输入绑定、输出契约、依赖、条件、必需性、适用检查/人工决定 |
| `ExecutionKind` | 受信任注册表中的 `id/revision`、输入输出 schema、所需能力、副作用类别、执行/取消/恢复适配器和验收证据 |
| `CompiledWorkflow` | 定义/角色/种类版本、参数化初始图、扩展/调度规则、输入输出/条件/约束与编译器版本；模板摘要不含某次 Run 的动态子任务 |
| `SchedulerGrant` / `SchedulingDecision` | 授予工作范围、委派边界与当前任期；角色提交的子图/依赖/绑定/优先级/派发决定、依据、base graph revision、幂等身份及回执 |
| `TaskGraphRevision` / `ExpansionSet` | 不可变运行图、父版本/授权/决定来源；动态集合的成员、封口事实和未完成义务 |
| `WorkflowStepInstance` | run/graph revision/step、Task/Attempt、条件决定、输入摘要、输出、返工及状态证据；已启动 Attempt 保持原快照 |

内置执行种类可包括 `agent_task`、`deterministic_check`、`human_decision`、`artifact_aggregate`、`candidate_integrate` 和 `publish_pr`。这些名称是待实现注册项，不表示已经有可用适配器；Agent 步骤必须引用角色，人工和可信确定性步骤不伪装成模型角色。用户可组合已登记种类，新增种类必须经过代码实现与能力验收。`deterministic_check` 只引用用户认可的检查配置，不能在 Workflow 中塞入任意新 shell 命令来绕过原授权。

初始编译校验已声明引用、输入输出、依赖无环、条件、扩展/委派范围、返工和授权影响，展示初始图与允许动态变化的边界。运行中每个 SchedulingDecision 再校验相同硬约束、提交身份/范围/任期及 base graph revision，接受后持久化新图再机械派发。未知角色/种类或不兼容契约拒绝，不猜测为 Worker，也不借动态调度引入未获准定义。

用户指定的 Profile/source、依赖和职责进入编译结果；规则只能在批准的选择范围内求解。角色本身不固定品牌，也不能覆盖严格单一 Profile 绑定、预算、风险下限或来源资格。外部 provider 网关只承担模型访问职责，不解释 Workflow、增添隐藏任务或获得 Run 批准权。

角色/Workflow API 为 Designer 的配置生成、保存、校验、图表投影和版本发布提供受控工具。建议资源 `/v1/role-definitions`、`/v1/workflow-definitions` 仍是拟新增契约；Designer 调用计入规划预算，纯解析/绘图/校验不调用模型、不预留任务资源。定义发布登记不可变模板，真实部署还须物化 pending 包、由调度器加载读回核对，再条件发布 active。用户的一次“确认并部署”可以在内部组合发布与部署，无需重复确认；若同时明确批准具体 Run 输入与权限，也可组合“部署并运行”。运行批准仍复用原 Plan/Run 约束，不由 Designer 自签授权。模板 compiled digest 与具体 Run 的 input/run_plan digest 分别保存；部署命令、配置包和回执见 [部署契约](10-conversational-workflow-deployment.md)。写操作继续校验认证、Idempotency-Key 和适用的 If-Match，过期编辑返回冲突。

## 4. 依赖、条件、并行与返工

每个被接受的运行图保持普通依赖无环。获权角色根据实际任务/反馈决定拆分、角色、依赖、优先级与派发；引擎检查输入、条件、授权和资源后执行，不按前后端模板改写其合法决定。静态步骤可按已批准的声明式派发规则推进。

Karajan 不固定全局或项目 coding Agent 数量，也不补新默认人数。用户可显式设置数量/预算政策；真实来源并发、宿主容量、费用和工作区冲突形成可解释背压，合法图保留排队，不因暂缺资源截断或删节点。每个可写工作区仍只有一个 writer；独立工作区按授权与实际资源并行。

条件采用有类型的有限表达式，例如 `equals`、`all`、`any`、`not` 和整数边界比较。条件只能引用已批准输入、可信步骤结果或当前有效人工决定；模型自由文本、墙上时间或前端图标不能直接决定 gate。表达式只计算数据，不执行代码或发起请求。相关事实为 unknown 时结果仍为 unknown，保持等待或核对，不能按 false 跳过义务。

分支在编译时声明出口，并在执行时保存使用的输入摘要和选择事实。不选中的分支记录 `not_applicable` 与原因；失败、取消、缺失输入或 unknown 不能改写为 `not_applicable`。必需产物没有合法生产路径、必需步骤无法到达，或者条件试图绕过目标交付的硬 gate 时拒绝发布或批准。

并行汇合等待全部必需输入，动态 ExpansionSet 还必须有可信封口事实；暂时没有新节点或第一个结果不表示全部完成。已承诺义务不能靠删图、取消或改为可选抹掉。图修订记录取代/失效和剩余义务，保留运行、消费与核对事实，详见 [11](11-role-directed-scheduling.md)。

返工通过显式 `rework` 规则表达：触发结果、返回的步骤集合、失效的输入/证据、最大轮数与预算边界。编译器把它展开为带批次身份的有限执行，不允许任意回边构造无限循环。每次返工生成新的 StepInstance/Task revision 或 Attempt，并重新绑定输入；代码、基准或相关输入变化后重做受影响 gate。

返工次数沿 `run_id + repair_chain_id + validation_cycle_id` 等稳定身份累计；改名、重启、重派、换模型、新 Task 或新计划不能自动清零。同一验证批次的多个失败只计一轮。基础设施重试独立计数，并与返工共同受 Run 总时间、调用和预算约束。到达边界后保存产物与问题，等待新的有效决定。

## 5. 人工节点、冻结边界与动态运行图

人工节点显式声明需要谁决定、决定对象及输入摘要、允许选项、超时行为和结果影响。可信服务验证认证用户与当前对象身份；已失效、旧 revision 或已经取消的决定请求拒绝。超时默认等待或按预先批准的无执行副作用分支处理，不能默认同意授权、提高预算或发布 PR。

Run 冻结 Workflow/部署/编译器、获准角色/种类集合、初始输入/Plan、初始授权/SchedulerGrant、来源允许集合、验证/交付政策与累计边界。已启动 Attempt 固定自己的图/任务/来源/权限快照。冻结这些边界不冻结运行图：范围内可形成 TaskGraphRevision，不改写原版本或扩权。Run 仍受当前账户/宿主准入硬约束，不在执行期解析 latest。

以下操作分别处理：

| 修改 | 对既有 Run 的影响 |
|---|---|
| 编辑并发布角色或 Workflow 模板 | 只供未来编译；既有批准版本保持不变 |
| 修改未批准的提案 | 生成新提案/计划版本，更新影响预览，旧批准请求失效 |
| 获权角色在范围内拆分/重排/绑定/派发 | SchedulingDecision 经 CAS 校验后生成图修订并排队执行，保存依据、义务和影响，无需逐子任务人批 |
| 采用范围外定义、来源、预算或权限 | 展示计划/授权影响，用户批准新增范围后生效；已启动 Attempt 不热修改 |
| 只改会话草稿、筛选或展示选择 | 不改变 Workflow、Task、Attempt、授权或调度 |
| 修改进行中 Attempt 的模型或工具范围 | 不热修改；保留原绑定，需要时停止或证实隔离后创建新 Attempt |

图修订可复用输入、职责、执行约束和证据均未变的成果；变化时显式判断失效与新义务。旧 Attempt 迟到产物不自动满足新版，消费和物理核对保留。初始授权覆盖的动态拆分/依赖/分工/优先级/派发、有限返工和替代集合均可自动执行；范围外才请求用户决定，角色不得自改授予边界或清零累计限制。

## 6. 产物、交付目标与完成语义

Workflow 的目标必须显式选择 `delivery_kind: report | patch | pr`。它表达用户期待的结果类型，不授予文件写入、外部发布或账户消费权限。更改目标类型需要重新编译影响；从 report/patch 升到 PR 不继承缺失的远端授权与证据。

`Artifact` 是带类型、来源、内容 digest、输入摘要和固定版本的产物。报告、文档和代码候选都可通过 Artifact 引用交接；代码 Candidate 继续保存仓库、base/tree SHA、父候选和冻结事实，不把任意文本输出冒充 Candidate。Evidence 指向其实际验证的 Artifact/Candidate 及配置版本；PR gate 只能消费匹配最终 Candidate 的代码证据。

| 目标 | 完成所需事实 | 用户看到的结果 |
|---|---|---|
| `report` | 声明的必需步骤结束，报告/文档产物冻结并通过输入输出契约、声明的质量检查和人工节点 | 可读取或下载的报告及来源、限制和证据；没有 PR 不算缺项 |
| `patch` | 形成固定仓库基准和内容的 Candidate/patch，通过完整性、范围及项目/批准版本要求的检查与审查 | 可查看 diff、证据并下载的 patch；不自动改原工作区或创建远端对象 |
| `pr` | 当前批准范围的必需检查和独立 Review 通过，独立交付 gate 接受同一冻结 Candidate，并已核对远端 PR 身份与 head | 同一 PR、diff、checks、Review 与远端状态；CI 按项目预设 gate 处理，合并由用户决定 |

report 可以只读仓库并把文档写到获准的产物区；如果需要修改仓库文档、导出到别处或发到外部服务，必须在批准范围内明确相应副作用。`patch` 不表示免验证；其批准计划和项目策略要求的检查、审查仍然必需。report/patch 不要求 PR，也不调用 PR 发布执行器；把 `publish_pr` 填进这两类流程属于目标与副作用不一致，编译拒绝，需显式修改目标和授权后重新批准。

`pr` 的质量与交付硬 gate 由可信控制器强制，Workflow 只能增加步骤和更严格要求，不能删除、改名、条件跳过或用模型自述满足它们。每个 PR 都需要真实机器检查和独立审查；T3 等适用的不同模型家族要求继续生效。审查者不得兼任该候选作者，来源未知不能假装满足独立性；检查配置来自可信项目配置及已批准变更。远端写入只由独立交付入口执行，并在每个副作用前重查批准、候选、证据和生命周期。

Run completed 由冻结完成政策/delivery_kind、当前有效图、已封口扩展集合和全部承诺义务判定，不写死 PR，也不能靠删节点通过。摘要列出产物、必需结果、限制和待核对状态；模型结束不是检查通过。业务完成与进程退出/消费结算独立，不释放未知占用。

## 7. 取消、恢复与事件

暂停只阻止新的步骤派发与交付；取消请求停止活动 Attempt，并撤销尚未取得 activation 的副作用。汇合、条件选择、人工决定和返工批次都保存为持久事实，不能在页面刷新时重新触发。尚未确定是否停止的执行保持 reconciling/unknown，不启动可能重复写入或消费的替代者。

命令携带主体/授权范围/任期、Project/Conversation/Run、base TaskGraphRevision、Step revision 与幂等键。决定/步骤事件关联冻结定义、扩展/封口、Task/Attempt、输入输出摘要及游标；Hub 展示引擎接受的运行图和决定依据，不另存模型自报 DAG。回执丢失先查同一调度/人工/交付命令，不重提新图或重复启动。

恢复加载冻结定义/初始授权、已接受图修订、提交任期、ExpansionSet 与事件水位，再核对执行/产物/消费；不重新调用模型猜图代替持久事实。配置更新不迁移旧 Run；缺历史版本或摘要不符明确阻塞。新事实可触发有依据的受控调度，仍用原定义与有效授权；历史恢复沿 restore epoch 冻结，不自动重放旧 outbox。

## 8. 两个具体配置例子

以下 YAML 是设计示意，展示声明式意图，不代表当前已有此 schema 的解析器；`@N` 表示固定 revision，输入引用必须由编译器解析为受管产物身份。

### 例 A：研究两种方案并交付报告

```yaml
id: compare-designs
revision: 1
delivery_kind: report
roles:
  researcher: role:source-researcher@1
  editor: role:technical-editor@2
steps:
  - id: research-a
    kind: agent_task@1
    role: researcher
    inputs: { topic: requirement.option_a }
    output_contract: sourced-notes@1
  - id: research-b
    kind: agent_task@1
    role: researcher
    inputs: { topic: requirement.option_b }
    output_contract: sourced-notes@1
  - id: report
    kind: agent_task@1
    role: editor
    depends_on: [research-a, research-b]
    join: all_required
    inputs: { sources: [research-a.output, research-b.output] }
    output_contract: comparison-report@1
completion:
  required_steps: [research-a, research-b, report]
  artifact: report.output
```

两位研究者可并行读取已批准材料；编辑者等待两个产物后整理报告。来源引用完整性等检查由 `comparison-report@1` 与批准的质量策略声明。全部产物接受后 Run 可以 completed；不会插入 Worker 编码、Candidate integration、Git push 或 PR。若用户添加“人工选方案”，则报告后增加 human_decision，等待当前用户对该报告版本作出决定。

### 例 B：代码修改并创建 PR

code-to-pr 可配置获权角色读取需求/仓库后决定是否拆分、边界和派发；扩展集合封口并收齐结果后形成组合候选，再 checks、独立 Review、PR。拆分可按模块、接口或迁移阶段，不固定前后端或人数；已有静态模板仍可使用。

Review 返回 changes_requested 时，获权角色按已批准的有限返工政策安排修复；新候选重新执行必需 checks 和独立 Review。inconclusive 保持核对；删审查义务、设永不成立条件或让作者改名审查均拒绝。改变 PR 目标按授权影响处理，保留原义务/历史，不能把未完成 PR 写成已完成交付。

## 9. 从当前固定契约迁移

2026-09-14 核对的现有入口包括 `backend/karajan/routing/models.py` 的 `Role = Literal["commander", "worker", "reviewer"]`、`Collaboration.command_mode = "lead_with_advisers"`、`delivery_target = "pull_request"`，以及 `backend/karajan/runs/models.py` 的同类 `PlanTask.role` 和 `Authorization.delivery = "none" | "pull_request"`。项目配置、配额、probe 和部分运行适配契约也包含固定角色字段。这些是迁移触点，不是新契约已经实现的证据。

迁移按以下边界实施：

1. 发布受管内置 RoleDefinition、code-to-pr WorkflowDefinition 和执行种类 revision，使旧三角色与原交付行为有确定映射。旧 JSON/历史 qualification 的原字节、digest 和证据不重写；使用带版本的兼容解析/投影保存新引用和迁移记录。
2. 新 RoleDefinitionRef/WorkflowDefinitionRef 贯穿配置、提案编译、批准、任务、路由、准入、运行视图和事件。以稳定引用与能力/可信职责约束替代产品角色枚举；不能只在前端允许任意名字、后端仍默认为 Worker。
3. 状态写入权与业务调度决策权分开，以工作范围的 SchedulerGrant/任期替代全 Run 固定 Commander 独占；保留审查独立性、资源保护和交付门，旧名称映射不继承新能力资格。
4. 版本化增加 delivery_kind 和通用 Artifact/完成条件。旧 `delivery="none"` 只证明没有远端交付授权，不能直接推断为 report 或 patch；已知旧 code-to-pr Run 保留其目标，无法确定目标的旧数据进入兼容详情或显式迁移决定，不能自动记成 completed。
5. 以冻结定义及有来源 TaskGraphRevision 替代硬编码流水线，接通角色真实调度、范围内自动执行、扩展封口与资源排队。旧批准仍验原 digest，不重算历史 hash，不把旧串行路径视为动态调度通过。
6. 前端以 Designer 对话生成配置，并从同一版本构造流程图、角色/步骤表、diff 与确认部署入口；角色引用、步骤与目标产物替代固定三选一和固定阶段文案。Hub 保留同一会话、任务卡和可信反馈；表格/API 是协作工具而非先填表的主流程。旧草稿通过明确版本兼容转换，不触发执行。
7. 接通配置文件到实际部署记录、调度器加载及 Run 输入的同一摘要链；模板发布和文件下载不算部署成功。Designer 模型行为、图表版本、部署回读和实际执行分别验收，不能只以声明式样例或 API 结构关闭产品闭环。

## 10. 可观察验收

| ID | 操作 | 必须观察到的结果 |
|---|---|---|
| WF-AC01 自定义职责 | 创建研究员/文档编辑及定制调度职责，引用固定角色版本批准运行 | 职责和契约真实传递；核对有效 grant/任期，重叠范围经 CAS，用户授权不被角色取得 |
| WF-AC02 非 PR 完成 | 运行例 A，并分别完成一个只产 patch 的流程 | 按各自必需产物/证据完成，无自动编码/PR 步骤，不调用远端发布入口 |
| WF-AC03 真实拓扑 | 配置两个并行分支及汇合；令一个分支失败或 unknown | 独立 Attempt 与输入可追溯；汇合不提前通过，unknown 不被跳过；真实模型并行另取 S 证据 |
| WF-AC04 条件与有限返工 | 触发条件分支和两轮修复，再重启、改名或重新派发 | 选择与输入摘要持久化，未选分支有明确原因；次数不重置，达到边界停止自动返工 |
| WF-AC05 人工决定 | 对当前节点确认，再提交旧版本、重复键和超时请求 | 同一有效决定只生效一次；旧决定拒绝，超时不等于同意，不复制模型批准权 |
| WF-AC06 冻结与修订 | 发布模板新版，同时提交范围内图修订与范围外变更 | 旧 Run 保留原定义；范围内机械校验自动生效，范围外才批准；保留承诺义务及 Attempt 快照 |
| WF-AC07 PR 不可降门 | 删除/跳过检查审查、让作者改名审查，或用 report 产物触发 PR | 编译或最终 gate 拒绝并解释；只有同候选机器 checks、真实独立 Review 和有效授权能交付 |
| WF-AC08 权限隔离 | 自定义角色请求越界工具/来源/受保护额度或远端写入 | 名称、职责和模板均不扩大权限；实际执行边界由可信授权和适配器验证 |
| WF-AC09 取消恢复 | 在分支、汇合、返工、人工节点和 PR 回执空窗取消/重启 | 不重复启动、消费或发布；未知先核对，冻结定义/事件水位和对象身份保持一致 |
| WF-AC10 兼容迁移 | 迁移旧三角色 Run、none 授权、历史批准与 qualification | 原 ID/digest/证据可追溯，行为不被重写；none 不被误判目标，未验收新能力不自动启用 |
| WF-AC11 执行种类边界 | 引用未知种类、自由脚本条件或不兼容输入输出 | 在发布/批准前明确拒绝，不执行配置中的代码；注册新种类必须有独立实现与验收 |

C/U 证明配置/授权/页面，P 证明本机执行/取消/隔离，S 证明真实模型，G 证明 PR 副作用。r8 实际角色调度与无内置人数限制另按 [RS-AC](11-role-directed-scheduling.md) 验收；活动用例修订不回写已发布 Issue/证据或扩大 P3/A01–A26。fixture 不替代模型实际提交决定与执行证据。
