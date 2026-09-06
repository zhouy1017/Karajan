# 只读 Reviewer：角色资格、批准依赖选路与不可变 Review 绑定

Native parent: #13。Related: #14、#21、#90、[#94 全部可信 Checks](https://github.com/zhouy1017/Karajan/issues/94)。最终集成 Blocked by: Checks 子票的真实证据输出；真正批准 Task 的端到端 S 验收还依赖 [#93 真实 Commander 规划桥](https://github.com/zhouy1017/Karajan/issues/93)。资格实现与 C/P 可并行，不因最终依赖未完而停止。

## 目标与范围

为实际 Go Worker Candidate 选择并运行一个已获当前来源资格的只读 Reviewer。消费批准的 Reviewer Task、持久全体作者和全部最终 Check Evidence，经统一 Rulebook/资源准入产生新 Attempt/context，保存真实结构化审查结果与可当前核验的验证收据。无资格/容量/完整检查时等待，失败/不确定时阻塞。

本票不把 Go Worker scope 改名为 Reviewer 资格，不用固定脚本 verdict 代替模型审查，不实现 GitHub 写入。真实首条通路可以是 T1、同家族但独立上下文的 Go Reviewer；架构仍跨服务选路。#13 原有至少两种合格来源及 T2/T3 的完整真实验收继续保留，不能因为本子票通过而关闭父票。

## 输入与公开接口

- 原批准 Plan 从开始包含 Worker 和 `depends_on=[worker_task_id]` 的 Reviewer，tools/read_paths、Profile/Rulebook stage grant、预算、context/duration 均已批准。本票只实现一个 Worker 依赖；其它未支持依赖明确拒绝。
- 当前 Candidate/作者来自 #90 持久捕获和原 Task，不来自调用者的 JSON。作者风险/复杂度/paths 取原批准 Task，Attempt/Profile/fence/context/provenance 取候选与执行历史。
- 全部检查来自 Checks 子票当前精确 Evidence 集；缺失、失败、unavailable、invalidated 时不启动 Reviewer。
- 公开仅 Run/operation/principal 的 advance/get/reconcile；不接受调用者 Profile、作者、候选路径、argv、prompt、verdict、findings 或日志。内部构造只读输入包并复用原 stores。
- 旧 Run 若没有批准 Reviewer Task，返回明确 blocker 或走既有 Plan revision/批准流程，不能恢复时静默增节点或改摘要。

## 必须实现的完整接线

### 角色资格和只读 consumer

新增有固定 suite/ref、runtime/source digest 与 authentication generation 的 Reviewer 机制资格。可优先使用同一 Go 推理通道和固定 OpenCode，但必须新只读 Profile/scope：实际全新 session/context；只读批准 Candidate 文件；拒绝 edit、shell、插件/MCP、未批准/宿主路径和交付权限；核对实际计量、次数、撤销、停止和结构化输出。旧 projected Go v2 仅 T1 Worker/read+edit，其 passed 不能自动启用 Reviewer。

复用 `ProfileQualificationStore` 的持久 start/seal/最新有效观察/撤销语义；fixture 与 official scope 严格分开，latest unknown/failed/revoked 不回退较旧 pass。资格探针在独立固定工作区完成，不依赖尚未获得的 Task reservation，避免资格→reservation→workspace→资格循环。

真实 Reviewer consumer 复用固定 IsolatedOpenCode 投影、GoRelay/GoJournal、credential generation 与 Host直属 child 模式，但不接 Worker 的 capture/edit consumer。真实输入包由 CAS、需求/验收、最终 diff、必要源文件、全部可信 Check Evidence 编译；无作者 reasoning/chat history。输入摘要必须对应实际发送材料，日志/代码内指令仅作为待审数据。必要材料超限时明确阻塞，不能隐藏裁剪。

### 统一依赖路由与资源

扩展 `ApprovedRunRouting` 当前的 Worker-only 拒绝处，从持久谱系解析批准 Reviewer 依赖并填入真实 authors；复用 `select_rule/evaluate_route`，不要复制品牌或模型选择器。必须继承作者风险/复杂度，T1/T2 非作者 Attempt/新context；T3 家族未知或与任一作者相同则拒绝。

Reviewer 独立稳定 Attempt/context 和显式 Profile estimate；复用当前资格、原规则 grants、Capacity admit/activate/pre_effect_guard。开始和每次 HTTP send 前重新核对批准、Profile、来源/generation、上下文上限、窗口/配额/原fence。无资格或容量不足等待；不借 Worker admission、不伪造统一请求/token估计、不用其它账本绕过 Commander 保护余量。

