# M3-02：共享配额池的离线准入与核销

关联 [M3-02 #24](https://github.com/zhouy1017/Karajan/issues/24)。两个 Run、同账户两个执行配置共同使用短期/长期服务池和平台自身 allowance；每次准入必须同时满足所有池，并使用账户当前 CapacityPolicy。此切片实现内部持久容量账及可运行验证，资源工作台、完整公平队列和协调器接线尚未完成，整张 Issue 保持未验收。

## 可运行入口和真实效果

```text
.venv/Scripts/python.exe -m pytest tests/capacity -q
.venv/Scripts/python.exe -m ruff check backend/karajan/capacity tests/capacity
.venv/Scripts/python.exe -m mypy backend/karajan/capacity
.venv/Scripts/python.exe -m karajan.capacity probe examples/capacity/shared-pools.json --directory .cache/capacity-new-run
```

每次使用新目录，既有报告不覆盖。Linux/WSL2 使用 Python 3.12，并让 `PYTHONPATH` 指向 `backend`。入口只接受 [固定 fixture 配置](../../examples/capacity/shared-pools.json)，没有真实 endpoint、认证或现金 API 参数；冒充 official 的观察不能通过此入口。

样例先拒绝超量请求，再从两个独立 SQLite 连接同时准入两个 Worker。恰好一个获得全部池的预留，另一个没有部分预留；随后主 Commander 可以准入。通过复验的请求真正发送到临时 `127.0.0.1` HTTP 接收端，记录脚本给定的用量并核销。本次 Windows 和 WSL2 均实际接收 2 次，被拒绝请求接收 0 次，保存 2 条用量凭据；重新打开数据库后快照一致。

完整报告含输入和全部源文件 SHA-256、OS/Python、准入/复验结果、接收记录、用量、策略和观察快照：[Windows](../../examples/capacity/windows.report.json)、[WSL2](../../examples/capacity/wsl.report.json)。这些是真实本机 SQLite、线程竞争及回环 HTTP 的证据；配额值、报告来源和用量都是脚本数据，没有真实服务请求。

## 内部接口与状态

`CapacityStore(path, clock=...)` 是可信协调代码使用的内部接口。它不认证调用者，也不自行证明 `role=commander/purpose=lead`、授权引用或消费凭据真实。生产接线必须从已批准 Run、当前 Commander 身份及合格适配器构造这些输入，不能把 API 直接暴露给 Worker 或浏览器。

| 接口 | 结果和约束 |
|---|---|
| `register_pool` / `register_profile` | 池 ID 不可重定义；执行配置 ID/revision 的容量绑定不可变，必须包含同账户全部适用池和至少一个服务池。此处 Profile 只是容量绑定，不是 F01 资格记录。 |
| `observe` | 保存服务观察时间、本地接收时间、来源、覆盖和校准理由；返回前后观察。重复/延迟及提前换窗报告保留审计但不替换当前值；固定窗口已知 reset 不可悄悄改写。 |
| `activate_policy` | CAS 发布账户当前版本；所有 Run 的新准入读取该版本。调整保护量保留既有消费和预留。 |
| `admit` | 在 `BEGIN IMMEDIATE` 内检查完整池向量、并发、时长、观察、保护和冷却；全部通过才保存一个 Admission。结果保留 Run Rulebook 与 CapacityPolicy 两个版本。 |
| `activate` | 在副作用前持久化意图，并用当前政策/观察重新检查；保留原到期时间。幂等命令可重读原结果，另一启动命令不能重新激活同一 Admission。 |
| `record_usage` | 保存有唯一 ID 的增量凭据和显式窗口归属；重复凭据不重复计入，冲突拒绝；实际值高于估算仍完整记账，并返回超估算池。 |
| `reconcile` | 本地和远端结束、完整用量分别报告；只有确定未发送，或两端结束并收到完整用量，才能释放相应预留。显式零用量也是凭据。 |
| `record_failure` / `snapshot` | 保存限流/耗尽事实和有限冷却；提供池、绑定、所有策略、观察、生命周期与用量历史。 |

`reserved` 表示尚未激活；安全过期后转 `expired`，同 Attempt 不可重新领取。`activate` 提交后为 `active`，执行回执不确定或只有本机结束时为 `unknown`，这两种状态不会仅因到期而退还预留。确认未发送为 `released`；两端及用量全部核对后为 `ended`。已发生而尚未被服务报告覆盖的用量在 ended 后仍留在容量计算中。

所有命令保存幂等摘要和原结果；相同命令键配不同内容拒绝。用量 ID 另有跨命令去重，不能靠换命令键重复记账。事务内不访问网络。此数据库没有生产 StartAttempt outbox；与 RunPlanner/RunnerHost 的统一原子准入必须在后续接线完成。`capacity_revalidated` 仅表示容量检查通过，所有结果的 `activation_allowed` 仍是 false；显式 probe 的本机效果不因此获得真实模型资格。

## 配额计算和不确定性

已知数值的池分别计算：报告剩余量，减去本机未覆盖消费、未来切片、安全量和当前角色无权使用的 Commander 保留量。`used` 先由该池 limit 转成 remaining；余额型 remaining 不再次减 used。服务池记录账户总消费，allowance 只记录平台自身份额，例如服务已用 50/100 与平台 allowance 20 分别产生 50 和 20 的余量。

每条用量从对应 Admission 的未来切片扣出，所以已知消费 1、剩余未来 1 合计为 2，不是父预留 2 再加消费 1。超估算不会截断为原预留；后续准入可能显示负余量并拒绝。单位是各池的 requests/percent/tokens，不是货币；这里只复用现有账本的十进制数值转换，没有建立第二份现金账。

只有显式 `covered_usage_ids` 加覆盖凭据才能消除报告重叠，时间较新不等于覆盖。窗口归属也需要凭据：固定短窗重置可以移除确定属于旧窗的用量，跨窗未知消费和其他长期池保留。没有覆盖的结果是保守扣减，不能显示为已精确核对的服务总消费。人工校准保存理由、原观察、新观察和全部历史凭据。

unknown 模式要求明确启用，并同时设置有限并发、Attempt 时长、观察有效期与 cooldown。缺任何一项都拒绝；要求官方数值的服务池不能借此降为估算，本地 allowance 仍使用本地观察。已知零额度或 `QUOTA_EXHAUSTED` 不会被新鲜 unknown 报告抹掉：必须先有可信数值恢复；确定的固定窗口重置只处理已归属该窗口的已知零额度。错误回路的冷却跨重开保留，Retry-After 不能缩短已配置冷却。

## 验证与审查

2026-09-06：Windows/Python 3.12.14 的 **46 项公共测试通过，2.97 秒**；WSL2 Ubuntu/Python 3.12.3 同样 **46 项通过，4.14 秒**。测试覆盖真实并发争用、当前策略、启动复验、未知/过期/核销、完整池向量、实际超估算、重复/延迟/覆盖、人工校准、长短窗、unknown 四项限制、真实公开 CLI 接收计数以及无效输入拒绝。Ruff 和 5 个源文件的 strict mypy 通过。

独立 Spec 审查发现一个 P2：同窗口已耗尽后，更新为 unknown 会错误恢复准入，包含错误回路和数值零两种输入。保存 [原始输入及修复前后报告](../../examples/capacity/review-fixes/README.md)，两个原输入均复验为拒绝；新增回归也证明新鲜数值恢复后可重新准入。独立复跑 46 项测试（3.04 秒）和公开 CLI，通过并核对全部源码/输入摘要。

开发期实际失败→修复包括：缺少激活/核销 API；策略变更后的启动复验；用量/窗口覆盖计算未实现；unknown 有限模式与冷却未实现；固定窗口 reset 可被重写；超大时钟值抛裸异常；缺 CLI 入口。补充测试未观察到失败的部分只记为验证，不编造红→绿。

独立 Standards 审查为 0 项发现；另行重跑 46 项测试、Ruff 和 strict mypy 均通过，异常 Unicode 标识符与极端数值输入稳定拒绝。两轴审查均仅覆盖本切片。

## 剩余边界

本切片没有完成公平轮转/等待提升、资源页面、与 Run 控制/预算/启动 outbox 的同事务接线，也没有执行当前主身份/批准来源集合的外部鉴权。阻塞请求不阻止后续独立准入的行为已验证，但不等于完整公平调度验收。当前结束但用量未核对的 Admission 会保守地继续占并发槽，尚未拆成页面可见的本地/远端/未知消费三种占位。

官方观察和窗口覆盖凭据仍需来源适配器验证，不能接受任意调用者自称。真实订阅外部消费不可被本地预留锁住，订阅估算不能保证 Commander 永远有额度；超估算的实际取消还需执行协调器处理。现金受既有 ResourceBroker 约束，能力未通过的真实配置不会启用。当前现金 API 调用为零，所有真实来源资格仍为 `not_run`。
