# 完整产品的开发接线与验收边界

修订：2026-09-16，r9 开发就绪审核。本文冻结跨模块责任和集成出口，不替代 01–12 的领域契约，不宣称产品已完成。用户已确认：全功能开发、来源独立验收；账号、具体 endpoint、额度和消费许可缺口只阻塞对应实测，完整 v1 的原出口不降低。任务与证据映射见 [开发就绪清单](../planning/r9-development-readiness-20260916/README.md)。

## 1. 已有基线与剩余范围

审核基线为 dev `75ec6e9fab3a6801007420557f2b1443124ee7f6`。以下是源码和已有固定版本证据的复用边界，本次未重跑产品验收。

| 已有能力 | 直接复用 | 尚须交付 |
|---|---|---|
| Project/Conversation/Run、消息/草稿、Hub 读模型 | PR167 及当前会话 API | PR172 的真实页面恢复缺陷、P2 提案 PR173 的独立核验；不能从持久后端推断完整 UI 通过 |
| 网关连接/绑定版本、无推理目录探测 | #175 / PR179，GatewayCatalogStore | 受控推理、可信路由/用量采集、协议与工具循环资格；目录可见不是来源合格 |
| 实际配置文件、编译器、角色与同源预览 | #176 / PR180，WorkflowStore/compiler | Designer 实际生成、浏览器图表/表格/文件往返及消费边界 |
| pending/加载/读回/active、回滚与进程恢复 | #177 / PR181，DeploymentStore/loader | `deploy_and_run`、具体输入到 Run 的冻结绑定和执行；现有实现明确拒绝该动作 |
| active 定义到调度 Run、SchedulerGrant、决定/图修订、claim/队列/封口/义务 | #178 / PR182，SchedulingStore（create_run 已经读回 active 并冻结具体输入） | 复用该调度 Run 接通 Hub/既有业务执行身份、来自实际角色的决定、claim 到物理执行及结果/停止核对 |
| 执行种类注册表 | 已声明六类种类，只有 `artifact_aggregate@1` 有可用业务适配器 | agent_task、deterministic_check、human_decision、candidate_integrate、publish_pr 的生产接线与各自能力门 |
| Planning、资源、Checks、Review、Delivery 的原语与兼容通道 | 按原候选和原 AC 复用 | 尚未满足的业务闭环与来源资格；不能把新网关通过回填为原 Codex/Claude/Go 路径通过 |

已有 HTTP 路由和持久对象为扩展起点，禁止另起一套会话、预算、任务图、批准或统计权威。代码中的可调用适配器只证明实现存在；真实来源启用还需当前 Profile/Runtime/网关配置资格，fixture 注册不得成为生产 enabled。

## 2. 从一次会话动作到结果的接口责任

| 接线边界 | 输入及产生的持久事实 | 拒绝、幂等和恢复要求 |
|---|---|---|
| 会话 → 创作/规划调用 | 可信 project/conversation、用户消息版本、明确创作/规划授权、ExecutionRef、固定 Profile/ModelBinding、累计预算 | 浏览器只提交意图和已登记选择；不接收自报 prompt authority、来源资格、admitted 或模型结果。未获准不发送 |
| 调用授权 → 网关/显式兼容执行器 | 固定身份/变换策略、输入与完整上下文摘要、有限输出/时间/预算；send intent、发送回执、结果及 usage | 发送前重新核对当前授权/来源/容量；未知不盲重发，显式兼容路径不会成为网关静默后备 |
| Designer → 配置服务 | 完整受控包文件或针对当前 proposal 的结构化编辑，由可信服务生成 manifest、保存新 revision 并编译 | 不能提交宿主路径、可执行导入或伪造摘要；校验修复走同一累计预算，旧输出不能覆盖新编辑 |
| 预览 → 部署 → Run | 精确确认绑定会话、preview、bundle/compiled digest、slot revision；运行另绑定 input/run_plan digest、初始授权与 grant | 同一复合命令关联唯一 Run 创建意图；active 完成不等于 Run 成功。启动应答未知只查询原 Run，不重新部署/重建 |
| Run → 调度角色 | 冻结定义/输入/授权、当前图 revision、可信触发事件、已有产物和未完成义务 | 模型返回 SchedulingDecision；当前执行身份/grant 由服务注入并校验，不能相信模型自填主体。机械排队/唤醒不再次调用模型 |
| 调度 claim → RunnerHost | task revision、Attempt/ExecutionRef、有效 claim/activation、Profile、工作区和预算；持久 start identity | 接受 claim 不等于进程已启动。生产调度登记必须关联真实 Run，管理测试入口不能替代用户批准；未知 spawn 保守核对 |
| RunnerHost → 业务结果 | 原执行身份的事件、固定 Artifact/Candidate、退出/停止与实际消费；可信校验后满足该版本结果契约 | 模型自报完成不直接 seal 结果；迟到旧版本不满足新任务，物理执行未结束不释放互斥，费用独立核对 |
| 执行结果 → checks/review/交付 | 当前 Candidate、检查政策及全部 Evidence、独立 Reviewer context、产物目标与交付授权 | 新候选重验相关证据；report/patch 不强制 PR，PR 复用独立交付和 expected-head 核对，不自动 merge |
| 全部调用 → 会话用量 → Hub | pre-Run 或 Run/Task/Attempt 归属、角色定义/实例、请求与实际来源、原始 usage/覆盖关系 | 所有调用路径接入同一统计入口；版本化映射、未知/估算、覆盖去重及查询快照按 [12](12-conversation-usage-accounting.md) |

