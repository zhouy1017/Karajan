# 共享 RunExecutionBudget 独立评估

审查者 capacity_facts；本次预算及 Writer/Check 接线作者 ui_spec_finalize。审查对象是新增共享预算与实际调用点，未将审查者过去写的历史 intent 端口本身作为独立审查成果。

**结果：0 confirmed finding。** Windows 10 passed（13.70 秒），WSL Linux 同 10 项 passed（8.74 秒）。两平台前后 6 个相关产品源摘要保持一致，详见 `review.json`。独立原输入是 `test-first.py.txt`，实际执行测试是 `test_effect_budget.py`；本组没有红灯，不把 root 早期两个假设测试当作本组已确认发现。

实际公共入口证明：

- Writer prepare 后排队耗尽共享时长、或原库缺预算表时，`advance` 拒绝；原 Capacity/Journal 字节不变，无 Host 执行行或 native 目录。历史 `get/reconcile` 仍可用，不补造预算。
- `consume_go_task` 真正传给 producer 的 native/start/send 回调在到期后拒绝；包含最后 Host 身份等待期间推进可信时钟的案例和未到期正控。原 claim 不重领，Journal 保留撤销和零请求历史。
- 到期后的原有效停止/内容 capture 经 `ApprovedGoCollector.collect/recover` 实际写入并恢复同一 Candidate CAS；预算和 Journal 不变。
- Check 的 prepared/host_prepared 排队到期或旧库缺预算表均拒绝下一阶段，原历史和预算不变，无新 supervisor。

证据层级为 C：Run/Project/Capacity/Host/Journal/CAS 是真实库；资格、规划、Host 直属身份和 native producer 是明确 fixture。这里验证真实业务代码的效果准入回调，并未运行 OpenCode 或发出 HTTP；不将 callback 计数冒称实际 provider 请求。共享门是控制器准入时限；回调后的 runtime/transport 内部准备与服务端接收仍受各自原有截止/停止事实约束，不声称端到端硬截止。

关键实现核对：一次 claim 与原 operation 同事务、重放不重置 started_at/计数；old schema 只读允许而新 effect 缺历史则拒绝；当前预算检查与历史 capture 身份 guard 分开；Writer 在最后 Host/source 检查后再比较 transient checked_at/deadline，未将临时预算门写入 operation。

可复现命令见 `plan.md`；Linux 使用同组、显式多目录 pythonpath 和 `-p no:cacheprovider`。本审查未修改产品、作者 tests、CI 或 Git，未读取真实凭据、调用 provider 或制造资格。
