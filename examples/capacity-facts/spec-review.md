# 容量事实导出的独立 Spec 审阅

本次仅验收 `CapacityStore.routing_facts` 的真实容量片段。对冻结源码独立运行
10 项检查，全部通过，未发现需要修改产品的 Spec 问题。实际结果和源码绑定见
[spec-review.json](spec-review.json)，原始测试结果见
[spec-independent.junit.xml](spec-independent.junit.xml)。

检查通过公共接口构造非空 SQLite，独立比较全部九张逻辑表。已确认：

- 部分覆盖后的报告剩余 80、未覆盖消费 2、未来切片 5 分列保存。
- 过期但尚未写回的 reserved 保留原状态；active/unknown 跨截止时间持续占用，
  同账户两个 Run 共同计数。
- 只有具备归属和 reset 证据的固定窗口消费被排除；滚动、余额、未知窗口和
  未归属消费继续计入。
- 已耗尽的数值观察不会被 unknown 或后来未应用的报告抹掉；冷却结束也不消除
  仍需新观察的事实。
- used 为 101、limit 为 100 时保留剩余 -1；两笔合法最大用量的聚合
  18446744073709.551614 保留，并明确标识路由数值转换缺口。
- 在 WAL 模式下，另一连接分别于读取暂停期间实际提交策略和用量更新。
  本次读取仍返回完整旧快照，下一次读取返回新事实。
- 无效过滤/时钟稳定拒绝；导出期间阻止 socket connect/connect_ex，实际调用为零。

输入事件、关键观测和全表摘要保存在
[spec-observed-cases.json](spec-observed-cases.json)。较大的中间快照留在本地缓存，
没有为每个断言重复提交整份快照。可从仓库根目录重新运行独立检查：

```powershell
.venv/Scripts/python.exe -m pytest examples/capacity-facts/spec-review/test_independent_spec.py -q
```

输入和断言分别见 [cases.py](spec-review/cases.py) 与
[test_independent_spec.py](spec-review/test_independent_spec.py)。每次运行会生成新的
本地 admission 标识；同一固定数据库状态和时钟下的两次导出必须逐字相同。

这里没有执行 provider 资格、凭据或现金 API 操作，也没有验收完整 Run builder、
跨数据库准入或 Host 启动。逻辑表不变不表示存储文件头、日志文件和时序逐字不变。
容量事实和摘要不授予任务权限，也不构成执行授权签名。
