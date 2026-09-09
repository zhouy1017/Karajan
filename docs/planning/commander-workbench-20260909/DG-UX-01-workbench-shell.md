# 实现切片｜Commander Workbench 主入口与详情抽屉

Parent: [#16 可恢复运行视图与当前有效操作](https://github.com/zhouy1017/Karajan/issues/16)。Related: [#153 工作台业务闭环](https://github.com/zhouy1017/Karajan/issues/153)、[#11 项目与认证入口](https://github.com/zhouy1017/Karajan/issues/11)、[#12 有界规划与确认](https://github.com/zhouy1017/Karajan/issues/12)、[#158 设计治理](https://github.com/zhouy1017/Karajan/issues/158)。

发布标签：`kind:task`、`status:queued`。Worker：`gpt-5.6-luna`；独立 Reviewer：`gpt-6-astra`。实现依赖：现有认证工作台、Project/Run 读模型和活动 Commander Workbench PRD 的字段；不要求 #153 的真实整链先完成。真实验收 gate：#16/#153 原 AC 中对应的 U/C 状态持久化与恢复条件，不能由静态截图或 mock 页面替代。

关联交互验收：UX-AC01、UX-AC02、UX-AC05、UX-AC10，定义见 [工作台设计](https://github.com/zhouy1017/Karajan/blob/codex/commander-workbench-design-20260909/docs/prd/commander-workbench.md)。

## 范围

在现有认证工作台加入 Tasks / Agents 双列表、任务详情抽屉、Commander 会话入口和可恢复的会话/任务状态。入口以用户正在处理的仓库和任务为中心；资格、规则、预算和容量显示为按需状态摘要及具体阻塞，不把它们做成每天打开仓库后的首屏表单。

## 验收

- [ ] **U：进入。** 用户从认证工作台打开一个受管仓库后，能选择高级 Commander 的模型与来源并进入其会话；页面显示 Tasks 列表、Agents 列表和任务详情抽屉，当前选中对象有稳定 ID。
- [ ] **U/C：读回。** 刷新、关闭并重新打开页面后，仍可读回同一仓库、会话、Task/Agent 身份、状态、当前 term、阻塞原因和下一步；视图来自持久事实，不由浏览器缓存拼接。
- [ ] **C：边界。** 浏览器可提交用户自然语言消息和模型选择；服务端不信任浏览器提供的编译后 runtime prompt、argv、endpoint、Profile 资格、模型输出或 admitted status 作为授权；越界输入得到具体错误且不产生模型请求、文件修改或执行 activation。
- [ ] **U：入口层级。** 日常首屏围绕 Tasks / Agents、抽屉和会话；资源/规则/资格信息只作为上下文状态或按需详情，用户仍能看懂阻塞及解除条件。
- [ ] **G：交付。** 固定实现 commit、可复现操作、U/C 证据、相关失败和独立 Standards/Spec 审查齐备；本票完成不关闭 #16/#153，也不宣称 Planning、真实来源或执行整链通过。

## 非范围与证据边界

本票不实现真实 Planning transport、模型调用、Task dispatch、并行 writer、checks、Reviewer、GitHub PR、资格授予或预算/规则编辑。已有离线领域证据可以支持组件接线，不能代替本票的真实页面操作、刷新恢复和拒绝路径。

## 交接

完成后把页面实际字段、操作入口、失败响应和证据链接回填 #16/#153；若发现字段需要改变用户行为，更新活动 PRD revision 和治理映射，不修改发布快照。
