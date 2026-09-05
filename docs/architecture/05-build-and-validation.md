# 实施、资格测试与运行维护

本文把设计转成依次交付和验收的工程工作。所有通过标准都是待执行标准；没有真实运行结果前不得填写 passed。

## 1. 已确认技术组合

| 部分 | v1 基线 | 理由与约束 |
|---|---|---|
| 后端 | Python、FastAPI、显式类型化领域对象和配置验证 | 与 Python 底座复用方向衔接；HTTP 等待 I/O，Agent/测试独立运行 |
| Web | React + TypeScript，按 HTTP schema 生成客户端类型 | 展示持久状态，SSE 增量与快照恢复；不另建业务状态机 |
| 存储 | SQLite、本地 WAL、外键、短事务、单写入协调器 | 单机可维护；配额与 outbox 同库原子提交 |
| 产物 | 内容寻址目录＋数据库 manifest | 大日志/diff 不挤入事务，完整性可核对 |
| 执行 | Codex app-server、Claude CLI、OpenCode API runner 三种适配器 | 复用真实 Agent 循环，把差异放在执行管理 |
| API 接入 | 自有窄 broker＋具体 provider 协议 | API key、逐调用准入、固定通道和消费记录 |
| 交付 | 独立受限进程、Git CLI＋托管平台接口；首个目标 GitHub | 控制远端副作用，核对分支/PR 身份 |
| 隔离 | 官方工具沙箱资格验收；API runner 优先独立容器/受限环境 | Windows 上优先探测 WSL2 的 Claude/API 路径，不假定已可用 |

