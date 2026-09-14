---
status: accepted
---

# 独立交付入口发布有证据的固定候选

2026-09-05 审阅 Q9 已确认：Worker 只产候选，可信验证绑定确定内容与基准，独立交付入口持有远端凭据并幂等形成 PR。模型及项目测试环境不能借用交付身份。

相比 Commander 执行器直接 push/开 PR，这增加候选导入和远端核对，但能在旧结果迟到、证据失效、取消和请求超时后保持清楚的交付判断。隔离通过实际环境验收，不能只依靠提示词。详见 [执行与交付](../architecture/03-execution-and-delivery.md)。

2026-09-14 [ADR 0006](0006-configurable-roles-and-workflows.md) 将本决定明确绑定于 `delivery_kind=pr`。report/patch 按各自目标验收，不强制创建 PR；PR 仍必须通过同候选机器 checks、独立审查和远端授权，不能由自定义角色改名或 Workflow 条件跳过。原 PR Issue 的验收不因此变成报告验收。
