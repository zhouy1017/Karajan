Parent: [#93 真实 Commander 规划桥](https://github.com/zhouy1017/Karajan/issues/93)。Depends on: [#109 计划输出 parser](https://github.com/zhouy1017/Karajan/issues/109)（最终组合验收；可并行实现）。

执行分派：gpt-5.6-terra，CI/审查修复仍由 terra；独立 reviewer 为 gpt-6-astra high。基线 `dev@6cf89abc1a6ca8cb99d76f82191dc7d82efeb05b`。

## 有界目标与输入

本票从 #93 A 拆出持久控制器 C 层：公开调用只给 Run/intent/execution ID、authenticated principal 与 command key，绑定原 owner/当前 lead/term/Profile revision 与 digest、配置/规则/授权 ceiling、planning_budget_ref、需求摘要、独立 Attempt/fence、原容量 request/key/source 与 output/submission 身份。真实 SQLite 保存阶段；未知恢复只读查询原请求，不重发或造 admitted。

建议新增 `orchestration/planning_execution.py`（必要时同前缀内部模块）；允许最小受控扩展 `runs/planning.py` 与必要 receipt schema，但禁止把 provenance 字符串改为 live 以替代证据。不修改 luna 的 `runs/planning_output.py`、其专属测试，亦不改共享 Reviewer/Journal/Relay/CI。

## 接口与可信责任

- 对外建议 begin/get/reconcile/submit/cancel，以 ID 进入；不暴露 record_receipt、record_output(payload)、任意 prompt/Profile/argv/计划 JSON 的 HTTP/CLI 接口。此票无需添加 HTTP。
- 内部只读 authority 分开返回精确 PlanningAdmissionEvidence 与 PlanningOutputEvidence。容量 receipt 仅证明容量，不能自动映射为完整规划 admitted：必须关联原 planning binding、budget 与 source；缺一拒绝。受信 authority 的实现与 factory 权限明确区分。
- 直接复用真实 `CapacityStore.command_receipt` 的 admit/activate 完整 request+key 查询；记录各跨库阶段，缺 receipt 为 unknown，查询不申请新 reservation。
- 固定 output consumer 仅按 execution ID 从内部 authority 读取原完整 artifact bytes/sha256/size/source/完成身份，自己核对摘要并调用 #109 `parse_planning_output(..., version=...)`。不得接受 caller 提供的 parsed_plan 或 artifact 路径。原输出不可变且只在私有存储保留，公开诊断不输出原文。
- consumer 经既有 RunPlanner 当前 term、lead、intent、expected revision、plan/ceiling/routing 校验提交，持久绑定原输出和精确 submission receipt；owner 仍通过原批准入口决定。
- 尚无真实 transport/budget/qualification authority 时生产入口明确不可用。C 测试替身必须显式标记 fixture；不能被 production factory 接受或导出可启用资格。不把本票宣称完整 #93 A。

## 验收标准

- [ ] 真实 SQLite 重开及重复 begin/get/reconcile/submit 返回同一绑定/输出/提交记录；不同 payload/key 或身份不可改绑；无重复提交、准入、进程或请求。
- [ ] 非 owner、旧/顾问 term、非当前 lead、错误 Run/intent/Profile/budget/source/config/ceiling 拒绝。原 Capacity admit/activate 丢回复从完整原 request 精确查询；unknown/missing/mismatched 不 claim、不提交，不造退款。
- [ ] 输出按 execution ID 读取；缺完成/输出、digest/size/source/身份不符、修改原输出、#109 parser 拒绝均不生成新 Plan；完整输出生成精确 Plan 版本和 receipt，丢回复只读找原提交。
- [ ] 取消先持久撤销未来提交权限；取消、旧 term 或来源变化后的迟到输出只保留历史，不成为当前计划；并发 cancel/submit 有确定可观察边界。
- [ ] v1/v2 由真实 Run 协议固定；顾问不能提交；owner 的精确计划/授权/配置/routing 批准及旧版本拒绝保留原回归。
- [ ] 无实际 production authority 时 fail closed；显式 fixture 控制器测试无真实模型调用，不能生成真实资格。真实 Capacity+SQLite/业务 API 的 C 证据与未做的 P/S 分列。
- [ ] 相关 pytest、ruff、mypy；固定候选独立 Standards/Spec 与当前必需 CI 通过。文档映射每项 AC、命令、实际结果和 remaining。

## 已完成、剩余、阻塞

已完成：无。
剩余工作：本票全部条件；#93 后续子票负责原 Rulebook/完整 budget+Capacity 生产准入、固定只读 RunnerHost transport/逐 send guard、精确物理取消及真实资格；另由整链票负责真实规划→owner批准→Worker Candidate。
阻塞：最终组合依赖 #109；离线实现可先行。没有生产来源 authority 不阻塞本票 C 子范围，但不得据此放行真实 Commander 或关闭 #93。