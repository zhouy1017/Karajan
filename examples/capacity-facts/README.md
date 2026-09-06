# 容量事实导出证据

本目录验证 `CapacityStore.routing_facts` 的真实 SQLite 只读行为，关联 [M3-02 #24](https://github.com/zhouy1017/Karajan/issues/24)。完整调度仍须接入批准、资格、需求估计和原子准入。

- `author-verification.json` 保留作者实际命令、源码摘要和报告原路径；这些 `.cache/` 报告在此目录以相同文件名保存原字节。首次失败是公开接口尚不存在，随后通过。
- `capacity-routing-facts-all.junit.xml`：Windows 全容量 77 passed（新增 26）。
- `capacity-routing-facts-wsl.junit.xml`：Root 独立 WSL2 全容量 77 passed。
- [独立 Spec](spec-review.md)：10 项真实公共接口用例；[观测记录](spec-observed-cases.json) 保留计量、来源及全九张表的前后摘要。
- `root-standards.json`：冻结源码检查、WSL 命令和本次范围。数据只读与网络拒绝行为分别由实际测试证明，不把内容摘要当作授权签名。

固定窗口的覆盖证据、未覆盖消费、未来占用、未知/过期/耗尽和超额原值均单独保留。WAL 竞争使用实际第二连接提交更新；当前导出保持完整旧版，下次读取取得新版。

`freeze.report.json` 将本切片最终提交的源、测试与证据绑定到暂存字节。没有真实供应商资格或现金 API 调用，结果不是启动凭证，也不关闭完整 FR08/FR09 或统一路由验收。
