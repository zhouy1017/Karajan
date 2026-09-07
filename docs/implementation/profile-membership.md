# Rulebook 的资格集合接口

对应 [#99](https://github.com/zhouy1017/Karajan/issues/99)。

`evaluate_profile_membership(task_snapshot, policy_snapshot, *, as_of)` 回答：根据
这些版本化输入，哪些 Profile 满足当前阶段与静态资格要求。调用方不必构造容量、费用或
调用需求估算；结果不读取真实时间、账户、凭据或数据库。

它与原 `evaluate_route`、`evaluate_reserved_profile` 共享分类、Rulebook 选择、阶段、
批准分组、权限、角色/能力、上下文和作者独立性判断。正常阶段与已支持的质量阶段保留
原规则；未经批准或尚未达到的质量升级不会扩大集合。完整路由继续检查配额、时长、现金
和优先级，再决定实际候选。资格集合不承担这些资源准入职责。

结果包含输入与 Rulebook 摘要、选中的规则、最终等级、每个 Profile 的拒绝原因，以及
按身份排序的 `eligible_profiles`。这只是集合顺序。`selected_profile=null`、
`activation_allowed=false`、`dispatch_enabled=false` 和 `live_qualification=not_run`
始终明确。有限数值 as_of 必须由可信调用方提供；bool、字符串、NaN/Infinity 均拒绝。

Reviewer 绑定编译器需要从原批准 Run、完整作者来源和当前资格存储取得真实输入，不能
把 caller 提供的 snapshot 当当前授权。现有 Go Worker 资格不能借用于 Reviewer；真实
只读 Reviewer 资格与后续资源准入、执行和证据仍由 [#95](https://github.com/zhouy1017/Karajan/issues/95) 完成。

实现 `3a8cc5875b075285ab18796d1ab4bc36303192a1` 从 dev 独立交付。作者和独立复核在
Windows/WSL 各通过 156 项，14 份原完整路由结果一致；原失败、LF 转换和最终源码摘要见
[证据索引](../../examples/profile-membership/README.md)。这些是纯函数 C 证据，当前 PR CI
与合并需另行核对。
