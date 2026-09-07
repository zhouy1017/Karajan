# 实现切片｜Go Task 的持久执行意图、受控输入与原生执行边界

Parent: #21
Related: #13, #23, #24, #25
Depends on: #87（PR #88）

## 范围

将已预约的 Task 和已冻结 Workspace 绑定到原 operation 中的持久执行意图；增加真正直属 runner 进程的身份核验、每次发送的业务门禁接口，以及消费批准文件快照的原生 Task producer。保持当前资格事实与 Task 实际执行、候选验收的区别。

## 行为验收

- [x] 准备意图仅消费原预约和 Workspace，固定 Attempt、上下文、来源、预算与启动/grant key，不激活配额、不启动进程、不读取真实 provider key。
- [x] 一次性 effect claim 独立提交；同进程或重开后重放均不重新发放。取消通过原 admission 入口持久化，未知执行不会被改成已取消或释放资源。
- [x] Host 仅允许 supervisor 实际登记的直属 ProcessSpec child（PID + birth）进入 runner guard，同组孙进程被拒绝；每次核当前 fence、活体 supervisor 和实际 containment。
- [x] 原生 Task producer 强制依赖 start_native 和 send_guard。发送 guard 覆盖持久 begin_call 至真实发送；拒绝不占调用槽，已发送未知不退款、不重发。
- [x] 输入编译器从批准 Workspace 和完整 CAS 基线恢复文件，重新核对 Plan/Approval/Policy、读写范围及 read/edit 工具范围；原仓库变化不改变输入，超长提示拒绝而非截断。
- [ ] 公共离线接口测试、真实 Linux namespace + 本地 HTTP fixture、独立审查完成，当前 PR head 的必需 CI 通过后按仓库流程验收。

## 实测口径

本片原生 producer 的本地 HTTP fixture 验证真实 read/edit 和实际停止后的 StoppedProjection，不产生官方资格。当前片没有新增官方 API 调用，也不把合成资格 fixture 标为真实通过。历史 #87 官方报告保留原源码绑定；生产来源变化后必须重新资格。

## 接下来的完整接线

另一个实现切片负责固定可信 runner entry/facade：原 activation receipt 的恢复、实际 child 启动、完整 operation→Run→Project→Capacity→Host guards、单次 grant 创建/未知恢复、真实 CandidateStore 收集与结果回写、取消收尾和只读恢复入口。这些尚未完成的行为不能由本片接口测试替代。

独立验证环境、Reviewer、PR 交付、其他服务资格及父票的全链路完成条件保持未完成。关闭本切片不关闭父票；合并由所有者决定。
