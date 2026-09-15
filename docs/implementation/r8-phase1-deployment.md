# R8-P1-03 部署物化、可信加载与 active 槽位

实现日期：2026-09-15。本记录对应 [#177](https://github.com/zhouy1017/Karajan/issues/177)，父票 [#174](https://github.com/zhouy1017/Karajan/issues/174)。设计依据 [对话式 Workflow 设计与部署](../architecture/10-conversational-workflow-deployment.md)、[可定制角色与 Workflow](../architecture/09-configurable-workflows.md) 与 [角色主导的调度](../architecture/11-role-directed-scheduling.md)；上游切片见 [R8-P1-02](r8-phase1-workflows.md) 与 [R8-P1-01](r8-phase1-gateway.md)。

本切片交付可调用、持久化且可恢复的**部署控制面**：持久部署意图、真实的 pending 包物化、可信 loader 的真实重读/重编译/能力校验、同摘要加载回执、条件 active 槽位，以及一个不可变的定义消费句柄。它**不**创建 Run、不调用模型、不执行任何业务步骤，也不声称完整 WD/Designer/运行范围已完成。

## 1. 模块与接口

| 路径 | 作用 |
|---|---|
| [`karajan/workflows/deployments.py`](../../backend/karajan/workflows/deployments.py) | `DeploymentStore`：意图持久化、pending 物化、loader 调用、槽位 CAS、回滚、待核对读取、不可变定义句柄 |
| [`karajan/workflows/loader.py`](../../backend/karajan/workflows/loader.py) | `WorkflowLoader` + `LoadReceipt`：重开真实 pending 文件、核对清单与身份、用私有注册表重编译、要求真实可调用能力 |
| [`karajan/workflows/layout.py`](../../backend/karajan/workflows/layout.py) | 新增 `require_segment`、`require_trusted_chain`、`discard_staging_tree`；受管链路校验由一处实现，bundle 与 deployment 共用 |
| [`karajan/web/workflows.py`](../../backend/karajan/web/workflows.py) | `DeployPayload`/`RollbackPayload` 与 `register_deployment_routes` |
| [`karajan/web/app.py`](../../backend/karajan/web/app.py) | 注册 deployment store 与路由；`app.state.{workflow,deployment}_store` 供本机工具直接持有活对象 |

存储与项目库同库（`projects.sqlite`），沿用既有归属、`commands` 幂等账本与 `BEGIN IMMEDIATE`；pending 包位于独立受管数据根 `state/workflow-deployments`。没有第二套认证、第二张命令表或第二条 idempotency 路径。

```text
POST /v1/projects/{id}/conversations/{cid}/workflows/{bundle_id}/revisions/{n}/deployments  Idempotency-Key
GET  /v1/projects/{id}/workflow-deployments/{slot}
GET  /v1/projects/{id}/workflow-deployments/{slot}/history
GET  /v1/projects/{id}/workflow-deployments/{slot}/deployments/{deployment_id}
GET  /v1/projects/{id}/workflow-deployments/{slot}/definition
POST /v1/projects/{id}/workflow-rollbacks                                                 Idempotency-Key
```

### 1.1 五个有序步骤

1. **持久化意图。** 在复制任何字节或运行任何 loader 之前，完整授权命令（主体、动作、项目/会话、bundle 身份与 revision、bundle/manifest/compiled/逐文件摘要、compiler 身份与 revision、已确认 preview、目标槽位、expected active revision）先落一条持久记录。
2. **用已核验源字节物化 pending。** 字节只来自 `WorkflowStore._verified_bundle` —— 它在本调用内重读已发布 revision 的每个文件、重算摘要并重编译。来源绝不是请求。写第一个字节前校验整条受管链路的 reparse point。
3. **重开并重编译真实包。** `WorkflowLoader` 读回本进程刚写的文件，用**模块私有**注册表重编译。ready 是这次读取产生的能力事实，不是意图里的字段。
4. **把回执与意图比对后才释放槽位。** 只有 loader 观测到的 bundle/manifest/compiled 摘要与 compiler 身份、revision 全部等于冻结值，回执才被接受，并与条件槽位更新写在**同一个短事务**里。
5. **条件激活。** 槽位更新携带 expected active revision，因此两个并发命令恰有一个胜出，回滚也不能覆盖更新的部署。

### 1.2 三种互不混淆的事实

| 事实 | 含义 | 何时被改写 |
|---|---|---|
| 历史加载回执 `load_receipt` | 激活该部署的那一刻，本进程真实读到了什么 | 永不；它是不可变证据 |
| 当前加载/就绪 `current` | 本进程现在重新读取后看到什么 | 每次读取都重算；新进程会替换它 |
| 命令意图 `workflow_deployment_intents` | 原命令持久化了什么、已完成哪些步骤、失败原因、最终结果 | 每次变更都在一个事务内读—改—写当前行 |

第二个与第一个分开正是 AC4 的核心：新服务进程启动时重新加载 active 的真实文件，而不是把旧 ready 回执当成本进程已加载。

### 1.3 意图状态的一致性

所有意图变更遵守同一条规则：**在写入它的那个事务内重读当前行，合并本次变更，再写回**。因此任何一次变更都作用在真正存储的内容上，而不是调用方手里的快照，两条同键尝试也就不会互相抹除证据。三条路径都遵循这一规则，只是所在事务不同：

- `_complete_intent` 在 `_activate` **已经打开的那个事务内**直接合并（激活、槽位 CAS、账本写入与完成标记必须在同一事务，不能拆开）；
- `_record_step` 与 `_record_failure` 各自经 `_mutate_intent` 打开一个短事务：在同一事务内先查权威账本再决定是否写入，再合并当前行。

由此：步骤检查点不会用陈旧快照覆盖另一同键尝试写下的失败；已提交的成功命令不会被改写为失败；完成时此前所有失败搬进 `failure_history` 后再标记 `activated`，既不删除证据也不自相矛盾。

## 2. 逐项验收

| 原验收条件 | 实现与行为证据 | 层级 |
|---|---|---|
| AC1 确认绑定项目/会话、bundle/file/compiled 摘要、preview revision、slot expected active revision 与 deploy_only | `test_an_exact_confirmation_materialises_loads_and_activates`（真实 pending 目录清单与源 revision **逐字节相同**、回执摘要与记录一致、`process_id == os.getpid()`、`verify_bytes is True`） | C/P |
| AC1 不能由请求断言身份 | `test_a_confirmation_request_cannot_supply_identities`（`bundle_digest`/`compiled_digest`/`manifest`/`loader`/`files`/`business_inputs`/`principal` 全部 422 `INPUT_INVALID`）、`test_an_unknown_deploy_field_is_refused` | C |
| AC1 deploy_and_run 明确 unsupported，且不写任何状态 | `test_the_action_is_explicit_and_deploy_and_run_is_unsupported`（501 `WORKFLOW_DEPLOY_ACTION_UNSUPPORTED`）、`test_an_unsupported_action_is_refused_before_any_state_change`（意图表为空、槽位 0） | C |
| AC1 旧预览/摘要错误/变更字节拒绝 | `test_a_stale_confirmation_is_refused_after_a_newer_revision`（revision 2 发布后 revision 1 的确认 409 `WORKFLOW_PREVIEW_STALE` + `current_revision`）、`test_a_wrong_preview_identity_is_refused`、`test_a_package_whose_bytes_change_is_refused` | C/P |
| AC1 无业务输入的参数化模板可部署 | `test_a_parameterised_template_with_no_business_input_is_deployable`（`runs_started == 0`、`model_calls == 0`，模板输入仍为符号引用） | C |
| AC2 pending 期间旧 active 保持有效、pending 不可消费 | `test_a_failure_during_the_load_keeps_the_old_active_and_records_the_reason`（新版本加载失败时，旧 active 仍 `ready` 且其定义句柄仍可获取，失败意图标为不可消费）、`test_the_old_active_survives_while_a_new_one_is_prepared`、`test_a_pending_package_is_never_consumable`、`test_two_overlapping_services_admit_exactly_one_slot_winner`（重叠期间观察者看到槽位仍为 0、`active` 为 null、两条命令均 pending 且 `consumable false`） | C/P |
| AC2 只有真实可调用能力才能给出 ready | `test_an_unknown_capability_blocks_readiness`（`agent_task@1` 已登记但无适配器 → 422 `WORKFLOW_CAPABILITY_UNAVAILABLE`，槽位不动）、`loader._require_capabilities` 要求 `adapter is not None and callable(...) and kind.available` | C/P |
| AC2 编译/部署不执行任何业务步骤 | `test_the_business_adapter_is_never_executed_by_a_deployment`（替换适配器为记录型包装后调用数仍为 0）、`business_steps_executed == 0` | C/P |
| AC2 请求不能注入 loader | `test_the_deployment_module_imports_nothing_from_a_request`（deployments/loader 源码不含 `importlib`/`__import__`/`eval(`/`exec(`/`subprocess`） | C |
| AC3 意图先于文件系统/loader 工作持久化 | `test_the_intent_is_persisted_before_the_copy`、`test_an_oserror_during_the_copy_leaves_a_reconcilable_intent`（在第一个受管文件写入的**那一刻**断言意图已存在） | P |
| AC3 同键同载荷返回原结果（含槽位移动后） | `test_a_repeat_returns_the_original_result_after_the_slot_moves`（`replayed is True`、原 `load_receipt` 与 `completed_at` 不变、`current.is_active is False`） | C/P |
| AC3 同键异载荷冲突 | `test_a_changed_payload_on_the_same_key_conflicts`（409 `WORKFLOW_IDEMPOTENCY_CONFLICT`） | C |
| AC3 并发槽位至多一者成功 | `test_only_one_of_two_concurrent_commands_takes_the_slot`（顺序 stale-CAS 覆盖）、`test_two_overlapping_services_admit_exactly_one_slot_winner`（两个真实服务在同一状态上真正重叠：两者都在**真实 load 之后**到达栅栏，观察者看到槽位仍为 0 且两条命令均 pending 不可消费，随后恰一者激活，败者保留 `WORKFLOW_SLOT_REVISION_CONFLICT` 诊断） | P |
| AC3 结果未知时先核对原命令，不用新部署掩盖 | `test_a_completed_copy_with_no_step_checkpoint_is_adopted_after_replay`（复制真的完成后检查点丢失 → 重建服务**采纳同一 deployment**，不新建目录）、`test_a_committed_activation_whose_response_was_lost_returns_the_same_result`（激活真的提交后响应丢失 → 重放返回原提交结果与原回执，active revision 只前进一次） | P |
| AC3 失败保留可恢复诊断 | `test_a_failure_during_the_load_keeps_the_old_active_and_records_the_reason`（旧 active 仍可消费；待核对条目 `outcome failed`/`at_step load`/`consumable false`/`package_present true`）、`test_a_failure_during_activation_leaves_the_slot_unmoved` | P |
| AC3 观察到的普通 I/O 失败可诊断且不泄露 | `test_an_oserror_during_the_copy_leaves_a_reconcilable_intent`（`WORKFLOW_STEP_FAILED_OSERROR`；`failure` 只含固定键，无异常消息、无 host 路径） | P |
| AC3 诊断不得与权威账本矛盾、也不得抹除历史 | `test_a_step_checkpoint_cannot_erase_a_failure_written_meanwhile`、`test_a_success_landing_after_a_failure_check_does_not_contradict_it`（同键两次尝试按真实交错执行；最终 `activated` 且早期失败保留在 `failure_history`） | P |
| AC4 新服务进程真实重新加载/核对 active 文件 | `test_a_separate_process_loads_the_active_package_itself`（**独立解释器**；观测 PID ≠ 测试进程，`loaded_by_process == 该 PID`，`verify_bytes is False`，历史回执仍是部署者的） | P |
| AC4 文件损坏/缺失或 compiler 不匹配显示 unavailable/blocked | `test_a_separate_process_blocks_on_a_corrupt_package` 与 `test_a_separate_process_blocks_when_the_package_is_gone`（均由**独立解释器**观测到 `blocked` 与具体理由码，且定义句柄被拒）、`test_a_compiler_revision_mismatch_blocks`（本进程读取即 `blocked`，再由新进程复核同一结果）、`test_a_package_whose_bytes_change_is_refused`（本进程 `status` 与 `definition` 两条路径） | P |
| AC4 回滚走相同 pending/loader/CAS 流程，不覆盖更新部署 | `test_rollback_goes_through_the_same_pipeline`（新 deployment 目录、`rollback_of` 指向精确历史 deployment、`bundle_revision` 回到 1、旧记录不被改写）、`test_a_rollback_cannot_overwrite_a_newer_deployment`（409）、`test_an_unknown_rollback_target_is_refused` | C/P |
| AC5 可信消费读取“已加载且 active”的不可变定义 | `test_the_definition_handle_is_immutable_across_a_new_deployment`、`test_a_held_handle_is_not_changed_by_a_later_deployment`（持有**活对象**，新部署后重读同一对象仍描述 revision 1）、`test_a_held_handle_survives_a_rollback` | C/P |
| AC5 嵌套集合不能改摘要绕过 | `test_nested_collections_cannot_be_edited_to_bypass_the_digest`（改写返回副本的嵌套 list/dict 后，新获取与同一句柄的冻结身份均不变） | C |
| AC5 未就绪不可消费 | `test_an_empty_slot_has_no_definition`、损坏/缺失包的 `definition` 请求 409 | C/P |
| AC6 认证/CSRF/跨项目/跨会话 | `test_authentication_origin_and_csrf_are_required`（无会话 401、错误 Origin 403、缺 CSRF 403，且被拒请求不落库）、`test_a_deployment_is_scoped_to_its_project_and_conversation`（跨项目会话 404、跨会话 409 `WORKFLOW_CONVERSATION_MISMATCH`）、`test_a_readback_of_another_projects_deployment_is_not_found`（按 deployment id 读取另一项目返回 404，其 history 为空）、`test_a_slot_name_can_never_be_a_path` | C/P |
| AC6 提供可复现示例，Ruff/mypy/快速门通过 | 见第 3、4 节 | C/P |

## 3. 可复现 API/文件样例

```text
python examples/workflows/workflow_deployment.py
```

脚本在同一解释器的真实认证边界（Session + Origin + CSRF + `Idempotency-Key`）内：发布两个真实配置包（其一引用尚无适配器的 `agent_task@1`）、确认一次 `deploy_only`、重放同一命令、发布并部署 revision 2、提交一次过期确认、回滚到精确历史 deployment、读回槽位/待核对/回执、获取定义句柄，并在**新建的应用实例**上重开同一真实 SQLite 与文件后重新加载。实际输出（2026-09-15，Windows 11 + Python 3.12.14）：内容身份（`bundle_digest`、`compiled_digest`、`manifest_file_digest`）是确定性的；deployment id、会话 id、加载回执摘要与定义句柄摘要把获取时刻、进程与部署事实纳入身份，因此每次运行都不同。

```text
bundle: 1 executable 0
model template deploy: 422 WORKFLOW_CAPABILITY_UNAVAILABLE ['agent_task@1']
deploy_and_run: 501 WORKFLOW_DEPLOY_ACTION_UNSUPPORTED
deployed: 1 slot 1 readiness ready runs 0 business steps 0
  bundle digest   cbfbb374306b0198
  compiled digest 0e77613b99db342c
  loader          karajan.workflow-loader.v1
  verify_bytes    True
replay: 200 replayed True same deployment True
revised: 2 4e787c9b52cbb05e
stale confirmation: 409 WORKFLOW_PREVIEW_STALE
deployed revision 2: slot 2 4e787c9b52cbb05e
rollback: slot 3 revision 1 rollback_of True
slot: 3 readiness ready active rolled back
  pending commands: ['blocked']
  historical receipt preserved: 53e1a02f245d454b verify_bytes True
definition handle: 1 steps ['option-a', 'option-b', 'comparison'] loaded by 40780
  digest: 7e3d42ce49b6efc4
after restart: ready loaded by this process True verify_bytes False
adapter (not run by any deployment): input_name_ascending 2 adf41c5750d8d7e8
activation: {"slot": "default", "action": "deploy_only"}
```

`pending commands: ['blocked']` 是**正确**结果而非缺陷：被拒的模型模板部署保留了一条不可消费的待核对意图，正是 AC3 要求的“失败保留可恢复诊断”。`after restart` 一行由同一解释器内的新应用实例产生；真正**新进程**的加载事实由 `tests/web/test_workflow_deployment_process.py` 的独立解释器用例证明。

## 4. 本机验证

```text
uv lock --check --offline                                    -> Resolved 46 packages
.venv/Scripts/python.exe -m ruff check .                     -> All checks passed!
.venv/Scripts/python.exe -m mypy backend/karajan             -> Success: no issues found in 182 source files
```

```text
pytest tests/workflows tests/web tests/contract              -> 337 passed, 4 skipped, 14 subtests passed
                                                                (其中一条新增断言出现假阳性；见第 5 节)
pytest tests/web/test_workflow_deployment_process.py         -> 14 passed
pytest tests/web/test_workflow_deployment_http.py \
       tests/web/test_workflow_deployment_process.py         -> 47 passed
pytest <the five quick-gate files from testing-gates.md>      -> 54 passed
```

完整日志保存在 `.cache/r8-phase1/workers/logs-03/`（`pytest-20260915-163452-49276.log`
广泛套件、`...-163943-24568.log` 进程套件、`...-164041-18700.log` 快速门），每一项都带真实
退出码。

`tests/web/test_workflow_deployment_process.py` 与 `..._http.py` 共 47 项通过。已知假阳性与全部历史失败记录见第 5 节。

## 5. 证据边界与保留的失败事实

**C/P 覆盖**：本切片全部证据为 C（产品行为）与 P（本机执行）。没有 S（真实服务）或 G（GitHub 远端）证据，也没有任何真实模型调用。

**不声称**：
- 不创建、不执行任何 Run；`deploy_only` 不执行任何业务步骤（`business_steps_executed == 0`）；
- 不声称某个模型执行器 ready：模板引用的未知能力一律阻塞；
- 不声称 #178 的调度、派发或待执行队列已实现——本切片只提供它消费的定义句柄；
- 不声称完整 WD-AC、Designer 对话创作或原 P3/P4 已完成。

**保留的失败事实**：
- 本切片开发期间的独立探测记录于 `.cache/r8-phase1/workers/03/`（helper 与报告），其中若干**早期 draft** 的失败被保留为历史：`177-spec-early.md` 记录的“expected compiler_revision 2 被接受而回执报告 revision 1”“revision 2 发布后 revision 1 仍能激活”“`load_compiled` 接受了错误的 expected compiled digest”；`177-standards-diagnostics.md` 记录的两次诊断交错缺陷。这些缺陷已在本候选修复，并由上表列出的永久回归覆盖；早期记录保留其原字节。
- `tests/web/test_workflow_deployment_process.py` 曾在广泛套件中出现一次断言假阳性：`assert "28" not in recorded` 误伤了 `recorded_at` 时间戳中的数字。该断言已改为断言诊断的**固定键集合**（`reason_code`/`at_step`/`recorded_at`）与理由码，不再禁止时间戳中的任意数字；原失败保留于 `logs-03/pytest-20260915-163452-49276.log`。
