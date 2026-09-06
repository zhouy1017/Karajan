# 固定 Go 隔离实测与回归证据

2026-09-06 使用用户明确授权的 Go key，执行固定 Linux OpenCode 1.18.29 的两个场景。
最终运行绑定实际 ELF、完整运行描述符和持久 grant，工作区仅映射 `fixture.py`。

| 场景 | 真实 Go 请求 | 结果 |
|---|---:|---|
| [edit](edit.report.json) | 3，全部 HTTP 200 | 原生 read/edit 完成，四个 clamp 功能检查通过 |
| [denied_read](denied_read.report.json) | 2，全部 HTTP 200 | 原生读取被拒绝，host 文件不变 |

两个 grant 的 journal 均已撤销、调用数保持原值，每个已发送调用都有持久 receipt。
两次本地停止均 confirmed；provider 远端停止仍 unknown。报告不含真实 key、原始请求/响应文本或临时 capability。
`edit.start.json`、`denied_read.start.json` 是各次运行在发送前保存的身份，均明确不是已登记 Profile。
可复现实测入口是 [run_live.py](run_live.py)，参数及范围见[实现文档](../../docs/implementation/m2-opencode-go-isolated.md)。

## 离线检查

- `wsl-regression.xml`：最终 Go journal/relay/控制入口与实际 native 组合，137 项通过。
- `independent-export-wsl.xml`：发布目录中的四组独立回归，38 项在实际 Linux 上通过。
- `windows-regression.xml`：OpenCode 与隔离模块回归，162 项通过、3 个子测试通过；Linux 专用项明确跳过。
- `namespace-author/`：最终单文件投影的 14 项真实 Linux 作者测试及 8 个子测试，保留修复历史。
- `journal-author/`：到期收敛的作者 red→green 与原作者/独立用例合跑记录。
- `observer-grant-before.xml` / `observer-grant-after.xml`：旧许可不能通过换目录重启固定诊断。

独立用例按原目录深度发布，便于保持测试字节和证据 hash：

- [Journal 独立审查](../opencode-go-isolated-journal/report.md)
- [Relay 独立审查](../opencode-go-isolated-relay/review.md)
- [Observer 独立 Spec 审查](../opencode-go-isolated-observer/report.md)
- [CLI 独立审查](../opencode-go-isolated-observer/cli-review.md)
- [Namespace 独立 Standards 审查](../opencode-go-isolated-namespace/review.md)

审查文中的 `.cache/...` 是当时的执行位置；同名报告、测试和 JUnit 在上述目录保留。
namespace 审查的历史测试源码保留原始字节并加 `.txt` 后缀，避免旧版失败用例参加当前 CI。
原始失败与环境跳过不会被后续 green 覆盖。`history/` 的两次早期真实观测分别来自绑定校验和单文件投影前，
不能用它们代替最终报告。早期真实请求总计另有 5 次；本目录最终实测总计 5 次。

这些材料不授予 `runtime_tools` 资格或打开 dispatch，也不证明计量窗口、现金限制、任意 Task 路径、
Reviewer/Commander 或 Collector 已验收。下一步持久消费接口见[资格接线安排](../../docs/planning/go-runtime-qualification-next.md)。
