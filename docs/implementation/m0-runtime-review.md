# M0 运行时与隔离切片审查

基准为 `6a3af0872c1d9661b808cc029fe3c08802d4787c`。范围为 OpenCode 本地模拟 provider 探针、WSL2 固定 canary 隔离探针、锁定的 runtime 依赖及 CI 安装步骤；对应 #6/#7 的部分验收。真实服务和完整工具隔离不在通过范围。

## Standards / Spec

OpenCode 由非作者独立检查 Standards 与 Spec，15 项公开测试、Ruff、严格类型检查通过。修复并验证了父进程代理影响管理请求，以及启动/清理异常时未完整关闭通信和保留证据的问题。两项修复后未发现新增可操作问题。

隔离模块由另一名非作者独立审查。发现已启动 canary 被终止后，空检查结果可能被标为 unsupported，默认测试继而跳过。真实 CLI 故障回归先复现退出 2，再修为退出 1/failed：保留进程身份、退出码、已观察写入与未收到报告的事实，未知检查不伪造为通过。修复后二次独立运行该故障用例通过。详见 [中断证据](../../examples/isolation/interrupted.report.json)。

审查来源摘要对应隔离 `probe.py`：`a0e622e6da74b5c366898f00b2fd035d4ddb6b9ecabf3712b6ca18e44d369f50`。WSL 实际 13 个 unittest 中 12 个通过、1 个 Windows 专用用例跳过；Windows 5 个通过、8 个 Linux/WSL 用例跳过。真实 runtime/MCP/hooks/账户边界仍为 not_run，报告的 dispatch_eligible 始终 false。

组合收集时发现不同目录的测试文件同名会导致 pytest 导入冲突，已将新增文件命名为 `test_opencode_probe.py` 与 `test_isolation_probe.py`，保留现有测试行为。运行时版本固定为 OpenCode 1.18.29；CI 两系统安装相同 npm 锁文件再运行实际探针，缺二进制不能静默跳过。

## 限制

独立审查不替代目标提交的 CI；新的远端运行记录在 PR 中补充。本次没有调用真实 provider 或读取真实认证，也没有取得现金计费上界、通用 runtime 沙箱或完整 #6/#7 出口资格。
