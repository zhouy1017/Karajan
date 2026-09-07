# Rulebook 资格集合编译：复用静态准入，不读取 Capacity

Parent: #95。这是纯规则模块的 C 切片，不取得真实角色资格或启动模型。

## 行为与范围

增加 `evaluate_profile_membership(task_snapshot, policy_snapshot, *, as_of)`，从版本化输入返回原批准阶段、分组和静态准入的合格 Profile 集合。它复用现有路由的分类、Rulebook 选择、审批约束、Profile 权限、角色/能力、上下文及作者独立性判断。它不要求或构造 CapacitySnapshot，不读取订阅/API 配额、不排名或选择模型、不预留或授予执行权限。

`evaluate_route` 和 reserved-profile 路径共享同一静态实现，继续执行各自原有配额、现金及排序逻辑。传入 facts 只是输入事实；公开结果明确 `live_qualification=not_run`、`activation_allowed=false`、`dispatch_enabled=false` 和 `selected_profile=null`。

## 验收标准

- [ ] **C：同一政策。** 正常阶段及已支持的质量阶段复用原 classification/rule/stage/group 交集，未达到或未批准的阶段不扩大允许集合；原 route/reserved 的行为与错误语义保持。
- [ ] **C：静态资格。** 精确 Profile revision、批准集合、channel/account/destination/tool 约束、角色/能力事实、有效期、context 和完整作者独立性均参与；T3 任一作者 family 未知或相同不合格。
- [ ] **C：时间与输入。** as_of 只接受有限真实数值，bool、字符串、NaN/Infinity 拒绝；版本化输入、重复身份和凭据值继续按原合同拒绝。
- [ ] **C：无资源效果。** 无 Capacity 输入或资源读取；eligible_profiles 仅规范顺序的集合，不表示排名或选中。改变容量不能通过该接口假造资格；所有执行授权字段固定关闭。
- [ ] **C：公共验证。** 新公共入口的正反例、实际原路由与 reserved 回归、边界差异核对通过；不只测试复制的私有实现。
- [ ] **G：交付。** 固定实现 commit、公开输入、独立 Standards/Spec、静态检查、当前 PR 必需 CI 齐备；合入 dev 后按本切片范围验收，不自动合并。

## 保留与状态

当前无已进入 dev 的完成项；本地实现已形成 Draft PR #102，候选验证见下文。#95 仍拥有 ID-only 可信绑定编译、当前角色资格来源、只读 Reviewer、资源 admission 和 S。此纯函数接受合成 snapshots 的测试仅证明 C，不把 Worker 资格升级为 Reviewer，也不提供真实资格。

本票不需要 #94 Checks 已通过即可独立开发。最终所在候选仍需满足基础 Task/relay 当前 CI；指定 Spark 修复额度不足是单独的 CI 阻塞。

<!-- profile-membership-local-progress:start -->
## 本地候选与交付进展（2026-09-07）

- **已进入 dev 的完成项：** 无。原验收条件保留；本票仍 Open / in-progress。
- **本地实现：** [Draft PR #102](https://github.com/zhouy1017/Karajan/pull/102)，当前候选 `01aa1397c286eef252858946052bc8de137e4328`，实现 `3a8cc5875b075285ab18796d1ab4bc36303192a1`。从 dev `2e587d1` 独立交付。
- **C 证据：** 33 新＋123 原有用例，两平台各 156；最终 LF 源码独立复验 Windows 156 / 3.32s、Linux 156 / 5.40s，14 份完整原路由结果逐份一致。独立 Standards / Spec 无确认问题；Ruff、mypy 9 源通过。原始输入、失败历史及最终源码摘要见[证据目录](https://github.com/zhouy1017/Karajan/tree/01aa1397c286eef252858946052bc8de137e4328/examples/profile-membership)。
- **剩余工作：** 当前 PR 必需 CI 和 owner 合并尚未完成，G pending。合并后按原 AC 核验关票；此切片不产生 P / S 资格。
- **阻塞：** 当前远端检查尚在执行，不能由本地结果宣告通过。#95 的实际 Reviewer 资格、资源准入和执行仍由后续切片承担。
<!-- profile-membership-local-progress:end -->
