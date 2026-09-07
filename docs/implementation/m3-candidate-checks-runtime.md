# 固定 Check 进程的部署入口

对应 [#94](https://github.com/zhouy1017/Karajan/issues/94)。这份文档说明 Check 的 controller 部署边界；原检查组合见[证据索引](../../examples/candidate-checks/README.md)。#100/#101 的绑定与版本消费已实现，新增 C/P 见[版本交接证据](../../examples/reviewer-validation-subject/README.md)，实现固定为 `1fc97849697cfe89a79595cba07e9ec028c6d0b2`。正式 Reviewer 资格与部署配置、模型 Review、当前候选 G 和真实服务整链 S 仍未完成。

`CheckSettings` 固定控制目录、已有业务库目录、CandidateStore、RunnerHost、检查结果根目录、controller Python 和允许的仓库根。`CheckEnvironmentSource(id, revision, directory)` 把已批准环境引用解析到 controller 预置的 Python image。环境的实际来源摘要必须与原 ExecutionPolicy 相同，未支持的环境不能自动替换。

`write_check_bootstrap(settings)` 是显式部署动作，只创建一个私有 `candidate-check-bootstrap.json`，不创建业务数据库、环境资产或账户材料。文件已存在就拒绝覆盖。该文件严格拒绝重复 JSON 字段、未知字段、重复环境身份和非规范路径；部署路径不得位于任何注册仓库内，检查结果区域与控制、数据库、CAS、Host、环境资产分开。

`open_check_services(settings, run_id=..., operation_id=..., principal=..., for_execution=False)` 重新打开既有数据库并检查 owner。缺失或不完整的原权限账本不会创建替代数据库。历史模式不解析环境 image 或当前 controller 源码；可选执行资源丢失时，读取原 operation 和能够完成的清理仍可进行。需要新效果时显式使用 `for_execution=True`，再检查当前固定来源和实际环境资产。

Host 运行固定的 `_candidate_check_runner.py`，采用 controller Python 的 `-I` 模式，工作目录为私有控制目录。其参数只有 Run、operation、check_run、principal 四个原始 ID。入口不接命令、环境变量、候选路径、模型身份或结果。原生 claim 已消费、已有结果或已取消时，仅通过历史模式核对；不会因为重复启动入口而重新读取当前环境并执行候选代码。

版本交接后，固定入口按 Check ID 同时查当前与已归档 cycle。旧 cycle 或 pending transition
下的入口只恢复历史，不启动候选命令。factory 复用同一个真实 Project/qualification Store
创建 `ApprovedReviewerBindings`，把其 `current_locked` 注入 Check 消费者；ready 安装和
已安装版本的新效果都在原 operation → Run → Project guards 内核对当前来源。
生产没有调用者提供的 validator、资格 JSON 或 fixture 开关。

独立 `CheckAttemptManifest` 记录批准的环境和完整检查执行摘要，不需要 model/channel/account/billing 字段。Host 的原模型 AttemptManifest JSON 和模型探针契约保持原样。固定 child 必须先等待 supervisor 登记其真实 PID/birth，再由消费者在实际 namespace 进程创建前获得当前 operation、Run、Project 和 Host guards。Host 启动本身不代表已获得执行候选命令的资格。

检查环境为 `python312-stdlib` / `linux_x64`。controller 显式 provision 固定的本机 Python 3.12 资产，不下载依赖；检查进程只看到经验证的只读 image、完整候选的可写临时副本以及受限临时目录。实际 stdout/stderr 由外侧收集，退出状态、超时、取消、停止与日志完整性决定 Evidence，输出文本中的“passed”没有授权作用。

最终每项 Check 的完整 request 和实际日志 hash/size 在提交 Evidence 前持久化。`CandidateStore.lookup_evidence` 只查精确历史，既不运行检查，也不重写日志。找回提交记录不表示日志仍可用或当前 gate 有效，调用者仍需核对当前控制状态和 Candidate gate。必需 Review 未完成时，候选继续保持不可交付。

正常 factory 的 19 项回归中，新增 ready B 反例在真实资格 Store 未支持 Reviewer 时以
`REVIEWER_QUALIFICATION_REQUIRED` 拒绝安装，原历史不变且零 Host.prepare。该拒绝证明接线，
不证明正向 Reviewer 角色资格或认证 generation 已可在正式部署中消费。当前 factory 尚未
配置可接受的 Reviewer suite/credential runtime，正式正向路径继续 blocked。

新增两项 handoff P 使用仓库内单独的固定测试 child，将其自身源码摘要纳入实际 controller
来源，并显式替换资格边界；它不属于正式部署配置。实际 Host/namespace 执行五个 Check
进程：A/B 各两项全部通过，另一活跃 A 在并发取消后确认停止且第二项没有启动。外部取消
可呈现 `completed / exit_code=-9 / local_stop=confirmed`，这是业务 cancelled，不是 passed。
139 份实际 backend 来源在执行前后及五份持久执行描述中一致。

规划/作者/资格替身只证明相应 C/P 边界；真实 Commander 桥属于 [#93](https://github.com/zhouy1017/Karajan/issues/93)，真实只读 Reviewer 及其 Review Evidence 属于 [#95](https://github.com/zhouy1017/Karajan/issues/95)，生产 GitHub 交付仍属于 #14。不能把固定测试部署提升为正式正向资格或用本地报告宣布当前 PR CI/G 完成。本票不调用推理服务。
