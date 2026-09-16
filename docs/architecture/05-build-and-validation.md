# 实施、资格测试与运行维护

2026-09-14 r8 实施与验收设计。本文定义退出条件，不汇总当前通过状态；代码、离线 C/U/P 与真实 S/G 按原 Issue/证据登记。角色实际拆分/调度与无内置人数限制另见 [11 / RS-AC](11-role-directed-scheduling.md)，设计不等于通过。

完整开发接线与已有控制面复用见 [13](13-development-integration-contract.md)，Agent 领取及全量范围映射见 [就绪清单](../planning/r9-development-readiness-20260916/README.md)。r9 的 FR28/29、UX-AC25–27 和 [UA-AC](12-conversation-usage-accounting.md) 独立验收；全功能开发不因某一来源尚缺配置而整体暂停，原完整 v1 出口保留。

新范围由 [网关 GW-AC](08-provider-gateway.md#5-接入验收与未覆盖范围)、[Workflow WF-AC](09-configurable-workflows.md#10-可观察验收) 和 [对话设计与部署 WD-AC](10-conversational-workflow-deployment.md) 承接。主闭环是文字→Agent 实际配置文件→同源图表迭代→同版确认→pending 物化/加载读回→CAS active→可选的获准 Run。静态图、手工导入、登记模板或按钮回 200 都不足以证明完成。下方 M0–M4/A01–A26 保留历史范围，不据此扩大旧票 AC 或把代码模板硬编码为全部 Workflow。

## 1. 已确认技术组合

| 部分 | v1 基线 | 理由与约束 |
|---|---|---|
| 后端 | Python、FastAPI、显式类型化领域对象和配置验证 | 与 Python 底座复用方向衔接；HTTP 等待 I/O，Agent/测试独立运行 |
| Web | React + TypeScript，按 HTTP schema 生成客户端类型 | 展示持久状态，SSE 增量与快照恢复；不另建业务状态机 |
| 存储 | SQLite、本地 WAL、外键、短事务、单写入协调器 | 单机可维护；配额与 outbox 同库原子提交 |
| 产物 | 内容寻址目录＋数据库 manifest | 大日志/diff 不挤入事务，完整性可核对 |
| 执行 | 受控 Agent Runtime；Codex app-server、Claude CLI、OpenCode runner 为现有兼容/复用基线 | 复用 Agent 工具循环；默认模型通道走外置网关，特定原生能力须显式选择和验收 |
| 模型接入 | 外置 CLIProxyAPI＋Karajan 统一调用授权/账本 | 网关持有上游凭据与协议；Karajan 固定模型绑定、逐调用准入及消费记录 |
| Workflow | Designer、真实配置/图表、部署器及角色调度命令 | 冻结定义/初始授权/Attempt；获权角色提交动态 TaskGraphRevision，引擎校验/持久化/排队，能力独立验收 |
| 交付 | report/patch/pr 的版本化完成门；PR 使用独立受限进程、Git CLI＋托管平台接口 | 按产物验证；PR 必须同候选 checks/独立 Review，控制远端副作用且不自动合并 |
| 隔离 | 官方工具沙箱资格验收；API runner 优先独立容器/受限环境 | Windows 上优先探测 WSL2 的 Claude/API 路径，不假定已可用 |

FastAPI 异步接口用于等待 I/O，CPU 工作和阻塞工具执行仍应隔离；React 工作台从后端读取业务事实。[FastAPI 并发](https://fastapi.tiangolo.com/async/)、[React 状态与界面](https://react.dev/learn/thinking-in-react)

SQLite WAL 同时只有一个 writer，需要本地文件系统。采用严格持久化设置并验收崩溃恢复；固定依赖时检查官方已知 WAL 问题及修复版本。[SQLite WAL](https://www.sqlite.org/wal.html)

首版不引入 Redis、Kafka、Kubernetes 或分布式锁。出现多机需求或明确写瓶颈后再迁移数据库、拆 RunnerHost；领域 ID 和协议不依赖本机 PID。

## 2. 模块职责与拟定布局

```text
backend/karajan/
  planning/          # 需求、实例计划、控制提交身份与交接
  policy/            # 授权、Rulebook 编译与路由求解
  capacity/          # 配额观察、父子预留、消费核对
  coordination/      # 唯一状态写入、grant/决定/图CAS、机械准入与outbox/inbox
  execution/         # RunnerHost、执行适配器、工具限制
  inference/         # 网关 transport、调用授权与账本（provider 转换在外部）
  workflows/         # Designer 创作、配置包/图表编译、部署意图/加载与版本
  artifacts/         # 报告/补丁/代码候选、物化、集成与证据
  delivery/          # 按产物目标的交付门、PR 子类型及远端核对
  persistence/       # schema、migration、事务与内容索引
  http/              # 命令、快照、SSE、本地会话
frontend/src/
  projects/ designer/ workflows/ plans/ runs/ resources/ rulebook/ delivery/
contracts/           # 版本化 schema 和接口样例
tests/
  contract/ routing/ recovery/ acceptance/
```

这是职责示意，不是源码目录清单；已有模块按公开契约复用，不要求为文档路径重建或改名。新增范围按独立可验收切片实现，通过公开接口验证真实配置被调度器消费，并用 fake provider/supervisor/部署加载器注入故障。fixture 验收与真实模型/来源资格分别登记。

## 3. Bernstein 采用门

原报告的 Bernstein-first 在 2026-09-05 架构审阅中收敛为“先定义接口，按资格复用”。这是保留的 Runtime 采用门，不改变 r7 外置网关或 Workflow 控制权；是否已取得/验收某版本以有日期的来源与资格记录为准。普通插件和 routing hints 不足以证明强制准入。[来源与推断](sources.md#bernstein)

| Gate | 必须证明 |
|---|---|
| B1 固定版本 | 包/源码/配置可追溯，必需启动字段实际被接受 |
| B2 执行前拒绝 | admission 拒绝或异常时没有执行；明确覆盖 Attempt 还是每次模型请求 |
| B3 配置不漂移 | 重试、续接、配额错误不静默改变模型、执行器、认证或计费 |
| B4 内部工作受控 | delegation/fallback/continuation 能禁用或全部受授权及消费控制 |
| B5 启动可核对 | 已启动但回执丢失时定位原执行；未知时不重复 spawn |
| B6 恢复与取消 | 区分 running/exited/unknown，进程树和 session 可核对 |
| B7 无竞争控制 | 不启用自治 DAG、选路、业务重试或交付循环 |
| B8 无交付权限 | 执行器/项目工具无法借用 Git 凭据或交付端点 |

先关闭不需要的特性，必要时只添加明确的受控执行接口。若需改动多个核心循环才能满足契约，选用其他合格 Runtime 或显式原生兼容路径，不做大范围 fork；默认模型接入仍经外置 CLIProxyAPI。通过后也不把 Bernstein 格式变成角色、Workflow、计划和 Web 的公共契约。

## 4. 历史完整行为里程碑映射

下表保留 2026-09-05 代码 PR 场景的原里程碑和退出条件。三角色、跨来源与 PR 要求约束这些原场景，不是每个新 Workflow 的必选结构；当前排期以 [业务顺序](../planning/business-first.md) 和 [治理映射](../planning/design-governance-20260909.md) 为准。

| 阶段 | 交付物 | 退出条件 |
|---|---|---|
| M0 契约与资格探针 | 固定依赖、Profile schema、RunnerHost 探针、假 provider、最小资源账本 | 一个订阅端＋一个 API 端能启动/取消/核对；能力如实分级；作底座采用决定 |
| M1 第一条串行链路 | 薄 Web、规划/确认、矩阵、资源准入、Worker、测试/Review、PR | 用户确认后，跨至少两种来源完成小功能；选路与费用可解释 |
| M2 并行与多来源 | 2–3 任务 DAG、独立目录、串行集成、各来源接入 | 组合候选通过 gate；全部选定来源逐一有资格记录 |
| M3 配额与换源 | 共享/多窗口池、保留量、未知模式、自动换源、规则模拟 | 注入耗尽与外部消费；不降质量、不借未获准现金、不重复预留 |
| M4 日常可靠性 | 崩溃恢复、取消竞态、迟到结果、PR 核对、备份/升级 | 强制故障验收通过；在真实仓库连续运行并复盘 |

历史 M0 不必等所有服务接通才开始 M1，原用户选定来源仍须按各自范围补足资格。r7 新网关路径另验 GW-AC，不继承旧直接/native 来源的通过结果；未合格来源显示限制和未完成项。

实施任务采用纵向切片，例如“在仓库会话中批准两个显式代码步骤后得到可验证组合候选”，或“文字生成报告 Workflow、改图表后部署并运行该真实配置”。规格以 [活动 PRD](../prd/commander-workbench.md) 为准；P1–P4 是工程交付顺序，不是运行时固定流水线。网关、Workflow 与 Designer/部署新增范围单独承接，不能借旧票完成标记宣称已实现。

第一次真实业务模型调用即需合格配置、固定来源/授权集合、原币预算、有限 unknown 政策及适用隔离；资格探针独立有限授权，不要求先有它要证明的资格。用户选定主 Commander 交接仍由用户决定，其他角色按明确 grant 委派。Designer 创作可先于 Run，运行调度调用另受对应授权/累计预算；基础约束不推迟到资源 UI 完成后。

## 5. 可追踪验收矩阵

以下 A01–A26 是历史用例原文范围索引，PR/三角色不限制其他 Workflow。新 GW/WF/WD/RS/UA 以各主题原文为准，不回写或替代旧 AC；此表不表示当前执行状态。r8 必须验证角色实际生成不同规模子图、原授权内自动执行、重叠 grant/CAS、背压保留全部任务、扩展封口与义务，不只调大 max 字段。

| ID | 场景/故障 | 必须观察到的结果 | 阶段 |
|---|---|---|---|
| A01 | 真实小功能、2–3 子任务 | 确认后自动实现、测试、独立 Review、同一 PR；不自动 merge | M1–2 |
| A02 | Commander/Worker/Reviewer 分别来自不同合格来源 | 来源不限制同项目协作；配置、证据、费用可追溯 | M2 |
| A03 | 两个 key/模型争用账户短/周窗口 | 只接受容量允许的任务；不留下半份预留 | M3 |
| A04 | Worker/顾问持续请求保护池 | 不借主 Commander 保留量；主 Commander 权限按规则生效 | M3 |
| A05 | 外部用量、延迟报告、重复 usage | 不伪造精确余额、不重复计费；未核对支出保留 | M3 |
| A06 | 短窗口重置但周/月紧张 | 不绕过长池；跨窗口归属明确或标未知 | M3 |
| A07 | 429：拥塞/耗尽/未知 | 分类退避或阻塞，不反复探测已知耗尽窗口 | M3 |
| A08 | 耗尽换源、质量升级 | 原要求不下降；只用获准阶段集合；新 Attempt 保留原账 | M3 |
| A09 | 预留/spawn/ACK 各空窗崩溃 | 最多一个有效执行；unknown 先核对，无重复消费派发 | M0、4 |
| A10 | 取消失败、子进程存活、旧结果迟到 | 不声称已停止，不启用重复 writer；旧结果不能交付 | M0、4 |
| A11 | 发布扩大来源/预算的规则；批准旧版本 | 旧 Run 不静默扩权；旧 plan hash 被拒绝 | M1、3 |
| A12 | 环境/配置诱导模型或计费 fallback | 拒绝或停止并记录不符，不默许新收费路径 | M0、2 |
| A13 | 假 secret、junction、共享 Git、MCP/hooks、WSL 互操作 | 启用工具均不能读取平台/Git秘密、篡改他任务 | M0、2 |
| A14 | Worker 经 git/CLI/HTTP 尝试远端写入 | 无凭据或通路，不能 push/创建 PR | M0 |
| A15 | 测试失败、无合格 Reviewer、超时/日志缺失 | gate 失败或不确定，不能进入可交付状态 | M1 |
| A16 | 验证后改代码/基准/检查条件 | 旧证据不能授权新候选 | M1–2 |
| A17 | push/PR 丢响应，跨 revision 重复发布 | 查询同一分支/PR，条件更新互斥，无重复 PR/覆盖外部修改 | M1、4 |
| A18 | 反复修复/基础设施重试 | 达次数/预算/时间边界停止；API broker 逐请求计账，订阅不可观察部分保存 Attempt 覆盖范围/估算/未知 | M3 |
| A19 | 调用切片争抢，发送后记账前崩溃 | 父子不双算；send_unknown 保留上界，不盲目重发 | M0、3 |
| A20 | 后端重启、SSE 断线/游标过期 | 重新快照，批准/阻塞/交付事实不丢失 | M1、4 |
| A21 | 备份后执行/消费/撤销，再恢复旧快照；产物缺失或磁盘满 | 新 restore epoch 冻结旧 outbox；核对历史并重新决定继续；缺失证据不交付 | M4 |
| A22 | Plan v2 只改 B，复用已完成 A；旧 B 迟到 | v2 成员明确复用 A，旧 B 不能满足新计划 | M1–2 |
| A23 | 原生权限请求后取消；批准迟到 | 请求失效，不授予 turn/session 权限，不恢复已取消执行 | M0、M4 |
| A24 | 多项检查在同一验证轮失败，新建多个 repair Task | 一批只计一轮，继承根链/stage；新 Task/换源不重置上限 | M3 |
| A25 | 两个 Run 固定不同 Rulebook，共享保留量被更新 | 所有新准入读取当前 CapacityPolicy，旧 Run 不能绕过 | M3 |
| A26 | SDK 重试无逻辑 ID；币种不同或价格变化 | 每次接收重新准入，未知不按正文去重；原币预算不混算，失效价格不发送硬预算请求 | M0、M3 |

资格记录包含 case_id、runtime/profile/ModelBinding revision、网关及变换版本（适用时）、OS/隔离、观测输入、结果、证据、日期和限制。结果为 passed、failed、not_run、unsupported；unsupported 不计为 passed。Designer 的真实生成证据、部署加载回执与具体 Workflow Run 执行分别记录，旧 native S 或离线部署测试不能代替新路径 S。

## 6. 量测目标

先满足正确性。建议排除模型/网络/检查耗时后，普通命令/状态读取 p95 小于 1 秒，事件通常 2 秒内到 UI。原 2 writer＋审查＋规划仅保留历史负载样本，不是产品默认人数；r8 再验证角色按实际输入形成不同规模图，以及资源背压、持续扩展和部署期间 Hub 可操作。并发按显式用户政策和实测资源，不设 Karajan 全局/项目 coding Agent 默认上限。

记录按任务类别划分的成功率、修复次数、端到端时间、配额等待、已知消费/未知比例、恢复时间和重试成本。优化通过验收的交付成本与时间，不能只看 token 单价。模型不一定完成每项需求，平台必须正确表达失败、停止和恢复。

## 7. 服务运行、Workflow 部署与维护

单机 Karajan 包含 Web/协调器、Designer 配置/编译服务、可信 Workflow 部署器、RunnerHost、调用授权/账本、产物交付进程和数据目录；CLIProxyAPI 是独立部署的模型网关。Windows 控制端可连本机 WSL2/容器，路径转换、停止和 IPC 认证由适配器负责。网关与 Runtime 管理端均须与工具网络隔离，不能以同容器或 localhost 代替边界验收。

启动先校验配置/数据库并取得单实例锁，开放只读状态，恢复 grant/任期、图修订/决定、扩展封口/义务，核对创作/部署/未完执行和交付；新 Run 入口重取 active 加载事实后准入，旧 Run 用自身冻结定义和已接受图。关闭保存决定/outbox/执行/部署记录，旧 ready 不是新进程加载证明。

Workflow 发布只登记不可变定义。用户确认部署后先物化 pending、加载/readback 同一包与模板编译摘要，再 CAS 发布 active 与部署结果；旧 active 在准备期间有效，准备失败不替换旧版。复合“部署并运行”另绑定具体输入、Plan 和权限，一次确认可完成，无需重复审批；仅部署不启动 Run。回执丢失查询原命令，回滚也须核对目标并条件生效，旧 Run 仍固定原版本。详细失败窗口与 WD 验收以 [10](10-conversational-workflow-deployment.md) 为准。

数据根与用户仓库分开，日志轮转并报告磁盘不足。所有控制数据库（Project Registry、Run、Capacity、Task Admission）、执行账本和 RunnerHost 根都必须在实际注册的仓库根之外；打开既有根时解析路径别名，并对现有 SQLite inode 检查仓库内 hard link，不能相信调用方声称的路径，也不能为校验创建或修复目录。migration 在无活跃写入时执行；升级前备份，CLI/adapter 升级重跑相关资格用例。固定版本锁文件和兼容矩阵随实现保存。

备份停止新准入与部署切换，取得一致数据库快照，按引用复制不可变产物、配置包/定义/编译器版本并暂停垃圾回收；保存 manifest/校验值，恢复检查引用完整性。上游网关秘密由其独立运维，Karajan 所需客户端秘密独立备份或重新设置，不进入运行导出。不能只复制活跃 SQLite 主文件而忽略事务状态。[SQLite backup API](https://www.sqlite.org/backup.html)

备份还包含可用的 RunnerHost/调用账本/部署操作登记和版本 manifest；跨文件快照不能证明外部服务同时静止。正常重启沿用同一安装日志，历史恢复创建新 restore epoch、使旧 activation 无效、默认冻结恢复 Run/创作/部署副作用，旧 outbox 不自动发送。核对备份后消费、部署槽位、远端结果与撤销，历史缺口保持 unknown，由用户针对当前状态决定继续。旧预算不是新现金，旧快照也不能恢复已撤销授权。

保留/备份同时覆盖调用归属、原始计量观察/更正、实际路由证据与会话用量快照来源；可重建投影不替代账本，旧备份的统计缺口不能清零或按当前配置补造。按保留期清理已结束工作区和旧日志；无进程仍使用且产物已保存才能清理。active 部署、旧 Run、证据或未完恢复意图引用的配置/定义保留。睡眠、断网和时钟改变进入失联核对，不能把本地超时解释成服务端已取消。

## 8. 接入前待填配置

| 配置/事实 | 解决方式 | 影响 |
|---|---|---|
| 网关实例/版本、严格路由、真实模型/账户/计费映射 | 外置部署与 GW-AC 验收；无成本检查和获准真实探针分开 | 新 ModelBinding 启用，不继承旧 Profile 资格 |
| Designer 来源、创作预算与停止界限 | 独立规划授权及 WD-AC 生成/修复/未知恢复验收 | 可在目标 Run 前创作，不能无限自动修复 |
| Workflow 包/编译器、部署目标槽位与能力注册表 | 同源预览、pending 加载/readback、条件激活与读回验收 | 真实部署；具体 Run 输入稍后实例化并批准 |
| SchedulerGrant、动态动作/委派、终止及用户资源政策 | RS-AC 独立验收，保留初始授权、真实决定、图链、封口与背压证据 | 原范围自动调度，超授权另批；不预设 Agent 人数或截合法图 |
| 订阅档位、模型目录 | 官方接入＋有明确预算的资格探针 | 对应 Profile 启用 |
| 第三方厂商/endpoint | 用户指定并核对官方协议与计费 | 第三方接入 |
| 规划/Run 现金限额、保留量、超时 | 资源设置明确填写 | 相应消费准入 |
| 仓库/分支、小功能、检查命令 | 用户选基准任务，平台核对项目 | 真实端到端验收 |
| 目标机器隔离能力 | M0 验证 native/WSL2/容器路径 | 自主工具执行 |
| Bernstein/OpenCode 兼容版本 | 固定版本执行契约测试 | 该 runtime 采用 |

这些值的配置位置和失败语义已在设计中明确，不代表相应设置页面或后端已实现。可调初值在设置时明确，定义/部署/Run 的版本与授权边界保持独立，实际工作按原 Issue 和新增承接任务验收。
