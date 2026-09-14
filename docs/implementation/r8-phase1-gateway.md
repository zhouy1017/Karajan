# R8-P1-01 外置网关目录与无推理目录探测

实现日期：2026-09-14。本记录对应 [#175](https://github.com/zhouy1017/Karajan/issues/175)，父票 [#174](https://github.com/zhouy1017/Karajan/issues/174)。设计依据 [外置网关](../architecture/08-provider-gateway.md) 与工作台 PRD FR21/UX-AC15。

本切片交付可调用、持久化且可恢复的**控制面**：项目内登记网关连接与不可变模型绑定 revision、认证与幂等/归属边界、以及一次显式的无推理目录探测。它不连接真实 provider、不做推理、不做自动换源，也不取得任何真实来源资格。

## 1. 模块与接口

| 路径 | 作用 |
|---|---|
| [`karajan/gateway/models.py`](../../backend/karajan/gateway/models.py) | 连接/绑定声明、地址规范化、凭据形状校验、协议发现路径 |
| [`karajan/gateway/catalog.py`](../../backend/karajan/gateway/catalog.py) | 复用 `ProjectRegistry` 事务/归属/幂等账本的追加式目录存储 |
| [`karajan/gateway/probe.py`](../../backend/karajan/gateway/probe.py) | 单次有界目录读取、受信 `secret_ref` 解析、结构化观察 |
| [`karajan/web/gateways.py`](../../backend/karajan/web/gateways.py) | 认证 HTTP 路由，注册进现有 `create_app` |

目录表与项目库同库（`projects.sqlite`），因此归属、`commands` 幂等账本和 `BEGIN IMMEDIATE` 都沿用既有原语，没有第二套存储或第二套认证。`create_app` 新增可选 `gateway_secret_resolver`；未提供时任何带 `secret_ref` 的连接都在**发起请求之前**返回明确缺口。

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
```

命令主体固定为 `owner`（与既有路由一致），并由 `project_owners` 校验；跨项目引用不可见，因此不会被当作“存在但无权”。

## 2. 逐项验收

| 原验收条件 | 实现与行为证据 | 层级 |
|---|---|---|
| AC1 创建/读取连接与不可变绑定 revision；校验归属与引用版本；重启读回相同对象与摘要 | `tests/web/test_gateway_http.py::test_connection_and_binding_revisions_survive_restart_and_keep_old_references`：创建 → 发布 revision 2 → 旧 revision 1 仍可读回同一对象，重新 `create_app` 后仍相同 | C |
| AC2 幂等键绑定主体、CAS/expected revision、同键同载荷返回原结果、同键异载荷冲突、旧版本冲突、未知项目/跨项目拒绝、未认证与错误 Origin/CSRF | 同上；`test_write_commands_require_identity_cas_and_ownership`；`test_unauthenticated_and_wrong_origin_writes_keep_existing_rejection_semantics` | C |
| AC3 保存 base URL、协议/版本标识与 `secret_ref`；凭据由可信 resolver 解析；不接受/返回明文 credential；不在响应/错误/日志回显 secret；严格绑定 gateway revision、alias、provider/account/计费与变换策略；声明身份与核验证据分开 | `test_credentials_are_never_accepted_persisted_or_echoed`（含直接读取 SQLite 断言无 sentinel）、`test_a_credential_echoed_by_the_upstream_never_reaches_the_client`、`test_binding_declares_identity_that_stays_separate_from_verification`、`tests/gateway/test_gateway_declared_identity.py` | C |
| AC4 通过所登记连接获取 catalog、禁止重定向到另一来源、不推理/不自动 fallback；受控本机 HTTP 上游证明请求路径与恢复结果；网络错误/认证失败/未知保持结构化；`/models` 可见不授予执行资格；未配置 `secret_ref` 返回明确缺口 | `test_catalog_probe_reads_only_the_registered_discovery_surface`、`test_probe_failures_are_structured_and_never_follow_a_redirect`、`test_network_failure_and_unreachable_upstream_are_reported_not_raised`、`test_unconfigured_secret_ref_is_an_explicit_gap_without_any_request`、`test_probe_uses_a_real_ipv6_loopback_authority`、`test_unregistered_discovery_paths_cannot_be_requested` | C |
| AC5 HTTP/持久版本/幂等/CAS/归属/凭据不泄漏/probe 失败测试；Ruff/mypy/既有快速门 | 见第 4 节实际命令与结果；可复现样例见第 3 节 | C/P |

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
.venv/Scripts/python.exe -m pytest tests/gateway tests/web   -> 198 passed, 4 skipped
.venv/Scripts/python.exe -m pytest tests/routing/test_authorization.py \
  tests/projects/test_qualification_store.py tests/runs/test_admission_guard.py \
  tests/web/test_task_admission_http.py tests/tools/test_ci_quality_gate.py -> 54 passed
```

`tests/projects` 全量另有一次 `REPOSITORY_ROOT_REQUIRED` 与 `REPOSITORY_INVALID` 不一致的失败；该用例在未包含本票改动的 `HEAD` 上同样失败（`projects/registry.py` 未被本票修改），属既有基线失败，保留原样。

## 5. 边界与未覆盖范围

- 不实现推理、工具循环、账户登录或网关管理 API；探测只对 `openai_compatible` 的 `/v1/models` 发一次 `GET`。
- 目录可见、别名存在或绑定已登记都不构成 dispatch/execution 资格；所有记录固定 `execution_eligible=false`。
- 声明身份与核验证据分开存储：`declared_identity=true` 时 `verified=false`、`verification_evidence=null`、provider/account/billing 观察值为 `unknown`。
- 不修改现有来源资格、已批准 Run、旧 Profile schema，也不新增任何 Agent 数量默认值。
- 无真实 provider、凭据或付费调用；本记录的 C/P 证据不构成 S 证据。
