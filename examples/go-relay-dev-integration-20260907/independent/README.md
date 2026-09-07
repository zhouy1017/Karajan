# Relay / dev 合成的有界独立审查

结论：**Standards 0 个确认问题；Spec 0 个确认问题。** 本次仅读取合成代码、提交及现有日志，没有重新执行测试、修改 Git/产品或调用 provider。测试结果的执行者是 root/Luna；此审查独立核对其实际输入、XML 和来源，不将别人的执行改称本次独立运行。

读取时合并已提交为 `046045f2d9573c3cae6375c38c8de2f6b4190d70`，双父为 `9e092f868db3cdcf6c215de08f3a1a1eacd833ec` 和 `2e587d1773c514361689e13ebbd16ba62f1cd219`。审查绑定 Relay 的 commit/index/raw 相同字节：

- SHA-256：`b79f1e08afa5aa0931e4b1056dbce7f674014d04606ce64a9d3cd48ced723881`
- Git blob：`c5cfae045c22d8aa0534346d4e654e7ac9dcfd04`

## Standards

合成复用已有深模块，没有新增调度、授权或通用跳过开关。`.gitattributes` 包含两父的全部有效规则，保留两侧历史证据字节。相对 dev，backend 变化仅 `go_relay.py`；Task 基础代码来自已有 dev，不包含 #90 entry/Collector、Checks 或 Reviewer。

Luna 静态日志实际记录 Ruff `All checks passed!`、mypy `Success: no issues found in 122 source files`。这是作者静态结果，不是本 reviewer 新执行；日志和来源记录的字节哈希见 `review.json`。

## Spec

逐 hunk 核对 `_handle`，并用 AST 对比两父：7 个 drain/request/recovery/withdraw/persist/error 方法与 9e 一致，`_guard_send` 与 dev 一致。

- 拒绝体按有效 Content-Length 的剩余字节读取，保留 0.5 秒 monotonic 总期限；无有效 framing 不推测 drain，已读 body 不再多读。
- 成功 protocol receipt 在响应发布前持久；`relay_completed` 在 body 写完后置位，异常清零两个标志。
- 两个 begin 异常分支都保留原 `call_id`；只读恢复已提交 Journal 并撤销精确 grant，不产生第二次发送权。
- dev 的 send_guard/ExitStack 完整保留：业务 guard 覆盖 begin 至 HTTP context 进入，condition 不跨网络等待，响应 body 读取不持续持有业务锁。

## 实际补验证据核对

| 执行记录 | XML 实际结果 | 此次独立核对 |
| --- | --- | --- |
| root Windows | 205 passed / 0 skipped / 0 failed | 229 项来源 before=after，且当前文件逐项匹配。 |
| root Linux | 232 passed / 0 skipped / 0 failed | 同上；输入包括原基础 native producer、send_guard、UDS 和实际 tokenizer/source 补验。 |
| root Windows 原反例组合 | 13 passed / 0 skipped / 0 failed | 3 publication + 原 6 拒绝/恢复 + 4 framing；4 份测试副本与原件/记录 hash 全部一致；Relay before=after=b79。 |

XML、stdout、command/result/source-map 均已读取并计算 SHA；未凭摘要推定。Linux 使用实际 namespace/OpenCode 和本地 HTTP fixture，仅证明其 C/P 范围。Luna 早期 145 pass / 60 skip 的 stdout 明确原因为 tokenizer 未配置；该历史保持原样，root 配置后 205/0skip 才覆盖这些测试，不把跳过算作通过。

结构化核对、证据路径和哈希见 `review.json`。本次只绑定上述 Relay 字节及双父集成，不宣称仍可能追加文档的整个工作树已冻结。**旧 9e 绿色 CI 不算新合成 G**；本审查未查询或宣称 046 的远端 CI 成功，也没有官方 S 或完整 #90 交付结论。
