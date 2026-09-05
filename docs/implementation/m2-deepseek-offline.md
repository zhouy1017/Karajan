# M2-04：DeepSeek 协议与离线预算接入

关联 [M2-04 #20](https://github.com/zhouy1017/Karajan/issues/20)。本切片提供纯协议转换、固定 OpenCode 的真实本机工具循环，以及现有 ResourceBroker 的原子逐调用准入。没有连接真实 DeepSeek、读取账户认证或发送现金 API 请求；真实资格继续为 `not_run`，不能关闭整张来源接入票。

## 已实现的入口

- `prepare_request(bytes, model=…, output_limit=…)`：校验并规范化有限的文本、单个 Read 工具和非思考请求。返回官方 endpoint 身份元数据和固定 wire body，本函数没有 I/O。
- `observe_response(bytes, model=…, content_type=…, status=…)`：解析有界 JSON/SSE，保留模型、请求 ID、工具请求和可见 usage。它不执行工具、不发送重试、不推导实际账单。
- `DeepSeekOfflineProbe(runtime, directory).run_file(spec)`：只接受固定本地场景和两种模型标识，运行本机假服务与 OpenCode 1.18.29，持久化 F01 Profile/Attempt、逐调用 receipt、账本及响应观察。
- `python -m karajan.adapters.deepseek probe …`：相同离线入口的命令行包装，返回机器可读的结果。不能提供真实 endpoint、密钥或启用生产模式。

```text
.venv/Scripts/python.exe -m pytest tests/adapters/deepseek -q
.venv/Scripts/python.exe -m ruff check backend/karajan/adapters/deepseek tests/adapters/deepseek examples/deepseek
.venv/Scripts/python.exe -m mypy backend/karajan/adapters/deepseek
.venv/Scripts/python.exe -m karajan.adapters.deepseek probe examples/deepseek/cases/tool_loop.json --runtime runtimes/opencode/node_modules/opencode-ai/bin/opencode.exe --directory .cache/deepseek-one-new-run
.venv/Scripts/python.exe examples/deepseek/run_suite.py --runtime runtimes/opencode/node_modules/opencode-ai/bin/opencode.exe --directory .cache/deepseek-another-new-run --output .cache/deepseek-another-new-run.report.json
```

每次提供新目录；已有证据不覆盖。样例 [固定输入](../../examples/deepseek/cases/tool_loop.json)、[整套入口](../../examples/deepseek/run_suite.py) 和 [实际 Windows 报告](../../examples/deepseek/windows.report.json) 已保存。完整本机请求及进程事件位于执行目录，提交的汇总保留源文件 SHA-256、输入摘要、调用/账本/usage 事实及退出情况。

固定场景 JSON 和审查用 SSE 按原始字节存储，避免 Git 换行转换改变证据中的输入摘要；SSE 的末尾空行是协议结束分隔符。

## 协议依据与支持范围

2026-09-05 核对 [官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)、[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/) 和 [工具调用](https://api-docs.deepseek.com/guides/tool_calls/)。本切片选定 `deepseek-v4-flash` 与 `deepseek-v4-pro` 文本子集，并显式设置 `thinking.type=disabled`；官方默认是 enabled。工具返回关联原调用 ID；工具参数仍须由执行侧验证。

DeepSeek 的流式 usage 位于带终止原因的最后一个内容 chunk，没有单独的 usage-only chunk。缓存命中/未命中属于输入细分，reasoning 属于输出细分，不能重复相加。缺失字段保持 unknown/partial；不从 token 观察计算真实账单。价格页不被复制成此切片的计费资格。

当前最大输出 256 是离线 Profile 的测试限制，不是服务最大能力。文本 parts 归一成字符串；固定 SDK 的空 `reasoning_content` 被去掉，有内容的思考输入拒绝。其余工具、多模态、思考、额外请求参数和未列出的模型拒绝。模型响应只有与固定模型/请求 ID/创建时间一致的单选择流可接受；错误类型、重复 JSON key、非法 Unicode、过大正文与结束后追加内容返回稳定失败原因。

SSE 未完成的最后一帧不能终结响应；损坏尾部也不会抹掉前面已观察到的 usage。工具请求只有在完整结束和参数结构有效时才成为可用观察。此解析边界并非真实服务接受配置的证明。

## 真实本机行为与假计费的边界

实际路径为：固定 OpenCode → 本地 chat-completions 网关 → 现有 ResourceBroker → 本地 `/infer` 脚本服务。网关先绑定 Attempt/fence/Profile 摘要、模型、权限和最大输出；每个合格请求由 broker 保存新 receipt 和 send intent，再进入假服务。`/infer` 是既有账本的离线封装，内部包含规范化的 DeepSeek 协议 body，不能拿去作为生产 DeepSeek transport。

假服务请求 Read，OpenCode 实际读取新建临时文件，再把内容放入下一次模型请求，假服务最后返回相同内容。全程复用现有 SQLite 账本，没有新增现金账本。每次固定 `0.010000 CNY`、总额 `0.060000 CNY` 都是假服务测试数字，与 DeepSeek 的真实 token 价格或用户余额无关。

429 后 OpenCode 的实际重试获得新 call/receipt；既有未知调用的上界继续占用。断线、缺 usage 或缺终止帧均不提供可执行工具返回，保留未知占用；关闭并重开账本不再次发送。预算为零、价格失效、收费项不全、缺输出上界或模型变化的场景，实际假服务接收数为零。

执行使用隔离的临时 HOME/XDG、固定本地 provider、关闭自动更新/默认插件/外部 skills 等配置，并在发送前比较实际配置。继承的假 API key、代理和无关 OpenCode 配置未进入执行结果。仍未证明生产 OS 出站限制或管理凭据与工具进程隔离；OpenCode 的只读工具能力不能据此提升为全部生产工具能力。未启用真实服务、生产网络代理或外部发送入口。

## 实际验证与剩余验收

2026-09-05，Windows/Python 3.12.14 上 **66 项测试通过，34.72 秒**：50 项纯协议行为和 16 项本机/CLI 测试。Ruff 和 6 个源文件的 strict mypy 通过。另一次独立样例执行的 **14 个固定场景全部通过**，包括正常工具循环、六类零接收拒绝、429、断线、缺 usage、缺终止帧和三类实际配置漂移。该结果仅是 Windows 本机证据，尚未宣称远端 CI 或其他操作系统的当前提交已通过。

独立审查重跑上述 66 项（34.30 秒）及静态检查后，发现三个 P2：非空思考内容被忽略；部分工具参数可经正常 tool_calls 返回；超大整数转换浮点时抛裸异常。新增四个公共回归先失败后修复，独立复验在原始输入上关闭三项问题；保存 [思考漂移](../../examples/deepseek/review-fixes/unexpected-thinking.before-after.json)、[部分工具调用](../../examples/deepseek/review-fixes/partial-tool.before-after.json) 和 [大整数](../../examples/deepseek/review-fixes/huge-integer.before-after.json) 的前后结果及同目录原输入。

最终 **70 项测试通过，35.40 秒**，包含 54 项纯协议测试；14 场景样例再次全部通过。当前 [报告](../../examples/deepseek/windows.report.json) 绑定最终源文件摘要，先前报告另存 [审查前记录](../../examples/deepseek/windows.pre-review.report.json)。复验 protocol SHA-256 为 `9A35BD696DE33C704E8DAFB5B62B906F6E71C9E2B342B2F846DAD5D1B291FA50`，response 为 `5B0C2ADD8FA1F9B60E14615A6C392347D0A1B39AB50096742DDD64D300DD2C94`；已声明离线范围无剩余已证实审查问题。

保留的真实失败→修复记录：畸形 JSON choice 抛异常；非法 role 产生不稳定异常；截断 DONE 被当完成；流尾损坏丢失已有 usage；文本 parts 未归一；实际 OpenCode 第二轮空 reasoning 字段被错误拒绝；新场景缺实现；缺运行时先创建了状态目录；CLI 入口缺失；实际配置漂移场景缺入口。相应回归用例修复后通过，不把未失败的补充用例写成红→绿。

剩余工作包括生产可复用 HTTP transport、完整 RunnerHost/Run 状态接线、不同收费项的真实有界计量、指定账户与 secret_ref、真实模型/工具/权限/停止资格，以及用户允许现金测试后的真实对账。本轮现金 API 调用数为零；固定协议与本地账本通过不能给真实 Profile 标记 enabled 或 bounded_calls。

远端验证补记：提交 `3534f967f9742054011ed0b9d226bf50b23dd841` 的 [PR #39](https://github.com/zhouy1017/Karajan/pull/39) 已通过 [GitHub CI](https://github.com/zhouy1017/Karajan/actions/runs/33976539011)，包括 Windows/Linux 后端、前端和 `quality-gate`；同提交的 push CI 也通过。该记录补充上述本机证据，不代表真实 DeepSeek 资格通过。
