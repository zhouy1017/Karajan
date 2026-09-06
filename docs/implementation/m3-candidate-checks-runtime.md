# 固定 Check 进程的部署入口

对应 [#94](https://github.com/zhouy1017/Karajan/issues/94)。这份文档说明 Check 的 controller 部署边界；已验证的本地组合见[证据索引](../../examples/candidate-checks/README.md)。Review 版本绑定、当前 PR CI 和真实服务整链仍未完成。

`CheckSettings` 固定控制目录、已有业务库目录、CandidateStore、RunnerHost、检查结果根目录、controller Python 和允许的仓库根。`CheckEnvironmentSource(id, revision, directory)` 把已批准环境引用解析到 controller 预置的 Python image。环境的实际来源摘要必须与原 ExecutionPolicy 相同，未支持的环境不能自动替换。

`write_check_bootstrap(settings)` 是显式部署动作，只创建一个私有 `candidate-check-bootstrap.json`，不创建业务数据库、环境资产或账户材料。文件已存在就拒绝覆盖。该文件严格拒绝重复 JSON 字段、未知字段、重复环境身份和非规范路径；部署路径不得位于任何注册仓库内，检查结果区域与控制、数据库、CAS、Host、环境资产分开。

`open_check_services(settings, run_id=..., operation_id=..., principal=..., for_execution=False)` 重新打开既有数据库并检查 owner。缺失或不完整的原权限账本不会创建替代数据库。历史模式不解析环境 image 或当前 controller 源码；可选执行资源丢失时，读取原 operation 和能够完成的清理仍可进行。需要新效果时显式使用 `for_execution=True`，再检查当前固定来源和实际环境资产。

Host 运行固定的 `_candidate_check_runner.py`，采用 controller Python 的 `-I` 模式，工作目录为私有控制目录。其参数只有 Run、operation、check_run、principal 四个原始 ID。入口不接命令、环境变量、候选路径、模型身份或结果。原生 claim 已消费、已有结果或已取消时，仅通过历史模式核对；不会因为重复启动入口而重新读取当前环境并执行候选代码。

独立 `CheckAttemptManifest` 记录批准的环境和完整检查执行摘要，不需要 model/channel/account/billing 字段。Host 的原模型 AttemptManifest JSON 和模型探针契约保持原样。固定 child 必须先等待 supervisor 登记其真实 PID/birth，再由消费者在实际 namespace 进程创建前获得当前 operation、Run、Project 和 Host guards。Host 启动本身不代表已获得执行候选命令的资格。

检查环境为 `python312-stdlib` / `linux_x64`。controller 显式 provision 固定的本机 Python 3.12 资产，不下载依赖；检查进程只看到经验证的只读 image、完整候选的可写临时副本以及受限临时目录。实际 stdout/stderr 由外侧收集，退出状态、超时、取消、停止与日志完整性决定 Evidence，输出文本中的“passed”没有授权作用。

最终每项 Check 的完整 request 和实际日志 hash/size 在提交 Evidence 前持久化。`CandidateStore.lookup_evidence` 只查精确历史，既不运行检查，也不重写日志。找回提交记录不表示日志仍可用或当前 gate 有效，调用者仍需核对当前控制状态和 Candidate gate。必需 Review 未完成时，候选继续保持不可交付。

规划/作者返回替身只用于本票 C 行为夹具；实际 Git/CAS、Host、隔离检查和故障恢复分别记录 P。真实 Commander 桥属于 [#93](https://github.com/zhouy1017/Karajan/issues/93)，真实只读 Reviewer 属于 [#95](https://github.com/zhouy1017/Karajan/issues/95)，生产 GitHub 交付仍属于 #14。本票不调用推理服务。
