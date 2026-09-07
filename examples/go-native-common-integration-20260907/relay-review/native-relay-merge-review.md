# Native 当前合成 Relay：有界只读审查

**0 个确认问题。** 本次只审 `go_relay.py` 的合成保留性，没有运行测试或修改产品/Git。

初次读取时 native HEAD 为 `78c6dc8654099250bb0a3b7829538816d042cd93`，MERGE_HEAD 为 `046045f2d9573c3cae6375c38c8de2f6b4190d70`。收尾时合并已经提交，HEAD 为 `ef440f4e7902bc1d3a262afa9112154cd9690a4a`，其双父为 `aa384798fb49797cdc5b140130dae93ee8f651b4` 和 `401038d9105731c914d722cf3ad5998f94a875eb`。Relay raw/index 字节一致，整个读取期间没有变化：

- SHA-256 `c45f2784bff518407fc90c3f61d9adf0a92b69f701b77dde1aef86e6b0b5d2cd`
- Git blob `3e8b388e8541b5c546311442801f48beb236782c`

逐 hunk 与 AST 核对：

1. `GoRelay` 方法集合与 046 相同，唯一不同方法是 `start`。
2. `_handle`、`_guard_send`、drain/request/recovery/withdraw/persist/error 九个方法全部与 046 AST 相同。合法声明 body 的剩余边界、0.5 秒总 drain 期限、成功响应前 protocol receipt 发布、写失败标志清零、原 call_id 恢复，以及 send_guard/ExitStack 覆盖 begin→实际 HTTP 进入的边界均保留。
3. `start` 与 78 AST 完全相同。保留 Linux 平台要求、父目录 lstat 的链接/非目录拒绝、按 `os.fsencode` 计算的 107-byte 路径上限，以及已存在/链接 socket 拒绝；这些检查在服务器创建前发生。
4. 对 046 的完整 Relay diff 仅为 `os` import、UDS 上限常量及上述 78 的启动预检，没有用旧整文件覆盖新修复。

初次读取时 `.gitattributes` 仍有 merge 冲突、Luna 正修改一项正式测试，随后合并在审查期间完成。此结论只绑定上述 Relay 字节；本 reviewer 没有修改合并或参与测试修复，也没有重新核对作者最终跨平台测试。因此不宣称整个 native 组合已冻结，不将 046 的旧补验或任何旧 CI 转为这份 native 来源的新 C/P/G/S。

完整读取时间、两个参考源码摘要和 AST 结果见 `native-relay-merge-review.json`。
