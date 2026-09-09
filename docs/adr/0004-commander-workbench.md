---
status: accepted
---

# 以持久 Commander 工作台承载并行交付

2026-09-09 决定以仓库/会话导航、持久主 Commander 对话、中央 Tasks/Agents 并行看板和右侧候选详情作为首演工作台。Commander 先提出完整默认任务分工，用户可以一键接受，也可以按需编辑角色、模型、来源和依赖后批准同一版本；两个无依赖 Worker 随后可并行形成候选，由 checks、独立 Reviewer 和交付入口继续完成。Commander 负责拆分、委派、升级建议和验收判断，可信协调器负责机械调度、权限/额度核验和状态提交；运行中的 Attempt 不热切换模型。

选择这一形状是为了把 [Bernstein 的任务列表、详情抽屉和 Agents 视图](https://bernstein.readthedocs.io/en/latest/gui/screens/) 与 [Toil 的主模型分解/委派/验收工作模式](https://github.com/zhouy1017/toil/blob/main/src/toil/_assets/skills/toil-offloading/SKILL.md) 组合到同一可追溯入口。它们是信息架构和协作方式的参考：Bernstein 的 [per-step routing](https://bernstein.readthedocs.io/en/latest/workflows/per-step-routing/) 主要覆盖 Claude 兼容 model/effort 路由，Toil 的固定 Sol/Deepseek/Luna 也不是跨来源资格或现成桌面 UI，因此不能直接成为 Karajan 的执行或路由所有权。

本 ADR 只记录产品入口和协作边界，不全面替换现有执行底座、Rulebook、资源账本或独立交付决定。持久会话、看板筛选、Diff/checks/review/logs/dependencies 详情、加载/空态/运行/阻塞/恢复和模型改派边界由 [Commander 工作台设计](../prd/commander-workbench.md) 细化；在对应 UX-AC 和原 FR 验收前均保持未完成或阻塞状态。

后端身份沿 `Project → CommanderConversation → Run` 归属：一个项目可以有多个会话，一个会话可以保留多个 Run；新对话/新任务只保存消息、草稿或待确认提案，不隐含 execution approval。迁移、兼容旧 Project/Run 路由、Hub 聚合、SSE 缺口恢复以及 model progress 与 connection heartbeat 的分离见 [Commander Workbench 后端契约](../architecture/07-commander-workbench-backend-contract.md)。
