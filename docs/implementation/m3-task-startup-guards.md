# 已预留任务的启动前复查

同一个 Task 预留容量后，不能再按“新任务”执行一次完整路由，否则自己的额度与槽位会被重复扣算。本切片固定原 assessment 的 Profile、Attempt 和 Context，复查当前授权和来源；最新容量由原 reservation 的 guard 检查。它仍不创建 Agent 进程，也不授予真实 Task 执行资格。

## 固定 Profile 与原授权

`evaluate_reserved_profile` 复用原路由的规则、阶段、组、角色、难度、资格、工具、独立性、预算、持续时间与需求向量检查。它只检查指定 Profile，不排序、不换模型、不修改容量快照。实时并发、余额、冷却和观察可用性由 Capacity 复查，报告明确标记 `quota_revalidation_required=true` 与 `activation_allowed=false`。原 `evaluate_route` 的行为和输出形状保持兼容。

`ApprovedRunRouting.reserved_execution_guard(run_id, assessment_id, principal=...)` 从持久 assessment 读取原选中 Profile 和计划身份，随后持有 Run、Project 的事务直到消费者退出。当前批准、执行政策、完整任务快照，以及原 Profile 的资格与预测记录必须一致。预测即使金额相同，提高 revision 也不能被旧 Attempt 静默采用。未取得 Task scope 资格的真实来源继续阻塞。

这是内部消费者端口，不能单凭它推断 reservation、Workspace 或启动权限已存在。后续执行器还须持有原 operation，核对未取消状态、Workspace、原请求与实际运行来源，再进入容量和进程边界。

## 历史激活与最新容量

`CapacityStore.command_receipt("activate", ...)` 只读原命令结果，不读取新时间、不修改状态、不产生激活；丢失响应后从原回执恢复。历史成功不是新的启动许可。

`CapacityStore.pre_effect_guard(admission_id, expected_request=...)` 要求原激活意图已经提交，request 包含完整 policy/window/Commander 保护绑定并与存储一致，状态仍为 active 且原期限未过。它在同一个 `BEGIN IMMEDIATE` 内检查最新 policy、窗口、观察、消费、其他预留和槽位，贯穿消费者的短启动边界。内部连接使用 query-only，不写回执、不退款、不把 unknown 改回 active。

检查只排除本 admission 的预留。其他 reserved、active、unknown 与尚未被覆盖的消费仍占容量；只有已经过期且从未激活的 reserved 可从有效占用中排除。调用方抛异常不会回滚此前已提交的 activation。数据库事务约束受控 Capacity 写入，无法锁住时钟或 provider 的远端状态；执行器应紧邻实际 effect 检查，不长期占锁后再启动。

推荐顺序为 operation → Run → Project → Capacity。先提交固定命令的 activation，再进入最新 pre-effect guard。不能在一个可回滚事务里先写 activation、同时启动进程，并在失败时抹掉启动意图。

## 验证与后续执行入口

Windows 与 WSL 的全部受影响 Capacity、Routing、Run 测试各通过 393 项。独立验收覆盖 31 项容量边界和 10 项批准来源边界。真实存储的贯通用例在三个 Worker 槽位均被预留时，先确认重复的新任务路由会阻塞，再确认固定原 Profile 和原 reservation 可以通过复查，另外两个任务的占用仍保留。没有调用真实 provider 或启动模型进程。

下一步使用固定受信 Go runner 消费这些端口。`RunnerHost.start` 返回时后台 supervisor 可能尚未创建实际 native 进程，业务 guard 必须放在受信 runner 的真实 `IsolatedOpenCode.start` 边界，不能只包住父进程的 Host 调用。恢复先核对原 Host 身份和发送账本，未知状态不重发。

资格准备也须先于 Task 预留：在独立资格工作区验证受控文件投影、上下文计量和可信候选收集的机制，实际 Task 再由批准 Workspace 给出具体路径。不能让资格反过来依赖只能在 reserved 后准备的 Workspace，也不能把旧固定 Go 场景改名当作完整 Worker 资格。
