# Commander Workbench 后端契约

原修订：2026-09-09；活动正文：2026-09-16 r9。本文是目标契约，不是整体验收记录。PR #167 的会话/草稿/Hub 后端子集不代表网关、Workflow、Designer 部署、角色动态调度或会话用量统计已完成；新增行为独立实现验收，旧 PR155 缺口仅留历史。

Hub 的 Designer 在独立有限创作授权下接收文字并实际写配置，可信服务从同一文件包生成可互动修改的图表；表格/文件直接修改走确定性校验编译，无需模型调用。确认后先准备 pending、调度器加载/readback，再 CAS active，取得真实部署回执。一次明确授权可同时批准部署和一个具体 Run，不要求重复确认。来源、定义与完整部署生命周期分别以 [08](08-provider-gateway.md)、[09](09-configurable-workflows.md)、[10](10-conversational-workflow-deployment.md) 为准。

r8 增加 [11](11-role-directed-scheduling.md) 的 SchedulerGrant、SchedulingDecision、TaskGraphRevision 与 ExpansionSet。Workflow 中获权角色实际决定拆分/依赖/角色/优先级/派发，范围内命令经引擎校验后自动生效；Karajan 不固定前后端或 coding Agent 人数，资源不足排队且保留合法图。

r9 增加 [12 会话用量](12-conversation-usage-accounting.md)：调用归属包含无 Run 的创作执行，Hub 用量摘要覆盖整个会话；角色/Agent、任务、模型、实际 provider 的聚合与明细读取同一持久账本，不以当前选中 Run 或配置别名代替真实范围和路由。

## 1. 领域关系与兼容迁移

规范关系是 `Project 1 ── N CommanderConversation 1 ── N Run`：

- `Project` 是受管仓库的稳定身份，拥有仓库基准、策略和所有会话；项目名称、分支文本或当前页面选择不能代替 `project_id`。
- `CommanderConversation` 是项目内持久用户控制上下文，拥有消息、草稿、默认模型/来源、设计会话、提案与确认记录；可以没有 Run 或包含多个 Run，不跨项目。Commander 是默认规划身份的产品名称，不要求所有自定义 Workflow 有同名节点。
- Designer 创作会话可先于目标 Run，绑定独立 Profile/ModelBinding、授权和累计预算/调用/修复/时间界限。它产生 WorkflowBundle/Preview 候选，不能自行激活部署或授权 Run。
- WorkflowDeployment 是项目内目标槽位的持久部署事实，固定包/模板编译摘要与加载回执；创建它的会话可追溯。定义登记不等于 active，active 也不等于某次 Run 已获执行授权。
- Run 绑定唯一 conversation/project，冻结部署/定义/编译摘要、初始输入/Plan/授权、可用角色/来源及产物政策。当前运行图由 TaskGraphRevision 演进，已启动 Attempt 固定自己的任务/图/来源/权限；新 active/回滚不回写旧 Run。多个有效 grant 可明确授权重叠范围，经 CAS 处理图竞争；同一 grant/交接槽位仅当前任期，不限制全 Run 只能一个调度角色。
- 任务、条件/汇合/返工裁决、模型执行与产物证据归属 Run；设计候选和部署有自己的生命周期。Hub 聚合这些事实，不拥有第二份状态，也不把 report/patch 转成强制 PR。

2026-09-09 迁移设计以 PR155 的 Project→Run 入口为起点；PR #167 后已出现会话持久化/Hub 后端子集，不应重复把这些整体列为缺失。以下是需由实现持续保证的兼容不变量，而非尚未执行的工作清单：

