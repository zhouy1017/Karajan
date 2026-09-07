# 持久规划执行控制器

本切片对应 [#110](https://github.com/zhouy1017/Karajan/issues/110)，实现 #93 的离线 C 层：
控制器以 owner、Run、planning intent 与 execution ID 为入口，保存不可变的规划绑定、原容量
回执观察、预先冻结的输出来源、输出摘要和既有 `RunPlanner` 的提交回执。它不实现模型 transport、资格、真实预算
准入、`RunnerHost` 或物理取消。

`PlanningExecution.begin/get/reconcile/submit/cancel` 不接受 prompt、Profile、路径、计划 JSON、
artifact 路径或调用方提供的 receipt。`reconcile` 只通过 `PlanningAdmissionAuthority` 查询原
capacity request/key 的已有回执；没有回执、状态 unknown、绑定不一致或权限来源不完整时不申请
新的 admission、不创建进程且不提交计划。

`reconcile` 还要求控制器持有实际的 `CapacityStore`，用原 capacity request/key 与原 activation
request/key 分别只读重开 `admit` 和 `activate` receipt，并逐字段比较 authority 的观察；两个 receipt
都不能缺失或用无关命令替代。它会从内部 output authority 封存来源 SHA-256；
`submit` 只按 execution ID 读取完整 bytes、大小、SHA-256、完成身份和来源摘要，并要求来源等于封存值。
提交前还会再次读取当前 source；冻结来源匹配但当前来源已改变的迟到旧输出同样拒绝。
控制器校验摘要后调用 `parse_planning_output`；成功内容仍由现有 `RunPlanner`
复查 term、lead、授权 ceiling、配置和 v1/v2 routing。owner 的精确批准继续是原有入口，控制器
不替代该决定。

输出捕获后先持久保存完整、已解析的 `submit_plan` request 与固定 key。状态依次为
`output_captured`（尚未 claim）、`submit_claimed`（可能已进入 Run store）、`submission_unknown`
（claim 后找不到 receipt）和 `submitted`。恢复只通过 `RunPlanner.command_receipt` 读取该固定
request/key；已 claim 且 receipt 缺失不会再次提交。取消在 claim 前落为 `cancelled`；claim 后只记录
取消请求并返回 unknown，避免把已经或可能已经提交的计划误报为取消。Run 提交前的 guard 会再次读取
控制器的取消请求，所以先完成的取消不会落 Plan。

复核反例保存在 `.cache/standards-92934b5/` 和 `.cache/reviewer-high-110/`，原件不改写。
旧 Standards 的两个注入都发生在新加入的 `submit_claimed` 之后：`SystemExit` 后没有 Run receipt
会收敛为 `submission_unknown` 而不重投；在私有 Run bridge 内注入取消也收敛为 unknown，但 guard
阻止 Plan 写入。正式回归覆盖 claim 前取消为 `cancelled` 且无 Plan、Run 已提交但回复丢失时只读
恢复原 receipt，以及这些边界所依赖的真实 `CapacityStore` 和 SQLite 记录。

当前没有 production authority factory。因此 production 默认硬拒绝，连 authority 自报
`authority_kind=production` 也不能放行；fixture authority 只可由测试显式设置
`allow_fixture_authorities=True`。这证明 SQLite/控制器行为（C），不证明真实规划、计量、账户、
认证、隔离或调用资格（P/S 仍为 `not_run`）。

验证：

```powershell
$env:PYTHONPATH='backend'
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m pytest --basetemp .pytest-planning-execution tests/runs/test_planning_output.py tests/orchestration/test_planning_execution.py -q
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m pytest tests --collect-only -q
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m ruff check backend/karajan/orchestration/planning_execution.py backend/karajan/runs/planning.py backend/karajan/runs/models.py tests/orchestration/test_planning_execution.py
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m mypy backend/karajan/orchestration/planning_execution.py backend/karajan/runs/planning.py
```
