# R8-P1-01 外置网关目录与无推理目录探测

实现日期：2026-09-14。本记录对应 [#175](https://github.com/zhouy1017/Karajan/issues/175)，父票 [#174](https://github.com/zhouy1017/Karajan/issues/174)。设计依据 [外置网关](../architecture/08-provider-gateway.md) 与工作台 PRD FR21/UX-AC15。

本切片交付可调用、持久化且可恢复的**控制面**：项目内登记网关连接与不可变模型绑定 revision、认证与幂等/归属边界、以及一次显式的无推理目录探测。它不连接真实 provider、不做推理、不做自动换源，也不取得任何真实来源资格。

## 1. 模块与接口

| 路径 | 作用 |
|---|---|
| [`karajan/gateway/models.py`](../../backend/karajan/gateway/models.py) | 连接/绑定声明、地址与主机名校验、可寻址身份、凭据形状参数校验、协议发现路径 |
| [`karajan/gateway/catalog.py`](../../backend/karajan/gateway/catalog.py) | 复用 `ProjectRegistry` 事务/归属/幂等账本的追加式目录存储、内容摘要、探测 claim 与恢复 |
| [`karajan/gateway/probe.py`](../../backend/karajan/gateway/probe.py) | 单次有界目录读取、受信 `secret_ref` 解析、结构化观察 |
| [`karajan/web/gateways.py`](../../backend/karajan/web/gateways.py) | 认证 HTTP 路由，注册进现有 `create_app` |

目录表与项目库同库（`projects.sqlite`），因此归属、`commands` 幂等账本和 `BEGIN IMMEDIATE` 都沿用既有原语，没有第二套存储或第二套认证。`create_app` 新增可选 `gateway_secret_resolver`；未提供时任何带 `secret_ref` 的连接都在**发起请求之前**返回明确缺口。

探测采用**两段短事务 + 持久 claim**：第一段事务写入 `gateway_probe_claims` 与同键的 `commands` 预留标记，网络读取在事务之外执行，第二段事务原子写入观察、回执并把 claim 置为完成。因此：

- 一个项目的慢/不可达上游不会阻塞另一项目的目录读写；
- 探测在途时同键被占用：同键探测返回 `pending`（无 `observation_id`），其他命令复用同键得到 `IDEMPOTENCY_CONFLICT`，都不会覆盖写；
- 中断/重启后 claim 超过 TTL 时，同键重试得到一笔 `outcome_unknown` 观察（`requests_sent` 为 `unknown` 而非 0）并保留该回执；同键重放返回同一结果，不重新发起请求；明确重试需使用新键；
- 在途状态经 `GET .../gateway-probe-claims` 可读回核对。

```text
GET  /v1/projects/{id}/gateway-connections
GET  /v1/projects/{id}/gateway-connections/{connection_id}/revisions/{revision}
POST /v1/projects/{id}/gateway-connections                     Idempotency-Key
PUT  /v1/projects/{id}/gateway-connections/{connection_id}     Idempotency-Key + If-Match
GET  /v1/projects/{id}/gateway-bindings
GET  /v1/projects/{id}/gateway-bindings/{binding_id}/revisions/{revision}
POST /v1/projects/{id}/gateway-bindings                        Idempotency-Key
PUT  /v1/projects/{id}/gateway-bindings/{binding_id}           Idempotency-Key + If-Match
POST /v1/projects/{id}/gateway-connections/{connection_id}/revisions/{revision}/catalog-probes
GET  /v1/projects/{id}/gateway-catalog-observations
GET  /v1/projects/{id}/gateway-probe-claims
```

命令主体固定为 `owner`（与既有路由一致），并由 `project_owners` 校验；跨项目引用不可见，因此不会被当作“存在但无权”。

## 2. 逐项验收

以下每项都经独立审查并附修复后的行为回归。

| 原验收条件 | 实现与行为证据 | 层级 |
|---|---|---|
| AC1 创建/读取连接与不可变绑定 revision；校验归属与引用版本；重启读回相同对象与摘要 | `test_gateway_review_fixes.py::test_public_objects_carry_a_stable_canonical_content_digest`：每个公开对象携带自身规范化内容摘要（摘要覆盖除 `digest` 字段外的全部内容，不自引用），create/revise/GET/list/幂等重放一致，改版生成新摘要且旧引用不变，重启后相同 | C |
| AC1 记录完整性在读取与列表上一致校验 | `test_tampered_stored_record_is_refused_on_read_and_list`：直接篡改 SQLite 行后，单条读取与列表都返回 `GATEWAY_RECORD_CHANGED` | C |
| AC1 可寻址身份 | `test_unaddressable_connection_identities_are_rejected_before_persistence`（`group/gateway`、`group%2Fgateway`、`a b`、`.`、`..` 全部 422 且无持久化行）、`test_addressable_connection_and_binding_identities_round_trip`；别名/`secret_ref` 保留更宽语法见 `test_model_alias_and_secret_ref_keep_their_wider_grammar` | C |
| AC2 主体绑定 Idempotency-Key、CAS、同键同载荷返回原结果、同键异载荷与旧版本冲突、未知项目/跨项目拒绝、未认证与错误 Origin/CSRF | `test_gateway_http.py::test_write_commands_require_identity_cas_and_ownership`、`test_unauthenticated_and_wrong_origin_writes_keep_existing_rejection_semantics` | C |
| AC2 探测期间命令键全局保留；同键竞态不重复发送、不静默覆盖 | `test_gateway_probe_isolation.py::test_same_key_race_never_sends_a_duplicate_probe`：探测在途时同键返回 `pending`（无 `observation_id`、`requests_sent` 未知），另一命令复用同键得到 `IDEMPOTENCY_CONFLICT`，上游仅一次请求 | C |
| AC2/AC4 网络 I/O 期间不持有项目写事务 | `test_gateway_probe_isolation.py::test_a_paused_probe_does_not_block_an_unrelated_project`：受控慢速本机 HTTP 挂起 A 的探测时，B 项目的读与写都先完成 | C |
| AC2/AC4 中断/重启的在途探测显式可核对，不静默重发 | `test_gateway_probe_isolation.py::test_interrupted_claim_is_reconciled_without_resending`：经真实公共路径开 claim+commands 预留后中断，重建 store 并推进时钟过 TTL，同键重试得到一笔 `outcome_unknown` 回执/观察，零重复上游请求，随后同键重放一致；TTL 之前为 `pending` | C |
| AC3 连接保存 base URL、协议/版本标识与 secret_ref；凭据由可信 resolver 解析；不接受/返回明文 credential；不回显 secret | `test_gateway_http.py::test_credentials_are_never_accepted_persisted_or_echoed`、`test_a_credential_echoed_by_the_upstream_never_reaches_the_client`、`test_gateway_review_fixes.py::test_credential_shaped_parameter_names_are_refused_over_http`（含 `token_auth`、`auth_token`、`accessToken` 等重排/紧凑变体；无响应/库/重启回显）、`test_legitimate_model_parameters_still_persist_and_survive_restart` | C |
| AC3 声明身份与核验证据分开；严格绑定固定 gateway revision/alias/声明 provider/账户/计费/变换策略 | `test_binding_declares_identity_that_stays_separate_from_verification` | C |
| AC4 通过所登记连接获取 catalog；禁止重定向；不推理/不自动 fallback；受控本机上游证明请求路径与结果；失败结构化；`/models` 可见不授予执行资格；未配置 secret_ref 为明确缺口 | `test_catalog_probe_reads_only_the_registered_discovery_surface`、`test_probe_failures_are_structured_and_never_follow_a_redirect`、`test_network_failure_and_unreachable_upstream_are_reported_not_raised`、`test_unconfigured_secret_ref_is_an_explicit_gap_without_any_request`、`test_probe_uses_a_real_ipv6_loopback_authority`、`test_unregistered_discovery_paths_cannot_be_requested` | C |
| AC4 非法/不可连接 authority 在登记时拒绝，构造阶段失败结构化 | `test_gateway_review_fixes.py::test_malformed_upstream_authority_is_refused_at_registration`、`test_unconnectable_stored_authority_yields_a_structured_observation` | C |
| AC5 覆盖上述 HTTP/持久版本/幂等/CAS/归属/凭据不泄漏/probe 失败；Ruff/mypy/既有快速门；可复现 API 样例 | 见第 3、4 节 | C/P |

## 3. 可复现 API 样例

```text
# 1) 认证(使用一次性本地 bootstrap 文件中的 token)
POST /v1/session/bootstrap            {"token": "<bootstrap>"}
# 2) 登记回环连接(仅 http 回环,或显式 TLS + 数据去向)
POST /v1/projects/<id>/gateway-connections
     Idempotency-Key: connection-1
     {"connection_id":"local-gateway","base_url":"http://127.0.0.1:8790",
      "protocol":{"family":"openai_compatible","protocol_version":"v1"},
      "secret_ref":"secret:local-gateway",
      "request_transformation":{"policy_id":"no-transform","revision":1}}
# 3) 绑定固定版本与声明身份
POST /v1/projects/<id>/gateway-bindings
     Idempotency-Key: binding-1
     {"binding_id":"vendor-binding","connection_id":"local-gateway","connection_revision":1,
      "model_alias":"vendor-model-a",
      "declared":{"provider_id":"vendor","account_id":"vendor-account",
                  "billing_path":"subscription_only","required_parameters":{"temperature":0.2}},
      "request_transformation":{"policy_id":"no-transform","revision":1}}
# 4) 无推理目录探测(读取派生的 /v1/models)
POST /v1/projects/<id>/gateway-connections/local-gateway/revisions/1/catalog-probes
```

实际运行步骤（本机受控上游 + 真实 HTTP 客户端）见 `examples/gateway/README.md`。

## 4. 本机验证

```text
uv lock --check                                              -> Resolved 46 packages / pass
.venv/Scripts/python.exe -m ruff check .                     -> All checks passed!
.venv/Scripts/python.exe -m mypy backend/karajan             -> Success: no issues found in 170 source files
.venv/Scripts/python.exe -m pytest tests/gateway tests/web   -> 234 passed, 4 skipped
.venv/Scripts/python.exe -m pytest tests/projects \
  -o "pythonpath=backend tests/web tests/runs tests/projects tests/adapters/opencode"
                                                             -> 367 passed, 31 skipped
.venv/Scripts/python.exe -m pytest tests/routing/test_authorization.py \
  tests/projects/test_qualification_store.py tests/runs/test_admission_guard.py \
  tests/web/test_task_admission_http.py tests/tools/test_ci_quality_gate.py -> 54 passed
```

测试经 `--basetemp=<新唯一目录>` 指向 Git 检出**之外**的绝对路径。

### 关于一次被更正的历史失败记录

本记录早先版本把 `tests/projects/test_registry.py::test_invalid_project_identity_never_creates_a_project[not-git-REPOSITORY_INVALID]`（实际抛 `REPOSITORY_ROOT_REQUIRED`）记为"既有基线产品缺陷"。独立复核证明该判断是错的：当时 `--basetemp` 位于 Git worktree **内部**，`git rev-parse --show-toplevel` 因此向上找到 worktree 祖先并返回成功，用例的"非 Git 目录"前提被破坏。把 basetemp 放到 Git 检出之外后，四种变体全部通过。

更正后的结论：这是**测试环境位置问题**，不是产品缺陷，也不构成本票修改 `projects/registry.py` 的证据。原始失败输出保留在上文引用的历史记录与 CI 日志中，不被改写或删除；本节仅说明更正。

## 5. 边界与未覆盖范围

- 不实现推理、工具循环、账户登录或网关管理 API；探测只对 `openai_compatible` 的 `/v1/models` 发一次 `GET`。
- 目录可见、别名存在或绑定已登记都不构成 dispatch/execution 资格；所有记录固定 `execution_eligible=false`。
- 声明身份与核验证据分开存储：`declared_identity=true` 时 `verified=false`、`verification_evidence=null`、provider/account/billing 观察值为 `unknown`。
- 凭据形状参数名采用**歧义即拒绝**策略：`header_count` 这类仅提及受保护名词的名称也被拒绝，因为名称本身无法把它与 `header_value` 区分（`token_limit`、`max_tokens` 等常规参数不受影响）。
- claim TTL 目前是构造参数默认 300 秒；过期后的自动核对发生在**下一次同键请求**时，本切片没有后台清理任务。
- 不修改现有来源资格、已批准 Run、旧 Profile schema，也不新增任何 Agent 数量默认值。
- 无真实 provider、凭据或付费调用；本记录的 C/P 证据不构成 S 证据。
