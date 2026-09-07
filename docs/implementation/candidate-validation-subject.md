# Candidate 验证版本交接

关联 [#94 全部可信 Checks](https://github.com/zhouy1017/Karajan/issues/94) 与
[#95 只读 Reviewer](https://github.com/zhouy1017/Karajan/issues/95)。本文件固定两个实现
切片之间的接口要求。CAS policy-rebind 原语在 [#96](https://github.com/zhouy1017/Karajan/issues/96)
独立切片提供存储原语。[#100](https://github.com/zhouy1017/Karajan/issues/100) 的 ID-only
绑定编译器和 [#101](https://github.com/zhouy1017/Karajan/issues/101) 的版本消费者已在
`1fc97849697cfe89a79595cba07e9ec028c6d0b2` 实现并完成本地 C/P 验证，见
[发布证据](../../examples/reviewer-validation-subject/README.md)。正向角色资格使用明确替身；
正式 Reviewer 资格、模型 Review、S、当前 PR 的 G 与生产部署配置仍未完成。

## 已有来源与两个身份

`execution.collection.candidate` 永远指向原 Worker 停止后捕获的 Candidate。
它与原 `capture_digest`、Freeze request、完整 baseline 和捕获文件摘要一起，供精确历史
恢复使用。原 Collector 的 `approved_reviewers=[]` 保持不变。

`validation.subject.source_candidate` 保存该来源的完整身份。
`validation.subject.candidate` 保存当前要运行全部 Checks 和 Review 的验证版本。
首版两者相同，`subject.revision=1`；Reviewer 接入后可以产生内容相同、派生验证政策不同的
新 Candidate revision。原 capture 指针不得随之改写。

两处完整身份至少包含 `id / series_id / revision / repository_identity / base_sha /
tree_sha / content_sha256 / manifest_sha256 / input_sha256 / policy_sha256 / baseline_id`。
subject 同时绑定原 capture、批准、Plan、ExecutionPolicy 摘要；不能仅凭 Candidate ID
或内容摘要判断属于当前批准 Run。CAS rebind 的来源身份另含 `request_sha256`，绑定完整
Freeze request 中的作者、Writer 停止观察、路径与任务等级。

## 可信 Reviewer 绑定

`ApprovedReviewerBindings.advance/get/reconcile(run_id, worker_operation_id, *, principal)`
从原批准 Reviewer Task、依赖 Worker 的完整作者历史、当前 Rulebook normal-stage grants 和
当前角色资格编译绑定。不接受 Web 请求指定 Profile、作者、
候选路径、资格字符串或新的检查政策。

绑定包含版本、来源 Candidate 全身份、Reviewer Task 与批准摘要、规则来源、允许的
Profile revision / model family / qualification ref、实际资格来源与认证 generation 的
摘要。允许集合必须来自规则与资格的交集；这一预备绑定不预留 Capacity、不授予模型启动。
实际 Reviewer admission 仍等待新的全部 Check Evidence 通过。

原 operation 的 `validation.subject_transition` 使用
`karajan.candidate-subject-transition.v1`，阶段为 `prepared` → `rebind_claimed` →
`ready` → `installed`。完整 ReviewerBinding、预期旧 subject 摘要、固定 command_key、
语义摘要与精确 Candidate 收据共同保存。只有原 claim 提交明确返回成功的调用可首次执行
CAS；已 claimed 的重开只查精确历史，缺收据保持待核对，不重发。

尚未 claim 的 prepared 意图可因当前来源变化在同一 operation 事务内归档至
`validation.intent_history` 并换用全新 ID/key；claimed/ready 不可替换。安装时
`validation.review_binding` 保存完整 installed transition，其 `.binding` 是编译结果。
新的 incoming transition 不覆盖当前已安装 binding。当前来源验证使用传入的 Project
事务，不嵌套另一组同库锁，也不把稳定准备身份当作真实 Reviewer Attempt。

## CAS policy-rebind 存储边界

CandidateStore 提供受控内部接口，以来源 Candidate 的精确身份、完整绑定及其摘要为
输入，从已有 CAS 建立同一 series 的新 revision。该接口只证明内容与政策谱系，不证明
绑定具有当前授权；可信调用方在当前 Run/Project/规则/资格 guards 内调用。

- 验证并保留完整 manifest、文件模式、baseline、tree、content、input、作者、Writer
  停止观察、允许路径、任务等级及全部批准 Checks。
- 只派生 Review 的允许集合和相应 policy 摘要。不得接收任意替代 Freeze request、
  检查 argv、检查环境或降低独立性的任务等级。
- 保存来源 Candidate 与绑定摘要的不可变谱系；原记录、原 Evidence 不修改。
- 新提交前核实 CAS 完整可用；不用 Worker 的旧可变目录，也不重新执行 Worker。
- 精确重放只返回同一历史提交。丢回复后用精确只读查询恢复；不得从“最新相似 Candidate”
  推断该绑定已提交，也不得在恢复时重新物化文件。
- 来源已被其他内容 revision 取代时，不从旧来源重新授权。精确历史收据仍可读取，
  `gate` 另外判断当前有效性。

## Checks 消费与切换顺序

#100 先持久化绑定与 rebind 意图，再提交 Candidate，最后只读核对并关联新 subject。
跨 Candidate/operation 两个数据库不宣称原子提交。Checks 从原 operation 中读取该可信
关联，并核对来源 capture、绑定和新 Candidate 的完整身份；公开接口仍只有 Run、
operation 与 principal。

切换之前必须确认旧 Check 不再运行；停止未知时保持阻塞，不同时启动两套验证。
旧 subject 和它的 checks/review 记录保留为历史，新 subject 使用递增 revision 和新的
Check Attempt/start/evidence key。所有必需 Checks 重跑并计入同一 Run 的累计预算；
不得复制旧版本的通过记录。随后 Review 绑定新 Candidate 和实际最终 Check Evidence
ID 集。没有合格 Reviewer 或绑定未完成时，质量门和交付资格继续为 false。

消费者在 ready 安装及已安装版本的新 Check 效果前调用生产者的 `current_locked`，
复查原批准、规则、资格和认证来源。缺验证器或不合格来源明确拒绝。`validation.history`
保留各旧 cycle；历史 child 只能恢复旧记录，晚到的观察和 Evidence 只更新对应旧 cycle。
A→B→C 中原 capture 始终锚定 A，而新 binding 的直接前驱是 B。停止未知时即使旧 Evidence
已 recorded 也不能切换；已确认停止但尚未提交 Evidence 的旧观察可归档后继续精确提交。

## 当前证据与保留范围

生产者 18 项 C 在 Windows/WSL 通过；消费者 16 项 C 与两项真实 Linux P 通过。P 中
A、B 各运行两项批准检查，使用不同 Attempt/Evidence，完整 CAS、用户树、原 capture 与
共享预算起点保留。另一项 P 观察到活跃 namespace PID/birth 和固定命令标记后验证拒绝
切换，再并发两个 advance 与 cancel，确认停止且第二项无 claim。独立审查未发现确认问题；
精确安装提交丢回复、旧 Evidence 晚到等故障证据分别保留，不重复计数。

这两项 P 使用独立的固定测试 child 和资格替身，child 字节摘要进入实际执行来源；正式
factory 没有 fixture 开关。正常 factory 的真实资格 Store 对未获 Reviewer 资格的 ready
版本以 `REVIEWER_QUALIFICATION_REQUIRED` 拒绝，且不准备 Host。正式可接受的 Reviewer
suite/认证配置、只读 Reviewer 进程及 Review Evidence、真实规划桥 #93、整链 S 和当前
候选 G 不由上述 C/P 代替；旧 Review 不可转用新 Evidence 身份，实际 Review 消费仍属 #95。
