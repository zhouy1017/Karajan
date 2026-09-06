# v2 审批工作台独立 Spec 验收

最终冻结源码下，发布路径的 **12 项 HTTP 检查和 14 项 UI 交互检查全部通过**，未留下 Spec 发现。`final.source.json` 记录 10 个相关产品文件的 SHA-256；其中 `RoutingAuthorization.tsx` 为 `2136f41abb5ee40df4549bf1913cba4a70551d55a78ad4756f91666a6942ce49`，包含 Standards 审查提出的金额字符串修复。该 finding 归属 Standards，本报告没有将它计作 Spec 发现。

## 证据范围

- `test_http.py` 使用公开 ProjectRegistry / RunPlanner 命令、FastAPI TestClient 的真实认证 HTTP 路由及持久化 SQLite。它实际检查服务端会话、CSRF、四项摘要、旧计划、旧 Commander 任期、幂等重放、Run 固定配置和默认 v1 创建行为。TestClient 在进程内调用 ASGI，未启动监听端口。
- `inputs/valid-view.json` 是最终 HTTP 复跑中读取的合成项目、v2 Run 和 v1 Run。Profile 证据与规划准入 receipt 均为 fixture，USD/CNY 限额为 0，没有真实资格或派发授权。
- `workbench.test.ts` 用该完整读取结果挂载真实 React 组件，通过 jsdom、Testing Library 和 fetch 替身检查显示范围与交互。14 项覆盖完整权限展示和精确 v2 请求、六类缺失或矛盾材料、Commander 变更、409/422 重新审阅、冲突后的读取失败、重开 Run、会话切换及 v1 协议兼容。这不是浏览器或网络端到端测试。
- 本目录未读取真实密钥、调用 provider、启用派发或保存浏览器 session / bootstrap / SQLite state。真实浏览器验证由上层证据另行记录，不纳入本报告 26 项计数。
- `pre-publication.*` 是旧缓存路径的历史结果和修复前源码绑定，原样保留。最终结论使用 `final.*`；历史通过数不重复相加。

## 复跑

先在仓库根安装锁定的 Python dev 与前端依赖。HTTP 输入要求一个已存在、拥有 `main` 分支的独立 Git 样例仓库；测试只读取其身份，不修改 Git。以下 PowerShell 示例中的 `$specRepository` 必须替换为该样例仓库的实际绝对路径。

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
$specRepository = 'C:\path\to\independent-fixture-repository'
$env:KARAJAN_SPEC_REPOSITORY = $specRepository
$specScratch = '.cache/v2-ui-spec/replay-' + [guid]::NewGuid().ToString('N')
python -m pytest examples/v2-approval-workbench/spec/test_http.py --basetemp $specScratch --junitxml ($specScratch + '.http.junit.xml') -q
```

`seed.py` 限制临时 state 位于 `.cache/v2-ui-spec/` 内并校验 backend 的实际导入位置。每次使用新的 scratch 路径，避免覆盖旧 evidence 或浏览器 state。HTTP 检查还会将完整读取结果写入 `.cache/v2-ui-spec/published-observed/valid-view.json`，它不含认证 session。

使用已发布的最终合成输入复跑 UI：

```powershell
node frontend/node_modules/vitest/vitest.mjs run --config examples/v2-approval-workbench/spec/vitest.config.mts
python -m ruff check examples/v2-approval-workbench/spec
python -m ruff format --check examples/v2-approval-workbench/spec
node frontend/node_modules/prettier/bin/prettier.cjs --check examples/v2-approval-workbench/spec/workbench.test.ts examples/v2-approval-workbench/spec/vitest.config.mts
```

已发布的最终 UI 运行使用本次 HTTP 读取结果，随后才冻结 `inputs/valid-view.json`。复跑不应改写已发布的 `final.*` 结果；需要记录新结果时，请使用新的缓存输出路径。

## 限制

这些检查证明冻结版本的审批显示、确认协议和相关拒绝路径符合本切片范围；不证明真实 provider 资格、资金约束、执行调度或完整项目交付。最终 HTTP 运行有两条依赖弃用提示（Starlette TestClient 的 httpx 与 AnyIO BlockingPortal 别名），没有失败或跳过。
