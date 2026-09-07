# 持久规划执行控制器

本切片对应 [#110](https://github.com/zhouy1017/Karajan/issues/110)，实现 #93 的离线 C 层：
控制器以 owner、Run、planning intent 与 execution ID 为入口，保存不可变的规划绑定、原容量
回执观察、输出摘要和既有 `RunPlanner` 的提交回执。它不实现模型 transport、资格、真实预算
准入、`RunnerHost` 或物理取消。

`PlanningExecution.begin/get/reconcile/submit/cancel` 不接受 prompt、Profile、路径、计划 JSON、
artifact 路径或调用方提供的 receipt。`reconcile` 只通过 `PlanningAdmissionAuthority` 查询原
capacity request/key 的已有回执；没有回执、状态 unknown、绑定不一致或权限来源不完整时不申请
新的 admission、不创建进程且不提交计划。

`submit` 只按 execution ID 从 `PlanningOutputAuthority` 读取完整 bytes、大小、SHA-256、完成
身份和来源摘要。控制器校验摘要后调用 `parse_planning_output`；成功内容仍由现有 `RunPlanner`
复查 term、lead、授权 ceiling、配置和 v1/v2 routing。owner 的精确批准继续是原有入口，控制器
不替代该决定。

当前没有生产 authority factory。因此 production 默认硬拒绝，连 authority 自报
`authority_kind=production` 也不能放行；fixture authority 只可由测试显式设置
`allow_fixture_authorities=True`。这证明 SQLite/控制器行为（C），不证明真实规划、计量、账户、
认证、隔离或调用资格（P/S 仍为 `not_run`）。

验证：

```powershell
$env:PYTHONPATH='backend'
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m pytest --basetemp .pytest-planning-execution tests/runs/test_planning_output.py tests/orchestration/test_planning_execution.py -q
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m ruff check backend/karajan/orchestration/planning_execution.py backend/karajan/runs/planning.py backend/karajan/runs/models.py tests/orchestration/test_planning_execution.py
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe' -m mypy backend/karajan/orchestration/planning_execution.py backend/karajan/runs/planning.py
```
