# 批准 Go Candidate 的全部可信 Checks：隔离执行、证据与恢复

Native parent: #13。Related: #14、#90、#95。Blocked by: #90 当前 Candidate/捕获契约；候选接口已可用于并行实现。当前状态：`status:in-progress`。

## 目标与范围

从批准 Run 中持久关联的 Go Task Candidate 出发，在真实、固定来源的 Linux/WSL 隔离检查环境运行**全部** owner 批准的检查，将可信退出/日志/环境事实记录为 Candidate Evidence，并提供可恢复的 `checks_passed / blocked / reconciliation_required` 状态。

这是 #13 的确定性 Checks 子范围。它不执行模型 Reviewer、不授予 Profile 资格、不宣布完整质量门或 Run 完成；缺 Review 时 `local_gate_passed=false`、`delivery_eligible=false`。#13 的真实 Reviewer、T1/T2/T3 独立性和至少两个合格来源归 Reviewer 子票及父票；#14 的独立交付/真实 GitHub 完成语义不减少。

## 输入与公开接口

- 单机 Linux/WSL；已有批准 Run/Worker operation、#90 `execution.collection.candidate` 与固定 capture digest，完整 CAS baseline/tree/manifest/modes。
- owner 版本化 ExecutionPolicy v2 的 validation.checks/environments，包括精确 argv、environment_ref/source_sha256、timeout、env、network=none、max_log_bytes；Plan 中批准的 check ID 集。
- 控制器私有部署配置提供固定环境资产与 source resolver；公开请求不能传 argv、路径、镜像、环境变量、Profile、退出码或日志。
- 建议 `ApprovedCandidateChecks.advance(run_id, worker_operation_id, principal)`、`get(...)`、`reconcile(...)`。第一次从原 capture Candidate 派生 subject；若 Reviewer 子票已发布新的不可变 validation Candidate revision，则使用该 revision。切换 subject 是可信版本绑定行为，不是 caller 指定任意 Candidate。
- 取消复用当前 Run/operation 控制；读取、恢复不重新启动已 claim 的检查。

## 必须复用与最小新增

复用 CandidateStore 的 `get/materialize/record_check/gate`，原 operation JSON/事务、RunPlanner 当前审批、ExecutionPolicy 注册表、RunnerHost 持久 start/activation/inspect/cancel 与现有候选内容身份。旧 SerialCoordinator/LocalFixtureRunner 仅保持兼容；其固定脚本和 checks[0] 不能作为生产执行路径。

原 operation 中增加版本化 `validation.checks` 记录：subject Candidate 全身份、来源 capture/approval/Plan/ExecutionPolicy 摘要；每个必需检查的稳定 check_run/Host Attempt/start/activation/evidence_key、完整固定 spec、当前阶段、可信退出/停止/日志摘要和 Evidence ref。一个 check 的丢回复不重置 Attempt 或总计数；主动再次检查必须是明确的新执行 revision，并计入原根任务/Run 次数和时间边界。

增加固定环境 Check runner。它从 CAS 导出全新完整副本，按批准 argv 执行；环境来源实际观察须等于批准 source_sha256。运行非可信候选测试/构建代码时，没有宿主网络、交付凭据、控制数据库、SSH agent、宿主目录或高权限 Git 配置；可写 scratch/副本，不可修改冻结 CAS。RunnerHost 的进程树控制不单独构成沙箱。固定 OpenCode namespace 不能被改成任意 argv 后继续沿用旧工具资格。

外侧可信观察进程退出、停止、stdout/stderr 与日志完整性；日志位于执行区之外或经受控 pipe 收集。超限不得截断后仍算完整通过。未支持环境、来源不匹配、策略与 artifact 硬上限不兼容均前置拒绝，不自行替换命令/环境。

增加 CandidateStore 最小只读 Evidence 精确查询（建议 `lookup_evidence(evidence_key, expected_request, log_sha256)`）：恢复已提交记录不重新写 artifact、不从可变旧目录重新读日志、不重跑检查。检查结果与日志身份先由可信 runner 持久，再调用 `record_check`；不允许 caller 自报 `trusted_observation`。

效果顺序：短事务固定 subject/spec/claim → 提交 → 当前审批/subject/source/Host guards → 实际进程启动 → 持久实际结果/完整日志 → Evidence 提交 → 只读核对/链接。取消先持久后停止；不要长时间持 Run/Project 写锁等待检查。各库按原 operation → Run → Project → Host 顺序获取当前授权，跨库失败靠持久意图恢复，不宣称联合原子事务。

## 验收标准

