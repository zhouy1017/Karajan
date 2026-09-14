# 接口与 Web 工作台

2026-09-14 r8 设计契约。本文定义 HTTP、执行和资源边界；Hub/身份见 [07](07-commander-workbench-backend-contract.md)，角色实际调度、授权与动态图见 [11](11-role-directed-scheduling.md)。部分 API 已实现，目标字段/路径不代表整体已有。

主入口是 [Designer 对话、配置和部署](10-conversational-workflow-deployment.md)：Agent 实际生成/修改配置，可信服务编译出同源图表供互动修改，精确确认后真实部署。API、表格与文件编辑支撑这一闭环。模型创作计入独立规划账本，纯保存/校验/投影不调用模型；一次确认可组合部署与已明确授权的 Run。职责、步骤与产物政策来自 [Workflow 定义](09-configurable-workflows.md)，模型映射和凭据归属来自 [外置网关](08-provider-gateway.md)。

## 1. 任务输入与模型输出

以下是默认代码模板中一个自定义步骤的目标输入示意，省略完整定义与来源快照；它不是现有封闭三角色 wire schema。职责文本和权限分别引用，新增角色名无需注册为运行时枚举。

```json
{
  "task_id": "task-export-api",
  "revision": 1,
  "task_graph_revision_ref": "graph:run-export:r2",
  "scheduling_decision_ref": "decision:expand-export",
  "scheduler_grant_ref": "grant:export-scope:r1",
  "workflow_revision_ref": "workflow:csv-export:r1",
  "step_id": "implement-export",
  "execution_kind": "agent_task",
  "role_definition_ref": "role:export-engineer:r1",
  "role_digest": "<resolved-role-digest>",
  "model_binding_ref": "model-binding:export:r1",
  "authorization_ref": "authorization:task-export-api:r1",
  "readiness": "ready",
  "complexity": "T2",
  "risk": "standard",
  "domain": ["backend"],
  "objective": "Implement CSV export using the approved field contract",
  "base_sha": "<resolved-commit>",
  "dependencies": [],
  "allowed_paths": ["src/export/**", "tests/export/**"],
  "required_capabilities": ["bounded_implementation", "controlled_tools"],
  "acceptance_refs": ["acceptance:csv-v1"],
  "check_profile_revision": "checks:project-v1",
  "stop_conditions": ["API contract change required", "write outside scope required"],
  "output_kind": "task_candidate",
  "delivery_policy_ref": "delivery-policy:code-pr:r1"
}
```

模型可提交配置候选、PlanProposal、SchedulingDecision、TaskResult、ReviewResult、ChangeRequest。获权角色的 SchedulingDecision 是实际可生效命令，按 grant/身份/当前图/动作/义务校验后提交，不只保存为建议；初始授权内无需逐任务人批。模型不能签发 grant/activation/部署回执，Designer 仍须提交真实文件而非声称已部署。

TaskResult 包含状态、产物引用、变更摘要、验收尝试、未决事项和交回原因。声明的文件、测试和模型身份全部视为待核验信息。ReviewResult 包含 findings 与 `pass / changes_requested / inconclusive`，由 gate 转换成业务结论。

既有代码审查输出的[严格解析边界](../implementation/reviewer-output-parser.md)把上述 wire 值映射为 `passed / failed / inconclusive`，拒绝 `pass` 与 blocking finding 的矛盾。这是内置代码审查结果的兼容契约，不把所有自定义角色输出变成 ReviewResult。纯解析不能证明真实身份、来源、独立性或 Evidence/gate；这些仍由可信控制层按产物政策核验。

## 2. 执行管理接口

执行接口统一使用带类型的 `ExecutionRef`：`run_attempt` 固定 project/conversation/run/task/attempt，`designer_execution` 固定 project/conversation/design_session/execution；两者都绑定授权、fence、预算与启动身份。Designer 不需要伪造 Run/Task/Attempt。旧 `attempt_id` 参数仅是 `run_attempt` 的兼容入口，由可信服务解析完整身份，不适用于独立创作执行。

