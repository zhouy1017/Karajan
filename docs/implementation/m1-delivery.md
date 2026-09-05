# M1-04：独立交付协调协议

关联 [M1-04 #14](https://github.com/zhouy1017/Karajan/issues/14)。本切片实现持久交付意图、分步 activation、条件 Git 推送、PR 身份核对、暂停/取消和完成条件。它使用真实本地 bare Git remote 与明确的 PR/验证替身，尚未连接真实 GitHub 交付凭据或生产资格权威；不关闭整个 M1-04。

## 入口和信任边界

`DeliveryCoordinator(database, git_remote=None, pr_service=None, verification_reader=None, mode="production")` 提供：

| 入口 | 公开行为 |
|---|---|
| `plan(request, command_key, principal)` | 为固定 Run/delivery revision 保存不可变请求绑定与幂等回执，不立即推送 |
| `advance(delivery_id, principal)` | 推进一个当前步骤或核对一次未知结果；不会在一次调用中连做 push 和 PR 创建 |
| `set_control(run_id, active/paused/cancelled/revoked, command_key, principal)` | 持久化控制状态；取消/撤销不可由 resume 解除 |
| `get(delivery_id)` | 读取当前控制、步骤事实、PR、CI、merge 和完成条件 |

变更入口只接受可信 `controller` 身份，执行角色 worker/reviewer/check 被拒绝。此身份参数属于控制端内部调用约定，不是面向不可信进程的认证机制；目前没有 HTTP 交付路由。生产部署仍需独立凭据/IPC 域及真实隔离证明，不能因字符串参数通过就声称 Worker 无法访问控制端。

默认 production 不能激活交付。只有显式 `offline_fixture`、本地 bare adapter、明确 fixture PR 网关和可信测试回执 reader 的组合可跑离线副作用。`production_qualified=false` 始终保持；离线完成记录的 scope 不会变成真实 Run 的完成权限。

`LocalGitRemote` 只绑定调用方提供的本地可信普通 Git 仓库和 bare remote，不读取或接受远程凭据。命令环境去掉模型/认证环境变量、全局/系统 Git 配置和交互认证，关闭 hooks/fsmonitor/credential helper，协议限本地 file。它仍使用显式可信仓库的本地 Git 元数据；不应将 Worker 可写的 `.git` 传入此入口。此 adapter 不能代表已完成生产网络/进程隔离资格。

## 固定绑定与两步发布

请求固定 Run、交付 revision、repo/受管分支/目标分支、测试基准、候选内容/tree/commit、授权摘要、证据摘要、验证引用、精确 expected_old_sha 以及是否预设 CI gate。同键同输入重放，不重复插入；同键或同 revision 改输入拒绝。

同一 Run 的 repo/head/base 目标固定，`(repository_id, managed_branch)` 在协调库内只属于一个 Run；跨交付 revision 仍保留归属。新的 revision 不能越过旧的 send_unknown；较旧 revision 不再取得新 activation。并发的同一步采用 SQLite 事务内的当前状态比较，避免两个调用分别创建有效发送步骤。

PR ID 另在 Run 级唯一绑定，不能靠下一 revision 的相同标记重新认领另一 ID。已绑定后查询为空或变成另一 ID 均阻塞；选中既有 PR 时在 activation 内固定它。打开较早的本地 fixture 数据库时，从已确认快照回填该绑定；历史冲突拒绝打开，不能选择其中一条覆盖。此兼容处理限本地 fixture，尚不是完整历史备份恢复功能。

每一步在外部动作前，将 activation 和 `send_unknown` 一起提交；activation 保存固定请求摘要、验证回执与时间。push 前验证 base、tree 与精确远端 head，初建要求受管 ref 不存在。更新同时要求旧 head 为新 commit 祖先，再使用显式 ref/expected SHA 的 lease 比较；lease 不授权改写历史。

push 确认后，另一次 `advance` 才考虑 PR。它重新检查当前控制、验证回执和实际 Git head/base，按 repo/head/base/Run 标记查询已有 PR。已有唯一匹配时传确切 ID 更新；归属冲突或歧义时阻塞，不根据标题认领，也不另建第二个 PR。

PR 的字段、ID、SHA、CI 状态和 merged 布尔值由严格响应契约验证；畸形发布回执保留 send_unknown，再查询同一对象恢复。确认完成前，在读取当前验证回执后再次观察实际 Git head/base；发布前的旧观察或 publish 响应自报的旧 head 不能代替这次核对。外部系统仍可能在观察后发生新的变化，下次观察会使失效的完成条件撤回，不能把读取当作锁住远端。

控制命令与新 activation 的先后关系在同一个 SQLite 事务序列中裁决。暂停/取消/撤销在 activation 前生效，就不产生该步写操作；已经激活的操作可能继续完成，随后只核对其事实。push 后取消会阻止尚未激活的 PR 步骤。恢复中的 PR 事实可以确认，但当前控制或验证不允许时不宣称 Run 完成。

验证 reader 是独立可信来源，不接受浏览器直接提交的 allow 内容。每次资格/完成门重新读取并核对 receipt_ref、完整请求摘要、decision 和 provenance。当前示例的 reader 是脚本化 fixture，尚未接入 CandidateStore/串行协调器的生产当前状态与撤权协议；跨模块原子授权、真实当前证据和资格仍待集成，不能把 fixture 的允许决定提升成真实授权。

## 未知结果与完成条件

push 回执丢失后先观察指定 ref，未确认时不再次 push。PR 回执丢失后只查询指定身份；查询为空也不代表请求必定未生效，继续 reconciling。实际 Git head/base 与 PR 观察必须一致。已有确认先到、迟到未知回执后到时，不覆盖已确认事实。

完成显示分列：

- 默认模式：当前验证允许且 PR 创建已确认，离线 scope 的完成条件成立；CI pending 和未合并仍单独保留。
- 预设 CI gate：继续等待当前 commit 的 CI success，旧 commit 的成功不满足条件。
- 外部 head/base 变化、当前验证回执拒绝、暂停/取消或撤销：不能继续认定当前交付满足完成条件。
- 没有任何自动 merge 操作。

## 实际验证

2026-09-05 的 **34 项公共行为测试**在 Windows/Python 3.12.14 通过（27.99 秒），在 WSL2 Ubuntu/Python 3.12.3、真实 ext4 临时仓库通过（3.71 秒）。Ruff 与 5 个源文件的 strict mypy 通过。较早的 29 项 WSL 运行也通过，但报告过 Windows 工作区 pytest 缓存无写权限警告；最终使用独立 `/tmp` 测试缓存完成，没有改系统权限。

随后独立审查在冻结源码上实际复现两项 P2：同一 Run 的下一 revision 可以换 PR ID；PR publish 期间远端变化后仍可按旧 head 完成。补充的四个变体（替换/消失、head/base 变化）先失败后修复通过。另四项畸形发布回执（缺 CI、merged 字符串、非对象、非法 CI SHA）也先失败后修复。最终 **42 项测试**在 Windows 通过（40.49 秒），WSL2/ext4 通过（5.29 秒），Ruff 与 strict mypy 通过；示例再次通过同一 PR 的丢回执/CI 流程。

独立复审又执行完整 42 项测试（40.48 秒）并重跑反例，关闭两项 P2；已声明离线范围内无剩余已证实 Standards/Spec 问题。保存 [PR ID 修复前](../../examples/delivery/review-before-pr-id.json)、[发布后漂移修复前](../../examples/delivery/review-before-head.json) 和 [独立复验](../../examples/delivery/review-after.json)。复审 coordinator SHA-256 为 `A7A39D1C345F541EEFFF04F25D8CDC5EF9F6F2C7A30EEF49B108BD0754DDE383`，models 为 `A3FB0094D83BF0E530BFAD65C2D6E449F7B451E967175A8B54C93AF7F454DAB1`。生产资格、凭据/IPC 和真实网关依然待集成。

实现期间真实红→绿包括：丢失 PR 回执后异常退出；CI 成功后不刷新；控制入口缺失；跨 Run 分支归属冲突被接受；后续 revision 重建 PR；错误 PR 归属被认领；外部 head 变化仍保留完成；取消/资格撤销后的恢复误判完成；空/非法命令键；控制状态不能立即重读；迟到未知回执覆盖先到的确认。旧未决 revision 阻塞与暂停在验证期间生效等补充用例直接通过，未冒称它们经历过失败。

```text
.venv/Scripts/python.exe -m pytest tests/delivery -q
.venv/Scripts/python.exe -m ruff check backend/karajan/delivery tests/delivery examples/delivery
.venv/Scripts/python.exe -m mypy backend/karajan/delivery
.venv/Scripts/python.exe examples/delivery/probe_delivery.py --output examples/delivery/local.report.json
```

[可运行示例](../../examples/delivery/probe_delivery.py) 实际创建临时 Git 仓库和 bare remote，PR 替身保存到本地 JSON 文件；创建后故意丢失回执，重开协调器，分别观察旧/当前 SHA 的 CI 成功。保存的 [实际报告](../../examples/delivery/local.report.json) 记录 `planned → pushed → reconciling → awaiting_ci → awaiting_ci → delivered`、两次 activation、真实 commit/tree/head 和同一 PR 身份。

样例验证回执是明确的测试替身，并非真实候选检查或模型 Review。没有实际模型、现金 API 或外部 PR 请求。临时样例目录保留用于本机核对，报告不包含凭据。

待完成：真实 GitHub gateway 的身份/响应丢失验收，独立交付 IPC 与凭据域，当前候选/检查/Review 权威接线，统一 Run 状态与工作台视图，以及覆盖生产恢复的完整组合验收。

已发布 [PR #38](https://github.com/zhouy1017/Karajan/pull/38)。功能提交 `ed76dc7d2fd9882b0837397d9e2ac68338c33ed3` 随后同步远端重排的基础历史，产生内容完全相同的 `4eb09ca2da0b85c74ae18988f9973d497305bbf7`；后者已通过双系统后端、前端及汇总门 [CI](https://github.com/zhouy1017/Karajan/actions/runs/33976002824)。PR 保持打开，CI 不代表生产交付资格。
