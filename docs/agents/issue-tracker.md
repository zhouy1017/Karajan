# 任务跟踪约定

使用 [zhouy1017/Karajan 的 GitHub Issues](https://github.com/zhouy1017/Karajan/issues) 作为任务状态和讨论的唯一来源。

PRD 父 Issue 保存产品需求。M0 子 Issue 保存可执行范围、验收和阻塞条件；阶段路线图不自动等于可执行票据。实现 PR 关联对应任务，不因某个子任务完成而关闭整个 PRD。

本地 PRD 与 Issue Markdown 是版本化规格和发布正文快照；发布清单保存实际 GitHub 编号、URL 和关系核验结果。后续修改需同步并说明规格 revision，避免把本地文件状态当作远端任务状态。

使用 GitHub 原生子任务关系与 blocked-by 依赖，同时在正文保留可读链接。`ready-for-agent` 表示正文足够完整，仍须满足依赖和执行所需配置；不能解释为已获得账户消费授权。可选 Bernstein 任务不进入关键路径。

真实调用、资格结果和证据应写入相应 Issue/实现产物；密钥只保存引用，不进入正文或日志。发布任务本身不会启用 Profile 或触发实现。