Worker native stopped 不等于 remote_ended/usage_complete。不能为了腾出 Reviewer 额度而填造 Capacity reconcile 字段或退款；必要时等待真实可用容量。取消/恢复沿用原 intent-before-effect/once claim/原 grant ID，不能重置请求数或 Run/根任务累计边界。

### 不可变 Review 绑定与 Checks 交接

当前 Collector 固定 `approved_reviewers=[]`。保持其历史和 CandidateStore 的允许集合检查；不能直接写 passed Review，也不能原地给旧政策加人。

新增由可信控制器编译的版本化 `validation.review_binding`：取已批准 Reviewer rule grants 与当前角色-qualified Profile 的交集、家族来源及规则独立性，记录允许集合/source refs/digest；这一预备资格集合不预留额度或授予模型执行。实际选路仍在最终 Check Evidence 齐全后进行。

通过 CandidateStore 的受控 CAS policy-rebind 接口，从原内容创建同一 series 的新 Candidate revision：保留 baseline/tree/content/input/全部作者/writer-stop 来源/允许路径/所有批准检查，修改派生的 Reviewer 允许集合与 policy摘要，不改 owner ExecutionPolicy 的 argv/环境/规范。保存源 Candidate/绑定来源；不重跑 Worker、不读取旧可变树。`execution.collection.candidate` 保留原捕获身份，`validation.subject` 指向新验证 revision。

新的 revision 要求 Checks 子票重新执行全部检查；旧空集 Candidate 的证据不能搬来通过。资格集合/来源、内容/base/input/check策略变化产生新验证 revision/失效链；缺合格 Reviewer 时可保留此前真实 Checks 作为历史，但质量门继续等待。

实际 Reviewer 结束后，由可信观察编译 `ReviewResult`：candidate/input/policy/environment/review revision、精确最终 Check Evidence ID 集、实际 Actor/新context/来源、author_reasoning_included=false、verdict 与完整结构化 findings/log。复用 CandidateStore.record_review/gate；source文档或模型自述不能替代来源资格。Evidence 提交丢回复复用 Checks 子票的只读精确查询，不重新发送请求。

### 当前验证结果与 #14

原 operation 保存 content-free validation receipt，绑定原审批、验证 Candidate 全内容/政策身份、所有 Check/Review Evidence IDs/digests、来源/环境/Actor/context 和结果。读取“当前有效”结论必须重新核验控制面与 gate；旧 passed 不是永久交付 token。

交付状态仍 false/not_run；不移除 DeliveryCoordinator 现有 production 禁用/fixture-only 限制。#14 接收该结果，另完成 delivery target/最终 commit/expected head 绑定与每步独立 activation。PR 创建前不得把 Run 标为默认完成。

## 验收标准

- [ ] **C：谱系与批准。** 未批准 Reviewer task、错误/多重依赖、伪造作者/不同 Candidate、旧审批、缺或失败检查均不选中/预留/启动。真实作者风险复杂度不能因 Reviewer 自报低难度而降级；按批准规则和跨服务 Profile 集合评估。
- [ ] **C/P：新角色资格。** 当前 source/generation 的只读机制必须实际观察新session/context、只读/路径拒绝、无shell/插件/MCP/宿主/交付权限、计量/次数/撤销/停止。旧 Worker pass、fixture-only 或过期/revoked/latest unknown 都不启用真实 Reviewer。
- [ ] **C/P：版本化绑定。** 空集源 Candidate不修改；从真实完整CAS产生同内容新验证revision，保留全部baseline/modes/作者历史，所有必需Checks由A票真实重跑。资格名单/source、候选/base/input/检查条件改变使旧Review无效；不得更改旧Policy或降低独立性来让测试过。
- [ ] **C/P：真实独立执行通路。** 实际原生Reviewer + 本地HTTPfixture走生产consumer；观察实际发送的最终diff/必要源/验收/完整Check Evidence集，证明没有作者论证，新Attempt/context与全部作者不同。正/负/畸形/不确定/缺日志响应分别进入正确门禁。fixture结论只能证明接线，不是真实Reviewer资格或质量。
- [ ] **C/P：独立性负例。** T1/T2同Attempt/context拒绝；T3同家族或任一未知家族拒绝且不花slot；已通过检查的正确输出缺结构化severity/位置/行为/触发/验收依据/blocking也不可passed。阻断finding不能被verdict='passed'覆盖。
- [ ] **C/P：资源和撤回。** 独立真实Capacity预留；重复advance/activation/start/send回执丢失不重复claim/grant/send。实际send前发生取消/禁用/重资格/generation/source/window变化时无下一发送；发送未知仍占账，不退款；Worker余量不重复扣算或虚假释放。
- [ ] **C/P：恢复。** Review包/结果/Evidence提交的丢回复与重开只读恢复原内容和精确收据；不存在充分身份则保留unknown。取消后的结果可保留历史，但不能重新成为当前有效验证或启动新Review。
- [ ] **S：机制与真实审查。** 当前固定source/Profile/generation通过官方只读Reviewer资格；在相同consumer通路实际审查受控正确候选及一个有具体缺陷的负例，保存真实请求/Journal、输入摘要、新context和停止/structured result。失败和后续重试历史保留，不只验证provider返回200或让fixture直接给passed。
- [ ] **S：真实批准Task来源。** 真正用户意图→Commander→批准→Go Worker Candidate→真实Checks→真实Reviewer的最终S实例，必须消费#12生产planning admitted reader/准入桥的可核验回执。该桥缺失时保持此条件blocked/not_run；owner手写Plan/授权字符串或ScriptedAdmissionReader不得冒充真实Commander。机制S可独立先做，C/P可继续。
- [ ] **C/G：交付边界与当前版本。** validation receipt可按ID当前核验；所有验证执行角色无法push或调用交付端点。当前PR head必需CI、独立Standards/Spec与本票C/P/S证据齐备，才按本票范围验收；不自动merge、不宣布完整#13/#14或多来源/T3真实能力完成。

