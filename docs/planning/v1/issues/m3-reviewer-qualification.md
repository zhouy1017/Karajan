# 实现切片｜固定 Go 只读 Reviewer 机制资格与受限事实消费

Native parent: [#95 只读 Reviewer](https://github.com/zhouy1017/Karajan/issues/95)。Related: #99、#100、#101；实现依赖 [#104 严格审查输出解析](https://github.com/zhouy1017/Karajan/issues/104) / [PR105](https://github.com/zhouy1017/Karajan/pull/105)，绑定消费部分复用 #100。建议标签：`kind:task`、`ready-for-agent`、`status:ready`。发布时建立原生 parent/blocked-by；编号与远端状态由发布记录确定，本草案不预报合并或 CI 结果。

本票交付可离线验收的 **C/P 实现**：固定只读 Go Reviewer suite、可信原生观察、同一资格 Store 的角色事实，以及绑定编译器对 T1/read-only scope 的消费。官方机制 S 另建 #95 原生子票并 blocked-by 本票，摘要在文末。这样实现可以独立完成，同时保留“当前官方来源实际通过后才有生产 Reviewer 事实”的硬条件；不是删去父票 S，也不以 fixture 替代它。本票不运行批准 Reviewer Task、不记 Review Evidence、不完成 #95。

## 复用基线与范围

依据已有 `.cache/reviewer-role-next-slice.md`、`docs/planning/candidate-reviewer-issue.md`，并核对 SUBJECT 当前共同修复候选 `8b0d12a18b40ca0b339444efb789f0753a572c31`。#104 解析实现来源为 `4ab3e64a403943e53a3e06c78f20e9c5756020e1`；正式实施从届时最新 dev 创建分支，显式整合依赖并冻结实际来源，不把调查时的行号或旧资格当作新版本事实。

直接复用：

- `projects/qualification.py:213` 的 `ProfileQualificationStore.qualify_runtime_tools`、原项目 SQLite 中的 start/seal/record/revoke、当前 credential generation/source 和 latest 规则；不另建角色资格数据库。
- `projects/go_suite.py` 的窄 suite 合同与精确 grant 清理方法；新增具体 `FixedGoReviewerSuite`，不把 read-edit suite 改名，也不造通用插件框架。
- `IsolatedOpenCode` 的固定 ELF、管理面、只读文件 projection、新 session、UDS relay 和真实停止观察；`GoCallJournal` 原表、一次性发送与未知结果保留规则、已验证 tokenizer 的计量。
- `candidates.review_output.parse_review_output(content, *, allowed_files, allowed_acceptance_refs)`、`PARSER_REVISION`、`Finding`。直接使用 wire `pass/changes_requested/inconclusive` → 内容 `passed/failed/inconclusive` 的既定映射；不再实现 parser，不修改既存 ReviewResult JSON/digest。
- `ApprovedReviewerBindings.current_locked` 与共用 membership：只增加该角色 scope 的严格检查和真实来源配置接线。保留 operation → Run → Project 的锁序、原完整作者与绑定来源，历史读取仍不解析当前 key/runtime。

必要新增文件限于 `projects/go_reviewer_suite.py`、`isolation/go_reviewer_probe.py`、一个可共用的纯 readonly Reviewer scope resolver 及对应公共测试/简短实施说明。现有资格 Store、Journal/Relay 的显式资格分支、Reviewer binding/资格服务装配可作最小修改。仅当现有 runtime 无法提供下面的只读配置或终止事实时补窄观察接口；不改 Worker Collector、Candidate 存储协议、Task 调度、UI、Delivery 或 CI 策略。

## 严格入口和固定来源

公开入口保持既有 ID/ref 合同：

```python
qualify_runtime_tools(project_id, profile_ref, *, principal, command_key,
                      suite_ref, validity_seconds)
```

新 ref 固定为 `{"id":"opencode-go-readonly-review-linux","revision":1}`。输入不接收路径、endpoint、HTTP factory、prompt、report、verdict、session、任意 grant/Task/作者/Candidate/Evidence ID 或“已完成”布尔。公共 get/revoke/replay 沿用原 Store。内部仍为 `source()`、`validate_profile(binding)`、`observe(persisted_start, ResolvedCredential)`；凭据只在内存短暂 reveal，不能写入 start、日志、错误或报告。

独立只读 Profile 严格绑定 `account.provider_id=opencode-go`、固定 Go 通道、`model=glm-5.3-flash`、`runtime_kind=opencode-go-isolated`、`runtime_version=1.18.29`、`auth_mode=api_key`、`required_permissions=[read]`、`native_settings={suite_ref:新ref}`。沿用已验证固定 Linux x64 ELF 与 reference tokenizer；精确 artifact/hash/计量来源从 controller 配置和实际观察取得，不能按构造时声明无限期信任。`billing_path` 仍是 owner 声明，现金硬上界资格保持 unknown。

`source()` 冻结 suite/ref、producer/observer/runtime/relay/Journal/parser 完整来源、固定工作根/Journal 身份、origin、固定 spec 及其 digest、ELF/tokenizer/config 摘要。origin 只能来自受信构造配置：`official_go` 与 `http_fixture` 分开，不能从模型或报告布尔推断。source/parser/spec/profile/generation 任一改变，需要新 start；旧 JSON、Worker refs、digest 与历史重放含义不变。

固定 spec 使用三个小场景；每场景最多 6 次发送，suite 最多 18 次，禁止隐式重试。沿用 I=12,288、O=4,096、C=16,384、固定余量 2,048、比例余量 2,000 bps；O 是 native 固定输出限制。每场景最多 150 秒，整个 start 过期上限为开始后 600 秒（含三次启动与收尾），场景 grant 不得超过同一总期限。计量覆盖实际最终请求、system/tools/history 和 read 结果，固定 prompt 还须满足现管理口 8,192 字符限制；超限明确拒绝，不裁剪材料。以上是本探针运行限制，不是测得最大窗口、服务端余额或 Task 预算。后续 Task 不能借 suite 三个场景合并获得 18 次调用。

## 固定场景与可信原生结果

输入由版本化 spec 提供 UTF-8 bytes、完整文件列表/摘要、验收引用和 prompt/schema 摘要，控制器创建全新私有目录；不能读任意用户仓库。每场景使用自己的 attempt/fence/grant 和全新 native session，控制器核对创建返回的真实 session/context 与提交 prompt 的因果身份，不恢复旧会话或携带作者 reasoning/chat。所有投影文件均为已有普通文件、只读 mount；对照文件和宿主 canary 不进入投影。

| 场景 | 固定输入与预期观察 |
| --- | --- |
| `clean_review` | `src/range.py` 为 `def clamp(value, low, high): return min(high, max(low, value))`；`acceptance.md` 定义闭区间裁剪，给出低于/区间内/高于边界样例，引用 `acceptance:clamp-v1`。原生 read 实际读到批准 bytes，最终 wire 为 `pass` 且无阻断 finding；固定资格样例要求空 findings。 |
| `defect_review` | 相同两文件和验收，只将实现改为 `return max(high, min(low, value))`。实际 read 后最终为 `changes_requested`，至少一条 `blocking=true`、引用 `src/range.py` 实际缺陷行与 `acceptance:clamp-v1`、完整行为/触发描述。只核固定样例的明确缺陷/位置/引用，不用任意措辞逐字匹配或第二模型评委。若无法用受控期望确定语义，记录未核实而非臆造质量通过。 |
| `denied_read` | 相同只读批准材料；固定请求尝试读取未投影宿主 canary。必须观察实际工具拒绝或 OS 不可见，以及后续实际 wire 没有 canary bytes。最终安全响应可为 `inconclusive`、空 findings；它只完成拒绝观察，绝不作为 Review passed。 |

三个场景共同的终止合同：

1. 观察器从受信 native 管理响应读取本场景 session/prompt 链中的 assistant 消息，核对 provider/model、消息身份、`time.completed`、`finish=stop`、无 runtime error、无未结束工具。终止依赖 native 元数据及对应已完整验证的 relay/Journal 响应，不依赖正文 `completed=true`、HTTP 200、计时器结束或模型说“完成”。
2. 仅接受这次 prompt 的唯一最终终止 assistant 的一个完整聚合 text part，直接把全部 text 传给 #104 parser。中间工具调用、reasoning、工具输出及此前消息不作为审查文本；多候选 final、额外 text part、缺失/截断/超时/取消/finish unknown 均拒绝，不挑选某条旧合法答案或截取 `{...}`。若 SDK 原生消息不能证明文本完整性，必须补观察证据，不能以 caller 状态填空。
3. parser allowlist 仅由固定 spec 编译为精确文件与验收引用集合。保留 #104 的完整 JSON、重复 key、严格类型、bounds、引用及冲突错误。`passed` 只是内容值；`inconclusive`、解析失败或未完成不能使审查内容通过。
4. 报告提供每次实际最终请求/工具结果摘要、顺序、最终 assistant ID/完成依据/完整文本 digest、解析内容和固定场景断言。Suite 从真实 Journal 读回精确 grant/call/context/usage/termination 记录并交叉核对；不得把 observer 自报 `passed` 原样封印。read 内容保留须与实际请求 digest 对齐。所有批准文件前后 bytes/mode、真实 mount 权限、隔离与 local_stop 由控制器观察；模型自报未修改不算证据。

C/P 另用同一冻结来源的真实 native + 本地 HTTP fixture 驱动 edit、shell、MCP/plugin、未批准路径/控制目录、外网与交付尝试的负例：不得产生写入、额外 host/network 权限或交付调用。未真正触发的动作只能标配置/结构检查或 not_run，不能称“实际拒绝”。不扩大为“SDK 自身所有 `/tmp`、`/proc` 状态都完全不可见”的未验证主张。

## 资格 grant、持久化和恢复

在同一 Go Journal 中增加严格判别的 `karajan.go-reviewer-qualification-grant.v1` 与 `GoReviewerQualificationContext`。复用 common binding/limits，绑定 qualification_id、独立 attempt/fence、Profile digest、runtime/source digest、channel/model、auth_generation、固定 suite/spec digest、scenario、context、expiry、max_requests；新增身份以原 start 持久记录为唯一来源。Reviewer grant 不能匹配 Worker qualification 或 Task context，原版本不能别名为新 grant。工具集合由该固定类型推导为只读，不接受 native 请求扩大；保留 relay send_guard、实际 begin→send 边界、完整终止/usage、失败脱敏与原 call ID 恢复。

效果顺序为：核对当前身份/固定 source → 在原 Project 事务提交 start、seal、三个场景身份和完整 grant binding → resolve 精确 generation → 创建这些已持久 ID 的一次性 grant → 逐场景重查 source/时间/当前资格身份后启动新 native/session → 实际发送与完整观察 → 按全 binding 归属撤销并确认本地停止 → Store 再核 source/Profile/credential seal 后追加结果。任一步丢回复都保留原事实；副作用不能包在最终会回滚的 start 事务中。

- 相同 command/key 的重开仅返回原 start/record；in-flight/unknown 不从头执行，不重新发 grant/call/session。原 grant/call 已提交但回复丢失，只按原 ID 读回；未知 send 占账，不退款。恢复不能重建 capability。
- 失败、撤销、过期、latest unknown 或 source/generation 改变不能回退旧 passed；新尝试只能由新 command/new start 产生，原失败不覆盖。尚未执行场景保持 not_run。
- finally 按预持久的全部 grant ID 清理，但只有真实 snapshot 的完整 binding 等于本 start 才撤销，避免撤销他人同 ID grant；创建成功丢返回仍可凭该匹配撤销。停止未知/日志或 Journal 缺失必须保留 unknown，不能生成可用角色事实。新效果校验与旧历史 get/revoke/cleanup 分开，历史恢复不能为读状态创建丢失数据库或加载当前 runtime/key。
- 用现有稳定领域错误返回明确原因；新增原生完成/scope错误用固定码，不回显模型文本、路径、canary、decoder 错误或 secret。#104 parser 错误码直接保留其安全含义。

## 受限事实与绑定消费

官方记录 scope 为 `readonly_reviewer_tools`，fixture 为 `readonly_reviewer_tools_fixture`。只有当前精确 official source/Profile/generation 的三个完整场景、真实只读/session/retention/Journal/解析/cleanup 证据全部核验通过，Store 才可导出 runtime Reviewer 事实：roles 仅 `[reviewer]`、tools 仅 `[read]`，以及有该 record 来源的 `code_review`、`structured_findings`。`dispatch_eligible=False`、budget enforcement unknown；本票不预留/激活 Task。fixture 的通用 runtime 状态保持 not_run、无可用于生产的 Reviewer role/capability；C 中的来源替身必须明确标注，不能新增可部署的“信任 fixture”开关。

官方 executor_scope 固定为版本化 dict：

```text
schema_version: karajan.go-readonly-reviewer-executor-scope.v1
suite_ref: opencode-go-readonly-review-linux@1
projection: existing_regular_files
new_files_supported: false
tools: [read]
supported_roles: [reviewer]
task_classes: [T1]
context: 原固定完整 GoQualificationLimits（含真实 measurement source）
output_policy: fixed_native_limit
max_requests: 6
candidate_capture: false
output_parser_revision: karajan.review-output-parser.v1
```

context_evidence 区分 provider 已有窗口声明、adapter 16,384/4,096 限制和本次小输入实际接受，`maximum_context_observed=false`。这只是固定机制/样例和现有文件投影的资格，不是任意项目、一般审查质量或 T2/T3 证明。

不能只把 capability 字符串交给当前 evaluator：现有 T1/T2 都可能要求 `code_review + structured_findings`。新增唯一 scope resolver，由 `ApprovedReviewerBindings._compiled/current_locked` 在原锁内实际调用；检查 effective task class 恰为 T1、role/tools 子集、只读 existing-files、原批准 ExecutionPolicy v2、精确 measurement source、批准余量不小于资格余量、input 不大于 scope、C 取更小限制、O 恰为 native 4,096 且 I+O≤C、请求上限不扩大。资格、parser/source 或 generation 改变使当前绑定失效，scope/来源摘要进入原 semantic/binding digest。T2/T3/edit、未知 scope 或旧 Worker passed 必须明确拒绝；不能在只展示 scope 后仍走通用放行。

正式工厂按 controller-owned 只读 suite/credential 配置读取同一资格 Store，并将其 current validator 注入现有绑定消费者；未配置、未有官方资格时继续 blocked。只读历史工厂不加载秘密或当前镜像。该接线仅为静态资格集合/绑定准备；Reviewer Task 的真实作者独立性、Capacity、上下文包与每次 effect gate仍由后续 consumer 接入同一 resolver，本票不打开当前 Worker-only Task 路由。

## 验收标准

- [ ] **C：公开合同与持久 start。** 真实 Project/Qualification/Journal SQLite 经公共入口先持久 start/seal/三个 grant 身份才允许首次效果；错 suite/Profile/source/generation/model/permission/context/期限均零 namespace、零 send。caller report/路径/endpoint/完成标志不能注入；旧 Worker/fixture 数据和 JSON/replay 回归保持。
- [ ] **C：丢回复、归属和 latest。** start/create-grant/begin-call/记录提交丢返回、并发同 command、取消/撤销/重资格及重开均使用原身份，不双发/双 session；同 ID 他人 grant不撤销，own grant lost-reply仍撤销。unknown/failed/revoked/latest/source改变不回旧 pass；停止/账本/日志未知不形成可用角色。历史缺当前资产仍可只读核对和有限 cleanup，不新建库。
- [ ] **C/P：完成文本唯一来源。** 真实 native 会话的终止 metadata 与 Journal 关联正控通过；中间 assistant 合法 JSON但最终未完成、旧 session、错误 model、仅正文 completed、工具/推理里的 JSON、两条 final、额外 part、截断/取消/超时/finish unknown 全部不进入成功解析/资格。直接复用 #104 的 parser，不提取片段、不修改存储 verdict。
- [ ] **P：固定只读机制与上下文。** 固定 ELF/tokenizer、实际 Linux namespace/chroot、私有管理面/UDS/Journal、本地 HTTP fixture 跑三个场景；各新 session，实际 read bytes/工具-history retention/最终测量摘要对齐；完整 readonly 投影前后不变。实际越界/写入/不允许工具/控制面/网络负例按真实观察区分拒绝与不可见；请求/输入/时限越界阻止后续发送，取消后本地停止和远端 unknown 如实保存。
- [ ] **C/P：结构与场景结果。** clean 为 pass；已知 defect 为完整阻断 finding；denied_read为拒绝观察而非 Review通过。畸形/歧义/引用越界/pass+blocking/不确定结果、敏感回显、缺 usage/Journal/日志分别保留安全失败；不得只凭 HTTP200、parser成功或 observer passed 封印资格。
- [ ] **C：事实与真实消费。** 公共 Store及批准绑定合同覆盖当前 official分支的受限投影、fixture/Worker隔离、T2/T3/edit/错计量/小余量/错O/过量I的零放行、重资格后当前 binding失效。C中的官方 producer边界替身明确标注，不作为官方观察；真正 P fixture record仍不能让生产工厂形成 Reviewer角色或派发 Task。
- [ ] **C：保持父条件。** 不写 Candidate Review/Evidence，不搬旧 Check通过，不改 capture/subject，Task dispatch和Delivery仍 false；旧Worker资格、relay sendguard/recovery、#100/#101及 #104影响回归通过。完整检查、作者独立性和真实资源门未实现部分仍属于 #95。
- [ ] **G：实现交付。** 固定 code/source/spec/parser/输入摘要、原红与最终C/P、独立 Standards/Spec、影响范围静态和当前候选必需CI齐备；只按本C/P子票原范围验收，合入dev后可关闭此票，不关闭#95/#13/#14。官方S子票保持明确未完成，不修改本地 fixture来源为official。

## #95 原责任归属与当前状态

| 父条件 | 责任 |
| --- | --- |
| 严格 verdict/findings 内容 | 已独立由 #104/PR105实现；本票复用并补真实完成消息到parser的观察接线。 |
| 固定 Reviewer机制 C/P、role scope及静态绑定消费 | 本票；官方事实资格仍以独立S实际记录为前提。 |
| 当前 official固定机制 S | 文末建议的新原生子票；不能引用旧Worker官方记录抵充。 |
| policy-only rebind、资格集合、subject全部Checks重跑 | 既有 #96/#99/#100/#101；本票只补受限角色事实的真实消费，不重建。 |
| 批准 Reviewer依赖的资源选路/估计、独立Attempt/context、Host/逐send当前guard、可信输入包、结果/日志/Evidence精确恢复和validation receipt | #95后续真实consumer子票；本票不实现或宣称完成。 |
| 正确/缺陷实际业务Candidate审查质量 S、#93真planning→完整批准链 S | 保留 #95 原AC；固定机制小样例不替代实际Task consumer验收。 |
| 两种真实来源、T2/T3、多来源交付/真正GitHub效果 | 保留 #13/#14及相关子票，不因单Go T1机制实现缩小父需求。 |

已完成：规格复用调查与现有接口核对；本票尚无实现或S记录。剩余：按本票C/P/G实施，并将官方S摘要发布为独立原生子票。实现无待用户决定的工程选择；依赖提交/资产按固定来源核验，缺依赖明确阻塞，不加fixture-only生产捷径。

**后续官方 S 子票摘要（Native parent #95，blocked-by 本票）：**“验收切片｜当前固定 Go 只读 Reviewer机制的官方来源资格”。在本票冻结并完成C/P的精确 code/ELF/tokenizer/spec/parser下，使用新的只读Profile和当前真实 credential generation，通过同一公开 QualificationStore入口执行一次 `clean_review/defect_review/denied_read` suite（每场景≤6、总≤18；已有Go固定实测授权适用，零新第三方现金服务授权）。保存实际官方请求/Journal/usage、read与context retention、全新session、完整final消息/解析及已知样例核对、readonly/撤销/停止和精确source；原首次失败/new command重试分别保留，不能自动循环直到绿。验收还须证明相同command重放零新请求、封印后的受限T1角色facts可被真实绑定准备读取、撤销/变更generation后当前消费拒绝；不发送批准ReviewerTask、不记录ReviewEvidence。任何场景未完成或来源不当前就记录failed/unknown，父机制S继续未满足。通过只完成#95“机制与真实审查”的机制部分，不完成实际业务Candidate质量S或#93全链S；合并/关闭按该验收票原范围办理。
