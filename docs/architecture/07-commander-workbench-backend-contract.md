# Commander Workbench 后端契约

修订：2026-09-09。状态：**提议中的后端设计，尚未实现或验收。** 本文把 [Commander Workbench PRD](../prd/commander-workbench.md) 的项目、会话、Hub 聚合和实时反馈落到现有 Project/Run API 的迁移边界。现有实现证据、PR155 候选和原 A01–A26 仍按各自范围记录，不能由本文件的 schema 或路由提议升级为产品通过。

## 1. 领域关系与兼容迁移

规范关系是 `Project 1 ── N CommanderConversation 1 ── N Run`：

- `Project` 是受管仓库的稳定身份，拥有仓库基准、策略和所有会话；项目名称、分支文本或当前页面选择不能代替 `project_id`。
- `CommanderConversation` 是项目内持久的用户/Commander 控制上下文，拥有消息、未发送草稿、当前 Commander profile/source、任务建议和用户确认记录。它可以没有 Run，也可以有多个历史或当前 Run；一个会话不会跨项目。
- `Run` 是会话针对某项 Requirement 的一次完整执行过程，仍拥有 plan revision、Task、Attempt、Candidate、Evidence 和 Delivery。每个 Run 必须绑定且只能绑定一个 `conversation_id` 和一个 `project_id`；该 project 必须等于 conversation 的 project。Run 批准后固定其 Commander term、Profile/source 和 authorization；用户之后切换会话默认选择只影响未来草稿/新 Run，不回写或热切换已批准 Run。
- `Task`、`Agent/Attempt`、Candidate、checks、Review 和 PR 事实归属 Run；Hub 是聚合读模型，不拥有第二份状态。

当前代码已有认证的 `POST/GET /v1/projects`、`GET /v1/projects/{id}`、`POST/GET /v1/runs`、`GET /v1/runs/{id}` 以及 ProjectRuns/NewRunForm 的 Project→Run 入口。只读核对 PR155 候选确认它仍以 `project_id` 创建 Run、在 Run 里保存 Commander term/plan/approval，并扩展 Planning readiness、snapshot/transport 与前端计划状态；它没有建立 Conversation 身份。迁移保持旧路由可用，同时把现有 Run 归入一个确定的会话：

1. 数据库新增 `commander_conversations`，以及 `runs.conversation_id` 非空外键；迁移脚本为每个旧 Project 建立一个确定的 legacy conversation，旧 Run 按规范化 `created_at, id` 顺序归入，保留原 Run ID、revision、term、plan 和证据引用。迁移运行多次结果相同。
2. 新建项目不自动创建会话或 Run；用户点击“与 Commander 开始”或“新对话”才创建 Conversation。为兼容旧 `POST /v1/runs`，服务端在确实缺少 `conversation_id` 时幂等地 ensure 该项目的 default/legacy conversation，再创建 Run；这不是用户导航创建会话。若请求带有不存在、已删除或不属于该项目的 conversation，则返回 `CROSS_PROJECT_REFERENCE`/`CONVERSATION_NOT_FOUND`，不能猜测归属。新任务只创建会话草稿/待确认建议，不创建执行 Attempt。
3. `GET /v1/runs?project_id=` 继续返回所有项目 Run；新增 `conversation_id` 过滤和显式 `GET /v1/conversations/{id}/runs`。旧 `POST /v1/runs` 可以继续接受兼容请求，但必须由服务端解析 `conversation_id`，缺失时使用项目的 legacy/default conversation，并返回迁移后的身份。
4. 新 UI 只把 Conversation/Hub 作为控制入口；旧 ProjectRuns 作为详情/兼容读视图。两者读取同一个快照聚合和事件游标，不各自维护运行状态。缺失 conversation 只对历史迁移/兼容创建走 deterministic default；已声明但无效的引用一律阻塞。

迁移要求保持幂等、可回滚和可审计：重复迁移不改变 Run ID 或候选 digest；跨项目 conversation/run 绑定返回稳定 `CROSS_PROJECT_REFERENCE`；旧数据缺 conversation 时显示恢复阻塞而不是猜测归属。

## 2. 持久对象与版本字段

以下是最小后端契约，具体 SQL/ORM 由实现任务决定。所有可变聚合使用整数 `revision`；所有跨请求写操作带 `Idempotency-Key` 和主体绑定的规范化载荷 digest。

