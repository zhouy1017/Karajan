# M1-01：可信项目与固定配置登记

本文件记录 [M1-01 #11](https://github.com/zhouy1017/Karajan/issues/11) 的项目领域部分。HTTP 会话、Host/Origin/CSRF、页面和受认证读取由 `karajan.web` 集成；本文的 Python 接口仅供可信本地控制器调用。它不接收模型授权、不启动执行器、不预留资源，也不调用任何模型。

## 可运行接口与证据

公开入口是 `from karajan.projects import ProjectRegistry, ProjectError`。构造方式为 `ProjectRegistry(database: Path, allowed_roots: Sequence[Path])`。SQLite 文件与同目录的控制文件不能处于登记仓库内；可信服务部署时将控制数据与仓库设为兄弟目录。

```text
.venv/Scripts/python.exe -m pytest tests/projects -q
.venv/Scripts/python.exe examples/projects/probe_registration.py --directory .cache/projects-probe-new
```

探针目录必须是新目录，不能指向已有项目。它创建专用本地 Git 仓库及独立 SQLite 目录，直接使用公开服务完成登记、重试、配置预览/应用/导出和规则预览。输入是 [offline-configuration.json](../../examples/projects/offline-configuration.json)；保存的实际运行结果是 [registration.report.json](../../examples/projects/registration.report.json)。报告记录日期、OS、Python 版本、实际代码文件 SHA-256、配置输入 SHA-256、解析的 Git commit、仓库内所有文件前后 SHA-256 和十项条件。

该实际运行使用 Windows、Python 3.12 和真实本地 Git/SQLite，十项条件通过，包括原仓库文件完全未变、T0 阻塞、critical→T3、配置往返和假凭据拒存。订阅/API 模型请求数为零；现金 API 验收仍为 `not_run`。HTTP/浏览器证据由上层集成单独提供，不把本探针当作认证、进程隔离或真实 Profile 资格证据。

## 项目与命令协议

`create(request, *, command_key, principal)` 的请求：

```json
{
  "name": "Example project",
  "repository_path": "<已有且位于允许根目录下的本地仓库>",
  "base_ref": "main",
  "target_branch": "main",
  "allowed_target_branches": ["main"]
}
```

登记严格检查字段类型、允许根、真实仓库顶层目录、可解析 commit 以及合法且获准的目标分支。Git 使用固定参数向量、有限等待和受控环境，只调用只读的引用检查，不执行 fetch、checkout、项目脚本、模型或交付操作。`identity_sha256` 是规范化本地仓库路径身份；`base_sha` 是当时解析的不可变 commit。这不是全局 remote 身份或持续文件完整性证明，真正执行/交付须再次验证目标。

`update(project_id, request, *, expected_revision, command_key, principal)` 接收相同的 name/base/target/allowed-target 字段，但不能改变 repository_path。更换仓库应新建项目。`get(id)` 返回当前快照，`list()` 返回登记顺序的快照列表。

项目返回字段：`schema_version/id/revision/name/repository/target_branch/allowed_target_branches/configuration/live_qualified`。repository 包含 root、identity_sha256、base_ref、base_sha。configuration 包含自己的 revision、status、digest、preview_id、dispatch_eligible。初始状态为 `unconfigured`，所有执行资格标识始终 false。

create、update、preview、apply 都要求非空、有限长的 command_key 和可信 principal。幂等键在同一 principal 下绑定操作、目标、规范化载荷及预期版本；同键异载荷报 `IDEMPOTENCY_CONFLICT`。已成功的同键重试先返回原保存结果，再考虑当前 revision，因此成功后网络应答丢失不会变成冲突。新 update/apply 必须匹配当前项目 revision。所有账本记录与相应业务写入同一个 SQLite 事务提交。

连接使用有限等待、外键及 synchronous=FULL，退出事务后显式关闭连接。Git 查询在写事务之外进行，之后在事务中重查命令与版本；同键并发只产生一条项目业务结果。

## 配置预览、应用和读取

`preview_configuration(project_id, configuration, *, command_key, principal)` 是持久命令。它保存输入摘要、具体校验结果、项目 revision、固定规则校验器版本和规则摘要，不改变项目 revision，不预留额度。返回 `preview_id/project_id/project_revision/configuration_digest/status/issues/can_apply/dispatch_eligible/qualification_scope/live_qualified/validation`。

配置封套是 `karajan.project-config.v1`，包含 rulebook、resources、approved_profile_refs。字段结构在 `karajan.projects.models` 严格定义；示例给出完整可导入输入。固定 Rulebook 来自架构示例的一个打包副本，其完整固定规则、硬条件和排序约定被核对。M1 只允许绑定 Profile group、预算引用和有限重试/并行数量，不提供任意规则编程。

resources 包含 accounts、channels、profiles、quota_pools、capacity_policies、budgets。Profile 登记包装保留 id/revision、model_family、max_class、隔离要求、声明启用状态、池引用和能力证据；其 `profile` 对象直接复用 M0-01 的冻结 Profile 契约，也可暂为 null。未知型号/认证/资格可保存为草稿，不能因声明 enabled 或某一 fixture passed 而真实启用。

校验包括：

- 账户/通道/池/预算身份唯一，Profile binding 与账户、通道、收费路径及 revision 对应；池属于同一账户，服务池和平台 allowance 保留不同 kind，不把平台预算当作服务额度。
- 必需 planning/run 预算引用、原币大写三字母代码、非负且最多六位小数的金额字符串、明确正数次数/时长限制。预算数据只登记，实际原子准入和累计消费仍由资源控制器执行。
- 保守模式显式启用且包含正数本地并发、Attempt 时长、观察最大年龄、cooldown。本次有限目录不接受这些缺项作为无限容量，也不建立服务商已锁定配额的事实。
- 固定组只能引用已登记且已批准的 Profile revision。required class、目的地、认证引用、工具隔离要求、模型家族及能力证据都须完整。
- 能力记录为 passed 仍须匹配规范化 Profile digest、runtime version、唯一能力记录及 evidence_ref；not_run、unsupported、失败、缺失/旧证据均不能满足要求。导入的证据只是离线检查材料，本模块不验证真实外部证据仓库。

`draft` 表示仍有具体 issues；`offline_valid` 只表示这些有限输入检查通过。二者都没有实际派发资格。目录尚未实现完整 price/FX/多窗口观察协议、实时能力证据更新、账户保护量调度或真实现金准入；这些要与后续控制器/资格接口绑定，不能直接依据本模块的候选列表消费。

`apply_configuration(project_id, preview_id, *, expected_revision, command_key, principal)` 只接受已有 preview_id，没有可替换 payload 参数。同事务核对项目、preview 所属项目、原 revision、校验器版本/固定规则摘要及可保存状态，再更新项目和配置 revision。旧/跨项目预览、丢弃内容、版本冲突均不更新项目。保留其他 Run 既有授权的责任在上层协调器，应用项目配置不迁移已批 Run。

合法但不完整的草稿 `can_apply=true`，可保存以后编辑。结构非法或含凭据正文的输入 `can_apply=false`，不保存其正文；apply 返回 `CONFIGURATION_NOT_STORABLE`。`get_configuration(project_id)` 在一致事务内返回当前已应用内容及 project_revision/configuration_revision，未配置时 configuration=null；未应用和被拒绝的预览不污染当前导出。

## 规则预览的边界

`evaluate_task(project_id, task)` 是无副作用读取。task 包含 role、readiness（T0/ready）、complexity（T1/T2/T3）、由可信控制器给出的 risk（standard/critical）、本任务批准 Profile 集合，以及可选 purpose、必需能力、作者 Profile/家族信息。

它计算 `max(complexity, trusted risk floor)`，其中 standard→T1、critical→T3；T0 Worker 返回 TASK_NOT_READY。它只匹配固定规则，在本任务批准集合内过滤能力和 T3 家族独立性，不替换来源、不排序费用、不产生 Attempt 或预留。相同持久配置和输入返回相同结果。

Reviewer 返回所需 fresh_context/non_author_attempt 等独立性要求，真正的上下文与 Attempt 证明仍由执行/审查 gate 提供。普通审查可在新的非作者 Attempt 复用同一 Profile；T3 要求不同模型家族。risk 和作者范围必须由可信计划/候选逻辑提供，本模块不把模型自报的低风险当作项目路径风险判定。所有结果保留 `dispatch_eligible=false`。

## 秘密与错误边界

配置只接受 secret_ref/auth_ref 等引用，不读取 secret store、订阅登录文件或真实 token。递归检查配置对象中的 api_key/apikey/access_token/refresh_token/authorization/password/client_secret/secret/token 字段，包括 native_settings；env/environment/headers 载荷不在此配置入口的支持范围。检查还覆盖大小写及连字符归一化后的 *_api_key、*_auth_token、*_access_token、*_refresh_token、*_client_secret 环境键，例如 OPENAI_API_KEY 和 X-Api-Key。命中时只保存不可逆输入摘要和固定 issue，原文列为 NULL，输出不包含值。其他严格 schema 错误同样不保存原文。

这检测明确的凭据字段，不声称能识别任意编码、别名或伪装在普通文本中的秘密。真实凭据登记仍须走独立受限入口；不能把本模块当作通用 DLP。公开导出及实际 SQLite 文件上的假 canary 回归均已执行。

`ProjectError.code` 提供固定原因，`current_revision` 只在相关版本错误时提供。所有公开对象 ID 及命令主体/键先做严格字符串、有限长度、可打印字符及 UTF-8 校验，未配对 surrogate 在进入 SQLite 前返回固定错误。错误不回显 Git stderr、Pydantic input 或凭据值。非 JSON 数值等输入返回 INPUT_NOT_JSON；HTTP 层负责自己的认证、体积限制和错误映射。

## Test-first 记录

测试只操作公开项目服务、真实临时 Git、公开快照/导出和作为安全输出边界的 SQLite 文件 canary 扫描，不查询私有表来验证业务。

| 周期 | 行为 | 实际 red |
|---|---|---|
| 1–5 | 登记持久化、路径/base/分支、并发幂等、严格字段、版本更新 | 模块缺失；无效项目创建；并发生成多 ID；未知字段通过；update 缺失 |
| 6–10 | 草稿预览、固定 preview 应用、预算引用/金额、有限 unknown | preview/apply 缺失；缺预算/坏原币/缺 unknown 限制被标为 offline_valid |
| 11–15 | preview 幂等、凭据、schema、固定规则、共享关系 | 不接受命令键；凭据/未知结构/放宽规则/错误共享关系通过 |
| 16–18 | 证据绑定、权限、控制存储保护 | 旧/未运行能力和未批准 Profile 通过；未知权限通过；含控制数据仓库被登记 |
| 19–22 | T0/risk、任务额外要求、额外能力旧证据、错误预算引用 | evaluate 缺失；额外能力/独立性未过滤；旧证据可用；错误引用触发 TypeError |
| 23–27 | 命令身份、预览版本证据、非 JSON、不可保存内容、当前导出 | 空/布尔命令键通过；版本字段缺失；NaN 原异常；缺 can_apply；导出入口缺失 |
| 28 | 普通审查复用 Profile 与独立 Attempt 分开 | 普通审查被错误按作者 Profile 排除 |
| 29–30 | 实际凭据环境载荷与 Unicode ID | env.OPENAI_API_KEY、直接环境键、headers 可保存；五类公开 ID 入口触发 SQLite UnicodeEncodeError |

额外要求周期中的“批准集合为空”在已有过滤下立即通过，未冒称为 red。整体验证曾暴露路径保护的错误原因优先级，已保持越界检查先于控制目录检查并重跑。最终定向测试 **73 passed**；Ruff/格式和严格 mypy 覆盖本模块，实际完整结果与提交范围由集成任务再核对。没有将这些离线结果计入真实执行、收费、OS 隔离或完整 M1 出口资格。
