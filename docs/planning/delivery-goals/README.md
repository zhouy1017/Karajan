# 历史 Delivery Goals 范围映射

当前开发入口是 [当前业务顺序](../business-first.md)，Issue 状态和依赖遵循 [Issue 跟踪流程](../../agents/issue-tracker.md)。本目录保留历史阶段的原范围、原 AC、证据和安排；这些文档不再提供自动串行启动 prompt，也不覆盖当前业务顺序。

| 历史阶段 | 保留文档 | 原范围映射 |
|---|---|---|
| DG00 | [复审](00-review.md) | 流程与既有候选修复 |
| DG01 | [执行](01-execution.md) | Planning／Reviewer C/P 接线 |
| DG02 | [Go 业务](02-go-business.md) | Go 资格与真实业务链 |
| DG03 | [首条交付](03-first-delivery.md) | 首条完整 PR、修订和工作台 |
| DG04 | [并行与来源](04-parallel-sources.md) | 2–3 子任务与五来源 |
| DG05 | [规则与资源](05-policy-resources.md) | Rulebook、资源、换源与人工交接 |
| DG06 | [恢复与维护](06-recovery-maintenance.md) | 故障恢复、备份、升级与保留 |
| DG07 | [v1 出口](07-v1-exit.md) | 全量原需求与最终证据 |

原 Issue 正文和验收清单继续有效；阶段编号仅用于历史范围映射。重新执行其中任何范围时，先从当前入口选取仍属于主线的最小可观察切片，并回到 Issue 流程记录依赖、证据和限制。

覆盖关系仍见 [需求与完成证据审计](../../implementation/requirement-coverage.md)。
