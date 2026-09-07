# #101 独立消费者审查

审查者：capacity_facts。基线 `3d47194147edf153a0a48a183b34ff7222d674d4`，审查对象为当前冻结的 `candidate_checks.py`、`candidate_subjects.py` 和 `_candidate_check_runner.py`；完整 SHA 见 `review.json`。审查者不是这三个文件的作者。曾参与旧 Host 接口和本批 membership 实现，本报告不把这些部分算作独立审查；尚未冻结的 Reviewer producer 也不在范围内。所有团队槽位均在使用，本次由同一审查者分别记录 Standards 和 Spec，不声称两名独立审查者。

## Standards

无确认问题。按 AGENTS/Issue 跟踪流程、CONTEXT 术语和测试门约定检查：共享 subject DTO 集中处理谱系与历史 cycle；消费入口继续只收原 Run/operation/principal，模型或检查文本不构成权威结果；历史 CAS/Evidence 查询不创造执行资格。fixture child 独立位于 tests，没有生产 fixture 开关，限定其 C/P 证明范围。未将工具已检查的格式问题作为产品发现。

## Spec

无确认问题。核对 #101 及 #94 原要求：A 的 capture 锚点保留，B/C 使用完整前驱和精确 rebind 收据；安装与新效果执行当前 producer guard，pending transition 禁止旧效果；完整旧 cycle 归档，旧 child/迟到 Evidence 按原 check ID 回填；新 cycle 重建全部检查身份并沿用 Run 累计预算。已停止与未知停止明确区分；缺 Review 仍不可交付。

独立 C 故障测试 5 项：实际安装 COMMIT 后回复丢失；native claim 持久后资格撤销；完整 receipt 的 request_sha256/baseline_id 各自不匹配；A→B→C 后 A 迟到失败只更新 A 历史。真实 Run/Project/operation/Host ledger/Git/CAS/Evidence，资格与检查进程结果为显式边界 double。没有官方 provider 调用。

Windows 独立 5＋作者新 16＋旧 Checks 26：47 passed，pytest 输出 78.00s，JUnit 77.423s。WSL 独立 5：5 passed，pytest 输出 6.28s，JUnit 6.200s。两个统计时钟分别保留，不改原 XML。冻结产品及作者测试 SHA 在运行后逐项复核未变。独立脚本之后仅 Ruff 格式化，AST 相等，实际执行字节保存在 `test-final-executed.py.txt`，前后摘要见 `review.json`。

首轮 4 passed / 1 failed 是审查用例把 Digest 类型的 baseline_id 写成非 Digest 字符串，产品正确先报 TRANSITION_INVALID；改为合法异 Digest 后测试预期 RECEIPT_MISMATCH 通过。`test-first.py.txt` 和 `windows-first.xml` 保留，不能计为产品红灯。首次普通沙箱启动 WSL 返回 E_ACCESSDENIED，未执行测试；授权本地 WSL 命令成功后才记录 Linux 结果。

## 重现

Windows：在本树设置 `PYTHONPATH=backend` 后，执行根 `.venv/Scripts/python.exe -m pytest -o "pythonpath=backend tests/runs tests/projects tests/isolation" .cache/subject-independent-review/test_subject_boundaries.py tests/runs/test_candidate_subjects.py tests/runs/test_candidate_checks.py -q`。

WSL：在同树使用 `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest -c pyproject.toml -p no:cacheprovider -o "pythonpath=backend tests/runs tests/projects tests/isolation" .cache/subject-independent-review/test_subject_boundaries.py -q`，同样显式 `PYTHONPATH=backend`。无需 tokenizer、模型运行时或账号。

本报告没有执行新增两项 namespace P，也不替代组合源码最终冻结后的 P 和当前 PR CI。生产目前没有合格 Reviewer 时仍 blocked，不宣布 #95、整个 #94 或真实端到端链已完成。
