# Go 已有文件投影的执行资格

本切片将实际文件投影、逐次计量和停止后的候选采集组合成显式 revision 2 资格，供已批准任务的路由读取。它延续 [持久资格入口](m3-go-profile-qualification.md)，使用独立固定样本完成资格验证；任务仍从自己的批准与 [Workspace](m3-approved-task-workspace.md) 获取具体文件范围。

## 固定资格与任务的边界

控制器显式配置 `FixedGoSuite(..., suite_ref={"id": "opencode-go-native-read-edit-linux", "revision": 2}, accounting=...)`。默认仍为 revision 1，旧观察和历史重放不升级。Profile 的 `native_settings.suite_ref` 必须与控制器一致。

revision 2 的两个固定场景分别验证：读取只读参考和已有代码、修改代码并保留完整基线；以及实际拒绝未授权读取并保持基线。每个场景先持久化独立 qualification grant，最多六次发送。每次最终请求使用同一固定参考 tokenizer 计量，工具历史与参考输入只记录保留事实和摘要。

`GoCallJournal.authenticate_grant` 是只读身份检查，不创建发送槽，也不承诺之后仍有效。真实 HTTP 发送仍必须经过 `begin_call`。身份、场景、来源、计量配置不符时，观察器不能启动 namespace；并发撤销仍由每次发送门禁执行。

原生执行停止后，`capture_projection()` 取得固定身份的文件字节，`CandidateStore.freeze_projection()` 重建完整 Git 基线。资格复核同时读取真实候选账本和重建文件，检查只读文件、未投影二进制与执行位均保留。测试与独立 Review 的缺失仍体现在 Candidate gate 中，采集成功不生成检查或审查通过记录。

## 事实的范围

只有官方来源、精确 revision 2、两个场景及采集和计量全部通过，当前资格 Store 才导出以下范围：

| 轴 | 当前资格范围 |
| --- | --- |
| 角色与任务难度 | Worker、T1 |
| 工具与文件 | read/edit、已有普通文件；不支持新建、删除、重命名 |
| 能力 | bounded_code_edit、controlled_tools、candidate_capture |
| 计量 | 固定来源的 reference_tokenizer_estimate |
| 输入 / 输出 / 运行上下文 | 12,288 / 4,096 / 16,384 token |
| 输入余量 | 固定 2,048，加本地计数的 20% 向上取整 |
| 请求数 | 每个 grant 最多 6 次 |

这些限制保存在 `executor_scope`。`context_evidence` 分别记录既有官方来源声明、当前执行器限制和本次小输入观察；16,384 不是实测最大模型窗口。当前原生配置固定请求 4,096 输出 token，任务政策要求更小或更大输出时都不能选择该执行器。

本地 HTTP fixture 的事实单独保存为 `projected_native_tools_fixture`，不能导出生产 `runtime_tools` 能力。配置里手工声明的 passed 不替代 Store 观察。相同 Profile/scope/suite 的最新 unknown、failed、撤销或来源变化不能回退到旧成功。

## 已批准任务如何消费

`ApprovedRunRouting` 在现有资格 guard 内读取 `executor_scope`，检查当前 Task 的角色、有效难度、工具及 ExecutionPolicy v2。上下文计量来源必须一致；任务可以降低输入与运行上下文上限、增加余量，不能扩大资格范围。运行上下文取项目上限与资格上限中的较小值。

解析结果 `execution_context` 与 qualification 引用、scope 摘要、执行政策摘要共同保存在 assessment 的 profile source 中。预约后的 `reserved_execution_guard` 比较整个 source，资格或解析结果变化会阻塞原 Attempt；不能借复查暗中换源。

判断成功仍不启动模型。后续真实消费者必须在实际 namespace 创建处持有当前 Run、Project、Capacity 与 Attempt 身份检查，并从已批准 Workspace 取得文件。Commander、Reviewer、T2/T3、其他来源、真实检查与完整 PR 交付继续按各自资格推进。

## 验证

本地验证使用真实 SQLite、Git、固定 OpenCode ELF、Linux namespace、Unix socket 和参考 tokenizer；只有测试上游响应与规划准入替身明确标为合成材料。官方验证通过单独的 `examples/go-projected-qualification/run_live.py --live ...` 入口执行；缺少显式参数时不读凭据或发送请求。

首轮官方观察在第一次 HTTP 200 响应处被 `INVALID_TOOL_NAME` 拒绝，未发生工具修改，已确认本地停止并撤销剩余发送；第二场景未运行。原始响应没有保存，因此无法仅凭该错误码确定具体字段形态。独立本地复现另发现流式工具名 `null` 被错误拒绝：这类可空增量由 [OpenAI 官方生成类型](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_chunk.py) 定义。兼容修复只把 `null` 作为无新增名称的片段；最终完整名称仍仅允许 read/edit，其他类型、超长或不完整调用继续拒绝。成功时可额外记录可空片段数量，不保留名称原文。

实际执行次数、检查结果与来源摘要见 [本切片记录](../../examples/go-projected-qualification/README.md)。GitHub CI 运行无真实凭据的测试；CI 失败按 [既定约定](testing-gates.md) 在 GitHub 委派 Copilot。任何 PR 合并仍由项目所有者决定。
