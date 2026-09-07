# Native CI 修复：历史证据归档计划

状态：**仅清单，等待最终作者与独立验证冻结，尚未复制或发布。**

机器白名单：[native-evidence-whitelist.json](native-evidence-whitelist.json)。共 **54 份原始文件，529,761 bytes**，另有 5 组已核对逐字节相同的引用映射。所有文件均以 ROOT `C:/Users/Chooo/Playground/Karajan` 为相对路径基准，逐项记录 SHA-256、字节数、来源轮次、用途及建议归档名；没有通配符复制规则。45 份可与原 manifest/review/freeze 中已有摘要直接核对，其余报告/清单本身以本次读取的完整 SHA 固定。全部在清单生成前再次读取，摘要保持一致。

## 选取范围

| 建议目录 | 原来源 | 数量 | 保留重点 |
|---|---|---:|---|
| `author-round-one/` | `.cache/ci-spark-task-native/.cache/luna-native-round-one/` | 11 | 原未验收 manifest、作者报告、diff、4 份源码/正式测试快照，以及 Windows/初次 WSL 的 skip、定向通过和该轮最后 3 native 通过 stdout |
| `independent-round-one/` | 同 worktree 的 `.cache/native-resume-independent/` | 13 | 三个 P2 与静态失败的独立结论、source map、公共输入、初始错误 fixture、各类 XML、Ruff/mypy 原输出、consumer/intent/固定 child 源码 |
| `independent-round-two/` | 独立目录内 `round-two/` | 13 | 原三项 scoped green、claim green、新 enum 两例 red、对应公共输入与执行时源码/正式测试、静态输出和 review |
| `pr92-pathname-diagnosis/` | ROOT `.cache/ci-pr92-independent/` | 12 | 固定 head 的路径诊断、107/108 边界、126-byte producer 结果、原用例失败复现、输入/命令、来源摘要及脱敏 operation 摘要 |
| `pr103-failure/` | ROOT `.cache/ci-resume-20260907/` | 5 | 历史 PR snapshot、两次失败 run 的元数据、原 log 获取摘要及一份 PR-event Ubuntu 原始失败 log |

这不是整目录打包：未选作者早一次重复 native stdout、单 helper 复跑、重复源码/输入、第二份同类 CI 全 log、CI 聚合状态/活动记录/辅助脚本。原报告引用到未选文件的路径保留历史含义，不补造缺失文件。

## 失败与通过的时间链

1. **PR92 固定来源诊断，未修复时。** head 为 `825248a29c4dcdb4f432157fdf0979f26ed9c9b9`；原 CI 测试的 merge ref 是 `50b479897746aeeafd4ada0a03e03ad48925b9c6`，不得混为同一 revision。独立 5 项诊断 `uds.junit.xml` 全通过，其中包含预期拒绝的路径；这不表示 5 次 native 成功。107-byte pathname 正控可用，108-byte 拒绝；126-byte public producer 在 native 启动前失败、撤销原 grant、零发送。未改的两项原 integration test 在相同长度临时根下确实失败，`original-long-path.junit.xml` 为 2 failures/72.133s。
2. **PR103 的独立远端失败记录。** 历史 head `c80e41abfdc13b13034f6c82ea3fe47eb71a3b72` 的 push run `34050183894` 与 PR run `34050185708` 都在 Ubuntu pytest 步骤失败，quality-gate 随之失败，后续步骤 skipped。保留两次 run 的全部已保存元数据，只保留 PR-event job `101532317939` 的一份完整 log；它在第 967 行列出 normal 的 `TASK_STOPPED_CAPTURE_REQUIRED`，第 972 行列出 cancel-after-send 的 `0 == 1`。这两处外层断言与 PR92 复现相似，但 CI 未保存的内层错误不能从后续推断冒称已实际观测。
3. **Luna 第一轮局部 green，整体未验收。** Windows `pytest-01.log` 是 7 passed/23 skipped；初次 WSL `pytest-02.log` 是 14 passed/16 skipped，固定 runtime 路径未到位；不能当完整 Linux 测试通过。`pytest-04.log` 是 4 passed/27 deselected；该轮最后 `pytest-06.log` 的真实 Host/native 三例通过 155.69s。仍须并列下一项独立失败，不升级为整个修复已完成。
4. **独立第一轮 red。** `first.xml` 三例 skipped，原因是审查 harness 未提供 runtime；不是产品执行。`semantic-before.xml` 虽记录 3 failures，但只有前两项是 NATIVE-001/002；第三项当时缺 Git baseline，产品正确拒绝，是 fixture 错误。修正 fixture 后 `consumer-before.xml` 的 1 failure 才证明 NATIVE-003。`test-first-baseline-missing.py.txt` 与最终三例输入都保留。Ruff 及 Windows backend mypy 的失败原输出单列为 NATIVE-STATIC；作者 compileall/diff check 不能替代必需静态门禁。
5. **独立第二轮部分关闭，仍有新 red。** 原三例输入保持相同 SHA，`original-three.xml` 3 passed/34.704s，记录 NATIVE-001/002/003 在该轮源码上的关闭。`claim-windows.xml` 1 passed/1.063s；报告所述 2.17s 是另一计时值，保留原值不强行改齐。`cleanup-enum-before.xml` 2 failures/2.126s，证明 NATIVE-004；同时报告正式维护回归缺口 NATIVE-TEST。此轮是实际 SQLite/控制器流程配明确 native port double 的 C，不是审查者新跑的 OpenCode namespace P。

