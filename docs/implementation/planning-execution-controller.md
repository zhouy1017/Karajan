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
这个检查同时位于新输出捕获和从 `output_captured` 重开时的首次 claim 之前；只有已有 Run receipt 的
`submit_claimed`/`submission_unknown` 恢复可以只读历史而不要求 live authority。
控制器校验摘要后调用 `parse_planning_output`；成功内容仍由现有 `RunPlanner`
复查 term、lead、授权 ceiling、配置和 v1/v2 routing。owner 的精确批准继续是原有入口，控制器
不替代该决定。

输出捕获后先持久保存完整、已解析的 `submit_plan` request 与固定 key。状态依次为
`output_captured`（尚未 claim）、`submit_claimed`（可能已进入 Run store）、`submission_unknown`
（claim 后找不到 receipt）和 `submitted`。恢复只通过 `RunPlanner.command_receipt` 读取该固定
request/key；已 claim 且 receipt 缺失不会再次提交。receipt-only 恢复会返回观察性的 unknown，
但保留 durable `submit_claimed`，因为它不能分辨崩溃的 claimant 与仍在进入 Run store 的 claimant；
后续 `get` 读出的 claim 仍表示未核清，且不给恢复者提交权。取消在 claim 前落为 `cancelled`；claim 后
只记录取消请求并返回 unknown，避免把已经或可能已经提交的计划误报为取消。Run 提交前的 guard 先读取
取消、在不持控制器锁时读取并校验当前 output source，随后再次读取取消；因此 source read 中或之前完成的
取消不会落 Plan。

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
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe' --basetemp .cache\terra-planning-execution-pytest-windows tests/runs/test_planning_execution.py tests/runs/test_routing_authorization.py -q
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe' tests --collect-only -q
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\ruff.exe' check backend tests
& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\mypy.exe' backend/karajan
```

## PR #119 测试收集修复证据（2026-09-07）

候选 `9911f1a0a0cd20246adc53cd3be29b31f0da5daf` 的 Windows 和 Linux CI
(`34097329556`、`34097326425`) 都以 `uv run --frozen --extra dev pytest tests`
进入收集，得到 2,368 项和 31 个 `ModuleNotFoundError: No module named 'tests'`。
失败来自控制器测试跨目录使用 `tests.runs.*`，以及它把 `test_routing_authorization`
连带改为该包导入。`python -m pytest` 会把当前目录加入 `sys.path`，所以其本地成功不能证明
CI 的 console-script 入口。

修复候选 `ca0366fc1c822d9c6f797fc20f6ea20bc7f33eeb` 将控制器测试移到已有的
`tests/runs/` fixture 域，恢复 `test_planning` 和 `test_routing_authorization` 的扁平导入。
没有把 `tests` 变成包，也没有修改 CI、产品代码或既有独立审查 archive。Windows 与 WSL 的
同一 `pytest` console launcher 全库收集均为 2,748 项、零错误；Windows 定向回归 49 passed；
`ruff check backend tests` 与 `mypy backend/karajan` 通过。工作机没有 `uv`，所以记录的是 CI 中
`uv` 最终执行的同一 console launcher；完整命令和 stdout 摘要见
[`examples/planning-execution-ci-repair-119/README.md`](../../examples/planning-execution-ci-repair-119/README.md)。
