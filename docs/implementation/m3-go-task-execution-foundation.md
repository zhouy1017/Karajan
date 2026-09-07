# Go Task 执行基础接口

本片使已有 Run/Task 准入、Workspace、Host 与 Go 原生工具执行具备可组合的接口。
它尚未提供一个完整的“批准 Task → 启动可信 runner → Candidate 回写”入口。

`GoExecutionIntents` 把执行状态保存在原 `ApprovedTaskAdmission.operations` 行中，
不创建第二套任务数据库。准备仅接受 run/operation/principal/command 标识；可信控制器
构造时提供实际来源摘要与 Host。它固定原 Attempt/context、Workspace、assessment、
AdmissionRequest、执行范围、预算及稳定 activation/start/grant/cancel key。
历史读取用既有 SQLite 只读连接，不调用刷新、时钟、模型或任何启动接口。

启动过程区分三件事：持久启动意图、Host 启动观察、一次性原生效果 claim。
claim 在真正 native.start 前独立提交，只有首次活调用得到 claim_allowed=true；
返回丢失不回滚 claim，也不产生第二次权限。startup_guard 和 effect_claim_guard 持原
operation 锁，后续消费者仍须按 operation → Run → Project → Capacity → Host 顺序
加入当前业务门禁。旧 activation 回执只用于恢复原标识和 expiry。

Host 的 supervisor 在唯一 Popen 后记录直属命令子进程的 PID/birth。
wait_for_runner_registration 在业务锁外等待这个只读握手，不授予权限。
current_runner_guard 不接 caller PID：它取当前调用进程的真实身份，与登记值精确比较，
并核对当前 Host fence、活体 supervisor 和实际进程组成员。孙进程不能冒用直属 child。
Windows venv python.exe 可能是额外启动器；测试使用真实解释器验证直属进程。
同组终止时子进程退出，或仍存活但失去 supervisor 证明，两者均不能产生新效果。

`build_task_input` 重新验证 Workspace 双摘要、Plan/Approval/ExecutionPolicy 的绑定，
再从完整 CAS 基线计算获准文件集合。输入来自固定内容，不重新读取用户的工作树。
第一版要求 Task 精确使用 read/edit；没有实现动态收窄原生工具集时，较窄的工具要求
明确拒绝。prompt 包含批准需求和任务，超过 8192 字符拒绝，时限使用批准 Task 的
duration_seconds。输入字节、prompt 和 credential 均不进入公开 repr 或结果报告。

`task_host_manifest`、`task_host_activation`、`task_grant_binding` 与
`task_relay_context` 从原 operation 编译固定标识。它们不读取密钥，也不是授权接口。
`task_runner_source` 分别绑定资格机制、Task producer 和控制器来源。Task runner 来源
变化使旧 execution intent 的来源不再适用；资格机制来源变化使旧资格记录不再匹配。
后续加入固定 entry、实际消费者和 Collector 时必须扩展
来源清单，再使用动态当前来源检查启动及每次发送。

`execute_go_task` 是内部原生 producer。新接口强制要求 start_native/send_guard，
实际 Task 文本经固定 read/edit namespace 执行，每次请求使用批准参考计量限制。
GoRelay 的发送门禁在 relay condition 外获取，覆盖 begin_call 与 HTTP stream 进入；
响应正文流式读取不持续持有业务数据库锁。取消先短事务保存意图，再释放锁并撤销
grant、停止 Host/relay，不持 operation 锁等待正在申请该锁的 handler。
producer 仅在实际停止可确认时返回 StoppedProjection；否则可返回 None/unknown，
报告不包含原始模型历史。
Candidate 验收、验证、Review 和 PR 状态由后续可信消费者处理。

本片测试使用临时 Git、真实 SQLite、真实本地进程和 Linux 原生 OpenCode；上游是
显式本地 HTTP fixture。合成资格只供接口边界测试，不能作为部署资格。
已验证普通任务文件的 read/edit 与首个请求后撤回，不将其等同于批准 Run 的完整执行。
CI 错误按当前[修复分工](testing-gates.md#ci-失败的修复分工)派发，独立功能开发继续。
