# M1-02：Run 计划与人工 Commander 交接领域切片

对应 [M1-02 / #12](https://github.com/zhouy1017/Karajan/issues/12)，实现 PRD FR03、FR04、FR12 与 AC02/AC04 的离线领域部分。当前真实模型调用数为零，现金 API 调用数为零；本模块不启动 Commander、Worker 或 RunnerHost，不创建执行 activation。M0-07 及真实来源资格继续为 `not_run`，不能据此关闭整个 M1-02 的真实验收。

## 公开入口与信任关系

`RunPlanner(database, projects, *, admissions=None, clock=time.time)` 接受已有 `ProjectRegistry` 和可选的可信准入回执读取器。字段契约见 `backend/karajan/runs/models.py`，所有请求模型采用 strict、extra=forbid。`RunError.code` 是稳定拒绝原因。

| 入口 | 输入与结果 |
|---|---|
| `create(request, command_key, principal)` | 需求、用户指定参与者、授权上限和确切项目/configuration revision、digest；产生 Run |
| `list(principal, project_id=None)` | 只返回该 owner 的 Run，可按项目过滤 |
| `get(run_id, principal=None)` | 返回独立快照；指定 principal 时非 owner 和缺失 ID 均为 RUN_NOT_FOUND |
| `planning_intent(run_id, term, command_key, principal)` | 当前主 Commander 或顾问的只读规划意图，绑定冻结 Profile 与规划 budget_ref；不发送请求、不预留金额 |
| `attach_planning_receipt(run_id, intent_id, receipt_ref, command_key, principal)` | owner/controller 以引用登记权威回执；不接受浏览器提供的 receipt 内容 |
| `submit_plan(run_id, request, command_key, principal)` | 当前主 term 且拥有匹配 admitted 意图的 Commander 提交结构化提案；产生不可变 plan revision、摘要与影响清单 |
| `approve_plan(run_id, request, command_key, principal)` | owner 批准确切版本与摘要；保持 dispatch_enabled=false |
| `propose_handoff(run_id, request, command_key, principal)` | 当前主 Commander 或 owner 提供检查点、指定候选、预算影响及有效期；只创建 pending 提案 |
| `decide_handoff(run_id, request, command_key, principal)` | owner 对当前材料 approve/reject；approve 才切换到指定候选的新 term |
| `task_gate(run_id, task_id)` | 仅查询当前批准版本的任务授权；依赖/资源/运行时执行资格仍必需，不是启动许可 |
| `events(run_id, principal)` | owner 读取带序号、时间、命令摘要和接受/拒绝结果的审计记录 |

表中 `principal` 与 `command_key` 等为关键字参数，精确签名以代码为准。`get` 不传 principal 仅供可信协调器内部使用；HTTP 总是传已认证 owner。模型输出不能自称 owner/current Commander；身份必须由可信控制端从实际会话映射得到。CLI 同样是本机控制入口，不是供不可信模型直接调用的认证机制。

浏览器可以建立需求、读已有状态、批准已有计划以及决定已有交接方案；生产模型的计划提交、准入回执和交接提案应由可信协调器接入。没有合格模型时，不为展示而生成假 Commander 结果。

## Run 创建与冻结材料

创建字段包含：

- `project_id / project_revision / configuration_digest`。
- `requirement {goal, acceptance[]}`。
- `participants[] {principal, profile{id,revision}, purpose}`；恰好一个 lead，advice 只能顾问，candidate 尚未激活。参与者身份唯一，owner 不能同时成为模型身份。
- `authorization {profile_refs, read_paths, write_paths, budget_ref, checks, delivery, target_branch}`。

Profile 必须来自项目批准集合；主 Commander、候选和顾问分别核对冻结 Rulebook 对应组。授权中的预算引用必须匹配固定 Run 预算；来源/分支/检查均明确。独立审查是必需检查，不能通过删除 `independent_review` 绕过。路径按显式相对路径前缀表达，不接受绝对路径、父目录跳转、反斜杠/盘符、通配符、尾随点/空格或 Windows 保留设备名。写授权和 Worker 任务路径中的 `.git` 组件按大小写无关规则拒绝，包含宽目录授权下的嵌套元数据路径。这是计划范围检查，不是文件系统沙箱证明。

Run 保存完整项目配置、配置 revision/digest、项目 revision、仓库身份和基准、目标分支以及授权上限。ProjectRegistry 的两次只读快照必须在 revision 与内容摘要上相符。跨项目库和 Run 库没有原子事务承诺：创建捕获一个具体且一致的历史快照；之后项目修改不原地改变该 Run。批准再次绑定 Run 已冻结的配置摘要，不静默转到项目的新配置。全局撤销、当前 CapacityPolicy 和 activation 排序仍由后续真实执行准入负责。

## 规划意图与权威资源回执

本模块没有第二套配额或现金账本。`planning_intent` 仅持久化规划需求，权限固定为 read，包含 Profile revision、主 term 和冻结配置中的 `planning_budget_ref`。创建意图不等于消耗/保留了任何服务额度。

构造时的 `admissions(receipt_ref)` 必须是可信、只读、不可伪造来源的适配入口，返回规范化字段：

```text
receipt_ref, authority_revision, run_id, intent_id, term, principal,
profile{id,revision}, budget_ref,
state admitted|denied|unknown,
provenance fixture|imported_observation
```

平台核对 intent/Run/term/参与者/Profile/预算全部绑定；一个意图只接受一次回执登记。顾问回执不能变成主 Commander 的提交权；其他 intent 的回执不能重绑。denied、unknown 或缺失回执都不能提交有效计划。回执及审计事件被持久保存，以供查询规划接收 trace；接收记录数量不代表真实模型调用覆盖范围或已经保障有限预算。

当前没有生产 reader 适配器，缺失时 `ADMISSION_AUTHORITY_UNAVAILABLE`。后续需将 reader 接到已有资源权威的准确 receipt/Attempt 身份与生命周期，验证调用发生前的准入和撤销排序。测试层的 `ScriptedAdmissionReader` 只提供测试回复；包、CLI 与页面没有 fixture 额度授予入口。即便回执是 imported_observation，本模块也不把它提升成 live qualification。

## 计划提交与版本批准

提交请求含 `term / intent_id / expected_plan_revision / plan`。Plan 包含 summary、请求授权和 tasks；任务带稳定 ID、revision、角色、T0/ready、复杂度、风险、路径、依赖、验收条件及 required 标记。

程序拒绝循环图、重复任务、缺失依赖、范围扩大、未批准来源、预算变更或删除必需检查。角色和未知字段由严格 schema 拒绝。T0 任务可以保留在提案中，但批准计划也不会让它取得实现授权。

`plan_revision` 在同一 SQLite 事务中以期望版本比较后递增。已保存计划不能原地修改；相同 task ID/revision 不能重新定义。新提案列出 added/removed/changed/affected/reusable，沿依赖传播影响；授权变化会扩大影响范围。`attempt_reconciliation=not_run` 明示尚未接入真实 Attempt 取消、收尾与候选采用。

批准输入固定为 `term / plan_revision / plan_digest / authorization_digest / configuration_digest`。只有 owner 可以批准当前主 term 提交的最新提案；旧版本和任意摘要不符被拒绝。批准记录包括用户、时间和完整绑定。保存 executing 表示计划已进入可协调执行阶段，不表示本模块已启动进程。

旧的已批准计划在新提案待确认时继续作为 `task_gate` 的授权来源。新版本批准后切到新成员关系；实际执行端还必须处理影响清单、占用、旧 Attempt 与结果有效性。本模块不声称已完成这些调度动作。

## 每次交接都由用户决定

交接提案固定当前 term、最新计划 revision/digest、当前批准版本/授权摘要、配置摘要、指定候选、检查点内容及 artifact 引用/摘要、预算影响和不超过一天的决定有效期。资源影响只能引用原规划预算，不能据此增加金额或换到现金路径。artifact 引用摘要在本切片中是绑定材料；真实文件内容和完整交接包仍需后续 materializer/协调器核对。

新交接方案将旧 pending 方案标为 superseded。不答复不换人；reject 保留原 Commander；有效 approve 只切到方案中的候选并将 term 增加一次。已过期、已决定、旧 term、被替代或计划/批准材料发生变化的确认都被拒绝。并发确认只会产生一次有效 term 切换。

旧主 Commander 不能在新 term 提交；新主 Commander 也必须先取得自己的新 term 规划意图和匹配准入回执，不能继承旧人的调用记录。交接等待不改变已批准任务的授权查询，也不进行全 Run 暂停。它没有提前启动替任者的 outbox 或进程操作。

## 幂等、审计与真实状态边界

所有变更命令以 `(principal, command_key)` 保存请求摘要和成功/拒绝结果；同键同输入重放历史结果，不重复应用转换，同键不同输入拒绝。`BEGIN IMMEDIATE`、保存点和提交把 Run 修改、结果与审计一起持久化；领域拒绝回滚该次修改再记录失败。格式不合法及外部项目/准入读取阶段的拒绝尚不进入 Run 事件日志。

幂等重放返回原命令回执，不能当作当前可执行状态。例如旧批准成功后的同键重试返回旧回执，但不会覆盖后来批准的新计划；调用方需重新 get 当前状态。当前只有本地 SQLite 串行/重开和并发行为实测；没有宣称跨库 exactly-once、模型请求 exactly-once、生产恢复或全系统协调器锁已通过。

所有快照 `dispatch_enabled=false`、`live_qualification=not_run`。`task_gate.scope_approved` 只说明该任务范围已在当前计划批准，仍返回资源/依赖/运行时必需门。没有仓库写入、模型发送、现金调用、Git 交付或 RunnerHost activation。

## 验证与实际结果

Windows / Python 3.12，当前 49 项公共行为测试通过（审查修复后最终复跑 11.21 秒）：真实 SQLite 和临时本地 Git/ProjectRegistry；公共 CLI 另开 Python 进程创建需求并重启读取。脚本化 authority 仅替代外部准入读取，测试不访问私有表或 mock 模块内部。并发用例覆盖重复创建与 Commander 同时确认；任务图/权限/过期/摘要/回执错绑均有拒绝回归。带当前源码摘要的结果索引见 [verification.json](../../examples/runs/verification.json)；它记录本地检查结果，不是来源资格证书。

按行为逐轮红→绿：创建模块缺失；同键并发创建多个 Run；规划意图/回执入口缺失；计划提交/批准入口缺失；不合法计划被接纳；交接入口缺失；相同 task revision 被覆盖；创建时任意 Profile/预算/检查被接受；owner 列表/审计入口缺失；公共 CLI 缺失。后补的严格输入、到期拒绝与已有防护验证直接通过，没有冒称每个变体都曾失败。

独立审查补充两项 P2 后又实跑 7 个红→绿变体：Windows `.GIT`/尾随点别名及设备名、宽目录下嵌套 Git 元数据被接受，以及 surrogate Run ID 触发底层 UnicodeEncodeError。修复后各入口稳定拒绝且状态不变；Run ID 在命令进入和数据库读取前均校验，拒绝日志不会再次绑定非法字符串导致异常。

```text
.venv/Scripts/python.exe -m pytest tests/runs -q
.venv/Scripts/python.exe -m ruff check backend/karajan/runs tests/runs examples/runs
.venv/Scripts/python.exe -m mypy backend/karajan/runs
```

模板和 CLI 方法见 [examples/runs](../../examples/runs/README.md)。测试进程只操作新建临时仓库；公开 CLI 回归确认原文件仍是 untouched、计划/意图为空。Ruff 和 strict mypy 覆盖本切片 5 个源文件，无新增依赖。实现 commit/PR 与远端 gate 由 root 后续绑定；本切片没有执行 Git 提交、发布或任何真实模型调用。

完整 M1-02 仍须真实合格规划/顾问来源、权威资源准入接线、真实 Commander 输出、运行中独立任务推进和完整 HTTP/工作台组合验收。现金 API 真实验收本轮保持 not_run；离线 49 项通过不能替代这些门。
