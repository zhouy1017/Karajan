# 实现切片｜Go 投影资格 v2 与已批准任务的范围约束

Parent: #21
Related: #13, #23, #24, #25
Depends on: #85（PR #59）

## 原范围

通过现有持久资格入口验证已有文件投影、逐次参考计量与实际停止后的完整候选采集，并让已批准 Run 路由消费这些有限事实。默认 fixed suite revision 1、旧 grant、历史重放继续兼容。

## 行为验收

- [x] 显式 suite revision 2 在首个效果前持久 start、来源、凭据 generation、两个场景及各自 v2 grant；相同命令重开不重发，未知/撤销/来源变化不回退旧成功。
- [x] 真实 Linux OpenCode 通过精确投影读取参考与目标、修改已有代码、拒绝未授权读取；每次最终请求经固定参考 tokenizer 计量，保留输入及工具历史的事实与发送账本逐条一致。
- [x] 实际停止后的字节经 CandidateStore 重建完整基线，保留未投影二进制与执行位；Suite 复核真实候选和函数行为，缺少检查/Review 时 gate 仍未通过。
- [x] 官方小样本通过只产生 Worker/T1/read/edit/已有文件、明确 I/O/C/余量与请求数范围；fixture 不产生生产资格，也不声称实测最大上下文。
- [x] 批准 Run 消费并持久上述 scope 和上下文限制；不兼容角色、有效难度、工具、计量来源/余量/输出限制时无配额预约，重新资格或撤销阻塞旧 Attempt。
- [ ] 受影响的公共 Store、Journal、Run/Capacity 与真实 native 测试通过；独立复核完成，当前 PR head 必需 CI 通过后按仓库流程验收。

## 证据

代码与结果在 `docs/implementation/m3-go-projected-qualification.md` 和 `examples/go-projected-qualification/` 中随实现 PR 发布。真实官方调用与本地 HTTP fixture 明确区分；不发布密钥、原始模型历史或私有账本。

## 父票仍需完成

实际 Task runner、启动时业务准入、验证环境、独立 Reviewer、完整 PR 交付和其他服务的资格不在本切片完成声明中。路由 selected 或资格 passed 不等于模型已启动。关闭该切片不关闭 #21 或相关父票；功能 PR 的合并仍由所有者决定。
