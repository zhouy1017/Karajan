# Toil-like Heavy Agent Framework Research

关于构建重度 Toil 类多 Agent 代码交付控制平面的调研与架构建议。

## 报告

- [重度 Toil 类框架：底座选型与 Bernstein-first 设计报告](outputs/toil-like-heavy-framework-report.md)

报告比较了 Toil、Bernstein、Claim Plane、Agent Workspace Fabric 等项目，并推荐以固定版本的 Bernstein Runtime 加独立策略扩展发行版的方式实现项目准入、模型路由、owner 授权、候选验证和自动 commit/push/PR/merge。
