# 取消后 Check 回执恢复：独立收尾审查

固定基线 `c58fca7d08841b44200d543ba9cadd062aead92b`，候选 `444fed2abdd32a4335a5da6320965da7ff640750`。本次由同一名独立 reviewer capacity_facts 分别核 Standards / Spec，非两名审查者，也非 Luna 产品/测试作者。仅查读差异、命令与原始证据；未运行行为测试、P、静态检查或 GitHub API。

## Standards

**0 confirmed findings。** 净差异只有 `candidate_checks.py` 与其正式测试。没有引入新的状态库、调用者权限或等待循环；原 operation 取消先提交、外部停止在锁外、完整 Host 取消绑定与后续原行回写继续使用既有端口。原 `check_runner.py` 与基线 Git bytes 相同，拒绝的自等待方案未留在最终产品。新测试通过公开 advance / consume_check / cancel / reconcile 建立原 claim；runner 与 Host 返回是明确的边界 double，未包装为真实隔离证据。

## Spec

**0 confirmed findings；本修复范围可以接受。** 已有 native claim 但 runner 尚无收据时，保留 Host 中的发布者，native cancel 仍立即调用；已有原持久 observation 即使历史实例未加载 runner，仍允许精确 Host cleanup。缺失收据、异常和停止未知没有被改成通过，下一次 reconcile 只恢复原身份，不开启第二进程。对应 #94 恢复/取消、#101 活跃旧 subject 取消与未知停止边界；不是重新验收所有父票能力。

正式两例分别覆盖迟到收据前不取消 Host、收据持久后收尾，以及历史无 runner 的收尾。原真实 Linux namespace 并发取消例和两公共 C 例在 root 保存的组合结果中为 **3 passed / 25.99s**；Windows 为 **554 passed / 3 POSIX skips / 310.92s**。XML 分别为 3/0/0 与 557/0/3，时间25.986和310.846秒，与 CLI 摘要口径区分。Ruff原输出通过、mypy原输出145源通过；这些是查验 root 证据，并非本 reviewer 重跑。

## 发布证据

13份 map 引用逐份验证发布件 length/SHA 和原文件或 Git blob bytes 完全相等。原 CI 失败保留；作者仅在 tool output 的 red/green 明示为历史陈述，未伪造 raw。145个源码记录都匹配实际受测工作树。144个也匹配候选 Git bytes；qualification.py 为 CRLF→LF，normalized bytes/AST 完全相等，root 已补 README 说明，两份 SHA 均在 review.json 保留。最初严格字节审计触发的断言不是产品测试失败。

当前必需 GitHub G、实际 dev 合并仍需后续核验。无新增官方 S；#107 与父 #95/#13/#14 剩余需求不由本修复完成。

## 固定输入

- 产品 SHA256：`7e8e2c3ef2a90090e64fabc253334e9fc2442d96e463863e49d61af773ee014e`
- 正式测试 SHA256：`7ee76501ae92d4a0cd3c2272a62b1b0cc81dc51fc297ce03f9eb26c9b57b9d22`
- `review.json` 保留 README/commands/map/XML/raw/source 摘要与完整校验结果；`reviewed.diff`/`commits.txt`固定比较范围。
