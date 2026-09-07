# Native / Relay common 修复整合证据（2026-09-07）

这份归档记录已组合的本地C/P修复与独立审查。代码提交为 `aa384798fb49797cdc5b140130dae93ee8f651b4`；随后仅合入Relay档案的HEAD为 `ef440f4e7902bc1d3a262afa9112154cd9690a4a`。10个关键产品/测试与两个提交的Git blob逐字节相同，详见[提交绑定](commit-binding.json)。归档自身将由root另行提交；此目录不宣称当前远端CI已经通过或任何官方S/角色资格完成。

| 实际证据 | 结果 | 适用范围 |
|---|---|---|
| root common Linux contracts | 284 passed，125.603s | 组合后的适用协议/Journal/计量/意图/诊断合同；来源冻结。 |
| root common Linux native长路径 | 3 passed，153.146s | 真实Host/native/namespace/本地HTTPfixture；未调用官方provider。两条实际启动operation的旧socket拼接路径均127 UTF-8 bytes >107，丢grant回复例没有新namespace。 |
| root common Windows旧组 | 250 passed +1 failed，62.390s | 原失败是Linux fingerprint前置在Windows不可用，原XML和stdout保留，没有覆盖为绿色。 |
| 后续marker精确target | Windows1 skip；Linux1 passed，31.279s | 只给真实Linux fingerprint用例加平台标记，正文与重放断言不变；不是重新跑过完整Windows全绿。 |
| 独立全库静态 | Ruff通过；mypy backend128源通过 | 由capacity_facts实际运行，原命令/日志保留。 |

## 独立结论

[common marker/来源审查](independent/README.md)接受平台标记：测试通过approved_fixture读取真实Linux runtime descriptor及system executable hashes，不能用假fingerprint让Windows执行。非Linux只跳过这一条；普通诊断store的11项正式合同在Windows旧组内全部passed，没有被跳过。原NATIVE-001–004已通过此前独立复验，本归档不重复旧大历史。

[Relay合并语义审查](relay-review/native-relay-merge-review.md)保留独立code-only结论，来源与本提交绑定。它不是另跑一次nativeP；共同root原始运行承担组合证据。

原common独立报告撰写时Git仍在合并，因此报告内保留当时78c6dc/046上下文；随后源字节未变，commit-binding.json把最终aa/ef提交与全部被审代码对应起来，不改写历史报告。

## 文件与复现

- `root/linux`、`root/win32`：完整该轮命令、原stdout/XML、退出码、前后源码摘要；root/run.py.txt是原运行脚本存档。
- `independent`：原Standards/Spec/marker结论、逐源摘要、实际Ruff/mypy日志及固定输入快照。
- `marker`：最终正式test原字节输入，以及作者target双平台原stdout/XML。
- `relay-review`：另一位独立reviewer对组合Relay保留语义的原报告/JSON。
- `publication-map.json`：来源→归档路径、长度/SHA与执行命令位置；所有拷贝逐字节核验。`.gitattributes`用raw `-text`防止历史换行改写；脚本/测试原输入以`.py.txt`归档，不加入pytest递归发现。

要复现请使用对应代码提交、正常依赖环境与root/*/commands.json的pytest输入，明确PYTHONPATH指本worktree/backend。Linux需要已固定官方OpenCode ELF和离线tokenizer；只有HTTPfixture，无provider/key权限。使用新输出目录，不覆盖已保存XML；原run.py.txt的路径/拒绝重用目录语义保持原样，不能直接改名后向本归档写入。

未复制数据库、缓存bytecode、实际native工作区、凭据或Luna CLI会话日志。旧go-native-ci-repair-20260907和go-relay-ci-repair-20260907档案未修改；本目录只收本次common增量。合并、发布及远端G由root继续处理。
