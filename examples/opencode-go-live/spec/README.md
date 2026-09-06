# OpenCode Go probe 独立 Spec 验收

结论：29 项公共入口测试通过，1 项 P2 发现已修复，无未解决发现。源码在最终测试前后哈希一致；完整绑定见 [report.json](report.json)。

已发布测试完成 import 与换行格式整理，其 AST 与保留在 `.cache/go-probe-spec` 的原始输入一致。格式整理后从本目录复跑仍为 29 项通过，全仓 `ruff check .` 通过；原红灯证据未改动。当前测试 SHA-256 为 `b6358b235dc17b18824ca2aee15645ac42d85c92e33893ee8c6ed2b9e4fa7c3f`，最终 JUnit 与其哈希已更新。

本目录全部是 **synthetic 离线验收**。测试仅调用公开 `GoLiveProbe.run` 和 `main`，使用自建假凭据及合成 Server/Relay，禁止 socket 连接并替换进程边界。没有读取真实凭据、启动真实 OpenCode 或调用 Go；也没有验证 relay 的真实 HTTP/SSE 实现。真实运行结果由目录外的实际观测另行提供。

测试保留了真实配置构造、场景编排、证据提取、文件判定、清理分支、扫描和报告路径。其通过表示这些公共边界在明确合成输入下行为符合要求，不构成 Profile 资格。

## 覆盖结果

- `edit` 必须有 read/edit 工具完成、目标文件变化及四项功能检查通过。`denied_read` 必须得到原生权限规则拒绝且文件不变，不要求修复原有 clamp。
- 无工具、错误路径、额外文件、一般错误冒充权限拒绝、模型漂移、最终 assistant 未完成、session/provider 错误及空 receipts 均不能通过。
- 版本、配置或工作区不符时不提交 prompt；超时及 abort 异常仍完成两层清理，清理不确定或失败时不通过。
- 首次 poll 发现 relay 失败 receipt 时只发送一次 abort，不 sleep、不等待 native 完成，也不将其记录为 timeout。
- 缺少 `--live`、凭据文件缺失及已有输出目录在对应读取/启动边界前拒绝，旧证据不被覆盖。
- 假凭据直接泄漏、跨 64 KiB 扫描边界泄漏及文件不可读均不能通过。每次完整运行都断言零 socket 连接，以及 Profile/dispatch 未启用。

## 已修复发现 GO-SPEC-01

受限 fixture 判定器原先没有拒绝 Python 泛型 `FunctionDef.type_params`。原输入包含 `def clamp[T: unexpected_callable()](value, low, high):`，返回表达式为允许的 min/max；公共 probe 错误给出 `passed`。没有观察到 `unexpected_callable` 被执行，问题是受限语法误接纳及诊断假阳性。

作者补充 type_params 拒绝后，同一输入由公共 probe 返回 `failed` 和 `FIXTURE_BEHAVIOR_FAILED`，已包含在最终 29 项通过结果中。

- 原输入：[type-parameter.input.py.txt](type-parameter.input.py.txt)
- 修前 1 项失败：[type-parameter.before.junit.xml](type-parameter.before.junit.xml)
- 修前实际生成的**合成**报告：[synthetic-type-parameter.before.report.json](synthetic-type-parameter.before.report.json)。报告内的产品 `scope` 字段沿用 live 命名，不能将其视为真实 Go 观测。
- 最终 29 项通过：[final.junit.xml](final.junit.xml)
- 独立测试输入：[test_public_probe.py](test_public_probe.py)

## 复验

在仓库根目录的 PowerShell 中运行；仅创建合成测试文件，仍不会调用 provider：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'backend')
.\.venv\Scripts\python.exe -m pytest examples/opencode-go-live/spec/test_public_probe.py -q
```

最终源码绑定为 `go_live.py` 3b35606c…、`go_evidence.py` 95a45cbd…、`go_relay.py` ca2aa659…；完整 SHA-256 及其他依赖、证据文件哈希均保留在 report.json。对 relay 的绑定只表示本次代码版本，不表示合成测试执行了真实 relay 协议。
