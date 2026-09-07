Related: #10、PR #118/#119/#121。Worker: gpt-5.6-luna；CI和审查修复仍由luna；Reviewer: gpt-6-astra high。

## 可观察问题与固定证据
PR118 e533163，push CI run34088605252 / Windows job101637431744：固定tokenizer、ruff、mypy已通过，pytest收集2704项，从05:56:43 UTC执行到06:35:04达到93%；40分钟job上限随后取消，后续独立检查未执行。无自然断言失败报告，取消栈不证明Capacity锁死。Luna本地Windows reserved_execution_guard 10项3.8s通过。另一候选02e72bc Windows全门禁约28min50s通过，说明宿主运行时长有明显波动，不能声称所有Windows都必现。

## 范围与设计
仅CI配置及执行预算说明，可选择有证据支撑的有限Windows job预算（建议60分钟；Linux可保留40）或同覆盖的有界分片。优先最小配置改动。不得删测、加skip、continue-on-error、弱化quality-gate/required checks、关闭隔离/资产要求、换锁定依赖或更改产品deadline。修复PR独立记录该范围，与#120下载问题区分。

## 验收
- [ ] 记录原取消运行、成功对照与预算选择依据，不把超时直接宣称产品死锁或测试失败。
- [ ] Linux/Windows原测试、全部独立examples边界、前端与聚合gate仍实际执行，名称/依赖保持可识别；超预算仍失败。
- [ ] 验证workflow语义与固定候选diff；这种配置改动不另造实现镜像单元测试。
- [ ] GPT-6 high独立Standards/Spec复核，最终组合候选当前必需CI实际通过；原cancelled历史保留。
- [ ] 合入dev前Issue保持Open；仅满足原范围后关联Closes。

已完成：只读诊断。剩余：配置修改、复核和最终CI。阻塞：worker队列；不新增模型或第三方现金调用。