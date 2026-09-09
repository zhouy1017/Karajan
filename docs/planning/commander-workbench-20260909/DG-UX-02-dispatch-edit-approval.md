# 实现切片｜可编辑任务分工与同版批准

Parent: [#12 有界规划、确认和最小人工交接](https://github.com/zhouy1017/Karajan/issues/12)。Related: [#93 Commander 规划桥](https://github.com/zhouy1017/Karajan/issues/93)、[#153 工作台业务闭环](https://github.com/zhouy1017/Karajan/issues/153)、[#158 设计治理](https://github.com/zhouy1017/Karajan/issues/158)。

发布标签：`kind:task`、`status:queued`。Worker：`gpt-5.6-terra`；独立 Reviewer：`gpt-6-astra`。实现依赖：P1 Workbench 主入口（[#159](https://github.com/zhouy1017/Karajan/issues/159)，原生 blocked-by）、现有 Planning controller/admission、#112 业务 transport、#142 仓库快照、#146 输入编译和 #147 资格前置。真实验收 gate：#12/#93/#153 的原 AC；资格 probe 不能替代业务 transport，真实来源资格由原责任票承担。

关联交互验收：UX-AC02、UX-AC03、UX-AC06、UX-AC09，定义见 [工作台设计](https://github.com/zhouy1017/Karajan/blob/codex/commander-workbench-design-20260909/docs/prd/commander-workbench.md)。

## 范围

从 Commander 持续会话提出任务图，允许用户一键接受完整默认分工，或按需编辑角色、明确模型来源、依赖、写权限和检查要求。服务端从可信需求、验收、仓库快照和当前资格/资源事实重新编译不可变版本；用户批准正在查看的同一版本。

## 验收

- [ ] **C/U：分工一致。** 角色、显式模型来源、依赖、写权限和 checks 在计划、授权摘要、Task 列表和读回页面中保持一致；浏览器不能伪造 Profile、模型输出、admission 或批准事实。
- [ ] **C/S：真实输入。** 受信服务端读取固定需求、验收和可信仓库内容，经现有 transport 生成完整最终输出；不得用 owner 手写 Plan、fixture 或 qualification probe 冒充真实 Commander。
- [ ] **C/U：硬 gate。** 能力、额度、认证、隔离或资源不满足时显示具体阻塞并零新模型发送；显式选择不被静默替换。严格单一 Profile 不自动替换；已明确 opt-in 且获批的替代集合可供新 Attempt 使用，范围内不重复批准，超出授权才形成新版本。unknown 额度仍按已有合法有限保守模式判断，不等于一律阻塞。
- [ ] **C：同版批准。** 批准记录绑定 plan/authorization revision、任务图、来源、预算和检查；旧 term、篡改、来源撤销或失效版本被拒绝；同 command key 重放不新增请求。
- [ ] **P：仓库边界。** 用户批准前测试仓库 bytes/modes 不变；批准只创建持久意图和授权，不直接绕过调度 admission。
- [ ] **G：交付。** 按当前 candidate commit 取得影响范围测试、独立 Standards/Spec 和必需 CI；本票不关闭 #12/#93/#153，不完成批准后的 Worker、Reviewer 或真实 PR。

## 非范围与证据边界

本票不实现两个 writer 的并行执行、自动换源、完整容量/预算产品、真实来源资格、checks consumer、Reviewer、GitHub 写入或现金后备。已有 v2 approval 页面和 Planning 领域测试只证明其明确子集；新版本必须绑定当前候选和本票实现 commit。

## 交接

完成后将版本字段、拒绝错误、来源/额度阻塞和重放证据回填 #12/#93/#153；真实资格结果分别回填 #147/#113 或其原责任子票。
