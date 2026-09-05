# M0-02：本地 RunnerHost 探针

本实现对应 [M0-02 / #3](https://github.com/zhouy1017/Karajan/issues/3)，只管理可信本地 Python 夹具的启动身份、进程生命周期与迟到事实。它不调用模型、不读取登录凭据、不进行现金 API 测试，也不实现完整 Run、DAG、路由或交付。

## 公共入口与持久边界

Python 入口为 `karajan.execution.RunnerHost`，复用 `contracts.probe.AttemptManifest`。状态目录包含 SQLite 登记和本地执行日志。以下接口属于可信协调器；不会作为模型工具或无需认证的 Web 管理接口公开。

| 入口 | 行为 |
|---|---|
| `prepare(manifest, start_key, ProcessSpec)` | 校验本地命令、目录和有限正超时；登记规范化 manifest/进程配置摘要。同键同内容重用 prepared identity，同键异内容或同 Attempt 另一键冲突 |
| `set_control(attempt_id, fence, authorization_ref, dispatch_enabled)` | 写入当前可信控制快照，拒绝 fence 倒退；此处不自行判断用户是否已授权 |
| `start(prepared_id, Activation)` | 绑定许可摘要，检查 Attempt/fence/授权/预算引用、暂停状态与有效期；持久接受后，在 spawn 前再次事务核验当前控制 |
| `inspect(attempt_id)` | 报告进程身份、启动阶段、当前物理状态、业务结果状态、消费观察；无法证明的状态为 unknown |
| `cancel(attempt_id, cancel_key, timeout_seconds=3)` | 持久撤销后请求停止，在有限时间内查询 OS 事实；confirmed 必须有本地停止证据，否则 unknown |
| `receive_result(attempt_id, fence, event_id, result)` | 当前执行的首次有效结果可标记业务 done；失效 fence、授权不匹配或取消后结果拒绝。重复事件返回其已保存裁决，不重新应用业务效果 |
| `record_usage(attempt_id, fence, event_id, usage)` | 不因旧 fence/业务终态丢弃已发生事实；相同事件幂等，异载荷冲突，新事实重新打开核对 |
| `settle_usage(attempt_id, through_sequence)` | 由可信调用方确认所见消费事实已核对；序号变化拒绝旧结算。它不是服务商账单结清证明 |
| `reconcile()` | 保留业务未结束、物理未退出或消费未核对的 Attempt；业务 done 不会隐藏活跃子进程 |
| `observe_process(ProcessIdentity)` | 只读比较 PID 与创建身份；返回 running/exited/identity_mismatch/unknown，不对不匹配 PID 执行终止 |

`ProcessSpec` 固定 argv、绝对工作目录和超时，不使用 shell 拼接。启动许可只保存引用与摘要；不要把秘密值放进 argv 或这些 JSON 文档。状态目录应放在工作区之外。当前同用户原型没有阻止工具读取该目录的 OS 安全隔离；此限制必须在 M0-06 解决，不能把“不提供管理 API”当作文件访问已经隔离。

Activation、控制写入与结果/消费观察复用公共身份字段的严格校验；布尔值不能冒充 fence，字符串不能冒充 dispatch 开关，空身份不能进入启动许可。CLI 的 JSON 输入遵守同一边界。

## 启动与恢复

准备记录先持久化；`start` 对启动键排他接受并固定 activation 摘要，再持久化激活决定，随后只派生一次独立 supervisor。Supervisor 先建立本地进程组并登记自己的 PID/创建身份，再运行夹具命令。调用方写入的 acknowledged 只表示本地派生回执，不表示模型已经接受配置或工作已经完成。

SQLite 提交与 OS spawn 不可能原子合并。`after_accept`、`before_spawn`、`after_spawn`、`after_ack` 是显式故障点；Python 入口抛 `ProbeCrash`，独立 CLI 在该点以 `os._exit(91)` 真正退出，跳过正常清理。新 RunnerHost 对同一启动身份只核对，不再次 spawn。已启动的 supervisor 可以自行登记，补足调用方丢失的回执；不能证明是否启动的记录保持 unknown。

未派生任何进程的故障案例也可能保留 unknown：这是刻意保守的接口结果，并非声称已有进程。当前探针没有“强行忘记并重启”命令；实际接管或释放写占用需由后续控制器核对。恢复此目录属于同一安装日志的重新打开，不实现历史备份恢复或跨机器迁移。

## 两种 OS 路径与限制

Windows 使用命名 Job Object；supervisor 先把自己加入 job，普通 CreateProcess 子进程随后继承该 job，不启用 breakaway。查询 job 成员得到实际进程列表，取消使用 TerminateJobObject 后再查询是否已空。PID 身份另绑定 GetProcessTimes 的创建时间。Named Job 的生命周期使调用方退出后仍可核对有关进程。[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)

Linux 使用独立进程组和 `/proc` 观察，PID 身份绑定 boot ID 与进程启动 ticks；僵尸进程不当作仍可执行的进程。取消由仍存活的 supervisor 读取撤销记录后终止自己的进程组，避免恢复端仅凭一个可复用 PID 盲目发信号。Supervisor 已丢失且还有无法确定归属的进程时，结果保持 unknown，不声称可以可靠接管。Python 的 session/进程组设置依据官方 subprocess 接口。[Python subprocess](https://docs.python.org/3.12/library/subprocess.html)

这些机制只验证生命周期，**不是文件、网络或恶意代码沙箱**。Linux 子进程主动创建新 session、Windows 经外部服务代启动进程、宿主管理员操作、WSL 互操作及原生 Agent 工具守卫均未由此证明安全。`remote_stop` 始终为 unknown；本地停止不推出远端推理已停止或会退款。真实 CLI/API Profile 仍须 M0-06/07 分别验收。

## 可复现 CLI

在仓库根目录、Python 3.12 开发环境中，选择一个尚不存在的演示目录。准备工具只生成合成输入，许可有效期为 5 分钟；超时或重新演示时使用新目录，不覆盖旧执行记录。

```text
uv run --frozen --extra dev python examples/runnerhost/make_inputs.py --directory .local/runnerhost-demo
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state prepare .local/runnerhost-demo/prepare.json
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state control .local/runnerhost-demo/control.json
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state start runnerhost-demo-start .local/runnerhost-demo/activation.json
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state inspect attempt-fixture-1
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state cancel attempt-fixture-1 demo-cancel
uv run --frozen --extra dev python -m karajan.execution --state .local/runnerhost-demo/state reconcile
```

夹具父进程启动一个写 heartbeat 的子进程后立即退出，运行上限为 5 秒。停止后 `workspace/heartbeat.txt` 不再增长。即使命令自然结束，未核对的业务/消费状态仍可出现在 reconcile 中。

CLI 的普通零退出码表示命令已处理，不代表运行时资格通过；取消未确认退出码为 2，输入或命令被拒绝为 1，显式崩溃为 91。输出始终包含 `live_qualified: false`。在 start 命令末尾加 `--crash-at after_spawn` 等选项可复现相应窗口；故障后去掉选项重放同一输入，不能增加一次执行。

## 已执行验证与未完成资格

2026-09-05，在当前 Windows 本机、Python 3.12.14 上通过 44 项公共入口测试。测试先后经历真实红/绿循环，覆盖：同键内容绑定、当前许可拒绝条件、同键并发、父进程先退后的子树取消、四个函数故障点与四个 CLI 硬退出窗口、PID 创建身份不符、业务 done 后仍恢复、串行/并发仅首次有效结果获准、取消/旧 fence 迟到结果、迟到消费幂等与重新核对、无取消请求时的执行期限、非有限限额、错误 argv、身份类型强转及空身份拒绝、禁用垃圾回收时连续 200 次核对的 OS 句柄稳定性。独立演示入口也已实际执行，观察到 3 个 job 成员；取消返回 confirmed 后 heartbeat 停止增长。

```text
uv run --frozen --extra dev pytest tests/execution
uv run --frozen --extra dev ruff check backend/karajan/execution tests/execution examples/runnerhost
uv run --frozen --extra dev mypy backend/karajan/execution
```

| 范围 | 当前证据 |
|---|---|
| Windows 本地可信 Python 进程生命周期 | 已执行上述测试；不等于工具沙箱资格 |
| PID 重用防护 | 用真实活跃 PID 与不匹配创建身份验证拒绝归属；没有强制造成 OS 实际 PID 重用 |
| Linux 进程组 | 实现并通过 Linux 平台类型检查；运行证据待 Linux CI，当前不计 passed |
| WSL2/容器/原生 Agent 工具 | not_run，属于后续部署与执行器资格 |
| 订阅/官方 API/第三方 API | not_run；本票没有账号调用 |
| 现金硬预算/供应商取消/最终账单 | 未由本票实现或证明，消费表只是事实登记 |

实现没有新增第三方依赖。此结果只完成 M0-02 的本地原语范围，不能关闭整个 M0 真实出口或 PRD。
