# 第二轮独立审查：原三项关闭，新增存储边界待修

capacity_facts 独立执行。未改产品、正式测试、Git 或远端；没有真实 provider 调用或真实 key 读取。父目录所有原始失败、输入和源码保持不变。

原三例按原字节复制（b3d7d779d6676c2f9c1ff16a43f66885ff9ce757c958d5b9141c963b495f46eb），Linux 3 passed /34.71s（original-three.xml）。未知停止保留原 socket 根、私有大写异常文本归固定分类、实际 consume 在 Collector 拒捕获后公共 get 保留诊断，NATIVE-001/002/003 均关闭。第三例进一步核对原 grant revoked/零请求，以及再次 consume 不重启 producer。本轮仍是实际控制器/SQLite、明确 native故障 port 的 C，未重跑作者 OpenCode namespace P。

新增 claim 边界 1 passed /2.17s（claim-windows.xml）：相同 PID 不同 birth 不能附诊断；原 runner 首写绑定原 intent，不改变 effect claim、activation_allowed=False；不同诊断不能覆盖。该函数在添加两个枚举用例时未修改。

## NATIVE-004 / P2

record_failure_diagnostic 只有 Literal 注解描述 native_stop/relay_status，没有运行时枚举验证。对真实已 claim intent 的两个公共调用分别传入合成非法字符串 PRIVATE_DIAGNOSTIC_CONTENT_IS_NOT_A_CLEANUP_STATE；原 SQLite operation 持久该值，read 可见。cleanup-enum-before.xml 为2 failed /3.67s，原输入 test_diagnostic_claim.py::test_invalid_cleanup_fact_is_rejected_before_persistence。

这是可信 controller 存储原语的严格事实合同缺陷，不是新增 HTTP 权限：实际 consumer 前置有枚举筛选。最小修复是在原语任何写入之前验证精确字符串及规定枚举，非法输入返回稳定领域拒绝；保留原 claim/冲突/取消历史语义。

## 正式维护回归缺口

当前正式 tests 只测 isolation 私有 _failure_diagnostic helper，没有 record_failure_diagnostic 或 consume→Collector 拒绝→原 operation get 持久诊断行为。NATIVE-003 回归仍仅在 cache。应加入正式公共回归，由既有 CI 执行；无需修改 workflow 或降低 gate。

## 静态与来源

独立 Ruff 对执行时4个受影响产品+2个正式测试通过；Windows 默认 mypy backend/karajan 128源通过。准确命令、输出、退出码保存在 checks.json/ruff.txt/mypy.txt。

source-before.json 固定执行时6个修改文件。收尾时4产品仍逐字节相同，仅作者 test_go_task.py 从94edba59…变为6a32aa80…：其 synthetic config.model 改为 opencode-go/glm-5.3-flash 并出现缩进变化。该变更位于作者测试体，不是独立测试消费的 prepared helper，故不影响本轮C复验。静态通过仅绑定执行时测试字节，最终测试字节应由作者收尾检查。review.json 逐项保留 before/current，不将旧结果升级成最终P、CI或S。

复现命令和运行环境沿父README；新增 XML 必须使用新路径，勿覆盖本轮原始结果。本轮已停写，等待指定作者修复 NATIVE-004 和正式维护回归。
