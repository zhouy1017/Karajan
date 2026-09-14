# Agent 工作约定

设计与文档维护从 [文档导航](docs/README.md) 开始；当前架构与三张主流程图见 [架构总览](docs/architecture/README.md)。活动 PRD/架构、ADR、历史排期、已发布 Issue 快照和实现证据按导航分工维护。新修订须更新冲突正文，不能只添加顶部声明；保留发布快照原字节及原验收/失败证据。

开始或继续实现、拆分 Issue、准备 PR、验收或关闭 Issue 时，先读 [Issue 跟踪流程](docs/agents/issue-tracker.md)，按原范围、当前候选版本和 C/U/P/S/G 证据推进状态。

2026-09-14 用户授权的 [r8 第一阶段](docs/planning/r8-phase1-20260914/README.md) 使用 Claude Code CLI worker；最初默认 gemini-use，随后用户重新配置默认 DeepSeek 并授权重试，实测配置为 `deepseek-v4.1-flash-high-opencodego[1m]`。本次按新默认派发，不覆盖模型参数；该批最新用户模型安排优先于旧开发模型表及原发布快照中的派发模型说明。worker 实现并提交 PR，Commander 独立审核、返修并合并到 dev；总计不超过 4 个 PR，返修沿用原 PR。当前子票范围及真实状态以 #174–#178 和本批导航为准。

涉及模型来源或 Workflow 时，遵循 [外置网关](docs/architecture/08-provider-gateway.md)、[自定义角色/Workflow](docs/architecture/09-configurable-workflows.md)、[对话设计与部署](docs/architecture/10-conversational-workflow-deployment.md) 和 2026-09-14 r8 [角色调度契约](docs/architecture/11-role-directed-scheduling.md)：Designer 生成同源配置与图表，确认后可信部署；运行中的获授权调度角色按实际任务拆分、分配和决定并行规模，原授权内自动生效。Karajan 不硬编码前后端拆分或 coding Agent 数量上限，实际资源/用户政策决定准入和排队。定义与已启动 Attempt 固定，授权内运行图修订留痕；越权才重新确认。P1–P4 是开发承接顺序，原两任务仅验收样例；新设计不表示实现或原 Issue 已完成。

选择开发任务或恢复队列时，再读 [当前业务顺序](docs/planning/business-first.md)；按其中 P1 开仓库会话、P2 显式分工、P3 最小并行整合、P4 checks/独立 review/交付选择下一步。

涉及 Commander Workbench 的界面、流程、任务角色、模型来源、依赖、并行或交付设计时，读取 [设计治理与承接矩阵](docs/planning/design-governance-20260909.md) 和活动 [Commander Workbench PRD](docs/prd/commander-workbench.md)。该治理索引定义文档分类、历史 M0–M4/DG 映射及未覆盖范围；原 Issue、架构和实现证据仍须按各自原文验收。