上述都是固定历史来源。作者正在写的 round-two/round-three 以及最终复验，尚未加入清单；本计划不读取它们，也不宣称最新修复状态。

## 精确去重与源记录差异

白名单 JSON 的 `deduplicated_exact_bytes` 保存五组完整 SHA 映射：

- 独立第一轮 relay/producer 快照分别复用已选作者第一轮相同源码快照。
- 独立第二轮 relay 和正式 Unix 测试快照分别复用相同的作者第一轮快照。
- 独立第二轮原三例 input 复用独立第一轮相同字节的最终 input。

未来顶层索引应以此解释原相对路径，不改写旧 `review.json` 使其看似指向新路径，也不创建伪造原件。独立第二轮新的 producer/intent/consumer 和执行时正式 test_go_task 快照仍分别保留，不能由后续工作树文件替换。

发现一处历史说明差异，需原样注明：第二轮 README 写正式 test_go_task 的收尾 hash 前缀 `6a32aa80`，而同轮 `review.json.sources_at_close` 记录 `95d97c2a`。二者均保存，不能凭其中之一声明最终测试输入已复核；执行时 `94edba59` 原快照及 before map 已在白名单中。该差异属于历史证据说明，不能默默改旧报告，也不在本任务中扩成新产品 finding。最终作者/独立 freeze 应另绑定最终输入。

## 原件、隐私与引用约束

- `.py` 和 `.sh` 未来只改归档名为 `.py.txt` / `.sh.txt`，原字节、换行、注释和失败内容不变。没有在本轮执行任何输入。
- 不选数据库、journal 全量、bootstrap、临时仓库、runtime/session 目录、tokenizer 资产、`__pycache__`、整份 CLI JSONL、任何 key 文件或真实请求/prompt 文件。输入中的 `PRIVATE_CUSTOMER_CASE_ACME_INTERNAL_2026` 等是负例明确构造的 synthetic canary，应保留以证明当时的问题；不能把它标为真实客户 prompt。
- 原始 CI stdout 仅选择一份已保存的 PR-event 测试失败 log，并绑定原下载摘要。`failed-job-receipts.json` 的 `exit_code=0` 是获取日志成功，**不是 CI 测试通过**。正式复制前仍须对所选 stdout/源码快照做敏感内容核对；清单不授权把真实 prompt/凭据混入公开归档。
- PR92 freeze 仍会引用未选的三份 harness-only XML、106/126 重复 relay 观察及汇总脚本；这些只是原历史目录引用。README 已区分其错误性质，本精简包不另复制。第一轮作者 manifest 中被省略的 `pytest-03.log` 和 `pytest-05.log` 同样不得补为新结果。
- PR92 的 `source-manifest.json` 记录原 CI log hash，但此处没有该 log 原文件；不补造内容。PR103 的 PR/CI snapshots 也是历史快照，不代表发布时最新远端状态。

## 最终归档前的必要步骤

1. 等 root 固定最终作者源码、正式输入、实际结果及独立复验；将其另列 `author-final` / `independent-final` 的明确白名单。当前 JSON 的 `copy_allowed_now=false`、implementation_commit=null 不可自行提升。
2. 对每份选中原件重核本清单 SHA/bytes，变化即停止并交 root 核对；不默默选择最新文件。最终提交源码 Git blobs 与 final freeze 另做精确绑定，不从旧 round 的 green 继承验收。
3. 复制时按具体 `source_path` → `proposed_archive_path` 执行，不递归复制目录；补顶层原路径/去重/缺失引用说明。实际copy完成后独立核对每份原字节，并记录最终 publication map。
4. 原始历史红/skip/static failures 保留；scope 内的旧关闭与新增未关闭 finding 分开。不能把本机 C/P 代替新 PR 的 G，也不能把 synthetic upstream 代替 provider S。
5. 新写 README/JSON 用 LF 并检查新文本空白；原证据按字节保留。若加 leaf `-text`，仅用于阻止 Git 换行转换，不改 whitespace 规则；检查实际 staged diff 时必须如实记录历史 XML/CRLF 空白，不因文件先前 untracked 就声称全部检查通过。

本轮只生成本计划及白名单 JSON；没有实际copy、产品/正式测试修改、行为测试执行、Git mutation 或远端操作。
