# Native Go Task CI 修复：版本化证据

固定修复实现 **`78c6dc8654099250bb0a3b7829538816d042cd93`**（N），父版本 **`825248a29c4dcdb4f432157fdf0979f26ed9c9b9`**。本目录保留历史失败、独立复验和作者测试维护收尾；它不声明当前 common R 的长路径组合 P、远端 G 或真实服务 S 已通过。root 会另行追加当前组合的验证。此归档没有执行行为测试、读取真实 key、调用 provider 或改变 Git/远端状态。

[publication-map.json](publication-map.json) 固定每份副本的原位置、轮次、SHA-256、字节数、目标及用途；[bytecheck.json](bytecheck.json) 是逐份原件/副本与 Git 对象复核结果。共 **92 份原始字节副本**：84 份文件原件（包括原 54 份历史白名单）与 8 份不可变 Git blob。另有本 README、map、bytecheck 和叶级 `.gitattributes` 四份新文件。所有 Python 输入以 `.py.txt` 保存，不被 pytest/Ruff 当正式测试发现。

## 经历与当前准确结果

Spark 额度耗尽后，Luna 完成产品修复；前两轮独立审查仍发现问题，不能用同期局部 green 抹去它们。第三轮在固定产品上关闭 NATIVE-001 至 NATIVE-004；随后 Luna 仅补正式 enum/claim 合同测试，并修正 consume 重放测试的错误预期，最后保持从 `consume_go_task` 公共入口验证只读恢复。

| 来源 | 原始结果及含义 |
|---|---|
| [历史计划](history-plan/native-evidence-plan.md) | 54 原件的历史事实、去重与缺失引用说明保持原文；其中 copy_allowed_now=false 是归档前的历史状态，本次复制由新的明确任务授权 |
| `author-round-one/` | 当轮未验收报告、源码/diff、Windows/WSL skip 与局部通过；不能代表最终源验收 |
| `independent-round-one/` | 原 001/002 red；首次第三例是缺基线 fixture 错误，修正后的 consumer red 才证明 003；静态错误原文保留 |
| `independent-round-two/` | 原三例与 claim 正控通过；两个非法 cleanup enum red 证明 004，正式维护覆盖仍缺 |
| [第三轮独立报告](independent-round-three/README.md) | 原 6 项独立 C 通过，另外 1 项正式测试因错误地要求第二次 consume 重抛而失败；它是 6 passed / 1 failed，不是全组 green，也不是新设计 6 项 |
| `author-round-three/` | 含名称带 green 但实为 fail/skip/setup error 的原 XML；重复 basetemp 期间的 SQLite 失败与 producer failure 保留；之后短目录三个 Host/native case 通过，不能替代最终 R 长路径 P |
| [最终作者记录](author-final/report.md) | 原文件名 luna-native-final-author **没有扩展名，是报告文件而非目录**；Windows 34 passed / 1 tokenizer skip，Linux 35 passed / 0 skip；随后公共 consume 最终单例 1 passed |

独立 6 项使用真实 SQLite/Host noop child 与显式 native 故障/身份替身，正确 ELF/tokenizer 只用于来源绑定；这组属于 C，不是 Reviewer 新跑的 namespace P。各轮含重叠用例，不相加声称唯一总测试数。

最终 Linux 35 项实际经过正式 consume、11 项新的正式 diagnostic 合同与23项原 intent 例；但该次测试文本的第二调用当时直接用了 `facade.reconcile`。报告后半部明确记录最终又改回 `consume_go_task` 公共重放入口并单独跑 1 项，通过。**35 项与最终 1 项的范围分开**：四份产品源码均与 N 相同，N 的最终 consume 测试 SHA 为 `64ca3965f03ea339041f7e2ba559af3b0e6355fceb92152ce4ccca70250b4f85`；不把前一次 35 项整组声称在这份最终测试字节上运行。Windows 被 skip 的 consume 不升级为成功。

最后静态原件为全 repo Ruff 通过、backend mypy 128 sources 通过、两个修改的测试文件 format check 通过。没有声称执行了全仓格式化；历史 compileall/diff-check 或成功的日志下载退出码都不是远端 CI 通过。

## 原始来源与不可变实现

8 份最终源码/正式输入均从 `git show N:path` 提取至 `implementation/`，map 同时记录 Git blob object ID、blob 内容 SHA-256 与 root `.cache/native-code-index-check.json` 的 tested working raw SHA，并逐一核对相等。这里没有用可能继续变动的工作树文件代替 N。早轮报告与 XML 仍只绑定该轮所记源码；最终测试维护不改写其 source maps 或失败内容。

原 54 历史文件 SHA 与既存白名单全部一致。原路径以 ROOT `C:/Users/Chooo/Playground/Karajan` 为基准；旧报告中 `.cache/...`、绝对 Windows/WSL路径和日志文件名是**当时的执行位置**，不是可点开本归档任意新文件的承诺。map 的 exact aliases 和 selected_report_reference_map 按完整 SHA 解释已选同字节位置；未选项保持 `not-selected-history-reference`，不补造原件或改旧报告。引用 `.py` 的原名与本归档 `.py.txt` 只有名称变化。

历史已知差异也保留：第二轮 README 的正式 test_go_task 收尾 hash 前缀 `6a32aa80` 与 review.json 的 `95d97c2a` 不同；该轮实际执行前快照 `94edba59` 单独存在，不能用任一收尾说明替代最终输入核对。PR92 head `825248a` 与当时 CI merge ref `50b4798` 不混为同源。PR103 `c80e41a` 的失败 log 只证明当时外层失败，不能将后来发现的内层原因反向冒称为 CI 已留存观测。

未复制整份 CLI JSONL、任务/prompt 指令文件、key、bootstrap、数据库、临时仓库、session/runtime 目录、tokenizer 或 bytecode。公开测试内的 PRIVATE_CUSTOMER_CASE_ACME_INTERNAL_2026 等明确是 synthetic canary，为保留 red 语义原样留下；不是客户真实内容。

## 字节与空白检查范围

所有原始副本保持原字节与换行，raw CRLF/XML/历史源码可能保留原尾空白。叶级 `.gitattributes` **仅 `* -text`** 防止 Git自动换行转换；没有改 whitespace 规则或隐藏历史记录。新写 README/JSON/gitattributes 使用 LF、无 BOM/尾空白并单独核对。此任务没有 stage，**未执行 staged whitespace gate，也不声称整份发布 diff --check 全过**；后续暂存检查应如实区分原始证据空白与产品/正式测试 CI。
