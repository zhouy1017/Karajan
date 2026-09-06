# 已知耗尽不能被 unknown 报告重新开放

2026-09-06 独立 Spec 审查发现一个 P2，保存两种公共 API 原始输入：

- `exhaustion-unknown.input.json`：记录 QUOTA_EXHAUSTED 后等待 cooldown，同窗口仅出现更新的 unknown 报告。
- `zero-unknown.input.json`：同窗口已有 remaining=0，随后出现 unknown 报告。

各自的 `*.before.json` 保留错误准入；`*.after.json` 是在相同原始输入上的独立复验，修复后均拒绝，原因是 `EXHAUSTION_REQUIRES_NEW_OBSERVATION:weekly`。输入按原始字节保存；报告中的输入/源码摘要可核对。before 报告的源码摘要是实际发现时的版本，不冒充最终实现。

仓库公共回归入口：

```text
.venv/Scripts/python.exe -m pytest tests/capacity/test_review_cases.py -q
```

两例均不创建 provider 接收端，不发送任何模型或现金请求；它们证明内部容量账的裁决。回归还验证更晚的可信数值恢复可以重新准入。完整公开 CLI 的实际回环 HTTP 发送证据位于本目录上一级报告。