1. 数据库新增 `commander_conversations`，以及 `runs.conversation_id` 非空外键；迁移脚本为每个旧 Project 建立一个确定的 legacy conversation，旧 Run 按规范化 `created_at, id` 顺序归入，保留原 Run ID、revision、term、plan 和证据引用。迁移运行多次结果相同。
2. 新建项目不自动创建会话或 Run；用户明确新建会话后才创建 Conversation。兼容旧 `POST /v1/runs` 时，只有确实缺少 conversation_id 才幂等 ensure 项目 default/legacy conversation；这是兼容解析，不是导航触发。已声明但无效/跨项目引用拒绝。新任务/设计草稿不创建执行 Attempt；明确的 Designer 创作命令才消费已批准的创作预算。
3. `GET /v1/runs?project_id=` 继续返回所有项目 Run；新增 `conversation_id` 过滤和显式 `GET /v1/conversations/{id}/runs`。旧 `POST /v1/runs` 可以继续接受兼容请求，但必须由服务端解析 `conversation_id`，缺失时使用项目的 legacy/default conversation，并返回迁移后的身份。
4. 新 UI 以 Conversation/Hub 为控制入口，ProjectRuns 保留详情/兼容读视图；两者读取同一事实和游标。r7 新定义/字段经显式兼容映射进入新契约，旧 Run 保留原 schema/批准/证据；未具备新完成语义的旧 `delivery=none` 不自动成为 report/patch。

迁移要求保持幂等、可回滚和可审计：重复迁移不改变 Run ID 或候选 digest；跨项目 conversation/run 绑定返回稳定 `CROSS_PROJECT_REFERENCE`；迁移后仍缺 conversation 或项目身份无效的数据，显示恢复阻塞而不是猜测归属。

## 2. 持久对象与版本字段

以下是 r8 目标对象契约，SQL/ORM 与兼容 DTO 由实现任务决定，不代表已存在。可变聚合使用 revision，写命令绑定幂等键/主体/载荷，不可变定义和每个图修订保存摘要。

| 对象 | 必需字段 | 不变量 |
|---|---|---|
| `Project` | `id`, `repository_identity`, `base_ref`, `project_revision` | 仓库身份稳定；读取/写入都核对认证主体和 project revision |
| `CommanderConversation` | `id`, `project_id`, `title`, `state`, `commander_profile_ref`, `commander_source_ref`, `draft_revision`, `last_event_seq`, `revision` | 永远只属于一个 Project；选择/切换不暂停或启动 Run；关闭会话不删除 Run |
| `ConversationMessage` | `id`, `conversation_id`, `client_message_id`, `role`, `content_ref`, `created_at`, `revision` | message role 是消息发言者，不是业务角色枚举；普通文字不授予执行权；精确确认须由 UI/服务转为绑定当前预览的命令；同 client key 幂等 |
| `ConversationDraft` | `conversation_id`, `draft_id`, `content`, `selected_task_id`, `base_plan_revision`, `revision` | 草稿与项目/会话绑定；切换项目恢复对应草稿；新对话不覆盖旧草稿 |
| Designer 创作会话 | project/conversation、控制提交身份、Profile/ModelBinding、创作授权、累计调用/自动修复/时间/费用、候选及恢复状态 | 可先于目标 Run；未知调用先核对，新 proposal/重启不重置界限；模型文字不能部署 |
| WorkflowBundle / Preview | bundle/schema revision、manifest/file byte digest、bundle_digest、compiler revision、模板 compiled_digest、proposal/preview revision、图/表/diff、权限/资源影响 | 实际文件是同源输入；参数化模板摘要不含具体 Run 输入；直接改表格/文件重新校验，不强制调用模型 |
| WorkflowDeployment | command/deployment ID、project/conversation、槽位与 expected active revision、包/编译摘要、pending/active、加载/readiness 回执、intent/result | pending 先加载/readback 再 CAS active；旧 active 在准备期有效；结果未知先查询，旧 ready 不是重启后加载证明 |
| `PlanProposal` | 会话、Run、定义/部署、初始输入/Plan、来源/产物政策、调度范围与授权预览 | 展示初始任务和允许的动态动作/扩展点，不要求预枚举全部节点；初始批准与后续授权内接受分开 |
| SchedulerGrant / SchedulingDecision | 实际 ExecutionRef、授予/委派链、动作/范围/来源/累计边界、当前 revision/任期；触发材料、base graph revision、决定与回执 | 多有效 grant 可明确重叠；范围内校验自动生效，角色不自签用户授权或 activation |
| TaskGraphRevision / ExpansionSet | 当前图唯一引用、父版本/digest、决定/授权来源、任务/依赖/优先级/派发、扩展成员/封口与义务 | CAS/幂等接受；资源不足排队，不截断任务图；未封口或义务未完成不汇合，不能删节点免除义务 |
| `TaskAssignment` | run/task/step revision、role_definition_ref/digest、execution_kind、适用的 Profile/ModelBinding、依赖产物、路径/工具约束、验收与 revision | 任意角色显示名不授予权限；显式单一绑定不回退；只有预先 opt-in 的替代集合可用于新 Attempt |
| `Run` | 既有身份、冻结部署/初始输入/Plan/授权/政策、当前图/有效 grant 引用、event watermark/revision | 继续/返工用自己的冻结定义及合法后续图，不追新 active；不改已启动 Attempt |
| `RunSnapshot` | 冻结定义/初始授权、当前图与决定/来源、扩展集合/义务/排队、tasks/attempts/artifacts/完成门 | Hub/详情同一事实；初始批准不冒充每次用户新批准，资源背压不删任务 |
| `ModelFeedback` | 明确的 Attempt 或 Designer 创作执行引用、feedback_seq、kind、observed_at、source、可空模型与文本引用 | 进度与连接心跳分开；自报模型不替代网关实际来源证据 |
| `ConnectionHeartbeat` | `connection_id`, `observed_at`, `last_event_seq` | 只表示浏览器连接存活，不改变 Attempt 状态或 terminal truth |
| `Event` | project/conversation、适用的 Run/design session/deployment 引用、seq、event_type、object_revision、payload_digest | 单调游标、可重放、按真实归属过滤；不为 pre-Run 创作伪造 Run，跨项目事件不能进入 Hub |

