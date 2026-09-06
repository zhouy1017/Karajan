# Issue 管理与 M0 原范围独立验收审计

审计时间：2026-09-06。审计结论：**Issue #3 和 #4 均满足原始验收范围，可以关闭为 completed。** 两票关闭只代表本地进程与本地假 provider 原语通过，不代表整个 M0、真实模型资格、WSL2 隔离或完整产品交付完成。

## 审计基准与本轮执行

- 原始验收清单直接读取 [Issue #3](https://github.com/zhouy1017/Karajan/issues/3) 与 [Issue #4](https://github.com/zhouy1017/Karajan/issues/4)，读取时均为 open、清单未勾选。
- GitHub connector `fetch_commit(repo_full_name="zhouy1017/Karajan", commit_sha="codex/m0-execution-probes")` 在审计读取时确认远端集成源为 [`d903fa9b850d292b44016cd2321e69d8100ec2fe`](https://github.com/zhouy1017/Karajan/commit/d903fa9b850d292b44016cd2321e69d8100ec2fe)，即已合并 PR #57。本地 `origin/codex/m0-execution-probes` 为相同 SHA。
- 未 checkout、未修改当前开发分支。用 `git archive` 将该 SHA 的相关源码、现有测试、示例及 `pyproject.toml` 导出到独立 `.cache/issue-management/m0-audit-source-d903fa9`；`PYTHONPATH` 显式指向导出源码。
- 环境：Windows，Python 3.12.14；复用已安装的开发依赖，不安装、不更新依赖。
- 本轮命令：`python -m pytest tests/execution tests/resources -q --tb=short --junitxml=..\m0-audit-tests.xml`（已安装开发环境的 Python；仅将本机可执行文件绝对路径简写为 `python`）。
- 结果：**83 passed in 13.00s**；JUnit 精确分类为 RunnerHost 44、resources broker 34、resources probe 5；errors 0、failures 0、skipped 0。
- 本轮另行执行公开资源 CLI：`python -m karajan.resources demo --scenario examples/resources/local-fake-scenario.json --directory ..\m0-resource-demo-d903fa9`，退出码 **0**，8 项观察条件全部 passed。
- 没有读取秘密，没有订阅或收费 API 请求，没有实际模型调用。测试只启动真实本机操作系统进程、SQLite 及绑定 loopback 的合成 HTTP provider。

本轮机器可读证据：

- [JUnit 测试结果](../../examples/issue-management/m0-tests.xml)
- [资源 CLI 完整报告](../../examples/issue-management/resource-report.json)
- 完整资源 JSON 原样保存全部快照、实际 HTTP 收包与条件结果；原 SQLite 账本保留在本机，不纳入本次公开证据。
- JUnit 发布副本仅把本机 hostname 替换为 `redacted-windows-host` 并规范化 XML 排版；83 个用例、状态、时间及计数均保留，未重写测试结论。

## Issue #3：RunnerHost

原 scope 使用真实操作系统进程与假模型，明确排除完整协调器、Run 恢复、Web、所有隔离策略。真实运行时取消资格属于 M0-07，不是此票关票前置条件。

| 原验收条件 | 判定 | 证据与实际观察 |
|---|---|---|
| 固定种子的故障脚本覆盖崩溃点，执行登记与实际子进程计数可检查 | passed | [固定枚举故障测试](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L168) 与 [CLI 真进程硬退出测试](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L341) 均逐一覆盖 after_accept、before_spawn、after_spawn、after_ack；CLI 实际退出码 91。使用固定穷举而非随机抽样，无依赖随机故障种子的不可复现路径；随机临时身份不影响断言。测试检查持久登记、unknown、marker 次数及 OS 进程事实。本轮全部通过。 |
| 同一 start_key 重放不产生第二进程；unknown 禁止再次 spawn | passed | [跨 Host 重放](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L113)、四个故障点及 [8 路并发 start](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L520) 断言仅一次执行。accept/before_spawn 丢失时返回 unknown 且 marker 不存在；不能凭旧结果失效掩盖重复执行。 |
| 取消后的子进程实测退出或确认隔离；取消 ACK 不算证明 | passed | [父进程先退出后取消子树](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L135) 在取消前观察至少两个进程，取消后 status confirmed、state exited、processes 为空，heartbeat 停止增长；本轮通过。 |
| 业务成功但子进程存活的夹具，重启后仍在核对列表 | passed | [业务 done 后恢复测试](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L192) 用真实 sleep 子进程；新 RunnerHost 的 reconcile 仍返回 business done、physical running 及进程身份。 |
| 迟到结果被拒，消费/退出事实继续追加且不复活任务 | passed | [旧 fence / 已取消结果拒绝](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L289)、[迟到消费重新打开核对](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/execution/test_runnerhost.py#L310) 验证取消业务与 exited 物理事实保留、旧 fence 用量幂等追加、结算后退出核对；本轮通过。 |
| 报告区分已测 Windows/WSL2 与 not_run；不声称远端停止或退款 | passed | [实现报告](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/docs/implementation/m0-runnerhost.md) 已列明 Windows 本机通过、WSL2/容器/原生工具及账户调用 not_run，并明确本地停止不推出远端停止或退款。本轮 Windows CLI 测试继续断言 `live_qualified=false`、`remote_stop=unknown`。本次未执行 WSL2/Linux，不将其记为 passed。 |

关闭建议：勾选 6 条原验收项，追加上述固定 SHA、44 passed、Windows 范围与 WSL2/真实服务 not_run 的验收说明，然后关闭 completed。不要把真实模型/完整恢复/恶意工具隔离范围追加为本票的新阻塞。

## Issue #4：预算 broker

原 scope 是最小 SQLite 账本、窄 broker、假金额和本地 provider，明确不证明真实厂商价格完整性或现金硬上限。真实计费边界属于 M0-07。

| 原验收条件 | 判定 | 证据与实际观察 |
|---|---|---|
| 并发父预算、子切片、结算总量可核对；拒绝请求实际收包为零 | passed | [父子不重复计账](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L112)、[同父并发竞争](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L198)：两请求一拒绝一结算、不同 receipt、provider 恰好收一条、held=3。本轮独立 CLI 重现相同事实，原父额没有与子额重复叠加。 |
| 发送前、发送后未记回执、记回执后崩溃可重现；unknown 不恢复余额或盲重发 | passed | [四窗口真实进程退出](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L289) 在 before_send_intent、after_send_intent、after_response、after_settlement 用 `os._exit(71)`。恢复分别保留可判定 not_sent、unknown 上界 2 或 settled 1，并检查实际收包数为 0/0/1/1。本轮全部通过。 |
| 重复结算幂等；Attempt 终态后的迟到真实费用仍计账 | passed | [unknown / finish / late duplicate usage](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L256)：finish 后 held 仍为 2，迟到费用 1 连续提交两次后 held=1、usage 仅一条、收包仍一条。另有未发送调用不允许结算的回归。本轮 CLI 全部场景累计 held 从 4 收敛为 3。 |
| 无 logical_call_id 每次新准入；有 ID 只允许已验证语义幂等 | passed | [可信传输证据参数化测试](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L132) 覆盖无 ID、无可信证据同 ID、有可信证据同 ID，实际发送次数分别为 2/2/1；每次到达仍有不同 receipt。没有按 prompt 正文去重，也未假设 provider 支持幂等。 |
| 不混算币种；价格失效/收费上界缺项时不发送 | passed | [过期/缺项价格零收包](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L222)、[独立 USD/CNY 预算与版本快照](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_broker.py#L384)。USD held=4 与 CNY held=5 分别保留；过期、缺 input/output 收费项或未承诺覆盖全部收费项均拒绝且无 HTTP 请求。 |
| 输出账本快照、provider 收包与故障报告，可外部重算 | passed | [公开演示测试](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/tests/resources/test_probe.py#L12)、[仓库已保存报告](https://github.com/zhouy1017/Karajan/blob/d903fa9b850d292b44016cd2321e69d8100ec2fe/examples/resources/local-fake-report.json) 及本轮独立 CLI 报告均包含快照和实际 HTTP provider_records。本轮 8/8 conditions true，provider 总收包=2、未知阶段 USD held=4、迟到重复费用核对后 held=3，现金 API disabled。 |

关闭建议：勾选 6 条原验收项，追加固定 SHA、39 passed、CLI 8/8 与可重算的收包/金额证据，然后关闭 completed。真实模型价格、完整路由、配额窗口、Commander 保留量、FX、Web 与整机网络隔离仍属于其他 Issue。

## 管理动作边界

本文件记录本轮审计结论，不把关票状态当作行为证据；具体 Issue、PR、默认分支及保护规则以远端核对结果为准。后续自动验收应使用具体任务的原验收条件与固定提交证据；父 Issue 只在其完整清单全部满足后关闭，已验收的独立任务不必等待整个 v1。


## 管理政策独立审查

本轮另对 `AGENTS.md`、`docs/agents/issue-tracker.md`、PR 模板、实现任务表单及 CI 的 dev push 增量进行独立 Standards / Spec 审查，两轴均为 **0 findings**。完整说明见 [政策审查报告](../../examples/issue-management/policy-review.md)，结构核验及五文件 SHA-256 见 [policy-validation.json](../../examples/issue-management/policy-validation.json)。

审查确认：开发 Agent 的自动验收与关票使用用户本次授权；保留原 scope、父票必需条件和剩余责任子票。Closing 只关联合入默认 dev 后完整满足原条件的任务，合并前须核对当前候选 CI、独立审查和行为证据；未合并保持 Open。该流程没有新增任意功能 PR 自动合并权限或付费模型调用权限。

表单 YAML 解析、ID 唯一性、必填字段与本地链接核验通过。CI 解析后的唯一语义变化是 push 分支增加 dev；所有 jobs、矩阵、权限、PR/merge_group 触发和 quality-gate 成功条件保持一致。本报告与模板属于待发布管理变更；此处不声称其最终提交已通过远端 CI、已合并，或模板已在线渲染。

发布证据时重新核对五个管理文件摘要，均与独立审查记录一致。此复制整理过程没有修改这些管理文件，也没有运行真实服务；本轮 WSL2/Linux 及真实 Profile 资格仍为 not_run。
