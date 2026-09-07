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

当前无已进入 dev 的完成项；本地实现已形成 Draft PR #103，详见下方候选证据。规划、Writer/角色资格若使用显式 fixture，只证明对应 C/P；不声明 #95 真实 Reviewer、#93 真实规划或父票整链 S。生产没有真实 Reviewer 资格时仍需 blocked。#14 的生产 GitHub 交付与完整 v1 不在本票内。

<!-- reviewer-subject-local-progress:start -->
## 本地候选与交付进展（2026-09-07）

- **已进入 dev 的完成项：** 无，原 AC 与依赖完整保留；Issue 仍 Open / in-progress。
- **已发布本地实现：** [Draft PR #103](https://github.com/zhouy1017/Karajan/pull/103)，首次候选 `d05456c0d8b3fd20b45ecbd8a4ea5c7951ee3bc3`，实现 `1fc97849697cfe89a79595cba07e9ec028c6d0b2`。16 作者 C、5 独立边界设计及旧 Checks 回归通过。WSL 一次运行16C＋2P通过：A/B各两项真实 namespace Checks全部重跑；活跃旧进程与并发取消拒绝切换并确认停止。
- **验证：** [逐项证据与限制](https://github.com/zhouy1017/Karajan/blob/d05456c0d8b3fd20b45ecbd8a4ea5c7951ee3bc3/examples/reviewer-validation-subject/README.md)。独立 Standards / Spec 无确认问题；Ruff、mypy 139 backend 源通过；原始源码/输入/失败历史和5份真实Check日志已冻结。原始证据空白诊断如实保留。
- **剩余工作：** 本 PR 当前必需 CI 与 owner 合并尚未完成，G pending。正路径的 Reviewer 资格使用显式 double；生产真实 Store 缺角色资格仍 blocked，无真实 Reviewer 或 S 通过记录。#95 的实际角色资格、容量准入、模型 Review 与交付判定保留。
- **阻塞：** 依赖 PR97/98 当前 Linux CI 失败，指定本地 Spark 额度不可用，尚未完成修复；不能以本地通过替代当前 CI。

<!-- reviewer-subject-local-progress:end -->
