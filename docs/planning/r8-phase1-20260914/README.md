# r8 第一阶段实现派发

日期：2026-09-14。父范围 [#174](https://github.com/zhouy1017/Karajan/issues/174) 隶属产品 [#1](https://github.com/zhouy1017/Karajan/issues/1)。本批按用户授权由 Claude Code CLI worker 实现，优先默认 `gemini-use`；Commander 独立审查、回派修复和合并到 `dev`。总计最多 4 个 PR，返修更新原 PR，不顺带合并旧 #172/#173，也不另开第 5 个文档 PR。

## 切片与依赖

| 切片 | 原始 Issue 正文快照 | 可观察出口 | 依赖 |
|---|---|---|---|
| [#175 网关目录](https://github.com/zhouy1017/Karajan/issues/175) | [01](01-issue.md) | 项目内连接/固定模型绑定、无推理目录探测、持久读回与认证边界 | 当前 dev |
| [#176 配置与预览](https://github.com/zhouy1017/Karajan/issues/176) | [02](02-issue.md) | 实际文件、声明式编译与同源图/表/diff，自定义职责及完整任务图 | #175 |
| [#177 部署加载](https://github.com/zhouy1017/Karajan/issues/177) | [03](03-issue.md) | pending 文件加载/读回、条件 active、重启和回滚、可信消费句柄 | #176 |
| [#178 授权调度](https://github.com/zhouy1017/Karajan/issues/178) | [04](04-issue.md) | grant/有效命令/动态图修订、封口义务、完整 ready/资源等待队列与领取 | #177 |

初始发布摘要和编号见 [github-publication.json](github-publication.json)。快照保留原字节，后续状态从远端读回；独立范围全部满足才关闭子票。此依赖链是本批实现接口顺序，不是用户 Workflow 的固定拓扑或并行人数。

## 证据边界

这四票交付可调用、持久化且可恢复的控制面，不能只提交孤立 DTO。真实模型对话创作、生产来源资格、物理 Agent 并行、候选整合及 PR 交付仍需后续验收；ready/claim/active 不能分别冒充已执行、已完成或业务成功。确定性适配器和受信协议消费者的 C/P 证据不冒充模型 S 证据。原 P1–P4、A01–A26、失败和原 Issue 范围保留。

每个 PR 的当前 head 必须满足原子票 AC、影响回归、Ruff/mypy、必需 `quality-gate` 及独立 Standards/Spec 审查。第一 PR 的首个 commit 纳入已确认 r8 文档基线；后续功能 commit 由 Claude Code worker 编写。临时运行目录、提示词、日志、凭据和本机 PROGRESS 检查点不提交到产品变更。