`ModelFeedback` 的 progress、waiting_input、waiting_dependency、output_received 更新最近反馈，ConnectionHeartbeat 只更新连接新鲜度。Run/Attempt 终态由可信执行/核对产生，部署 ready 由同摘要加载/readiness 与 active 发布结果产生，Designer 输出已保存只证明候选保存。心跳新鲜但无输出显示等待响应；来源窗口过期显示待核对，不伪造完成、失败或停止。

## 3. HTTP 路由与命令语义

以下列出既有兼容与目标命令面，不宣称整表已实现；新 Designer/部署路径和 DTO 需实现切片单独定稿。所有入口受 [04](04-api-and-workbench.md) 的认证、CSRF、幂等、If-Match 和 reason code 约束。

| 操作 | 路由 | 语义 |
|---|---|---|
| 项目会话列表 | `GET /v1/projects/{project_id}/conversations` | 只返回该 Project 会话；按稳定 ID 过滤，不按名称猜测 |
| 创建会话 | `POST /v1/projects/{project_id}/conversations` | 保存 Commander profile/source 选择和会话标题；不创建 Run、Attempt 或 execution approval |
| 会话快照 | `GET /v1/conversations/{conversation_id}/snapshot` | 草稿、消息游标、设计候选/预览、部署与 Plan/Run 摘要及 freshness；跨项目主体拒绝 |
| 会话消息 | `POST /v1/conversations/{id}/messages` | 持久化文字及 client message key；明确创作请求可进入获准的有限 Designer 调用，普通消息不成为 runtime/deploy authorization |
| 会话草稿 | `PUT /v1/conversations/{id}/draft` | `If-Match` + draft revision；切换会话可恢复，冲突返回当前草稿 |
| 新任务建议 | `POST /v1/conversations/{id}/task-drafts` | 创建需求/任务草稿和待确认 proposal；不自动插入已批准 Run 或启动执行 |
| 提案修订 | `POST /v1/conversations/{id}/proposals` | 可信服务解析 role/workflow/execution kind/model binding/产物政策与依赖，生成新 Plan/授权预览；单纯编译不消费模型 |
| 角色实际调度 | ExecutionRef/grant 绑定的 SchedulingDecision 窄接口，见 [11](11-role-directed-scheduling.md) | 校验有效 grant/委派链、expected graph revision、动作/输入/义务，CAS 接受后自动派发或排队；不是逐任务待人批建议 |
| 动态图/授权读回 | decision/graph/grant ID 的结果查询与授权内委派命令 | 重复命令同结果；竞争重叠图返回具体 CAS 冲突，角色可在原 grant 内重作决定；资源重试不重调模型 |
| Designer 生成/修订 | 设计会话下的持久创作命令，见 [10](10-conversational-workflow-deployment.md) | 先核对独立创作授权和累计界限，再调用模型产出真实文件；错误可在有界循环内修复，不自动部署 |
| 配置/图表修改 | 设计候选 revision 的文件/表格变更与编译命令 | 确定性校验并生成同源图/表/diff，新 revision 使旧预览失效；图节点不能保存第二份执行图 |
| 确认并部署 | 绑定 preview 的持久部署命令和结果查询 | 精确绑定包/模板编译摘要、项目/会话、槽位 expected revision、授权影响及 deploy_only 动作；pending 加载/readback 后 CAS active |
| 确认、部署并运行 | 部署命令的明确复合动作 | 另绑定 input digest、具体 Plan/执行授权并幂等关联 Run；一次确认可组合 acceptance/部署/approval，变更拒绝旧命令 |
| 部署查看/回滚 | deployment/slot 的受管 ID 读取与条件更新命令 | 读取实际生效文件/图表/加载回执；回滚核对目标就绪后条件切换，仅影响新 Run |
| 接受建议 | `POST /v1/conversations/{id}/proposals/{revision}/accept` | 可选的轻量确认动作，用于保存用户对默认分工的调整；不要求用户为“接受建议”与“批准计划”重复确认 |
| 计划批准/分发 | 现有 `POST /v1/runs/{id}/plan-approval` 的兼容批准语义 | 初始输入/Plan/执行与调度授权同版确认，复合部署可内部消费；范围外变更再批，范围内图修订标 accepted_under_grant，不伪造用户新批准 |
| Hub 快照 | `GET /v1/conversations/{id}/hub` | 聚合设计/部署与 Run/Step/Task/Agent/Artifact/完成门；代码 PR 再显示 Candidate/checks/review/PR；只读 |
| 会话用量 / 明细 | `GET /v1/conversations/{id}/usage` 与 `/usage/records` | 角色/Agent、任务、模型及实际 provider 分组/交叉筛选；全会话含 pre-Run，返回同一账本 revision、覆盖与未知状态，分页不截断汇总；详见 [12](12-conversation-usage-accounting.md) |
| 事件流 | `GET /v1/conversations/{id}/events?after_seq=N` | SSE 返回 snapshot watermark；游标过期或检测到缺口返回 `event_gap`，客户端重新 GET snapshot 后从新 seq 继续 |
| 单 Attempt 反馈 | `GET /v1/attempts/{id}/feedback?after_seq=N` | 显示 model progress、来源时间和 terminal truth；不以连接 heartbeat 代替执行事件 |

