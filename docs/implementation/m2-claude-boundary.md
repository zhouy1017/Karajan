# M2-03：Claude 订阅的离线适配边界

本切片实现固定版本的协议回放入口，完成 M2-03 的离线工程准备。**没有运行 Claude 模型，没有读取用户认证文件，没有接通真实订阅，也没有让任何 Profile 获得派发资格。** M2-03 的真实验收仍未完成。

## 1. 实际执行与来源

2026-09-05，在 Windows 上对已安装的官方 `@anthropic-ai/claude-code` 原生程序执行了 `--version`、`--help`，两者退出码均为 0，stderr 为空。版本为 **2.1.260**。执行使用新的临时工作目录、HOME、USERPROFILE、CLAUDE_CONFIG_DIR 和最小环境，不沿用用户配置或认证环境。没有执行 `auth status`、`doctor`、`-p`、登录或真实模型探针。

- [protocol-source.json](../../examples/claude/protocol-source.json) 保存实际二进制、输出及官方参考源的 SHA-256；[version.txt](../../examples/claude/version.txt) 和 [help.txt](../../examples/claude/help.txt) 为本机原始元数据。
- 安装包没有导出完整原生事件 schema；工具输入声明不能替代事件协议。此切片参照 [官方 Python SDK parser 的固定 commit](https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/b1b838b1c5730a7a0b270915a79b15861a8ca716/src/claude_agent_sdk/_internal/message_parser.py)，未安装或启动 SDK。参考源码 SHA 为 `febb1aee19e47e03433d89cd4b1f8c5636950e206df77e4f4ca2738b0c900393`。**参考解析器与本机 CLI 的实际兼容性仍是 not_run。**

