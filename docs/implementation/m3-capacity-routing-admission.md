# 路由事实与真实容量准入的绑定

`CapacityStore.admit()` 接受可选的完整 `expected_capacity` 对象。可信路由调用必须从服务端读取的事实与已批准规则构造它；这个底层接口继续兼容原有不带绑定的本地调用。

```json
{
  "expected_capacity": {
    "policy_revision": 3,
    "pool_windows": { "short": "window-a", "weekly": "window-b" },
    "lead_reserve_access": false
  }
}
```

对象存在时三个字段都必需，类型严格；窗口向量为 1–32 项，必须与注册 Profile 的完整 pool 集合一致。账号与池定义沿用不可变的注册 Profile/pool 身份。政策 revision 使用容量模型现有的正整数范围。

`admit()` 和 `activate()` 都在各自真实 SQLite 写事务内重新读取当前政策、最新观测和所有 Run 的有效占用。政策 revision 变化返回 `CAPACITY_POLICY_REVISION_CHANGED`，窗口集合不完整返回 `CAPACITY_WINDOW_VECTOR_MISMATCH`，当前窗口不同返回 `CAPACITY_WINDOW_CHANGED:<pool_id>`。同窗口的观测更新不会仅因 observation sequence 改变而拒绝，但最新余额、消费、预留、冷却与保留量仍重新检查。

`lead_reserve_access` 只限制已具备 commander/lead 角色的请求。false 同时禁止使用 lead 保留量和保留槽位，true 不会给 worker 或 adviser 增加权限。绑定随 reservation 持久保存，activate 使用保存的绑定，不接受替换权限。此接口不验证调用者是否为真实主 Commander；批准 Run builder 与 Coordinator 必须继续从可信记录确定角色、规则和任务身份。

新绑定不修改旧请求的存储形式或幂等摘要：省略字段和显式 null 都按原有 payload 处理。相同 command key 重放返回原收据；不同绑定复用同一 key 返回 `IDEMPOTENCY_CONFLICT`。重放收据描述原事务，不是新的当前授权或启动许可。绑定变更后应重新路由；已持有的旧 reservation 需通过既有 reconciliation 处理，不能覆盖身份重新预留。

本切片不将路由、容量、Run 或执行数据库合并成一个事务，也不授予真实模型启动权限。`activation_allowed` 仍为 false；执行入口仍需检查最新批准版本和 fence。Coordinator 的可恢复意图和跨库处理由接线层实现。

验证使用公共 store、真实 SQLite 和两线程竞争：26 项新增用例覆盖两个事务边界的政策/窗口失效、同窗口最新余额、完整向量、lead 限制、旧命令摘要、持久重放及多 Run 争抢最后一个槽位。原实现实际运行结果为 20 项失败、6 项通过；实现后 26 项通过。完整 capacity 回归为 103 项通过，Ruff 与 capacity 类型检查通过。所有输入为离线合成样例，没有模型、密钥或真实账户调用。
