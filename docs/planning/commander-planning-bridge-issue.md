# 真实 Commander 规划桥：原准入、固定来源、模型输出与 owner 版本批准

Parent：[M1-02｜有界规划、确认和最小人工交接 #12](https://github.com/zhouy1017/Karajan/issues/12)

Related：[ChatGPT 订阅 #18](https://github.com/zhouy1017/Karajan/issues/18)、[Claude 订阅 #19](https://github.com/zhouy1017/Karajan/issues/19)、[OpenCode Go #21](https://github.com/zhouy1017/Karajan/issues/21)、[Rulebook #23](https://github.com/zhouy1017/Karajan/issues/23)、[共享容量 #24](https://github.com/zhouy1017/Karajan/issues/24)、[逐调用与原币预算 #25](https://github.com/zhouy1017/Karajan/issues/25)。下游验证关联 [Go Task 可信入口 #90](https://github.com/zhouy1017/Karajan/issues/90)。

类型：`kind:task`。本票包含真实服务验收，不能仅凭 fixture reader 或离线状态机关闭。A/B/C 按文中范围实施，完整链路责任由本票保留。

## 目标与当前缺口

用户提交需求后，指定的真实 Commander 在原规划授权与资源上限内运行，输出结构化计划。平台将真实模型输出、原资源准入和固定来源关联；只有该 Run 的当前主 Commander 能提交，owner 对返回的确切 plan/authorization/routing 摘要批准。批准前不允许写项目代码。随后已批准 Task 能被现有任务准入和执行消费者接受，而不需要任何合成规划回执。

当前 `RunPlanner` 已持久化需求、规划意图、计划版本、owner 批准和人工交接。`attach_planning_receipt` 只接受引用，但其构造依赖 `admissions(receipt_ref)`；产品尚无生产 reader。缺 reader 时返回 `ADMISSION_AUTHORITY_UNAVAILABLE`，缺 admitted intent 时返回 `PLANNING_ADMISSION_REQUIRED`。现有 `PlanningReceipt.provenance=imported_observation` 只是一种来源分类，不能证明模型调用、真实准入或预算已生效。

本票补齐这条真实路径。不得注入 `ScriptedAdmissionReader`、`SyntheticSuite`、手写 `admitted=true`、直接写 Run SQLite 或把开发聊天的模型身份当成产品 Commander 资格。

## 输入与已确认的产品边界

- 现有 owner-authenticated Project/Run、冻结配置/Rulebook、当前 `planning_intent`、Commander principal/Profile revision、term、需求与验收标准、规划授权上限和 planning_budget_ref。
- controller 配置的固定执行器、官方认证来源、模型/native settings、OS/权限边界、限定输入和有限订阅/预算策略。实际凭据只在 controller 私有存储/进程内使用，不由模型、HTTP 请求或 Issue 提交路径/值。
- 针对所选 Commander 的真实来源资格：至少覆盖结构化规划输出、受控只读输入、实际模型/认证/计费路径、可见消费范围和停止/恢复声明。不能继承另一模型/执行器或 T1 Worker 的资格。
- 模型选择沿用既定设计：主 Commander 固定；顾问不能取代主 Commander。主 Commander 不可用时记录阻塞/交接建议，不能自动切换；换人继续使用现有 owner 决定与新 term 流程。
- 不新增付费授权。非 Go 现金调用仍按现有暂停范围保持 `not_run`。ChatGPT/Claude 真实订阅运行先核对已有具体授权和真实配置；开发会话可用不等于产品登录已配置。Go 用户已授权的固定调用也不自动提供 Commander 能力。

## 完整调用链

1. 仅以 Run/intent 等持久 ID 进入可信协调器，从 owner 的当前 Run、当前 term 和固定配置组装规划输入。实际 prompt 和只读文件来自批准范围/受控快照；请求不得替换 prompt、Profile、预算、模型、任意 argv/baseURL 或路径。
2. 复用 Rulebook 规则选择与当前资格/资源检查。规划不能使用要求“已有批准 Plan”的任务组装器来循环依赖自己；从 `Run.authorization_ceiling + planning_intent + 当前目录/资格` 构造专门的受控规划输入，复用既有纯规则求解和资源深模块。
3. 在任何真实模型效果前持久固定 execution ID、Attempt/fence、source/input/authentication 摘要、原准入 command keys 和当前 term。依次取得原共享容量和对应的规划预算资格；所有资源向量完整才允许继续。跨多个 SQLite 的恢复协议明确记录阶段，不声称一个全库原子事务。
4. 固定入口经 RunnerHost 启动实际受控 child；实际模型开始和每次可观察请求之前重核当前 Run/term、来源、预算/容量、Host 身份及取消状态。不能把 parent Host.start 当成所有后续模型效果的唯一检查点。
5. 一次 claim 后调用真实 executor；保存其真实 session/turn/request 等已观察身份、返回输出 artifact 的摘要、状态和消费证据。没有可信 logical ID 的调用不按 prompt/body 去重，也不因超时盲重发。
6. 生产 reader 只从原执行/资源账本读取并关联证据，返回对原 intent 的受控投影。容量 admitted 只说明容量准入，不能直接证明规划已完成、现金预算有界或当前 source 合格。
7. 固定输出 consumer 从原 execution ID 读取不可变模型输出，解析/校验结构化计划并调用现有 `RunPlanner.submit_plan`。外部调用方不能提交一份计划字典然后声称它来自真实模型。原始输出与解析结果都保留可核对摘要；失败不回填一份脚本手写计划。
8. owner 通过既有入口查看并批准确切当前版本。模型/reader/runner 都不能替 owner 批准。旧 term、顾问、旧 plan 或不匹配摘要不得取得有效批准。
9. 验证所得真实批准 Run 可以进入当前 `ApprovedTaskAdmission` / Workspace / 固定 Worker 入口。真实 Go Worker 若为下游选择，仍需独立当前 v2 资格、原 Task 范围和资源门禁；不能从 Commander 证据继承。

## 最小接口与责任边界

具体文件名可由作者根据深模块边界定型；先确认一个被实际消费者直接使用的接口，不为此次接线增加第二套独立调度系统或通用 Provider 框架。

**外部入口**建议限制为启动/读取/取消/核对：传 `run_id`、`intent_id` 或原 `execution_id`，身份来自已认证 controller context。幂等键由 controller 绑定一项逻辑操作；相同逻辑操作的重放不得取得新执行权。不能暴露 `record_receipt(payload)`、任意 `record_output(json)` 或 `execute(profile, prompt, command)` 入口。

**内部生产端口**至少区分以下材料：

- 不可变 `PlanningExecutionBinding`：owner/project/Run/intent/term/Commander principal/Profile digest/runtime source/authentication generation、requirements/input digest、authorization ceiling、Rulebook/policy/预算 refs、Attempt/fence、原启动/准入/调用 command IDs。
- `PlanningAdmissionEvidence`：已有 Capacity/budget 权威的原 request 和准确 receipt ID/digest/state、当前检查结果及有效期。它不是模型执行结果。
- `PlanningOutputEvidence`：原执行器固定 source、实际执行身份、原返回 artifact 的 content digest/size/schema、结束/失败/unknown 事实、可见 usage/调用边界。原始模型文本按私有 artifact 保留，只在 owner 计划审阅中展示必要内容，不写公开诊断/Issue。
- `PlanningSubmissionReceipt`：真实输出 artifact 与解析计划、RunPlanner 返回 plan revision/digest 的精确关联；重放只能返回同一记录。新授权或重规划使用显式新操作，不能覆写旧执行/输出。

接口不能让调用者通过来源字符串或 `status=passed` 获得能力。普通只读查询不刷新时效、触发 provider、创建新 reservation 或清理旧 unknown 消费。

## 可并行实现的范围

以下 A、B 在先固定上述 binding/结果协议后可并行；C 依赖二者。可以作为本票内的明确子任务，也可以发布成原生子 Issue。若发布，各自的 Closing 范围必须分别采用表中定义，不移除本票的真实完整目标。

| 工作包 | 原范围与可独立验收 | 允许的测试边界 | 完成后仍由谁负责 |
| --- | --- | --- | --- |
| A：持久规划执行与 ID-only ingress | 持久原绑定、claim、原 Capacity receipt 只读恢复、取消/旧 term、输出 artifact/解析提案关联、owner 审核引用；真实 SQLite/Host 与业务 API 的反例 | 明确测试替身可以替代真实模型返回；不能生成可被产品视为真实的资格 | B 负责真实 transport/资格；C 与本票负责真实源、真实输出、owner 批准和下游集成。A 通过不关闭本票 |
| B：一个选定官方执行器的受控只读规划 transport | 固定一个真实 Commander Profile 的认证/模型/native settings、只读输入、实际调用与有界取消/usage；直接实现 A 的消费协议，不只有 parser | 先本机/协议假服务验证；关闭 B 若承诺真实 transport，必须补该同一 Profile 的实际 S 证据 | A 负责原业务/资源绑定；C 与本票负责完整 Run 准入/输出/批准。未资格来源保持 blocked |
| C：真实规划 → owner 批准 → 真实批准 Task 消费 | 使用 A+B 生产工厂，无 fixture/imported-status 快捷路径；实际模型输出与原准入/预算 trace、owner 确切批准和下游任务入口关联 | C 的 S 验收不可由替身代替；故障反例仍可 C/P 分别记录 | 本票只在 A/B/C 完整验收后关闭；#12 继续承担其余顾问与交接/独立任务继续范围 |

建议 A 可立即进入本地实现；B 的离线 transport 也可并行。B 的真实启用必须确定具体合格来源，缺来源不能为了完成 C 改用测试 reader。C 在 A/B 达到各自当前源验收之前不能标记完成。

## 状态、恢复和权限要求

- 区分准备、原准入已提交、启动/发送未知、执行中、输出已捕获、计划已提交、失败、取消、待核对；不能用单一 success 字段掩盖资源或模型未知。
- 原准入已 commit 但返回丢失：用原 key/request 的只读 receipt 查回，不能再次 admit/activate 获取新身份。没有 receipt 保留未知并阻止效果。
- 启动/发送 claim 丢回复：不重新 spawn 或重新 grant；只能观察原 Attempt，按照已验证的 executor 重传语义恢复。不能把同 prompt 当稳定调用身份。
- 取消先撤销将来的效果权限，再停止精确原 owned Host/native；完整 frozen manifest/ProcessSpec/fence/source 绑定不符时不得取消其他执行。远端停止和结算不明保持 unknown，不退款、不清零原上界。
- 当前 source/credential、规划授权或 term 改变：停止新的效果；已完成原输出保留历史，但不可成为当前 term 的新有效计划。顾问输出只能成为顾问 artifact，不可通过 receipt 变成 lead 提交权。
- 只读规划的 OS/工具边界必须实际验证：禁止写仓库、执行任意 shell、MCP/hook/子 agent 绕过；工具集合和网络出口只限当前明确配置。无需读文件的最小规划来源可使用受控快照作为输入，但仍不能声称未测的仓库工具能力。
- 订阅不可见配额允许已批准有限保守模式，未观察余额保持 unknown。现金路径如果价格/所有收费项/上界不可证明则拒绝，不把 token 数或原生 `total_cost_usd` 字段自动当真实账单或硬限额。

## 验收标准

- [ ] A：真实公开持久 API 通过 owner/Run/intent/term/Profile/budget/source 身份拒绝、幂等重开、输出不可变与 ID-only 外部入口测试；非法 `admitted`/payload 不能造生产 receipt。
- [ ] A：原 Capacity admit/activate 回复丢失能只读查回；unknown 无新预留/claim/进程/HTTP；receipt 改绑或过期拒绝。并发取消与 effect 的边界有实际反例证据。
- [ ] A：顾问/旧 term/非当前主 Commander 不可提交；owner 必须批准确切当前 plan、authorization、configuration 和 routing digest；旧版本批准无效。
- [ ] B：一个明确指定的真实 Commander Profile 完成受控只读规划 transport 资格；认证/模型/原生设置、实际 OS/工具/network、可见 usage/停止边界及有限配置逐项记录，不继承 Worker/另一执行器证据。
- [ ] B：未批准前原仓库完整基线不变；拒绝写/越界路径、原生附加工具与未授权委派的证据对应实际启用工具，不把 prompt 指令当隔离。
- [ ] C：至少一次真实 Commander 调用发生于原业务和资源准入之后；原规划 budget trace、Capacity request/receipt、实际执行身份、原输出 artifact、解析计划及 owner 批准可逐项关联。失败、重试、未知调用全部保留，不能只保存最终通过结果。
- [ ] C：无需 fixture reader 或 DB 修改，真实计划通过现有 `RunPlanner` 版本校验和 owner 批准；批准的 T1 Task 被实际任务准入/Workspace 消费。当前合格 Go Worker 可作为下游验收，但它继续独立检查自身最新 v2 来源/授权。
- [ ] C：至少一个该真实批准 Task 经固定生产 Task 入口产生实际 Candidate；Candidate 必需检查与独立 Review 仍按原状态展示，未执行时不可称 PR 可交付。下游已由其他票完成的证据必须绑定同一实际输入与当前 source，不能只引用功能存在。
- [ ] 每项证据标 C/U/P/S/G、固定 commit/source、case、Profile/runtime/OS、日期、命令和实际副作用。C/P 替身覆盖与 S 真实覆盖分开；发布包不含 key、capability、原始认证材料或私有库。
- [ ] PR 具有当前候选 CI 和独立 Standards/Spec 审查；合入 dev 后逐项核对本票完整原范围再关闭。只有 A 或 B 通过时本票保持 Open。

## 明确不在本票的范围

不重写 Rulebook 排序、共享容量或原币账本；不同时打通所有 provider；不自动切换主 Commander；不增加本轮现金授权。本票只启用经过此次准确资格和绑定的单个真实 Commander 来源。

父 #12 的真实顾问完整运行、实际替任启动和交接等待期间独立任务继续，若未在本票用例覆盖，继续留在 #12，并各自有明确剩余责任；不能因为主 Commander 桥完成而关闭 #12。#18/#19/#21 的其他角色/工具/计费范围、#23 旧 Run 重评估、#24 公平队列/全部窗口、#25 全角色原币对账也不由本票代为完成。

## 当前可复用端口与缺口（审计于 624ad8b）

| 模块 | 已有公共能力 | 本票必须补的绑定或限制 |
| --- | --- | --- |
| `RunPlanner` | create、planning_intent、attach_planning_receipt、submit_plan、approve_plan、activation_guard、现有 term/交接控制 | production reader 未实现；现 receipt 无真实 output/execution/source/预算证明，需生产受控关联；不是把 provenance 字符串改成 live |
| `CapacityStore` | `admit` 持久完整 request；`activate`；`pre_effect_guard`；`command_receipt(admit/activate/reconcile/cancel_unactivated)` 精确请求只读查询；routing_facts/snapshot | request 含 Run/Attempt/Profile/role/purpose/auth ref/Rulebook rev/duration/demand，但没有 planning_intent/term/principal/planning_budget_ref。需在 controller 的不可变 binding 中准确关联，不能只把 admitted 映射成完整 PlanningReceipt |
| `ResourceBroker` | 原币/父预留/调用/settle/unknown 的本地行为实现 | 明确限定 `local_fake` 和 loopback，不能作为现成真实现金出口。真实预算/订阅消费接线按 #25 具体边界推进，不制造硬预算证明 |
| `RunnerHost` | 固定 ProcessSpec、一次 initialize_control、start、实际 child 身份 guard、inspect、完整 expected_binding cancel | 只提供进程身份/生命周期；不自动证明模型已准入、只读 OS 工具限制或真实订阅认证 |
| 纯 routing | rule/group、role/purpose、能力、限额与候选检查 | `ApprovedRunRouting` 依赖已有批准 Plan，不能直接拿来生成规划前置。需从原 Run ceiling/intents 构造受控规划快照，复用纯管线，不绕过当前资格 |
| Codex adapter | `adapters/codex/replay.py` + permissions；纯离线 app-server 记录/权限回放 | 没有产品实时 planning transport。#18 还需官方订阅固定身份/真实角色输出与权限资格 |
| Claude adapter | `native.py` 是 wire schema，`replay.py` 是解析/消费回放 | 文件名 native 不表示已实现实际控制 transport；#19 的真实订阅/只读工具/停止路径待接 |
| OpenCode/Go | 受控管理通道、真实 relay、持久 Journal、固定 v2 qualification、Task read/edit producer | 现有实际 scope 仅 T1 Worker 现有文件；Task grant subject 不支持 planning，qualification grant 也不能冒用 Run。若选 Go Commander，必须独立新增可辨别规划 subject/输出协议和对应资格，不把 run_id 填进 qualification_id |
| DeepSeek | 请求/响应协议、固定 read 子集和本机 fake transport | 官方 endpoint 仅身份元数据，当前产品没有 live-account 入口；现金实测仍暂停，不作为快捷后备 |

这些结论仅来自仓库源码审计，不表示本机官方工具一定未安装或账户不存在；没有启动任何 provider 或扫描登录材料。

## 依赖与管理状态

**已完成**：父 #12 的持久计划/最小人工交接与 v2 owner 批准入口已分片交付；本票可直接复用其领域校验。Capacity/Host/Worker 生命周期存在可复用实现；不将其旧局部验证提升成此次真实规划证据。

**剩余工作**：A/B/C 全部条款。发布后将原生子任务号和负责人回填本表；若不拆票，仍按同一责任划分并行实施。

**阻塞**：C 的真实执行依赖一个准确、可用且具规划资格的 Commander Profile 与生产预算/容量 reader/transport。A/B 离线工作无需因此停止。ChatGPT 与 Claude 是可选择的来源依赖，不能把 #18、#19 整票都设为必须完成；如选择 Go 则只取 #21 对此次规划 scope 的准确子任务。原生 blocked-by 应指向所选具体可交付子票，而不是为了关联而形成全票循环依赖。

独立 S 验收只在既有具体授权内执行。后续官方调用前由 root 核对来源配置，不在 Issue 中索取密钥值。当前草稿基于 2026-09-06 实际读取的 #12/#18/#19/#21/#23/#24/#25 正文与候选 `624ad8b8490003f155baf7842ba91b9975b9526a`；远端状态发布时再次核对。