所有会话、设计包/预览、部署、提案和命令检查认证主体及真实 project/conversation/run 归属，不信任浏览器传入的多级 ID。相同幂等键和载荷返回原结果，不同载荷返回 `IDEMPOTENCY_KEY_REUSED`。需要当前版本的动作使用 If-Match/expected revision；旧预览、旧 Plan、旧 term 或并发槽位变化返回具体冲突。部署回执丢失先查询命令/槽位/关联 Run，不能重新生成配置或再建 Run 掩盖未知。

## 4. Hub 聚合、SSE 恢复与调度边界

Hub 分别标明配置预览、active、Run 冻结定义/初始授权和当前已接受运行图，展示每次角色决定/图 diff、授予范围、扩展封口及排队原因。不能把草稿/模型自报图或新 active 冒充旧 Run 配置，也不能因超出 UI 分页而丢掉任务。产物/详情读同一 watermark，不为报告流程添加 PR。

Hub 的会话用量摘要携带独立 ledger revision、事件 watermark 与完整性；用量变化遵循相同快照/事件恢复机制，查询的汇总和分页明细固定同一用量快照。跨会话筛选拒绝，迟到回执只更新原归属；刷新不调用 Commander，不另建前端累计计数器。计量、路由与去重仅以 [12](12-conversation-usage-accounting.md) 为详细契约。

SSE 客户端按以下顺序恢复：

1. 保存最后收到的 `event_seq`，断线重连携带 `after_seq` 和 conversation identity。
2. 服务端检查事件保留窗口及项目/会话归属；可补齐则按 seq 重放，重复事件由 event ID/seq 幂等处理。
3. 发现 seq gap、游标过期、revision 不连续或项目切换时，先返回 `event_gap`/`snapshot_required`，客户端读取同一 Conversation Hub snapshot，再从 snapshot watermark 订阅。
4. 快照和事件不能把连接、动画或模型文本当终态。Run/Attempt 由可信协调器与 execution/reconcile 写入，部署由持久 intent、实际加载/readback 和 active CAS 结果写入；重启后旧 ready 仅作历史，核对完实际 active 加载后才能接新 Run。

