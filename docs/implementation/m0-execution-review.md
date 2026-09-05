# 执行、资源与订阅协议切片审查

基准为首批 PR #31 的 `51f8d4642cb0693bfeade2715c744d59a3b041c8`。新增范围是 `execution`、`resources`、`adapters/codex`，以及各自测试、示例与实现记录；正在开发的 OpenCode、隔离和 Web 工作台不在本次范围。

## Standards

独立审查未发现明确的仓库文档标准违规。发现一项有实际资源观测的维护问题：RunnerHost 和 supervisor 的 SQLite `with connection` 只结束事务，不及时关闭连接。在暂停循环垃圾回收的对照实验中，200 次公开 `reconcile()` 使 Windows 句柄增加 200 个。作者改用 `finally: close()` 的上下文管理入口，supervisor 同样复用；公开句柄回归及原生命周期测试通过。资源模块已有正确关闭逻辑。

## Spec

三个模块各发现一项行为问题，均已通过公开入口先复现再修复：

| 模块 | 问题 | 最终行为与回归 |
|---|---|---|
| RunnerHost | 同一 Attempt 的两个不同完成事件均获接纳 | 同一事务只接受 pending 业务的首次新结果；顺序和 8 路并发只接受一个；相同事件重放保留原裁决，迟到费用仍追加 |
| 资源 broker | 已释放的 `not_sent` 调用结算再次返还切片，原父预算 4 被计为 6 | `prepared/not_sent` 拒绝结算，快照不变且无 HTTP 收包；已发送调用在 Attempt 终态后的核对继续支持 |
| Codex 回放 | 未检查 turn 状态/错误和完成顺序，失败 turn 仍可获批准并报告通过 | started 仅接受无错误的 inProgress；完成必须来自活跃未结束 turn；失败、中断、矛盾状态/错误及重复完成均非通过并关闭权限 gate |

根复核：Windows 3.12 上，原契约 29、执行器 44、资源 39、Codex 41，共 **153 项测试及 49 个 subtests 通过**（28.88 秒）；新增模块 Ruff 和 strict mypy（14 个源文件）通过。Codex 的七组保存报告由当前公开 CLI 重新生成；输入采用 Git 提交中的 LF 字节，避免跨平台换行使输入摘要失配。

## 资格范围

M0-02/03 证明实际本机进程、SQLite、本地假 HTTP 的有限原语；M0-04 只完成固定版本协议与权限回放，其真实订阅登录、推理、文件工具和取消尚未执行。全部真实 Profile 均不能据此启用，现金 API 请求为零。

Windows 本地通过不能代替 Linux CI、目标 WSL 环境或工具沙箱资格。后续 PR 的必需检查负责验证提交本身；相应远端运行结果需单独追加，不能从本记录推断。
