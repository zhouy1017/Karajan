# 已预留任务的启动前复查证据

本切片不启动真实模型。它复用原 Capacity/Run/Project 状态，避免再次路由时重复扣算自己的预留，并在原激活已提交后提供持锁的最新容量检查。

- `windows.xml`、`wsl.xml`：两种系统各 393 项受影响模块回归，零失败、零跳过。
- `capacity-review`：31 项独立公共容量边界用例，使用真实多连接 SQLite。原始接口未实现时的红灯与后续验证均保留。
- `routing-review`：10 项独立公共批准来源用例；正向资格明确使用测试替身。最初一个用例的观察时间错误在夹具中修正，未将其算作产品缺陷。
- `routing-compatibility.json`：改动前捕获的九份完整路由报告；作者在改动后逐字段比较相等。复验脚本以 `.txt` 存档，未作为新的自动测试执行。
- `author`：新增接口的原始红绿记录和作者冻结说明。

两套独立用例从根目录运行：

```text
python -m pytest -o "pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web" examples/go-task-startup
```

成功的 guard 只在它持锁的代码块内提供当前事实。后续受信执行入口仍须核对原 operation、Workspace、来源、启动身份和发送账本；没有因此授予任意 Task scope、原生执行、候选收集或 PR 交付资格。[实现与后续边界](../../docs/implementation/m3-task-startup-guards.md)。
