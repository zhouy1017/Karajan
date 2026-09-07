# GoRelay 响应回执发布修复

用户于 2026-09-06 因 Copilot 额度耗尽，将 CI 修复改派给本地
`gpt-5.3-codex-spark`。本次实际使用该模型，经独立复核后形成代码提交
`64e1e5b79c7d542e6c585b4c20cfb909d47a66ba`，对应
[PR #88](https://github.com/zhouy1017/Karajan/pull/88) 的成功响应时序问题。

旧实现可能在客户端收到完整 HTTP 200 后，仍公开 `protocol_passed=false`。
修复先发布已验证的协议和无内容的 nullable-name 计数，再发送正文；正文实际写成功后
才标记 `relay_completed=true`。Journal 的最终提交仍可能晚于客户端收完正文。
写入失败保留上游观察和调用占用，同时明确记录失败，不据此退款或声称远端停止。

| 验证 | 实际结果 |
|---|---|
| Windows 四套相关回归 | 99 passed，0 skipped |
| WSL 同四套回归 | 99 passed，0 skipped |
| 独立 Windows 边界与相关用例 | 29 passed，0 skipped |
| 同一新回归绑定旧 `cd45797` | 在 `protocol_passed` 断言准确失败（负对照） |
| Ruff / 生产模块 mypy | passed |

两套回归均加载已有固定 tokenizer，未下载依赖或调用真实 provider。WSL 留存一个
pytest 缓存目录权限警告，99 项检查仍实际执行；它不影响断言结果。独立 TCP reset
使用真实本机 socket 与 SQLite，上游回答和凭据是合成 fixture。新 PR head 的 GitHub
CI 单独核对，不沿用旧 head 的绿色状态，也不把这些结果升级为官方模型资格。

[最终独立审查](independent/final/README.md) 保留了精确命令、来源摘要、正负对照及限制。
初版测试的时序问题和原始失败也保留；修正后的测试使用明确写入暂停与 handler 完成
屏障，不把客户端返回当成 Journal 完成。历史原生/官方资格报告未改写，后续执行必须
按当前组合源码重新验证。

[发布清单](evidence.json) 记录原始路径、发布路径、字节数及 SHA-256，复制不改内容。
独立测试源以 `.py.txt` 留档，防止故意失败的历史负对照被当成普通 CI 用例；复现时应
复制到临时目录并还原 `.py` 后缀。作者正式回归位于 `tests/adapters/opencode/`，自动
纳入原有 Windows/Linux pytest 门。CLI 会话日志、账号材料和真实密钥均未发布。
