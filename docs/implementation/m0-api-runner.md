# M0-05：官方 OpenCode 与本地模拟推理通路

本实现对应 [M0-05 / #6](https://github.com/zhouy1017/Karajan/issues/6)。它运行官方固定版本 `opencode-ai@1.18.29`，由 OpenCode 完成工具循环；Python 只管理探针、接收 HTTP、模拟模型响应和保存证据。没有真实账户、订阅凭据或现金 API 调用。此实现的自主执行资格始终拒绝，原因是管理面与工具身份尚未隔离、没有 OS 网络封闭和真实服务资格。

## 公共入口

`OpenCodeProbe(runtime_path, fresh_directory).run(scenario, tamper=None)` 返回 `ProbeReport`，并写入证据。目录必须全新，不覆盖旧执行。只接受内置本地场景，不接受服务商密钥或任意远端 URL。

```text
uv run --frozen --extra dev python -m karajan.adapters.opencode --runtime runtimes/opencode/node_modules/opencode-ai/bin/opencode.exe --directory .local/api-runner-demo --scenario tool_loop
```

Linux 将 `opencode.exe` 改为 `opencode`。先依据 [runtime pin](../../runtimes/opencode/README.md) 安装锁定依赖；缺失二进制或版本变化应使测试失败，不能跳过后显示通过。

运行全部内置场景并生成索引：

```text
uv run --frozen --extra dev python examples/api-runner/run_suite.py --directory .local/api-runner-suite
```

CLI 退出码 0 表示完成本地执行或取消观察；1 表示输入/配置拒绝；2 表示执行器报错、观察超时或清理失败。`timeout_once`、`admission_limit` 和 `cleanup_fault` 刻意产生 2，配置篡改刻意产生 1。任何退出码都不表示真实 Profile 合格；输出中的 `live_qualified`、`profile_enabled` 固定为 false，`qualification_decision` 固定为 rejected。

## 实际通路与身份

官方 server 监听一个 loopback 管理端口，受一次性 Basic 凭证保护。模型请求发往另一个 loopback broker 端口，再由 broker 转发至第三个本地模拟 provider 端口。工具只有 OpenCode 的 `read`，读取新工作区中的随机夹具文本；模拟 provider 只有在下一次请求携带真实工具结果时才返回对应最终文本。Python 不调用工具来替代 OpenCode。

可信管理连接 `ManagementClient` 只接受固定 `http://127.0.0.1:port`，使用标准库 HTTPConnection 直接连接；不发现父进程/系统代理，也不跟随 HTTP 重定向。公开 CLI 已用假代理与空 NO_PROXY 验证仍可执行；普通请求和 SSE 都用本地 302 响应验证不向重定向目标发送凭证。它是 RunnerHost 侧连接，不是供工具使用的管理 API。[Python HTTPConnection](https://docs.python.org/3.12/library/http.client.html)

复用 M0-01 的 Profile、AttemptManifest、Binding 与 ProbeDocument。夹具声明 API 通路，因此契约中的 `billing_path` 是 `api_cash`；其账户固定为 `no-real-account`、授权为 `offline-fixture-only`、预算引用为 `no-cash-calls`。这是合成协议身份，不是已配置现金来源或支出授权。

Broker 绑定 Attempt、fence、Profile 摘要和一次性合成能力，逐次校验固定模型、路径、read 工具集合、256 输出 token 上界与 1 MB 请求体上界。每次接收都生成独立 receipt，包含完整请求体和除 Authorization 外的请求头。正常场景最多准入 6 次，限额场景仅准入 1 次；这只是传输探针的次数限制，不能替代 M0-03 的持久现金账本、fencing 撤销或最终计账。

本机实际请求头存在 `x-session-id` 与 `x-session-affinity`，没有可证明跨重传稳定的 logical call ID；它们表示会话，不能据此合并两次模型请求。重试和相同 body 均作为新调用重新准入。固定源码的 AI SDK 入口默认 `maxRetries` 为 0，而会话层有独立重试策略；因此不能仅依据一个 SDK 参数宣称无重试。[固定 LLM 源码](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/llm.ts)、[会话重试源码](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/retry.ts)

## Windows 实测场景

2026-09-05，Windows / Python 3.12.14 / OpenCode 1.18.29：

| 场景 | 可观察结果 |
|---|---|
| `tool_loop` | 两次模型 HTTP 请求，真实 read 工具结果进入第二次请求；收到 server SSE 文本增量与工具事件 |
| `rate_limit_once` | provider 第一次返回 429；真实会话重试后完成工具循环，共三次独立准入 |
| `disconnect_once` | provider 首次在响应头前断线；真实重试后完成，共三次独立准入 |
| `timeout_once` | provider 首次延迟响应，执行器 500 ms 超时报 `UnknownError: The operation timed out.`；本场景观察到一次接收，没有重试，报告 runtime_error |
| `cancel_stream` | provider 保持流；管理 abort 回执为 true，随后至少 0.5 秒内没有新增模型请求；保存取消时间与观察窗口 |
| `admission_limit` | 第一次请求可读工具，第二次模型请求被 broker 403 拒绝，provider 只收到一次 |
| `cleanup_fault` | 实际工具循环后，在 server 清理完成处注入异常；仍关闭本地 HTTP peers、保存全部轨迹并报告 unknown |
| `--tamper model/permission/endpoint` | 修改实际传入的启动配置，GET /config 核对发现与固定配置不同，发送 prompt 前拒绝，零模型请求 |
| 继承环境污染 | 注入合成 API key 与不相关配置路径，独立 CLI 忽略这些继承值，仍只使用本地夹具 Profile |

超时、重试和取消结论只适用于这些明确注入方式与观察窗口。没有测试所有断线阶段，也不从客户端 abort 或 TCP 断开推断远端推理停止、退款或账单结清。

## 配置和隔离边界

每次运行使用新 HOME/USERPROFILE、XDG_CONFIG_HOME/DATA_HOME/CACHE_HOME/STATE_HOME 以及临时目录；清理继承的认证和 OpenCode 配置变量。关闭项目配置发现、外部技能、默认插件、自动更新、模型目录抓取、自动压缩、分享、LSP 和额外 Agent；正常探针命名会话，避免标题生成。实际 GET /config 值要与固定 Profile 配置匹配，provider 接收端另核对模型和工具请求。

上述“关闭”描述显式设置的配置与开关。实际 SSE 仍出现 provider 注册、models-dev、config-plugin 等内置 `plugin.added` 事件，不能把空插件配置解释为所有内置模块均未加载。这些场景没有捕获到额外的模型调用，不足以证明后台 HTTP 总量为零。

这些配置降低意外继承，但不是 OS 安全边界。官方配置会合并多个来源，启动后的恶意配置竞争、managed settings、内置组件、绕过代理的额外网络路径不能由此得到全面否定证明。代理与 npm registry 指向无服务的 loopback 地址，也不等同于防火墙。当前只证明已捕获的模型请求经过本地 broker；没有声称所有进程网络流量都被捕获。[官方配置](https://opencode.ai/docs/config/)、[固定配置加载源码](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/config/config.ts)

管理凭证在官方 server 环境内；工具与 server 仍处同一 OS 用户身份，环境或文件访问的隔离没有经过证明。因此管理面隔离与 OS egress 均标记 unsupported，不允许据此授予无人值守仓库执行资格。动态工具授权、完整工具子进程树和生产 broker 接入留给 M0-06/07。真实 DeepSeek、Go 和第三方认证、协议参数、用量、计费以及远端取消全部 not_run。

## 证据与检查

每个目录保存 `report.json`、`broker-receipts.json`、`provider-requests.json`、`server-events.json` 与兼容 M0-01 的 `probe-document.json`，另有官方 server 日志和隔离状态。所有内容来自合成场景；随机 fixture 文本也会出现在轨迹中。可重新执行相同场景获得新证据，不能将旧轨迹当成真实账户验收。

清理先请求终止 server 主进程，最多等待 2 秒，再强制终止并最多等待 2 秒；无法证实时保留 unknown。SSE socket 显式 shutdown 后关闭。每个清理步骤独立收集错误，最后尝试保存已捕获证据；主进程退出不证明工具子进程树或远端停止。清理异常测试是明确注入的控制器失败，没有声称实测 Windows 进程拒绝 TerminateProcess。

```text
uv run --frozen --extra dev pytest tests/adapters/opencode
uv run --frozen --extra dev ruff check backend/karajan/adapters/opencode tests/adapters/opencode examples/api-runner
uv run --frozen --extra dev mypy backend/karajan/adapters/opencode
```

本机首轮 11 项公共入口测试与完整 9 场景 CLI 示例已通过；审查后增加代理、重定向和清理失败回归，最终 15 项测试通过（46.75 秒），完整 10 场景 CLI 示例退出状态符合预期。Ruff、Windows/Linux 类型检查均通过。Linux 实际运行待 CI，不以 Windows 通过或 Linux 类型检查替代。工具协议选择、管理接口和取消调用依据 [官方 server 文档](https://opencode.ai/docs/server/) 与实际二进制 `/doc`，Chat Completions 使用官方支持的 `@ai-sdk/openai-compatible` 配置。[官方 provider 文档](https://opencode.ai/docs/providers/)
