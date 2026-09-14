# 架构配置样例的适用范围

本目录的 [rulebook.v1.json](rulebook.v1.json) 和 [resources.v1.json](resources.v1.json) 是 2026-09-05 架构基线的资源与路由示意。它们保留旧 schema、Profile 和三角色示例，供理解原契约或使用明确匹配的校验器；它们不是当前 WorkflowBundle、网关部署配置或真实账户授权。

r8 的角色和 Workflow 格式、固定版本引用见 [09 配置契约](../09-configurable-workflows.md)；配置包清单、实际文件、预览与部署见 [10 对话设计与部署](../10-conversational-workflow-deployment.md)；角色授权、动态任务图与资源排队见 [11 角色调度契约](../11-role-directed-scheduling.md)。其中 YAML 同样是待实现的设计示意，不冒充已有可执行 schema。模型网关配置以 [08 网关契约](../08-provider-gateway.md) 为准。

旧样例保留原字节以便核对既有实现与历史证据。后续可执行的新 schema 应提供独立版本样例及验证结果，不原地换壳后沿用旧资格。
