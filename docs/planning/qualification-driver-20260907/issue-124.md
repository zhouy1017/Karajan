Parent: #107（固定 Reviewer 机制官方 S）；Related: #95。Worker: gpt-5.6-terra；修复同级；独立 reviewer: gpt-6-astra high。此票只处理验收 driver 的 C/P 可靠性，不新增模型调用授权，也不代替 #107 正控 S。

## 实际问题
attempt4 的固定 suite 已通过6请求，但整个命令没有完整工具回执，最终证据只在末尾写入；无法从driver日志定位停止阶段，原资格600秒窗口内未完成实际binding正控。之后恢复命令错误使用系统python3，在导入pydantic前失败；原记录最终expired、consumer无prepared/ready转移。保存上述具体事实和unknown，不能归为已证实产品缺陷。此前attempt2/3各6请求和提前撤销的操作缺口全部保留。

## 有界实现
只修改 examples/go-readonly-reviewer-qualification-20260907 下的 ordered driver / consumer辅助代码、其独立测试及说明。产品QualificationStore/GoSuite/Runner/Relay/Journal来源与安全合同保持。将preflight、原start/record读回、资格完成、正控、撤销、负控/历史、完成分别落盘到原子且脱敏的阶段回执；不只在函数末尾一次输出。固定完整WSL解释器/当前产品树/精确命令和稳定错误类型或code，禁止任意异常文本进入公开报告。

新增明确resume入口，只从原command/start/record与原Profile/source/generation继续。resume绝不调用qualify或创建新start；pending/unknown/expired保持对应事实，不能改时钟、延长原record、篡改资格或使fixture成为S。正控后中断必须恢复同一持久membership证据，不重建批准Run或丢失原receipt；资格已revoke时不得再伪造当前正控。业务ReviewerTask/ReviewEvidence仍不执行。suite own-grant cleanup与qualification record revoke分开。

在任何未来真实调用前先完整准备并验证controller fixture与解释器；调用窗口与资格有效期的区别需明确，不用一个600秒总执行预算替代恢复策略。此票期间不运行新Go。

## 验收
- [ ] 真实本地Store/SQLite和受控故障证明各阶段回执原子、脱敏；首次失败与未知保留。
- [ ] 原record恢复无qualify/newstart/model effect；有效时按同一consumer正控->revoke->负控/历史，过期时稳定拒绝。
- [ ] 正控后、撤销后、末尾写证据前中断均能从原身份恢复事实，不新建批准内容/不冒充当前资格。
- [ ] 复现与execute/resume命令明确使用已存在的WSL离线venv，并证明导入当前工作树；no-effect演练不能写作S通过。
- [ ] 必要窄回归、ruff及适用类型/语法检查；独立Standards/Spec与当前候选G通过后可待合并。#107的实际binding正控及S/G仍单独验收。

已完成：现象与持久状态已确认。剩余：上述可靠性修复与验收。阻塞：无；此C/P任务不需要新的Go请求。