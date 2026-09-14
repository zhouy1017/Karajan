# Rulebook、配额与换源

2026-09-14 r8 设计契约。Rulebook 负责来源选择与资源规则；获权 Workflow 角色根据实际任务作拆分、依赖、角色绑定、优先级和派发决定，见 [11](11-role-directed-scheduling.md)。引擎机械校验授权/能力和资源，不重新做业务分工。显式绑定优先，所有创作/调度/执行/审查/重试调用都准入计账。

Workflow 决定自定义职责与步骤，Rulebook 负责来源和资源选择。能力从执行种类、风险、产物政策和 RoleDefinition 约束计算，不从角色名称推断；控制提交身份、保护量资格与审查独立性由可信策略绑定。网关连接、真实模型映射与共享账户去重见 [外置网关](08-provider-gateway.md)，流程及批准见 [Workflow](09-configurable-workflows.md)，目标 Run 之前的 Designer 创作准入与部署见 [对话与部署](10-conversational-workflow-deployment.md)。这些是设计要求，不表示新接入已获资格。

## 1. 接入目录

| 对象 | 必需内容 | 不应混淆的对象 |
|---|---|---|
| Provider | 服务身份与官方/第三方来源 | 模型家族 |
| Account | 订阅或 API 计费主体、共享额度关系、网关来源证据；上游秘密由外置网关持有 | 某一个 API key 或一个网关 alias |
| Channel | 认证模式、endpoint/protocol、计费模式、可接受的数据去向 | Agent 角色 |
| Model descriptor | 厂商 ID、已知版本/家族、上下文与工具能力、证据日期 | 自报型号或营销能力等级 |
| Runtime descriptor | 固定执行器版本、OS/隔离、事件、取消与准入粒度 | 单纯模型 API |
| GatewayConnection / ModelBinding revision | 外置网关地址/协议/凭据引用、配置与变换摘要、公开模型及允许的真实来源映射 | provider 本身或未经验证的 model alias |
| Execution Profile revision | 上述绑定＋运行参数＋所需能力的验收结果＋角色引用约束＋配额池关联 | 一段模型名称字符串或固定业务角色枚举 |

Profile 生命周期为 `draft → qualifying → enabled → suspended / retired`。只有 enabled 且所需能力为 passed 的配置可接对应工作。capability 包含 `status / evidence_ref / tested_version / tested_at`，不能只写一个未经验证的 `supports_tools: true`。

第三方厂商可以有多个 endpoint，但用户配置的是明确允许的地址；模型不能构造新地址。相同模型经多个渠道调用不自动视为审查独立，也不自动视为独立额度。

## 2. Rulebook 的三层

| 层 | 内容 | 改变时的影响 |
|---|---|---|
| 协作约束 | 引用角色/步骤的 T0 准备度、能力约束、简报、验收、停止和交回条件；职责提示不授予工具权限 | 定义内容在 Workflow 中版本化；影响实例任务或授权时形成计划变更 |
| 能力映射 | execution_kind/required_capabilities/complexity/risk/domain 及显式 role_ref 约束 → 合格 Profile 集；参数、审查独立性、升级/换源边界 | 形成 Rulebook revision；运行采用需重评估 |
| 资源策略 | Run 固定的预算模式、排序与额外限制；另引用账户级 CapacityPolicy 的保留量、安全余量和未知模式 | Run 策略不静默替换；共享池全局当前策略对所有新准入一致生效 |

匹配维度包括执行契约、准备度、复杂度、风险、领域、工具要求、上下文需求、数据去向、最低隔离、独立审查与计费许可。角色引用可进一步收紧候选，但把角色命名为 Commander/Reviewer 不会获得规划预算、保护量或审查资格。确定性步骤不参与模型选路，只接受适用的本地资源准入。

| 工作类型 | 资格判断 | 建议候选类别 |
|---|---|---|
| T0：未决问题 | 不派实现步骤；先澄清目标、接口、授权或取舍 | 已获独立创作/规划预算的 Designer 或规划 Agent |
| T1：机械工作 | 已知范围、检查明确，风险仍单独判断 | `fast_qualified` |
| T2：有界实现 | 稳定接口、明确简报、常规实现判断 | `standard_qualified` |
| T3：复杂/关键任务 | 高复杂度或安全、数据、恢复等风险下限触发 | `critical_qualified` |
| 产物政策要求的独立审查 | 按审查范围复杂度/风险选；独立上下文、独立职责；PR 必需 | `review_*_qualified` |

