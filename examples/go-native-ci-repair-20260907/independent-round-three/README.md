# 第三轮独立复验

capacity_facts 执行；仅在新 round-three 写入独立输入/证据，不改产品、正式tests、Git或远端；无真实 provider 或真实 key 操作。原两轮字节保持不变。

## 产品结果：001–004 与 claim 正控全部通过

原 test_native_boundaries.py（SHA b3d7d779d6676c2f9c1ff16a43f66885ff9ce757c958d5b9141c963b495f46eb）与 test_diagnostic_claim.py（SHA c41cacaea53a279a8c2b01b46b8c661071d3e8c5876d2bcb50b33dd8e9b83713）逐字节复制，使用正确 WSL ELF/tokenizer+require/offline 环境实际运行。原6项全部passed：unknown停止保留根目录、私有异常文本不回显、公共consume拒capture仍保存内层诊断/重放不重复、替换runner及覆盖诊断拒绝、两个非法cleanup枚举在写入前拒绝。NATIVE-001/002/003/004 均关闭。

## 正式维护测试：实际运行发现测试预期错误

同命令还运行 tests/runs/test_go_task_execution.py::test_consume_preserves_native_failure_when_collector_rejects_missing_capture。首次consume、真实Collector拒绝、公共get完整诊断、原effectclaim、grant revoked/零请求全部断言通过；第二次consume处使用 with raises TASK_STOPPED_CAPTURE_REQUIRED，实际 DID NOT RAISE。

现有consume_go_task:338–341在已有effect claim时返回facade.reconcile，正是原来规定的不重启/只读恢复。修正应仅去掉正式测试第二次错误的raises，继续断言producer调用次数/原诊断/claim不变；不要修改产品让恢复再次执行Collector或重抛首次错误。final-linux.xml保存本次1failed/6passed、75.02s，无skip。这个失败不重新打开已关闭的NATIVE-003。

另外，正式tests搜索仍没有record_failure_diagnostic直接枚举/claim合同回归。该原语实现已由本轮独立测试实证正确，但NATIVE-004及claim维护用例仍仅cache，应由指定作者加入正式CI覆盖；不需要改workflow或放宽gate。

## 来源与边界

source-before.json/source-after.json的4个产品与3个正式测试SHA完全一致。原独立输入SHA亦未变化。review.json绑定逐文件来源、XML testcase结果和准确命令。

本轮6独立及正式例属于真实SQLite/Host noop child、显式native故障/身份double的C。正确ELF/tokenizer只作为真实源码/资产绑定，不把故障double称作namespace P。作者 linux-positive.xml 为1pass5.948s，只读引用为author证据；此前 formal-consume-green.xml 实为Windows missing-ELF 1skip，不能升级成功。作者最终真实三P尚由root按新来源核验，本报告不将旧失败或带green名称的XML当最终P/CI/S。