Run 的生产构造复用 SchedulingStore.create_run 已有的 active 读回、具体输入/授权冻结和重放能力，不再另建一套部署 Run。下一步将其与 Hub、既有 Run/Attempt/RunnerHost 业务身份接通：一个业务运行只有一个公开 Run 身份；需要兼容映射时持久化一对一映射，校验双向归属和原摘要，不能存在各自独立推进的两份 Run 状态。管理测试入口不能替代用户批准，调用者传入 run_id 也不能成为授权证明。

角色和任务数量不设产品默认上限；接口 payload、模板文件数量、批处理或分页上限属于传输/存储边界，实际运行图须分批扩展并保留全量身份。物理并行按真实资源及显式用户政策准入，不把实现队列大小暴露成业务人数上限。

## 3. 可独立实现的协议与配置边界

现有 `workflow.v1`、`karajan.workflow-bundle.v1` 与编译器身份为当前 schema 基线，真实布局为 manifest.json、workflow.yaml、roles/、templates/，以受管布局校验为准。架构中的示意配置不是另一个解析协议；可运行样例先使用已有 examples/workflows 及原实现证据。兼容扩展须版本化并保留旧已批准包，不能为新功能原地改写旧文件/digest。

输入/输出契约与执行种类均由可信注册表提供。首版交付六种已声明种类及文本/结构化/代码候选/审查决定的已登记契约；不承诺用户上传任意插件或代码定义新的执行语义。用户可组合、新建角色和 Workflow，选择已登记的契约、工具和检查。未知契约、未知工具、未知种类或缺适配器均定位拒绝；新增种类是后续代码任务。

实现可以选择内部类/表结构，但不能把以下决定留给 Agent 猜测：对象身份/归属、授权与来源冻结、调用覆盖关系、结果终态与物理停止的区分、当前版本拒绝、未知语义、产物义务和交付门。现有 API 错误码保留；新增拒绝使用稳定、内容无关的 reason code，前端必须展示原因和可行动入口，不能只返回 success=false。

环境配置由所有者在设置/批准入口提供，全部以非秘密引用传入任务：网关版本与 endpoint、账户/secret_ref、Profile/模型、允许工具与数据去向、有限调用/输出/时间/修复预算、项目基准/checks、交付 repo/base/head 以及保留期。缺值阻塞对应启用/运行，不要求重新决定已确定的产品方向，不允许开发 Agent 猜选第三方厂商或购买额度。开发可用本地受控 provider 与假秘密验证；真实消费须按已有授权另作来源验收。

## 4. 用量接线必须覆盖的调用集合

| 调用来源 | 必需归属 | UI 分组 |
|---|---|---|
| 会话回复、独立规划/顾问、Designer | project/conversation + 角色/实际执行实例；可无 Run/Task | 会话对话/创作或无任务；仍计入会话总计 |
| 动态调度角色、普通 Agent、Reviewer、返工 | 同一会话及原 Run/Task/Attempt/角色 revision | 自身消费，按需展开子任务小计；改派的新实例与原实例分开 |
| 用量自然语言分析 | 发起请求的当前会话与实际角色调用 | 本次分析正常增加用量；读表、筛选与事件刷新不产生调用 |
| 网关内部发送、重试或兼容执行器汇总 | 原 call/执行覆盖范围；可观测 route 与其证据 | 可证部分分配；仅 Attempt 总量或混合来源不能伪造逐调用/逐 provider 分摊 |

实现先冻结一个可重放的归一化读模型：每个 token 指标包含 nullable 数值、reported/estimated/unknown、覆盖粒度/缺项和来源；每次查询返回 scope/filter/grouping、ledger revision、as_of、watermark、同快照总计及分组。时间采用 UTC 存储和半开区间 `[from,to)`，界面用用户时区显示；迟到 usage 按原调用时间归属，分页 cursor 绑定同一查询/快照。字段和 cursor 的具体编码由实现选择，上述语义不可省略。

统计去重必须区分调用幂等与用量回执幂等：没有可靠 logical call ID 的新 HTTP 接收仍按原 broker 规则重新准入，不能因 prompt/token 数相同而删除真实消费；对同一已识别发送的重复 usage 才按来源事件及覆盖去重。冲突观察不静默择较小值，估算被可靠实报覆盖后保留历史并退出同范围估算小计。

## 5. 完整开发、来源资格与交付三个出口

1. **开发可领取**：需求及输入输出/失败行为完整，原范围和排除项清楚，实际代码依赖已具备，且进入当前队列。`spec:ready` 仅表示规格完成；`ready-for-agent` 才表示当前可领取。真实资格失败不阻止无依赖的本地实现，但不能允许其生产启用。
2. **r9 完整行为**：真实页面完成文字创作→同源编辑→确认/部署→实际 Run→角色动态执行→report/patch/pr 与当前证据；同会话多维用量及可信 provider 关联贯穿，并证明重启/取消/迟到与无模型刷新。C/U/P 和 S/G 各自留证，不把脚本或 fixture 当作真实模型。
3. **原完整 v1**：继续按 #1/#30 的 FR、PRD/A/D 原范围、五来源、取消恢复/维护、预算/换源和真实交付逐项验收。原官方 Codex/Claude 或固定 Go 路径的要求不会因新网关验收通过而消失。来源未配置/失败/未授权消费继续保留缺项，不能写成完整 v1 已完成。

新功能切片合入只完成其自身范围。最终候选的 CI、独立 Standards/Spec、实际证据与合入树按 [Issue 流程](../agents/issue-tracker.md) 绑定；旧通过不升级新候选，旧失败不删除。P1/P2 现有 PR172/173 留在原 PR 返修/复核，准备开发不构成合并授权。
