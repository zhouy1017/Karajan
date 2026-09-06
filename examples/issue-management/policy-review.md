# Issue 管理流程独立审查

日期：2026-09-06。基线：`d903fa9b850d292b44016cd2321e69d8100ec2fe`；审查对象为当前管理变更中的 `AGENTS.md`、`docs/agents/issue-tracker.md`、`.github/pull_request_template.md`、`.github/ISSUE_TEMPLATE/implementation-task.yml` 及 `.github/workflows/ci.yml` 五个文件。审查时尚未提交；逐文件 SHA-256 记录于 [policy-validation.json](policy-validation.json)，最终候选应核对这些文件内容一致。

## Standards

**0 findings。** 独立 Standards 审查确认，变更保留原验收范围、父票所有必需条件、证据层级和 GitHub 唯一状态来源，符合既有需求审计及质量门约定。当前候选 CI、独立审查和影响范围补验仍是要求。自动验收与关票限定于本次用户授权，没有新增任意功能 PR 合并或付费调用权限。未发现适用于这些文档与模板的实质代码气味。

## Spec

**0 findings。** 独立 Spec 审查确认，流程要求原验收条件全部有归属、已完成子票独立关闭、父票保留未完成要求。Closing 仅用于完整满足范围的任务，合并前必须核对当前候选证据；未合并保持 Open，合并后核对远端并回填完成、剩余及阻塞。Agent 入口、PR 模板及任务表单覆盖用户要求的后续执行流程。

## 结构与增量核验

- `git diff --check` 通过。
- PyYAML 6.0.3 解析 Issue 表单通过；5 个字段 ID 唯一且字符合法，顶层必需字段与类型正确，4 个范围/验收字段设置布尔型 required。验收内容保留 Markdown 编辑与清单语义。字段定义符合 [Issue form 顶层语法](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms) 与 [GitHub form schema](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema)。
- 解析 CI 前后对象并移除新增的 `dev` push 分支后，两者完全相同：没有删除检查、扩大 workflow 权限、修改矩阵、放宽退出码或汇总门；PR 与 merge_group 触发保持不变。
- 三份 Markdown 中的本地文件链接全部存在。
- Closing 在合入默认分支时生效的流程与 [GitHub 官方说明](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue) 一致。

本报告验证文件内容和结构，不代替最终提交的远端 CI，也不声称已核验远端默认分支、保护规则、模板在线渲染或具体 Issue 关闭状态。审查阶段没有执行模型或付费调用，没有修改实现文件或 GitHub。

结论：Standards 0 项、Spec 0 项实质问题；该文件集合可进入当前候选的发布与远端 CI 流程。
