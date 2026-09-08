Parent: [#95](https://github.com/zhouy1017/Karajan/issues/95)。

负责 worker：gpt-5.6-luna；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前已派发并行实现；原生依赖#101/#104均已完成。

从#95实际consumer拆出只读输入编译C原语。输入仅为可信controller内部CandidateStore+validation subject身份、原批准需求/验收和精确完整最终Check Evidence IDs；公开调用不得接受任意文件路径、diff文本、check verdict或作者对话。
从CAS完整baseline/候选内容和当前validation.subject.candidate编译最终diff、必要源文件、验收、全部可信Checks（含对应内容/结果/日志摘要）。保留原execution.collection.candidate；不改原Candidate政策。所有作者只有可信身份元数据，不包含reasoning/chat/history。
建议新增 orchestration/reviewer_input.py 与专属tests/说明；不改共享routing/Journal/Relay。返回规范只读输入包+content digest/size/必要文件allowlist，直接由后续生产Reviewer consumer消费。

验收：
- [ ] 固定CAS/原批准输入+全部最终Checks得到确定bytes/摘要，实际binary/modes/增删/空diff按scope明确支持或拒绝。
- [ ] 缺/失败/旧Candidate/旧policy/旧input/部分或重复Checks拒绝；文件来自CAS，不读可变工作树或caller路径。
- [ ] 当前Go只支持T1/read/已有普通文件；超scope/超输入预算明确block，不隐藏剪裁必要源或Checks，不制造资格。
- [ ] 源码/日志的指令仅编码为待审数据，作者reasoning或聊天不入包；不执行输入中的任何指令。
- [ ] 不产生reservation/Attempt/session/模型请求/Evidence/Review passed；纯编译成功不等于可信执行。
- [ ] C测试覆盖实际CAS与公开compiler输出/反例、ruff/mypy、当前CI和GPT-6 high独立审查。
#95后续准入/执行consumer、#107官方机制、业务质量和#93整链S仍保留。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：无；luna正在专属工作树实现，后续真实consumer和S由#116/#117负责。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。