# 实现切片｜批准 Go Task 的固定执行入口、取消恢复与候选收集

Parent: #21
Related: #13, #23, #24, #25
Depends on: #89（执行基础接口）；#87（投影资格与范围）

## 范围

在单机 Linux/WSL2 中连接既有批准 Run、已预约 operation 和冻结 Workspace。公开推进、读取、取消、核对入口仅接受 Run/operation/principal 标识；实际执行由固定 Host 直属 child 完成。复用原业务数据库、Capacity、Journal、Host 和 CandidateStore，不新增调度权威。

## 行为验收（C + P；真实官方资格另列 S）

- [ ] 固定入口仅打开既有数据库/私有部署配置；缺库、空库、重连时库消失及来源变化在实际效果前拒绝，不初始化替代账本，不接受用户 argv、密钥、prompt 或任意路径。
- [ ] 原 activation/start/grant 身份和 expiry 持久绑定，Host 控制仅首次初始化。重复推进、丢失 activation/启动/claim 回执不复活撤销控制、不重复 claim、grant 或原生执行。
- [ ] 实际 Linux Host 直属 child 经当前 operation → Run → Project → Capacity → Host guards 启动 namespace，并在每次 HTTP 发送前重新核对；批准/资格/材料/来源/配额/fence 撤回后无下一效果。
- [ ] 使用明确合成资格与本地 HTTP fixture，实际 read/edit 一个批准已有文件；模型输入来自冻结 CAS，原工作树不变。报告区分真实本机行为与合成外部事实，不继承官方资格。
- [ ] 仅实际 producer 返回的停止捕获可进入内部 Collector；完整基线/文件模式保留，Freeze 由原批准检查和作者身份编译。检查及独立 Review 未完成时 Candidate 不可交付。
- [ ] Candidate 提交前持久化完整 request/projection/内容摘要；提交回执丢失后只读查询精确原收据，没有充分身份时保持 unknown，不重跑 worker 或读取可变旧目录。
- [ ] 取消先持久化再释放锁收尾，精确撤销原 grant/Host；核对不激活、启动、发请求或替换 grant。真实终止范围与远端 unknown 分开，已发送未知占用不退款。
- [ ] 独立审查、上述公共接口及本机故障用例留存证据，当前 PR head 的必需 CI 通过后按仓库流程验收。

## 边界与后续

本片先完成可控本地 HTTP 的完整批准 Task → Candidate 链路；官方 Go 调用须按最终当前源码重新资格并单独记录，既有授权无需重复确认。本片不执行其他通道现金调用。验证环境执行、合格 Reviewer、修复/组合任务、PR 交付、账户窗口核销及完整父需求由关联票继续负责。没有真实计量映射时不把 token 数冒充订阅窗口用量。

## 管理状态

当前状态：`status:in-progress`。

### 已完成

无已合入 dev 的本切片验收；前置基础实现候选见 #89。

### 剩余工作

以上全部验收项。实现分为既有库工厂与部署来源、执行生命周期、固定 consumer/facade、可信 Collector 四部分协同推进。

### 阻塞

最终集成依赖 #89/#87 的当前候选与 CI/独立审查。其接口已有本地实现，可开展本切片离线工作；本票保持 Open，合并由所有者决定。
