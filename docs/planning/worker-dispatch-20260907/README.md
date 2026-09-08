# 并行编码派发快照（2026-09-07）

> 历史快照：当前业务顺序以 [当前业务顺序](../business-first.md) 为唯一入口；本文件正文、`publication.json`、关系核验和 manifest hash 只用于追溯，不是当前派发命令。

本次派发以 `dev@6cf89abc1a6ca8cb99d76f82191dc7d82efeb05b`、既有开发计划和父 Issue 原验收条件为基线。GitHub Issues 是实时状态来源；本目录记录发布时的任务正文与关系，`labels_at_snapshot` 不代表读取时的当前状态。

用户指定 gpt-5.6-luna 与 gpt-5.6-terra 承担编码及各自的 CI/审查修复，gpt-6-astra high 独立审查；指挥者负责设计、依赖与验收调度。审查分别记录 Standards 和 Spec，固定候选 commit，保留失败反例；旧候选通过不替代当前候选的 CI。Issue 验收和合入流程见 [Issue 跟踪流程](../../agents/issue-tracker.md)。

## 任务与原范围归属

| 任务 | Worker | 可独立验收的范围 | 前置任务 |
|---|---|---|---|
| [#109](https://github.com/zhouy1017/Karajan/issues/109) | Luna | 严格 v1/v2 计划输出解析；内容无授权能力 | 无 |
| [#110](https://github.com/zhouy1017/Karajan/issues/110) | Terra | 持久规划控制器、原输出精确提交、只读恢复与取消边界（C） | #109；接口确定后可并行编码 |
| [#111](https://github.com/zhouy1017/Karajan/issues/111) | Terra | 真实规则、资格、预算、容量的规划前置准入 | #110 |
| [#112](https://github.com/zhouy1017/Karajan/issues/112) | Terra | 固定 Go 只读规划 transport、逐次发送约束及物理停止（C/P） | #110 |
| [#113](https://github.com/zhouy1017/Karajan/issues/113) | Terra | 官方 Commander 资格、真实规划到 owner 批准的 Worker Candidate（S） | #111、#112 |
| [#114](https://github.com/zhouy1017/Karajan/issues/114) | Luna | 从实际 Candidate CAS 与最终 Checks 编译可信 Reviewer 输入包（C） | #101、#104 |
| [#115](https://github.com/zhouy1017/Karajan/issues/115) | Terra | 批准 Reviewer Task 的真实依赖选路与独立资源生命周期 | #100、#101、#106 |
| [#116](https://github.com/zhouy1017/Karajan/issues/116) | Terra | 实际只读 Reviewer consumer、Evidence 和当前验证收据（C/P） | #114、#115 |
| [#117](https://github.com/zhouy1017/Karajan/issues/117) | Terra | 真实业务 Candidate 的 Reviewer 质量与规划整链（S） | #107、#113、#116 |
| [#120](https://github.com/zhouy1017/Karajan/issues/120) | Luna | 官方 tokenizer 准备的有限重试、总期限和脱敏错误；修复本批 CI 阻塞 | 无 |

#109–#113 是 [#93](https://github.com/zhouy1017/Karajan/issues/93) 的原生子任务：前三个实现切片与 transport 共同承担规划桥，#113 承担真实来源及批准后整链证据。#114–#117 是 [#95](https://github.com/zhouy1017/Karajan/issues/95) 的原生子任务：输入编译、准入、执行及业务质量分别验收，既有 #107 承担固定机制官方资格。父票原正文和验收清单保留，未完成条件继续由父票及对应子票持有。#120 关联现有 #10 与 PR #118/#119，不改变产品父票范围。

独立工作区按切片隔离。#109/#110 可从共同接口并行；#114/#115 可在既有依赖已合入的基线上并行。实现依赖满足后才能将后继任务提升为可执行；没有生产 authority 的 C 层控制器明确拒绝生产执行。CI 失败优先回到原级别 worker，不由 reviewer 或指挥者代写修复。

## 真实来源与交付边界

本会话已获 OpenCode Go 订阅使用授权。每次真实资格仍固定 Profile、runtime/source、suite 和请求/时间上限，记录所有失败及累计调用。源码、fixture、HTTP 成功、模型自述或另一 Profile 的资格不代替 S 证据；不能确认的远端停止保持 `unknown`。不在发布快照中保存密钥、capability、原始请求头或模型隐藏推理。

满足完整子票验收、独立审查及当前候选 CI 后，PR 可关联 `Closes`，Issue 保持 Open / `status:awaiting-merge`。合并仍按 owner 决定的现有约定执行。合入 `dev` 后再核对关票；完整 v1 和父票剩余的生产准入、transport、真实资格与端到端业务条件不会随本批离线切片自动完成。

## 快照可核对性

`publication.json` 保存实际编号、URL、正文文件的 SHA-256、执行模型和原生 blocked-by。发布时通过 GitHub API 回读 #93/#95 的 `sub_issues` 及各票的 `dependencies/blocked_by`，确认父子关系和依赖。`relation-verification.json` 保存回读结果与核对时间。

正文文件采用 UTF-8，无 BOM；本目录 `.gitattributes` 关闭快照文件的换行转换，使 Git blob 与 manifest 的字节摘要一致。快照正文中的待办状态仅反映其捕获时点；后续执行证据、阻塞、候选及 CI 结论以对应 GitHub Issue/PR 为准。
