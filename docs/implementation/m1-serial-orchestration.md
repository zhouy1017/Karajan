# M1-03 串行协调：已批准计划到本地验证候选

对应 [M1-03 #13](https://github.com/zhouy1017/Karajan/issues/13)。本切片提供可持久恢复的串行协调入口，并用真实 SQLite、临时 Git 仓库和固定本地子进程连接 RunPlanner、RunnerHost、CandidateStore。**这是离线集成资格，不是 M1 或跨来源产品资格通过。** 真实模型调用和现金 API 调用均为零；F05、原生权限接线、资源准入及生产激活仍为 `not_run` / 未实现。

## 公共入口与调用者

`karajan.orchestration` 导出 `SerialCoordinator`、`LocalFixtureRunner`、`CoordinationError`。不增加依赖。

```python
coordinator = SerialCoordinator(state_directory, planner, host, candidates)
coordinator.enqueue(run_id, task_id, profile_ref={"id": "...", "revision": 1},
                    principal="owner", command_key="...")
coordinator.snapshot(run_id, principal="owner")
coordinator.advance(run_id)
coordinator.control(run_id, "pause", principal="owner", command_key="...")
coordinator.control(run_id, "resume", principal="owner", command_key="...")
coordinator.control(run_id, "cancel", principal="owner", command_key="...")
coordinator.retry(run_id, task_id, principal="owner", command_key="...")
```

默认构造方式在资格未接通时返回 `LIVE_QUALIFICATION_NOT_RUN`，不会调用 RunnerHost.prepare 或启动进程。`profile_ref` 必须显式选择，且符合当前批准计划、固定配置和角色规则；不隐式选择列表第一个来源。

`enqueue`、`control`、`retry` 核对 Run owner。幂等键按 principal 保存，重复同一命令返回原命令回执，换内容拒绝；它不是最新状态读取。被拒绝的 enqueue 独立保存拒绝回执，不覆盖已有 Run 的执行快照；首次拒绝、尚无协调快照时保留初始阻塞诊断。`snapshot` 返回最近一次持久观察，浏览器调用方必须传 owner。省略 principal 以及 `advance` 仅供可信本地协调服务，当前模块没有 HTTP 或浏览器执行接口。浏览器不得传 argv、规划回执、资格声明或 `LocalFixtureRunner`。

显式离线入口额外传 `fixture_runner=LocalFixtureRunner(new_fixture_root)`。只接受固定 fixture runtime/version/model、无认证和无原生设置的绑定。固定程序、Python、目录、检查/Review/故障模式摘要均写入 recipe 身份；它与资格报告不互换，不会启用任何真实 Codex、Claude 或 OpenCode Profile。

## 所有权、持久启动与恢复

协调器的 SQLite 保存 Run/Task 的本地业务状态、Attempt 固定输入、命令回执、outbox、inbox 和累计计数。RunnerHost 仍独占实际进程树、启动阶段、会话/退出和用量观察；CandidateStore 独占不可变候选及其证据。协调器不把退出码或 Runtime 的业务字段直接当成 Run 验收结果。

第一次入队先提交固定 Attempt ID、start_key、fence、Manifest 和 activation，再消费启动意图。崩溃恢复沿用同一身份和许可向 Host 核对；不创建替代 writer。Host 的 `before_spawn` / `after_spawn` 注入断点只在显式 fixture 模式提供。`unknown` 保留等待核对，不自动 retry 或声称未发送。排队许可 30 秒过期时稳定拒绝，不重新生成许可或准备新进程；已经被 Host 接受的启动仍按原身份核对。Run 截止时间会进一步收紧该许可及固定进程时限。

输入绑定包含用户需求、当前 Task 及其传递依赖的版本/路径/验收、批准授权和固定配置摘要。记录来源 plan revision/digest 与 planning term；term 不进入 Worker 有效输入摘要。用户逐次批准 Commander 交接之后，未受影响的已批准 Worker 继续使用原 start_key。后续已批准计划的影响清单包含该 Task，或授权/任务输入/配置改变时，先持久递增 fence 和失效意图，再由后续 `advance` 实际停止并核对。既有 Worker's term 不因交接而重写。

`pause` 停止新激活和候选接收，保留既有进程事实；它不等同杀进程。暂停期间若批准输入失效或产生新的质量门阻塞，resume 只清除暂停标志，保留当前结论，不恢复陈旧 queued。`cancel` 先持久保存终态意图和 fence，再核对实际子树停止。未证实停止保持 `cancelling_unknown`，不得释放为成功或再派 writer。配置 recipe 在运行中改变也走实际停止/失效。调用者必须持续驱动 `advance` 才能完成这类核对；本切片没有常驻后台派发器。

取消后的迟到结果由 Host 当前 fence 拒绝；迟到用量仍保留并可刷新到取消后的快照。进程 exited 不代表远端停止、消费已结算或资金可释放。这里没有新增现金账本。

## 固定输入、候选与质量门

基准 root 和 base SHA 取自 ProjectRegistry 冻结快照，通过 CandidateStore 的可信 Git 读取入口注册。Writer 只运行随仓库提供的固定 Python 程序，在显式 fixture 根下新建独立工作目录。目标已存在、路径越出根或经过符号链接/重解析点均拒绝；原始临时 Git 工作树不被 Writer 修改。

只有 Host 确认真实进程树停止且退出成功，才将实际工作区字节、原始 base、允许路径、输入摘要、作者身份/fence 和停止观察绑定到 CandidateStore。Collector 沿用 CandidateStore 已有的安全 Git/文件收集边界，不加载候选 hooks 或配置。该固定程序不是 OS 沙箱：同用户临时目录和路径检查不能证明任意 Agent 的文件、网络、凭据或管理 API 隔离。

检查与 Review 分别启动新的固定进程和候选副本，保存真实日志与退出观察。检查失败、日志缺失、Review 不确定或输出不符合固定协议均不能通过。Review 需要独立进程/context、已批准 Reviewer 注册来源及精确检查证据集；T3 无可证实不同家族 Reviewer 时在 Worker 启动前阻塞。固定 Review 必须覆盖完整声明候选路径，并使用候选原冻结策略允许的 Profile。

`local_gate_passed` 只表示单 Worker 的固定本地检查/Review 当前成立。重新 `advance` 会复验已完成候选的当前 gate；候选字节损坏、新候选取代旧候选或后续失败检查会撤销旧通过状态。这里核对的是冻结 Project 基准，实际远端 head/base/PR/CI 必须由 Delivery 再读取，不能用该缓存快照授权交付。所有状态都保留 `delivery_eligible=false`。

一个候选通过后，其他必需 Task 未完成时保持 `awaiting_tasks` 并列出 ID。多 Worker 候选合并后的集成验证尚未实现；不会把单个候选提升为整个 Run 的交付许可。当前切片的完整通过路径限定为单 Worker → 固定检查 → 独立固定 Review。

## 累计边界与资源接线缺口

从首次排队开始保存 Run 总次数、开始时间、持续时间上限、质量修复上限和每根任务基础设施重试上限。上限来自批准配置，不因重开协调器而重置。这里的次数是受监督的 Writer/检查/Review 进程次数，**不是模型调用或订阅消费计数**。固定检查也保守计入次数；其复用 fixture Manifest 的形式不表示生产确定性检查需要模型 Profile。

基础设施 retry 只接受同一固定 fixture Writer 的真实 exit 75、已确认停止及当前有效绑定，使用新 Attempt 和原根任务累计最多配置次数，默认 2 次。失败检查/不确定停止不能冒充基础设施错误。新 Worker ID 不在首次执行计划中时返回 `TASK_LINEAGE_REQUIRED`，防止换任务名重置根链。后续任务改版/质量修复的显式 lineage 协议尚未实现；不存在自动修复入口，质量修复次数仍为 0、enabled=false，默认最多 2 轮限制已记录。

`budget_ref` 目前只指向已批准配置，并不是 ResourceBroker / Capacity 的权威准入 receipt。真实 F03 原币现金上界、父子切片、send_unknown 与结算接线以及共享池当前策略的 activate 复验尚未完成。测试层/示例中的 synthetic planning receipt 只帮助构造已批准输入，不能为产品授权有限预算或任何模型调用。真实激活继续拒绝，不能把固定进程计数包装成真实资源保证。

当前每步使用协调 SQLite 的写事务串行处理本地状态；启动/候选等其他模块调用仍发生在该事务期间。没有跨 RunPlanner/ProjectRegistry/Host/CandidateStore 数据库的原子批准撤销协议，也没有实现规范最终要求的单后端进程锁、短事务外 outbox dispatcher、关系化实体/事件序号/foreign keys 或安装 epoch 恢复。生产接线必须补齐这些边界；不能由本切片的单数据库互斥推导出多实例全平台原子性。

## 可运行入口和证据

在项目根、安装现有开发依赖后执行；`--directory` 必须是新的显式临时路径，拒绝覆盖既有目录。示例只创建本地临时 Git 和假规划回执，不读取真实认证材料，不发模型或现金 API 请求。

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'backend')
.venv/Scripts/python.exe examples/orchestration/probe.py --directory <新的临时路径> --scenario success
.venv/Scripts/python.exe -m pytest tests/orchestration -q
.venv/Scripts/python.exe -m ruff check backend/karajan/orchestration tests/orchestration examples/orchestration/probe.py
.venv/Scripts/python.exe -m mypy backend/karajan/orchestration
```

另有 `production_blocked`、`unapproved`、`check_failed`、`review_inconclusive` 四个 CLI 场景。它们观察到指定拒绝时报告该探针 passed，Run 仍为 blocked；这不是资格或交付 passed。每次生成 report.json，包含源码指纹、OS/Python、日期、固定输入/Manifest、候选 manifest/base、检查/Review、outbox/inbox、实际进程与清理状态。finally 会停止本次 Host 拥有的进程，不删除证据目录。

2026-09-06 本地结果如下，当前提交 SHA 由发布者在提交后补记；源码 SHA 用于固定本地审查点。

| 入口 | 实际结果 | 证据 |
|---|---|---|
| Windows 最终全套公共测试 | 36 passed、1 POSIX 专项 skipped；67.01 秒 | `examples/orchestration/windows.junit.xml` |
| WSL Ubuntu / ext4 首跑 | 33 passed、1 expiry 测试 failed；59.23 秒 | `examples/orchestration/linux-first-run.junit.xml` |
| WSL expiry 诊断 | 等待后 UTC 尚早于 expires_at 2.035 秒，测试前提断言 failed | `examples/orchestration/linux-expiry-diagnosis.junit.xml` |
| WSL 修复审查问题后最终全套 | 37 passed；63.47 秒，包含真实 POSIX 链接 canary | `examples/orchestration/linux.junit.xml` |
| Windows 同一等待前提修正后定向 | expiry 1 passed；30.90 秒 | `examples/orchestration/windows-expiry-final.junit.xml` |
| Windows 5 个 CLI 场景 | 全部观察到预期成功/拒绝，退出 0 | `examples/orchestration/windows.scenarios.report.json` |
| WSL/ext4 独立 success CLI | 实际本地候选、检查和独立进程 Review 通过 | `examples/orchestration/linux.success.report.json` |
| Ruff、strict mypy | Ruff 范围包含模块/测试/示例；mypy 5 个源文件通过 | `examples/orchestration/freeze.report.json` |

WSL 首跑没有跳过或改判为通过。增加实际 UTC 观察后，诊断显示 `1788626927.514177 < 1788626929.5495715`：单次 monotonic sleep 返回时 UTC 仍未过期。测试现以最长 60 秒单调等待上限反复观察实际 UTC 截止，而不把“请求休眠 30 秒”当作过期事实；产品准入逻辑未改变。审查前 Windows 33 passed/1 skipped、WSL 34 passed 的记录保留为 `windows-pre-review.junit.xml` / `linux-pre-review.junit.xml`；随后两项独立审查状态修复产生新源码，已重新跑双方完整套件和 Windows 5 个 CLI / WSL success。旧与新 CLI 报告均保留；`pre-review.freeze.report.json` 对应旧源码。后续 CI 必须绑定实际提交再验证，不能用本地结果代替远端 gate。

### 红绿记录与范围

测试经批准的公开入口驱动真实 SQLite、Host 和临时文件/Git，不直接修改协调器表，不调用远程模型。保留的主要行为红绿包括：未批准/无资格零启动；重启和失联同身份；真实子树取消及迟到用量；缺 Review 资格与 T3 家族限制；失败/缺日志/不确定 Review；受影响输入停止与无关任务输入复用；累计次数和根链限制；越界/既有工作区拒绝；旧候选/证据失效；过期排队许可；运行中 recipe 改变的实际停止；部分路径 Review 拒绝。

后期反例的实际失败与修复结果：

| 公共场景 | 修复前实际观察 | 修复后实际观察 |
|---|---|---|
| 已通过后追加失败检查，再重开 | 仍 local_gate_passed | blocked，检查失败且 Review 检查集合失效 |
| 实际等待排队 activation 到期 | 抛 Host LaunchDenied，已准备工作区 | 稳定 ACTIVATION_NOT_CURRENT，零 prepare/spawn |
| 有 heartbeat 的 Writer 改 recipe | blocked 但子树仍 running | 先持久失效，实际 exited 后 blocked |
| Review 只声明部分 Worker 路径 | 继续 queued | REVIEW_SCOPE_UNSUPPORTED，无第三个进程 |
| WSL/ext4 工作区父目录链接指向假 canary | 可能写入根外临时 canary | FIXTURE_PATH_UNSAFE，根外空目录不变 |
| Worker candidate_ready 时提前 enqueue Review | 拒绝覆盖 Run；反复 advance 仍 1 Attempt，漏检查 | 拒绝回执独立保存；实际检查完成，awaiting_review、2 Attempts |
| 暂停期间批准输入变化，再 resume | 恢复旧 queued，Task 已 invalidated、零进程 | 保留 blocked / APPROVED_INPUT_CHANGED、零进程 |

独立 Spec 审查的提前 Review 原输入及完整 before/after 位于 `examples/orchestration/review-fixes/`，两份报告的 input_sha256 一致。对应公共回归红/绿 JUnit 分别以 `premature-review.*`、`resume-invalidated.*` 保存。已通过后无效任务不能破坏 local_gate_passed，以及合法第二任务不遮蔽待检查工作均有追加回归；这些追加验证不另声称发生过红灯。检查派发从持久 Task 的 candidate_ready 事实选择，不靠全局展示状态推断是否存在工作。

上述红绿是本地开发实测历史；完整旧实现未另做提交，不能仅凭最终文件声称可重放旧版本。Commander 交接保持 Worker、后续候选损坏/取代等已有保护的追加回归直接验证通过，不伪称先有失败。

F01 固定绑定、F02 启动/停止、CandidateStore 质量门在上述离线范围内有实际集成证据；F03 资源、F04 原生权限/隔离、F05 真实来源、跨来源相同候选、远端 Delivery、全平台恢复与多 Worker 集成均未由本切片验收。后续接线必须使用对应权威接口与真实证据，不能补一个支持布尔值就放行。

最终独立 Spec 和 Standards 窄复核均无未闭合发现。Spec 已在原始提前 Review 输入上观察到检查正常推进；Standards 另核对恢复失效状态、无效命令不破坏已通过 Run、合法入队继续检查三项回归。30 项源码和执行证据指纹均匹配当前冻结文件；原始输入和 XML/JSON 证据按字节保存。独立完整长测未重复，最终 Windows/WSL 完整结果来自上表已核验的执行记录。
