# 测试与合并质量门

仓库自动检查入口是 [CI workflow](../../.github/workflows/ci.yml)。PR 和 merge queue 的稳定必需汇总检查名为 `quality-gate`。它仅证明当前提交完成了下面的快速检查；不代表完整回归、产品 PRD 或任何真实模型服务资格。

## 触发和目标

`pull_request`、`merge_group: checks_requested` 与向 `dev`、`main` 的 push 运行同一套快速门禁。普通 `codex/**` push 不再重复 PR 检查。workflow 没有路径过滤或成功占位 job；快速门禁对所有这些变更都执行。

`schedule` 每天只运行一次：`17 18 * * *`（18:17 UTC，即 Asia/Hong_Kong 次日 02:17）。GitHub 计划任务从默认分支 `dev` 运行。它只运行 nightly，不启动快速 job 或 `quality-gate`；nightly 使用独立的 `nightly-quality-gate` 汇总结果。手动或 push/PR 不会启动全量回归。

正常可用 runner 的目标是从首个 job 开始至 `quality-gate` 完成为少于 180 秒，平台排队时间单列。workflow 的 timeout 只是故障保护，不能证明该目标已经达成；每个候选须在自己的 GitHub run 中记录队列、各 job 与关键路径时长。uv 与 npm 都按锁文件启用依赖缓存，以缩短正常重复运行，缓存未命中仍必须保持同一检查语义。

同一事件和 PR/分支只保留最新运行；被取消的运行不算通过。merge queue 是独立事件，必须单独订阅才会为 merge group 报告 `quality-gate`。

## PR / merge queue 快速门禁

`quick-python` 同时在 Ubuntu 24.04 和 Windows 2022 运行，并且两端都必须成功。它执行：

```text
uv lock --check
uv sync --frozen --extra dev
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev mypy backend/karajan
uv run --frozen --extra dev pytest \
  tests/routing/test_authorization.py \
  tests/projects/test_qualification_store.py \
  tests/runs/test_admission_guard.py \
  tests/web/test_task_admission_http.py \
  tests/tools/test_ci_quality_gate.py
```

这组回归覆盖规则授权不可扩大、持久资格/撤销的 fail-closed 读取、准入 reservation 与 HTTP 请求的权限边界。它使用 fixture、临时目录、本地 git 和 HTTP test client；不下载 tokenizer、不安装或启动 OpenCode/native runtime，也不会接触真实模型或 provider 凭据。它是代表性快速业务覆盖，不能冒充全库测试。

`frontend-quality` 在 Ubuntu 上使用固定 Node.js 24.18.1，执行 `npm ci --no-audit --no-fund`、`npm run typecheck`、`npm run format:check`、三个代表性交互文件（`App.test.tsx`、`NewRunForm.test.tsx`、`ProjectRuns.v2.test.tsx`）和 `npm run build`。因此前端类型、格式、登录/建 Run/批准交互及生产构建仍是 PR 必需项；完整前端测试只在 nightly。

## 每日全量 nightly

`python-nightly` 保留原有 Linux/Windows Python 3.12 矩阵、完整 `pytest tests`、全仓 Ruff、backend mypy、固定 tokenizer 制备和离线环境变量。它也保留原有的全部独立 examples：Go diagnostic、approved routing、task admission、isolated Go runtime、persistent Go qualification、task context、task startup、writer capture 和 projected qualification。Linux namespace 许可、固定 OpenCode 1.18.29 安装、Linux 原生 binary 与 `KARAJAN_REQUIRE_OPENCODE_ISOLATION=1` 仍只在 nightly 执行；Windows 继续保留其平台行为和 Linux 专用 skip。

`frontend-nightly` 保留完整前端类型、格式、交互和构建检查。`nightly-quality-gate` 只在 `python-nightly` 和 `frontend-nightly` 都成功时成功。这是每日回归结果，不能替代 PR 当前候选的快速门禁，也不能证明真实服务资格。

## 汇总的 fail-closed 规则

`quality-gate` 以 `if: always()` 等待 `quick-python` 与 `frontend-quality`，然后在汇总 job 内以小型内联断言严格要求两项结果均为 `success`。失败、取消、skip、缺失、未知和 workflow `needs` 集合不一致都会使汇总非零。`nightly-quality-gate` 对两项 nightly dependency 使用相同规则。`tests/tools/test_ci_quality_gate.py` 从 workflow 提取并执行两段实际内联 Python，覆盖成功、`failure`、`cancelled`、`skipped`、缺失结果和依赖列表缺失情形。

汇总 job 不检出仓库，也不执行项目代码、依赖安装、native runtime 或测试；它只读取 GitHub 注入的 `needs` 结果并作严格判断。这样上游失败时，汇总不会启动候选的任何代码。

仓库 ruleset / branch protection 应继续要求精确名称 `quality-gate`，并要求当前提交或 merge group 的检查通过。workflow 文件本身不会修改 GitHub 规则；每个候选的远端检查结果和计时须单独读取。`nightly-quality-gate` 是可追踪的回归状态，不是 PR required check。

## 安全、锁定和解释边界

workflow 权限固定为 `contents: read`；checkout 不持久保存 Git 凭据；不使用 `pull_request_target`，不引用模型账号、订阅登录文件、provider key 或交付凭据。所有 action 保留完整 SHA 固定：checkout v7.0.1、setup-python v7.0.0、setup-uv v10.0.1、setup-node v7.0.0。uv 固定为 0.12.10；Node 固定为 24.18.1。锁文件缺失或过期必须失败，CI 不会重写它。

CI 绿色只证明实际运行的离线契约和本机行为。它不能证明官方订阅身份、真实模型/参数接受情况、收费上界、远端取消，或用户机器上的 WSL2/容器隔离资格。真实资格继续由明确授权的独立流程记录；没有执行应保持 `not_run`，不得借 CI skip 或本机 fixture 标记为 passed。
