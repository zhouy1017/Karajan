# Common repair：Windows marker 与静态门独立复核

**接受本次 marker；0 新 finding，已关闭的NATIVE-001–004不重开。** capacity_facts 仅只读审查和独立运行静态门，不更改、stage、commit产品或测试。无provider/key操作。

当前工作区HEAD78c6dc8654099250bb0a3b7829538816d042cd93，正在合入046045f2d9573c3cae6375c38c8de2f6b4190d70；本报告绑定工作区文件字节，不冒称已形成最终合并commit或当前远端CI通过。合并收尾由root负责。

## Marker判断

仅给test_consume_preserves_native_failure_when_collector_rejects_missing_capture加 `skipif(sys.platform != "linux")`。移除新增decorator后，整个test_go_task_execution.py与HEAD版本AST完全相同，正文没有减弱断言。

测试使用approved_fixture→task_runner_source→native_task_source→projected_runtime_source→go_runtime_source：最后入口明确拒绝非Linux，并实际绑定固定ELF、kernel/machine、/usr/bin/unshare/mount/ip/python3摘要。即使Windows设置了Linux ELF路径，也不能取得此真实Linux fingerprint。按平台跳过这一条来源绑定测试合适，不需要为让Windows执行而捏造fingerprint。

Linux正文仍要求首次consume缺capture而拒绝、公共get保存完整failure_diagnostic、原effectclaim存在、grant revoked/零请求，第二次consume恢复且producer不重跑。最新作者target XML：Windows1skip（标记在fixture执行前生效），Linux1passed31.279s。

Windows适用的普通存储合同没有被隐藏：test_go_native_diagnostic_contract.py没有平台marker，common Windows XML内11项全部passed（替换runner、冲突/幂等、两字段5类非法值）。先前仅cache的维护缺口已由此正式文件补齐。

## 实际独立静态

在本工作区、明确PYTHONPATH=backend，使用root .venv Python：
- `python -m ruff check .`：exit0，All checks passed。
- `python -m mypy backend`：exit0，128 source files通过。

命令/退出码/原输出保存在checks.json、ruff.log、mypy.log。此次不重跑已闭环的后端行为全套，也不把别人的XML当自己的执行。

## Common C/P来源核对

root Linux contracts284pass/125.603s、native三case全部pass/153.146s；Windows旧组250pass+1平台错误保留，再补精确marker target Win1skip/Linux1pass。common Linux全247个来源与当前工作树比较，唯一变化是上述marker文件，4个后端源与其余测试相同；删marker后测试AST同，因此原Linux行为覆盖未减弱。

long-path-observation.json记录的两个实际启动operation，其旧拼接socket路径都是127 UTF-8 bytes，大于Linux107限制；另一个丢grant回复case按设计没有新namespace。这是已覆盖原CI长路径的root C/P证据，不是官方S。没有把短basename或只测fixture冒充原长度回归。

source-before/source-after保存10个关键产品/测试SHA，前后无变化；marker-check/review记录common来源比较及作者XML哈希。本报告只验本次平台边界与静态门，远端G仍待root发布后的准确head检查。
