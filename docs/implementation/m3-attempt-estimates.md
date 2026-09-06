# 批准 Run 的有界 Attempt 需求预测

`AttemptEstimateStore` 提供 owner 显式维护的完整 Attempt 预测。登记直接读取持久化的批准 Run、任务、执行政策和当前项目资源；调用者不能用自填摘要冒充这些来源。记录保存到现有项目 SQLite，由可信路由 builder 直接消费。

当前只接受 `owner_conservative_estimate`。输出的 `confidence` 固定为 `unknown`，`price` 固定为 `null`。它不是校准结果、官方余额、已用 token 或可强制保证的现金上界，不能绕过当前账号的保守模式及其他准入检查。

## 登记与读取

```python
source = AttemptEstimateStore(planner)
record = source.register(
    run_id,
    task_id,
    {"id": "my-profile", "revision": 1},
    {
        "id": "feature-attempt-prediction",
        "revision": 1,
        "source_kind": "owner_conservative_estimate",
        "validity_seconds": 3600,
        "measurement_semantics": "window_independent_attempt",
        "demand": [
            {"pool_id": "short-requests", "unit": "requests",
             "window_kind": "fixed", "amount": "3"},
            {"pool_id": "weekly-percent", "unit": "percent",
             "window_kind": "fixed", "amount": "2.75"},
        ],
        "completion_seconds": None,
        "basis": "Owner 对完整 Attempt 的保守预测依据。",
    },
    principal=owner,
    command_key="register-feature-prediction",
)
```

示例必须与实际 Profile 的全部关联池一致，不能只登记最宽松的窗口。单位来自固定资源配置；次数和 token 必须为正整数，百分比点可以为正小数。所有数值遵守现有 `Quantity` 范围、六位精度及十进制字符串语义，允许科学计数法，拒绝 float、负数、零、溢出和多余/遗漏/重复池。预测最多 32 个池，Profile revision 和任务时限还须满足当前 Capacity 准入契约的可表示范围。

`completion_seconds` 仅接受显式 owner 预测或 `null`；不会拿 Task 的 `duration_seconds` 当完成时间。Task 的输入上下文上界及政策的输出预留分别绑定，均不转换成未经证明的多轮消费。接口不接受 `known`/`calibrated`、价格、资格或任意绑定摘要。

登记的每个版本不可替换；后续版本必须递增。相同 command key 的相同请求返回原记录，不重复登记；改参数返回幂等冲突。`get(project_id, id, revision, principal=...)` 返回原记录及独立撤销记录；`revoke(..., reason=...)` 保留历史。读取只检查该 Run/Task/Profile 最新登记，不因它被撤销或过期而退回更早的预测。

## 绑定和失效

服务端绑定项目/owner、Run、批准 receipt、plan revision/digest、授权及路由摘要、完整任务需求及摘要、context policy 及摘要、execution policy 摘要、完整 Profile/digest/runtime、完整登记、账号/通道以及全部池配置。任务需求包括角色、purpose、复杂度、风险、领域、路径、能力、工具、输入上下文和时限。

登记必须以一个实际已批准的 ready Task 为来源；新提案尚未批准时不会替换当前计划。新计划批准后，旧预测因绑定不同失效，需要 owner 登记新版本。当前 Profile、账号、认证引用、通道批准、权限、池或单位变化也拒绝旧预测。

观察时间由控制器生成，有效期是明确的 1–86400 秒。读取同时检查当前控制器时间和容量快照时间，不能用旧 `as_of` 让过期预测恢复有效；时钟回退同样拒绝。

`window_independent_attempt` 是 owner 明确声明“此完整 Attempt 预测适用于该池同类窗口”的语义，不依赖窗口长度进行比例换算。消费时必须为每个池提供可信容量来源当前的 account/kind/unit/window_kind/window_id；全部身份匹配后才绑定本次 window_id。窗口类型变化拒绝；重置到同类型的新窗口会生成新的 Estimate 窗口绑定，不会把旧窗口 Estimate 直接转移过去。真实预留仍须复查这些当前窗口和政策。

## 可信 builder 的消费契约

```python
# source 应在取得 guards 之前初始化。
with planner.activation_guard(run_id) as approved_run:
    with qualifications.routing_facts_guard(
        project_id, frozen_registrations, principal=owner,
    ) as qualified:
        result = source.estimate_locked(
            approved_run,
            task_id,
            profile_ref,
            current_catalog=qualified["catalog"],
            pool_windows=current_capacity_windows,
            as_of=capacity_captured_at,
        )
        # result = {estimate: Estimate 或 None,
        #           source_binding: 原登记记录或 None, reason_codes: [...]}
```

`estimate_locked` 是受信控制器端口：调用方必须持有 Run 和项目 guard，`current_catalog` 使用 qualification guard 的实际 `view.catalog`。它用独立只读 SQLite 连接查询估计和撤销，不开启嵌套 `BEGIN IMMEDIATE`；外层项目写事务会阻挡同时进行的登记/撤销。它还读取当前 catalog 核对传入内容，不能悄悄用旧资源材料。

普通 `estimate(run_id, task_id, profile_ref, principal=..., pool_windows=..., as_of=...)` 自取相同锁并共用解析。两者都只给内部控制器使用；没有接收任意外部容量快照的 HTTP 入口。这不声称 Run、项目、容量、预算和 Host 具有跨数据库原子事务。

公开测试 `tests/projects/test_demand_store.py` 通过项目/执行政策/Run/规划 receipt/计划批准的实际持久命令链测试登记与消费，包括真正的新计划批准、版本/撤销、身份变化、完整多单位向量、窗口变化和 guard 对并发撤销的阻挡。规划 receipt 在这些测试中明确来自测试替身，测试不调用模型，也不证明真实运行资格或收费上界。
