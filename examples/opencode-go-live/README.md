# Go 实测与离线回归证据

实际用户授权日期：2026-09-06。只发布诊断报告、固定合成测试和来源摘要；实际密钥、原生日志、数据库与会话正文留在本地忽略目录。

## 历史原型

`direct_models.json` 和 `direct_text.json` 是固定官方 Go 通道的真实 HTTP 200 观察，后者请求 `glm-5.3-flash` 并得到预期标记。模型目录不是可派发模型清单，不证明目录中所有模型都受当前订阅覆盖。

`prototype_edit_recorder_error.json` 保留首次原生工具测试的记录器部分失败：read/edit 和四个函数输入完成，但最后一段 SSE 的 null 工具数组触发解析异常，最后一次上游 usage 未收齐。不能把这份记录记为完整探针通过。

`prototype_edit.json` 是修复后成功重跑；`prototype_denied_read.json` 是受禁文件的真实读取尝试。后者保存了未修函数的原始输入结果，不以函数结果判定权限场景。最终入口另行要求明确的原生权限拒绝类别。

以上五份报告只把工具路径转换为文件名、移除本机 PID 并补充阶段说明。`prototype-provenance.json` 记录转换前原件 SHA-256。原件保存在本地 `.cache/opencode-go-live`，不覆盖或改写。历史报告没有最终探针的源码绑定，不能冒充最终实现的验收。

`strict-trailer-failure.report.json` 是可复用入口首次真实运行的失败原件：严格解析器误拒绝了 Go 结束标记后的计量尾帧，实际发生六次 HTTP 200 请求但没有工具执行。`sse-shape.json` 来自随后一次实际结构观察，相邻相同事件形状合并并记录重复数；`sse-trailer.json` 来自另一次小请求，确认 cost 的字符串类型（该采集器未保留字符串值，null 不是实际 cost）。两份结构观察均不保存文本或推理内容，用来固定兼容边界。

## 最终入口与测试

完整命令、版本、请求和能力边界见 [实现记录](../../docs/implementation/m2-opencode-go-live.md)。最终 `edit.report.json` 与 `denied_read.report.json` 分别记录正式入口的两种真实场景，报告内的 `source_sha256` 绑定实际执行源码。结果始终不启用 Profile。

| 检查 | 实际结果 | 记录 |
| --- | --- | --- |
| Go + OpenCode edit | 三次 HTTP 200；read/edit 完成；四个函数输入通过 | `edit.report.json` |
| Go + OpenCode denied_read | 两次 HTTP 200；原生规则拒绝；文件不变；标记未上行 | `denied_read.report.json` |
| Windows 全 OpenCode 回归 | 97 项通过 | `windows.junit.xml` |
| WSL2 Go 回归 | 82 项通过 | `wsl.junit.xml` |
| 独立 Spec 公共入口 | 29 项合成离线检查 | `spec/README.md` |
| 独立 Standards | 八个合成原例复验 | `standards/standards-review.md` |

局部 `.gitattributes` 保留 JSON/XML 原始字节和行尾，避免 Git 改写既有证据。`evidence-before`、`relay-before` 保存首次缺接口失败；`relay-cleanup-before` 和 `relay-trailer-before` 保存修复前的具体边界失败。最终全部 Go 作者回归收录在 Windows/WSL 记录中。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/adapters/opencode -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy backend/karajan
```

这些常规命令只运行本地 HTTP/MockTransport、协议与判断回归；不读取 Go key，也不调用真实 provider。真实测试必须额外显式运行带 `--live` 的诊断命令，输出到新的 `.cache` 目录。测试结果文件中的主机、临时目录和时间是该次运行记录，不能作为新的运行结果。

诊断的 `passed` 表示已观察到指定模型和工具行为且记录、清理检查通过。OS 隔离、供应商远端停止、账户窗口、现金计费上界、持续调度与 Run/Rulebook/容量准入仍分别保留未验证状态，未关闭整个 #21。
