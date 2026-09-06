# 批准 Run 路由评估的独立 Spec 验收

最终独立公共接口检查 **10 项通过**，无遗留 Spec 发现。输入由本目录的新建合成 Git 仓库、公开 ProjectRegistry/RunPlanner 命令和真实 SQLite 产生；规划 admission receipt 明确标记 fixture。测试不导入作者测试 helper，不注入资格通过、不直接写业务数据库，也不运行模型。

检查覆盖：

- 合法自定义 Rulebook 经正式 preview/publish 后进入批准 Run；多个规则复用同组时，仅当前匹配 rule 的 normal/quality 授权有效。
- 真实 CapacityStore 中另一个 Run 的 admission、activation 和 usage，使评估读取到原观测 40、未覆盖消费 3、未来预留 7，分别保留；assessment 本身没有新增容量 admission。
- 显式 owner Estimate 的 7.25 百分比需求绑定实际窗口，保持 unknown/无价格/无完成时间；缺失、撤销或到期不会补默认值。
- 新命令读取最新 unknown 观测，不回退旧 numeric；同 key 重放保留原历史收据与身份，不冒充新事实。
- 新增待批准计划不替代当前 active plan 的需求和输出预留；缺批准、缺任务、依赖 Reviewer 及越权请求拒绝。
- 当前账号身份变化后拒绝旧 Profile 和估计，但原批准计划和历史 receipt 保持可读。

根首先发现 `runs/validation.py` 固定索引 `commander_qualified` 的问题；Spec 用编译通过的 `owner_lead_pool` 重命名及公开 create 独立复现 `KeyError`，证据为 `history/renamed-commander.red.junit.xml`。根修复后，原用例随完整 10 项再次通过。此发现归属根、由 Spec 独立复现，未重复算成另外的问题。

准备阶段有两处测试输入修正，不计产品发现：最早把 mechanical 普通组改成它自己的 quality 组，触发合法的阶段循环拒绝；首次构造新计划更改 Task 字段但未提升 Task revision，触发合法的 `TASK_REVISION_REUSED`。随后使用合法 bounded-worker→fast_qualified 自定义与 Task revision 2。历史准备 JUnit 保留，最终结论仅使用 `final.junit.xml`。

当前受信资格生产器只支持 local_fixture，runtime_tools 保持缺证，所以这些真实来源评估仍为 blocked。结果只证明持久 assessment 和来源接线；不证明真实可调度资格、现金硬上限、配额 admission、工具执行或项目交付。本次没有把 test double 产生的 selected 结果当作实际可执行。

复跑（从本 worktree 根，选择新的 basetemp）：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
python -m pytest examples/approved-routing/routing/spec/test_public_assessment.py --basetemp .cache/approved-routing-spec/replay-new -q
```

Ruff check/format 已通过。`review.json` 绑定本轮读取的产品源码与独立测试/最终结果摘要。

正式发布测试以向上查找 `pyproject.toml` 定位仓库根。history/ 保留开发路径的历史记录；本目录的 source-final.json、review.json 与 final.junit.xml 对应发布路径实际复跑。发布目录不包含运行 state，临时 Git/SQLite 仅存在 --basetemp 指定缓存中。

`fixture.py` 是 Spec 自己维护的公共命令 fixture 模块，由本目录测试与 demand/spec 独立导入；没有作者测试依赖。三份 routing 产品源码经 CRLF→LF 规范化后重新执行完整验收，旧报告和映射保留在 history/，不将重复运行数量累加。
