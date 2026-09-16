# r9 全量设计审核与 Agent 开发就绪清单

日期：2026-09-16。范围：活动 PRD FR01–29、UX-AC01–27，架构 01–13、全部新增验收，以及原完整 v1 的 A01–26/PRD-AC01–06/D01–09 和现有 Issue。审核基线 dev `75ec6e9fab3a6801007420557f2b1443124ee7f6`，远端原票清单 118 项，其中 47 项 Open。

本轮工作是设计、任务和状态审核，没有实现产品功能、重跑产品验收、执行真实模型请求或合并旧 PR。已有控制面结论引用原固定版本证据；旧失败、原票 AC 与发布快照保留。

## 审核结论与用户决定

用户意图已覆盖：同一 Hub 内完成自定义 Workflow 设计/部署/运行、授权角色按需求拆分派发、按真实资源并行/排队、独立质量与交付，并按每个角色/Agent、任务、模型和实际 provider 查看 token 用量。审核发现的主要问题是实现状态陈旧、运行接线缺少统一责任、r6–r9 尚缺完整任务承接、原票状态与当前候选不一致；这些已形成正文修订、[接线契约](../../architecture/13-development-integration-contract.md) 和下列可检验任务。

- **已确认：全功能开发，来源独立验收。** 缺账号、endpoint、预算/消费许可仅阻塞对应真实验收；其余实现可用本地受控 provider、真实数据库/进程/UI 验证。#1/#30 原完整 v1 的五来源及所有出口保留。
- **已确认：用户已确认：缺少精确 provider/token 可明确标未知或不完整，在原资格、授权、严格绑定与有限预算门全部满足时启用；完整统计独立验收。缺口使原硬门不可验证时仍阻塞，不新增消费许可。** 两项产品决定均已解决，详见 [决定记录](decisions.json)。

第三方厂商、来源实际账号/模型、网关安装配置、有限预算、项目基准/检查/交付目标等是运行前配置输入，已有明确填写位置和缺失拒绝语义；不是需要再设计一轮的产品分歧。开发 Agent 不得猜选付费厂商或消费额度。

## 审核发现与集中修订

| 发现 | 对开发的影响 | 集中修订/责任 |
|---|---|---|
| 活动文档仍把 #175–#178 全列为未实现 | 重建网关目录、编译/部署或调度控制面 | 08–11、两份 PRD、总览和业务顺序明确已有控制面，13 固定复用基线 |
| 仅声明 execution kind 也可能被误当可执行 | Designer 图能画但无实际业务运行 | RUNTIME/WF-KINDS 交付真实适配器；生产资格与 callable 分开 |
| 调度 Run 已存在，但尚未连通 Hub/既有执行身份 | 新旧 Run/Attempt 可能各自推进 | DEPLOY-RUN 复用 active→调度 Run，统一公开身份/受控一对一映射与唯一状态提交 |
| deploy_only 控制面被误当部署并运行 | UI“完成”却未执行，重试可能多开 Run | DEPLOY-RUN 的复合确认、唯一 Run 意图与故障恢复 |
| 固定 Go 资格失败被用于阻塞所有产品开发 | 新网关/Designer/UI 无故等待旧通道 | 新网关与原兼容路径分轨；只在需要该来源的执行/验收设置 gate |
| ModelCall/凭证描述仅绑定 Attempt | pre-Run Commander/Designer 用量遗漏 | 08 与13统一 ExecutionRef 和 project/conversation，USAGE覆盖全部调用 |
| 预算预留、实报token和服务额度容易混算 | 统计不准确，可能重复扣账 | 12/13明确原生单位、观察覆盖、reported/estimated/unknown和重复回执语义 |
| 模型别名或网关地址可能冒充实际provider | 无法验证用户要求的底层用量 | GW-CALL/GW-QUALIFY采集可信路由，USAGE逐字段标可见范围 |
| r9 无实现责任，原父票多有剩余AC | 全部Agent只修局部却无法完整交付 | 新增25个纵向/独立验收切片，配合原47票；下面矩阵逐项指定owner |
| remote ready 标签说明与领取流程不一致 | 依赖未完的票被当作立即可运行 | spec:ready、planning:decomposed、ready-for-agent分离并读回核验 |
| 旧 in-progress / worker / 初始候选文案仍活跃 | 重启已结束worker或丢弃原失败 | 原正文移入可展开历史，当前块只保留本轮状态/依赖；PR172/173沿原PR |
| 备份/恢复未明确覆盖新用量/配置/动态图 | 旧备份清零消费或重发执行 | MAINTENANCE与架构05覆盖新对象，restore epoch/撤销与unknown继续有效 |

