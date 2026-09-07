Parent: [#95](https://github.com/zhouy1017/Karajan/issues/95)。

负责 worker：gpt-5.6-terra；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前为后续批次，依赖按实际发布关系核验。

承担#95剩余S验收，复用已有#107固定官方机制资格，不重建同义资格票。依赖真实Reviewer输入/独立准入/执行Evidence consumer当前候选；最终真实规划整链还依赖#93真实Commander到Candidate验收。
先用同一生产consumer实际审查受控正确业务Candidate及有具体可验证缺陷的负例，保存真实Go请求、Journal、新context、实际材料摘要、结构化结果与停止；再以真正用户意图→原Commander准入→模型Plan→owner精确批准→GoWorker Candidate→全部Checks→Reviewer运行整链。fixture或开发聊天模型身份不能替代产品角色资格。
验收：
- [ ] 同一当前官方source/Profile/generation且限定T1/read scope，正确/缺陷Candidate均有可复核真实结构化结果与全部最终Checks证据。
- [ ] 输入来自可信CAS和原需求，不含作者reasoning；所有作者独立性、原累计预算/requests与每次send guard真实保留。
- [ ] 原始失败、unknown、重试与新command记录完整，同key零重复效果；停止本地/远端/usage分别记录。
- [ ] 真正生产planning admitted/output/submission/owner批准到最终review Evidence IDs完整关联，不能手写Plan或admitted。
- [ ] 当前validation receipt读回有效性与取消/source失效反例成立，未执行交付仍为not_run。
- [ ] 当前候选C/P/S/G及独立审查逐项闭合后复核#95；#13两种合格来源/T2/T3与#14真实交付保持原责任。
缺真实输入或前置资格只阻塞相应S，不改父票范围、不用HTTP200代替质量。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：依赖下列发布记录中的当前候选接口/资格，解除后由原分派模型执行。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。