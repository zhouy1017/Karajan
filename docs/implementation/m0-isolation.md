# M0-06：WSL2 固定 canary 隔离探针

对应 [M0-06 / #7](https://github.com/zhouy1017/Karajan/issues/7)。本切片完成一条真实的本地 OS 边界探针：允许合成工作区读写，同时对生成的假秘密、本地服务和测试 Git remote 做访问对照。它不运行 Codex/OpenCode，不接触真实认证文件，也不宣布整个 M0-06 或任何真实 Profile 已合格。

2026-09-05 的实际结果：WSL2 Ubuntu 上 13 项固定 Python canary 检查均为 `passed`；原生 Windows 的该执行路径为 `unsupported`，13 项实际检查均为 `not_run`。两份报告始终保持 `runtime_tools_status: not_run`、`dispatch_eligible: false`。

## 本机路径和依赖证据

| 项目 | 实际观察 |
|---|---|
| WSL | 已安装 Ubuntu，版本 2；本次启动后内核为 `6.18.33.2-microsoft-standard-WSL2`，现有用户 uid/gid 为 1000 |
| Python / Git / unshare | `/usr/bin/python3` 为 3.12.3；系统 Git、util-linux unshare/mount 可用；没有新增依赖 |
| 容器工具 | Windows 与 Ubuntu 均未发现 Docker/Podman；Ubuntu 没有 bwrap；没有安装、拉镜像或修改账户/全局权限 |
| 命名空间 | 实际运行用户、挂载、PID、网络命名空间成功；本次不需要特权容器或宿主 root |
| WSL 默认出口 | 宿主 `/mnt/c` 和 WSLInterop 默认存在，不能把“进入 WSL”本身当作隔离 |
| 工具可见性 | Codex 外层命令沙箱最初枚举 WSL 返回 `E_ACCESSDENIED`；经受控宿主执行才取得上述事实。枚举失败不能误报为 WSL 不存在 |

Linux `prctl`、capability ABI 常量取自本机 `/usr/include/linux/prctl.h`、`/usr/include/linux/capability.h`，并由实际能力观察和拒绝操作验证。此探针当前针对 Ubuntu x86-64/Python 3.12 布局，不是全部 Linux 发行版或架构适配器。

## 公开接口

```text
python -m karajan.isolation probe --spec <JSON> --directory <全新临时目录>
run_probe(spec, directory)
require_qualified(report, exact_binding, scope="runtime_tools")
```

输入为 `karajan.isolation.probe.v1`，只接受 `schema_version`、`case_id`、`binding`。绑定包含 Attempt/fence、Profile ID/revision/digest、`runtime_kind: python-canary`、Python 版本和 `execution_path: unshare-chroot-v1`。未知字段、布尔 fence、其他运行时类型会在创建目录前拒绝。没有任意命令、endpoint、凭据路径或需要测试的宿主目标参数。

输出目录必须是系统临时根下尚不存在的目录；不能选仓库或覆盖既有证据。探针自行生成工作区、独立 `.git`、本地 bare remote、五类假秘密、网络服务和 marker。所有读写攻击目标均在这个临时根中；唯一额外读取的是明示的系统运行库及用于固定 echo 对照的系统 `cmd.exe` 二进制。

CLI 输出 `karajan.isolation.report.v1` 并保存 `report.json`：固定 canary 通过退出 `0`；环境不支持退出 `2`；失败或未完成退出 `1`。输入拒绝只输出稳定原因，不回显输入内容。可用环境中每个检查有独立状态、执行边界类别和观察证据；环境不能运行时保留全部必需检查为 `not_run`。

`require_qualified` 默认请求 `runtime_tools`，始终拒绝本票报告。只有显式指定 `scope="fixed_python_canary"`、精确匹配完整绑定、具备全部 13 个通过检查，才能取得一个仍带 `dispatch_eligible: false` 的证据确认结果。改变 scope、删除必需检查、换 fence 或把 JSON 中的运行时资格改成 passed，不会使真实运行时可派发。这是可信控制面的报告一致性门；报告摘要用于关联证据，不是签名或防伪认证。

## 实际隔离构造

受信任引导在一次性子进程中运行：

```text
unshare --user --map-root-user --mount --propagation private
        --pid --fork --kill-child=KILL --net ...
```

映射 root 仅用于新用户命名空间里的准备。引导建立全新 chroot，把工作区作为可写挂载；提供独立 `/tmp`、新 PID 命名空间的 `/proc` 和 `/dev/null`、`zero`、`urandom`。不挂载用户 home、`/mnt/c`、`/init` 或宿主 socket 目录。

只读运行目录固定为：

- `/usr/bin`
- `/usr/lib/x86_64-linux-gnu`
- `/usr/lib/python3.12`
- `/usr/lib/git-core`
- `/usr/lib64`

`/bin`、`/lib`、`/lib64` 是指向上述目录的沙箱内链接。整个新根也只读；`/workspace`、`/tmp` 是明确的可写例外。最初尝试整体绑定 `/usr` 被 WSL 的子挂载约束拒绝，最终使用上述较窄路径，没有隐藏系统失败或修改宿主挂载表。

chroot 后先切换 cwd，再清除 capability bounding、effective、permitted、inheritable、ambient 集，并设置 `no_new_privs`。运行固定 canary 时仅传递 PATH、LANG 和临时 HOME，关闭额外继承描述符。引导和 canary 没有执行用户提供的命令；本票没有提供通用 sandbox-run 接口。

## 13 项观察及其证明范围

| 检查 | 实际对照与结果 |
|---|---|
| `workspace_read_write` | 读取允许输入并写回新内容成功，证明并非禁用全部工具 |
| `protected_files` | 平台、Git 凭据、其他 Attempt、broker 管理、订阅认证五个假文件在控制面可读；沙箱中均不可读写；宿主字节保持不变 |
| `symlink_escape` | 工作区指向外部假文件的链接在控制面可读，沙箱中不可读 |
| `host_proc` | 同 uid 的控制进程 `/proc/<pid>/root` 路径可到达假文件；沙箱的新进程视图不能借此读取 |
| `environment` | 对照子进程可见注入的假 secret 环境变量；执行 canary 不可见 |
| `inherited_fds` | 对照子进程获得刻意设为可继承的假文件 FD；沙箱没有继承其 inode/device 身份 |
| `capabilities` | 实际 `/proc/self/status` 五组能力均为 0，NoNewPrivs 为 1；重新 mount、chroot 及改写生成的只读 canary 均失败 |
| `wsl_interop` | 把系统 `cmd.exe` 复制为临时 PE 对照，固定 `/D /Q /C echo KARAJAN_WSL_CANARY` 在宿主可执行，在沙箱内不可执行；`/D` 禁止 AutoRun |
| `network_endpoints` | 四个回环假服务代表 control、broker-admin、provider、delivery；宿主收到 4 次请求，沙箱尝试后新增收包为 0；未访问真实网络服务 |
| `git_remote` | 工作区拥有独立 `.git`；沙箱 Git 仍可读该仓库，但不能向临时 bare remote 写入；对照 push 成功，隔离尝试后 remote head 不变 |
| `process_cancel` | 实際观察至少 3 个受控进程的 PID/创建 ticks 和持续增长的 heartbeat；取消新建 unshare 后，原进程身份不再可执行，heartbeat 停止；`remote_stop` 保持 unknown |
| `candidate_collection` | 所有已观察 writer 停止后才收集固定 `allowed.txt`。假 hook 直接运行可以写 marker；配置了 fsmonitor、hooks、filter 与合成 MCP 文件后，收集没有改变 marker。产物由原始字节和 SHA-256 固定，不运行 Git |
| `runtime_binding` | canary 报告实际 Python 版本/可执行路径，与声明比较；报告同时记录内核、Python 二进制摘要及探针源文件摘要 |

外部进程身份核对只针对本次固定、已观察完整的三进程夹具；没有实现未知进程接管、历史恢复或通用 supervisor。退出码 `-9` 是本次主动取消的真实观察，不伪造为进程自然成功。固定文件收集使用 `O_NOFOLLOW`、普通文件检查和 1 MB 上限；没有声称已实现整个产品的 materializer/collector。

## 复现和实际产物

从 Windows 仓库终端选择新的临时根，并按当前仓库位置填写 WSL 路径：

```powershell
$linuxRepo = '/mnt/c/Users/Chooo/Playground/Karajan'
$probeRoot = '/tmp/karajan-isolation-' + [guid]::NewGuid().ToString('N')
wsl.exe --distribution Ubuntu --exec env "PYTHONPATH=$linuxRepo/backend" python3 -m karajan.isolation probe --spec "$linuxRepo/examples/isolation/python-canary.json" --directory $probeRoot
```

输入：[python-canary.json](../../examples/isolation/python-canary.json)。实际产物：[WSL2 报告](../../examples/isolation/wsl2.report.json)、[Windows unsupported 报告](../../examples/isolation/windows.report.json)。它们由当前 CLI/公开入口实际运行生成；端口、PID、时间和本地 Git commit ID 每次可能改变。

初次 WSL 证据目录为 `/tmp/karajan-isolation-evidence-ucshc2ez/probe`；状态修复后的当前源码报告保存在 `/tmp/karajan-isolation-fix-56mxa54t/normal`，真实中断报告在同目录 `interrupted`，并导出 [中断报告](../../examples/isolation/interrupted.report.json)。保留 report、工作区、假目标和候选以供核对。所有目录由探针新建，没有清理用户目录、修改宿主账户或全局权限。

## 测试与红绿证据

Windows 当前测试结果为 5 passed、8 个明确的 Linux/WSL 跳过、3 个输入校验子测试通过。WSL2 设置 `KARAJAN_REQUIRE_UNSHARE=1` 后运行 13 个 unittest：12 个通过，1 个 Windows 专用用例跳过；实际隔离用例不允许以环境跳过代替通过。Ruff 与 Windows/Linux 两个平台的严格 mypy 均通过，共 7 个源文件，无新增第三方依赖。

审查补充的真实故障回归通过公开 CLI 启动探针，在宿主观察 `allowed.txt` 已变为 `allowed update` 后，只终止该 CLI 新建的 `/usr/bin/unshare` 子进程。红阶段实际返回 exit 2/unsupported；修复后返回 exit 1/failed、`NAMESPACE_EXECUTION_UNCONFIRMED`，保留 supervisor PID/创建 ticks、实际 exit -9、已观察写入以及缺少完整 canary 报告的事实。未观察的 13 项维持 not_run，不能进入默认测试的 unsupported 跳过分支。只有明确的 unshare 不支持/拒绝错误且无写入，或明确缺少所需系统程序时，才归为 namespace unavailable；空 stdout 或异常退出本身不证明环境不支持。此失败不会撤销已有副作用，也不会证明未观察进程或远端已停止。

```text
.venv/Scripts/python.exe -m pytest tests/isolation -q --tb=short
.venv/Scripts/python.exe -m ruff check backend/karajan/isolation tests/isolation
.venv/Scripts/python.exe -m mypy backend/karajan/isolation
.venv/Scripts/python.exe -m mypy backend/karajan/isolation --platform linux
wsl.exe --distribution Ubuntu --exec env PYTHONPATH=/mnt/c/Users/Chooo/Playground/Karajan/backend KARAJAN_REQUIRE_UNSHARE=1 python3 -m unittest discover -s /mnt/c/Users/Chooo/Playground/Karajan/tests/isolation -p test_isolation_probe.py -v
```

按公开 CLI/库入口逐轮观察红→绿：模块缺失；Linux 路径错误报告 unsupported；环境/FD/proc 观察项缺失；网络/Git 观察缺失；互操作和权限操作观察缺失；实际取消观察缺失；候选收集缺失；资格门缺失及错误绑定创建目录；Windows 报告遗漏必需 not_run 项。现有目录不覆盖、仓库目标拒绝等既有保护的验证直接通过，未冒称红绿循环。

## 仍需完成的 M0-06 / M0-07 边界

本票提供可接线的固定引导思路和实际 OS 证据，尚未把 `RunnerHost.ProcessSpec` 接入 namespace 生命周期。RunnerHost 对同一目标的写占用释放、失联核对、故障重启仍需组合验收；本探针只在确认已观察子树停止后收集自己的临时文件，不声称已经实现平台写租约。

Codex/OpenCode 真实原生文件工具、MCP/hooks、动态审批、实际认证存储、订阅控制进程与工具身份的分离均为 `not_run`。API runner 的全部模型出口经 broker、broker 可用网络白名单，以及相应 credentials 生命周期也尚未接线。本次网络命名空间是完全隔离的探针网络，不能直接用于需要访问真实 broker 的运行时。

假秘密只能证明这些具体路径在本次构造下的访问结果。不能据此推断真实认证路径安全，不能拿本报告为真实 Codex/OpenCode Profile 授予 `tool_sandboxed` 或 `attempt_isolated`。上述必需边界保留在 `remaining_required_checks` 中，未通过删掉检查来宣称整个 Issue 完成。
