# R8-P1-04 授权角色调度、动态任务图与完整待执行队列

实现日期：2026-09-15。本记录对应 [#178](https://github.com/zhouy1017/Karajan/issues/178)，父票 [#174](https://github.com/zhouy1017/Karajan/issues/174)。设计依据 [角色主导的任务拆分与调度](../architecture/11-role-directed-scheduling.md)、[可定制角色与 Workflow](../architecture/09-configurable-workflows.md) 与 [对话式 Workflow](../architecture/10-conversational-workflow-deployment.md)；上游切片见 [R8-P1-01](r8-phase1-gateway.md)、[R8-P1-02](r8-phase1-workflows.md) 与 [R8-P1-03](r8-phase1-deployment.md)。

本切片交付**受认证的角色调度控制面**：从 active 且本进程重新加载的部署创建持久 Workflow 运行、受认证的管理授权与窄协议凭证、版本化不可变任务图与决定回执、扩展集合与完成义务、可信资源观察与准入、以及执行消费者的写区互斥领取。它**不**调用模型、不启动进程、不执行任何业务步骤；`claim` 是工作交接而不是物理启动或完成，消费者的自述结果作为报告记录，只有明确的可信核对才会改变控制面状态。

## 1. 模块与接口

| 路径 | 作用 |
|---|---|
| [`karajan/scheduling/store.py`](../../backend/karajan/scheduling/store.py) | `SchedulingStore`：运行冻结、授权与凭证签发、图修订 CAS 与原子提交、冲突回执、资源准入与占用、领取与报告 |
| [`karajan/scheduling/grants.py`](../../backend/karajan/scheduling/grants.py) | `SchedulerGrant` 校验与逐维严格子集/预算共享/委派边界 |
| [`karajan/scheduling/graph.py`](../../backend/karajan/scheduling/graph.py) | 任务与图修订文档、依赖推断与环检测、义务覆盖、扩展集合封口 |
| [`karajan/scheduling/credentials.py`](../../backend/karajan/scheduling/credentials.py) | 三种凭证类型、`Principal`、能力集合与运行/授权绑定 |
| [`karajan/scheduling/values.py`](../../backend/karajan/scheduling/values.py) | 标识符/引用/路径作用域校验、写入区同一性判定 |
| [`karajan/web/scheduling.py`](../../backend/karajan/web/scheduling.py) | 管理路由与 `/v1/scheduling/protocol/` 协议路由及其各自认证 |

存储与项目库同库（`projects.sqlite`），沿用既有项目归属校验、会话中间件与 `BEGIN IMMEDIATE`。管理命令的 idempotency 账本（`workflow_run_keys`）按项目库的既有模式新建；协议侧另有其**作用域明确**的持久账本：`scheduling_credentials`（凭证摘要、期限与撤销）、`credential_issuances`（签发命令身份，不含 raw token）、`scheduling_decisions`（已接受决定回执）与 `scheduling_decision_rejections`（被拒绝决定回执）。这些是新的表，但**不是**第二套认证：管理路径仍复用同一会话/归属边界，协议路径只认自己签发的 bearer 能力，两者不互相授权。定义消费仍走 #177 的公共 API（`DeploymentStore.accept`）。

### 1.1 两种认证边界

| 家族 | 前缀 | 认证 | 可做什么 |
|---|---|---|---|
| 管理 | `/v1/projects/...`、`/v1/scheduling/resource-*` | 会话 + Origin + CSRF | 建 Run、签发/委派/撤销 grant、签发凭证、发布可信资源观察、可信核对 |
| 协议 | `/v1/scheduling/protocol/...` | `Authorization: Bearer <token>` | 角色提交决定；消费者读队列、领取、报告 |

协议家族是唯一不走会话中间件的路径前缀，且其中每条路由都认证**自己**签发的凭证能力。会话 cookie 出现在协议路由上不授予任何权限：协议依赖只读 bearer，因此浏览器会话不是第二条进入路径。凭证只有 sha256 落库，raw token 仅在签发响应中出现一次；列表与回执永不携带 token。

### 1.2 决定提交的原子性

一个决定先被完整编译（动作应用到内存副本、检查冻结定义/环/义务），再在**一个短事务**中写入图修订、决定回执与完整队列。因此「先合法后非法」的批次不会留下部分图或部分队列；冲突时用一个 `UPDATE ... WHERE revision=?` 的 CAS 决定唯一胜者。

被拒绝的决定在**回滚之后**用独立短事务写入 `scheduling_decision_rejections`：图保持原样，而「这条命令被拒、原因是什么、当时当前修订是多少」成为可读回的证据，而不是只存在于失败方记忆里的 409。同键重放以原回执回答。

### 1.3 三种状态的分离

| 事实 | 含义 | 谁能改变 |
|---|---|---|
| 控制面任务状态 | 排队/等待/已领取/完成/失败/取消/被替代/未知 | 引擎按已接受的决定与**可信**核对 |
| `reported_state` | 消费者对自己执行的自述 | 消费者，作为观察记录 |
| `physical_execution` | 引擎是否验证过物理执行 | 永不；本切片固定 `not_started` |

消费者的 completed 报告**不会**终止任务、满足依赖或释放容量：报告被记录，任务保持 `claimed`。只有带 `reconcile` 能力的管理会话可以核对，核对后释放该 claim 的占用并让等待任务成为 ready。核对之后到达的迟到报告只是追加观察，不会重新打开已核对的任务或恢复其占用。

## 2. 逐项验收

| 原验收条件 | 实现与行为证据 | 层级 |
|---|---|---|
| AC1 从 active 且本进程加载就绪的部署、具体输入与显式授权创建持久运行；冻结摘要；拒绝未就绪/跨项目/旧批准；重传不多建 | `create_run` 经 `DeploymentStore.accept` 取得本进程真实重读的句柄；`test_a_run_freezes_the_loaded_deployment_inputs_and_authorization`（冻结 deployment/slot/bundle/compiled 摘要、acquisition 身份、输入与初始授权摘要）、`test_an_unloaded_or_wrong_deployment_is_refused`（409 `SCHEDULING_RUN_SOURCE_STALE`）、`test_the_same_creation_key_returns_the_original_run`（200、同 run，且冻结内容与初始授权按解析后的 JSON 结构相等）、`test_a_changed_payload_on_the_same_key_conflicts`（409）、`test_a_run_cannot_be_read_through_another_project`（404）、`test_the_run_is_readable_after_a_restart` | C/P |
| AC1 输入必须满足冻结声明与契约 | `_validate_run_inputs`：缺失已声明输入 422 `SCHEDULING_RUN_INPUT_MISSING`、未声明输入 422 `SCHEDULING_RUN_INPUT_UNEXPECTED`、`text@1` 形状不符 422 `SCHEDULING_RUN_INPUT_INVALID`；用真实 `requirement.option_a/b` 文本输入覆盖 | C/P |
| AC1 命令身份稳定、新命令不撞旧 run | run id 由 `(project, conversation, command_key)` 派生；重放**先于**新鲜来源检查（早期实现顺序相反，导致 active 更换后原命令被误报 stale）；`test_the_frozen_source_survives_a_later_active_replacement`（active 更换后 run 冻结不变） | C/P |
| AC1 与旧 Run schema 兼容边界 | 既有 `/v1/runs` 路由与 `RunPlanner` 未改动；`test_a_legacy_run_route_still_works` 断言旧路由仍以其原语义作答，新资源不遮蔽它 | C |
| AC2 grant 记录主体/范围/动作/固定角色模型工具路径/父授权/资源政策；子集；默认不隐含转授权；有界深度与到期；父撤销传播；共享预算 | `validate_grant_payload` + `require_subset` + `require_delegation_depth`；`test_a_child_cannot_widen_its_parent`（路径越界 422）、`test_a_child_may_act_only_with_its_parents_delegable_actions`（`delegation.actions` 之外的 `bind_role` 422 `SCHEDULING_GRANT_NOT_A_SUBSET`）、`test_delegation_is_disabled_by_default`、`test_a_path_scope_is_not_widened_by_a_string_prefix`（`src/ab/**`、`src/a/../outside/**` 均拒）、`test_sibling_grants_share_one_budget_and_occupancy`（同池容量 1，第二条 409 `SCHEDULING_CAPACITY_BUSY`）、`test_revoking_a_parent_stops_a_descendants_late_decision` | C/P |
| AC2 管理授权入口创建真实角色实例与窄凭证；测试经该入口一路提交 | `issue_grant`/`issue_credential` 为管理命令；`test_an_authenticated_user_issues_a_grant_and_a_narrow_credential`、`test_a_listed_credential_never_carries_its_token`、`test_repeating_an_issuance_returns_the_same_credential`（重放返回同一身份且不复现 token）、`test_a_changed_issuance_under_the_same_key_conflicts` | C/P |
| AC2 自报身份无效；管理接口与角色命令接口分开 | `test_a_body_cannot_authenticate_itself`（`user`/`role` 字段 422）、`test_a_protocol_request_needs_a_bearer_credential`、`test_a_role_credential_cannot_claim_or_reach_management`（403 `SCHEDULING_CREDENTIAL_SCOPE_INSUFFICIENT`）、`test_a_consumer_credential_cannot_submit_a_decision`、`test_a_credential_for_another_run_is_refused`、`test_an_unknown_bearer_token_is_refused` | C/P |
| AC2 grant 不能发明冻结部署之外的角色/种类/交付目标 | `_require_frozen_definitions`；`test_a_grant_cannot_invent_a_role_outside_the_frozen_deployment`（422 `SCHEDULING_GRANT_ROLE_UNRESOLVED` 且未写入）、`test_a_grant_cannot_raise_the_delivery_target`（report 部署不能授予 pr 目标） | C/P |
| AC3 有效决定经身份/grant/动作/图版本/输入输出/无环/义务校验后在同一短事务提交图修订、回执与完整队列 | `_plan_decision` + `_commit_decision`；`test_a_decision_commits_a_graph_revision_and_a_receipt`、`test_an_invalid_second_operation_leaves_the_graph_untouched`（404 且图修订 0、任务为空）、`test_a_decision_made_against_a_stale_revision_conflicts`、`test_a_dependency_cycle_is_refused`、`test_a_decision_made_against_different_inputs_is_refused`、`test_an_action_the_grant_did_not_authorise_is_refused` | C/P |
| AC3 幂等键重传不扩出重复节点、同键异载荷拒绝 | `test_the_same_decision_key_replays_and_a_changed_payload_conflicts`（重放 200，返回与首次**结构相等**的回执 JSON；异载荷 409 `SCHEDULING_IDEMPOTENCY_CONFLICT`） | C/P |
| AC3 输出契约按所选执行种类校验 | `_Definitions.require_contract_for_kind`：`test_an_output_contract_the_kind_cannot_produce_is_refused`（`artifact_aggregate@1` + `repair-patch@1` 422 `SCHEDULING_OUTPUT_CONTRACT_INCOMPATIBLE` 且无图变更；兼容的 `aggregated-report@1` 仍 201） | C/P |
| AC3 输入引用按声明与执行种类校验，不按参数名授权 | `_Definitions.validate_inputs` + `_references_of`：参数名来自执行种类，引用来自 grant 许可集合；`test_a_declared_reference_is_accepted`（正向）、`test_an_unknown_reference_is_refused`、`test_an_output_reference_to_a_nonexistent_producer_is_refused` | C/P |
| AC4 两个有效 grant 可明确覆盖重叠范围；CAS 防覆盖并保留冲突回执 | `test_two_overlapping_grants_decide_concurrently_with_one_winner`：两个各自持凭证的 grant 从**两个线程**在同一 `expected_graph_revision` 上真正并发提交，恰好 201/409 各一，图只有 1 个新修订与 1 个任务，`scheduling_decision_rejections` 中留有失败方回执，双方重放各自可解释；`test_a_refused_decision_leaves_a_durable_conflict_receipt`、`test_a_refused_decision_does_not_commit_partial_work` | P |
| AC4 冻结已领取工作项；后续图形成新修订；保留被替代/失败/消费与既有义务 | `test_a_claimed_snapshot_is_unchanged_by_a_later_revision`（领取回执按解析后的 JSON 结构相等，含身份与输入绑定）；被领取任务的另一个消费者报告 `SCHEDULING_TASK_NOT_OWNED` 断言为**精确 403**，输出契约不匹配断言为**精确 422**、`test_an_obligation_survives_every_supported_edit`（`set_priority`/`bind_role`/`request_dispatch`/`set_dependencies` 与失败报告均不改写 `required_outcomes`；被 hold 的任务确实不可领取，解除后才可）、`test_an_optional_member_still_carries_its_outcome`、`test_an_uncovered_obligation_is_refused_rather_than_committed` | C/P |
| AC4 替代保留历史并覆盖义务 | `_require_coverage`：支持的映射形状是「被替代任务欠下的义务名 → 后继**实际承诺**的产物名」的字符串对（如 `{"config-defect-triage": "config-defect-triage"}`）。键必须是被替代任务真正欠下的义务，值必须是后继真正承诺的产物；只要前驱欠下义务，映射就是必需的，缺失、无关键、未映射的义务与不存在的目标值都 422 `SCHEDULING_OBLIGATION_COVERAGE_INVALID` 且不改图。`test_a_replacement_supersedes_the_task_it_replaces` 覆盖 `nodes_superseded` 记录、后继 `supersedes` 指向前驱、前驱保留正文与历史、合法映射使开放集合可封口，以及上述四种拒绝；`_required_coverage` 的值为此前只能靠产物流通、却从未被读取的字段提供了真实校验 | C/P |
| AC4 一个决定批次内的中间 pinned 版本可读回 | `_archive_version` + `scheduling_task_versions`；`test_a_version_pinned_within_one_decision_is_readable`（批次内 pin 的中间版本按 revision+digest 200 读回） | C/P |
| AC5 批准来源区分初始批准与 grant 接受 | 每个已接受修订记录 `accepted_under_grant` 与 `authorizing_grant_id`/`authorizing_grant_depth`，Run 自身的初始批准单列；`test_a_decision_commits_a_graph_revision_and_a_receipt` 断言该组字段 | C/P |
| AC4 已封口成员版本不可被后续修订静默改写 | `scheduling_task_versions` 归档每个任务版本；`_refresh_member` 只更新**未封口**集合；`test_a_sealed_member_version_is_readable_after_the_task_changes`（封口后编辑现有成员，sealed 记录不变，按 pinned revision+digest 读回原正文，错 digest 被拒）、`test_a_sealed_version_is_readable_after_a_restart`（在同一解释器内新建 `create_app` 重开同一 SQLite 与文件；**新 OS 进程**的队列/领取恢复由远端 root 探针单独证明，不由本用例声称） | C/P |
| AC5 open 时不提前汇合；seal 固定成员 revision 并校验义务 | `_join_blocked` 区分「成员的集合仍 open」与「Join 引用的集合仍 open」；`test_an_open_expansion_holds_its_join_and_its_members`、`test_sealing_releases_the_members_and_the_join`、`test_a_seal_cannot_name_a_member_that_does_not_exist`、`test_an_obligation_survives_every_supported_edit` 中的未覆盖义务拒绝 | C/P |
| AC5 1/3/7 及超过 100 项任务完整持久/分页读回，无默认 Agent/任务总数 | `test_tasks_are_persisted_and_paged_without_loss`（103 项、37/页、无重复无遗漏）、`test_a_large_task_set_is_persisted_and_paged_completely`（103 项全集相等，且第 100 项之后的任务仍可按精确身份取回）；分页上限只是单次响应字节/传输限制 | C/P |
| AC5 观察的度量语义被正确解读 | `_admit` 按 `metric` 解释读数：`remaining` 是可用量本身，`used` 需配合其 `limit` 推导 `limit-used`（耗尽为 0），缺 limit 的 `used` 保持 unknown；`test_a_used_observation_derives_its_remaining_from_its_limit`（used1/limit1 拒绝、used0/limit1 放行、缺 limit unknown、remaining 路径不变） | C/P |
| AC5 受信 unknown 不阻挡后续终端释放 | 只有**终端**核对才置释放守卫；`test_a_trusted_unknown_then_a_terminal_reconciliation_releases_once` 覆盖完整序列：受信 `unknown` 保留占用、等待任务仍 409、随后终端核对释放**一次**、等待任务 201、迟到未核实报告不能重开或恢复占用 | C/P |
| AC5 资源等待由显式政策/可信观察判定，缺容量保留任务，释放后 ready；唤醒不调用模型 | `_admit` 在池无政策或无观察时返回 `SCHEDULING_CAPACITY_UNKNOWN`（未知不等于无限）；`test_sibling_grants_share_one_budget_and_occupancy`、`test_a_late_untrusted_report_cannot_undo_a_trusted_reconciliation`（容量 1 时第二任务 409，核对释放后可领取，全程 `model_calls == 0`） | C/P |
| AC6 可信执行消费者读 ready 并幂等领取；记录 task/graph/grant 与写区；同区互斥、独立区可领取 | `claim` 在同一事务内重查任务/依赖/grant 链/写区/容量；写区占用从消耗账本读取，因此图节点被替代后其未结算 claim 仍持有该区（`test_a_superseded_claim_still_holds_its_write_zone`）；`test_overlapping_write_zones_are_one_zone_and_independent_ones_are_not`（`src/config/zone` 与 `src/config/ZONE` 同一目录、`src/config/zone/sub` 在其内，三者互斥；`src/config/other` 独立可领取）、`test_the_queue_reports_waiting_reasons_without_dropping_tasks`、`test_a_repeated_claim_returns_the_same_work`（重复/丢失响应返回同一 claim）、`test_a_claim_for_another_task_under_a_used_key_conflicts` | C/P |
| AC6 不能把 claim 或自述标成物理 started/completed；未知执行不自动释放占用 | `physical_execution` 固定 `not_started`、`verified_by_engine` false；`test_a_claim_is_a_handover_and_not_an_execution`、`test_a_late_untrusted_report_cannot_undo_a_trusted_reconciliation`（未核对 completed 保持 `claimed` 且占用 1）、`test_another_consumer_cannot_report_on_work_it_does_not_hold`（**精确 403** `SCHEDULING_TASK_NOT_OWNED`）、`test_a_consumer_cannot_reconcile_its_own_report`、`test_a_consumer_cannot_publish_a_trusted_resource_observation` | C/P |
| AC7 真实 SQLite/HTTP/文件集成测试；Ruff/mypy/快速门通过 | 全部用例经真实 FastAPI 应用、真实会话/CSRF、真实 SQLite 与磁盘文件；第 3、4 节 | C/P |

## 3. 可复现 API/文件样例

```text
python examples/workflows/role_scheduling.py
```

脚本在真实认证边界内：发布并部署真实配置包、从 active 定义创建 Run、经管理命令签发 grant 与两个窄凭证、用角色凭证提交并封口一次决定、设置可信资源政策与观察、由执行消费者领取一个任务并在容量 1 下看到第二个任务被拒、消费者报告后由所有者核对并释放容量、等待任务转为可领取，最后读回图修订/任务分页/封口成员与其 pinned 版本正文。

实际输出（2026-09-15，Windows 11 + Python 3.12.14）。下面是
`.cache/r8-phase1/workers/04/` 下示例日志中被逐行核对过的内容；`decision` 一行取自修复后的运行
（`accepted_under_grant`），其余身份字段是其所来自那次运行的真实值，逐次运行不同：

```text
deployed: revision 1 slot 1 readiness ready
run: run-2e2cf7d5a0224033af186e720886bfbe frozen revision 1 graph revision 0 model calls 0
  frozen deployment 53405732f0cd compiled 0e77613b99db342c
grant: configuration-repair-scope depth 0 actions ['bind_role', 'expand_graph', 'seal', 'set_priority'] delegation False
credentials: cred-135f741a24b87800cd636bad routes ['decision'] raw token stored False
credential replay: True same identity True re-reveals token False
decision: decision-1 graph revision 1 accepted under accepted_under_grant tasks 2 approval required False
  outstanding outcomes ['comparison-report'] nodes added ['decision-1.option-a', 'decision-1.option-b']
seal: {'defects': ['decision-1.option-a', 'decision-1.option-b']} graph revision 2
resource: pool-a remaining 1 source local_ledger
queue: ready 2 waiting 0 [decision-1.option-a, decision-1.option-b]
claim: claim-425956db9f1787d04c0b653b write zone src/config/option-a physical execution not_started verified by engine False
second claim: 409 SCHEDULING_CAPACITY_BUSY capacity 1 occupied 1
consumer report: claimed reported completed verified by engine False
reconciled by the owner: completed released at True
run resource state: live claims 0
waiting task after release: 201 decision-1.option-b
graph: revision 2 revisions [1, 2] expansions [('defects', 'sealed')]
tasks: 2 paged 2 next cursor None
sealed member pin: revision 1 digest bd69e82610b4b0ac
pinned version readback: decision-1.option-a revision 1 inputs {'sources': 'requirement.option_a'}
activation: {"slot": "default", "action": "deploy_only", "model_calls": 0}
```

**每次运行都变化**：run、deployment、claim、凭证与 session 身份，以及 acquisition 摘要把加载时刻与进程纳入身份，
因此它们逐次不同。**在相同内容下每次运行相同**：`compiled_digest`，因为编译结果是内容的纯函数。
**不跨运行声称相同**：任务与图正文包含时间、状态与决策身份字段，因此它们的摘要随运行变化；
本文不对它们跨运行的相等性作任何断言。脚本内以断言校验的是结构事实
（例如 `physical_execution == "not_started"`、`stores_raw_token is False`、容量 1 下第二条 claim 为 409），
身份字段仅作展示。

`second claim: 409 SCHEDULING_CAPACITY_BUSY` 与 `consumer report: claimed` 都是**正确**结果：容量为 1 时合法任务全部保留并排队；未核对的消费者自述不改变控制面状态。`waiting task after release: 201` 是核对释放容量后同一任务被真实领取的证据。

## 4. 本机验证

```text
uv lock --check                                              -> Resolved 46 packages, exit 0
.venv/Scripts/python.exe -m ruff check .                     -> All checks passed!, exit 0
.venv/Scripts/python.exe -m mypy backend/karajan             -> Success: no issues found in 190 source files, exit 0
```

```text
pytest tests/scheduling                                     -> 70 passed, exit 0
pytest tests/routing/test_authorization.py
       tests/projects/test_qualification_store.py
       tests/runs/test_admission_guard.py
       tests/web/test_task_admission_http.py
       tests/tools/test_ci_quality_gate.py                   -> 54 passed, exit 0
pytest tests/web/test_workflow_deployment_process.py        -> 14 passed, exit 0
python examples/workflows/role_scheduling.py                 -> exit 0
```

广泛套件的**逐次来源**必须分开读，不能合并成一个总数：

```text
pytest tests/workflows tests/web tests/contract tests/scheduling
  181332-6160  -> 1 failed, 408 passed, 4 skipped, 14 subtests passed, exit 1
                  （唯一失败是 #177 套件中 test_an_oserror_...:329 的断言假阳性：
                    assert "28" not in ... 撞上时间戳 1789467351.5286982；
                    消毒后的 reason_code/at_step 检查本身通过）
  182015-44336 -> 409 passed, 4 skipped, 14 subtests passed, exit 0
                  （在断言**修正**之后启动，但在把该断言收紧为「记录形状恰好是
                    reason_code/at_step/recorded_at 且 recorded_at 为数值」之前启动）
  182222-39796 -> 14 passed, exit 0
                  （严格形状断言生效后的定向重跑，绑定该断言本身）
```

因此：广泛套件在 182015 的 409 passed 是真实的，但它测的是修正版而非收紧版断言；收紧后的
证据是 182222 的定向 14 passed。两个数字各自保留其来源，不互相替代，也不回填成一次
「全绿」总数。原始失败记录 181332-6160 原样保留。

每项都使用仓库质量门入口（见 [测试与合并质量门](testing-gates.md)）；`tests/scheduling` 的完整用例经真实 HTTP/SQLite/文件路径，不在断言前直接写库；当前候选为 **78 passed / exit 0**（`logs-04/pytest-20260915-193211-33592.log`，`tests/scheduling/test_scheduling_state.py` 单独 34 passed / exit 0）。完整日志、真实退出码与 basetemp 见 `.cache/r8-phase1/workers/logs-04/` 及本目录下的 `178-*` 记录。

远端证据（同一候选 `4d3dc7d`，base `cf96eba`）：

```text
PR #182  quality-gate  pass
         quick-python (ubuntu-24.04)  pass
         quick-python (windows-2022)  pass
         frontend-quality             pass
```

这是该 head 自己的 `quality-gate`，不是更早候选的历史记录。CI 通过只证明它实际覆盖的离线检查；
独立审查由 Commander 在本候选上单独进行，不以 CI 绿灯代替。本节的本地结果不包含远端 CI，
也不代替独立审查。

## 5. 证据边界与保留的失败事实

**C/P 覆盖**：第 3、4 节描述的证据均为 C（产品行为）与 P（本机执行），不含 S（真实服务）证据，也没有任何真实模型调用。远端 G（GitHub）证据在 PR 中逐候选报告。

**不声称**：
- 不声称模型已自主判断 1/3/7 或任何数量，也不声称真实物理并行：`model_calls == 0`，`physical_execution == "not_started"`；
- 不声称最终候选已集成、PR 已交付或完整 RS-AC/原 P3/P4 已完成；
- 不声称任何真实执行器的资格、计费或隔离；本切片的凭证与消费者是受信协议身份，取 C/P 而非 S。

**保留的失败事实**：本切片开发期间的独立探测记录于 `.cache/r8-phase1/workers/04/`。若干**早期 draft** 的失败被保留为历史并已在本候选修复，其中较重要的有：run id 只由 `(project, conversation, slot, inputs)` 派生导致新命令撞旧 run；原命令的查找发生在**新鲜来源校验之后**，因此 active 更换后原命令被误报 stale（修正是先做幂等重放）；输入只校验非空映射；grant 以参数名而非引用授权；`uncovered_obligations` 签名漂移；写区用字符串相等比较导致大小写别名与父子目录互相放行；凭证签发回执列名不一致；封口后成员版本被后续修订改写；未被核对的 completed 报告会终止任务并释放占用；被拒绝的决定没有持久回执；观察的 `used` 量被当作 `remaining` 读取而放行已耗尽的池；受信的 `unknown` 核对
永久挡住后续终端释放；已封口的 join 在必需成员尚未产出结果时就汇合；合法替代被静默忽略；一个决定批次内的
中间 pinned 版本未归档；输出契约只按全局注册表存在性校验；后续决定被标成新的用户批准。每一处都由第 2 节
列出的永久回归覆盖，早期记录保留其原字节。