任务发现、结果或需判断的阻塞事件可触发获权调度角色实际作决定；可信引擎消费接受命令/outbox，并机械处理依赖/资源。刷新、心跳、直接改配置、图表编译、资源释放与部署核对不轮询模型；资源释放继续原派发动作。新对话/草稿不授予执行权，授权内动态任务命令则可直接生效。

新 Run 采用部署时核对 active/加载摘要，旧 Run 继续/重试/返工用冻结定义、初始授权及已接受动态图；已启动 Attempt 不热改。角色决定业务安排，引擎核对有效 grant、依赖、sealed 集合及义务/资源，不固定全局/项目 Agent 人数。pending 不接新 Run，旧 active 在准备期有效，切换未知先核对。

## 5. 历史 owner 映射与新增范围

下表保留 2026-09-09 根据 PR155 形成的原任务归属；它不报告修订日的开闭/通过状态，也不因 PR #167 的后端子集合入而宣布整个 P1 完成。原票 AC 和当次候选证据仍是验收依据。

| 原缺口 | 当时背景 | 原归属与验收 |
|---|---|---|
| Project/Run 之间缺 Conversation 持久身份 | PR155 以 project 直接关联，尚无会话聚合；后续 PR #167 已承接后端子集 | #159 负责身份迁移与兼容读写，#160 复用其身份接入计划；UX-AC01/02/13/14，FR01/03/17/18 |
| Hub 聚合与 currentCandidate 回链 | 当时 ProjectRuns/计划页面按 Run 展示，需项目内会话主聚合和候选回链 | #159/#162；UX-AC05/08/11，FR14/17/19/20 |
| 消息、草稿、任务提案持久化 | 当时 Run 输入未承载会话消息/草稿与项目切换恢复 | #159 会话/消息/草稿；#160 提案生成/编辑；UX-AC02/03/13/14，FR03/04/05/17 |
| 版本/幂等/跨项目约束 | 当时部分 Project/Run 命令已有 key/revision，要求贯穿新增层 | #159 会话/草稿；#160 提案/批准；#161/#162 运行/交付；UX-AC03/06/09/14，FR04/05/11/15/18 |
| progress/heartbeat/terminal truth | 当时需统一模型反馈、连接心跳和终态字段 | #159/#161/#162；UX-AC10/12，FR15/17/18/19 |
| 新建入口不自动执行 | 新 UI 需让新对话/新任务落在 proposal/draft，不触发 approval/dispatch | #159 负责非执行的新会话/任务草稿创建与读回；#160 承接交给 Commander 的提案及批准接线；UX-AC02/03/13，FR03/04/12 |
| 真实并行和独立 Review | 原设计要求真实身份/重叠窗口/独立审查，不能用模型自报或 fixture 代替；实际状态按当次证据 | #161/#162；UX-AC04/08，FR07/13/14/19/20；保留原真实执行范围 |

网关、角色/Workflow、Artifact、Designer/部署与 r8 动态调度分别按 GW/WF/WD/RS 承接；复用旧身份/事务不等于已完成，也不自动加入 #159–#162 原 AC。[治理索引](../planning/design-governance-20260909.md) 维护归属。

## 6. 验收记录要求

本契约定稿只表示设计和迁移方案可审阅。原接口验收保存 schema revision、迁移输入/输出、Project/Conversation/Run IDs、请求/响应、SSE gap/recovery、跨项目拒绝、幂等/If-Match 冲突与反馈/heartbeat 样例。P1/P2 的 C/U/P 不替代 P3 原要求的真实重叠执行或 P4 独立审查 S 证据，仍按原 Issue 验收。

r7 的配置/同源/部署/Run 闭环仍按 [WD](10-conversational-workflow-deployment.md) 留证，原不同拓扑样例保留。r8 另保存真实角色 SchedulingDecision、grant/委派链、图修订/CAS、背压与扩展封口，证明原授权内自动执行、旧 Attempt 冻结和义务不删减；静态手写图不替代 [RS-AC](11-role-directed-scheduling.md)。不回写原 P3 两任务或 A01–A26 验收。
