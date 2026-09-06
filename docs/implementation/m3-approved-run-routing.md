# 从已批准 Run 生成路由判断

`ApprovedRunRouting.assess(run_id, task_id, principal=..., command_key=...)` 从实际持久 Run 读取当前批准计划，取得冻结执行政策、Rulebook、逐规则分发许可、任务要求和批准收据，再读取当前资源目录、控制器资格、显式任务估计和共享容量账本，调用统一 `evaluate_route()` 并保存结果。

外部调用只提供 Run 和任务 ID。认证 HTTP 入口为 `POST /v1/runs/{run_id}/tasks/{task_id}/routing-assessments`，正文必须是 `{}`，需要现有会话、Origin、CSRF 和 Idempotency-Key。`GET /v1/runs/{run_id}/routing-assessments/{assessment_id}` 读取原收据。客户端不能提交 Profile、作者、授权、政策、容量快照或升级阶段来替换真实来源。

该入口目前是**持久路由判断**：处理已批准的、没有前置依赖的 Worker 正常阶段，生成 planned Attempt/context 身份，但不创建容量预留、不启动执行器、不启用 Profile，也不更改 Run 的 `dispatch_enabled`。Reviewer、依赖任务、Commander 和质量修复的正式消费仍需执行历史接线。即便 `state=selected`，也不是执行许可。

## 输入与判断

1. 当前 active plan 必须有精确匹配的批准收据。尚未批准的新 proposal 不覆盖 active plan；Commander 交接本身不废止先前批准工作。
2. `select_rule()` 先按照任务就绪状态、复杂度、风险路径下限和唯一最高优先级确定规则。它与完整 evaluator 共用同一选择实现；不要求先提供阶段授权、资格或容量。
3. 只从选中规则的 `routing_binding.stage_grants` 提取正常组、质量组及允许的索引。另一个规则获批的同名组或相同索引不会合并进来。
4. TaskSnapshot 保留批准输入上下文上界和执行时限，并单独携带执行政策的 `reserved_output_tokens`。资格中的总上下文必须覆盖两者之和；v1 模拟材料省略该新字段时按 0 处理。求解器版本变为 `karajan.routing.lexicographic.v2`。
5. 当前 Profile、账户、通道与完整池绑定必须保持一致。配置中的能力 `passed` 被清除，只有资格服务返回的能力观察可供判断。实际数据去向还必须等于该通道在冻结执行政策中的精确映射，不能因另一个去向也在全局允许集合而替换。
6. [资格 Store](m3-profile-qualification.md) 的当前真实 `runtime_tools` 来源仍未实现，返回 `RUNTIME_TOOLS_NOT_QUALIFIED`。固定三进程 fixture 的成功不变成模型能力、上下文容量或工具沙箱资格。
7. [Attempt Estimate Store](m3-attempt-estimates.md) 读取 owner 对精确批准任务/Profile 登记的完整池预测。没有登记则保持缺失；所有现有声明预测为 unknown，不填默认调用数、token 数或完成时间。
8. [容量事实](m3-capacity-facts.md) 映射当前全局政策、所有 Run 占用、最新观测、未覆盖消费和未来预留。无置信来源时保持 unknown；旧数字观测不补最新 unknown，没有观测不制造窗口。账号现金余额、Run 现金余额、价格与汇率均保持缺失。

收据保存三个实际输入快照、候选与拒绝理由、批准/政策/目录摘要、资格和估计来源，以及容量来源摘要。估计存在时同时保存完整期望窗口、政策 revision 和 lead reserve 限制，供后续 [真实容量准入](m3-capacity-routing-admission.md) 消费。

## 事务与重放

Run 数据库事务稳定 active plan、批准收据、幂等和保存结果；其内的项目资格 guard 阻止目录、资格、估计登记及撤销并发变化。估计读取复用外层项目锁，不嵌套同库写事务。容量使用独立只读快照；这些锁不构成跨库原子准入。

重复 command key 返回原收据。撤销后重放仍能查看原判断，重新判断必须使用新 command key；任何收据都不能替代 effect 前的最新授权和容量复查。下一步执行接线必须使用可恢复 admission 意图处理跨库崩溃，而不能直接把 `selected_profile` 交给 Host。

## 自定义 Rulebook 的修复

合法地重命名 Commander 或顾问组原先会在创建 v2 Run 时触发内置组名 KeyError。现在 v2 参与者从角色与用途相符规则的正常候选组解析；质量阶段组不赋予规划参与资格。参与者被允许提出规划意图，实际规划仍需独立可信 admission receipt。v1 固定流程保持兼容，缺组返回结构化拒绝。

## 验证与剩余工作

Windows 初次相关回归为 561 passed、1 个仅 POSIX 适用项 skipped；自定义组和去向修复后，受影响 Run/Web/路由回归 248 passed，全部后端类型检查 101 个文件通过，Ruff 通过。WSL2 新增切片合计 104 passed。两项第三方依赖弃用警告和 WSL pytest 默认缓存权限警告不影响已执行测试与指定报告输出。

作者测试见 `tests/runs/test_approved_routing*.py`、`tests/routing/test_rule_selection.py`、`tests/web/test_approved_routing_http.py`，资格、估计和准入各有公共 Store 测试。真实批准 Run → 显式估计登记 → 容量观测 → 候选选择用例仅替换资格来源，明确标为 `test_double`；生产 Web 不注入它。其余真实来源判断保持 blocked。独立 Spec/Standards、原始失败与最终摘要见 [可复查记录](../../examples/approved-routing/README.md)。

尚未完成：真实服务工具资格、可信额度置信/校准、真实现金账本、消费执行历史的 Reviewer/质量阶段、正式 Coordinator 资源准入和 Host effect、多 Worker 集成与真实 PR 交付。本切片不关闭整个 M3-01、FR04、FR06 或 v1 交付任务。