- [ ] **C：批准来源。** 公开只有 ID；精确关联原 operation、capture、Candidate、输入/base/政策；缺库、重连丢库、空 schema、越权 principal、缺失或未知 check ID、环境/source 改变均零进程启动且不创建替代状态。原 default/legacy 行为保留。
- [ ] **C/P：全部检查。** 至少两个不同的已批准 check ID/argv 在真实隔离环境执行，证明每个检查都有自己的固定运行身份和 Evidence；不是仅运行 checks[0]。临时真实仓库中的小功能通过，随后可控功能缺陷使真实测试非零失败。
- [ ] **P：完整内容和隔离。** 完整 CAS 树/模式被导出；未修改文件保留，scratch 不改原 Candidate/用户树。实际测试尝试读取宿主 canary、访问控制库/凭据、联网、执行高权限 Git hook/config、调用交付入口均失败；不能用仅 mock 返回拒绝证明隔离。
- [ ] **C/P：失败与日志。** 真实非零、超时、取消、退出未知、停止未知、日志缺失/损坏/超限均不 passed。候选删除检查配置或伪造日志里的 passed 文本不能删掉批准检查或覆盖进程事实。
- [ ] **C：失效。** Candidate/base/input/ExecutionPolicy/环境或检查条件变化后旧 Evidence 无当前效力；新 subject revision 重跑全部必需检查。新 Check Evidence ID 使绑定旧 ID 集的 Review 失效；不能挑选较早成功记录掩盖最新失败。
- [ ] **C/P：恢复。** 分别在检查 start claim、实际启动、可信结果保存、Evidence 提交后丢回复/重开；只读恢复原 Host/精确 Evidence，没有充分事实保持 reconciliation_required，不创建新 start/evidence key 或第二进程。包含两个并发 advance 与一个取消竞争的真实 SQLite/进程用例。
- [ ] **C：取消与授权。** 暂停/取消/审批失效阻止下一检查；取消后的迟到结果可以保留历史但不形成当前有效验证结论。检查不借用或释放 Worker 的模型 Capacity、不制造 provider 结算事实；总次数/时间保持原 Run/根任务归属。
- [ ] **C：Reviewer 接线。** 最终导出精确 subject + 全部 Check Evidence IDs/digests/环境观察；当 Review 缺失时质量门仍等待。Reviewer 子票发布同内容的新 policy/Candidate revision 后，本接口能消费新 subject 并重跑，不把原 capture 候选指针改成不同 Freeze request 的候选。
- [ ] **G：当前提交。** 实现 commit、可复跑公开入口与固定输入、独立 Standards/Spec、当前 PR head 必需 CI 和上述 C/P 报告齐备；失败与修正历史保留。不得用文档/源码/fixture 代替真实隔离执行。

## C / P / S / G 归属与规划前置

C/P 必须使用真实 stores、临时 Git/CAS、Host/隔离进程；规划/资格若暂用明确 fixture，只证明本子票的本地 Checks 行为，不授予真实 Task/Commander 资格。本票无模型调用，因此 **S 不适用本票的 Checks 实现完成**；父 #13 的 S 绝不因此通过。

真正用户意图→Commander→批准→Worker→Checks 的整条真实链还依赖 [#93 真实 Commander 规划桥](https://github.com/zhouy1017/Karajan/issues/93)。禁止 owner 手写规划回执或 ScriptedAdmissionReader 假扮真实 Commander。缺该桥不阻止本子票 C/P 开发，也不能用 C/P 结果宣布真实整链已完成。

#14 必须另行完成真实 GitHub 独立凭据/IPC、push/PR 逐步 activation、响应丢失核对、expected head/祖先约束、默认和预设 CI gate 完成模式及不自动 merge。这里仅提供后续需要的可信检查证据。

## 建议文件范围和交接

`orchestration/candidate_checks.py`、固定 check runner/私有环境 resolver、必要 CandidateStore Evidence 查询、公开入口与相应测试/实施文档；复用原存储，不另起任务调度数据库。A票持有 checks 运行记录；Reviewer票持有 review binding/Reviewer执行记录。共同的 `validation.subject` 及 CandidateStore 新接口由一个作者统一实现并先冻结契约，其他工作可并行。

<!-- candidate-checks-local-progress -->
## 当前实现与验收进展（2026-09-07）

已完成：尚无本票实现已进入 dev 的完成项。草稿 [PR #97](https://github.com/zhouy1017/Karajan/pull/97) 已发布原 capture subject revision 1 的实现和本地 C/P 证据；代码 `0d63cde8cc4098894ecf4eec01109a1b7d3b7a70`，首发候选 `3beb8074bc102b2e96f30ed207c5eba10d90a2f5`。

本地验证：实际固定工厂、Host child、namespace 与全部 Checks 的正反例 2 passed / 73.85s；运行中两个 advance 与 cancel 竞争 1 passed / 22.18s。每项 Check 独立 Evidence，缺 Review 时 gate/delivery 仍 false；157 份复制证据和 136 个 backend 源码 Git blob 摘要已核对。[证据索引](https://github.com/zhouy1017/Karajan/blob/3beb8074bc102b2e96f30ed207c5eba10d90a2f5/examples/candidate-checks/README.md)保留作者与独立报告、失败和修正历史。规划/资格/Writer 输出为明确 fixture，没有官方 provider 调用。

剩余工作：原验收清单保持不变。#95/#96 可信 Reviewer 绑定的新 Candidate revision 消费、原 capture 指针保留、全部 Checks 重跑及新 Review ID 集交接尚未实现；当前 PR 必需 CI、合并与原范围最终验收未完成。状态保持 status:in-progress，未使用 Closes。

阻塞：基础 Task #90 / PR #92 及共享 relay PR #88 有已定位 CI 失败。指定 Spark 修复模型当前无额度，替换模型选择等待 owner；本草稿尚不可合并。真实整链另依赖 #93，不从本地 C/P 推断 S。
