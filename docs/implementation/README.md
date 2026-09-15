# 实现说明、操作入口与证据导航

分类核对：2026-09-14。本目录保存按切片形成的实现说明、运行方式、审查与失败记录；文件前缀 M0/M1/M2/M3/DG 是历史归属，不是今天的开发顺序，也不代表当前产品阶段全部完成。

当前设计看[架构](../architecture/README.md)和[工作台 PRD](../prd/commander-workbench.md)，下一项工作看[业务顺序](../planning/business-first.md)。本文只索引现有记录，不把旧 passed 更新为新候选通过；记录中的接口、命令与“下一步”须结合其固定 commit、OS、Profile、来源及原 AC 理解。

## 开发检查与汇总审计

| 材料 | 用途与限制 |
|---|---|
| [测试与合并质量门](testing-gates.md) | 开发验证入口；执行前以当前仓库配置核对命令及 gate |
| [需求覆盖审计](requirement-coverage.md) | FR/AC/A 与 C/U/P/S/G 的既有证据映射；其更新日期和增量不是实时全仓结论 |
| [Issue 管理审计](issue-management-audit.md) | 原范围验收与关票证据；实时状态仍以 GitHub 为准 |
| [首批审查](initial-review.md)、[执行切片审查](m0-execution-review.md)、[运行时与隔离审查](m0-runtime-review.md) | 原候选的独立审查与限制 |
| [CI 预算](ci-quality-budget.md)、[DG01 timeout 修复](dg01-opencode-timeout-ci.md)、[tokenizer 准备](go-tokenizer-provision.md) | 固定问题及相应操作说明，不是当前 CI 通过证明 |

## M0 执行与隔离原语

[探针契约](m0-contract.md)、[RunnerHost](m0-runnerhost.md)、[资源与未知发送](m0-resource-broker.md)、[订阅协议](m0-subscription.md)、[API runner](m0-api-runner.md)、[隔离 canary](m0-isolation.md)。各自只证明记录的本机或协议子集；原来源不等于新的 CLIProxyAPI 路径。

## 项目、Run、工作台与交付切片

[项目登记](m1-project-registry.md)、[本地工作台](m1-local-workbench.md)、[规划与人工交接](m1-run-planning.md)、[Run 工作台](m1-run-workbench.md)、[串行协调](m1-serial-orchestration.md)、[候选验证](m1-candidate-validation.md)、[独立交付协议](m1-delivery.md)、[Run v2 路由授权](m3-run-routing-authorization.md)、[v2 计划审阅与批准](m3-v2-approval-workbench.md)。

这些历史界面/领域说明不代表当前 Commander Hub、可配置 Workflow、真实部署或授权角色动态调度已经实现。新旧范围关系见[治理](../planning/design-governance-20260909.md)，当前代码与候选另外核对。

## 规划输入、执行与资格

| 范围 | 现有记录 |
|---|---|
| 规划内容与持久执行 | [输出解析](planning-output-parser.md)、[模型输入](planning-model-input.md)、[执行控制器](planning-execution-controller.md)、[业务 Relay](dg01-business-relay.md) |
| 仓库输入 | [仓库快照](planning-repository-snapshot.md)、[第四次修复](planning-repository-snapshot-fourth-repair.md)、[第五次](planning-repository-snapshot-fifth-repair.md)、[第六次](planning-repository-snapshot-sixth-repair.md)、[第七次](planning-repository-snapshot-seventh-repair.md)、[第八次](planning-repository-snapshot-eighth-repair.md) |
| Commander 资格接线 | [资格 Relay/Journal](go-commander-qualification-relay.md)、[store/source](commander-qualification-store-source.md)、[#147 producer 证据](commander147-producer-evidence.md) |

连续修复文档保留各次反例和候选，不能只读文件名顺序就推断最新资格已通过。parser、controller、transport、真实角色资格和实际业务 Plan 是不同验收层。

## 规则、资源与批准来源

| 范围 | 现有记录 |
|---|---|
| 外置网关目录 | [R8-P1-01 网关目录](r8-phase1-gateway.md) |
| Workflow 配置包与同源预览 | [R8-P1-02 配置包与同源编译预览](r8-phase1-workflows.md) |
| Workflow 部署物化、可信加载与 active 槽位 | [R8-P1-03 部署加载与条件激活](r8-phase1-deployment.md) |
| 规则版本及工作台 | [Rulebook 路由](m3-rulebook-routing.md)、[版本发布](m3-rulebook-publication.md)、[路由模拟](m3-routing-workbench.md)、[资源工作台](m3-resource-workbench.md) |
| 资格与绑定 | [Profile 资格](m3-profile-qualification.md)、[资格集合](profile-membership.md)、[批准 Run 路由](m3-approved-run-routing.md) |
| 容量与估计 | [共享容量](m3-shared-capacity.md)、[容量事实](m3-capacity-facts.md)、[容量与路由准入](m3-capacity-routing-admission.md)、[Attempt 估计](m3-attempt-estimates.md) |
| 任务准入 | [持久预留](m3-task-admission.md)、[启动前复查](m3-task-startup-guards.md)、[批准工作区](m3-approved-task-workspace.md) |
| 历史实施安排 | [Rulebook 发布计划](m3-rulebook-publication-plan.md)仅解释当时接线安排，不是当前队列 |

## 固定来源与 Go Task

| 范围 | 现有记录 |
|---|---|
| 原来源适配 | [Claude 离线边界](m2-claude-boundary.md)、[DeepSeek 离线适配](m2-deepseek-offline.md)、[Go 实际诊断](m2-opencode-go-live.md)、[Go 固定隔离](m2-opencode-go-isolated.md) |
| Go 场景资格 | [持久 Profile 观察](m3-go-profile-qualification.md)、[投影资格](m3-go-projected-qualification.md) |
| Go Task 执行 | [执行基础](m3-go-task-execution-foundation.md)、[固定执行入口](m3-go-task-execution.md)、[上下文计量](m3-go-task-context.md)、[停止后捕获](m3-go-task-capture.md) |

原授权与 scope 不扩展到任意 Task、模型、系统或网关。固定场景成功不自动满足当前 Planning、Worker、Reviewer 的全部真实资格；未运行、失败与 unknown 保留原事实。

## Candidate、Checks 与 Reviewer

[全部 Candidate Checks](m3-candidate-checks.md)、[固定 Check 进程入口](m3-candidate-checks-runtime.md)、[验证版本交接](candidate-validation-subject.md)、[Reviewer candidate rebind](m3-reviewer-candidate-rebind.md)、[Reviewer 输入](reviewer-input.md)、[Reviewer 执行意图](reviewer-execution-intent.md)、[#143 执行意图](reviewer-execution-intent-issue-143.md)、[审查输出解析](reviewer-output-parser.md)、[固定只读资格](go-readonly-reviewer-qualification.md)。

每份 evidence 必须绑定其候选/head、完整检查及实际作者/Reviewer 身份；组件存在或历史开发 PR 合入不能替代产品当前候选的独立审查与远端交付。

## 维护方式

本次分类只新增导航，不改这些证据正文。操作说明需要随实现更新时，保留原证据和版本边界并追加明确修订；发布状态从原 Issue/PR 读回。[规划快照导航](../planning/README.md)保留原任务与 publication hash，[研究归档](../../outputs/README.md)保留设计来源。新 r6/r7/r8 能力另取实现与资格证据，不回填旧文档制造已完成状态。