## 明确保留的父条件

#13仍必须跨至少两种**各自真实合格**来源获得相同内容身份的Candidate与独立Review，保留T2/T3实际资格与Run/根任务次数/时间/预算累计。单Go T1实例不能替代这些条件；选择规则允许其他来源也不是实际跨来源验收。

#14仍必须在真实GitHub和独立凭据/IPC域验证：验证收据失效/暂停/取消阻止新步骤；push成功后的取消阻止未activation的PR创建；丢回复先查同对象、不重复PR；外部head不覆盖、精确expected_old_sha及祖先关系；默认PR已确认完成与预设当前SHA CI gate两种模式；不自动merge。

## 开发与证据边界

初始实现基线 `.cache/go-task-validation@624ad8b8490003f155baf7842ba91b9975b9526a`。建议修改 role-scoped资格/只读consumer、ApprovedRunRouting可信Reviewer依赖读取、原operation review-binding/持久身份、必要CandidateStore CAS rebind及入口/测试/文档。Checks执行器和Evidence查询由A票提供；公共subject/冻结契约先对齐再并行。

两子票都保留旧v1fixture、#90捕获/恢复与Capacity/权限门回归。所有证据明确C/P/S/G、source/环境/Profile/generation/命令/实际结果，脱敏保存，不写key；真实Go沿用会话已授权范围，其他现金服务不自动启用。缺实际资产或planning桥是具体依赖，不通过改父AC来解除。

<!-- reviewer-storage-slice -->
## 存储实现子任务（2026-09-07）

- [ ] [#96 同内容 Candidate policy-rebind 与精确恢复](https://github.com/zhouy1017/Karajan/issues/96)

已完成：尚无本子范围已进入 dev 的完成项；本地 C/P 原语与独立复核已完成。

剩余工作：该子票当前提交/CI/合并，以及本父票原有资格编译、依赖选路、真实 Reviewer、Check subject 交接和 S 验收，原验收标准不减少。

阻塞：最终真实批准链仍依赖 #93；完整 Checks 输出依赖 #94；当前基础 Task 的远端 CI 修复仍由 #90 跟踪。

<!-- reviewer-binding-integration-slices -->
## 验证交接实现切片（2026-09-07）

- [ ] [#99 实现切片｜Rulebook 静态资格集合与路由共用检查](https://github.com/zhouy1017/Karajan/issues/99)
- [ ] [#100 实现切片｜批准 Run 的可信 Reviewer 绑定与持久意图](https://github.com/zhouy1017/Karajan/issues/100)

已完成：本次新切片尚无已进入 dev 的完成项；已有本地结果与草稿 PR 按前文独立记录。

剩余工作：子票覆盖原验收中的资格集合、可信绑定或新 subject 消费；原验收清单保持不变。绑定准备无需等待 Reviewer 模型执行，真实 Review 仍等待新 subject 全部 Checks。

阻塞：当前生产角色资格只有 Go Worker，不能借用为 Reviewer；新资格和实际 Review/S 仍由 #95 跟踪。基础 Task/relay CI 修复及真实规划 #93 的原依赖保持。
