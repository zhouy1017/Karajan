# Karajan 开发到交付 Goals

Revision：2026-09-08。本轮交付是复审、修复和以下阶段任务包；阶段文档存在不代表后续实现已启动。`DG` 是执行分组，区别于 PRD 目标 G1–G5 和 GitHub 证据层级 G。原 M0–M4、父 Issue 和验收清单继续有效。

## 启动方式

将下表一个 prompt 交给 Commander。开始前读 [交接执行约定](../commander-handoff-20260908.md)、[Issue 流程](../../agents/issue-tracker.md) 和所选阶段文档；只加载相关源码与原 Issue AC。实际远端状态优先于日期快照。保持已有会话的授权边界，文档存入仓库本身不增加权限。

| Goal / 文档 | 简短 prompt | 可核验结果 |
|---|---|---|
| [DG00 复审修复](00-review.md) | 将 DG00 设为当前 goal，按 docs/planning/delivery-goals/00-review.md 修复复审问题并完成当前候选验收。 | 两项 P1、流程漂移有修复及当前证据 |
| [DG01 执行接线](01-execution.md) | 将 DG01 设为当前 goal，按 docs/planning/delivery-goals/01-execution.md 完成 Planning／Reviewer 的可信 C/P 执行闭环。 | 真实本机执行、输入/输出权威、取消恢复接通 |
| [DG02 Go 真实业务](02-go-business.md) | 将 DG02 设为当前 goal，按 docs/planning/delivery-goals/02-go-business.md 完成 Go 资格与真实规划、批准、候选、审查链。 | 固定来源的有界 S 与当前业务证据 |
| [DG03 首条完整 PR](03-first-delivery.md) | 将 DG03 设为当前 goal，按 docs/planning/delivery-goals/03-first-delivery.md 完成跨来源串行 PR、计划修订和可恢复工作台。 | #11–#16 原范围及真实同一 PR |
| [DG04 并行与五来源](04-parallel-sources.md) | 将 DG04 设为当前 goal，按 docs/planning/delivery-goals/04-parallel-sources.md 推进产品并行与五来源；阻塞只限定相应真实路径。 | 2–3 子任务组合验证，五来源分别验收 |
| [DG05 规则和资源](05-policy-resources.md) | 将 DG05 设为当前 goal，按 docs/planning/delivery-goals/05-policy-resources.md 完成规则、共享资源、有界换源和人工交接。 | #23–#27 完整产品行为 |
| [DG06 恢复和维护](06-recovery-maintenance.md) | 将 DG06 设为当前 goal，按 docs/planning/delivery-goals/06-recovery-maintenance.md 完成故障恢复、备份升级与受管保留。 | #28/#29 实际故障与维护证据 |
| [DG07 v1 出口](07-v1-exit.md) | 将 DG07 设为当前 goal，按 docs/planning/delivery-goals/07-v1-exit.md 核验全部原需求并补齐交付证据，仅在完整出口满足时完成 goal。 | #30/#1 全量验收；否则准确列出缺口 |

如需一次授权持续推进，可复制：

> 按 docs/planning/delivery-goals/README.md 从首个未完成 DG 持续推进至 DG07，沿用 commander-handoff-20260908.md 的角色和消费边界。我授权在本仓库已确认需求内拆 Issue、创建 worktree、提交、push、准备 PR，并在精确候选通过原 AC、双轴独立审查和必需 CI 后合入 dev、核对关票。每个阶段完成后设置下一个阶段 goal；只对实际新输入或具体产品批准保留阻塞，继续其他可执行工作。

该段是供用户下达的启动 prompt；本轮编写它不自动启动 DG01–DG07，不授权 `main` 发布、部署、现金 API 或产品替用户合并 PR。

## 依赖与并行

阶段编号是优先顺序和验收分组，不是全局串行锁。用 Issue 原生 blocked-by 表达叶子真正的前置；父票依赖表示完整验收依赖，不能把它当成所有离线开发的阻塞。

