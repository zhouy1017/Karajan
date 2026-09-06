# Task 执行基础接口的本地验证

对应 [Issue #89](https://github.com/zhouy1017/Karajan/issues/89) 和
[实现说明](../../docs/implementation/m3-go-task-execution-foundation.md)。
本目录不包含官方 API 调用、原始模型历史或真实密钥。资格替身、Host 身份替身和 HTTP
fixture 均在各测试/报告中明确，不将其升级为真实部署资格或完整批准任务的执行结果。

| 范围 | Windows | Linux | 实际覆盖 |
|---|---|---|---|
| 新 send_guard | 9 通过 | 9 通过 | 本地 HTTP、真实 Journal、撤回线性化和未知发送 |
| 原 Relay 相关回归 | 196 通过 | 单列新门禁 | 作者来源见 send-guard-author/freeze.json |
| Host 全组 | 80 通过 | 与绑定合并 85 通过 | 实际进程、取消、重放、直属 child 身份 |
| Host 独立审查 | 11 通过 | 11 通过 | 同组孙进程、旧 schema、supervisor 丢失 |
| 执行意图 | 23 通过 | 23 通过 | 真实 Run/Project/Capacity/Workspace SQLite；资格为替身 |
| 意图独立审查 | 9 通过 | 9 通过 | 并发 claim、原取消入口、锁顺序、旧回执非权限 |
| 原生 Task producer | 7 通过，15 Linux 专用跳过 | 22 通过 | 实际 namespace/OpenCode、HTTP fixture 的 read/edit 和撤回 |
| 批准输入编译 | 24 通过 | 24 通过 | 临时 Git、真实 CAS、固定输入与扩权拒绝 |
| Task 标识编译 | 5 通过 | 5 通过 | 原批准 operation 到 Manifest/Activation/Journal binding |
| 标识独立审查 | 4 通过 | 后续组合回归 | 修复 operation/context/schema 外层身份遗漏 |
| 来源和参考计量 | 2 Linux 专用跳过 | 2 通过 | 真实固定 ELF/tokenizer、批准政策、只读计量 |

这些集合有重叠，不累加为一个“总通过数”。本地验证不替代当前 PR head 的必需 CI。
第一批来源下的标准检查为 Ruff 与 Windows/Linux 两种 mypy 平台检查通过。
后续与 Copilot 基准修复整合后的验证另加记录，不覆写已有来源或原始报告。

`publication-map.json` 将原 `.cache` 证据路径映射到公开路径，保留字节与 SHA-256。
各作者/独立审查 freeze 或 review 记录描述其实际测试的源码；历史 XML 包含预期红灯、
测试夹具错误及复验，不将失败报告改写为通过。Host 初次测试使用 Windows venv 启动器
而非实际直属解释器，随后明确选择真实解释器；不因此放宽直属 child 身份约束。
原生测试新增 case 的缺失 import、输入测试错误码包装和绑定测试的旧 fixture clock
问题在对应说明中记录。目录收录现存历史报告，不补造已被后续本地运行覆盖的输出。
最终生产行为按正式测试目录执行。

当前尚未实现固定可信 entry/facade 以及完整 Candidate 结果写回。本片 producer 返回的
StoppedProjection 不等于候选已通过验证或 Review，取消意图也不等于所有本地/远端效果
已结束。后续消费者必须完成实际业务 guards、grant/Host 恢复、Collector 和取消收尾。