| 对象 | 必需字段 | 不变量 |
|---|---|---|
| `Project` | `id`, `repository_identity`, `base_ref`, `project_revision` | 仓库身份稳定；读取/写入都核对认证主体和 project revision |
| `CommanderConversation` | `id`, `project_id`, `title`, `state`, `commander_profile_ref`, `commander_source_ref`, `draft_revision`, `last_event_seq`, `revision` | 永远只属于一个 Project；选择/切换不暂停或启动 Run；关闭会话不删除 Run |
| `ConversationMessage` | `id`, `conversation_id`, `client_message_id`, `role`, `content_ref`, `created_at`, `revision` | 自然语言用户消息可保存；runtime prompt、secret、执行事实不能由浏览器直接提交为授权；同 client key 幂等 |
| `ConversationDraft` | `conversation_id`, `draft_id`, `content`, `selected_task_id`, `base_plan_revision`, `revision` | 草稿与项目/会话绑定；切换项目恢复对应草稿；新对话不覆盖旧草稿 |
| `PlanProposal` | `conversation_id`, `run_id?`, `proposal_revision`, `task_graph_digest`, `source_digest`, `authorization_preview`, `status` | 提案可编辑但不执行；批准必须匹配页面所见 revision/digest |
| `TaskAssignment` | `run_id`, `task_revision_id`, `role`, `profile_ref`, `source_ref`, `dependency_refs`, `allowed_paths`, `checks`, `revision` | 显式绑定优先；严格指定单一 Profile 不静默换源；预先 opt-in 的替代集合才可创建新 Attempt |
| `Run` | 既有 Run 字段 + `conversation_id`, `project_id`, `plan_revision`, `snapshot_event_seq`, `revision` | conversation/project 必须一致；新任务/对话不自动批准或执行 |
| `RunSnapshot` | `run_id`, `snapshot_event_seq`, `run_revision`, `tasks`, `attempts`, `candidate`, `checks`, `review`, `delivery`, `freshness` | Hub、Tasks/Agents 和详情抽屉读取同一快照；缺口时先重新快照 |
| `ModelFeedback` | `attempt_id`, `feedback_seq`, `kind`, `observed_at`, `source`, `model_ref?`, `text_ref?` | 进度反馈与连接心跳分开；不把 UI timer 当运行证据 |
| `ConnectionHeartbeat` | `connection_id`, `observed_at`, `last_event_seq` | 只表示浏览器连接存活，不改变 Attempt 状态或 terminal truth |
| `Event` | `project_id`, `conversation_id`, `run_id?`, `seq`, `event_type`, `object_revision`, `payload_digest` | 单调游标、可重放、按身份过滤；跨项目事件不得进入当前 Hub |

`ModelFeedback` 的 `progress`、`waiting_input`、`waiting_dependency`、`output_received` 等事件更新“最近反馈”；`ConnectionHeartbeat` 只更新连接新鲜度。`completed`、`failed`、`cancelled`、`unknown` 只能来自可信 Attempt/Run 结果或核对事件。心跳新鲜而没有模型输出显示“等待响应”，反馈超过来源窗口显示“反馈中断/待核对”，不能伪造成功、失败或停止。

## 3. HTTP 路由与命令语义

以下路由是建议新增/迁移面；现有 Project/Run、Planning、approval、admission 路由继续受 [04 API 与工作台](04-api-and-workbench.md) 的认证、CSRF、Idempotency-Key、If-Match 和 reason code 约束。

| 操作 | 路由 | 语义 |
|---|---|---|
| 项目会话列表 | `GET /v1/projects/{project_id}/conversations` | 只返回该 Project 会话；按稳定 ID 过滤，不按名称猜测 |
| 创建会话 | `POST /v1/projects/{project_id}/conversations` | 保存 Commander profile/source 选择和会话标题；不创建 Run、Attempt 或 execution approval |
| 会话快照 | `GET /v1/conversations/{conversation_id}/snapshot` | 返回草稿、消息游标、当前 proposed plan、Run 摘要和 freshness；跨项目主体拒绝 |
| 会话消息 | `POST /v1/conversations/{id}/messages` | 接受用户自然语言消息，持久化 client message key；触发 Commander 判断属于显式命令，不能把消息文本当 runtime authorization |
| 会话草稿 | `PUT /v1/conversations/{id}/draft` | `If-Match` + draft revision；切换会话可恢复，冲突返回当前草稿 |
| 新任务建议 | `POST /v1/conversations/{id}/task-drafts` | 创建需求/任务草稿和待确认 proposal；不自动插入已批准 Run，不启动 Worker |
| 提案修订 | `POST /v1/conversations/{id}/proposals` | 由可信服务编译 task graph、profile/source、依赖和 authorization preview；生成新 proposal revision |
| 接受建议 | `POST /v1/conversations/{id}/proposals/{revision}/accept` | 可选的轻量确认动作，用于保存用户对默认分工的调整；不要求用户为“接受建议”与“批准计划”重复确认 |
| 计划批准/分发 | 现有 `POST /v1/runs/{id}/plan-approval`（由当前 `backend/karajan/web/runs.py` 暴露），建议增加 `conversation_id` 校验 | 必须传 `plan_revision`, `authorization_digest`, `If-Match`；旧版本/错误会话返回 409/412；这是唯一允许进入 dispatch 的用户命令。若用户直接确认未改动的默认建议，该命令同时记录 acceptance 与 approval |
| Hub 快照 | `GET /v1/conversations/{id}/hub` | 聚合当前 Run/Task/Agent/Candidate/checks/review/PR 摘要；只读，不能成为第二状态机 |
| 事件流 | `GET /v1/conversations/{id}/events?after_seq=N` | SSE 返回 snapshot watermark；游标过期或检测到缺口返回 `event_gap`，客户端重新 GET snapshot 后从新 seq 继续 |
| 单 Attempt 反馈 | `GET /v1/attempts/{id}/feedback?after_seq=N` | 显示 model progress、来源时间和 terminal truth；不以连接 heartbeat 代替执行事件 |

