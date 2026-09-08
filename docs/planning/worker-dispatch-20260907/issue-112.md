Parent: [#93](https://github.com/zhouy1017/Karajan/issues/93)。

负责 worker：gpt-5.6-terra；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前为后续批次，依赖按实际发布关系核验。

承担 #93 B 的可独立 C/P 实现。使用现有 Go 官方通道、固定 OpenCode/Relay/Journal/RunnerHost 的只读执行接缝，新增明确 planning subject；不能冒用 qualification_id 或借 Worker/Reviewer role facts。
先与 #110/#111 接口负责人冻结 PlanningExecutionBinding/AdmissionEvidence/OutputEvidence和每次effect guard。生产 factory只接受受信配置、原execution ID和完整资源authority；外部不能给prompt/argv/endpoint/Profile/原始输出。
输入从原需求/验收和批准只读快照编译；模型返回完整JSON Plan，#109解析由#110消费。模型不得写用户仓库、任意shell/MCP/plugin/hook/子agent/交付；允许的工具/网络通过实际OS/原生机制约束。
每个逻辑操作固定新Attempt/session/context、持久start/send/grant身份；启动和每个HTTP send重核原term/auth/source/generation/预算/窗口/Host/fence。Go调用沿用授权和有限上限，不启用其他现金渠道。

验收：
- [ ] 生产consumer+本地HTTPfixture实际native运行，看到原需求/验收/只读输入和完整最终输出artifact来源身份，#110可按ID消费。
- [ ] 真实Linux只读文件/宿主/写/shell/插件/控制面/交付拒绝；未批准前仓库bytes/modes不变；完整输入计量，超限拒绝不裁剪。
- [ ] 新session、完整最终assistant终止/usage/Journal/日志相符；中间/截断/多final/缺日志/未知不产生完成证据。
- [ ] prepare/admit/start/send/response丢回复不重复执行；未知占账保留，按原ID观察；取消精确owned process并记录local/remote区别。
- [ ] 实际send前取消、term/source/generation/窗口变更后无下一次发送；失败保留原请求累计。
- [ ] C/P回归和当前CI/独立审查，声明fixture不产生官方角色资格。
官方Commander资格及真实整链留给另一个S子票；本票不得关闭#93或启用未经该来源S资格的Commander。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：依赖下列发布记录中的当前候选接口/资格，解除后由原分派模型执行。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。