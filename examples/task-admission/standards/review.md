# Task admission Standards review

结论：0 项可操作 finding。范围仅为另一作者的 admission 协调器、routing guard、Web admission 与 app 接线；排除本审查者自己编写的 Capacity receipt/cancel 实现。Spec 由独立审查负责。

锁顺序是 admission journal → Run → project → Capacity；前置 owner 检查释放 Run 读锁后才进入 journal。共享 `_build` 保持资格/预测来源检查一致，guard 的 ExitStack 将当前项目事实锁持续到消费结束。先落盘 queued/cancellation intent，再跨库执行；丢失返回后读取不可变命令收据，既不重发也不重新授权已有副作用。代码没有宣称跨库原子性或实际执行权限。未发现需报告的基线代码异味。

公开路径复跑 39 条独立用例全部通过，包括真实持久命令链、参与者无 owner 权限、三类 HTTP 命令禁止注入 authority、session/CSRF、来源 DB 分离、估算撤销在 guard 内等待、撤销/到期后的丢回执恢复、幂等历史与新操作身份。新增 `_refresh` 也验证了 get/advance/enqueue 的到期投影及外部 released/active/unknown 状态；读取投影不会伪造容量账本副作用。

正例的 qualification 和规划 admission 来源明确是测试替身；没有将它们报告为真实资格。所有 operation 均保持 activation/dispatch 为 false。通过结论只覆盖预留协调与恢复，不涵盖真实 Host、provider、现金预算准入或完整 M2/M3 交付。两条既有 Starlette/httpx 弃用警告不影响测试通过。