本次未改变技术栈、单机/Windows+WSL2边界、无内置Agent人数上限、已批准范围自动执行、独立PR审查和用户决定合并。原两任务样本及两轮修复等既有政策按各自范围适用，不重复征询已确定事项。

## 标签与领取规则

| 标记 | 精确含义 |
|---|---|
| `spec:ready` | 目标、输入输出/负例、修改边界、原AC/证据及依赖已可供Agent执行；配置型gate明确，不表示依赖已完成 |
| `spec:pending-decision` | 已有具体任务和验收草案，但受尚未回答的产品决定影响；不能当作最终规格就绪 |
| `planning:decomposed` | 已读回原生子Issue，正文列明子票和剩余责任；只表示已拆分，不表示完成或全部子票可同时开工 |
| `status:ready` + `ready-for-agent` | 当前队列中无未满足实现依赖的叶子，可领取其明确范围；不授予现金、来源资格、产品批准或合并权限 |
| `status:queued` | 有效规格，等待依赖或未安排；依赖及解除条件写在正文 |
| `status:blocked` | 具体来源/配置/资格或授权gate阻止当前验收；写清缺项及解除条件 |

父范围与最终验收保持Open，不能因本次设计或标签更新关闭。已完成票只在存在真实原生子票时补“已拆分”标记，不改原completed状态。原验收始终有效，Agent Brief补充当前承接，不能覆盖原范围、降低失败判据或把旧pass转为当前pass。

## 新增完整开发切片

