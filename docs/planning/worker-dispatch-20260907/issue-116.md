Parent: [#95](https://github.com/zhouy1017/Karajan/issues/95)。

负责 worker：gpt-5.6-terra；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前为后续批次，依赖按实际发布关系核验。

承担#95实际业务Reviewer consumer的C/P完整接线。依赖可信Reviewer输入包、Reviewer独立资源准入、现有#104 parser/#106限定角色机制/#101当前验证subject。
消费原Run/operation IDs，固定native只读通路从CAS输入包发送实际最终diff/必要源/验收/全部Checks；新Attempt/context与全体作者独立。绑定原source/generation/批准/资源/Host，每次send重核guard；没有生产当前角色事实不得执行。
由可信observer确认完整最终assistant、停止/usage/Journal及日志，再调用现有parse_review_output，用controller身份编译ReviewResult，经CandidateStore.record_review/gate写证据。lookup_evidence(kind=review, exact request, log digest/size)恢复，不建立第二套Evidence store。

验收：
- [ ] 本地HTTPfixture+实际native consumer正/负/畸形/截断/不确定/缺日志各进入正确gate；blocked finding不能被passed verdict覆盖。
- [ ] 实际发送材料与输入包摘要一致，无作者reasoning，必要内容超限阻塞；只读OS/工具/交付边界和新context有P证据。
- [ ] start/send/output/Evidence丢回复和重开只读找原身份，缺证据unknown，不重发送；取消后历史可保留不可复活。
- [ ] 每次实际send前取消/禁用/重资格/source/generation/窗口变化零下一发送，unknown消费不退款/清零。
- [ ] content-free validation receipt按ID绑定精确candidate/base/input/policy/全部Check+ReviewEvidence/source/context；读当前有效时重新核验控制面和gate，过期passed不是永久token。
- [ ] 交付始终false/not_run；不移除DeliveryCoordinator production限制；C/P测试、当前CI、独立审查。
本地fixture不是真实Reviewer资格/质量；#107官方机制以及独立业务/整链S子票保留。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：依赖下列发布记录中的当前候选接口/资格，解除后由原分派模型执行。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。