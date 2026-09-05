---
status: accepted
---

# Karajan 拥有唯一业务协调器

用户要求跨订阅/API 的强制 Rulebook、配额预留与明确交付权限。2026-09-05 审阅 Q1 已确认：Karajan 唯一拥有 Run/Task/Attempt 业务状态，执行器只执行已绑定 Attempt 并报告物理事实。Bernstein 当前可核查的扩展点未证明覆盖所有派发、续接和 fallback。

相比“Bernstein 全任务图＋外置策略”，这需要实现有限的业务依赖推进，但不会让两套系统同时决定重试、模型与终态。先接具体 CLI/API adapter，Bernstein 满足同一受控执行接口后复用。此决定取代前期草案的状态所有权建议。

依据见 [来源](../architecture/sources.md#bernstein)，契约见 [状态设计](../architecture/01-control-and-state.md)。尚未实现或完成底座验收。
