# 已批准任务的工作区与原生文件投影

本切片把已批准 v2 Run 的路径范围变成不可变工作区清单，并让 OpenCode 的原生
`read/edit` 使用同一组明确的文件权限。候选存储可以重建完整基线，后续收集变更时不会
把未提供给模型的文件误判为删除。它仍是实际 Worker 执行链的准备阶段，尚未接通
配额激活、RunnerHost 启动、自动 Collector 或 Review/PR 交付。

## 批准材料与基线

`ApprovedTaskWorkspace(admissions, candidates)` 只提供 `prepare` 和 `get`，参数为
Run、准入 operation 与 owner 身份。调用方不能上传路径、Profile、任意仓库或 passed 报告。
数据复用准入数据库的 operation，不另建 Run 或 Attempt 状态库。

准备要求 operation 仍处于未取消的 reserved 状态，且当前执行中的 v2 计划、完整 approval、
路由绑定和执行政策与原 assessment 一致。只支持已批准的无依赖 Worker。
在协调器和 Run 的锁内，从冻结的仓库身份、base SHA 读取 Git 基线：

- 读集合是批准 `read_paths` 覆盖的已有普通文件。
- 写集合是 Task `paths` 与批准 `write_paths` 覆盖的已有普通文件；每个可写文件也必须可读。
- 空范围、新文件、歧义路径和大小写冲突明确拒绝。

清单包含原需求、计划与验收材料、历史 Profile 来源、原 Attempt/context 身份、完整 baseline、
展开后的路径和输入摘要。一项 operation 只保存一份清单。重开后读取同一历史结果，不因
工作目录或 HEAD 已改变就重新捕获输入；再次准备仍要求批准和 reservation 当前有效。
这些历史来源不能替代启动时对当前 Profile、auth generation、配额窗口及 Attempt fence 的核对。

`CandidateStore.materialize_baseline` 根据已登记的 baseline ID 验证清单和全部 artifact，
再写入全新的目标目录。保留文件字节与模式；拒绝存储区重叠、路径链接、硬链接和内容篡改。
它与候选 materialize 共用相同的文件写入逻辑。完整基线包括未投影文件，不能直接把模型的
窄投影目录传给 `CandidateStore.freeze`。

## Linux 原生执行器

可信控制器可以向 `IsolatedOpenCode` 提供精确 `projection`：每项只有相对文件路径、
SHA256 和是否可写。它是低层挂载材料，不承担批准、路由或资格判断。父进程在启动前、
namespace 在挂载前后分别检查文件类型、链接、路径与内容；每个文件单独 bind mount，
只读文件以只读方式挂载，工作区目录本身只读。未投影文件留在控制器侧。

原生 OpenCode 配置由同一清单生成，默认拒绝所有工具，仅逐文件允许 `read` 和指定的
`edit`。UDS 出口、无宿主网络、无 shell/MCP、固定模型、关闭自动压缩及停止流程保持原约束。
暂不支持创建、删除或重命名文件。省略 projection 时继续使用旧固定 fixture 路径；新增
投影实现也纳入 runtime source 摘要，旧来源记录不会自动成为新实现的资格。

Go 调用账本另有显式 `task_attempt` subject，绑定 project/Run/Task、Attempt/fence、
approval、执行政策、工作区、认证来源和 Profile/runtime。旧 qualification 绑定保持兼容。
两种 subject 不能互换；固定资格探针在读取 runtime 或创建目录之前拒绝 Task grant。
重放、已占用的发送槽位和撤销状态继续使用同一持久账本。

## 验证与接续

本地 Linux 原生投影、旧固定 runtime 与组合回归共 26 项及 8 个子测试通过，包含任意已有
源文件的真实 native read/edit、只读修改拒绝、越界读取拒绝、输入篡改和链接零启动。
上游为本地 HTTP fixture；这些结果不代表新一轮官方 Go 调用。

任务 grant 的独立公共检查及回归共 99 项通过；完整基线 materialize 的独立检查和原回归
在 WSL 共 98 项通过；投影额外独立检查 20 项通过。Workspace 新增 17 项公共测试，
Windows 相关回归共 44 项通过、WSL 与 baseline 合并共 33 项通过。Workspace 测试使用
真实 Run、批准、Capacity 与 Git 存储，仅正向资格来源明确为 synthetic。
Workspace 额外独立检查 4 项通过，既有固定 Go suite 回归 18 项通过。
Ruff 与 backend mypy（112 个源文件）通过。新增测试由现有必需 `pytest tests` 步骤收集，Linux
原生项继续要求固定 runtime 实际运行，Windows 明确跳过 Linux 专用项。

下一步将清单接到启动前资格/配额校验与 RunnerHost，逐次检查完整模型请求的上下文，
从确认停止且 fence 当前的 Worker 捕获授权变更，并重建完整候选。检查命令与 Reviewer
政策仍需绑定可信的版本化定义。上述接线完成前，固定 Go 资格的默认任务 guard 继续拒绝，
不通过配置数值补齐未知能力。

关联：[后续资格计划](../planning/go-runtime-qualification-next.md)、
[任务准入](m3-task-admission.md)、[门禁与 Copilot 修复分工](testing-gates.md)。
