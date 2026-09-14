# M1–M4 与 Reviewer 原 Issue

本目录保留 2026-09-05 起发布的原 v1 范围及后续 Reviewer 切片。它不包含 r6/r7 的完整新增需求，也不维护当前领取顺序；活动入口见[业务顺序](../business-first.md)和[设计治理](../design-governance-20260909.md)。

| 历史阶段 | 原正文 |
|---|---|
| M1：项目、规划、串行候选与交付 | [01](issues/m1-01.md)、[02](issues/m1-02.md)、[03](issues/m1-03.md)、[04](issues/m1-04.md)、[05](issues/m1-05.md)、[06](issues/m1-06.md) |
| M2：并行与分别接入来源 | [01](issues/m2-01.md)、[02](issues/m2-02.md)、[03](issues/m2-03.md)、[04](issues/m2-04.md)、[05](issues/m2-05.md)、[06](issues/m2-06.md) |
| M3：规则、容量、配额与换源 | [01](issues/m3-01.md)、[02](issues/m3-02.md)、[03](issues/m3-03.md)、[04](issues/m3-04.md)、[05](issues/m3-05.md) |
| M4：恢复、维护与原 v1 出口 | [01](issues/m4-01.md)、[02](issues/m4-02.md)、[03](issues/m4-03.md) |
| Reviewer 后续切片 | [输出解析](issues/m3-review-output-parser.md)、[机制资格](issues/m3-reviewer-qualification.md)、[官方资格](issues/m3-reviewer-qualification-official.md) |

阶段标题仅帮助检索；具体原范围及 AC 以各正文为准。[任务总表](../v1-backlog.md)保留较长的原映射。

| 记录 | 解释 |
|---|---|
| [publication-plan.json](publication-plan.json) | 发布前准备稿，`prepared_not_published` 是该稿的历史状态 |
| [github-publication.json](github-publication.json) | 当次实际发布编号及关系核验 |
| [review-output-parser-publication.json](review-output-parser-publication.json) | 输出解析切片的原候选、正文摘要及证据 |
| [reviewer-qualification-publication.json](reviewer-qualification-publication.json) | Reviewer 资格切片的原发布记录 |

上述快照保留原字节和摘要，不以改 JSON 状态或给 Issue 正文加头的方式更新进展。重新实现或验收时先读实际 GitHub Issue 与当前候选；固定代码交付模板的原 PR gate 继续保留，不能用新 report/patch 流程完成旧 PR 范围。返回[规划导航](../README.md)。
