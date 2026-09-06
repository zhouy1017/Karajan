# 持久 Profile 资格事实

`ProfileQualificationStore` 将资格观察保存到现有 `ProjectRegistry.database`。它为批准 Run 的可信输入组装提供直接消费接口；求解器仍不能将用户填写的 `passed`、旧离线探针或诊断报告当成已运行资格。

`qualify_local_fixture` 读取项目当前批准的精确 Profile，在项目允许的 fixture 根目录下新建独立目录，运行本仓库固定脚本的 write、check、review 三个子进程，核对实际文件和结构化输出，再写入不可变结果。它不读密钥、不访问服务、不运行仓库代码，也不是 OS 工具沙箱。本页描述这一范围；新增的 [`qualify_runtime_tools` 固定 Go 入口](m3-go-profile-qualification.md) 使用单独的凭据来源、suite 和 scope，同样不能自动满足任意 Task 的执行要求。

## 接口

```python
store = ProfileQualificationStore(projects)
observation = store.qualify_local_fixture(
    project_id,
    {"id": "fixture-profile", "revision": 1},
    principal=owner,
    command_key="qualify-fixture-1",
    fixture_root=fixture_root,
    validity_seconds=3600,
)

with store.routing_facts_guard(
    project_id,
    frozen_registrations,
    principal=owner,
    scope="runtime_tools",
) as view:
    current_catalog = view["catalog"]
    # 每个 profiles 项包含 profile、qualification 或 None、reason_codes。
    # 无合格资格时保留理由，不能从 current_catalog 的声明补造能力。
```

`facts_for_profile(project_id, frozen_registration, principal=..., scope=..., fixture_root=...)` 是单 Profile 读接口，返回事实和原观察；无事实则抛出带稳定 `code` 的 `QualificationError`。`get` 返回原记录与独立撤销记录，`revoke` 不覆盖原观察。全部接口检查项目所有者。

`routing_facts_guard` 与单项读取共用同一投影检查。guard 在 yield 期间持有项目 SQLite 事务，所以当前资源、资格及撤销不会在调用方的准入检查中途改变。调用方不要在该 guard 内嵌套调用同库 `ProjectRegistry` 公共读方法。这个锁只覆盖项目数据库，不能代表 Run、容量、预算和 Host 已获得跨数据库原子性。

## 来源与边界

观察绑定完整 Profile（模型、runtime、认证引用、channel/account、计费路径、原生参数、权限及准入/用量粒度）、完整登记及关联账户/通道、仓库身份。执行身份还包含操作系统与发行版本、体系结构、Python 版本/路径/可执行文件摘要、固定脚本路径/摘要、生产模块摘要和 fixture 根目录。更改这些绑定需要新观察。

记录的 `fact_sources` 区分实际子进程观察与 owner 配置声明。`model_family`、`max_class`、`required_isolation` 及旧 `capability_evidence` 作为配置身份保存，绝不因绑定它们而变成已验证能力。当前输出如下：

| 字段 | 当前结果与含义 |
|---|---|
| provenance / qualification_scope | `fixture` / `local_fixture`，真实本地进程运行的合成任务 |
| roles / tools | 仅固定 worker/reviewer 操作和 `fixture-tools` |
| capabilities | 仅 `fixed_fixture_write/check/review` |
| context_tokens | `null`；固定脚本没有测量语言模型上下文 |
| budget_enforcement | `unknown`；没有现金调用资格 |
| data_destination | `local_fixture`；固定脚本无网络操作，不声称网络被 OS 阻断 |
| runtime_tools_status / live_qualified | `not_run` / `false` |
| dispatch_eligible / activation_allowed | `false`；观察本身不能启动执行 |

本地 fixture 的 `runtime_tools` 读取仍返回 `RUNTIME_TOOLS_NOT_QUALIFIED`。固定 Go 入口产生的有效官方观察会进一步指出 `TASK_PERMISSION_SCOPE_NOT_QUALIFIED`，因为其文件范围尚不能表达为任意 Task 的权限材料。不存在通用导入 `passed` JSON 的接口；`imported_observation` 必须由对应受信执行路径生成，不能复用当前固定脚本或现有诊断报告授予真实自主工具资格。

## 持久和失效行为

运行进程前先写 start。相同 owner/command_key 的完成请求返回原记录，不重新测试；参数不同返回 `IDEMPOTENCY_CONFLICT`。已写 start 但结果未知时返回 `QUALIFICATION_IN_PROGRESS_OR_UNKNOWN`，不会自动重放。最新记录按同一项目、精确 Profile、scope 和 suite 选择；匹配的后来观察失败或未完成时，读取不会退回更早的通过结果。`get_command_start` 允许在丢失响应后用原请求编号找回 start。

观察时间由控制器生成，有效期必须为明确的 1–86400 秒；到期或时钟回退拒绝读取。撤销保留原记录，重启不会恢复它；当前批准集合、登记或账户/通道变化同样拒绝旧事实。资格运行期间配置改变会留下失败结果，而不是新身份的通过证据。

验证入口为 `tests/projects/test_qualification_store.py`。它通过公共服务操作真实项目数据库和固定进程，覆盖读回、撤销、身份各轴变化、过期、缺失/未知来源、失败覆盖、并发幂等，以及 guard 对并发撤销的实际阻塞。fixture 数据不证明真实模型通过率、工具沙箱或模型审查能力。