- DG01 内 Planning 与 Reviewer 的独占模块并行；同一共享模块只有一个作者。先冻结实际 producer/consumer，再派消费接口的 worker。
- DG02 的 #107 资格预检可与 DG01 并行，不依赖 #116 业务执行。Commander qualification producer 的 C/P 前置在 DG01 实现，避免首次探测要求已有 Commander 资格。
- DG04 的 #18/#19 官方订阅预检、协议/隔离实现及满足前置的有界 S 从 DG01 期间开始。DG03 的跨来源 #13 验收消费其中至少一个当前合格来源；不用等待 DG04 全部完成。
- DG03 的 #15 可在 #13 稳定后与 #14 并行；#16 消费二者。DG04 的产品并行 #17 等待 #14/#15 所需接口。
- DG05 的独立 Rulebook、资源和计量剩余切片可提前做；#26 最终组合依赖 #17/#23/#24/#25，#27 依赖 #23/#24。预算、权限、取消底线从第一次调用就适用。
- DG06 的 #28/#29 可并行。DG07 的覆盖核对、性能量测方案和缺口拆票可提前，最终通过须等所有必需前置。

任何阶段阻塞都记录具体叶子、缺少的输入、已尝试事实和解除条件；继续独立叶子。只有目标本身全部满足才把该 goal 标为 complete。创建了 PR、派发了 worker、CI 正在运行均不是完成；`not_run / failed / unsupported / unknown` 的必需项不能改成 passed。

## 原范围归属

| 原责任票 | 主要收尾 Goal | 说明 |
|---|---|---|
| #110/#114 | DG00 | 恢复原 AC 的修复；父 #93/#95 保留 |
| #112/#116 | DG01 | C/P；不借本地夹具产生资格 |
| #107/#113/#117 | DG02 | 各自限定 S；#93/#95 剩余原 AC 仍逐项核对 |
| #11–#16，#93/#95 其余 M1 范围 | DG03 | 含顾问、最小人工交接、两来源和用户操作 |
| #6/#7/#8，#17–#22，#87 相关剩余 | DG04 | M0 保留项与五来源；已合并子集只补缺口 |
| #23–#27 | DG05 | 当前政策、根链累计与完整人工交接 |
| #28/#29 | DG06 | 正常恢复与历史恢复分别验收 |
| #30/#1 | DG07 | 全量出口，复核全部适用证据 |
| #9 | 可选 | 不进入必需出口 |

[覆盖审计](../../implementation/requirement-coverage.md) 已把 FR01–20、PRD-AC01–06、A01–26、D01–09、G1–G5、US01–28 和 NF01–05 映射到原责任票。上表再将责任票映射到 DG；阶段验收必须读原行和原 Issue 正文全部条件，不能只按阶段摘要验收或复制一套更宽松的 AC。

## 每阶段的交接产物

在对应 Issue 维护唯一当前状态；本地提交可恢复规格/证据索引，避免多份实时状态表。每批至少记录：

1. 原 AC → 叶子 Issue → 实现 SHA → 命令/固定输入/实际输出 → C/U/P/S/G 结果及限制。
2. 基线、当前 candidate/head、PR/base、两轴独立审查报告、当前 CI 和合入 `dev` 的读回证据。
3. 已完成、剩余、阻塞；当前运行中的 worker/分支/独占文件、下一批实际可领取的叶子与解除条件。

单阶段启动时完成该阶段并交接下一条 prompt；连续启动模式下仅在当前 goal 已完成后切换下一个。一个任务只维护一个当前活动 goal，其他阶段保存在这些文档中；并行 worker 的叶子完成不替代父阶段完成。

连续模式中，当前阶段有真实阻塞而后面仍有独立工作时，保留当前 goal 未完成，按已有连续授权推进后续可执行叶子并记录其原 DG 归属。先收拢这些工作，待阻塞解除再完成原 goal；不会为切换工具状态把受阻阶段假标 complete，也不会在已有未完成 goal 时重复创建另一个。goal 工具的 blocked 状态仅按运行环境规定使用，Issue 的实际 blocker 立即如实记录。
