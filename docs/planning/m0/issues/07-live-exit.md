# M0-07｜完成一订阅＋一 API 的真实资格验证与 M0 出口

## 用户价值

开始建设产品闭环前，确认至少两个不同来源能在同一受控任务上下文工作，并有真实版本、权限、计量和停止证据支撑架构。

## 开始真实测试前必须具备

固定运行时/模型、官方订阅登录、API 厂商与 secret 引用、原币测试上限、有效价格及收费项上界、允许工具和测试目录。未填写的值不表示无限。仅收集配置和离线准备不代表用户已授权消费；真实调用须具有明确范围和金额授权。

## 范围与演示

将 RunnerHost、订阅适配器、API runner/broker 和隔离环境组合成串行探针：API Worker 在专用测试仓库完成很小的改动；官方订阅端在新上下文读取同一确定候选并返回检查意见。再分别执行有界取消与故障探针。无需完整规划、Web、任务图或真实 PR。

- 补齐 [M0-04 #5](https://github.com/zhouy1017/Karajan/issues/5)、[M0-05 #6](https://github.com/zhouy1017/Karajan/issues/6)、[M0-06 #7](https://github.com/zhouy1017/Karajan/issues/7) 留下的真实认证、模型/参数确认、文件工具和网络边界用例。
- 验证 API 请求实际经过准入通路，已发生和未知消费保留；不把“小额账单”当作上界证明。
- 若无法覆盖所有调用、价格与收费上界，则该 API Profile 不得标记 bounded_calls；不自动改用更宽的现金策略。
- 订阅用量只报告可见事实、估算及 unknown；本地取消不推断服务端停止或退款。
- 输出能力矩阵、限制、可采用的最小接口和需回写技术设计的差异。

## 验收标准

- [ ] 至少一个官方订阅 Profile 和一个 API Profile，在同一选定环境组合下通过各自角色所需资格；模型/收费通道固定且有证据。
- [ ] 小候选由 API 端产生、订阅端以新上下文接收，输入内容身份一致，结果结构可供后续平台使用。
- [ ] 必需隔离、原生权限、逐调用现金约束和取消/失联核对真实用例均有结果；不适用项有理由，不将 unsupported/not_run 算通过。
- [ ] 消费记录覆盖成功、失败和未知；支出不明时保留占用并停止进一步未经准入的调用。
- [ ] 发布 M0 资格结论及 M1 接口冻结清单，将关键不兼容项写入对应技术决定；所有后续任务可追溯具体能力证据。
- [ ] 没有合格的订阅＋API 组合、缺真实测试或关键能力失败时，M0 出口保持未通过，不能仅提交调查文档就关闭此出口票。

## 验证与边界

这是有配置后才执行的真实资格任务；本次 Issue 发布不启动测试、启用账户或消费。所有资料只记录 secret 引用与脱敏证据。Bernstein 不是必要出口，直接复用已合格执行器是有效方案。

关联 FR02、FR07、FR10、FR15、FR16；A09、A10、A12、A13、A14、A19、A23、A26 的 M0 子集。通过本票不表示完整 A01–A26 或 v1 验收完成。

## 依赖

Blocked by：[M0-03 #4](https://github.com/zhouy1017/Karajan/issues/4)（资源准入和未知消费）、[M0-06 #7](https://github.com/zhouy1017/Karajan/issues/7)（已组合生命周期、两执行器和隔离探针）。[M0-02 #3](https://github.com/zhouy1017/Karajan/issues/3)、[M0-04 #5](https://github.com/zhouy1017/Karajan/issues/5)、[M0-05 #6](https://github.com/zhouy1017/Karajan/issues/6) 经 [M0-06 #7](https://github.com/zhouy1017/Karajan/issues/7) 传递依赖，不重复建立边。

## Parent

[PRD v1 #1](https://github.com/zhouy1017/Karajan/issues/1)

<!-- karajan:m0:07 -->
