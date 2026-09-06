# 固定 Go CLI 独立负向审查

范围：root 编写的 `examples/opencode-go-isolated/run_live.py`，仅只读/负向审查；未改产品。测试使用自建 synthetic credential 文件与公开 observer 边界的异常/失败替身，没有读取真实凭据或发起 provider 请求。最终结果：0 未解决发现，2 项已关闭；独立固定 4 项全过（0.45 秒），Ruff 通过。

最终 CLI SHA256：`f1816fd9ef8a28d122dfbb20c26097dd3f620b4bebc50c6b57ebc32d05a81715`。独立测试 SHA256：`34d8e4082612e9482ce8ee1d07f7866e3a2b753589598b6043f335199492b8ac`；其他来源/证据绑定见 `cli-review.json`。

## GO-CLI-001：观察器预检异常后，已创建的 grant 留在 active（P2，已关闭）

CLI 先持久保存 start.json 和 grant，随后调用 observer。若 observer 在它自己的 finally 之前抛错（例如 runtime source 已变，预检拒绝），旧 CLI 只输出 failed 并返回；已有许可未撤销。独立 `test_cli.py::test_observer_exception_revokes_the_persisted_grant_without_echoing_secret` 使用真实 SQLite、公开 observer 端口注入异常，实际观察到 active。

`cli-before.junit.xml` 保存 1 失败、2 通过（0.49 秒），起点 CLI SHA256：`850facab10f1ce0d826363ecb43198f864ca05e974dbce2c23a565692678a8ff`。root 在 create_grant 与 observer 调用外新增 finally 兜底幂等 revoke；独立原例 `cli-001-after.junit.xml` 通过（0.21 秒），对应 CLI SHA256：`84294628c99d299f2a5667affc0dacc1c1d547c957f8f078be729ec0e8b9c2f6`。撤销失败仍输出 failed，不声称已经停止。

## GO-CLI-002：stdout 摘要拼入未扫描的凭据路径（P2，已关闭）

CLI 扫描 report 正文后才构造 stdout 摘要，摘要中的绝对 report 路径未做同样检查。独立自建 synthetic credential 恰位于输出目录名的例子，原样将该 credential 通过 `report` 字段输出。此处没有读取真实 key；目的是验证文档中“调用者路径也可能含秘密”的既定输出边界。

原例 `test_cli.py::test_stdout_summary_does_not_echo_a_credential_embedded_in_the_output_path` 实际失败，见 `cli-path-before.junit.xml`（0.24 秒）。root 将 stdout report 字段改为固定相对名 `report.json`，并在 start.json 写入之前增加 secret 检查，防止运行来源路径中的秘密先落盘。独立最终 `cli-final.junit.xml` 四项全过；未用作者自己的复跑代替独立结果。

## 通过的控制和边界

- 真正子进程未传 `--live`：返回明确 not_run，没有读取缺失 runtime/credential，也没有创建输出目录或其他文件，stderr 为空。
- 旧 directory 在 source/credential 读取前拒绝，已有 start.json 身份不改变，没有创建新 journal。
- 观察器异常文字中的 synthetic secret 不进入 stdout、start.json 或 SQLite，输出仅异常类型；start.json 明确 registered_profile=false。
- CLI 本身不写 ProjectRegistry，不登记或启用 Profile，使用固定 6 次 grant 与完整 runtime_source canonical digest。没有自动重试/恢复旧目录的分支。

固定矩阵为 4 项；修复后只复验这一矩阵并记录最终源 hash，没有扩大无依据的测试。命令为在本隔离 worktree 执行 `C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe -m pytest .cache/go-observer-review/test_cli.py -q --junitxml=.cache/go-observer-review/cli-final.junit.xml`。初始三项 failure XML 和独立路径 failure XML 原样保留。
