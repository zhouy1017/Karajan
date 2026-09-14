---
status: accepted
---

# 通过外置 CLIProxyAPI 接入多个模型来源

2026-09-14 用户要求把 provider 支持外置到统一网关。Karajan 默认通过独立部署的 CLIProxyAPI 调用不同 provider，保留供应商无关的连接/模型绑定和受控 Agent Runtime。网关负责认证、协议转换和已允许范围内的转发；Karajan 负责批准、工具执行、状态、资源账本、验证与交付。

此决定取代“所有订阅只能走官方 CLI、所有 API provider 适配均由 Karajan 自建”的接入排他性约束。旧路径与证据保留为显式兼容 Profile；不因新设计自动启用，也不成为失败时的隐式后备。Karajan 不实现供应商订阅 token 提取或转换逻辑；外置网关连接作为独立认证/计费通道验收。

选择外置而非嵌入 SDK，使网关版本和凭据管理独立于 Karajan 业务发布。代价是需核对路由映射、隐式重试、实际来源/用量与远端停止语义；统一协议不能替代这些事实。严格绑定无法由选定部署保证时阻塞，不能以别名或 HTTP200 作为资格。

[ADR 0001](0001-single-coordinator.md) 的唯一业务协调器、[ADR 0002](0002-profile-and-native-resource-ledger.md) 的固定 Profile 与原生资源账本、[ADR 0003](0003-independent-delivery.md) 的独立交付继续适用。详见 [网关契约与验收](../architecture/08-provider-gateway.md)。状态仅为设计接受，网关适配和真实资格尚未完成。
