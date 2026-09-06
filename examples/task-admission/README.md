# 任务准入与恢复证据

本目录对应 [可恢复任务准入](../../docs/implementation/m3-task-admission.md)。所有验证不调用模型服务、不读取用户 API key。正向路由资格使用明确标注的合成来源；Run、估算、操作、Capacity 数据库和故障进程均真实运行。生产 Profile 不因这些测试获得执行资格。

- `author`：当前相关后端回归、WSL 新增用例和源码摘要。
- `capacity-author`：容量只读命令收据与未激活取消接口的红绿过程、Windows/WSL 123 项结果。
- `spec`：独立公开用例，包含真正子进程退出、重新打开数据库、共享占用、撤销、取消与过期；保留过期状态遗漏的原始失败及修复。
- `standards`：独立审查 root 编写的操作、锁保护和 HTTP 接线；不把该审查者自己编写的 Capacity 接口计为独立审查。Capacity 变更另由 root 阅读审查，未发现问题。

与 CI 相同的独立边界命令：

```powershell
uv run --frozen --extra dev pytest -o "pythonpath=backend tests/runs tests/web" examples/task-admission/spec examples/task-admission/standards
```

JUnit 的历史失败文件不是当前失败状态；各目录说明区分产品发现和测试 fixture 修正。源码摘要绑定实际运行版本，历史版本摘要不替代最终结果。HTTP 验证使用生产应用的进程内 ASGI 入口，不宣称真实浏览器、HTTP 监听服务或 Agent 启动已经验收。

本次完成的是配额预留及其恢复。真实资格、后台推进、激活/effect、独立 Reviewer、费用与端到端交付仍待完成。
