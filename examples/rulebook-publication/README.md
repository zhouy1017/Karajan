# Rulebook 发布持久边界的本机证据

本切片沿用 `ProjectRegistry` 的项目 SQLite 数据库；所有发布、当前配置指针和命令回执在同一事务内更新。测试仅使用临时本机 Git 仓库、SQLite 和固定输入，没有模型、订阅或现金 API 调用。

## 公开行为

- `preview_rulebook(project_id, document, expected_revision=..., command_key=..., principal=...)` 保存服务器预览，绑定 owner、项目 revision、完整内容摘要、编译器身份、当前有效目录 revision/digest 和截止时间。默认有效期 300 秒。
- `publish_rulebook(project_id, preview_id, expected_revision=..., command_key=..., principal=...)` 只接受该类预览；相同命令和参数重试返回原发布回执，成功后即使预览已过期也不重复发布。并发确认由同一项目事务的 revision 比较决定唯一胜者。
- `get_rulebook` / `list_rulebook_versions` 读取不可变执行版本；`list_rulebook_publications` 读取追加发布记录。说明文字可以更新，但不改变同 id/revision 的执行摘要和已保存版本。
- 原 `preview_configuration` / `apply_configuration` 共用版本身份约束。`can_save_draft` 允许保存可导出的无效规则草稿；`can_publish` 另行判断结构、歧义和版本冲突。旧完整配置预览不取得显式发布权限。空组、资格缺失仍有诊断，发布回执固定为 `waiting_qualification`，`activation_allowed=false`。
- `get_effective_resources` 返回当前有效资源目录及批准集合；`effective_resources_guard` 在同项目写事务内保持该视图稳定，供本机启动边界复核当前限制。目录结构、引用、原币预算、已填写限额和时限必须有效；停用、撤销批准或资格不足不使合法的撤销无效。非法草稿不能覆盖最后有效目录。
- 增量迁移只从旧库已接受的配置历史恢复有效目录和版本，不从任意未应用预览取得权限。同 id/revision 的冲突历史显式标记，必须换用新版本。旧预览需要重新审阅。
- `evaluate_task` 复用固定模板离线预览；自定义矩阵返回 `ROUTING_SNAPSHOT_REQUIRED`，完整纯路由求解器仍独立强制 T3 的角色能力下限。

以上是发布持久边界子任务的范围。父切片另外接入 [Web 编辑与旧 Run 固定](../../docs/implementation/m3-rulebook-publication.md)；Run 规则采用、真实统一调度或真实 Profile 资格尚未完成。原始规则保存在项目配置，规范执行内容另存不可变版本。

## 可重跑检查

在仓库根目录使用项目 Python 环境：

```text
python -m pytest tests/projects tests/routing -q
python -m ruff check backend/karajan/projects tests/projects
python -m mypy backend/karajan/projects
```

最终 `projects-routing-final.junit.xml` 记录 **185 passed**；Ruff 通过，strict mypy 检查 6 个 projects 源文件通过。`publication-verification.json` 绑定本子任务修改的源码/测试与实际检查报告；父任务负责其他模块的联合验证。

保留的红绿证据：

- `validation-behavior-before.junit.xml`：14 failed、1 passed；10 个资源数值/引用反例、4 个坏 Unicode 反例失败，旧配置预览不能发布的现有行为通过。`validation-after.junit.xml`：15 passed。
- `validation-before.junit.xml` 是更早的完整原始输出，其中额外 1 个失败只是测试预期错误码写成了不存在的 `RULEBOOK_PREVIEW_REQUIRED`；修正测试为已有 `RULEBOOK_NOT_PUBLISHABLE` 后才记录上述行为红灯。该原始文件保留，不把测试错误计为产品缺陷。
- `identity-preview-before.junit.xml`：3 failed；两个入口和旧库冲突版本的预览错误显示可保存/可发布。`identity-preview-after.junit.xml`：3 passed，确认操作仍返回明确冲突码。
- `projects-routing.junit.xml` 保留收紧版本预览标志前的联合通过记录；最终记录使用新文件保存。
- `review-fixes/` 的原输入、before 和独立重放由 Spec 审查者维护，不被实现者覆盖。

父切片的真实检查记录另存于 `root-test-evidence.json`：Windows 联合 322 passed / 1 POSIX skipped，WSL2 定向 131 passed；14 项旧 Run 与启动竞态包含在其中。历史测试输入手误、HTTP 坏 Unicode 的错误预期及 WSL 缺依赖导致的收集失败分别注明，不计为产品修复。旧失败时没有保存完整旧源码的报告只作为当时实测记录，不宣称可从某个旧提交重建。

独立审查分别保存在 `review-fixes/spec-review.md`、`spec-pinned-run-review.json`、`root-persistence-standards.json` 与 Web/UI 记录。原输入和失败文件不覆盖。`browser-verification.json` 绑定实际构建与编辑、预览、发布、刷新当前版本及历史操作；三份 `publication-browser-*.json` 保存实际配置/发布及 Run/容量逻辑表摘要，说明 preview 无副作用、publish 不迁移旧 Run 或启用 Profile。前端最终 `rulebook-ui-final.junit.xml` 记录 90 项交互测试通过，较早的 77 项报告保留为历史。

`freeze.report.json` 将 98 份产品源码、47 份测试和 51 份其他证据绑定到工作目录与暂存 Git 字节。三份未修改的既有文件在 Windows 使用 CRLF、Git 使用 LF，分别保存两种摘要；全部本次新增/修改的源码、测试与证据逐字节一致。首轮遗漏既有测试换行差异的校验失败也如实记录，随后完整检查通过，未为通过校验而改写旧文件。