所有会话、消息、草稿、提案和命令检查认证主体、`project_id`、`conversation_id`、`run_id` 的一致性；以资源实际归属查询为准，不信任浏览器传入的多级 ID。相同幂等键和相同载荷返回原命令结果；相同 key 的不同 payload 返回 `IDEMPOTENCY_KEY_REUSED`。需要当前 revision 的动作使用 If-Match；旧计划批准、旧任务改派、旧 term 或已终止对象返回具体冲突，不覆盖新状态。

## 4. Hub 聚合、SSE 恢复与调度边界

Hub 是 primary aggregation view：它把同一 Project/Conversation 下的持久消息、proposal、Run snapshot、Task/Agent 状态、Candidate、checks、independent Review 和 Delivery 摘要合并给用户；当前 Candidate 必须链接回 Hub，使用户从会话回到任务详情仍看到同一 candidate identity。Tasks、Agents、Diff、Checks、Review、Logs、Dependencies 详情都使用 `snapshot_event_seq` 和同一事实源。

SSE 客户端按以下顺序恢复：

1. 保存最后收到的 `event_seq`，断线重连携带 `after_seq` 和 conversation identity。
2. 服务端检查事件保留窗口及项目/会话归属；可补齐则按 seq 重放，重复事件由 event ID/seq 幂等处理。
3. 发现 seq gap、游标过期、revision 不连续或项目切换时，先返回 `event_gap`/`snapshot_required`，客户端读取同一 Conversation Hub snapshot，再从 snapshot watermark 订阅。
4. 快照和事件都不能把连接存活、前端动画或 Commander 文本当作 terminal truth；terminal truth 只由可信协调器和 execution/reconcile 事实写入。

可信调度器直接消费持久命令、outbox 和 Execution 事件推进状态；它不调用高级 Commander 轮询进度，也不把模型调用用于每次刷新。需要用户判断、重新规划、冲突解释或最终汇报时才请求 Commander。`new conversation`、`new task`、草稿保存和查看 Hub 是非执行操作，不能隐含 approval 或 dispatch。

## 5. 当前缺口与 owner 映射

| 缺口 | 当前事实/风险 | 归属与验收 |
|---|---|---|
| Project/Run 之间缺 Conversation 持久身份 | 当前 `/v1/runs` 以 project 直接关联，PR155 扩展 Planning 但未提供会话聚合 | #159/#160 后端接线；UX-AC01/02/13/14，FR01/03/17/18；先完成 schema/backfill/兼容读写 |
| Hub 聚合与 currentCandidate 回链 | 现有 ProjectRuns/计划页面按 Run 展示，缺项目内会话主聚合和候选回链 | #159/#162；UX-AC05/08/11，FR14/17/19/20 |
| 消息、草稿、任务提案的持久化 | 现有 Run 输入不是 Conversation message/draft；自然语言输入、项目切换和未发送草稿需新实体 | #160；UX-AC02/03/13/14，FR03/04/05/17 |
| 版本/幂等/跨项目约束 | 现有 Project/Run 命令已有一部分 key/revision 约束，需贯穿新增层并拒绝交叉引用 | #160/#161；UX-AC03/06/09/14，FR04/05/11/15/18 |
| progress/heartbeat/terminal truth | 现有事件/快照设计有游标，但 Workbench 尚未统一模型反馈、连接心跳和终态字段 | #159/#161/#162；UX-AC10/12，FR15/17/18/19 |
| 新建入口不自动执行 | 新 UI 需让新对话/新任务落在 proposal/draft，不触发 approval/dispatch | #160；UX-AC02/03/13，FR03/04/12 |
| 真实并行和独立 Review | 本文只定义消费契约；当前 C/P/S/G 仍未完成，模型自报和 fixture 不能关闭首演 | #161/#162；UX-AC04/08，FR07/13/14/19/20；保留真实低级模型身份、重叠窗口和独立 Reviewer 证据 |

## 6. 验收记录要求

本契约完成只表示接口设计和迁移方案可审阅。实现任务必须分别保存 schema revision、迁移输入/输出、固定 Project/Conversation/Run IDs、请求/响应摘要、SSE gap/recovery 观察、跨项目拒绝、幂等/If-Match 冲突、模型反馈与 heartbeat 样例及限制。P1/P2 的 C/U/P 不能替代 P3 两个已授权低级模型的 S 级真实重叠执行，也不能替代 P4 独立 Reviewer 的 S 证据；后者仍按当前业务顺序和原 Issue 逐项验收。
