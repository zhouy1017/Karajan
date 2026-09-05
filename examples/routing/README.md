# Rulebook 离线编译与路由模拟

本切片提供严格编译器、同快照可重放的纯求解器及本机 JSON CLI。所有资格输入均为 `fixture` 或 `imported_observation` 的模拟事实，输出固定 `scope=simulation_only`、`live_qualification=not_run`、`activation_allowed=false`。选中候选只表示它通过所给快照的路由判断，不是账户启用、真实资格验收或 F05 启动授权。

最终验证为 78 项测试通过，见 `routing-results.final.xml`；最终回放及 Schema 使用 `.final.json` 后缀。首轮 73 项报告和原固定回放保留原文件，预算输入修复的红→绿记录另存 `budget-input-before.xml` 与 `budget-input-after.xml`。

## 公共入口

```python
from karajan.routing import compile_rulebook, evaluate_route, fixture_from_configuration

compiled = compile_rulebook(rulebook_document)
result = evaluate_route(task_snapshot, policy_snapshot, capacity_snapshot)
```

`compile_rulebook` 成功返回 `schema_version=karajan.routing.compiled.v1`、`compiler_revision`、规范 `document`、`rulebook_sha256`、`issues`、`warnings` 和 `activation_allowed=false`。摘要只覆盖可执行字段，不含 id/revision/status/description；版本身份另存。结构错误抛出 `RoutingError`，其 `code` 与 `issues:[{path,code}]` 可序列化，不回显输入值。静态歧义等 `issues` 供发布门禁阻断；空组用 `warnings` 表达等待绑定。编译不宣称有真实可用 Profile。

`evaluate_route` 返回规范完整输入及各自 SHA、算法和编译器版本、命中规则、解析组、全部候选与淘汰原因、每个适用池的独立分量、排序值和选择结果。无匹配、当前任务最高优先级歧义、T0、缺可信风险映射、阶段未批准等在 `reason_codes` 中解释；非法输入抛出 `RoutingError`。它不读取数据库、系统时间、账户、环境密钥或网络，不修改输入，不创建预留。CLI 额外把整个后端 Python 源码 SHA 绑定进回放报告。

`fixture_from_configuration(configuration, as_of=...)` 仅为明确的合成样例生成器：限定 `fixture-runtime / fixture-model / auth_mode=none`，不把未启用 Profile 改成启用。它合成的配额、需求、角色、工具、上下文和时间证据均明确标记 fixture，不能用于从普通真实配置导出运行快照。凭据字段在原生设置中出现时，生成器与求解器均在导出前拒绝。

## 冻结输入契约

完整类型以同目录四份 `*.schema.json` 和 `fixed-input.json` 为准。

| 输入 | 内容与权威边界 |
| --- | --- |
| TaskSnapshot | Task/root/Plan 身份、原授权摘要、工作要求、预定 Attempt/context、作者和集成范围、阶段与根链累计轮次。授权包含具体 Profile/ceiling 集、冻结 `approved_groups`、允许阶段和 `approved_quality_stage_indices`、工具/渠道/数据去向/原币预算上限。 |
| PolicySnapshot | 严格 Rulebook、既有 ResourceCatalog/F01 Profile 结构、项目批准集和硬约束、独立可信风险映射/路径下限、有版本/摘要/时间/出处的 Profile 事实。 |
| CapacitySnapshot | 固定 as_of、快照 id/revision、账户当前 Policy revision 与实际 Policy、并发及冷却/耗尽状态、各池窗口/单位/观察/上限/剩余/未覆盖/未来预留、逐 Profile 完整需求向量、账户和预算各自原币余额、版本化价格与可选 FX。 |

这些快照都是调用方已验证的可信输入。求解器检查绑定一致性，但不会证明调用方提供的授权摘要、当前 Policy 标记、作者身份或观察出处来自真实权威。后续 Host 必须从批准记录、固定 Profile、当前容量账和独立风险规则导出，不能让模型或前端自行提交“可信事实”。`authorization_digest` 是原批准记录的引用；实际路由子集另随 Task 快照摘要保存，不把该子集哈希冒充原记录哈希。

质量升级按 `quality_escalation_groups[quality_stage_index]` 单组激活。必须有 `QUALITY_FAILED`、已批准索引和冻结组成员、原批准来源、剩余修复轮次；不能把全部升级组混为普通候选，也不能用新组成员扩大旧批准。实际修复计数和阶段历史由协调器提供，求解器不自行累加。

硬门槛先于偏好：角色/难度/能力证据、工具/上下文/隔离、数据去向、Review 独立上下文与 T3 家族、账户当前 Policy、全部池和全部现金上界同时检查。普通 Worker 和可选顾问均不能使用 Commander 保护；规则显式 `lead_reserve_access=false` 可进一步限制 lead。未知观察、未知需求或未知窗口只能使用有限保守模式，其更小并发上限仍扣保护槽。

压力按每个可量化池 `(limit - remaining + local_uncovered + future_reserved + safety + role_reserve + demand) / limit` 计算，取可计算值最大值；无可计算值时明确 null，不填零。混合未知池仍保留不确定性档位 2 和各池原始分量。服务池与本地 allowance 各自计算，不互相重复扣减。

现金必须有逐调用控制、全部调用覆盖和有效价格上界，并逐项满足授权、配置预算、剩余预算、账户余额的同币种上限。排序使用参考 FX；缺任一适用汇率时，整个候选集合跳过现金项。未知成本/压力/完成时间均排在对应已知值之后。汇率不参与预算换算。

## 重跑

从仓库根目录设置 `PYTHONPATH=backend` 后运行：

```text
python -m karajan.routing compile --input docs/architecture/examples/rulebook.v1.json --output compiled.json
python -m karajan.routing evaluate --input examples/routing/fixed-input.json --output replay.json
python -m karajan.routing schema --kind task --output task.schema.json
python -m karajan.routing fixture --input examples/projects/offline-configuration.json --as-of 1000 --output fixture.json
python -m pytest tests/routing -q
```

CLI 输入只接受单个 JSON 对象，拒绝重复键、非有限数、非法 Unicode 与超过 2 MB 的输入；错误输出不包含原输入。回放不支持执行器、密钥、endpoint 或启用账户的参数。

## 本切片不授予的能力

配置发布权威仍是 ProjectRegistry；本模块没有创建第二个发布存储。现有项目/Run/Host/Web 尚未切换到该求解器。实际派发仍需统一 Host 事务重查授权、任务、健康状态、项目 Writer/Run 总次数/时间上限，并同时记录资源预留、Attempt 和 outbox。资源快照之间存在时间差，模拟结果不能替代原子准入。真实价格、服务能力和多账户资格测试仍未运行。