这些组是用户配置的具体 Profile revision 集，成员必须有相应能力证据。高级模型可以进入多个组；便宜模型经验证也可以进入适当组。T3 不因资源不足降为 T2；并行拓扑不改变任务类别。

平台以项目可信规则计算风险下限，例如鉴权、数据库迁移、秘密处理、恢复协议等路径或职责触发额外门槛。Agent 分类和理由作为输入保留，不能覆盖更严格下限。

派生字段由编译器计算：复杂度只取 T1/T2/T3，顺序为 T1＜T2＜T3；T0 是 readiness 未完成，不进入此序列。`trusted_risk_floor` 来自已确认项目风险规则，基线 standard→T1、critical→T3；缺失风险映射拒绝准入。`effective_class = max(complexity, trusted_risk_floor)`。独立审查步骤取其审查范围内作者任务和集成变化的最高要求，不能用角色名称或只读权限降低风险等级。

## 3. 匹配与排序算法

规则采用显式 priority；先合并所有全局/项目硬约束，再选择最高优先级的唯一匹配路由行。同级多行同时匹配时返回 `RULE_AMBIGUOUS`，没有匹配时返回 `NO_RULE`；不靠文件排列隐含覆盖。约束组合取更严格值，允许集合取交集，不能被低优先级规则放宽。

```text
on dependency / result / quota observation / reset timer / user command:
  先处理核对、撤销和取消
  ready_tasks = 已接受运行图中依赖有效且有合法派发决定或静态规则的任务
  保留获权角色的优先级/依赖；按用户批准的公平/资源政策机械选择可准入项
  对每个 task:
    固定 graph/task revision + SchedulingDecision/授权范围 + rulebook revision
    binding_mode 为互斥的 strict-single / preferred-with-approved-alternatives / default
    strict-single：候选仅为 strict Profile revision；不得 fallback
    preferred-with-approved-alternatives：首 Attempt 仅为 preferred Profile revision
    default：匹配 Rulebook，得到默认候选 Profile revisions 并按规则排序
    仅对 preferred-with-approved-alternatives：旧 Attempt 已确定停止或隔离后，新 Attempt 才以 approved alternatives
      与原 authorization、Rulebook 硬允许集合取交集重新建候选；不与旧单 Profile 候选取交集
    过滤能力、隔离、工具、数据去向、独立性、计费和健康硬条件
    计算每个候选的资源向量及已知/估算/未知程度
    排除任一硬约束不满足的候选
    对剩余候选按版本化策略稳定排序
    在短事务内重查任务/池版本并尝试原子预留
    成功：写 Attempt、RouteDecision（注明 binding_mode 与 preferred/alternative）和 StartAttempt outbox
    失败：保留合法图与任务，记录资源队列/Blocker；不截断图、不自行改变业务分工
```

任务优先级和派发意图来自有效 SchedulingDecision 或已批准静态规则；Rulebook 的 Profile 排序不是另一套业务调度。用户可批准必要反馈保护、公平轮转/等待提升等共享资源规则，引擎按这些规则提供背压并解释暂未启动原因，不暗中用固定 Commander/Worker 队列覆盖角色决定。资源不足不删合法任务，优先级也不能越过授权/能力门。

默认先限制资格和资源，再采用固定排序元组：`策略偏好档位 → 不确定性档位 → 瓶颈配额压力 → 预计新增现金 → 预计完成时间 → profile_id`。数据不足的排序项明确 unknown，并由该策略的保守规则处理，不能默认填 0。

`preference_band` 来自当前规则的 `profile_preferences`，未指定为 0，数值小者优先，不能越过硬门槛。`uncertainty_band` 为 0（必需额度观察新鲜且需求可计算）、1（含已校准估算）、2（含明确许可的未知保守模式）；未许可的未知在过滤阶段拒绝。同档的压力按各可量化池“本次请求后的占用＋安全/资格保护量”除以该池上限后取最大值；无可计算值使用 unknown 哨兵、排在有值者后。延迟取相应 Profile/任务类别的已记录估计，未知也排在有值者后。排序输入和算法版本一并保存。

瓶颈压力只对可比较的、归一化后的各池占用比例取最大值。接近重置且有剩余额度可在同档候选中加偏好；必须同时满足周/月等较长窗口。优先用完订阅、最快完成可以作为另外两套显式策略，不与默认策略暗中叠加。

