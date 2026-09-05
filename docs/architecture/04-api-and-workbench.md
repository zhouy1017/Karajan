# 接口与 Web 工作台

接口在 v1 内保持供应商无关。本文是拟实现协议；方法名和路径是设计，不代表仓库已有这些接口。

## 1. 任务输入与模型输出

```json
{
  "task_id": "task-export-api",
  "revision": 1,
  "role": "worker",
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
  "output_kind": "task_candidate"
}
```

模型可以提交 `PlanProposal / TaskResult / ReviewResult / ChangeRequest`。平台检查 schema、长度、引用、范围与身份；未知字段不获得权限。模型输出不允许直接包含可执行授权。

TaskResult 包含状态、产物引用、变更摘要、验收尝试、未决事项和交回原因。声明的文件、测试和模型身份全部视为待核验信息。ReviewResult 包含 findings 与 `pass / changes_requested / inconclusive`，由 gate 转换成业务结论。

## 2. 执行管理接口

| 方法 | 输入 | 返回与幂等语义 |
|---|---|---|
| `describe(runtime_version)` | 固定执行器/环境 | 能力及其证据，不启动模型 |
| `prepare(manifest, start_key)` | Attempt、Profile digest、输入包、工作区授权、资源引用 | prepared identity；重复键返回同一准备记录 |
| `start(prepared_id, activation)` | 当前 fence、授权、限额；不传明文 secrets | 启动接受回执；重复调用不能创建第二进程 |
| `inspect(attempt_id)` | 平台 Attempt ID | running/exited/unknown、进程身份、事件游标、可见消费；未知保持未知 |
| `events(attempt_id, after_seq)` | 从上次游标继续 | 可重传、可能重复；若不能补齐返回显式 gap |
| `cancel(attempt_id, cancel_key)` | 撤销版本与原因 | requested/confirmed/unknown；只报告实际可证实停止范围 |
| `respond_permission(request_ref, decision)` | 固定原生请求、fence 和当前授权的单次答复 | 仅动态审批已验收的 adapter 支持；过期请求拒绝，不升级为整会话授权 |
| `collect(attempt_id)` | 已冻结或已停止的执行 | 产物 manifest 与完整性信息；未停止写入不能伪装成冻结 |

Resume 不是所有适配器都有的必需能力；支持时显式声明其会话连续性和计费语义。不支持就新建 Attempt。接口不把 CLI 的 stdout 直接当作平台事件协议，适配器负责解析并保留原始来源。

启动清单包含执行器版本、native settings、auth mode/secret_ref、允许工具与网络、project/run/task/attempt、fence、输入与授权 hash、预算模式、截止条件。`requested / accepted / provider_reported / inferred / unknown` 分别保存；不能使用一个 `actual_model` 字段混合所有证据。