官方接入路径是 Claude Code 的 print 模式和结构化流。原生最终结果可能先于后台进程结束；`--permission-prompts none` 从 2.1.259 开始提供拒绝消息；`api_retry.no_response` 要求 **2.1.261**。当前解析器拒绝这项新字段，不能因网络文档已更新就宣称 2.1.260 支持它。[官方 headless 文档](https://code.claude.com/docs/en/headless)

本机帮助声明 `--bare` 会跳过 OAuth 和 keychain，因此不能把它用作订阅配置隔离方案。`--safe-mode` 与 `--restricted` 可以保留普通认证路径，但帮助文本不能证明真实工具、管理配置或凭据边界已经受控。

认证优先级必须单独核验：云供应商/网关、环境令牌或 API key、helper 等配置可能先于普通订阅登录生效。此切片只保存假 `secret_ref`；不抽取 OAuth、不构造通用订阅代理、不以 `auth_mode=claudeai` 声明证明实际计费。[官方认证文档](https://code.claude.com/docs/en/authentication)

官方订阅说明当前置顶写明此前 SDK 调整已暂停。页面保留的旧条款不能作为已生效的新计费规则；这里不推断用户套餐、剩余额度或额外余额状态。[官方订阅说明](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)

## 2. 公共入口与固定输入

```powershell
.venv/Scripts/python.exe -m karajan.adapters.claude replay examples/claude/completed.json
.venv/Scripts/python.exe examples/claude/replay_examples.py
.venv/Scripts/python.exe -m pytest tests/adapters/claude -q
.venv/Scripts/python.exe -m ruff check backend/karajan/adapters/claude tests/adapters/claude examples/claude
.venv/Scripts/python.exe -m mypy backend/karajan/adapters/claude
```

库入口是 `karajan.adapters.claude.replay_file(Path)`。它读取一个普通 JSON 文件并返回报告；CLI 只打印报告。没有 spawn、网络、模型调用、凭据读取、工具执行或原生授权回复接口。退出码 0 只表示离线记录可被一致解析，包括任务失败、取消或阻塞的有效记录；退出码 1 表示输入/协议失败或证据不足。必须同时读取 `result.state` 与 `qualification`。

输入复用 M0-01 的 `Profile`、`AttemptManifest`、`Provenance`。外层未知字段、重复 JSON key、错误类型、非法 Unicode、超过 4 MiB 的文件和超过 10,000 条的记录被拒绝。布尔值不能充当 fence、revision、时长或 token 数。

Profile 需以排序 key、紧凑分隔符、默认 ASCII 转义的 UTF-8 JSON 计算 SHA-256；Attempt 的 Profile 引用、revision、完整 binding 和权限必须一致。输入还绑定 session UUID、配置来源摘要、官方参考摘要、开始时间与有限时长。配置摘要是调用方声明，回放不会读取磁盘配置验证它。

本版支持一个只读 Profile 子集：`runtime_kind=claude-code`、2.1.260、`claudeai`、`subscription_only`、Attempt 粒度；权限为 `workspace_read`；原生设置精确见 [completed.json](../../examples/claude/completed.json)。设置包含 Read、dontAsk、permission_prompts=none、safe_mode、restricted、空 settings/MCP 来源、无 fallback、单次 text 输入、verbose stream-json 输出。**这些是请求值，不是启动或隔离证明。** Bash、写文件、MCP、hooks、后台子 agent、多轮流式输入及其他 Profile 子集未启用。

`started_at` 和 `max_attempt_duration_seconds`（1–3600）仅用于回放中的结果截止线。它们不构成进程时限、订阅额度上限或现金硬预算的实施证据。Native init 报告的模型、版本、cwd、permissionMode、tools、MCP/plugins 必须匹配；`auth_mode`、`billing_path` 继续为 null，设置确认只能是 partial。

## 3. 映射与裁决

| 输入 | 公开报告 | 实际边界 |
| --- | --- | --- |
| `system/init` | 部分 native_reported binding | 不确认认证、现金路径或完整 settings |
| `assistant` / `stream_event` | 消息/流观察、主 agent 的部分输入计数 | 不回显文本、thinking、工具输入；不是实际调用或文件副作用证据 |
| `tool_use` / `user` 中 `tool_result` | 已观察的工具请求与匹配返回数量 | 仅接受 Read；超出列表关闭结果接收；不声称 OS 阻止了动作 |
| `result` success / error | 首个有效结果的摘要或受控错误分类 | 正文仅保留长度和哈希；错误原文不输出；success 不能证明进程已退出 |
| `system/api_retry` | 原生重试次数、等待时间、HTTP 状态和受控类别 | 不是每次传输的 receipt，也不能计算隐式重试消费 |
| `rate_limit_event` | 原生窗口、利用率和 reset 提示 | 不折算成 token 余额、不推断整个账户余量、不自行分配全局配额 |
| `system/permission_denied` / final `permission_denials` | blocked、requires_new_attempt | 没有单次原生批准传输；不会发送 allow；不能拿拒绝消息证明隔离 |
| controller cancel/revoke/fence 或截止线 | 关闭后续结果接收，保留 usage | controller 是回放输入，**不是 Claude 原生协议事件或实际取消命令** |
| 缺最终结果 | `not_run`、unknown，保留部分 usage | 不解释成零消费或安全退出 |

同一原生 uuid 的完全相同重放去重，冲突内容拒绝；同一 assistant message.id 的输入计数只记一次。单次输入的第一个有效 result 才能成为候选，后续 result 只能留下额外消费观察。取消、授权撤销、fence 或超时之后的结果不会成为候选；重新授权需要新的 Attempt。本回放没有持久启动、重连或 delivery service，不能代替 RunnerHost 的执行身份和交付 fence 校验。

当前只读子集遇到非空 `parent_tool_use_id` 会报 `NATIVE_SUBAGENT_UNSUPPORTED` 并关闭结果接收；assistant/message_start 中可解析的子 agent 计数保存在 `child_message_snapshots`，不并入主循环、不累计为账单，输出 token 仍标明是 assistant 占位数。成功 result 若同时携带非空 `api_error_status`，按矛盾记录报 `NATIVE_RECORD_INVALID`，不能形成候选。

usage 中，assistant 输入/cache 证据和最终主循环计数分开保存；不把流式 output 占位数当最终输出数。`modelUsage` 单独保存，出现未请求模型会关闭结果接收并保留消费观察。后续 terminal snapshot 不与首个 snapshot 相加。客户端美元估算始终与 `cash_charged_usd=null` 区分，失败或缺失记录不补成 0。[官方成本跟踪说明](https://code.claude.com/docs/en/agent-sdk/cost-tracking)

`stopping.main_process/process_tree/remote` 始终为 unknown。本切片没有 SIGINT/SIGTERM、Windows 进程树或 RunnerHost 物理停止接线。`dynamic_permission_grant=unsupported`；真实认证、计费和工具/停止能力为 not_run。所有报告强制 `qualification.live_status=not_run`、`dispatch_eligible=false`，包括调用方标成 imported_observation 的文件。

## 4. 验证结果与未完成项

Windows 本地公开 CLI 测试 **59 passed**；覆盖正确结果、Profile/版本漂移、bool-as-int、错误映射、去重与冲突、部分/最终消费、取消/撤权/fence 后的迟到结果、权限阻塞、Read 工具与 stream 记录、429/窗口提示、2.1.261 扩展拒绝、恶意/超大输入及子 agent/错误状态矛盾。ruff 与严格 mypy 通过。上述测试不要求安装 Claude，不访问账户，Linux 运行由仓库 CI 验证；本切片没有执行 WSL Claude 测试。

[样例生成器](../../examples/claude/replay_examples.py) 通过公共函数重新生成 7 个 synthetic case 和 [对应报告](../../examples/claude/reports/completed.report.json)。其中 truncated=not_run；binding-mismatch 与 261-extension-unsupported=failed；其余为有效离线观察。没有真实模型结果混入 fixture。

独立审查在首轮 52 个测试通过后发现上述两个漏检。保留了审查者的原始 synthetic 输入、修复前报告和相同输入的修复后报告，输入 SHA 一致：

- [子 agent 输入](../../examples/claude/review-fixes/unsupported-child.input.json)：[修复前](../../examples/claude/review-fixes/unsupported-child.before.report.json)错误接受；[修复后](../../examples/claude/review-fixes/unsupported-child.after.report.json)拒绝候选并单列子 agent 消费。
- [成功携带 401 的输入](../../examples/claude/review-fixes/success-auth-error.input.json)：[修复前](../../examples/claude/review-fixes/success-auth-error.before.report.json)错误接受；[修复后](../../examples/claude/review-fixes/success-auth-error.after.report.json)报矛盾记录。可用上方公共 CLI 对任一 `.input.json` 重跑；预期退出码均为 1。

M2-03 后续真实 gate 仍需：固定官方登录/模型与实际 Windows 或 WSL2 runtime；核验所有计费优先级及现金/额外余额后备；绑定 RunnerHost 持久启动与物理 inspect/cancel；分别实测原生 Read、Bash、文件写入、子进程、启用的 MCP/hooks 以及 WSL 互操作出口；验证真实任务、订阅 usage 和 unknown 消费；随后才可批准某个精确 Profile 的角色与能力。不继承 Codex 或 M0-06 Python canary 的资格，也不以这些离线报告勾选整票真实验收。
