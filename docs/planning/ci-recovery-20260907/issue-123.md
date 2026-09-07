Related: #10、PR #121、既有CheckRunner丢回复边界。Worker: gpt-5.6-terra；CI及审查修复仍由terra；Reviewer: gpt-6-astra high。

## 实际失败输入
PR121初版02e72bc，Ubuntu push run34089902981 / job101641094674：全库2686passed/7平台skip，单项 test_lost_actual_popen_reply_never_claims_not_started_or_launches_again 失败。真实Popen启动后合成丢回复，恢复轮询 namespace-init.json 时 regular() 在读前/后元数据变化抛 CHECK_ASSET_CHANGED，未由恢复协议归一，runner.run冒出ValueError。原日志私有 .cache/dispatch-ci/pr121-ubuntu-101641094674-failed.log；公开证据使用脱敏片段和Actions链接。

## 有界范围
诊断并修复 backend/karajan/isolation/check_runner.py 与必要的精确相邻init生产/消费代码、专属回归及说明。先证实写入/读取边界，再选原子发布或合适的有界未就绪/unknown处理；不猜测任意异常都可重试。不得弱化regular文件/symlink/hardlink/identity/hash检查，不能吞掉恶意或持久畸形输入当成功，不能在Popen结果未知后再次启动或声称not_started/退款。

## 验收
- [ ] 用可控竞态反例证实原失败，保留首次CI失败与新候选结果。
- [ ] 精确owned init短暂未就绪/变化期间不会暴露未处理异常；最终观测有界，已运行或无法确认仍保持started/unknown合同，不重复Popen。
- [ ] init身份、文件类型、尺寸、变更防护及当前cancel/停止回归保留；不以全局catch ValueError改为通过。
- [ ] 真实Linux namespace丢Popen回复原测试和相关专属回归实际通过，区分Windows平台不支持。ruff/mypy与必要收集通过。
- [ ] GPT-6 high独立Standards/Spec与最终组合候选CI通过。文档固定实现SHA/命令/层级/remaining；不将本票提升为新的Go来源S。
- [ ] 合入dev且原范围满足前保持Open，不通过重复旧CI刷绿替代修复。

已完成：失败日志归属定位。剩余：同级worker诊断/修复与验收。阻塞：worker队列；无新增模型调用。