# 已批准 Run 路由接线证据

实现说明见 [持久路由判断](../../docs/implementation/m3-approved-run-routing.md)。固定起点为 `35c00f7cbc04704c805c36e0b00ff1b743f2143b`；该目录证明本切片覆盖的控制面行为，不证明真实模型派发或整个 PRD 完成。

| 范围 | 公开证据 |
|---|---|
| 固定三进程资格观察、身份、撤销、过期及锁 | [qualification/spec](qualification/spec/README.md) |
| 完整窗口/政策绑定及旧数据库兼容 | [capacity/standards](capacity/standards/README.md) |
| owner 的精确任务/Profile 估计登记及失效 | [demand/spec](demand/spec/README.md) |
| 批准 Run、每规则许可、当前事实与跨 Run 消费 | [routing/spec](routing/spec/README.md) |
| 认证 HTTP、源替换拒绝、幂等和参与者组 | [routing/standards](routing/standards/README.md) |

[作者冻结](author/source-freeze.json) 记录最终产品/测试摘要和三个实际回归报告。初次 Windows 相关范围为 561 passed、1 POSIX-only skipped；自定义组和去向修复后，受影响回归 248 passed。WSL2 新增用例 104 passed。三个路由文件随后仅规范化为 LF；[前后摘要](author/routing-line-ending-normalization.json) 保留该变化，独立公开路径复跑绑定最终字节。

`author/qualification`、`author/capacity`、`author/demand` 保留开发中的原始红/绿、回归、WSL 和各自冻结记录。它们是过程记录，不应把每个 XML 的 case 数累加成独立覆盖数。独立报告也保留真实失败和测试输入修正：合法 Commander 组重命名的创建失败已修复；最初组与自己的升级阶段重叠的输入本就不合法，已明确撤回该误判。

所有本切片验证没有 provider 或现金调用，也不读取 Go 密钥。正向候选选择的作者贯通用例只有资格来源是明确的 `test_double`；批准、估计、容量和持久判断均经实际公开服务。生产 HTTP 接线不会注入该替身，真实 `runtime_tools` 未资格时保持 blocked。判断收据、历史幂等响应与 `selected_profile` 都不构成启动许可。

从仓库根复跑主要入口：

```text
python -m pytest tests/projects tests/runs tests/routing tests/capacity tests/web tests/orchestration
python -m ruff check .
python -m mypy backend/karajan
```

独立检查的依赖、命令及旧版本兼容方式见各子目录 README。叶目录属性保留证据原始字节；不要格式化历史 JUnit 或改写旧摘要来隐藏失败。

GitHub workflow 的双系统 Python job 已增加这五份公开测试入口，共 70 项；本地以同一命令组合实际运行通过。组合命令明确设置 `-o "pythonpath=backend tests/web tests/runs"`，用于读取公开测试共用的 Run/Web fixture。最初缺少 helper 路径的收集失败保留为 `author/workflow-collection-before.xml`，修复后的实际结果为 `author/workflow-boundaries-final.xml`；该失败属于测试接线，不是产品行为发现。
