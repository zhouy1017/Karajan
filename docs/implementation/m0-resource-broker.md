# M0-03：资源切片与发送未知探针

范围：[M0-03 / #4](https://github.com/zhouy1017/Karajan/issues/4)。本票实现真实 SQLite 账本、一个窄的本地 Python broker 入口，以及实际接收 HTTP 的假 provider。所有价格、金额、模型与回复均为合成数据，没有调用真实模型或现金 API。

当前结果：Windows、Python 3.12.14 上 39 项资源测试通过，Ruff 和本模块严格 mypy 通过；独立 CLI 演示的 8 项观察条件全部通过。这里只验证 FR10、A19/A26 的部分记账与发送行为，不代表真实厂商计费资格、完整资源系统或 M0 总出口通过。

## 公开入口及信任边界

实现位于 `backend/karajan/resources`。入口是库 API `ResourceBroker` 与 `python -m karajan.resources demo`。本票没有面向不可信 worker 的 HTTP broker 服务、身份认证中间件或完整 DAG。

| 入口 | 作用与约束 |
|---|---|
| `configure_budget(currency, amount)` | 受信任控制面设置单个原币预算；重复设置不会覆盖历史预算 |
| `reserve_attempt(..., profile, amount, authorization_id, fence, authorization_expires_at)` | 原子创建父预留，固化本地 Profile、授权身份、fence 与失效时间 |
| `submit(attempt_id, fence, prompt, max_output_tokens, logical_call_id=None)` | 每次到达生成新 receipt；校验当前授权和父切片，然后发送至预先登记的本地假 provider |
| `settle(call_id, usage_event_id, actual_charge, currency, provider_request_id)` | 受信任控制面核对累计费用；可以在 Attempt 终态后使用 |
| `finish_attempt` / `revoke_attempt` | 清理未派发的父额度；撤销会增加 fence；不能释放可能已经发送的子调用 |
| `recover()` / `snapshot()` | 对发送状态进行保守恢复，或读取可独立核算的同一事务快照 |

这里的 `Price` 和 `Profile` 是本地协议的登记数据，未替代全项目的 Execution Profile 契约。绑定在创建 Attempt 时持久化；`submit` 不能传目标 URL、模型覆盖值或“逻辑 ID 已可信”的开关。价格、endpoint 和 `logical_id_evidence_ref` 由受信任登记方提供，引用字符串本身不证明真实协议资格。未来面向 worker 的服务必须在此入口之外实现调用方鉴权，并从控制面加载这些值。

## 记账和状态约束

金额输入只接受有限、非负、最多六位小数的十进制字符串；落库为整数微单位。过多小数不会静默四舍五入。USD、CNY 等各自检查预算，没有汇率换算。一个数据库在本票表示一个测试账户；完整账户身份、Run 多层上限和共享配额窗口属于后续工作。

父预留已经占用账户额度。子调用获得上界时从父 `future` 转出，不能再次叠加整个父 `reserved`。`reserved` 保留为最初分配记录，不参与重复扣算：

```text
某币种占用 = 所有父预留的 future
           + 已确认调用的 actual_charge
           + 尚未核对调用的 upper
```

`not_sent` 不计入调用占用。结算后，仍活跃的 Attempt 收回子上界与实际金额的差额；Attempt 已终止时只记录费用，不恢复可派发额度。真实报告费用超过声明上界时如实记账，并暂停该 Attempt 的后续调用。测试验证了此异常分支；它不构成真实价格模型可信的证明，也没有实现全局价格版本隔离工作流。

分配、激活与结算分别使用短的 `BEGIN IMMEDIATE` 事务，SQLite 启用 WAL、外键与 `synchronous=FULL`。网络请求发生在事务外。两个连接同时竞争同一父切片时，只有获得足够额度的请求会到达 provider。

| 子调用状态 | 持久含义 | 恢复或终止处理 |
|---|---|---|
| `prepared` | 已领取切片，尚未提交发送意图 | 确定没有进入发送阶段，可变为 `not_sent`；旧执行者激活前也重查此状态 |
| `send_pending` | 发送意图已提交，可能已发出请求 | 保守变为 `send_unknown`，保持上界 |
| `send_unknown` | 是否消费或具体费用尚未核对 | 不自动退款、不自动重发；等待明确费用回执 |
| `settled` | 已有累计费用、用量事件及 provider 请求身份 | 记账独立于 Attempt 生命周期 |
| `not_sent` | 已确定没有发送 | 不占子调用额度 |

激活事务再次检查 fence、授权失效时间、Attempt 状态及价格有效期，然后提交 `send_pending`。提交发送意图是本票的许可边界：此前撤销阻止发送；此后撤销不能证明网络尚未发出，只能阻止后续准入并保留可能消费。这里没有声称 SQLite 与网络发送具有原子性或 provider exactly-once。

当前假协议的上界为 `fixed_charge + input_byte_rate × UTF-8 输入字节数 + output_token_rate × max_output_tokens`。输入最多 1 MB，输出声明最多 1,000,000 token；缺少收费项、未声明覆盖所有收费项、价格过期或金额无效均拒绝。本公式只适用于本票定义的合成计费协议，不是任一真实模型的 tokenizer 或价格表。

## 收据、逻辑 ID 和核对

每次 `submit` 到达均落一个独立 `receipt_id`，包括拒绝请求。没有可信传输登记时，相同 prompt 或相同客户端 ID 仍创建新的调用，并重新领取额度。正文摘要只用于审计及检测“同一已验证逻辑 ID 却改变参数”的冲突，不用来合并两个推理请求。

只有固定 Profile 带有受信任的 `logical_id_evidence_ref` 时才启用逻辑 ID 重用：同一 Attempt、相同 ID 和参数查询原调用状态，生成新 receipt，但不再次发送。参数改变则拒绝。这个行为由合成本地登记验证，尚未证明任何真实 CLI/SDK 会稳定传递这种 ID。

回执必须含有效的 provider 请求 ID、用量事件 ID、原币和金额。错误状态码、连接异常、身份缺失、币种冲突或无效金额使调用保持未知，并把原因落入 receipt。`settle` 只允许 `send_pending`、`send_unknown` 或 `settled` 的调用；对 `prepared`、`not_sent` 返回 `CALL_NOT_SENT` 并保持整个账本不变，防止未发送或已经退回的切片再次结算。它将金额解释为该请求的累计已确认费用：相同事件和内容重放为无操作；事件内容冲突、provider 请求身份改变或累计总额倒退被拒绝。降低既有费用的人工调整协议不在本票范围内，不能靠新事件 ID 静默释放金额。

## 运行与观察证据

在已安装项目开发依赖的环境，从仓库根目录运行。输出目录必须不存在，以免覆盖已有证据：

```powershell
$resourceProbeOutput = Join-Path $env:TEMP ('karajan-resource-probe-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m karajan.resources demo --scenario examples/resources/local-fake-scenario.json --directory $resourceProbeOutput
```

CLI 向 stdout 输出 JSON，并在新目录写入 `resources.sqlite` 和 `report.json`；全部条件通过退出 `0`，否则退出 `1`。输入必须使用 `karajan.resources.probe.v1` schema 的完整字段，非有限/浮点/超精度金额会在创建目录前拒绝。

可复用文件：[演示输入](../../examples/resources/local-fake-scenario.json)、[本次实际生成的报告](../../examples/resources/local-fake-report.json)。报告包含 OS、Python 版本、观察时间、账本快照、实际 HTTP 收包记录及退出码。UUID、监听端口与时间每次运行都会改变。

本次演示的可重算结果：

1. 原币账户额度 10、父切片 3、每次调用上界 2；两个并发请求恰好一个通过，provider 只收到一条请求。结算金额 2 后，账户仍占 3，其中父 future 为 1、子实际费用为 2。
2. 结束首个 Attempt 后，为另一个 Attempt 预留 4。子进程在 HTTP 响应已返回、结算尚未落库时实际 `os._exit(71)`。
3. 新 broker 恢复并结束该 Attempt 后，未知调用仍占 2，加上首个已确认费用 2，合计占用 4。provider 累计只收两条请求。
4. 迟到费用 1 连续核对两次，最终费用为首个 2 加迟到 1，合计 3；用量事件没有重复入账，收包数仍为 2。

账本快照还暴露固定 Profile 及其 digest、价格版本、请求 digest、输入字节数、输出声明、调用上界、实际费用和事件身份。快照不复制 prompt；本地 SQLite 保存原始合成请求以支持复核。该存储尚未实现生产数据的保留、加密或脱敏策略，不应投入真实输入。

## 测试与红绿记录

测试只通过公开 API、CLI、实际 HTTP 接收记录和账本快照观察行为，没有替换 SQLite 或网络调用。四个发送窗口用真实子进程退出验证，授权失效和部分错误注入使用可控时钟/检查点；后者是测试夹具，不代表实际操作系统暂停能力。

| 新行为 | 首次观察到的失败 | 该轮补齐后 |
|---|---|---|
| 父子原币切片 | 模块尚不存在 | 1 项通过 |
| 并发准入及收据 | 快照缺少 receipts | 2 项通过 |
| 过期/缺项价格 | 价格字段不支持 | 6 项通过 |
| 未知与迟到重复结算 | 检查点及恢复能力缺失 | 7 项通过 |
| 可信逻辑 ID 区别 | 固定传输证据字段不支持 | 10 项通过 |
| 四个实际退出窗口 | 部分退出窗口未触发，子进程仍正常结束 | 14 项通过 |
| 激活前撤销与过期 | 时钟/授权有效期及撤销能力不支持 | 16 项通过 |
| 本地计费路径限制 | billing path 登记字段不支持 | 20 项通过 |
| 原币及绑定审计 | 快照缺少 Profile | 21 项通过 |
| 超上界真实记账 | 超额后下一调用仍发送 | 22 项通过 |
| 错误调用参数 | 异常或错误拒绝原因、空 ID 被接收 | 26 项通过 |
| 独立演示入口 | resources CLI 尚不存在 | 27 项通过 |
| 异常回执与费用冲突 | 空身份被结算、未知原因未落库、费用倒退被接收 | 32 项通过 |
| 错误演示金额 | NaN 引发 traceback、浮点输入创建了目录 | 36 项通过 |
| 精度不静默舍入 | Decimal 上下文将超精度金额误认为整数微单位 | 37 项通过 |
| 未发送调用不能结算 | Spec 审查后补两种公开回归：prepared/not_sent 均错误接纳用量，后者还再次增加父切片 | 39 项通过 |

最终执行：`ruff check backend/karajan/resources tests/resources` 通过；`mypy backend/karajan/resources` 在 4 个源文件上通过；`pytest tests/resources -q --tb=short` 为 39 passed（本机 2.40 秒），其中既有终态 Attempt 的迟到费用核对回归继续通过。没有增加依赖，未修改根 CLI、lock 或 CI。

## 尚未证明的边界

本票固定只允许 `local_fake`、`http://127.0.0.1:<port>/infer`。现金计费标签、非本地地址及非预期 endpoint 在创建子调用前拒绝；HTTP 客户端不使用环境代理，也不跟随重定向。此限制是当前 broker 的发送策略，未验证整个宿主或其他进程的网络隔离。

所有输出明确带 `qualification_scope: offline_local_fake`、`live_qualified: false`、`cash_api_enabled: false`。本票的快照/运行报告是实际本地探针证据，不是 M0-01 的离线文档一致性报告，也不会自动启用任何全局 Profile。真实 API 价格完整性、全部出口强制过 broker、真实调用身份/用量覆盖、与 RunnerHost 授权服务的连接、Linux/远端 CI，以及完整 A19/A26 均仍待对应资格与集成验收；本轮现金 API 验收为 `not_run`。
