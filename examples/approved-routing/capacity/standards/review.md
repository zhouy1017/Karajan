# Capacity admission Standards review

结论：0 项可操作 finding。按父任务指定，仅审查 Standards 轴；Spec 由独立审查负责。

已核对 35c00f7 基线与四个冻结文件。CONTEXT.md 的 Attempt、Reservation、RouteDecision、授权术语保持一致；新 ExpectedCapacity 将同行的窗口/政策/保留访问字段组成一个严格类型。admit 和 activate 共用同一 `_evaluate`，没有复制两套规则。代码在现有 `_command` 的 SQLite 写事务内读当前 policy revision、完整窗口和占用；没有跨数据库原子性或真实执行权限的额外宣称。未发现需报告的 baseline smell。

独立执行 15 条公共接口检查全部通过。直接加载实际基线的 models/store/facts，在旧代码下创建 SQLite 预留、幂等命令与 activation 收据，再由新代码重开和重放，证实省略字段及显式 null 均兼容。独立检查同时确认：旧准入收据重放不能越过新政策的 activate 核对；绑定字段不能用相同 command key 替换；true 不能让 Worker、Reviewer、Check 或顾问取得保留槽位；false 也作用于 unknown quota 的保守模式。

源码在检查后再次逐 SHA 核对，仍与作者冻结清单一致。公开发布后的 15 条测试再次全部通过。当前证据为 `junit.xml`、`review.json`、`source.json` 和保留的 `legacy_capacity/` 基线包；原始记录保留在 `history/`。

范围限制：底层旧接口仍允许不传 expected_capacity，符合兼容要求；正式 builder 必须由可信记录构造绑定。此审查没有调用模型、读取凭据或修改产品/Git。
