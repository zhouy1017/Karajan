# 交付切片｜组合候选 checks、独立 Review、diff 与 PR 证据

Parent: [#14 独立交付、同一 PR 与完成语义](https://github.com/zhouy1017/Karajan/issues/14)。Related: [#17 并行与组合验证](https://github.com/zhouy1017/Karajan/issues/17)、[#95 Candidate Reviewer](https://github.com/zhouy1017/Karajan/issues/95)、[#116 Reviewer 实际执行](https://github.com/zhouy1017/Karajan/issues/116)、[#158 设计治理](https://github.com/zhouy1017/Karajan/issues/158)。

发布标签：`kind:task`、`status:queued`。Worker：`gpt-5.6-terra`；独立 Reviewer：`gpt-6-astra`。实现依赖：P3 组合候选（[#161](https://github.com/zhouy1017/Karajan/issues/161)，原生 blocked-by）、#94 Checks/Candidate 原语、#95/#116 Reviewer 输入与执行接线。真实验收 gate：#14/#17/#95 原 AC、真实来源资格和 GitHub 远端证据；本票不改变其完整交付要求。

关联交互验收：UX-AC05、UX-AC08、UX-AC10，定义见 [工作台设计](https://github.com/zhouy1017/Karajan/blob/codex/commander-workbench-design-20260909/docs/prd/commander-workbench.md)。

## 范围

对组合 Candidate 运行必需 checks；从新上下文启动独立 Reviewer，读取同一 Candidate、批准版本和证据；工作台显示 checks、Review、diff、当前 head、PR 和限制。交付沿用既有独立 activation、当前 expected head 和响应丢失核对。

## 验收

- [ ] **C/P：checks 绑定。** 每个 check 绑定当前组合 Candidate、base/tree/policy digest 和执行环境；缺失、失败、不确定、过期或日志损坏阻止交付，旧 Candidate 的通过不可继承。
- [ ] **C/P/S：独立 Review。** 合格且已授权的真实 Reviewer 模型从新上下文读取同一 Candidate 和批准验收材料，保留实际请求与来源证据，不能读取 writer 会话或接收其自报 verdict；Review 输出绑定 Candidate、Evidence、当前 policy 和来源身份。
- [ ] **U：详情。** Tasks / Agents 详情抽屉能显示 checks、Review、diff、当前 head、PR 身份和限制；页面状态来自持久事实，刷新/丢 SSE 后可查询恢复。
- [ ] **G：交付核对。** 目标 repo/base/head、diff、PR、CI 和每步 activation 均可回读；响应丢失先查询同一对象，不重复创建 PR、不覆盖外部 head；本地通过、PR 创建、CI 和用户合入分别记 C/P/G 结果。
- [ ] **G：审查。** 当前候选取得独立 Standards / Spec、必需 CI 和原 AC 逐项证据；本票完成不关闭 #14/#17/#95，不宣称多来源/T2/T3 或完整 v1。

## 非范围与证据边界

本票不授予新的账户消费或 GitHub 权限，不实现自动 merge、自动换源、完整来源资格或恢复/升级策略。固定 Reviewer 脚本、模型自述、静态 diff 或 PR 页面存在不能替代当前 Candidate 的 C/P/G 证据。

## 交接

完成后将 Candidate、checks、Review、diff/head/PR 和失败/unknown 收据回填 #14/#17/#95/#116；真实来源资格沿 #113/#107 等原责任票独立验收。