每票都含明确输入、可观察输出、正/负/恢复验收、证据层级和排除项；完整机器可读正文见 [task-specifications.json](task-specifications.json)。R9产品增量父范围：[R9 / #183](https://github.com/zhouy1017/Karajan/issues/183)。当前来源纯实测仍遵守已有授权，缺配置不能自动消费。

| Key / Issue | 交付行为 | 直接实现依赖 | 原生父范围 | 证据 |
|---|---|---|---|---|
| [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184) | 网关受控调用、实际路由回执与会话调用记录 | 无实现依赖 | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185) | 受控 Agent 工具循环与真实执行身份接线 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [DESIGNER / #186](https://github.com/zhouy1017/Karajan/issues/186) | Hub 对话生成 Workflow 与同源图表编辑 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184)、[RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185)、[#165](https://github.com/zhouy1017/Karajan/issues/165) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [COMMANDER / #187](https://github.com/zhouy1017/Karajan/issues/187) | 网关 Commander 持续对话、可信规划与可编辑分工 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184)、[RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185)、[#165](https://github.com/zhouy1017/Karajan/issues/165)、[#170](https://github.com/zhouy1017/Karajan/issues/170) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188) | 确认部署并运行与冻结输入到 Run 的完整入口 | [RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185)、[#165](https://github.com/zhouy1017/Karajan/issues/165)、[#170](https://github.com/zhouy1017/Karajan/issues/170) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [ROLE-RUN / #189](https://github.com/zhouy1017/Karajan/issues/189) | 真实角色调度、自适应并行与资源排队回到 Hub | [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [WF-KINDS / #190](https://github.com/zhouy1017/Karajan/issues/190) | Workflow 条件、人工节点、返工及产物种类执行 | [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [USAGE / #191](https://github.com/zhouy1017/Karajan/issues/191) | 同一 Commander 会话多维 token 与实际 provider 用量面板 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184)、[DESIGNER / #186](https://github.com/zhouy1017/Karajan/issues/186)、[DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P |
| [GW-QUALIFY / #192](https://github.com/zhouy1017/Karajan/issues/192) | 固定网关部署的真实来源、角色能力与路由计量资格 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184)、[RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185)、[CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/P/S |
| [R9-EXIT / #193](https://github.com/zhouy1017/Karajan/issues/193) | r9 全行为跨流程验收与开发完成证据 | [COMMANDER / #187](https://github.com/zhouy1017/Karajan/issues/187)、[DESIGNER / #186](https://github.com/zhouy1017/Karajan/issues/186)、[DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188)、[ROLE-RUN / #189](https://github.com/zhouy1017/Karajan/issues/189)、[WF-KINDS / #190](https://github.com/zhouy1017/Karajan/issues/190)、[USAGE / #191](https://github.com/zhouy1017/Karajan/issues/191)、[GW-QUALIFY / #192](https://github.com/zhouy1017/Karajan/issues/192) | [R9 / #183](https://github.com/zhouy1017/Karajan/issues/183) | C/U/P/S/G |
| [PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | 项目来源设置与可执行资格状态的真实工作台 | [#165](https://github.com/zhouy1017/Karajan/issues/165) | [#11](https://github.com/zhouy1017/Karajan/issues/11) | C/U/P |
| [REVISION / #195](https://github.com/zhouy1017/Karajan/issues/195) | 计划变更、任务复用及原授权外修订工作台 | [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188)、[ROLE-RUN / #189](https://github.com/zhouy1017/Karajan/issues/189) | [#15](https://github.com/zhouy1017/Karajan/issues/15) | C/U/P |
| [RULES / #196](https://github.com/zhouy1017/Karajan/issues/196) | Rulebook 编辑模拟发布的完整可恢复工作台 | [#165](https://github.com/zhouy1017/Karajan/issues/165)、[#170](https://github.com/zhouy1017/Karajan/issues/170)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#23](https://github.com/zhouy1017/Karajan/issues/23) | C/U/P |
| [CAPACITY / #197](https://github.com/zhouy1017/Karajan/issues/197) | 共享池多窗口与保护量的运行和工作台接线 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#24](https://github.com/zhouy1017/Karajan/issues/24) | C/U/P |
| [CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198) | 逐调用原币预算与未知消费核对闭环 | [GW-CALL / #184](https://github.com/zhouy1017/Karajan/issues/184) | [#25](https://github.com/zhouy1017/Karajan/issues/25) | C/U/P |
| [REASSIGN / #199](https://github.com/zhouy1017/Karajan/issues/199) | 获准换源、质量升级与累计修复的真实流程 | [ROLE-RUN / #189](https://github.com/zhouy1017/Karajan/issues/189)、[WF-KINDS / #190](https://github.com/zhouy1017/Karajan/issues/190)、[RULES / #196](https://github.com/zhouy1017/Karajan/issues/196)、[CAPACITY / #197](https://github.com/zhouy1017/Karajan/issues/197)、[CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198) | [#26](https://github.com/zhouy1017/Karajan/issues/26) | C/U/P |
| [HANDOFF / #200](https://github.com/zhouy1017/Karajan/issues/200) | 用户决定的 Commander 交接与检查点工作台 | [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188)、[RULES / #196](https://github.com/zhouy1017/Karajan/issues/196)、[CAPACITY / #197](https://github.com/zhouy1017/Karajan/issues/197) | [#27](https://github.com/zhouy1017/Karajan/issues/27) | C/U/P |
| [RECOVERY / #201](https://github.com/zhouy1017/Karajan/issues/201) | 跨调用、进程、调度和交付的取消与正常恢复 | [ROLE-RUN / #189](https://github.com/zhouy1017/Karajan/issues/189)、[WF-KINDS / #190](https://github.com/zhouy1017/Karajan/issues/190)、[REASSIGN / #199](https://github.com/zhouy1017/Karajan/issues/199)、[HANDOFF / #200](https://github.com/zhouy1017/Karajan/issues/200) | [#28](https://github.com/zhouy1017/Karajan/issues/28) | C/U/P |
| [MAINTENANCE / #202](https://github.com/zhouy1017/Karajan/issues/202) | 备份恢复、升级与保留策略的可操作产品 | [DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188)、[USAGE / #191](https://github.com/zhouy1017/Karajan/issues/191)、[REASSIGN / #199](https://github.com/zhouy1017/Karajan/issues/199)、[HANDOFF / #200](https://github.com/zhouy1017/Karajan/issues/200) | [#29](https://github.com/zhouy1017/Karajan/issues/29) | C/U/P |
| [CODEX-S / #203](https://github.com/zhouy1017/Karajan/issues/203) | 官方 Codex 订阅的产品角色与原生工具资格 | [#7](https://github.com/zhouy1017/Karajan/issues/7)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#18](https://github.com/zhouy1017/Karajan/issues/18) | C/U/P/S |
| [CLAUDE-S / #204](https://github.com/zhouy1017/Karajan/issues/204) | 官方 Claude 订阅的产品角色与全工具路径资格 | [#7](https://github.com/zhouy1017/Karajan/issues/7)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#19](https://github.com/zhouy1017/Karajan/issues/19) | C/U/P/S |
| [DEEPSEEK-S / #205](https://github.com/zhouy1017/Karajan/issues/205) | DeepSeek 官方 API 产品能力与原币对账资格 | [CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#20](https://github.com/zhouy1017/Karajan/issues/20) | C/U/P/S |
| [GO-S / #206](https://github.com/zhouy1017/Karajan/issues/206) | Go 兼容通道产品接入与原范围整合验收 | [#87](https://github.com/zhouy1017/Karajan/issues/87)、[#113](https://github.com/zhouy1017/Karajan/issues/113)、[#117](https://github.com/zhouy1017/Karajan/issues/117)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#21](https://github.com/zhouy1017/Karajan/issues/21) | C/U/P/S |
| [THIRDPARTY-S / #207](https://github.com/zhouy1017/Karajan/issues/207) | 指定第三方 API 的协议边界与产品来源资格 | [CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198)、[PROJECT-SETTINGS / #194](https://github.com/zhouy1017/Karajan/issues/194) | [#22](https://github.com/zhouy1017/Karajan/issues/22) | C/U/P/S |
| [V1-EXIT / #208](https://github.com/zhouy1017/Karajan/issues/208) | 原完整 v1 的全量证据、远端故障与性能出口 | [R9-EXIT / #193](https://github.com/zhouy1017/Karajan/issues/193)、[CODEX-S / #203](https://github.com/zhouy1017/Karajan/issues/203)、[CLAUDE-S / #204](https://github.com/zhouy1017/Karajan/issues/204)、[DEEPSEEK-S / #205](https://github.com/zhouy1017/Karajan/issues/205)、[GO-S / #206](https://github.com/zhouy1017/Karajan/issues/206)、[THIRDPARTY-S / #207](https://github.com/zhouy1017/Karajan/issues/207)、[RECOVERY / #201](https://github.com/zhouy1017/Karajan/issues/201)、[MAINTENANCE / #202](https://github.com/zhouy1017/Karajan/issues/202)、[REVISION / #195](https://github.com/zhouy1017/Karajan/issues/195)、[RULES / #196](https://github.com/zhouy1017/Karajan/issues/196)、[CAPACITY / #197](https://github.com/zhouy1017/Karajan/issues/197)、[CALL-BUDGET / #198](https://github.com/zhouy1017/Karajan/issues/198)、[#8](https://github.com/zhouy1017/Karajan/issues/8)、[#153](https://github.com/zhouy1017/Karajan/issues/153)、[#160](https://github.com/zhouy1017/Karajan/issues/160)、[#161](https://github.com/zhouy1017/Karajan/issues/161)、[#162](https://github.com/zhouy1017/Karajan/issues/162) | [#30](https://github.com/zhouy1017/Karajan/issues/30) | C/U/P/S/G |

GW-CALL的代码前置已由#175和资源原语满足，用量启用策略已确认，可领取；既有无阻塞叶子可同时核验/返修。没有“先关闭全部产品父票才能写代码”的依赖。每个后继在前置接口合入、证据适用且实际可执行后再升ready；原父票native blocked-by作为最终验收gate继续保留。COMMANDER/WF-KINDS负责新网关下的规划/独立Review/交付接线，R9-EXIT不依赖固定Go专属资格通过；原#112/#113/#153/#161/#162的剩余责任在V1-EXIT仍逐项核验，不被新通道通过替代。

## 现有47个Open Issue的当前承接

本表为本次发布快照，不是长期实时状态源。后续必须读回GitHub当前事实；旧正文/失败保留在原Issue历史区，原字节摘要见 [原票身份清单](original-issue-manifest.json)。

| Issue | 类型/轨道 | 当前需要做什么 | 计划标记 | 未完成依赖（已完成原依赖仍保留） |
|---|---|---|---|---|
| [#1](https://github.com/zhouy1017/Karajan/issues/1) | 产品总范围 | FR01–29 与原完整v1；汇总子票，不作为编码叶子。 | queued（聚合验收） | 无实现依赖 |
| [#6](https://github.com/zhouy1017/Karajan/issues/6) | 兼容隔离 | 补原Scope管理接口/凭据实际工具canary；原六项已勾选不替代这项缺口。 | ready / ready-for-agent | 无实现依赖 |
| [#7](https://github.com/zhouy1017/Karajan/issues/7) | 隔离验收 | 按启用工具实际canary验证可用且受限的执行环境；不接受全禁用冒充隔离。 | queued（依赖） | [#6](https://github.com/zhouy1017/Karajan/issues/6) |
| [#8](https://github.com/zhouy1017/Karajan/issues/8) | 完整来源出口 | 一官方订阅＋一API同环境真实资格及跨来源候选交接；现金许可未扩大。 | blocked（真实资格/配置 gate） | [#7](https://github.com/zhouy1017/Karajan/issues/7) |
| [#9](https://github.com/zhouy1017/Karajan/issues/9) | 可选 | Bernstein可选调查不进入关键路径，不因本次全功能开发默认启动。 | queued（可选，不派发） | 无实现依赖 |
| [#11](https://github.com/zhouy1017/Karajan/issues/11) | 需求聚合 | 保留#63已合入原语；PROJECT-SETTINGS补完整产品与原AC。 | queued（聚合验收） | 无实现依赖 |
| [#12](https://github.com/zhouy1017/Karajan/issues/12) | 需求聚合 | 有界规划、同版批准与人工主Commander交接全部保留；复用#93/#153/#160与HANDOFF。 | queued（聚合验收） | [#8](https://github.com/zhouy1017/Karajan/issues/8)、[#11](https://github.com/zhouy1017/Karajan/issues/11) |
| [#13](https://github.com/zhouy1017/Karajan/issues/13) | 需求聚合 | 串行执行、冻结Candidate、完整checks和独立质量门；#94已合入不等于#95真实Review。 | queued（聚合验收） | [#12](https://github.com/zhouy1017/Karajan/issues/12) |
| [#14](https://github.com/zhouy1017/Karajan/issues/14) | 需求聚合 | 独立交付及同一PR；#66原语、#162当前候选展示和V1-EXIT远端故障共同覆盖。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#15](https://github.com/zhouy1017/Karajan/issues/15) | 需求聚合 | REVISION承接全部计划修订与复用，原A/B用例保留。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#16](https://github.com/zhouy1017/Karajan/issues/16) | 需求聚合 | 由#159与RECOVERY覆盖持久视图和有效动作；原依赖保留为最终出口。 | queued（聚合验收） | [#14](https://github.com/zhouy1017/Karajan/issues/14)、[#15](https://github.com/zhouy1017/Karajan/issues/15) |
| [#17](https://github.com/zhouy1017/Karajan/issues/17) | 需求聚合 | #161两任务最小真实并行样本保留，最终#14/#15 gate不取消；ROLE-RUN不替代原票。 | queued（聚合验收） | [#14](https://github.com/zhouy1017/Karajan/issues/14)、[#15](https://github.com/zhouy1017/Karajan/issues/15) |
| [#18](https://github.com/zhouy1017/Karajan/issues/18) | 来源需求 | CODEX-S验证原官方执行端路径；网关资格不替代。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#19](https://github.com/zhouy1017/Karajan/issues/19) | 来源需求 | #65原边界证据保留，CLAUDE-S补原角色/工具真实接入。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#20](https://github.com/zhouy1017/Karajan/issues/20) | 来源需求 | #67离线证据保留，DEEPSEEK-S补真实API原币资格，未授权仍阻塞S。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#21](https://github.com/zhouy1017/Karajan/issues/21) | 来源需求 | 保留全部原Go子票/失败；GO-S整合原角色与计量范围。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#22](https://github.com/zhouy1017/Karajan/issues/22) | 来源需求 | THIRDPARTY-S先本地协议开发，用户指定厂商/endpoint与许可后实测。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#23](https://github.com/zhouy1017/Karajan/issues/23) | 需求聚合 | #71/#72/#73/#75/#78已实现原语；RULES承接原完整工作台与回归。 | queued（聚合验收） | [#15](https://github.com/zhouy1017/Karajan/issues/15) |
| [#24](https://github.com/zhouy1017/Karajan/issues/24) | 需求聚合 | #68/#70/#74已实现原语；CAPACITY承接跨Run实际准入与UI。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#25](https://github.com/zhouy1017/Karajan/issues/25) | 需求聚合 | CALL-BUDGET逐请求现金约束、未知核对；USAGE不是原币预算的替代。 | queued（聚合验收） | [#13](https://github.com/zhouy1017/Karajan/issues/13) |
| [#26](https://github.com/zhouy1017/Karajan/issues/26) | 需求聚合 | REASSIGN承接全部获准自动换源/升级/累计修复。 | queued（聚合验收） | [#17](https://github.com/zhouy1017/Karajan/issues/17)、[#25](https://github.com/zhouy1017/Karajan/issues/25)、[#24](https://github.com/zhouy1017/Karajan/issues/24)、[#23](https://github.com/zhouy1017/Karajan/issues/23) |
| [#27](https://github.com/zhouy1017/Karajan/issues/27) | 需求聚合 | HANDOFF承接主Commander人工交接全部原AC。 | queued（聚合验收） | [#23](https://github.com/zhouy1017/Karajan/issues/23)、[#24](https://github.com/zhouy1017/Karajan/issues/24) |
| [#28](https://github.com/zhouy1017/Karajan/issues/28) | 需求聚合 | RECOVERY承接真实进程与各故障窗口，不使用模拟inspect代替。 | queued（聚合验收） | [#16](https://github.com/zhouy1017/Karajan/issues/16)、[#27](https://github.com/zhouy1017/Karajan/issues/27)、[#26](https://github.com/zhouy1017/Karajan/issues/26) |
| [#29](https://github.com/zhouy1017/Karajan/issues/29) | 需求聚合 | MAINTENANCE承接备份恢复/升级/清理及新对象。 | queued（聚合验收） | [#16](https://github.com/zhouy1017/Karajan/issues/16)、[#26](https://github.com/zhouy1017/Karajan/issues/26)、[#27](https://github.com/zhouy1017/Karajan/issues/27) |
| [#30](https://github.com/zhouy1017/Karajan/issues/30) | 最终验收聚合 | V1-EXIT承接完整原AC及新增r9证据；父票保持Open。 | queued（聚合验收） | [#18](https://github.com/zhouy1017/Karajan/issues/18)、[#19](https://github.com/zhouy1017/Karajan/issues/19)、[#20](https://github.com/zhouy1017/Karajan/issues/20)、[#21](https://github.com/zhouy1017/Karajan/issues/21)、[#22](https://github.com/zhouy1017/Karajan/issues/22)、[#28](https://github.com/zhouy1017/Karajan/issues/28)、[#29](https://github.com/zhouy1017/Karajan/issues/29) |
| [#87](https://github.com/zhouy1017/Karajan/issues/87) | 兼容实现 | 复用当前Go投影，逐条核验v2批准任务范围原AC；原失败保留，缺项返修。 | ready / ready-for-agent | 无实现依赖 |
| [#93](https://github.com/zhouy1017/Karajan/issues/93) | 兼容规划聚合 | #109/#110/#111与#112/#113共同覆盖原Go业务规划；新网关不替代。 | queued（聚合验收） | [#112](https://github.com/zhouy1017/Karajan/issues/112)、[#113](https://github.com/zhouy1017/Karajan/issues/113) |
| [#95](https://github.com/zhouy1017/Karajan/issues/95) | 兼容审查聚合 | 复用已完成输入/谱系/guard，#107/#116/#117补完整独立Review与S。 | queued（聚合验收） | [#93](https://github.com/zhouy1017/Karajan/issues/93)、[#107](https://github.com/zhouy1017/Karajan/issues/107)、[#116](https://github.com/zhouy1017/Karajan/issues/116)、[#117](https://github.com/zhouy1017/Karajan/issues/117) |
| [#107](https://github.com/zhouy1017/Karajan/issues/107) | 兼容资格 | 原固定Go只读Reviewer官方资格；先完成#149公开consumer driver，保留18次历史请求及未通过事实。 | blocked（真实资格/配置 gate） | [#149](https://github.com/zhouy1017/Karajan/issues/149) |
| [#112](https://github.com/zhouy1017/Karajan/issues/112) | 兼容实现 | 原Go业务native transport/完整输入/逐send guard/output authority接线，资格probe不可替代。 | queued（依赖） | [#142](https://github.com/zhouy1017/Karajan/issues/142)、[#146](https://github.com/zhouy1017/Karajan/issues/146) |
| [#113](https://github.com/zhouy1017/Karajan/issues/113) | 兼容真实验收 | 当前Go Commander资格→真实业务Plan→用户同版批准→Candidate；不是资格探针本身。 | queued（依赖） | [#112](https://github.com/zhouy1017/Karajan/issues/112)、[#147](https://github.com/zhouy1017/Karajan/issues/147) |
| [#116](https://github.com/zhouy1017/Karajan/issues/116) | 兼容实现 | 原Reviewer实际native执行、Evidence和当前验证收据接线，#143只提供intent/Host前置。 | queued（依赖） | [#143](https://github.com/zhouy1017/Karajan/issues/143) |
| [#117](https://github.com/zhouy1017/Karajan/issues/117) | 兼容真实验收 | 业务Candidate的实际独立Review及规划整链S；fixture或角色自述不满足。 | queued（依赖） | [#107](https://github.com/zhouy1017/Karajan/issues/107)、[#113](https://github.com/zhouy1017/Karajan/issues/113)、[#116](https://github.com/zhouy1017/Karajan/issues/116) |
| [#142](https://github.com/zhouy1017/Karajan/issues/142) | 原候选核验 | 现有快照源码/PR148证据可复用；按原六AC补齐重开/路径/零效果并绑定当前版本，不重复另建快照。 | ready / ready-for-agent | 无实现依赖 |
| [#143](https://github.com/zhouy1017/Karajan/issues/143) | 原候选核验 | PR150已合入intent/Host前置，按原七AC核验范围和当前证据；native Review仍归#116。 | ready / ready-for-agent | 无实现依赖 |
| [#146](https://github.com/zhouy1017/Karajan/issues/146) | 兼容实现 | 完整需求/验收/所有快照文件与schema进入原ID-only编译及pinned tokenizer，超限拒绝不剪裁。 | queued（依赖） | [#142](https://github.com/zhouy1017/Karajan/issues/142) |
| [#147](https://github.com/zhouy1017/Karajan/issues/147) | 兼容资格接线 | 核对已有producer/currentreader原C/P；#171补有限诊断后保留原S失败，未完成原资格前不能启用Commander。 | queued（依赖） | [#171](https://github.com/zhouy1017/Karajan/issues/171) |
| [#149](https://github.com/zhouy1017/Karajan/issues/149) | 兼容实现 | 原固定start的公开consumer正控与新身份恢复driver，保留旧计数/失败，无真实provider开发。 | ready / ready-for-agent | 无实现依赖 |
| [#153](https://github.com/zhouy1017/Karajan/issues/153) | 业务验收 | 当前持久Planning入口可复用，#112和#147完成后以真实完整Plan及用户精确批准验收原六AC。 | queued（依赖） | [#112](https://github.com/zhouy1017/Karajan/issues/112)、[#147](https://github.com/zhouy1017/Karajan/issues/147) |
| [#159](https://github.com/zhouy1017/Karajan/issues/159) | P1聚合 | #164已合入；#165/#166与原PR172完成真实Hub页面U/C/P；不承担真实模型派发。 | queued（聚合验收） | [#165](https://github.com/zhouy1017/Karajan/issues/165)、[#166](https://github.com/zhouy1017/Karajan/issues/166) |
| [#160](https://github.com/zhouy1017/Karajan/issues/160) | P2聚合 | #170原PR173补不可变提案，真实输入/Hub批准按本票原AC；与#112/#147资格边界分开。 | queued（聚合验收） | [#159](https://github.com/zhouy1017/Karajan/issues/159)、[#170](https://github.com/zhouy1017/Karajan/issues/170)、[#112](https://github.com/zhouy1017/Karajan/issues/112)、[#147](https://github.com/zhouy1017/Karajan/issues/147) |
| [#161](https://github.com/zhouy1017/Karajan/issues/161) | P3真实验收 | 按原样本两独立writer实际窗口重叠、固定顺序组合并重跑checks；不是产品人数上限。 | queued（依赖） | [#160](https://github.com/zhouy1017/Karajan/issues/160)、[RUNTIME / #185](https://github.com/zhouy1017/Karajan/issues/185)、[DEPLOY-RUN / #188](https://github.com/zhouy1017/Karajan/issues/188) |
| [#162](https://github.com/zhouy1017/Karajan/issues/162) | P4真实交付 | 当前Candidate的checks/独立Review/diff/head/PR真实展示与交付核对。 | queued（依赖） | [#161](https://github.com/zhouy1017/Karajan/issues/161)、[#116](https://github.com/zhouy1017/Karajan/issues/116) |
| [#165](https://github.com/zhouy1017/Karajan/issues/165) | 候选返修 | 沿PR172修复草稿替换身份、实际planning Attempt选中DTO、output_received命名SSE遗漏及当前真实浏览器验收。 | ready / ready-for-agent | 无实现依赖 |
| [#166](https://github.com/zhouy1017/Karajan/issues/166) | 候选复核 | 组件已在PR172，核验原反馈状态/时间/终态/无颜色/reduced-motion全部AC并复用原PR。 | ready / ready-for-agent | 无实现依赖 |
| [#170](https://github.com/zhouy1017/Karajan/issues/170) | 候选复核 | 沿PR173/e23abc0复核已修批准路由与当前dev整合，全部原拒绝/幂等/实际绑定AC保留。 | ready / ready-for-agent | 无实现依赖 |
| [#171](https://github.com/zhouy1017/Karajan/issues/171) | 兼容诊断实现 | 只补有限内容无关语义失败码，不修改严格语义门、不改prompt、不发真实模型请求。 | ready / ready-for-agent | 无实现依赖 |

PR172 当前候选 `f70b3b93abcd47889c882f33ae92d53b941587f0`（#165/#166）与 PR173 `e23abc0d819ce71179a72b8010f8b4bb35d9b074`（#170）继续沿原PR返修/核验；本次没有接受候选或改动代码。#165/#166共用PR172，实际分派须指定同一集成owner及互不冲突的写区，不由多个Agent同时推同一分支。

## 开发和验收的完成条件

- 先读当前dev及固定规格版本，复用已有API/存储/控制面；真实路径经过配置、授权、调用/执行、产物与Hub，不能只完成类型或新配置字段。
- 每票保留原AC并按C/U/P/S/G逐项绑定当前SHA、输入、命令/操作、实际结果和限制。失败、not_run、unsupported、旧候选不能被标签或设计更新升级。
- 新增r9完整路径由R9-EXIT核验；原完整v1由#30/V1-EXIT核验，包括五来源、原生专属路径、恢复/维护、预算/换源、真实远端和性能。两者不互相替代。
- 当前CI与独立Standards/Spec是开发PR合并前的门；合并权限依后续实际授权，本次准备不自动合并产品PR或发布产品。

## 发布记录

远端Issue、标签、原生父子/依赖和当前正文已读回核验，详见 [publication.json](publication.json)。这表示任务准备完成，不表示产品已实现。

首次发布快照读回 73 个 Open Issue：65 票规格完整，8 票当时待决定，9 个当时可领取范围；另有 37 票（含已完成父票）标识已拆分。已关闭票遗留的领取标签已按 publication 的 closed_queue_cleanup 单独清理，原完成状态和正文不变。

[文档校验记录](document-validation.json)仅验证本地链接/表格、原始编号与责任映射、依赖无环和diff格式，不是产品行为验收。

2026-09-16 用户已确认最后一项决定；[确认后发布读回](decision-publication.json)核验 73 个 Open Issue 全部为 spec:ready，0 票待决，10 个当前可领取范围，37 票已拆分（含已完成父票）。#184 为新增可领取任务，其余可领取范围为 #6/#87/#142/#143/#149/#165/#166/#170/#171。原生关系、原验收和历史失败保持不变；首次 publication.json 保留原字节。设计与任务已达到完整 Agent 开发就绪，后继仍按依赖领取，来源资格/配置/消费许可按各自验收。

[确认后文档校验](decision-validation.json)记录本次规格修订的校验范围；两项决定均有用户确认，不表示产品代码完成或已通过真实来源验收。
