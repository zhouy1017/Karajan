# 批准 Run 的可信 Reviewer 绑定与持久 rebind 意图

Parent: #95。Blocked by: #96、#99。这是绑定准备阶段；不等待 Reviewer 模型执行或全部 Checks 通过，不预留 Capacity。

## 输入与行为

`ApprovedReviewerBindings.advance/get/reconcile(run_id, worker_operation_id, *, principal)` 只接原持久 IDs。控制器从当前批准 Plan 找到唯一依赖该 Worker 的 Reviewer Task，精确核对原 capture/Freeze 和完整作者来源，并从原批准 Rulebook normal stage grants、当前 Profile registration、角色资格及认证 generation 编译允许集合。

在原 operation 中持久完整绑定、来源、固定 command key 和预期旧 subject 身份。新效果在原 operation → Run → Project guards 内重新核对；旧 Checks 未确认停止时不能 rebind。然后调用 #96，并用完整 binding/key 精确查询结果，发布供 #94 消费的 ready transition。历史查询不要求当前凭据、环境或 CAS 可用，不能从历史收据重新授权。

原 Worker capture A 永远不变。A→B→C 时 binding 的直接前驱依次为 A、B；相同当前绑定重开不反复创建 Candidate，不能因为 as_of 时间变动不断派生新 revision。

## 验收标准

- [ ] **C：可信来源。** caller 无法传 Profile、作者、资格、Candidate 内容或政策；跨 Run/operation/Reviewer Task、缺失/多个/不匹配依赖、错误 capture/批准摘要均零 rebind。
- [ ] **C：当前交集。** 仅原批准 normal stage 的合格集合，风险/复杂度继承全部作者来源；过期/撤销/变动来源、未知家族和不独立角色均不借 Worker 资格通过。没有当前合格 Reviewer 则明确 blocked，不构造空或虚假的非空绑定。
- [ ] **C：效果顺序。** 完整 intent 提交后才进入 CAS；pending transition 阻止旧 cycle 新效果；取消/暂停/审批变更或旧停止未知不提交新版本。跨库不宣称原子事务。
- [ ] **C：精确恢复。** CAS 提交回复丢失只读恢复同一 Candidate/key；当前来源消失仍能读历史，不能重新执行。重复 advance 不因时间观察或同一集合产生新 revision。
- [ ] **C：谱系。** 完整 request、原 A 锚点、直接前驱与绑定摘要相互匹配；不从“最新相似 Candidate”或任意已有 rebind JSON 推断资格。
- [ ] **C：无模型效果。** 不创建 Reviewer Attempt/Capacity reservation、provider 调用或 Review 通过记录。当前生产 ProfileQualificationStore 仅支持 Worker 时必须 blocked；显式 qualification source double 只能用于本切片正路径 C。
- [ ] **G：交付。** 真实 stores/CAS 的公共正反例、失联与并发边界、独立 Standards/Spec、静态检查和当前候选必需 CI 齐备；合入 dev 后按本票范围验收，不自动合并。

## 保留与状态

当前无已进入 dev 的完成项；本地实现进行中。#95 原有真实只读 Reviewer suite/Profile qualification、实际选路/Capacity/模型 Review/S 保留。当前角色资格验证可能读取私有凭据核对 seal，返回值不含密钥；本票不能把它描述为零凭据读取。历史读取另走不依赖当前资格的路径。

本票可在 #94 全部检查完成之前准备绑定；实际 Reviewer 执行必须等待派生 subject 的全部有效 Checks。父票 #95 对 #94/#93 的最终依赖保持，不据此宣称整链完成。
