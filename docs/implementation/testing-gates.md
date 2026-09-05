# 测试与合并质量门

仓库的自动检查入口是 [CI workflow](../../.github/workflows/ci.yml)，稳定汇总检查名为 `quality-gate`。它证明当前提交通过了仓库内已经实现的检查；它不代表全部 PRD 已完成，也不代表任何真实模型服务取得接入资格。

## 自动运行范围

截至 2026-09-05，[PR #31](https://github.com/zhouy1017/Karajan/pull/31) 已完成真实失败与恢复演练。提交 `3187f65` 的两套 Python 检查及汇总检查均失败，GitHub 返回 `mergeable_state=blocked`；移除临时用例后的 `e8f7142` 在 [PR 运行](https://github.com/zhouy1017/Karajan/actions/runs/33963017034) 和 [push 运行](https://github.com/zhouy1017/Karajan/actions/runs/33963015622) 均通过。

主分支的 `main-quality-gate` ruleset（ID `22331721`）已启用，要求 PR、当前基准上的 `quality-gate`（绑定 GitHub Actions，integration ID `15368`），禁止强推和删除，绕过名单为空。配置与失败历史保存于 [门禁证据](ci-gate-evidence.json)。这份记录对应上述具体提交；以后的变更仍须取得其自身的成功检查。

每个 pull request、向 `main` 或 `codex/**` 的 push，以及 merge queue 的 `merge_group: checks_requested` 都运行检查。不设置文件路径过滤，不因文档变更或前端尚未建立而跳过整个质量门。merge queue 事件独立于 pull request 与 push，必须单独订阅才能为合并组报告检查结果。[GitHub 事件说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#merge_group)

同一事件类型和 PR/分支只保留最新一次运行；较旧运行可以被取消。不同 PR、push 与合并组不会因为相同分支名共用并发组。被取消的运行不算通过，合并需要当前提交对应的成功检查。

## Python 必需检查

`python-quality` 在 `ubuntu-24.04` 和 `windows-2022` 上分别运行，均使用 Python 3.12。矩阵关闭 fail-fast，让一个系统失败时另一个仍能完成诊断；两者都必须成功。每个矩阵任务最多运行 20 分钟。

在仓库根目录依次执行：

```text
uv lock --check
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev mypy backend/karajan
uv run --frozen --extra dev pytest tests
```

`pyproject.toml` 的 `dev` extra 必须声明 pytest、ruff、mypy，具体依赖解析由提交的 `uv.lock` 固定。锁文件缺失或过期应失败，不在 CI 自动重写。`--frozen` 只使用现有锁文件，不检查它是否匹配项目配置，所以先执行 `uv lock --check`。[uv 锁定与同步语义](https://docs.astral.sh/uv/concepts/projects/sync/)

lint 覆盖仓库 Python 文件，类型检查覆盖 `backend/karajan`，pytest 收集 `tests` 下的测试，包括 `tests/contract`。缺少测试、测试收集错误、断言失败或检查进程非零退出都不能用成功占位替代。平台相关用例可以在不支持的系统明确 skip，但这不是该平台对应能力通过的证据；两端均适用的必需契约必须实际运行。

每条外部检查命令独占一步，保留原退出码，不使用 `continue-on-error`、`|| true`、`--if-present` 或忽略失败的包装。某一步失败导致后续步骤未运行时，整个矩阵任务仍为失败，汇总门不会将这些步骤当作成功。

## 汇总检查与仓库规则

`quality-gate` 使用 `if: always()` 等待所有必需 job，并逐个要求结果严格等于 `success`。上游 `failure`、`cancelled`、`skipped`、缺失或未知结果均使汇总步骤退出非零；它不是一个无条件输出成功的收尾任务。整个 workflow 被取消时，汇总检查也可能被取消，这同样不满足成功要求。[GitHub needs 与 always 语义](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds)

当前必需依赖为 `python-quality`。添加新检查 job 时，必须同时更新 `quality-gate.needs` 和汇总脚本的 `required` 集合；不能只运行新检查而让它与合并门无关。汇总 job 不检出或执行项目代码，最多运行 5 分钟。

仓库 ruleset/branch protection 应将精确名称 `quality-gate` 设为必需状态检查，并要求当前提交或 merge group 的检查通过。提交 workflow 文件本身不会修改 GitHub 仓库规则；规则是否已启用应以实际 GitHub 配置与检查结果为准，不从本文件推断。维护时保持这个名称稳定。[GitHub 必需状态检查](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-status-checks-before-merging)

## 前端检查的加入条件

当前 Python CI 包含一个明确的覆盖检查：若 `frontend/package.json` 已出现，但尚未配置前端质量门，CI 直接失败。它防止前端已经实现却继续只检查 Python；它不声称前端不存在时运行过前端测试。

首次加入前端时，必须在同一变更中替换这条覆盖检查，增加真正的 `frontend-quality` job，固定 Node/package manager 版本并提交锁文件，执行对应的锁定安装、类型检查、非 watch 测试和生产构建。例如选择 npm 时使用 `npm ci`，并要求 `typecheck`、`test`、`build` 脚本存在；不使用缺脚本也成功的选项。选择 pnpm 等工具时保留同等冻结锁文件语义。

前端 job 必须成为 `quality-gate` 的必需依赖。前端代码已经存在后，不使用 `hashFiles(...)` 条件跳过、成功占位 job，或检测失败后返回成功。前端脚本及 runtime 的具体版本在实际引入时核验，不提前声明尚未实现的检查已通过。

## 凭据与真实资格验证

workflow 权限限定为 `contents: read`；checkout 不持久保存 Git 凭据；不引用模型账号、订阅登录文件、provider key 或交付凭据，不上传这些材料。使用普通 `pull_request`，不借 `pull_request_target` 在高权限上下文运行 PR 代码。依赖安装需要网络，但测试只应使用本地夹具、假 provider、临时目录及临时进程。

CI 绿色只表示这些离线契约和本地行为检查通过。它不能证明官方订阅身份、真实模型/参数接受情况、收费上界完整性、真实工具沙箱、远端取消，或用户机器上的 WSL2/容器隔离已合格。Linux/Windows hosted runner 测试也不能替代目标部署的资格记录。当前实施阶段按用户要求不进行现金 API 真实调用，其未执行资格保持 `not_run`；离线通过不解除该限制。[资格记录与验收范围](../architecture/05-build-and-validation.md)

真实资格以后可以采用独立的手动流程：绑定具体提交、Profile/runtime revision、目标环境、官方认证引用、测试范围和预算；只在已具备相应授权时运行。它不在公共 PR CI 中注入账号，不通过“缺账号则 skip 并整体 passed”制造资格。没有执行就记录 `not_run`，能力不支持记录 `unsupported`，违反契约记录 `failed`；只有对应真实用例执行成功才记录 `passed`。本次仅规定接口边界，没有创建或运行真实资格 workflow。

## 固定版本与官方依据

以下版本及提交于 2026-09-05 从项目官方发布页核对。Actions 使用完整 commit SHA 固定，并在 workflow 注释保留发行版本；升级时同时审查发行说明、输入契约和两种系统的运行结果。[GitHub 固定 action SHA 的说明](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions)

| 组件 | 固定版本/提交 | 官方来源 |
|---|---|---|
| checkout | v7.0.1 / `3d3c42e5aac5ba805825da76410c181273ba90b1` | [发布说明](https://github.com/actions/checkout/releases/tag/v7.0.1)、[提交](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) |
| setup-python | v7.0.0 / `5fda3b95a4ea91299a34e894583c3862153e4b97` | [发布说明](https://github.com/actions/setup-python/releases/tag/v7.0.0)、[提交](https://github.com/actions/setup-python/commit/5fda3b95a4ea91299a34e894583c3862153e4b97) |
| setup-uv | v10.0.1 / `20cfd1bf945f4377ade1205e4dbc17946fc9a30d` | [发布说明](https://github.com/astral-sh/setup-uv/releases/tag/v10.0.1)、[固定版本输入定义](https://github.com/astral-sh/setup-uv/blob/20cfd1bf945f4377ade1205e4dbc17946fc9a30d/action.yml) |
| uv | 0.12.10 | [官方发布](https://github.com/astral-sh/uv/releases/tag/0.12.10)、[官方 CI 集成说明](https://docs.astral.sh/uv/guides/integration/github/) |

Python 固定 3.12 系列，由 setup-python 选择可用补丁版本；它不是 Python 二进制逐字节固定的承诺。uv 本体显式固定版本；当前关闭跨运行 uv 缓存。依赖升级修改项目声明与锁文件后，重新运行相同检查。
