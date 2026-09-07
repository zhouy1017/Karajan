# 测试与合并质量门

仓库的自动检查入口是 [CI workflow](../../.github/workflows/ci.yml)，稳定汇总检查名为 `quality-gate`。它证明当前提交通过了仓库内已经实现的检查；它不代表全部 PRD 已完成，也不代表任何真实模型服务取得接入资格。

## 自动运行范围

截至 2026-09-05，[PR #31](https://github.com/zhouy1017/Karajan/pull/31) 已完成真实失败与恢复演练。提交 `3187f65` 的两套 Python 检查及汇总检查均失败，GitHub 返回 `mergeable_state=blocked`；移除临时用例后的 `e8f7142` 在 [PR 运行](https://github.com/zhouy1017/Karajan/actions/runs/33963017034) 和 [push 运行](https://github.com/zhouy1017/Karajan/actions/runs/33963015622) 均通过。

主分支的 `main-quality-gate` ruleset（ID `22331721`）已启用，要求 PR、当前基准上的 `quality-gate`（绑定 GitHub Actions，integration ID `15368`），禁止强推和删除，绕过名单为空。配置与失败历史保存于 [门禁证据](ci-gate-evidence.json)。这份记录对应上述具体提交；以后的变更仍须取得其自身的成功检查。

每个 pull request、向 `main` 或 `codex/**` 的 push，以及 merge queue 的 `merge_group: checks_requested` 都运行检查。不设置文件路径过滤，不因文档变更或前端尚未建立而跳过整个质量门。merge queue 事件独立于 pull request 与 push，必须单独订阅才能为合并组报告检查结果。[GitHub 事件说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#merge_group)

同一事件类型和 PR/分支只保留最新一次运行；较旧运行可以被取消。不同 PR、push 与合并组不会因为相同分支名共用并发组。被取消的运行不算通过，合并需要当前提交对应的成功检查。

## CI 失败的修复分工

按用户于 2026-09-06 更新的工作方式，GitHub Copilot 额度耗尽后，后续 CI 失败由协调者
派发给本地 `gpt-5.3-codex-spark` 修复，替代此前在 PR 中 `@copilot` 的安排。任务应给出
失败提交、运行/job 链接、关键错误、期望行为及需要重跑的检查，并使用独立工作目录；
协调者继续推进独立开发，避免同时修改被派发的文件。修复不得通过删除必需检查、忽略
退出码或放宽验收条件消除红灯。收到修复后仍独立核对差异、本地回归及新提交的实际 CI；
模型完成回复不等于修复已验收。合并继续由用户决定，此分工不改变本地开发中的调试责任。

## Python 必需检查

Go Task 计量切片在两个 Python job 中增加固定 tokenizer 准备步骤：仅从固定官方 revision 下载三个公开数据文件，校验长度与 SHA-256 后发布到本地目录。`KARAJAN_REQUIRE_GO_TOKENIZER=1` 使缺少制品成为失败；`HF_HUB_OFFLINE` 和 `TRANSFORMERS_OFFLINE` 在测试阶段禁止库自动下载。准备脚本本身有本地故障测试，原生请求组合在 Linux namespace 中实际执行，未向真实 provider 发送请求。[实现范围](m3-go-task-context.md) 与 [独立检查](../../examples/go-task-context/README.md) 单列。

`examples/go-task-context` 的独立审查用例也是 Python 必需步骤；原有门、凭据隔离与 Windows/Linux 矩阵保持原要求。

`examples/go-task-startup` 增加 41 项独立启动前复查用例，使用真实临时存储，覆盖固定原 Profile、授权来源变化、历史激活与最新容量边界。测试不启动模型，也不能替代受信 runner 的真实进程启动验收。[范围与锁顺序](m3-task-startup-guards.md) 单列，新增步骤仍由原 `quality-gate` 汇总。

`python-quality` 在 `ubuntu-24.04` 和 `windows-2022` 上分别运行，均使用 Python 3.12。矩阵关闭 fail-fast，让一个系统失败时另一个仍能完成诊断；两者都必须成功。批准 Task 执行切片将每个矩阵任务的运行上限调整为 40 分钟：前置 [PR #91 的 Windows 运行](https://github.com/zhouy1017/Karajan/actions/runs/34036056124/job/101494213978) 已用 28 分 56 秒，本片新增既有库、生命周期、Collector 和三种实际子进程场景。扩大运行时间不改变断言、必需检查或成功条件。

在仓库根目录依次执行：

```text
uv lock --check
uv sync --frozen --extra dev
npm ci --prefix runtimes/opencode --no-audit --no-fund
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev mypy backend/karajan
uv run --frozen --extra dev pytest tests
```

`pyproject.toml` 的 `dev` extra 必须声明 pytest、ruff、mypy，具体依赖解析由提交的 `uv.lock` 固定。锁文件缺失或过期应失败，不在 CI 自动重写。`--frozen` 只使用现有锁文件，不检查它是否匹配项目配置，所以先执行 `uv lock --check`。[uv 锁定与同步语义](https://docs.astral.sh/uv/concepts/projects/sync/)

lint 覆盖仓库 Python 文件，类型检查覆盖 `backend/karajan`，pytest 收集 `tests` 下的测试，包括 `tests/contract`。缺少测试、测试收集错误、断言失败或检查进程非零退出都不能用成功占位替代。平台相关用例可以在不支持的系统明确 skip，但这不是该平台对应能力通过的证据；两端均适用的必需契约必须实际运行。

本切片还将下列四个发布目录加入 Python 必需检查，沿用 `pythonpath=backend tests/projects tests/isolation`：

- `examples/go-profile-qualification-spec`：持久资格入口的独立公共行为验收。
- `examples/go-profile-standards`：持久化失败、重放与权限边界。
- `examples/go-suite-independent-review`：固定 suite 的来源、调用与授权归属边界。
- `examples/go-profile-cli-spec`：显式实测入口的参数、来源和敏感输出边界。

这些目录使用合成凭据、HTTP fixture、临时仓库及真实本机进程，不进行官方 Go 调用。Linux 继续要求固定 artifact 和 namespace 检查实际执行，Windows 的 Linux 专用项明确 skip。既有后端、前端、已批准路由、任务准入及隔离链路门均保留；增加超时时间不能把失败、未知或跳过的必需检查变成成功。本段描述本切片的门禁接线，不表示尚未提交的新 PR 已通过 CI。

每条外部检查命令独占一步，保留原退出码，不使用 `continue-on-error`、`|| true`、`--if-present` 或忽略失败的包装。某一步失败导致后续步骤未运行时，整个矩阵任务仍为失败，汇总门不会将这些步骤当作成功。

## 汇总检查与仓库规则

`quality-gate` 使用 `if: always()` 等待所有必需 job，并逐个要求结果严格等于 `success`。上游 `failure`、`cancelled`、`skipped`、缺失或未知结果均使汇总步骤退出非零；它不是一个无条件输出成功的收尾任务。整个 workflow 被取消时，汇总检查也可能被取消，这同样不满足成功要求。[GitHub needs 与 always 语义](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds)

当前工作台变更的必需依赖为 `python-quality` 和 `frontend-quality`。添加新检查 job 时，必须同时更新 `quality-gate.needs` 和汇总脚本的 `required` 集合；不能只运行新检查而让它与合并门无关。汇总 job 不检出或执行项目代码，最多运行 5 分钟。前端加入前的历史提交仍只有 Python 依赖，不能把新门禁配置归属于旧 CI 记录。

仓库 ruleset/branch protection 应将精确名称 `quality-gate` 设为必需状态检查，并要求当前提交或 merge group 的检查通过。提交 workflow 文件本身不会修改 GitHub 仓库规则；规则是否已启用应以实际 GitHub 配置与检查结果为准，不从本文件推断。维护时保持这个名称稳定。[GitHub 必需状态检查](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging)

## 前端必需检查

工作台首次引入时，同批替换了原先“存在前端却未建立门禁则失败”的覆盖检查。`frontend-quality` 在 Ubuntu 上使用固定 Node.js `24.18.1` 及该发行包随附 npm，按提交的 `frontend/package-lock.json` 安装依赖。

在 `frontend` 目录依次执行 `npm ci --no-audit --no-fund`、`npm run typecheck`、`npm run format:check`、`npm test` 和 `npm run build`。交互测试覆盖真实 React 组件的登录、幂等重试、配置读取/预览/版本化应用及退出；网络依赖在此层可控模拟，真实 FastAPI 和浏览器行为另行记录。没有缺脚本也返回成功的选项。

前端 job 已成为当前 workflow 的必需依赖。不使用 `hashFiles(...)` 条件跳过、成功占位 job，或检测失败后返回成功。具体远端检查结果须引用当前提交运行，不能由本地构建成功推断。

Python 两个系统的 job 同时安装固定 `opencode-ai@1.18.29`，以实际二进制执行本地模拟 provider 探针；缺失或版本不符不能将该检查静默跳过。固定 Go 隔离链路另外要求 Linux 原生 ELF、namespace 和 UDS 组合测试必须执行：设置 `KARAJAN_REQUIRE_OPENCODE_ISOLATION=1`，只在一次性的 Linux CI runner 中允许所需 user namespace。Windows 的 Linux 专用项明确跳过。CI 的本机观察不能升级为用户部署环境的 Profile 资格；[执行范围](m2-opencode-go-isolated.md) 单列。

## 凭据与真实资格验证

workflow 权限限定为 `contents: read`；checkout 不持久保存 Git 凭据；不引用模型账号、订阅登录文件、provider key 或交付凭据，不上传这些材料。使用普通 `pull_request`，不借 `pull_request_target` 在高权限上下文运行 PR 代码。依赖安装需要网络，但测试只应使用本地夹具、假 provider、临时目录及临时进程。

CI 绿色只表示这些离线契约和本地行为检查通过。它不能证明官方订阅身份、真实模型/参数接受情况、收费上界完整性、远端取消，或用户机器上的 WSL2/容器隔离已合格。Linux/Windows hosted runner 测试也不能替代目标部署的资格记录。用户已授权固定 OpenCode Go 通道的真实测试，其实测单独保存；其他通道的现金调用仍暂停，未执行资格保持 `not_run`。[资格记录与验收范围](../architecture/05-build-and-validation.md)

固定 Go 已有独立于公共 PR CI 的[受控持久资格入口](m3-go-profile-qualification.md)，并于 2026-09-06 完成一次真实公共入口验证：两场景共 5 次 HTTP 200，同命令重放相等且无新增请求，默认任务 guard 仍返回 `TASK_PERMISSION_SCOPE_NOT_QUALIFIED`。实测绑定具体源码、Profile/runtime revision、目标环境、官方认证引用及固定范围，记录见[实测证据](../../examples/go-profile-qualification/README.md)。这是固定 scope 的观察，不授予任意 Task 权限或可信 Collector 能力，也不替代本次新提交的 CI。

后续真实资格继续使用明确授权的独立流程，不在公共 PR CI 中注入账号，不通过“缺账号则 skip 并整体 passed”制造资格。没有执行就记录 `not_run`，能力不支持记录 `unsupported`，违反契约记录 `failed`；只有对应真实用例执行成功才记录 `passed`。本切片没有创建自动执行真实资格的 GitHub workflow。

## 固定版本与官方依据

以下版本及提交于 2026-09-05 从项目官方发布页核对。Actions 使用完整 commit SHA 固定，并在 workflow 注释保留发行版本；升级时同时审查发行说明、输入契约和两种系统的运行结果。[GitHub 固定 action SHA 的说明](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions)

| 组件 | 固定版本/提交 | 官方来源 |
|---|---|---|
| checkout | v7.0.1 / `3d3c42e5aac5ba805825da76410c181273ba90b1` | [发布说明](https://github.com/actions/checkout/releases/tag/v7.0.1)、[提交](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) |
| setup-python | v7.0.0 / `5fda3b95a4ea91299a34e894583c3862153e4b97` | [发布说明](https://github.com/actions/setup-python/releases/tag/v7.0.0)、[提交](https://github.com/actions/setup-python/commit/5fda3b95a4ea91299a34e894583c3862153e4b97) |
| setup-uv | v10.0.1 / `20cfd1bf945f4377ade1205e4dbc17946fc9a30d` | [发布说明](https://github.com/astral-sh/setup-uv/releases/tag/v10.0.1)、[固定版本输入定义](https://github.com/astral-sh/setup-uv/blob/20cfd1bf945f4377ade1205e4dbc17946fc9a30d/action.yml) |
| uv | 0.12.10 | [官方发布](https://github.com/astral-sh/uv/releases/tag/0.12.10)、[官方 CI 集成说明](https://docs.astral.sh/uv/guides/integration/github/) |
| setup-node | v7.0.0 / `820762786026740c76f36085b0efc47a31fe5020` | [官方发布](https://github.com/actions/setup-node/releases/tag/v7.0.0) |
| Node.js | 24.18.1 | [官方发布](https://github.com/nodejs/node/releases/tag/v24.18.1) |

Python 固定 3.12 系列，由 setup-python 选择可用补丁版本；它不是 Python 二进制逐字节固定的承诺。uv 本体显式固定版本；当前关闭跨运行 uv 缓存。依赖升级修改项目声明与锁文件后，重新运行相同检查。
