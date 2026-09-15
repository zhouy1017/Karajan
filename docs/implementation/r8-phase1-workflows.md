# R8-P1-02 实际 Workflow 配置包与同源编译预览

实现日期：2026-09-15。本记录对应 [#176](https://github.com/zhouy1017/Karajan/issues/176)，父票 [#174](https://github.com/zhouy1017/Karajan/issues/174)。设计依据 [可定制角色与 Workflow](../architecture/09-configurable-workflows.md)、[对话式 Workflow 设计与部署](../architecture/10-conversational-workflow-deployment.md) 与 [角色主导的调度](../architecture/11-role-directed-scheduling.md)。

本切片交付可调用、持久化且可恢复的**配置创作与编译控制面**：真实配置文件字节、不可变 revision、无自引用摘要、纯声明式编译器、自定义角色（不再映射回固定三角色枚举）、同源图表/表格/差异，以及**不调用模型**的表格/文件编辑。它**不**激活配置、不启动 Run、不调用模型、不执行任意脚本，也不声称对话 Designer 或完整 r8 已完成。

## 1. 模块与接口

| 路径 | 作用 |
|---|---|
| [`karajan/workflows/layout.py`](../../backend/karajan/workflows/layout.py) | 唯一允许的包布局；路径规范化、重复拼写、Windows 保留名/ADS/尾随点、reparse point 链校验 |
| [`karajan/workflows/yamls.py`](../../backend/karajan/workflows/yamls.py) | 安全 YAML：拒绝自定义标签、重复键、别名/anchor、表达式标量；节点/字节/深度为传输边界而非业务上限 |
| [`karajan/workflows/bundle.py`](../../backend/karajan/workflows/bundle.py) | 真实字节读写、manifest 生成与校验、封闭清单（多出文件即拒绝）、`compile_bundle` 纯接口 |
| [`karajan/workflows/compiler.py`](../../backend/karajan/workflows/compiler.py) | 纯声明式编译器：固定引用、类型化 I/O、依赖无环、声明式条件、有限返工、动态边界、交付目标硬门；graph/表格/Mermaid/diff 同源投影 |
| [`karajan/workflows/registry.py`](../../backend/karajan/workflows/registry.py) | 受信任执行种类注册表与**唯一真实适配器** `artifact_aggregate@1`（确定性文本合并） |
| [`karajan/workflows/store.py`](../../backend/karajan/workflows/store.py) | 项目内不可变 revision 存储、写前链路校验、确定性编辑、创作输入、网关绑定精确解析 |
| [`karajan/workflows/errors.py`](../../backend/karajan/workflows/errors.py) | 内容无关的失败码与**定位诊断**（location 永远只指向包内文件） |
| [`karajan/web/workflows.py`](../../backend/karajan/web/workflows.py) | 认证 HTTP 路由，注册进现有 `create_app` |
| [`karajan/web/body_limit.py`](../../backend/karajan/web/body_limit.py) | 请求体上界：一般命令 64 KiB；bundle 路由单独声明 4 MiB（字节上界，非任务数上限） |

存储与项目库同库（`projects.sqlite`），因此归属、`commands` 幂等账本与 `BEGIN IMMEDIATE` 都沿用既有原语。`create_app` 新增 `WorkflowStore` 注册与复用同一 `GatewayCatalogStore`；没有第二套认证或命令表。

### 1.1 三种互不混淆的身份

| 身份 | 覆盖范围 | 不包含 |
|---|---|---|
| 每文件 `byte_digest` | 该文件存储的原始字节 | — |
| `bundle_digest` | 规范化 manifest 内容（含已排序的文件摘要与固定引用），**排除自身 digest 字段** | manifest 自己；manifest 不进自己的文件清单，其自身字节哈希另记为 `manifest_file_digest` |
| `compiled_digest` | 参数化模板：workflow 身份、冻结角色定义与内容摘要、绑定与已解析绑定身份、声明输入、步骤、完成策略、调度边界、返工、交付门、编译器版本 | **某次 Run 的具体输入、动态子任务实例**；可读图表不作为哈希输入 |

读回（`verified_files`）从不复用已存的预览：每次读取都重新读取磁盘字节、重算摘要并重新编译，再与不可变 revision 记录逐项比对。因此未改动的文件在任何新服务实例或同一进程内的第二次读取中都得到**相同身份**（这也是重启无需缓存即可复现的原因）；只有当文件被替换或损坏、字节与已发布摘要不符时，才产生**不匹配**，而不是在旧 revision 身份下返回一份新模板。（本票的读回证据范围见第 3 节：新服务实例、同一解释器；新进程加载事实由 #177 单独要求。）

### 1.2 一次发布的原子性

`_write_revision` 先校验**整条受管链路**（受管根、项目目录、bundle 目录、revision 目录）在写入之前都不含 reparse point，然后把完整树写入唯一的暂存目录并 fsync，再以 `os.rename` 就位，最后读回校验。事务提交失败会留下一个**没有任何 revision 引用的惰性目录**；该目录由 `_reconcile_materialized` 处理：字节完全一致时被采纳，不一致时以 `WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH` 拒绝并原样保留待查，绝不静默替换或另建新 bundle。

```text
GET  /v1/projects/{id}/workflows
GET  /v1/projects/{id}/workflow-execution-kinds
POST /v1/projects/{id}/conversations/{cid}/workflows/{bundle_id}     Idempotency-Key
PUT  /v1/projects/{id}/conversations/{cid}/workflows/{bundle_id}     Idempotency-Key + If-Match
GET  /v1/projects/{id}/workflows/{bundle_id}/revisions/{revision}
GET  /v1/projects/{id}/workflows/{bundle_id}/revisions/{revision}/preview[?compare_to=N]
POST /v1/projects/{id}/conversations/{cid}/workflows/{bundle_id}/edits          Idempotency-Key + If-Match
GET  /v1/projects/{id}/workflows/{bundle_id}/authoring-inputs
POST /v1/projects/{id}/conversations/{cid}/workflows/{bundle_id}/authoring-inputs
```

命令主体固定为 `owner`（与既有路由一致），由 `project_owners` 校验；bundle 的**所属会话在创建时固定**，之后的 revision/编辑/创作输入都不能把它移到另一会话，跨项目会话与未知会话返回相同的 404，因此不泄露另一项目会话是否存在。

## 2. 逐项验收

| 原验收条件 | 实现与行为证据 | 层级 |
|---|---|---|
| AC1 真实文件持久化、重启读回、不可变 revision、文件/bundle 摘要 | `test_workflow_bundle.py::test_real_files_round_trip_with_separate_bundle_and_manifest_identities`（真实字节↔逐文件摘要一致、manifest 不自引用、`bundle_digest` 与字段自身无关）、`test_a_second_read_of_the_same_directory_is_identical`；HTTP 侧 `test_workflow_http.py::test_bundle_bytes_are_stored_verified_and_read_back_after_restart`（真实 `manifest.json` 与读回文档逐字节一致，重开后相同）、`test_workflow_http_bounds.py::test_a_restart_app_serves_the_same_preview_identity` | C/P |
| AC1 路径穿越/绝对/drive/UNC/符号链接与 junction 逃逸/重复规范化路径/非法清单拒绝 | `test_declared_paths_are_validated_before_any_write`（14 种路径逐一断言理由码与定位）、`test_case_and_separator_duplicates_are_refused`、`test_normalized_duplicate_paths_are_refused_in_a_manifest`、`test_a_manifest_that_lists_itself_is_refused`、`test_a_missing_required_file_is_refused`、`test_a_caller_cannot_supply_the_manifest`、`test_a_link_inside_the_bundle_is_refused`、`test_a_link_on_an_ancestor_of_the_revision_is_refused`、`test_bundle_identity_must_be_one_addressable_segment` | C/P |
| AC1 写边界：链接不得先收到字节 | `test_a_preexisting_link_never_receives_any_bytes`（项目目录与 bundle 目录两种植入，断言外部目录清单**逐项未变**）、HTTP 侧 `test_workflow_http_bounds.py::test_a_link_never_receives_any_bundle_bytes` 与 `test_the_store_never_writes_outside_its_managed_root` | P |
| AC1 未提交物化可恢复、不匹配孤儿字节被拒 | `test_an_uncommitted_materialization_is_adopted_or_refused`、HTTP 侧 `test_an_uncommitted_materialization_is_recovered_after_reopen`（同一解释器内的**新服务实例**重新打开真实数据库与文件后重放同一命令） | C/P |
| AC2 登记执行种类、自定义角色、I/O、静态依赖、声明式条件、动态扩展边界 | `test_workflow_compiler.py` 全量；`test_two_custom_roles_resolve_by_alias_and_fixed_reference`（别名→固定 `role:id@rev`）、`test_artifact_reference_must_name_a_real_depended_on_producer`、`test_condition_facts_resolve_against_real_steps_and_inputs`、`test_unknown_execution_kind_is_located_and_never_guessed`、`test_dynamic_boundaries_contribute_to_capability_availability` | C |
| AC2 缺引用/环/契约不兼容/未注册种类返回定位诊断，不猜成 Worker | `test_missing_references_cycles_and_contract_mismatches_are_located`、`test_unknown_kind_rejections_always_carry_a_location`（先断言 `diagnostics` 非空再遍历）、`test_condition_types_are_checked_against_declared_contract_fields`、`test_a_condition_cannot_create_a_hidden_cycle` | C |
| AC2 合法超过 100 节点完整编译/读取，无旧 100 项上限或人数截断 | `test_more_than_one_hundred_nodes_compile_without_truncation`（121 节点）、`test_five_thousand_steps_compile_from_real_documents`（5001 节点纯编译）、HTTP 侧 `test_five_thousand_steps_round_trip_through_the_real_api`（真实上传→落盘→读回→重启→投影，节点/边/必需步骤/首末 id 全部保留）、`test_dynamic_expansion_has_no_engine_count_cap`（省略计数保持省略；显式 100000 保留；`engine_member_cap` 恒为 null；用户 `agent_policy` 原样保存） | C/P |
| AC3 文件/表格/Mermaid/结构化图/diff 同源，绑定 bundle digest、compiler revision 与模板 compiled digest | `test_a_preview_is_derived_from_one_compiled_result`、`test_the_preview_and_the_stored_bytes_describe_one_revision`、`test_compiled_identity_excludes_run_inputs_and_is_reproducible`（文档内不含 run/input digest） | C |
| AC3 可视文本安全转义，角色名不能注入 Mermaid/HTML | `test_mermaid_node_identities_never_collide`（`a-b` 与 `a_2db` 节点身份不冲突、两条边各自正确）、`test_labels_are_escaped_for_mermaid_and_html`、HTTP 侧 hostile 步骤名断言标签内无 `<`/`>` | C |
| AC3 角色内容进入编译身份与差异 | `test_role_content_is_part_of_the_compiled_identity_and_diff`（改职责/工具约束即改 `compiled_digest`，`changed_roles` 如实报告）、`test_two_aliases_of_one_role_keep_their_own_binding_in_the_table`、`test_switching_between_two_aliases_changes_the_compiled_identity`、`test_an_instruction_change_changes_the_compiled_identity` | C |
| AC4 表格/文件编辑直接写新 revision 且确定性编译，不调用模型 | `test_direct_edits_are_deterministic_and_call_no_model`（`model_calls == 0`、`edit_path == deterministic_structural`、新图真实含两条依赖）、`test_a_replayed_edit_returns_the_complete_original_result`（重放返回**完整**原结果） | C |
| AC4 文字仅持久为创作输入/明确待生成状态 | `test_text_authoring_is_persisted_as_pending_and_generates_nothing`（`state == pending_generation`、`generated_configuration is None`、不产生 revision） | C |
| AC4 旧 expected revision 冲突、迟到写入不覆盖新候选、相同幂等请求不多建版本 | `test_idempotency_replay_conflict_and_late_write_rejection`、`test_a_replayed_edit_returns_the_complete_original_result`（编辑后旧 `If-Match` 得到 409 + 当前 head）、`test_concurrent_writers_leave_exactly_one_revision`（两个 app 同库竞争，仅一者成功） | C/P |
| AC5 HTTP 复用项目/会话归属与 Session/CSRF；两个不同角色/拓扑配置 | `test_two_role_topologies_compile_with_their_own_sources`、`test_an_unauthenticated_request_is_refused_before_any_file`（无会话 401、错误 Origin 403、缺 CSRF 403）、`test_a_record_is_read_only_through_its_own_project`、`test_a_second_project_cannot_read_another_projects_revision`、`test_authoring_text_cannot_be_filed_under_another_conversations_bundle`（含 authoring-first 抢占分支） | C |
| AC5 结构化字段不得携带凭据或脚本；合法散文与声明式上限保留 | `tests/web/test_workflow_fields.py`（嵌套凭据 422 且不回显取值、未知字段拒绝、脚本字段拒绝、散文保留、未声明引用字段拒绝）、`test_workflow_semantics.py::test_a_credential_nested_in_a_structured_role_field_is_refused` | C |
| AC2/AC3 身份与目标语义：literal 与引用分离、所选契约决定目标、模板字节绑定、独立性、调度别名 | `tests/workflows/test_workflow_semantics.py`、`test_a_literal_and_a_reference_keep_distinct_identities`、`test_a_marked_literal_prefix_is_decoded_exactly_once`、`test_a_supplied_instruction_template_is_bound_by_its_own_bytes`、`test_the_selected_scheduler_alias_and_binding_are_frozen` | C |
| AC5 非法路径/图、>100 节点、直接编辑无模型、重启读回、旧版本冲突通过 | 见上各项 | C/P |
| AC5 提供可运行 API/文件示例，Ruff/mypy/快速门通过 | 见第 3、4 节 | C/P |
| 真实注册的确定性 `artifact_aggregate` 适配器：类型化文本输入→确定性报告产物，无模型/shell/任意导入/外部副作用 | `test_the_real_adapter_is_reachable_from_the_registry`、`test_a_marked_literal_must_carry_data_and_the_adapter_agrees`（编译绑定直接喂给真实适配器）、`test_the_adapter_enforces_the_same_declared_contract_as_the_compiler`、`test_a_marked_literal_and_plain_text_differ_only_in_their_marker` | C |
| 编译与 deploy-only 只校验模板/能力，绝不执行业务适配器来制造 ready | `test_compilation_never_invokes_the_business_adapter`（用记录型包装替换适配器后编译路径仍零调用）；deploy-only 记录 `model_calls == 0`、`activation_allowed == false` | C |
| 与网关目录按固定引用衔接；未核验来源为草稿且明确不可执行 | `test_gateway_bindings_resolve_by_exact_revision_without_a_probe`（精确 revision + digest、`probe_performed:false`、`credential_resolved:false`、`execution_eligible:false`、`draft_only:true`；`@latest` 被拒；未知绑定不可解析） | C |
| report/patch/pr 必需目标政策不因自定义名称绕过；无适配器的种类显示 unavailable | `test_delivery_target_cannot_be_met_by_changing_its_name`、`test_protected_gate_survives_optional_and_conditional_evasion`、`test_a_report_expansion_cannot_smuggle_in_remote_delivery`、`test_a_candidate_kind_cannot_declare_a_text_contract`、`test_an_unavailable_capability_is_reviewable_but_not_executable`、`test_the_execution_kind_catalog_reports_real_availability` | C |

## 3. 可复现 API/文件样例

```text
python examples/workflows/bundle_preview.py
```

脚本在同一解释器内通过真实认证边界（Session + Origin + CSRF + `Idempotency-Key` + `If-Match`）创建会话、发布两个不同角色/拓扑的真实配置包、从表格命令编辑、读取同源 preview/diff、保存一条创作输入、读取执行种类目录，并在一个**新建的应用/服务实例**上重新打开同一真实 SQLite 与文件后读回同一 revision。

这里重开的是**同一解释器内的新服务实例**（重新 `create_app`、重新打开真实数据库与文件），不是新的操作系统进程；本票的 AC1 读回证据以该范围为准。真正的**新进程加载事实**由 #177 的部署加载契约单独要求与验收。实际输出（2026-09-15，Windows 11 + Python 3.12.14；会话 id 每次运行不同，其余摘要为确定性）：

```text
conversation: 201 fc7c501f-fb9e-4511-905e-dd9087246fe4
bundle 1: 201 revision 1 readiness executable model_calls 0
  bundle digest   b781c12f5607b420
  compiled digest 3bb1d1c3149b4d93
  manifest file   6ed0d1930e64bb03
bundle 2: 201 True
graph nodes: ['option-a', 'option-b', 'comparison']
graph edges: [('option-a', 'comparison')]
roles: ['editor', 'researcher']
mermaid:
    flowchart TD
        n0["option-a: artifact_aggregate@1 (role:source-researcher@2)"]
        n1["option-b: artifact_aggregate@1 (role:source-researcher@2)"]
        n2["comparison: artifact_aggregate@1 (role:technical-editor@1)"]
        n0 --> n2
edit: 201 revision 2 model_calls 0
diff: changed True changed_steps ['comparison'] roles_changed False
authoring: 201 state pending_generation generated None
execution kinds: {"agent_task@1": false, "artifact_aggregate@1": true, "candidate_integrate@1": false, "deterministic_check@1": false, "human_decision@1": false, "publish_pr@1": false}
after restart: verified_files verified True compiled digest matches True
adapter: input_name_ascending utf-8 inputs 2 digest adf41c5750d8d7e8
```

脚本使用 `TestClient` 而不是真实网络端口以保持确定性；同一套路由在 `python -m karajan.web serve` 下由 `create_app` 注册，认证、来源校验、CSRF 与请求体上界相同。

## 4. 本机验证

```text
uv lock --check --offline                                    -> Resolved 46 packages
.venv/Scripts/python.exe -m ruff check .                     -> All checks passed!
.venv/Scripts/python.exe -m mypy backend/karajan             -> Success: no issues found in 180 source files
.venv/Scripts/python.exe -m pytest tests/workflows tests/web -> 262 passed, 4 skipped
.venv/Scripts/python.exe -m pytest tests/projects tests/gateway \
  -o "pythonpath=backend tests/web tests/runs tests/projects tests/adapters/opencode"
                                                             -> 453 passed, 31 skipped
.venv/Scripts/python.exe -m pytest tests/routing/test_authorization.py \
  tests/projects/test_qualification_store.py tests/runs/test_admission_guard.py \
  tests/web/test_task_admission_http.py tests/tools/test_ci_quality_gate.py -> 54 passed
python examples/workflows/bundle_preview.py                  -> exit 0（输出见第 3 节）
```

每次 pytest 调用经 `run_tests_local.py`（本机检查点脚本，**不提交**）使用 `tempfile.mkdtemp` 生成的**全新唯一**目录作为 `--basetemp`，该目录位于所有 Git 检出之外，并保留真实退出码。

### 4.1 独立审查发现的缺陷与修复

以下问题由独立审查在**早期候选**上复现并在本 PR 内修复；每一项都有对应回归测试。原失败与更正后的归因一并保留。

| 发现 | 修复 | 回归 |
|---|---|---|
| literal 与 reference 编译为同一输入与摘要；`literal:` 前缀被解码两次 | 标记保留在编译绑定中，仅在适配器解码一次；未标记字符串在适配器处作为引用被拒绝 | `test_a_literal_and_a_reference_keep_distinct_identities`、`test_a_marked_literal_prefix_is_decoded_exactly_once` |
| 交付目标用"可能输出"而非**所选契约**判断 | 比较步骤实际声明的 `output_contract_kind` | `test_a_delivery_target_needs_the_selected_contract_not_a_possible_one` |
| 聚合输出声明 `digest`/`input_count` 但实际不返回；条件可读不存在的字段 | 声明 schema 与真实返回值对齐为 `content_digest`/`input_count`，并在适配器内断言 | `test_the_real_aggregate_emits_every_field_its_schema_promises` |
| 已声明指令模板缺失仍可执行 | 模板必须是本包真实文件，摘要进入角色身份 | `test_a_missing_instruction_template_is_refused`、`test_a_supplied_instruction_template_is_bound_by_its_own_bytes` |
| `independence_requirements` 被静默丢弃 | 归一化、校验并冻结进角色身份与差异 | `test_independence_requirements_enter_the_identity_and_diff` |
| 调度角色在两个别名（同 role ref、不同绑定）之间切换不改变摘要 | 冻结所选别名、`binding_ref` 与 `binding_identity`，并列出可绑定别名 | `test_the_selected_scheduler_alias_and_binding_are_frozen` |
| 顶层未知字段（如 `api_key`）被忽略并原样回显 | 拒绝未知字段与凭据形状字段名；仅报告字段名，**不回显取值** | `test_an_unknown_workflow_field_is_refused_rather_than_ignored` |
| 凭据可藏于合法结构化字段（`role.tool_constraints.auth.api_key`） | 递归检查映射/列表中的字段名；同时递归拒绝脚本/导入形状字段 | `test_a_credential_nested_in_a_structured_role_field_is_refused`、`test_a_credential_nested_in_a_legitimate_role_field_is_refused` |
| 创作文字可在创建前抢占归属：会话 A 先存文字，会话 B 仍可创建同 ID bundle | 归属由**先使用该身份的命令**确定；创建前先校验，另一会话被拒且不写任何文件 | `test_authoring_text_cannot_be_filed_under_another_conversations_bundle`（含 authoring-first 分支） |
| Windows-only 测试无条件执行 `cmd/mklink`，Linux 全量套件必然失败 | 改为可移植的目录链接辅助函数（Windows 用 junction，其他平台用 symlink），仅在平台确实无法建链时以明确原因跳过 | `test_a_link_never_receives_any_bundle_bytes`、`test_a_link_inside_the_bundle_is_refused`、`test_a_link_on_an_ancestor_of_the_revision_is_refused`、`test_a_preexisting_link_never_receives_any_bytes` |

**关于 Unicode 证据的更正。** 早先一次 `UnicodeEncodeError` 复现使用 `TestClient.post(json=...)`，异常发生在**客户端序列化阶段**，并非服务端 500。因此本记录**不**声称修复了一个已复现的 HTTP 500。真实 HTTP 边界对 ASCII 转义的代理项 JSON 本就返回 `422 INPUT_INVALID`；`prepare_files`/摘要与适配器中的直接编码防护属于**服务层校验加固**，并有 `test_unpaired_surrogate_text_is_refused_rather_than_raising` 覆盖。

**关于读取链路的一项撤回。** 曾有审查提出 `_verified_bundle` 只校验 bundle/revision 之下的链路。复核 `BundlePaths.resolve` 后确认其在读取前已用词法 root 做包含性检查，因此该猜测已**撤回**，未据此修改代码。

## 5. 边界与未覆盖范围

- 不激活配置、不启动 Run、不写 Run/Plan、不调用模型、不执行任意脚本或 shell 条件表达式。
- `deploy_only` 只校验模板与能力；编译路径**不**调用业务适配器来制造 ready 状态。
- 除 `artifact_aggregate@1` 外，`agent_task`、`deterministic_check`、`human_decision`、`candidate_integrate`、`publish_pr` 均为**已登记但无适配器**，在目录与编译结果中都显示 `available=false`、`executable=false`；登记本身不构成可用能力。
- 动态调度边界只声明范围：编译结果固定 `grants_authority=false`、`authority_source=scheduler_grant_required`、`product_agent_limit=null`。真正的授权与派发属于 #178。
- 本切片不提供拖拽画布、不实现对话 Designer 的模型调用；文字只持久为 `pending_generation` 创作输入。
- 参数化模板可无具体 Run 输入发布；`compiled_digest` 不含任何 Run 输入或动态子任务实例。
- 请求体上界是**传输**上界（一般 64 KiB、bundle 路由 4 MiB），不是任务/角色/并发数策略；单文件 1 MiB、单包 8 MiB、文档 2 MiB 与解析节点预算同理。
- 本记录的 C/P 证据不构成模型或真实来源的 S 证据，也不构成远端交付的 G 证据。
