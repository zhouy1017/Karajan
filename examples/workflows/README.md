# R8-P1-02 Workflow 配置包与同源预览样例

实现说明见 [R8-P1-02 配置包与同源编译预览](../../docs/implementation/r8-phase1-workflows.md)。本目录提供一个可复现操作入口，证明控制面行为，**不**证明真实模型创作、配置激活或完整 r8。

```text
python examples/workflows/bundle_preview.py
```

脚本在同一进程内通过真实认证边界（Session + Origin + CSRF + `Idempotency-Key` + `If-Match`）完成：

1. 登记项目并创建设计会话；
2. 发布两份**真实**配置包（不同角色与拓扑），落盘为不可变 revision；
3. 读取同源 preview：结构化图、角色/步骤表、Mermaid 图与差异；
4. 用表格命令直接编辑依赖，产生确定性的新 revision（`model_calls: 0`）；
5. 保存一条设计文字，状态为 `pending_generation` 且不产生任何配置；
6. 读取受信任执行种类目录；
7. 在新构造的应用上重新读回同一 revision，确认摘要一致；
8. 直接调用已登记的真实适配器 `artifact_aggregate@1`。

实际输出见实现说明第 3 节。

## 边界

| 项目 | 事实 |
|---|---|
| 真实 provider 调用 | 无 |
| 模型调用 | 无（脚本断言 `model_calls == 0`，创作输入状态为 `pending_generation`） |
| 配置激活 / Run | 无（`activation_allowed=false`、`dispatch_eligible=false`） |
| 业务适配器执行 | 仅在脚本最后**显式直接调用**一次；编译路径不调用 |
| 无适配器的种类 | `available=false`，不因登记而可用 |
| 证据层级 | C/P（本机控制面行为）；**不**宣称 S 或 G 资格 |

脚本使用 `TestClient` 而不是真实网络端口以保持确定性；同一套路由在 `python -m karajan.web serve` 下由 `create_app` 注册，认证、来源校验、CSRF 与请求体上界相同。
