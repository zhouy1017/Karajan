# DG01：Planning／Reviewer 可信执行闭环

目标：完成 #112/#116 的 C/P 原范围，使真实本机 runner 在受控本地服务下从持久意图推进到可信输出、Evidence 和当前 receipt。消费 DG00 修复，不以 fixture 证明 S 或启用生产交付。

## 开始与分解

读原 #112/#116、父 #93/#95 和 [交接队列 A](../commander-handoff-20260908.md#下一批任务队列)，核对已合并控制器、准入、Journal、输入包，先补缺口。Terra 为共享模块唯一作者；Luna 仅消费已经冻结的接口。

每个接口简报写明实际生产者/读取者、受信 ID、持久内容摘要、权限与来源变更、失败/取消/unknown、恢复入口和文件所有权。authority 尚不存在时先做有界实现/接口叶子，再标消费叶子 ready。

## 可观察出口

- Planning：批准只读范围内的 pre-plan repo snapshot 有持久只读生产来源，与原 execution 绑定；模型 payload 包含完整 requirement/acceptance/intent/必要源码，完整计量且超限拒绝。
- transport 保留原 Attempt/fence、启动和逐 send guard、真实 Journal；来源、term、generation、窗口变化或取消阻止下一 effect。输出 adapter 从真实 observer artifact 读取，原 ID 重放不重投，中间/截断/多 final/缺日志不产生计划。
- Reviewer：消费新的完整输入 schema、CAS 和全部最终 Checks；新上下文与作者独立，持久 intent/effect、只读 native、每次发送和精确终止可核查。
- 原 parser、`record_review`、`lookup_evidence` 与 gate 形成精确恢复；当前 receipt 每次重读资格、控制面、Checks+Review。取消/撤销使历史 passed 不再当前，delivery 保持 false/not_run。
- Commander 有限 qualification producer 的缺失 C/P 前置拆为 #113 关联叶子，首个探测使用专属有限准入，不循环依赖已有 Commander 资格；实际 S 在 DG02。

证据：真实 SQLite/Capacity、本地 HTTP fixture、Linux native/进程/隔离的 C/P，逐 effect 接收计数、崩溃重开和取消负例；工具缺失或平台不支持准确标记。独立双审、当前 CI、进入 dev 后按原 AC 关叶子并回填父票。完整父票未满足时保持 Open。

下一步：DG02；#107 可独立预检，#18/#19 前置准备并行。