原生权限请求记录 `attempt_id / fence / native_request_id / turn_id / request_digest / requested_scope / authorization_revision / expiry`。原授权内的请求由程序裁决；扩大范围先形成具体授权变更，再答复仍有效的请求。答复时重查 Attempt、请求与授权版本，取消后迟到批准无效。不得把普通 Blocker 的 resolve 映射成 session-wide accept；不支持精确动态授予的 adapter 采用拒绝当前请求→阻塞→新授权和新 Attempt。Codex 的原生审批作用域需按固定 app-server schema 验收。[官方审批协议](https://learn.chatgpt.com/docs/app-server#approvals)

## 3. 资源接口

`evaluate_route(task_snapshot, policy_snapshot, capacity_snapshot)` 为无副作用求解，返回候选、原因、排序和所需资源向量。`admit(decision, expected_versions)` 在事务中预留并生成 attempt；冲突返回重新求解，不把旧模拟结果直接执行。

`observe(pool_id, observation_key, payload)` 接受带来源、单位、窗口、时间、覆盖信息的额度事实；`record_usage(call_or_attempt_id, event_key, usage)` 幂等记录；`settle(reservation_id, reconciliation)` 区分释放未来占用和确认已发生消费。

错误响应提供稳定 reason_code、受影响对象、观测时间、是否可重试、推荐动作。不会把服务商完整错误体中的凭据、prompt 或私人路径直接暴露给浏览器。

## 4. 用户 HTTP 接口

所有业务 API、SSE、日志流与产物下载都必须认证；只有不含敏感内容的健康检查和受限 bootstrap 例外。带 `Idempotency-Key` 的相同命令只产生一次业务效果。键绑定主体、操作、资源及规范化载荷摘要；同键不同载荷返回冲突，相同请求重试返回原已保存结果，即使第一次成功已改变对象 revision。新命令需要已有对象版本时使用 `If-Match`，过期返回 409/412 和当前 revision。长任务返回 202 及 command ID，浏览器通过快照和事件跟进。

| 操作 | 拟定路径 | 关键语义 |
|---|---|---|
| 登记/读取项目 | `POST /v1/projects`，`GET /v1/projects/{id}` | 校验本地路径与 remote；不因为登记而开始模型执行 |
| 提交需求/开始规划 | `POST /v1/projects/{id}/runs` | 绑定规划策略和已配置规划预算；可先存草稿 |
| 反馈与计划修订 | `POST /v1/runs/{id}/feedback` | 生成新计划提案，不改已批准版本 |
| 查看/确认计划 | `GET /v1/runs/{id}/plan`，`POST /v1/runs/{id}/approve` | 确认包含 plan/authorization hash；过期版本拒绝 |
| 查看运行 | `GET /v1/runs/{id}` | 一致快照＋snapshot_event_seq＋对象 revision |
| 暂停/恢复/取消 | `POST /v1/runs/{id}/commands` | command 类型明确；暂停和取消含义分别展示 |
| 重试/阻塞决定 | `POST /v1/tasks/{id}/commands`，`POST /v1/blockers/{id}/resolve` | 只接受当前允许的动作；服务端再检查条件 |
| 重评估规则 | `POST /v1/runs/{id}/reevaluate-policy` | 明确目标 revision 与影响预览；不能越过原授权 |
| 主 Commander 交接 | `POST /v1/runs/{id}/commander-handoffs`，`POST /v1/commander-handoffs/{id}/approve` | 先准备检查点与候选；每次由用户决定；过期 term/材料需更新方案 |
| 路由与产物 | `GET /v1/attempts/{id}`，`GET /v1/candidates/{id}` | 决策解释、diff、证据链接和可观察身份 |
| Rulebook 编辑/发布 | `POST /v1/rulebooks/{id}/revisions`，`POST /v1/rulebooks/{id}/publish` | 校验、不可变版本；发布不自动迁移旧 Run |
| 规则模拟 | `POST /v1/routing/simulations` | 不消费配额；输入可用真实 Task 快照或模拟任务 |
| 资源目录/额度 | `GET /v1/profiles`，`GET /v1/quota-pools` | 状态、能力、共享关系、新鲜度与来源 |
| 接入/资格检查 | `POST /v1/profiles/{id}/qualify` | 区分不调用模型的检查与需要明确预算的实际探针 |
| 事件 | `GET /v1/events?run_id=...&after=...` | SSE，支持游标重连与缺口通知 |

凭据登录/登记走本地专用流程，只将 secret_ref 写入业务数据。官方订阅打开官方支持的登录方式；不让用户把 OAuth token 粘进普通 Rulebook 字段。第三方 API key 保存于 OS secret store 或独立受限凭据存储，日志与导出均不包含实际值。

## 5. 事件协议与恢复

```json
{
  "schema_version": 1,
  "event_id": "evt-opaque-id",
  "seq": 184,
  "type": "attempt.route_selected",
  "occurred_at": "2026-09-05T08:30:00Z",
  "run_id": "run-opaque-id",
  "entity": { "kind": "attempt", "id": "attempt-3", "revision": 2 },
  "payload": { "route_decision_id": "route-3", "profile_revision_id": "profile-7-r2" }
}
```

关键事件包括 plan.proposed/approved、attempt.reserved/started/result/exit_observed、quota.updated、blocker.opened/resolved、candidate.captured、evidence.recorded、delivery.reconciled。日志片段是单独的限速流，不能阻塞或替代状态事件。

浏览器先读取快照及游标，再从该游标订阅事件，避免读取与订阅之间漏掉变化。重复事件按 seq/event_id 去重；游标超出保留期时返回 `snapshot_required` 并重新读取。事件已持久化才可发布，断线不丢业务状态。

## 6. 工作台信息架构

| 页面 | 用户主要任务 | 核心内容 |
|---|---|---|
| 项目首页 | 选择仓库、提交需求、继续工作 | 活跃 Run、可处理阻塞、近期 PR、账户异常 |
| 需求与计划 | 与 Commander 讨论并确认 | 目标/验收、任务图、接口与路径、来源集合、费用语义、到 PR 权限 |
| Run 详情 | 理解当前进度并干预 | DAG/任务列表、当前角色与配置、日志/产物、阻塞卡、暂停/取消 |
| 任务详情抽屉 | 判断执行与路由是否合理 | 简报、全部 Attempt、选模解释、配额预留、消费、换源原因 |
| 交付 | 检查最终结果 | 当前 diff、必需证据、review findings、PR、head SHA、CI 新鲜度 |
| 资源 | 管理自己的模型服务 | 账户/渠道/Profile、多个额度窗口、已知/估算/未知、Commander 保护目标 |
| Rulebook | 编辑、模拟、发布规则 | 矩阵表、能力组、预算/换源策略、冲突提示、版本差异 |

运行页示意：

```text
Karajan / 项目 / CSV 导出             [暂停派发] [取消]
阶段：实现中    完成条件：0/3         PR：尚未创建

任务与依赖             当前任务                    资源摘要
✓ 接口设计             后端导出 · T2               ChatGPT 短窗口  已报告
● 后端导出             Worker / DeepSeek 官方       Claude 周窗口    未知
● 前端入口             运行中 · Attempt 1           Go 月窗口        估算
○ 集成验证             [为什么选它] [产物]          现金：已记账＋待核对

需要处理：Go 配额耗尽；前端已按规则换源，原尝试消耗待核对
```

UI 只展示有真实动作的阻塞卡，例如等待可信重置、补齐授权范围、修复登录、选择合格 Reviewer。自动范围内的合法换源展示结果，不反复打断用户确认。增加开销或数据去向时显示具体差异，而不是一个泛化的“允许继续”。

不显示缺乏依据的整体完成百分比或精确剩余任务数。使用任务完成数、当前阶段、可证实的配额窗口及置信标记。模型自报与实际启动接受的配置分别可查看。

## 7. 本地访问与操作安全

后端默认仅绑定 loopback；Host 和 Origin 采用允许列表，浏览器会话使用随机本地凭证及 HttpOnly/SameSite cookie，写操作校验 CSRF。首次访问采用本地一次性 bootstrap 交换，凭证不放在日志或可长期复用的 URL。仅监听 localhost 不能替代认证。

Web 不执行任意用户/模型提供的 shell，不直接读写文件路径；下载文件按已登记 artifact ID 解析。Markdown/日志/diff 作为不可信内容渲染，禁止执行 HTML 脚本。UI 不提供直接修改业务表或 RunnerHost 状态文件的入口。

执行沙箱不能访问 Web 控制凭证、数据库和交付 IPC。Broker 是另外一个窄接口，只允许受限推理调用，不提供管理功能。远程访问/多用户需要另行设计网络认证和租户隔离，不通过把监听地址改成 `0.0.0.0` 隐式开启。

## 8. 资源配置契约

除 Rulebook 外，资源目录采用独立 `karajan.resources.v1` 配置。可编辑模板见 [资源示例](examples/resources.v1.json)；它只描述对象结构，模型/凭据/金额为空，所有 Profile 为 draft，不能直接启动。

| 对象 | 必需结构及启用校验 |
|---|---|
| Account | id、provider_id、auth_kind、secret_ref、state；共享主体明确，密钥内容不进配置 |
| Channel | id、account_id、protocol、endpoint 或官方 runtime 路径、billing_path；不得隐式追加现金 |
| Profile revision | id/revision、channel_id、model_id/family、runtime kind/version、native settings、能力证据、池关联与所需隔离；未知能力不能标 enabled |
| Quota Pool | id、account_id、共享范围、unit、window_kind/identity、limit、观察来源；值缺失可存 draft，不能假装服务容量已知 |
| CapacityPolicy revision | account_id、pool_ref、安全余量、角色保留量、未知模式；金额均含 unit，保护量不得超池上限 |
| Conservative mode | enabled、max_local_active_attempts、max_attempt_duration_seconds、observation_max_age_seconds、cooldown_seconds；启用前有限值全部明确 |
| Budget | id、scope、按币种的 limits、cash_enforcement、Run 总次数/时间上限；null 表示未配置，不是无限 |
| Price/FX snapshot | id/revision、来源、币种、适用计费项/换算值、observed_at、valid_until；原币上界与展示换算分别使用 |

新增写入命令：`POST /v1/accounts`、`POST /v1/channels`、`POST /v1/profiles/{id}/revisions`、`POST /v1/quota-pools`、`POST /v1/accounts/{id}/capacity-policy/revisions`、`POST /v1/budgets`、`POST /v1/price-snapshots`。发布共享策略使用 `POST /v1/accounts/{id}/capacity-policy/activate`；人工额度校准使用 `POST /v1/quota-pools/{id}/adjustments` 并保存理由和前后快照。所有命令遵循认证、幂等与 If-Match 规则。

也可用 `POST /v1/resource-imports/preview` 对配置做无副作用检查，返回版本 diff、空引用、单位/共享关系错误及活跃 Run 影响；`POST /v1/resource-imports/{id}/apply` 只应用仍符合 expected revisions 的具体预览。新 Profile revision 不进入旧 Run 授权；共享 CapacityPolicy 只改变全局新准入，不删除旧预留；增加预算/现金路径需显式面向相应 Run 的授权操作。
