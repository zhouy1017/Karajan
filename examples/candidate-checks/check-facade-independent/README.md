# #94 Check facade 独立审查

结论：本次有界 Standards / Spec 审查未发现产品问题。审阅的是其他作者的
`ApprovedCandidateChecks`；审查者此前实现过 Check Host manifest/union，因此这里不把
Host 自身实现作为独立审查成果。测试调用实际 Host 和持久 SQLite/CAS，原生 Check
与 Host child 身份在故障排序用例中使用明确的边界替身；实际 Host 丢回复用例启动了
真实 Python 子进程。没有真实 provider、模型、用户密钥或网络调用。

## 结果与范围

- `windows-final.xml`：3 passed（JUnit 6.834 秒，执行终端摘要 9.23 秒）；
  `linux-final.xml`：同 3 项，6.48 秒。保留各自原始计时，不将差值解释成测试失败。
- `windows-original.xml`：独立复跑作者公开 26 项，26 passed，40.70 秒。
- 两个并发 advance 在 Evidence 提交窗口与取消竞争：只有一份提交，迟到成功记录
  保留历史，原取消不被覆盖，质量门与交付资格仍为 false。
- 实际 Host.start 已启动后丢失返回：通过原 Host 身份恢复；禁用当前 source resolver
  后仍可读回，不重复 Host.start、不启动原生 Check，不分配新执行身份。
- 日志 bytes 与持久观察的 hash/size 不匹配：在 Evidence claim 前拒绝，没有写入
  Candidate ledger，也不会因日志文本写着 passed 而产生通过证据。

代码检查同时核对 ID-only 输入、短事务 claim-before-effect、当前批准和共享时长门、
原生启动后的观察与日志来源、精确 Evidence 恢复、历史读取无需当前源码，以及失败
检查不省略其余批准的检查。原作者矩阵覆盖结果/提交丢回复、取消、未知日志与来源
失效；新增 3 项针对不同故障排序，没有把每个实现分支镜像成测试。

## 历史与固定来源

`test-initial.py.txt` 和 `windows-first.xml` 保留首轮：2 passed / 1 failed。
失败为审查脚本误用不存在的 `CandidateStore.database` 属性；修正为公开目录下真实
`candidates.sqlite` 后才执行日志故障断言。这不是产品 red 或作者缺陷修复。
产品和作者测试没有被本次审查修改。`source-before.json` 与 `review.json` 绑定本轮
开始/结束文件 hash，全部相同。

主产品 `candidate_checks.py` SHA256：
`346014ca0410b90acecc3139a87a2b45f81b93d5839e56040bcc00a0958b4d5b`。

## 尚未满足的整票范围

本报告只覆盖控制器契约与恢复故障；实际 Linux 隔离、完整生产 Host/namespace 链
由独立运行器和根集成报告提供，不能用这里的替身断言替代。
当前仅消费 revision 1 的原 capture subject；#95 的同内容验证 subject 重绑定与
重新执行全部 Checks 尚未实现。没有模型 Reviewer、真实 Commander 或 PR 交付，
`checks_passed` 也不是当前完整质量门通过。当前提交的 CI 与最终发布仍由根核对，
本报告不声明 #94 或父票完成。

## 复跑

在仓库根设置 `PYTHONPATH=backend`，执行：

```text
python -m pytest .cache/check-facade-independent/test_facade_boundaries.py -o "pythonpath=backend tests/runs tests/projects tests/capacity tests/isolation tests/adapters/opencode tests/routing" -q
python -m pytest tests/runs/test_candidate_checks.py -o "pythonpath=backend tests/runs tests/projects tests/capacity tests/isolation tests/adapters/opencode tests/routing" -q
```

公开测试源和 XML 不包含数据库或原生私有执行目录。若发布存档，将独立测试改名
为 `.py.txt` 可保持 bytes 并避免历史用例递归收集。
