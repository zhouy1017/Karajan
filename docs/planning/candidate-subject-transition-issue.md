# Candidate 验证版本消费与全部 Checks 重跑

Parent: #94。Blocked by: #96、#100。本票覆盖 #94 原验收中的 Reviewer 绑定版本交接，不执行 Reviewer 模型。

## 行为与接口

保持 `ApprovedCandidateChecks.advance/get/reconcile/cancel(run_id, operation_id, *, principal)`。仅消费原 operation 中由可信 ID-only 绑定生产者持久的 transition 和精确 CAS 收据；不新增接受 candidate_id 或资格 JSON 的公共切换入口。

安装新 subject 前确认原 cycle 未启动或已可靠停止，pending transition 阻止旧 cycle 新效果。在同一 operation 事务归档旧 cycle 并建立新 cycle，重新按原批准 ExecutionPolicy 编译全部检查并分配全新 Check/Attempt/Evidence 身份。原 capture A、原 Evidence 和共享 Run 的首次意图时间、累计次数都保留。

历史 child/结果只能恢复对应旧 cycle。新的 Candidate 不复制旧通过记录，晚到旧 Evidence 不能改变新 cycle；停止未知不能用“recorded”或“非 passed”替代确认。

## 验收标准

- [ ] **C：来源。** 缺可信生产者、跨 Run/operation、伪造或不匹配的 transition/前驱/完整 Freeze 身份均零新 Check；历史 rebind 收据不是当前授权。
- [ ] **C/P：全部重跑。** 临时真实仓库的 A 完成至少两项真实隔离 Checks，同内容 B 安装后两项全部实际重跑；原 CAS、用户树与 capture 指针不变，B 使用自己的 Evidence，Review 缺失仍不可交付。
- [ ] **C/P：停止与并发。** 活跃或停止未知的旧 Check 阻止切换；两个 advance 与取消竞争不产生两套活跃 cycle、不清除取消；安装前后当前 guards 有效。
- [ ] **C：恢复。** rebind 与安装提交分别丢回复，重开复用同一 Candidate/revision/全部 Check IDs；旧 child 和迟到 Evidence 只更新旧历史，不启动旧进程或改变新结论。
- [ ] **C：多级谱系。** A→B→C 保持原 A 锚点和直接前驱链；新 Evidence ID 集使旧 Review 不匹配，不能选旧成功记录掩盖新失败。
- [ ] **C：预算与失效。** 新 cycle 的全部 Checks 计入同一 Run 累计次数和时限；不足则零新进程。取消、审批失效或当前来源变化不能借历史收据绕过。
- [ ] **G：交付。** 新候选的实际 Linux/WSL P、公共边界测试、旧 Checks 回归、独立 Standards/Spec、静态与当前 PR 必需 CI 齐备；合入 dev 后按本票范围验收，不自动合并。

## 保留与状态

当前无已进入 dev 的完成项；本地实现进行中。规划、Writer/角色资格若使用显式 fixture，只证明对应 C/P；不声明 #95 真实 Reviewer、#93 真实规划或父票整链 S。生产没有真实 Reviewer 资格时仍需 blocked。#14 的生产 GitHub 交付与完整 v1 不在本票内。
