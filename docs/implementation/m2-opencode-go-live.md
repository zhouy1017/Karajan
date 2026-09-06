# OpenCode Go：实际模型与原生工具诊断

推进 [M2-05 / #21](https://github.com/zhouy1017/Karajan/issues/21)。2026-09-06 用户提供本地密钥，授权 Go 实测并说明额度由 provider 限制。其他服务的现金调用仍暂停。本切片不登记或启用 Execution Profile。

## 已取得的实际结果

固定官方 Go 通道与 `glm-5.3-flash` 的直接文本调用成功。固定 OpenCode **1.18.29** 通过可信本地中继完成以下实际任务：

- 读取有错误的 `fixture.py`，调用原生 edit 修正 clamp，再由探针解释受限表达式检查四个已知输入。
- 尝试读取 `blocked.txt`，由实际生效的原生权限规则拒绝；文件保持不变，随机测试标记未进入上游请求。

这是固定版本、模型和两种场景的观测。模型身份指官方响应中的模型字段，不是对供应商内部物理模型的独立认证。原型第一次 edit 的工具和函数检查成功，但最后一段 SSE 的 `tool_calls: null` 触发记录器错误；保留该部分失败记录，修复后重新执行成功。最终可复用入口的证据与原型分开保存，见 [证据说明](../../examples/opencode-go-live/README.md)。

最终入口的 edit 实测为三次请求、read/edit 完成和四项函数检查通过；denied_read 为两次请求、明确的 `permission_denied_by_rule`、两个文件不变和测试标记未上行。两份报告各扫描 28 个本地文件，无未完成扫描或密钥泄漏，绑定相同五个执行源码 SHA-256。

## 可重复入口

按仓库开发环境安装依赖（`uv sync --extra dev`，包含已锁定的 httpx），运行 `npm ci --prefix runtimes/opencode --no-audit --no-fund` 安装已锁定的原生执行器。普通导入、帮助和 pytest 不读取密钥或启动真实调用。

```powershell
.\.venv\Scripts\python.exe -m karajan.adapters.opencode.go_live --live --runtime runtimes/opencode/node_modules/opencode-ai/bin/opencode.exe --credential-file opencodego.key.txt --directory .cache/go-edit-example --scenario edit
.\.venv\Scripts\python.exe -m karajan.adapters.opencode.go_live --live --runtime runtimes/opencode/node_modules/opencode-ai/bin/opencode.exe --credential-file opencodego.key.txt --directory .cache/go-denied-example --scenario denied_read
```

示例使用本机实际存在的 `.key.txt` 文件名。密钥路径必须显式提供，每次选择尚不存在的输出目录。入口要求 `--live`；无此开关，在读取任何凭据或启动执行器前退出。目录中 `report.json` 是脱敏结果，原生日志和运行数据库只留在忽略目录中，不应上传。

退出 0 只表示该诊断通过；失败或证据不完整退出 1。所有结果保持 `profile_enabled=false`、`dispatch_eligible=false`。edit 要求实际 read/edit、文件变化和功能结果同时成立；denied_read 要求原生权限规则拒绝、无上行标记、两个文件均保持原样。普通路径错误不能充当权限拒绝，未修函数也不是 denied_read 的失败条件。

## 请求与凭据路径

可信中继独占真实密钥。OpenCode 只获得随机本地访问凭证，管理接口另有独立认证；子进程使用隔离配置目录，关闭自动更新、外部技能、默认插件、项目配置、LSP、MCP、额外 Agent 与自动压缩，固定模型和精确文件权限。启动后读取生效配置和实际 Git 工作根目录，核对后才提交提示词。

中继只接受固定模型的 Chat Completions 请求，默认上游固定为 `https://opencode.ai/zen/go/v1/chat/completions`，不继承代理、不跟随重定向。它检查授权、请求大小和 SSE 完整性，保留数字用量、HTTP 状态、模型/工具类别和请求序号；不保留提示词、工具内容、认证头或推理文本。为防止异常上游响应被原生执行器提前使用，最多缓存 1 MiB 的 SSE，验证后转交；本诊断不提供逐 token 实时中继。

每场景最多接收六次合格请求，每次输出上限 4096 是探针的循环终止控制，不能据此推导现金金额上限。native 内部重试若发生也必须经过同一入口，不自行切换模型或服务。HTTP 代理关闭只是一项进程配置，不等于操作系统出口隔离。

实际 Go 流在 `[DONE]` 后还发送一个 `choices=[]`、`cost` 为字符串的计量尾帧。首次严格入口把它当作结束后的异常数据，六次上游 HTTP 200 均被拒绝，未产生工具操作；该失败报告保留。通过单独的实际流结构观察定位后，仅兼容这一帧的精确字段、顺序及有限非负十进制值，仍拒绝结束后的内容、工具或重复尾帧。计量值保留来源与未知单位，不解释为余额。观察循环现在发现中继失败即请求中止，避免原生执行器继续重复同一协议错误。

## 验证与剩余接线

GitHub Windows/Linux Python job 执行新增离线测试，并新增 `Check Go diagnostic boundaries without provider credentials` 步骤，把独立公共入口与凭据边界回归也纳入必需检查。覆盖 HTTP 入口、SSE、失败判定和显式 live 门禁；不需要 GitHub secret，不自动运行此真实 CLI。现有 `quality-gate` 继续汇总后端、前端和测试结果。

提交前 Windows 全 OpenCode 检查 97 项通过，WSL2 新 Go 检查 82 项通过，全仓 Ruff 和 94 个后端源码的 mypy 通过。独立 Spec 29 项覆盖公共入口；Standards 八个原始输入覆盖凭据回显与控制例。凭据回显、Python 3.12 额外类型参数和未完成连接清理的失败输入均保留并复验；真实计量尾帧的首次拒绝与修复后成功也分别保存。

当前诊断不代表完整 M2-05 验收：尚未证明所有进程/网络出口隔离、远端推理停止、服务端计费窗口和余额、实际角色资格，也未接入持久 Attempt、Run 授权、Rulebook 路由与容量账本。停止后的凭据扫描只覆盖该次本地输出目录；进程清理仅报告原生 server 进程的退出，不能代替整棵进程树或供应商停止证明。

接口事实来自 2026-09-06 核对的 [OpenCode Go 官方文档](https://opencode.ai/docs/go/) 和锁定的 OpenCode 1.18.29。Go 的 provider ID、官方 endpoint 与会话请求头按该来源处理；不从其他 provider 的适配结果继承资格。
