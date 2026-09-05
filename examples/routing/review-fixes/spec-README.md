# 纯求解器 Spec 反例与复验

本目录的 `spec-*` 文件保存固定合成输入，经公开 `evaluate_route` 得到的历史结果。
`*.input.json` 和 `*.before.json` 从 `.cache/routing-spec-review` 原字节保留，
修复后的结果另存，不覆盖失败历史。每份结果绑定输入 SHA-256 和当次源码 SHA-256。

| 输入标识 | 修前观察 | 预期行为 |
|---|---|---|
| `unknown-estimate` | 需求估计为 unknown，未配置保守模式仍入选 | 未明确允许未知模式时拒绝候选 |
| `lead-reserve-denied` | 路由行明确禁止使用 Commander 保留量，但 lead 角色仍免扣保护量 | 行级禁止取更严格条件，保护量仍参与准入与压力 |
| `fx-normalized-one` | 参考币率 `1.000000` 被误作无适用汇率，跳过成本排序 | 按数量比较参考币率；不改变任何原币硬预算 |
| `credential-native-setting` | Profile 原生设置中的假 `api_key` 被完整复制进结果 | 安全拒绝凭据值，不在结果或错误中回显 |
| `mixed-pool-pressure` | 一个池未知时，另一个已量化的 91% 压力也变成 unknown | 按既定“各可量化池取最大值；均无值才 unknown”语义核对 |

最后一行最初作为规范语义核对项记录，随后按已确认的可量化池最大值语义修复，
仍保留 uncertainty=2；未因原保守处理本身宣称发生权限突破。
`credential-native-setting` 中的字符串只是固定假 canary，不是真实凭据。

在仓库根目录执行以下命令，`--case` 可替换为表内任意标识，输出必须使用尚不存在的路径：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
.venv/Scripts/python.exe examples/routing/review-fixes/spec_replay.py --case unknown-estimate --output .cache/routing-spec-review/unknown-estimate.replayed.json
```

重放脚本只读取固定 JSON、调用公开纯函数并写一份结果；不读取账户、调用模型、创建预留、连接数据库或执行 Runtime。
`activation_allowed=false` 与真实资格 `not_run` 的边界不因候选入选而改变。
这些证据仅覆盖纯求解器；不证明规则发布、旧 Run 采用、当前授权原子复验或真实派发接线已经完成。

独立最终复验：5 份原输入全部达到上表预期；4 个确认问题已闭合，
压力语义核对项也已通过。凭据反例的修后结果只含安全错误，不再含假 canary。
另独立执行完整路由回归，73 项通过（1.51 秒），JUnit 保存在 `spec-routing-final.junit.xml`。
入口 Ruff 检查通过。

修后报告绑定 routing 源和此次 Python 进程实际加载的其他 Karajan 依赖源，
包括新提取的 `contracts/credentials.py`。最终源码冻结、证据指纹与原输入复验状态
以同目录 `spec-review-index.json` 为准；提交 SHA 由主任务发布后补充，未编造已提交状态。
