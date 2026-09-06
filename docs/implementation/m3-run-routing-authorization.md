# Run v2：完整路由授权的持久批准入口

本切片把已批准 Run 接入统一调度所需的任务/授权资料先落实到真实领域命令和 SQLite。它不是实际路由、准入或执行接线：`dispatch_enabled=false`，旧 Serial 选择器明确拒绝 v2。没有启用账户，没有真实模型或现金请求。本步没有新增 Web 页面或 v2 专属 HTTP 入口；现有通用 HTTP 转发不构成 v2 页面验收，旧 UI 的 v1 批准格式不能批准 v2 Run。

## 公开命令

- `ProjectRegistry.register_execution_policy(project_id, document, command_key, principal)`：项目 owner 固定 `karajan.execution-policy.v1`。文档含 id/revision、实际 configuration_digest、项目硬约束、风险映射/路径下限、channel→destination、工具的权限映射、上下文输入上界记账定义/输出预留和最大上下文。缺字段不能补 fixture 默认值。返回正文、正文 digest、项目身份、登记者、时间；重放返回原记录。
- `ProjectRegistry.get_execution_policy(project_id, id, revision, principal)`：按精确项目/版本读取已登记正文。记录不能覆盖，revision 从 1 连续增加；risk/tool/context 子政策在同项目内复用同 id/revision 时必须是同正文。
- `RunPlanner.create`：显式 `schema_version=karajan.create-run.v2`，传入精确 execution_policy 的 id/revision/digest 和完整 v2 Authorization。服务从真实 Registry 读记录，核对 owner、配置和正文 digest，固定完整 policy snapshot。v1 原请求和摘要分支保持原语义。
- `RunPlanner.submit_plan`：显式 `schema_version=karajan.submit-plan.v2`，沿用当前 Commander principal/term、planning admission receipt、expected_plan_revision 与幂等约束。Task 必填 purpose、domains、额外能力、tools、context_tokens、duration_seconds。
- `RunPlanner.approve_plan`：显式 `schema_version=karajan.approve-plan.v2`，沿用 plan/configuration/authorization/term 摘要并增加 routing_digest。仅 owner 可批准最新精确计划。`get`、重启后的读取和命令重放返回同一持久记录。

## 授权和绑定

Authorization v2 保留 Profile、路径、预算引用、检查和交付范围，补充允许通道、工具、数据去向、必需能力、tool_sandboxed、原币限额、Attempt 时长、质量轮数和逐规则 stage_permissions。normal 是显式布尔许可；quality_indices 指向固定规则的有序升级组。

服务从 Run 原配置的 Rulebook、原 ceiling 与计划 Profile 交集生成 `routing_binding.stage_grants`。例如 bounded-worker 的 normal 绑定 standard_qualified 的精确 Profile revisions；quality index 0 绑定 critical_qualified 及其精确成员。调用者不能直接提交 expanded groups 作为已批准成员。规则正文 hash、政策正文 hash、项目/登记者、原 ceiling hash 和任务完整执行需求一并进入 routing_binding；authorization_digest 和 plan_digest 覆盖此绑定。新规则发布和新政策版本不会重写旧 Run。

工具/通道/数据去向/Profiles/stage 许可只可在原 ceiling 内收窄；新币种或现金金额不能越过原 ceiling 与配置预算。必需能力和原 checks 不得移除，Attempt 时长和质量轮数不得提高。Task 的输入 token 上界加政策输出预留必须在 policy 最大上下文内，任务时长在批准 Attempt 上限内。修改任何 Task 内容必须使用新 Task revision。

上下文 `explicit_approved_upper_bound` 是批准的请求范围，不是已测量模型 token；工具映射也是 owner 固定的权限要求，不是执行器资格。后续打包器/Runner 仍须核验实际内容、tokenizer、工具和权限；这里只保证它们没有在批准后被默认值替换。

## 兼容与下一步

v1 数据和历史 receipt 不做重写或重新签名，不能用 v2 命令把 v1 Run 原地升级，也不能用 v1 approval 漏掉 routing_digest 后批准 v2。旧 Serial fixture 不能解析并执行 v2 授权，返回 `APPROVED_ROUTING_INTEGRATION_REQUIRED`，没有 Attempt、workspace 或进程。

后续仍需从这些已批准记录组装完整 TaskSnapshot/PolicySnapshot，接入真实资格/需求估计及 capacity_facts，再用同一个 evaluate_route 驱动实际资源准入、RouteDecision、Attempt 和启动 outbox；在这之前不关闭统一调度验收。旧 Run 的显式升级、policy adoption、active Attempt reconciliation 也不在本步实现。

## 验证

测试从公开 Registry/RunPlanner 命令创建本地 Git 项目和 SQLite，使用明确标为 fixture 的规划准入回执，提交/批准后重新打开服务读回。`tests/runs/test_routing_authorization.py` 覆盖完整链、owner/版本/正文绑定、权限收窄、stage序号、需求改版、陈旧批准、v1回放和旧Coordinator拒绝。

实际开发红绿：最初缺公开登记入口；工具/上下文只有名称时错误被接受；嵌套 risk 同 revision 的不同正文错误被接受；旧 fixture Coordinator 错误将已批准 v2 加入队列。上述原测试输入及 before/after XML 在本工作树 `.cache/` 保存。其余边界验证直接通过，不宣称发生过额外红灯。最终运行数量由冻结报告记录。

独立审查修复两项边界错误：非对象 JSON 在版本分流前误抛 AttributeError，现在交给原严格模型返回稳定领域错误；超大 execution_policy.revision 原来进入 SQLite 抛 OverflowError，并使真实认证 HTTP 返回 500，现在引用与登记政策共用 1–1,000,000,000 的严格整数范围，在数据库调用前拒绝，HTTP 返回 422。独立原输入与 before/after 记录保存于 `examples/run-routing-authorization/`；作者还覆盖 null、字符串和临界上限，未修改独立用例。

修复后 Windows 的 Run、Project 与完整 Web 回归共 249 项通过；其中新增领域专项为 34 项。独立 Spec 23 项、Standards 5 项通过，两个发现均以原输入复验关闭；Ruff 全仓及 mypy 92 个源文件通过。实际 XML、独立报告和最终源码绑定见 `examples/run-routing-authorization/author-freeze.json`。审查前的含 Orchestration 回归为 250 通过/1 POSIX 跳过，作为先前执行记录保留，不混称修复后的重复长测或 WSL 结果。
