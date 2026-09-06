# Go observer 独立 Spec 审查

审查者 capacity_facts；产品作者 root。审查入口：`backend/karajan/isolation/go_probe.py::observe_go_tools`。依赖仅按公开端口读取；未改产品，也未读取密钥或请求真实 provider。

当前状态：0 项未解决发现。1 项发现已修复并独立复验关闭，最终 5 项实际 Linux 测试全部通过（43.76 秒），Ruff 通过。最终 observer SHA256：`86fc8ff126b14d03d809780f3bd27b158b6366db30a4db406647934b4b40aa3f`；journal SHA256：`2da85e23d025e5ff821dbd17869b1385ee9fde9f5ca0acaff150d94aced3852d`。完整来源与证据绑定见 `review.json`。

## GO-OBSERVER-001：缺少实际 runtime descriptor 与 grant 的绑定核验（P2）

当前 observer 预检只核 `journal.snapshot().binding == authorization.binding`。它运行实际固定 Linux OpenCode 与当前隔离代码，却不校验 grant 中 `runtime_digest` 是否对应这个实际运行来源组合。真实 native＋UDS relay＋SQLite、仅上游 HTTP 为 fixture 的原例，把 `runtime_digest` 改为 `b` 重复 64 次后仍发送两次请求并返回 `status=passed`。

必须比较完整 runtime_source 的 canonical digest。按 root 明确的 consumer 定义，runtime_source 包含 artifact、launcher/inner、relay/journal、原生设置和 OS；因此 runtime_digest **无需等于**单独的 artifact_sha256。初始审查把两摘要应相等作为依据的表述已纠正。有效发现是缺少完整来源组合的绑定校验，任意非匹配摘要仍可通过。

原输入：`test_observer.py::test_actual_artifact_cannot_pass_under_a_grant_for_a_different_runtime`。`native-before.junit.xml` 保存 1 失败、3 通过（37.65 秒）；`runtime-mismatch.before.json` 是真实执行后返回的脱敏报告。该次 observer 源 SHA256 为 `79c20d26894b15fafb1d4d57fd819e63f9a9b51bf2bd608a168268e73ccbd068`，报告内保存其他真实来源 hash。

作者新增公开 `go_runtime_source` 与 `source_digest`，在目录创建和 native 启动前核对完整来源摘要。复验保留错 `b*64` 输入，结果为固定 `GRANT_RUNTIME_SOURCE_MISMATCH`，journal request_count 保持 0。正常输入按新公开 canonical 契约构造并核对返回的 runtime_source。测试的正常绑定及拒绝形式断言作了对应契约调整；没有声称测试字节完全未变。`test-observer-before-fix.txt` 保留修复前、已增加超时控制的测试版本；初始失败 XML 与原报告未覆盖。

## 已通过的独立控制

- 实际固定 native 的 read/edit：三次本地 fake HTTP 请求，正确文件结果、read 与 edit 工具完成、native 停止、日志已撤销。观察来源为 `http_fixture`，不称真实 provider 资格。
- 实际 denied-read：两次本地 fake HTTP 请求，真实权限拒绝、文件不变。输出仅含工具状态和路径类别，不含原始 assistant 文本、synthetic credential 或 grant capability。
- HTTP 替身在实际发送后抛 ReadError：仅一次请求，持久记录为 `send_unknown`，报告失败，grant 被撤销且未重置计数。
- 在实际 native 发起固定场景后，仅替换 observer 自身的计时器触发超时；在真正 `IsolatedOpenCode.close` 公共入口读取真实 SQLite，grant 已是 revoked。`timeout-control.junit.xml` 保存 1 通过（9.12 秒）。

所有观察继续声明 `dispatch_eligible=false`、`runtime_tools_status=not_run`、`provider_remote_stop=unknown`、`billing_limit_qualification=not_run`。这些测试不授予 Profile 资格，不证明真实 provider 接受模型、现金限制或远端取消。

## 环境与复验入口

普通 sandbox 内 WSL 枚举返回 Access denied；已按授权升级运行本地 WSL 测试。首次未显式设置 artifact 路径导致四项环境跳过，保存在 `before.junit.xml`；之后明确设置已验证路径并要求 artifact 必须存在，实际执行见 `native-before.junit.xml`，没有把 skip 算通过。

测试导入固定当前 worktree 的 backend；HTTP helper 复用 `tests/isolation/test_opencode_go_composition.py` 的 `native_response`，实际产品端口、namespace、native runtime、Unix socket 与 journal 都运行。Unix socket 使用自动清理的短 Linux `/tmp/kgor-*` 路径，避免 DrvFS 不支持 socket。

```powershell
wsl.exe -d Ubuntu --cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-isolated-runtime -- /usr/bin/env KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/go-observer-review/test_observer.py -q -p no:cacheprovider -o "pythonpath=backend tests/isolation" --junitxml=.cache/go-observer-review/native-final.junit.xml
```

最终独立结果见 `native-final.junit.xml`，所有 5 项实际执行而非跳过；上述原失败和环境材料保留。
