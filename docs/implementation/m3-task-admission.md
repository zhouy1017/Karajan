# 已批准任务的可恢复配额预留

`ApprovedTaskAdmission` 将普通 Worker 的可信路由结果接到真实 `CapacityStore.admit()`。它保存操作、Attempt/context 身份、原始判断、完整准入请求、重新验证结果和容量收据。输入只有 Run、Task 或操作 ID；Profile、授权、需求和窗口均由控制器组装。

本片段只做到 **queued → reserved / blocked**，并提供取消和崩溃恢复。它不调用 `CapacityStore.activate()` 或 RunnerHost，不创建工具进程，不启用真实来源。当前生产 `runtime_tools` 仍未获得资格，因而生产入口返回明确阻塞且不预留配额。正向集成测试只替换资格来源，其他批准、估算、配额和操作存储均是真实持久数据库。

## 公共入口

| HTTP | 行为 |
|---|---|
| `POST /v1/runs/{run_id}/tasks/{task_id}/admissions` | 保存路由及准入意图，需要 Idempotency-Key |
| `GET /v1/runs/{run_id}/task-admissions/{operation_id}` | 读取操作，按当前容量事实更新到期等状态 |
| `POST /v1/runs/{run_id}/task-admissions/{operation_id}/advance` | 恢复已发生的准入，或重新验证后首次预留 |
| `POST /v1/runs/{run_id}/task-admissions/{operation_id}/cancel` | 保存取消意图，释放仍未激活的预留 |

所有 POST 正文必须为 `{}`。既有本地会话、Origin、CSRF 和请求体限制继续适用。advance/cancel 用不可变操作 ID 作为幂等身份；它们读取操作的当前状态。enqueue 重放返回该命令最初的收据，当前状态通过 GET 查询。此入口尚未增加工作台按钮或后台自动推进循环。

## 顺序与恢复

1. enqueue 在独立操作数据库中先保存固定身份、判断及请求，提交成功后才允许 advance。相同 Run/Task 有未结束的准入操作时拒绝第二次排队。
2. advance 先通过 `CapacityStore.command_receipt("admit", 原请求, command_key=...)` 只读查询历史结果。缺失不会占用 key、过期预留或重新发送请求；不同请求不能复用同一个 key。
3. 确认没有历史结果后，在 `ApprovedRunRouting.admission_guard()` 内重新读取批准、当前资格、估算与容量。若选中 Profile、批准绑定、完整需求、策略 revision 或窗口发生变化，则阻塞；不为同一个 Attempt 静默换源。
4. 操作锁 → Run 锁 → Project 锁 → Capacity 事务。批准、资格和估算在真实 `admit()` 完成前保持稳定；Capacity 在自己的事务中核对最新余额、共享占用、策略与完整窗口。
5. Capacity 提交后即使控制器进程退出，下一次 advance 也只读取原始结果并补记操作状态。历史结果不代表当前执行许可；返回的 `activation_allowed` 和 `dispatch_enabled` 始终为 false。

操作数据库与 Run、Project、Capacity 数据库必须分离，避免嵌套写锁。旧版 `SerialCoordinator` 的 fixture 执行路径保持原有接口；本片段没有并行引入第二套自治任务图或业务重试策略。

## 取消、到期与未知状态

取消意图在访问容量数据库之前独立提交。取消后即使进程退出，后续 advance 也不会再首次准入。若此前准入已提交但返回丢失，cancel 先读取其历史收据，再执行 `cancel_unactivated()`。

`cancel_unactivated()` 在同一容量事务中仅允许 reserved 转 released，已经到期的未发送预留保持 expired。active 或 unknown 返回 `CANNOT_RELEASE_ACTIVATED_ADMISSION`；这些占用必须经过真实执行核对。调用方不能用一个 `not_sent=true` 字段绕过检查。取消成功但响应丢失也使用只读命令收据恢复。

GET、advance 和新 enqueue 检查现有操作时，从 `CapacityFacts.admissions` 读取当前状态。未激活预留到期显示 expired，并允许同任务重新排队；原始准入收据保留。独立出现的 active/unknown/ended 显示 reconciliation_required，不推导任务完成，也不释放未知占用。普通取消只能取消本模块的预留，不能取消正在运行的 Agent。

## 验证及未完成项

可运行入口：

```powershell
uv run --frozen --extra dev pytest tests/runs/test_task_admission.py tests/runs/test_admission_guard.py tests/web/test_task_admission_http.py tests/capacity/test_command_receipts.py
```

公开用例覆盖真实持久化、重复请求、资格/估算撤销、锁保护、提交后丢失返回、取消与激活竞争、到期以及 HTTP 注入拒绝。独立 Spec 额外使用真正子进程在容量提交后退出，再重新打开数据库恢复。审查发现的“过期后仍显示 reserved 并阻止重新排队”已有修复；原始失败和复验记录与源码摘要一起保存。

当前相关后端回归 603 passed、1 个 Windows 上的 POSIX-only skipped；新增准入/HTTP 用例在 WSL2 为 19 passed。容量模块在 Windows/WSL2 各 123 passed，后端 Mypy 103 个文件和 Ruff 通过。独立结果与可复查记录见 [证据目录](../../examples/task-admission/README.md)。

仍需完成：真实工具资格生产器、统一后台推进、Attempt 执行履历、激活与真正 effect 前的复查、跨进程撤销/取消边界、Reviewer 和质量阶段、费用账本、真实来源观察与端到端交付。预留完成不等于整个 M3、FR08 或平台已完成。