三种 binding mode 都执行能力、隔离、工具、数据去向、独立性、计费、健康和全部资源硬规则。strict-single 不合格即登记 Blocker，不由默认 Rulebook 回退；default 才使用 Rulebook 候选和排序。approved alternatives 不是初始选择，也不能改变当前 Attempt；它们只在旧 Attempt 已确定停止或隔离、并完成暂停/消费核对后，用于新 Attempt，绝不新增后台热切换。路由解释保存完整输入快照：binding mode、命中规则、候选及淘汰原因、各池观察、估计依据、排序值、最终配置及同分规则。模拟使用同一个求解器，但不预留、不调用模型；模拟结果只对所示快照有效。

## 4. 配额池与预算

一次请求使用资源向量，而不是一个“剩余 token”字段。例如：

```text
Go account:    {5h provider_value, weekly provider_value, monthly provider_value}
DeepSeek:      {account concurrency, account cash(currency), Run cash budget}
Subscription:  {reported short-window %, reported long-window %, local attempt slots}
All profiles:  {workspace writer exclusion, measured host capacity, explicit user limits, execution bounds}
```

这些是不同来源可能提供的计量形态，具体池/单位/可观测性由资格确认。Karajan 不固定全局或项目 coding Agent 人数，不设置新的默认数量；用户明确数量政策或实际账户/宿主能力是准入条件。每工作区单 writer 是冲突排他规则，资源不足时合法图排队，不被裁剪。Go 服务计量与 API 现金不同，来源依据见 [来源记录](sources.md#模型来源)。

每个池声明 `scope / unit / window_kind / window_identity / reset_at / limit / observation_source / freshness / coverage`。窗口可能是固定重置、滚动窗口或令牌桶，不能统一为午夜清零。服务商未报告重置语义时标记 unknown，不能自行猜测下次可用时间。

对于已有可靠用量覆盖关系的池，准入时计算：

```text
available = effective_limit
          - provider_reported_used
          - locally_incurred_not_covered_by_report
          - future_reservations
          - safety_margin
          - reserve_not_available_to_this_authorized_identity
```

以上数量必须属于同一池、单位和窗口。服务池与 Karajan 自身 allowance 池分开：服务池扣账户总体消费，本地 allowance 池只扣 Karajan 的消费。例如服务上限 100、平台份额 20、用户手动已用 50、平台尚未使用时，分别检查服务剩余 50 与平台剩余 20，不能用 20 减 50。余额型池使用 reported balance 减未覆盖支出与预留，不再次减一个已包含的 used 值。

每个适用池都必须满足需求。同一上游账户经多个 key、网关 alias 或原生兼容通道使用时按可信来源映射去重，不登记多份额度；一个请求不能只通过最宽松的窗口。网关本地余额或 `/models` 可见性不能替代上游消费与共享身份的证明。

### 4.1 预留的三个部分

| 部分 | 何时登记 | 何时释放/结算 |
|---|---|---|
| 并发占用 | 接受启动前 | 对应本地进程或已知服务并发结束后；两者单独判断 |
| 未来消费预留 | 接受 Attempt 或调用额度切片时 | 尚未发送的部分可释放；延长需重新准入 |
| 可能已发生/已发生未核对消费 | 网络发送前持久化 send intent，并从未来切片转入不可直接释放的 send_pending | 确定未发送才退回；发送未知进入 send_unknown，按计费上界占账，不能盲目重发 |

在 `BEGIN IMMEDIATE` 短事务中同时检查并登记所有池、预算、Attempt 和 outbox；事务内不请求服务商数据。实际值可能高于估计，差额必须入账并触发后续停止/重评估，不能为了保持“预算没超”截断账目。

Attempt 预算与逐调用预算采用父子额度切片：call 从 Attempt 已预留额度中领取，不能同时把父预留和全部子预留重复扣减。Designer 调用使用其创作执行身份的父预算，不能要求先创建目标 Run 才可准入。未知长度的工作分段申请；没有余额时不发送下一次请求。显式原生兼容 CLI 无逐次控制时只能按已许可且已验收的有界 Attempt 处理。

### 4.2 报告延迟与外部消费

QuotaObservation 分别保存服务端时间与本地接收时间。能够用 request ID、可信覆盖游标或明确时间区间核对时精确消除重叠。累计百分比如果没有覆盖范围，无法精确区分手动消费与本地消费；此时保存“已报告下界＋未核对上界”及置信状态，不伪造精确值。

无可靠覆盖的池可以保守计入未核对占用，但不能把它永久双算并展示成实耗；显示区间/待核对，并使用新增了可验证覆盖关系的报告、明确窗口重置或人工校准解决。仅有时间更新的累计百分比不构成覆盖证明。这些估计只描述已观察部分，未报告的外部消费仍可能存在，不能称为服务总体消费的严格上界。人工校准保留调整记录，不删除费用历史。

平台自己的预留不会锁住官方客户端额度。外部消费导致余量下降时，阻止新准入，必要时停止下一次调用或在执行器允许处取消；已经发出的请求仍可能消费。重置只处理确定所属窗口的预留/账项；跨窗口请求按照供应商归属，未知则保守保留并标记。

### 4.3 可信控制身份的保留量

保留量在共享池定义，不由各 Run 重复占有。默认代码模板的控制反馈保护可保留为显式用户政策，资格绑定实际授予范围/提交身份，不按 Commander 字符串，也不因委派复制账户额度。Designer 仅在独立创作授权含对应资格时可用。

共享池有一个当前有效的全局 `CapacityPolicy revision`；所有 Run 在新准入时读取同一版本，Run 固定策略只能增加限制。旧 Run 不能用自己的旧保留量绕过新门槛。修改策略通过账户设置原子发布，保留既有消费与预留，不凭新值释放在途占用；下一调用/扩展预算重新准入。降低保护量也不扩大某 Run 已批准的来源或预算。RouteDecision 保存 Run Rulebook 与全局 CapacityPolicy 两个版本。

已准入运行还可设置必要 review/收尾预算，避免把所有现金用于实现后没有资源验收。规划、顾问、上下文重发、失败、重试、修复和 review 都计入费用；不只计算产生最终代码的那次调用。

保留量是本地分配目标。配额未知时显示“估算保护”；没有观测/校准依据时采用保守并发和调用上限，不能保证受保护的模型调用永远可用。

## 5. 现金限制与未知额度

每个 Profile 分别声明：`admission_granularity = attempt | model_call`，`budget_enforcement = estimated_stop | bounded_calls`。第一项表示能在哪里阻断，第二项表示开销能约束到什么程度；品牌不能决定这些字段。

`bounded_calls` 只有在所有请求必须经 broker、价格/计费路径明确、输入与最大输出及其他收费项有可靠上界、并发额度原子分配时才成立。计算各调用可计费上界后再发送。未知的缓存费、工具费、隐藏路由或不受限服务端生成会使这个能力不成立。

现金原币记账，USD/CNY 等币种分别设置硬预算，不直接相减或相加。调用绑定 price_revision 与计费上界；价格失效或变更后重新准入，无法获得可靠上界时不发送硬预算调用。跨币种成本排序只在有适用且版本化的参考汇率快照时比较；缺少快照时该候选集合整体跳过跨币种现金排序项，继续比较其他项并显示原因。换算仅供排序/展示，不能改变原币预算或声称汇率已锁定。

`estimated_stop` 仅表示达到估算阈值后不再继续，不承诺最终账单绝不超过阈值。要求硬上限的任务不会匹配仅支持 estimated_stop 的收费配置；用户可在清楚看到语义后配置允许使用估算模式。

用户在审阅 Q6 确认默认现金策略为 `bounded_calls`，订阅配额允许保守估算。仅支持 estimated_stop 的现金 Profile 不因已登记而自动启用；若将来选用，须明确更改对应预算策略。这个默认值不等于任何实际服务已经通过硬上限资格测试。

未设置现金预算时现金配置不可派发；订阅中隐藏的“额外余额自动兜底”也属于现金路径，必须在网关/服务设置和平台策略两处明确处理。Designer 创作及模型规划使用独立有限预算，记录调用、自动修复、耗时和未知尾账；部署 readiness 的无副作用校验不需要业务输入，也不得偷偷调用付费模型来“测通”。

unknown 配额不会被视为可无限调用：必须有用户允许的保守模式、并发/Attempt 上限、错误回路和本地消费跟踪。配置要求“必须有新鲜官方报告”而来源无法提供时，明确阻塞或换合格来源。

## 6. 换源、升级与失败分类

| 原因码 | 行为 |
|---|---|
| `RATE_LIMIT_TRANSIENT` | 根据 Retry-After/服务语义退避、减并发；不直接当成订阅耗尽 |
| `QUOTA_EXHAUSTED` | 当前池窗口内停止无意义重试；在允许集合换源或等待可信重置 |
| `AUTH_INVALID` / `BILLING_MISMATCH` | 挂起 Profile，修复身份；不静默转 API 现金 |
| `QUALITY_FAILED` | 使用失败证据构造有界修复或升级请求；保持原风险下限 |
| `CONTEXT_TOO_LARGE` | 构造保留必要输入的新交接包或换足够上下文的配置；不能静默丢验收条件 |
| `EXECUTION_UNKNOWN` | 先核对/停止，不立刻再启动一个可能重复消费的执行 |
| `NO_QUALIFIED_PROFILE` | 显示资格缺口，保留其他独立任务可运行 |
| `BUDGET_EXHAUSTED` | 停止新消费，给出等待/改计划/增预算的具体选项 |

自动换源是高级 opt-in：只有批准版本显式给出允许集合、成本/能力边界和数据去向时才可使用。它只改变获准的 Profile，保留 task revision、目标、难度、风险、权限和验收条件；来源、计费路径或成本扩大必须重新批准。原执行先暂停并确认结束，或已证明无法继续写入/调用且剩余消费单独占账，才可派发替代 Attempt。单纯 fence 失效不足以证明这一点，也绝不热切换。

质量升级使用同一路由行的显式 stage：审批时分别冻结正常组和各个预授权升级组；仅 `QUALITY_FAILED` 且修复次数允许时激活下一个 stage，再与授权集合和全部硬条件取交集。示例中的 `quality_escalation_groups` 按顺序定义阶段，不会被普通 eligible_groups 覆盖，也不会自动修改任务复杂度/风险。升级组未获批准则不能执行；任务目标或接口变化另建 Task revision。

修复身份归于 `repair_chain_id` 和 Run 的累计质量轮次，不能归于临时 Task ID。协调器先收齐一个 `validation_cycle_id` 对应的必需检查结果，再建立失败批次；同一批次生成多个修复 Task 只增加一轮，下一次验证失败才进入下一轮。每个 repair Task 继承根任务/集成验证目标、父链、当前 stage 与累计次数；换源、重启、新 Task 或新计划不能自动清零既有 Run 消费/轮次。基础设施重试同样按根任务累计，另受 Run 总次数/时间/预算约束。

需要工作区的新尝试使用独立副本和交接包。旧尝试消耗留在账上；未经验证的部分产物只能作为标明来源的参考。Run 内基础设施重试、质量修复与重新规划分别计数，受 Run 总调用/时间/费用上限约束。Run 之前的 Designer 创作循环另有持久累计上限，新 proposal、会话恢复或回执重试不清零；达到界限保留配置并等待用户决定，不自动部署。

用户编辑规则后，模拟、校验、发布为新 revision。已有 Run 若需采用，提交显式 `reevaluate_policy` 命令；新允许集合还要与原授权取交集。扩张范围形成新确认，紧急收紧通过撤销/取消明确生效。

## 7. 配置契约

[JSON 示例](examples/rulebook.v1.json) 的旧三角色和双 writer 默认值保留历史/兼容字节，不是 r8 运行限制。新配置引用角色/能力/授予范围与显式用户资源政策；不继承旧人数默认值。编译器解析内部类型，禁止任意 Python/JavaScript 表达式。

编译器必须拒绝：重复 ID、未知字段、无效 role/class、歧义匹配、未定义 Profile 组、单位不匹配、未知池引用、重复共享池、循环 fallback、空能力集合、不可执行的硬预算承诺。模型组可以暂为空，但发布时会产生“可模拟、不可派发”的明确警告；不能自动补一个型号。

初始批准将动态来源组解析为固定 Profile/ModelBinding 集合；获权角色可在该集合内为新子任务绑定来源，不逐任务再批。新增集合外来源仍需扩权，实际配额动态观察。职责/配置字节的定义变更生成新包/预览；运行图中选择获准角色或来源产生有来源的图修订，不改原定义摘要。

资源对象的字段和写入接口见 [接口文档第 8 节](04-api-and-workbench.md#8-资源配置契约)。示例中空引用表示尚未绑定账户，只有结构可检查；不是可运行配置。

能力校准使用真实任务样本的成功率、修复次数、延迟和费用，按任务/领域分组。首版由用户维护能力组和版本，平台提供报告，不自动把历史成功当作提高权限或降低质量门槛的理由。