| 方法 | 输入 | 返回与幂等语义 |
|---|---|---|
| `describe(runtime_version)` | 固定执行器/环境 | 能力及其证据，不启动模型 |
| `prepare(manifest, start_key)` | 执行身份、execution_kind、适用时的 Profile/ModelBinding digest、输入包、授权及资源引用 | prepared identity；重复键返回同一准备记录 |
| `start(prepared_id, activation)` | 当前 fence、授权、限额；不传明文 secrets | 启动接受回执；重复调用不能创建第二进程 |
| `inspect(execution_ref)` | 完整 ExecutionRef | running/exited/unknown、进程身份、事件游标、可见消费；未知保持未知 |
| `events(execution_ref, after_seq)` | 完整 ExecutionRef 与游标 | 可重传、可能重复；若不能补齐返回显式 gap |
| `cancel(execution_ref, cancel_key)` | ExecutionRef、撤销版本与原因 | requested/confirmed/unknown；只报告实际可证实停止范围 |
| `respond_permission(execution_ref, request_ref, decision)` | 归属该执行的原生请求、fence 和当前授权的单次答复 | 仅动态审批已验收的 adapter 支持；过期/错误归属拒绝，不升级为整会话授权 |
| `collect(execution_ref)` | 已冻结或已停止的执行 | 配置候选或业务产物的 manifest/完整性信息；未停止写入不能伪装成冻结，收集不等于部署或验收成功 |

Resume 不是所有适配器都有的必需能力；支持时显式声明会话连续性和计费语义。不支持就创建新 ExecutionRef（Run 为新 Attempt，Designer 为新创作 execution），保留旧消费与累计上限。接口不把 CLI stdout 直接当作平台事件协议，适配器负责解析并保留原始来源。

启动清单含 execution_kind、版本、角色/步骤、参数、工具/网络、ExecutionRef、fence、输入/授权摘要与截止条件。Run Attempt 固定自身 TaskGraphRevision、决定/授权及部署/编译摘要，后续图不热改它；Designer 仍用独立身份。获权调度角色另有绑定 ExecutionRef/grant 的窄命令能力，不含管理权限。模型路径保留网关绑定和有限凭证，真实来源证据分 requested/accepted/provider_reported/inferred/unknown。

