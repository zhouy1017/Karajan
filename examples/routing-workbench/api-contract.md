# 固定快照路由模拟 HTTP 切片

本接口把工作台提交的完整 Task、Policy、Capacity 快照直接交给现有 `evaluate_route`。项目只用于确认存在，不提供或补齐快照事实；输入中的授权也仅是本次显式模拟数据，不取得当前 Run 的执行权限。

## 接口

- `GET /v1/projects/{id}/rulebook/simulation-example`：认证后返回固定 `{task, policy, capacity}`。示例与 `examples/routing/fixed-input.json` 原字节相同，19,329 bytes；来源、资格与观察均明确为 fixture，真实资格仍是 `not_run`。示例不能当作真实资格或当前配额。
- `POST /v1/projects/{id}/rulebook/simulate`：提交且仅提交上述三个完整对象。沿用会话、Host、Origin、CSRF、Unicode/JSON 检查及 65,536 bytes 请求上限。无应用操作，不要求 `If-Match` 或幂等命令键。所有层级的重复 JSON 键都会被拒绝，与现有 CLI 保持一致。

成功响应固定为：

```json
{
  "schema_version": "karajan.rulebook-simulation.v1",
  "scope": "explicit_simulation",
  "activation_allowed": false,
  "model_calls": 0,
  "result": "evaluate_route 的完整结构化结果"
}
```

实际 `result` 为对象，包含固定输入、算法和规则身份、候选、淘汰理由、排序分量与资源来源。歧义、T0 未就绪和没有匹配规则均是正常模拟结果，HTTP 200 且无选中 Profile。相同输入的响应正文完全一致，不加入当前时间或随机身份。

输入/求解器校验错误返回 HTTP 422：`{reason_code, issues, activation_allowed:false, model_calls:0}`，只提供错误代码及字段路径，不回显输入值。会话/请求边界错误沿用现有 HTTP 状态和 `{reason_code}`；不存在的项目返回 404。错误和成功都不写预留、命令或发布，也不启动 Host 或模型。

## 实际验证

新增 21 项真实 TestClient 检查。最终 `api-web-routing-final.junit.xml` 是整个 Web 与 routing 的联合结果，**137 passed**；新增源码与最小接线通过 Ruff 和 strict mypy（2 个源文件）。

零调用证据来自真实行为：测试先通过公共接口创建已有 Run 和容量 Admission/Reservation，再对该 HTTP 应用实际存在的 **projects、runs、capacity 三库全部逻辑表逐行比较**。它同时把本机 HTTP 接收器地址写入本次提交的 Profile，更新显式 fixture 资格绑定，让该 Profile 正常被选中；接收器实际收到 **0 次请求**。快照使用与项目当前规则不同的 `supplied-worker`，验证服务器使用提交的规则。未初始化的资源账与 Host 不是本次实测账本范围；模拟模块也不依赖 Broker 或 Host。

最终 JUnit 的 suite properties 保留各表 before/after 行数与 SHA-256，以及本机接收计数。`api-verification.json` 汇集这些证据和源码摘要；响应中的 `model_calls:0` 本身不被当成零副作用证据。

红绿记录保留为新文件：

- `api-initial-before.junit.xml`：1 failed（新接口尚不存在，404）。`api-initial-after.junit.xml`：1 passed。
- `api-duplicates-before.junit.xml`：2 failed、1 passed（重复键被解析为最后一个值）。`api-duplicates-after.junit.xml`：3 passed。
- `api-behavior.junit.xml`：21 passed，为完整 API 行为第一次通过的记录。
- `api-populated-effects.junit.xml`：填充已有 Run/预留后的副作用专项通过；最终联合报告也包含这一验证。

在仓库根目录、项目 Python 环境中重跑：

```text
python -m pytest tests/web/test_simulation_http.py -q
python -m pytest tests/web tests/routing -q
python -m ruff check backend/karajan/web/simulation.py backend/karajan/web/app.py tests/web/test_simulation_http.py
python -m mypy backend/karajan/web/simulation.py backend/karajan/web/app.py
```

本切片未改动路由求解器或 ProjectRegistry，也没有真实账户调用、现金调用或远端请求。唯一新增 routing 文件是固定 JSON 示例。
