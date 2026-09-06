# Go 固定场景的持久 Profile 观察证据

本切片将服务端登记的 credential generation、固定隔离 Go suite 和持久资格入口连接起来。
固定文件的 read/edit 与越界读取检查仍不能授予任意 Task 的权限；`runtime_tools` 保持
`not_run`，`dispatch_eligible` 为 false，上下文容量和现金约束仍未取得资格。

设计与接口见 [实现说明](../../docs/implementation/m3-go-profile-qualification.md) 和
[下一切片安排](../../docs/planning/go-runtime-qualification-next.md)。原生隔离链路的前序证据见
[M2 隔离说明](../../docs/implementation/m2-opencode-go-isolated.md)。

## 已冻结的检查

| 检查 | 当前结果 | 原始证据与入口 |
|---|---|---|
| 持久入口独立 Spec | 当前 suite 源码下 5 passed，189.06 秒；真实 Linux native + HTTP fixture | [报告](../go-profile-qualification-spec/report.md)、[冻结](../go-profile-qualification-spec/freeze.json)、[独立测试](../go-profile-qualification-spec/test_public_persistence.py) |
| 持久入口独立 Standards | 4 passed；0 个硬性违规、0 个可操作异味发现 | [报告](../go-profile-standards/review.md)、[来源摘要](../go-profile-standards/review.json)、[独立测试](../go-profile-standards/test_store_failure_boundaries.py) |
| 固定 suite 作者验证 | 所有权修正后 Linux 18 passed；Windows 8 passed、10 skipped | [修正、前后证据和冻结](suite-ownership-correction/README.md)、[作者测试](../../tests/projects/test_go_suite.py) |
| Credential 作者验证 | Windows、WSL 各 22 passed | [说明](credential-source-evidence/README.md)、[冻结](credential-source-evidence/freeze.json)、[Windows 最终](credential-source-evidence/windows-frozen.xml)、[WSL 最终](credential-source-evidence/linux-frozen.xml)、[作者测试](../../tests/projects/test_credential_sources.py) |
| Project 集成回归 | suite 所有权修正前 WSL 206 passed；Windows 196 passed、10 个 Linux 场景 skipped；修正后的新增两例见上一行 | [WSL](go-profile-projects-wsl.xml)、[Windows](go-profile-projects-windows.xml)、[入口作者测试](../../tests/projects/test_runtime_qualification_store.py) |
| Routing/Web 回归 | Windows 130 passed、1 个 POSIX 场景 skipped | [结果](go-profile-routing-web-windows.xml) |
| 实测 CLI 独立 Spec | 修正前 5 passed、1 failed；修正后 6 passed | [问题与修正](../go-profile-cli-spec/report.md)、[原始输入及证据](../go-profile-cli-spec/review.json) |
| 固定 suite 独立 Spec | 最终 11 passed，90.67 秒；归属问题已关闭，包含明确时间回退拒绝 | [审查、历史与测试](../go-suite-independent-review/review.md) |

上述模型上游全部为合成 HTTP fixture，没有真实 Go 请求。Spec 的 30 次 fixture 请求实际经过
native、命名空间、UDS relay 和 SQLite；Standards 的四项使用明确的来源失败替身，没有 native
或 provider 执行。两者分别陈述自己的覆盖范围，作者测试不作为独立审查。

suite 独立检查曾有三次未记录完整内部原因的间歇性失败，不能由后续通过推断根因已消除。
只读采样另观察到 WSL 系统时钟回退约 2.48 秒；新增用例按该幅度验证公共入口拒绝并撤销授权，
零发送。非时间专项使用明确的稳定测试时钟，生产代码仍执行原有时间边界；历史三次失败未逐项
证实由该现象造成，详见独立审查记录。当前真实 Go 报告与该组测试使用相同产品源码。

原始失败历史保留在 [suite red](go-suite-red.xml)、[credential 首次 red](credential-source-evidence/red.xml)
及 credential 目录的后续 red/green 文件中。其 [作者说明](credential-source-evidence/README.md)
区分产品修正、fixture 时间戳修正及最终冻结结果；历史 XML 不代表当前仍有未解决失败。

Root 另外完成全树 Ruff、backend mypy（110 个源文件）、`uv lock --check`，以及不安装 dev extra
的生产环境 import 检查。前序 PR #53 的 `9bcf981` 两个 CI 运行
[PR](https://github.com/zhouy1017/Karajan/actions/runs/34020230826) 与
[push](https://github.com/zhouy1017/Karajan/actions/runs/34020228131) 已成功；它们不是本次尚未发布变更的 CI。

## 真实 Go 证据

本次公开持久入口已使用用户授权的 Go key 完成一次实测。`edit` 三次请求、`denied_read` 两次请求，
均收到完整 HTTP 200；四个修复样例通过，越界读取被原生权限拒绝。重开存储后以同一命令重放，
返回同一记录，总请求数仍为五。两个 grant 已撤销，本地进程停止已确认，服务端远程停止仍为 unknown。

[完整公开报告](live.report.json) 保留原始字节；[来源冻结与结果核对](live-freeze.json) 将其绑定到
该轮 `771339e` 的 12 个源码文件、Linux OpenCode 1.18.29 固定二进制和 [实测入口](run_live.py)。归档不包含
provider key、凭据私有库或项目数据库。`fixture-*` 仍是样例配置中的标识名称；报告的实际来源为
`official_go`，模型为 `glm-5.3-flash`。模型家族、订阅计费路径及配额值是样例登记，未由本次诊断验证。

首次 CI 的 Linux/前端通过，Windows 暴露了私有状态 owner 必须等于 TokenUser 的兼容问题。
[Windows 修正与独立复核](windows-owner-correction/README.md) 将 owner 限定到既定信任集合，
完整 DACL 检查继续执行；新增五项与原 22 项通过，Linux 行为未改。原实测报告和摘要不重写，
不把这次 Windows 文件变更归属于先前的真实 Go 执行。

本次命令在 WSL 执行，`<private-key-file>` 表示用户本地凭据路径；诊断目录必须尚不存在，
不能用重复命令重建额度。默认不加 `--live` 时返回 not_run 且不读取密钥：

```sh
PYTHONPATH=backend python examples/go-profile-qualification/run_live.py --live \
  --runtime /path/to/pinned-linux-opencode \
  --credential-file '<private-key-file>' \
  --directory /tmp/new-go-qualification
```

本次固定 scope 读取通过，普通路由仍返回 `TASK_PERMISSION_SCOPE_NOT_QUALIFIED`。
即使固定场景实测通过，也不代表任意 Task 路径、Reviewer/Commander、Collector 或整个 M2-05 已完成。

## 证据复制与复验

[复制清单](copy-manifest.json) 逐项记录原 `.cache` 路径、发布路径、大小和 SHA256；所列文件均逐字节一致。
独立测试保留与原 cache 相同的目录层级，冻结原文中的历史 cache 路径和命令未改写。
历史 Spec 输入以 [.py.txt](../go-profile-qualification-spec/history/before-suite-grant-ownership-fix/test_public_persistence.py.txt)
保留相同字节，避免与当前独立测试被同时收集；原 XML 和冻结元数据中的历史文件名保持不变。
仅发布 Markdown、Python 独立输入、JSON、XML 和静态检查文本，没有缓存字节码、数据库或私有凭据文件。

独立脚本会生成同目录观察文件。需要重现时，在独立临时 checkout 执行，使用仓库固定依赖与实际 Linux
runtime，并通过 `pythonpath=backend tests/projects tests/isolation` 运行 CI 所列独立目录；上游继续保持 fixture。
这里保存的冻结证据不因复验而覆盖。
