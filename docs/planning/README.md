# 开发规划与历史范围导航

分类核对：2026-09-16，活动设计为 r9。本页提供文档导航，不复制领取队列或推断 GitHub 当前状态。

## 当前入口

| 要做的事 | 入口 |
|---|---|
| 选择开发任务、恢复工作 | [当前业务顺序](business-first.md) |
| 全量开发责任、验收覆盖、Issue 标识与待决项 | [r9 开发就绪审核](r9-development-readiness-20260916/README.md) |
| 判断 r9、r8、r7、r6 与旧范围的承接 | [设计治理与承接矩阵](design-governance-20260909.md) |
| 理解用户行为与界面 | [Commander Workbench PRD](../prd/commander-workbench.md) |
| 实现对话设计、同源图表与真实部署 | [对话式 Workflow 部署契约](../architecture/10-conversational-workflow-deployment.md) |
| 实现授权角色的动态拆分与调度 | [角色调度契约](../architecture/11-role-directed-scheduling.md) |
| 验收原 Issue、候选或合并结果 | [Issue 跟踪流程](../agents/issue-tracker.md)、[实现证据导航](../implementation/README.md) |

当前安排由业务顺序与 r9 就绪清单承接。M0–M4、DG 和已发布 P1–P4 正文保留原范围，不能作为 r9 的完整任务清单；新增行为按活动治理承接。历史文件中的“当前”“下一步”“已派发”、模型分工及权限描述，只指其记录时点，不触发今天的执行或新增授权。

## 历史路线、交接与批次

| 材料 | 分类与保留用途 |
|---|---|
| [M0–M4 路线](roadmap.md)、[v1 原任务清单](v1-backlog.md) | 2026-09-05 起的阶段范围、原出口与依赖；保留正文，当前排期已迁移 |
| [r8 第一阶段](r8-phase1-20260914/README.md) | #174–#178 和四个 PR 已完成的控制面批次；原 worker、模型安排和合并授权不延伸到新批次 |
| [M0 Issue 原文](m0/README.md) | 原探针任务与发布编号 |
| [M1–M4 Issue 原文](v1/README.md) | 原 v1 子票及 Reviewer 后续切片；发布准备稿与已发布结果分开 |
| [DG00–DG07](delivery-goals/README.md) | 历史交付目标映射，原文已标记为历史，不再串行启动 |
| [2026-09-07 编码批次](worker-dispatch-20260907/README.md) | Planning / Reviewer 与 tokenizer 子票正文、当时关系核验 |
| [2026-09-07 CI 修复](ci-recovery-20260907/README.md) | 固定候选的 CI 预算与恢复竞态问题 |
| [2026-09-07 资格驱动](qualification-driver-20260907/README.md) | 固定资格场景的恢复子票 |
| [2026-09-08 复审](review-20260908.md) | 原候选、反例、修复与审查证据；不代表当前全仓结论 |
| [旧 Commander 交接入口](commander-handoff-20260908.md) | 兼容既有路径与锚点，已转向当前业务顺序 |
| [2026-09-09 工作台子票](commander-workbench-20260909/README.md) | #159–#162 的原发布范围，不能自动覆盖 r6/r7/r8 |
| [Go 固定资格与后续安排](go-runtime-qualification-next.md) | 2026-09-06 固定 scope 的证据与当时下一切片；非当前来源资格或派发结论 |

## 根目录 Issue 正文快照

以下原文就地保留；即使写有状态或分工，也不能当作实时队列。正文与对应发布 JSON 的摘要、编号和关系不在文档整理时重写。

| 历史范围 | 原正文 | 发布或关系记录 |
|---|---|---|
| 初始 CI 门 | [CI 任务](ci-initial-quality-gate.md) | [M0 发布记录](m0/github-publication.json)、[v1 发布记录](v1/github-publication.json)；当前开发检查入口见[测试门](../implementation/testing-gates.md) |
| Go 投影资格 | [投影资格](go-projected-qualification-issue.md) | 原 Parent / Related 保留，后续入口见下一行 |
| Go Task 执行 | [基础接口](go-task-execution-foundation-issue.md)、[执行接线](go-task-execution-issue.md) | [发布记录](go-task-execution-publication.json) |
| Commander / Candidate | [规划桥](commander-planning-bridge-issue.md)、[全部 Checks](candidate-checks-issue.md)、[Reviewer](candidate-reviewer-issue.md) | [发布记录](candidate-validation-publication.json) |
| Reviewer / 验证版本 | [候选 rebind](reviewer-candidate-rebind-issue.md)、[资格集合](profile-membership-issue.md)、[批准绑定](approved-reviewer-binding-issue.md)、[验证版本交接](candidate-subject-transition-issue.md) | [rebind 发布](reviewer-candidate-rebind-publication.json)、[绑定切片发布](reviewer-binding-slices-publication.json) |
| 工作台业务简报 | [#153 原文](business-first-issue.md) | [2026-09-08 发布记录](business-first-publication.json)；“唯一主线”仅指当时安排 |

独立候选和 CI 快照另有 [Checks PR](candidate-checks-pr-publication.json)、[Checks CI](candidate-checks-ci-observation.json)、[Reviewer subject PR](reviewer-subject-pr-publication.json)；其中 Open/Draft/通过等字段只属于记录的 commit。工作台设计的 [2026-09-09 发布清单](design-publication-20260909.json) 同样保留旧 PRD revision 与正文 hash，不因当前 PRD 已更新而改写。

## 维护边界

新的产品行为进入活动 PRD/架构与治理；新的实施事实进入绑定 commit 的证据。历史 Issue 正文、发布清单、关系核验和研究记录不承接今天的排期更新。需要改原范围时按 Issue 流程发布新修订或承接票，不能编辑快照制造已发布事实。原始选型与访谈见 [研究归档](../../outputs/README.md)。