FastAPI 的异步接口用于等待 I/O，CPU 工作和阻塞工具执行仍应隔离；React 是工作台实现建议。[FastAPI 并发](https://fastapi.tiangolo.com/async/)、[React 状态与界面](https://react.dev/learn/thinking-in-react)

SQLite WAL 同时只有一个 writer，需要本地文件系统。采用严格持久化设置并验收崩溃恢复；固定依赖时检查官方已知 WAL 问题及修复版本。[SQLite WAL](https://www.sqlite.org/wal.html)

首版不引入 Redis、Kafka、Kubernetes 或分布式锁。出现多机需求或明确写瓶颈后再迁移数据库、拆 RunnerHost；领域 ID 和协议不依赖本机 PID。

## 2. 拟定代码布局

```text
backend/karajan/
  planning/          # 需求、计划、Commander 任期与交接
  policy/            # 授权、Rulebook 编译与路由求解
  capacity/          # 配额观察、父子预留、消费核对
  coordination/      # 唯一业务状态机、依赖推进、outbox/inbox
  execution/         # RunnerHost、执行适配器、工具限制
  inference/         # API broker 与 provider 协议
  artifacts/         # 物化、候选、集成与证据
  delivery/          # 交付门、Git/PR 操作及远端核对
  persistence/       # schema、migration、事务与内容索引
  http/              # 命令、快照、SSE、本地会话
frontend/src/
  projects/ plans/ runs/ resources/ rulebook/ delivery/
contracts/           # 版本化 schema 和接口样例
tests/
  contract/ routing/ recovery/ acceptance/
```

这是拟定布局，不代表已创建实现。按第一条垂直流程逐步创建模块，避免一开始只生成空骨架。通过公开接口验收，使用 fake provider/supervisor 注入故障。

## 3. Bernstein 采用门

原报告推荐 Bernstein-first；本轮收敛为“先定义接口，按资格复用”。普通插件和 routing hints 不足以证明强制准入；固定版本源码尚未重新取得。[来源与推断](sources.md#bernstein)

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

先关闭不需要的特性，必要时只添加一个明确的同步执行接口。若需要改动多个核心循环才能满足契约，首版继续使用具体 CLI/API adapters，不做大范围 fork。通过后也不把 Bernstein 数据格式变成用户计划和 Web 契约。

## 4. 完整行为里程碑

| 阶段 | 交付物 | 退出条件 |
|---|---|---|
| M0 契约与资格探针 | 固定依赖、Profile schema、RunnerHost 探针、假 provider、最小资源账本 | 一个订阅端＋一个 API 端能启动/取消/核对；能力如实分级；作底座采用决定 |
| M1 第一条串行链路 | 薄 Web、规划/确认、矩阵、资源准入、Worker、测试/Review、PR | 用户确认后，跨至少两种来源完成小功能；选路与费用可解释 |
| M2 并行与多来源 | 2–3 任务 DAG、独立目录、串行集成、各来源接入 | 组合候选通过 gate；全部选定来源逐一有资格记录 |
| M3 配额与换源 | 共享/多窗口池、保留量、未知模式、自动换源、规则模拟 | 注入耗尽与外部消费；不降质量、不借未获准现金、不重复预留 |
| M4 日常可靠性 | 崩溃恢复、取消竞态、迟到结果、PR 核对、备份/升级 | 强制故障验收通过；在真实仓库连续运行并复盘 |

M0 不必等所有服务接通才开始 M1，但最终 v1 范围包含用户计划使用的全部来源。暂不合格的来源明确记录能力限制和未完成项，不悄悄删除。

实施任务采用纵向切片，例如“确认计划后经一次 API Worker 得到可验证候选”，同时连通数据、接口和界面。现在先发布 [PRD](../prd/karajan-v1.md) 和 M0 可执行探针 Issues；M1–M4 按 [路线图](../planning/roadmap.md) 保留阶段范围，待 M0 资格结果与接口收敛后逐批拆票。此次发布不启动开发或真实账户测试。

M1 的第一次真实执行即须具备合格配置、固定规则与批准集合、原币预算、有限 unknown 策略和必需隔离。主 Commander 不可用时等待用户决定，尚未实现的自动换源以明确阻塞处理。M3 完善规则编辑/模拟、资源平衡和交接工作台，不把这些基础约束推迟到 M3。

## 5. 可追踪验收矩阵

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

资格记录包含 case_id、runtime/profile revision、OS/隔离、观测输入、结果、证据、日期、限制。结果是 passed、failed、not_run、unsupported；unsupported 不计为 passed。

## 6. 量测目标

先满足正确性验收。建议本地性能目标：排除模型/网络/检查执行时间，普通命令接受和状态读取 p95 小于 1 秒；持久事件正常情况下 2 秒内到达 UI；2 writer＋必要 reviewer＋规划调用竞争资源时，工作台仍可操作。实际并发按机器和服务限制测定。

记录按任务类别划分的成功率、修复次数、端到端时间、配额等待、已知消费/未知比例、恢复时间和重试成本。优化通过验收的交付成本与时间，不能只看 token 单价。模型不一定完成每项需求，平台必须正确表达失败、停止和恢复。

## 7. 部署与维护

单机包含本地 Web/协调器、RunnerHost 执行环境、推理 broker、独立交付进程和数据目录。Windows 控制端可连本机 WSL2/容器；路径转换、进程停止和 IPC 认证由 adapter 负责。OpenCode 管理端与工具网络必须隔离，不能只在同一容器启动 server 就认为合格。

启动顺序：加载配置/secret refs → 校验版本/数据库 → 单实例锁 → 只读状态接口 → 核对所有业务或物理未完成执行/交付 → 对账 → 新派发。关闭先停准入，再保存 supervisor 状态并按设置等待或取消进程。

数据根与用户仓库分开，日志轮转并报告磁盘不足。migration 在无活跃写入时执行；升级前备份，CLI/adapter 升级重跑相关资格用例。固定版本锁文件和兼容矩阵随实现保存。

备份停止新派发，取得一致数据库快照，按引用复制不可变产物并暂停相关垃圾回收；保存 manifest/校验值，恢复检查引用完整性。秘密独立备份或重新登录，不进入运行导出。不能只复制活跃 SQLite 的主文件而忽略事务状态。[SQLite backup API](https://www.sqlite.org/backup.html)

备份还包含可用的 RunnerHost/broker 操作登记和版本 manifest；跨文件快照不能证明外部服务同时静止。正常重启沿用同一安装日志，历史恢复则创建新 restore epoch、使旧 activation 无效、默认冻结恢复 Run；旧 outbox 不自动发送。核对备份后消费/远端结果/撤销，历史缺口保持 unknown，并由用户针对当前状态决定继续。恢复旧备份不能把旧预算余额当作新现金或恢复已经撤销的授权。

按保留期清理已结束工作区和旧日志；无进程仍使用且产物已保存才能清理。睡眠、断网和时钟改变进入失联核对，不能把本地超时解释成服务端已取消。

## 8. 接入前待填配置

| 配置/事实 | 解决方式 | 影响 |
|---|---|---|
| 订阅档位、模型目录 | 官方接入＋有明确预算的资格探针 | 对应 Profile 启用 |
| 第三方厂商/endpoint | 用户指定并核对官方协议与计费 | 第三方接入 |
| 规划/Run 现金限额、保留量、超时 | 资源设置明确填写 | 相应消费准入 |
| 仓库/分支、小功能、检查命令 | 用户选基准任务，平台核对项目 | 真实端到端验收 |
| 目标机器隔离能力 | M0 验证 native/WSL2/容器路径 | 自主工具执行 |
| Bernstein/OpenCode 兼容版本 | 固定版本执行契约测试 | 该 runtime 采用 |

这些值已有配置位置和失败行为，不需要留下多套冲突架构。已确认的可调初值可在首次设置时明确调整，生效范围遵循策略版本和 Run 授权规则。