原生权限请求记录 `execution_ref / fence / native_request_id / turn_id / request_digest / requested_scope / authorization_revision / expiry`。旧 attempt_id 按上述兼容映射解析，不能借缺失 Run 绕过 Designer 归属。原授权内由程序裁决，扩大范围先形成具体授权变更，再答复仍有效请求；重查执行、请求与授权版本，取消后迟到批准无效。普通 Blocker resolve 不映射为 session-wide accept；不支持精确动态授予时拒绝→阻塞→新授权与新执行身份，Run 路径为新 Attempt，Designer 路径为新创作 execution 且不重置累计界限。Codex 原生作用域按固定 app-server schema 验收。[官方审批协议](https://learn.chatgpt.com/docs/app-server#approvals)

## 3. 资源接口

`evaluate_route(execution_snapshot, policy_snapshot, capacity_snapshot)` 为无副作用求解，返回候选、原因、排序和资源向量；输入可以是已实例化模型步骤或独立 Designer 创作调用。确定性步骤只评估所需本地资源。`admit(decision, expected_versions)` 在事务中预留并生成对应执行身份，冲突返回重新求解，不直接执行旧模拟结果。

`observe(pool_id, observation_key, payload)` 接受带来源、单位、窗口、时间、覆盖信息的额度事实；`record_usage(call_id_or_execution_ref, event_key, usage)` 幂等记录，旧 attempt 参数解析为 run_attempt；`settle(reservation_id, reconciliation)` 区分释放未来占用和确认已发生消费。

错误响应提供稳定 reason_code、受影响对象、观测时间、是否可重试、推荐动作。不会把服务商完整错误体中的凭据、prompt 或私人路径直接暴露给浏览器。

## 4. 用户 HTTP 接口

所有业务 API、SSE、日志流与产物下载都必须认证；只有不含敏感内容的健康检查和受限 bootstrap 例外。带 `Idempotency-Key` 的相同命令只产生一次业务效果。键绑定主体、操作、资源及规范化载荷摘要；同键不同载荷返回冲突，相同请求重试返回原已保存结果，即使第一次成功已改变对象 revision。新命令需要已有对象版本时使用 `If-Match`，过期返回 409/412 和当前 revision。长任务返回 202 及 command ID，浏览器通过快照和事件跟进。

| 操作 | 拟定路径 | 关键语义 |
|---|---|---|
| 登记/读取项目 | `POST /v1/projects`，`GET /v1/projects/{id}` | 校验本地路径与 remote；不因为登记而开始模型执行 |
| 创建 Run | `POST /v1/runs` | 保持现有入口并解析 project/conversation；实例化获选 Workflow 或保留原模型规划兼容路径，创建本身不批准执行 |
| 反馈与计划修订 | `POST /v1/runs/{id}/feedback` | 形成材料/变更提案；获权角色可在原范围内据此提交图修订，不自动扩权 |
| 角色调度决定 | 绑定 ExecutionRef/SchedulerGrant 的窄命令接口，见 [11](11-role-directed-scheduling.md) | 接受动态子图/依赖/角色来源/优先级/派发/封口；校验 base graph revision、身份、grant、义务后 CAS，范围内自动生效，资源不足保留排队 |
| 授权内委派/图读回 | grant 派生与 SchedulingDecision/TaskGraphRevision 查询 | 子授权只取父授权子集；多有效 grant 可重叠，经 CAS 处理竞争；丢回执先查询，不重复扩图 |
| 查看/确认计划 | `GET /v1/runs/{id}/plan`，现有 `POST /v1/runs/{id}/plan-approval` | 固定初始输入/Plan、定义/来源/产物政策与 SchedulerGrant；范围外变更另批，后续 accepted_under_grant 图版本不冒充用户新批准 |
| Designer 创作 | Project/Conversation 下的设计会话、消息与生成/修订命令，详见 [10](10-conversational-workflow-deployment.md) | 保存文字与候选；明确的创作请求在有限授权内调用模型，自动修复累计有界，普通保存不触发模型 |
| 配置和同源预览 | 设计会话下的配置 revision、校验/编译/preview 读写命令 | 真实文件、角色表/步骤图/diff 绑定同一 bundle/compiled digest；每次执行相关修改生成新 revision |
| 确认并部署 | 绑定 preview revision 的持久部署命令及查询 | 固定 project/conversation、包摘要、目标槽位 expected revision 和动作；pending 物化/加载/readback 后 CAS active；返回真实回执 |
| 确认、部署并运行 | 同一部署命令的明确复合动作 | 另固定具体 input/Plan/执行授权；一次用户确认，内部幂等关联 Run；重复或丢回执先查询，不重复启动 |
| 部署读回/回滚 | 按 deployment/slot ID 的状态与条件回滚命令 | 展示实际生效文件、图表和加载回执；回滚先核对目标 readiness，再条件切换，只影响新 Run |
| 查看运行 | `GET /v1/runs/{id}` | 一致快照＋snapshot_event_seq＋对象 revision |
| 暂停/恢复/取消 | `POST /v1/runs/{id}/commands` | command 类型明确；暂停和取消含义分别展示 |
| 重试/阻塞决定 | `POST /v1/tasks/{id}/commands`，`POST /v1/blockers/{id}/resolve` | 只接受当前允许的动作；服务端再检查条件 |
| 重评估规则 | `POST /v1/runs/{id}/reevaluate-policy` | 明确目标 revision 与影响预览；不能越过原授权 |
| 主 Commander 交接兼容入口 | `POST /v1/runs/{id}/commander-handoffs`，`POST /v1/commander-handoffs/{id}/approve` | 替换用户选定主 Commander 仍由用户决定；其他角色按有效 grant 委派/交接，不强制走此人工入口 |
| 路由与产物 | `GET /v1/attempts/{id}`，Artifact/Candidate 的受管 ID 读取入口 | 决策解释、报告/补丁/diff、证据和可观察身份；产物类型不由角色名推断 |
| Rulebook 编辑/发布 | `POST /v1/rulebooks/{id}/revisions`，`POST /v1/rulebooks/{id}/publish` | 校验、不可变版本；发布不自动迁移旧 Run |
| 规则模拟 | `POST /v1/routing/simulations` | 不消费配额；输入可用真实 Task 快照或模拟任务 |
| 资源目录/额度 | `GET /v1/profiles`，`GET /v1/quota-pools` | 状态、能力、共享关系、新鲜度与来源 |
| 接入/资格检查 | `POST /v1/profiles/{id}/qualify` | 区分不调用模型的检查与需要明确预算的实际探针 |
| 事件 | `GET /v1/events?run_id=...&after=...` | SSE，支持游标重连与缺口通知 |

默认来源的上游登录、OAuth/key 与刷新由独立 CLIProxyAPI 实例管理；Karajan 只在受限存储中保存网关客户端 secret_ref，普通 Rulebook、配置包、对话、日志与导出均不包含秘密。显式原生兼容通道可保留专用登录/凭据流程，但不能把其认证或资格静默移给网关。Designer 不能借配置文件请求读取上游秘密或访问网关管理面。

## 5. 事件协议与恢复

```json
{
  "schema_version": 1,
  "event_id": "evt-opaque-id",
  "seq": 184,
  "type": "attempt.route_selected",
  "occurred_at": "2026-09-05T08:30:00Z",
  "project_id": "project-opaque-id",
  "conversation_id": "conversation-opaque-id",
  "run_id": "run-opaque-id",
  "entity": { "kind": "attempt", "id": "attempt-3", "revision": 2 },
  "payload": { "route_decision_id": "route-3", "profile_revision_id": "profile-7-r2" }
}
```

以上为 Run 事件示意，新增 grant/决定/图 revision/ExpansionSet 成员与封口事件，保留触发事实、父版本、接受/拒绝与授权来源。创作/部署按实际身份关联而不伪造 Run。Hub 只读取已持久化事件，不按模型文本修改终态；日志/心跳不触发调度模型，资源释放机械唤醒同一待派发动作。

浏览器先读取快照及游标，再从该游标订阅事件，避免读取与订阅之间漏掉变化。重复事件按 seq/event_id 去重；游标超出保留期时返回 `snapshot_required` 并重新读取。事件已持久化才可发布，断线不丢业务状态。

## 6. 工作台信息架构

| 页面 | 用户主要任务 | 核心内容 |
|---|---|---|
| 项目分组侧栏 | 切换项目、展开会话、快捷新建 | 每个项目的独立会话；切换恢复草稿和上下文，不改变运行状态 |
| Commander Hub（默认主工作面） | 向 Designer 描述流程，讨论分工、确认部署与可选运行，跟进产物 | 设计会话与实际配置、同源图表/表格、修改差异、来源/权限/费用、部署目标及回执、Run 卡片和最终汇报 |
| Designer 图表与配置面 | 点选节点改职责/模型/依赖，用文字继续修订 | 同一版本的步骤图和角色/步骤表；YAML/manifest/diff 为高级面；冲突或修改使旧预览失效 |
| Tasks / Agents 详情 | 跟进动态分工与并行 | 引擎接受的当前图/决定依据、角色绑定、扩展集合及封口、排队原因、Attempt；数量来自实际任务，无内置人数默认值 |
| 任务详情抽屉 | 判断执行与路由是否合理 | 按步骤显示产物、Checks、Review、Logs、Deps，代码产物含 Diff；另有职责/权限、全部 Attempt、选模解释、预留/消费和换源原因 |
| 交付 | 检查目标产物与完成条件 | report/patch/pr 对应产物和证据；PR 另有当前 diff、独立审查、head SHA 与 CI 新鲜度 |
| 资源抽屉与设置 | 查看或管理模型服务 | 概览仅显示可用/受限/未知及已报告/估算费用；设置显示共享池、保留量、原币细节 |
| Rulebook | 编辑、模拟、发布规则 | 矩阵表、能力组、预算/换源策略、冲突提示、版本差异 |

以下是一个固定负载的代码 PR 详情样例，不规定默认任务分类、人数或所有流程都要 PR。实际子图来自有效调度决定，设计/部署/运行以 [工作台规格](../prd/commander-workbench.md) 为准。

```text
Karajan / 项目 / CSV 导出             [暂停派发] [取消]
阶段：实现中    完成条件：0/3         PR：尚未创建

Tasks / Agents          当前任务                    资源摘要
✓ 任务发现             导出逻辑 · T2               ChatGPT 短窗口  已报告
● 导出逻辑             自定义实现角色 / 显式来源     Claude 周窗口    未知
● 边界验证             并行运行 · Attempt 1          Go 月窗口        估算
○ 集成验证             [为什么选它] [产物]          现金：已记账＋待核对

需要处理：Go 配额耗尽；改派先暂停并核对旧 Attempt，未获 opt-in 时等待会话批准
```

UI 只展示有真实动作的阻塞卡，例如补齐配置、解决版本冲突、核对部署、等待可信重置、补齐授权或选择合格独立审查配置。显式绑定优先于 Rulebook；用户 opt-in 后才允许在冻结集合内自动替代。增加开销、权限、来源或数据去向时显示具体差异，不以泛化的“允许继续”扩大授权。部署状态与 Run 状态分别显示，Agent 回复不能点亮部署成功。

不显示缺乏依据的整体完成百分比或精确剩余任务数。使用任务完成数、当前阶段、可证实的配额窗口及置信标记。模型自报与实际启动接受的配置分别可查看；共享池、保留量、窗口和原币细节不占任务详情主面，收纳到资源抽屉/设置。

## 7. 本地访问与操作安全

后端默认仅绑定 loopback；Host 和 Origin 采用允许列表，浏览器会话使用随机本地凭证及 HttpOnly/SameSite cookie，写操作校验 CSRF。首次访问采用本地一次性 bootstrap 交换，凭证不放在日志或可长期复用的 URL。仅监听 localhost 不能替代认证。

Web 不执行任意用户/模型提供的 shell，不提供任意宿主路径读写；下载按已登记 artifact ID 解析。配置编辑通过受限服务按 bundle/file ID 写入真实受管文件，路径、schema 和内容均校验，不开放任意脚本/import。Markdown/日志/diff 作为不可信内容渲染，禁止执行 HTML 脚本。UI 不直接修改业务表、部署 active 指针或 RunnerHost 状态文件。

执行沙箱不持有通用 Web 控制凭证、数据库或部署/交付 IPC。调度角色仅通过 ExecutionRef/grant 绑定的窄接口提交获准动作；调用授权层不暴露网关管理面。远程访问/多用户需独立认证隔离，不通过改监听地址隐式开启。

## 8. 资源配置契约

历史 [resources.v1](examples/resources.v1.json) 保留原直接通道和固定样例，不能直接启动或作为 r8 完整 schema。新 Run 不继承其产品人数默认值；用户明确批准的数量政策仍尊重，目标目录保留下列来源/资源身份。

| 对象 | 必需结构及启用校验 |
|---|---|
| Account | id、provider_id、计费/共享主体和可信来源证据、state；默认上游凭据在网关，不进 Karajan 配置 |
| GatewayConnection revision | endpoint/protocol、数据面客户端 secret_ref、网关版本与配置/请求变换摘要、健康/资格证据；管理面与工具隔离 |
| ModelBinding revision | connection、公开 alias、允许的真实 provider/model/account/billing/pool 映射、严格绑定/已批准池语义及来源回执要求 |
| Channel | 来源/账户/协议与 billing_path；直接 endpoint 或原生 runtime 路径只在显式兼容模式使用，不隐式追加现金 |
| Profile revision | id/revision、ModelBinding 或显式兼容 channel、runtime kind/version、适用参数、能力证据、池关联与隔离；匹配执行契约，不封闭业务角色 |
| Quota Pool | id、account_id、共享范围、unit、window_kind/identity、limit、观察来源；值缺失可存 draft，不能假装服务容量已知 |
| CapacityPolicy revision | account_id、pool_ref、安全余量、可信消费资格的保留量、未知模式；不按显示角色名授权，保护量不得超池上限 |
| Conservative mode | enabled、max_local_active_attempts、max_attempt_duration_seconds、observation_max_age_seconds、cooldown_seconds；启用前有限值全部明确 |
| Budget | id、scope、按币种的 limits、cash_enforcement、Run 或独立创作会话总调用/修复/时间上限；null 表示未配置，不是无限 |
| Price/FX snapshot | id/revision、来源、币种、适用计费项/换算值、observed_at、valid_until；原币上界与展示换算分别使用 |

建议资源写入面包括账户、网关连接、模型绑定、Profile revision、共享池、CapacityPolicy、预算与价格快照；既有接口兼容保留，新增 DTO/路径须单独定稿和验收。共享策略 activate 与人工额度 adjustment 都保存理由、前后快照和当前版本。所有命令遵循认证、幂等与 If-Match；管理资源不自动部署 Workflow 或批准 Run。

也可用 `POST /v1/resource-imports/preview` 对配置做无副作用检查，返回版本 diff、空引用、单位/共享关系错误及活跃 Run 影响；`POST /v1/resource-imports/{id}/apply` 只应用仍符合 expected revisions 的具体预览。新 Profile revision 不进入旧 Run 授权；共享 CapacityPolicy 只改变全局新准入，不删除旧预留；增加预算/现金路径需显式面向相应 Run 的授权操作。
