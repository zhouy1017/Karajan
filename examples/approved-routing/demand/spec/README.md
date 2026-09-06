# Attempt 预测的独立 Spec 验收

本目录通过公共 ProjectRegistry、RunPlanner 和 AttemptEstimateStore 操作，检查 13 个登记/读取边界。合成 Git 项目与 SQLite 均创建在新的缓存目录。`../..` 下 routing/spec/fixture.py 是 Spec 自己维护的公共 fixture 模块；不依赖作者测试 helper，也不以直接数据库写入构造批准材料。

检查覆盖真实批准计划/任务/上下文政策绑定、科学计数法的显式预测值、禁止输入 known/价格/伪造绑定、最新版本撤销不回退、旧容量 as_of 无法复活已过期预测、完整池窗口身份、真实窗口重置后的重新绑定、新计划批准失效，以及项目 guard 对并发撤销的实际阻塞。

规划 admission receipt 明确为 fixture。所有预测均是 owner_conservative_estimate：confidence=unknown、price=null；输入上下文上界、输出预留、执行时限和预测用量分别保存。测试没有运行模型、读取密钥、验证现金上界或授予 runtime_tools 资格。

从仓库根复跑（Python dev 依赖已安装，选择新的 basetemp）：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
python -m pytest examples/approved-routing/demand/spec/test_public_demand.py --basetemp .cache/demand-spec/replay-new -q
```

测试文件和 fixture 模块通过向上查找 pyproject.toml 定位根目录。final.junit.xml 是发布路径实际复跑结果，review.json 与 source-final.json 绑定当前源码及测试摘要。history/development.junit.xml 保留开发路径的结果，不重复计数。没有 Spec 发现；项目库锁仍不代表跨数据库原子准入。
