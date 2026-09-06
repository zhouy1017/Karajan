# 共享 RunExecutionBudget 独立边界

仅审本次 Writer/Check 共享次数与时长接线；不要求历史 collection guard 承担新 effect 授权。等待作者冻结后执行，不把 WIP 缺口计入 findings。

固定 10 项公共行为：

- `ApprovedGoTaskExecution.advance`：prepare 后排队至共享截止，或旧库缺预算表；读取/恢复仍可用，新 Capacity activation、Host 行和 Journal effect 为零。
- `consume_go_task`：原生产者端口收到的 `start_native` / `send_guard` 确实检查共享时间。分别在 native 前、最后 Host 身份等待期间、send 前到期；含未到期正控。Run/Project/Capacity/Host/Journal 均真实库，Host 直属身份与 native producer 是显式 fixture；不将回调计数称为真实 provider 或 OpenCode 运行证明。
- `ApprovedGoCollector.collect/recover`：共享截止已过，原可靠停止/内容事实仍能进入真实 CAS 并精确恢复，不重置预算或 Journal。
- `ApprovedCandidateChecks.advance`：prepared、host_prepared 排队到期，以及旧库缺预算表；不启动新 Host，历史和原预算不变。

预先 collect-only 已发现 10 项，无导入/fixture 收集错误。最终应同时记录产品源前后 SHA、测试 SHA、实际 Windows/适用 Linux 结果、任何失败的原输入；发现只针对真实 effect 入口，不能由 `effect_claim_guard` 曾被历史路径复用推导泛化缺陷。

```text
PYTHONPATH=backend
python -m pytest .cache/shared-budget-independent/test_effect_budget.py -o "pythonpath=backend tests/runs tests/projects tests/capacity tests/isolation tests/adapters/opencode tests/routing" -q --junitxml=.cache/shared-budget-independent/windows-first.xml
```

无真实 key/provider/模型调用，未改产品/作者 tests/CI/Git。
