# M1 本地项目工作台

对应 [M1-01 / #11](https://github.com/zhouy1017/Karajan/issues/11) 的本机认证与项目配置操作切片。工作台已接通 FastAPI 和实际 SQLite 项目登记；Run、模型执行、SSE、交付与完整来源配置界面仍在后续实施中。配置检查通过不启用真实模型。

## 已实现的用户流程

1. 启动本机服务，从生成的本地文件取得一次性访问码；十分钟内使用一次，交换为十二小时会话。
2. 登记允许目录内的已有 Git 仓库、起始版本与 PR 目标分支。后端检查真实仓库，登记不运行模型、不修改仓库。
3. 打开当前已保存配置；编辑后先预览，再应用指定预览。应用绑定项目版本，修改内容会撤下旧预览的保存操作；并发修改返回冲突。
4. 保存格式正确但尚未绑定资源的草稿；字段格式错误或含不支持凭据载荷时不能保存。重新打开读取的是已应用配置。
5. 退出登录在服务端撤销会话，刷新后仍要求登录。

当前配置编辑为结构化 JSON 草稿入口。凭据仅使用引用；账号、来源与 Rulebook 的完整表单随 M2/M3 实施。本切片没有提供任意文件、命令、密钥查看或模型调用接口。

## 本机边界

CLI 仅监听 `127.0.0.1`。专用状态目录不能是 symlink/junction，已有目录必须含本工具标记；新目录在 Windows 关闭继承并仅授予当前 SID 完全控制，POSIX 使用私有权限。访问码文件不写日志，仅输出路径；会话库保存摘要。不要把状态目录放在被登记仓库内部。

所有 `/v1` 私有路径检查持久会话，写命令额外检查精确 Host/Origin 与当前 CSRF；bootstrap 仅豁免会话和 CSRF。Cookie 使用 HttpOnly、SameSite=strict，本机 HTTP 不宣称使用 Secure。重复失败登录在 SQLite 保存短窗计数，重启不清零。响应禁止缓存，限制外部脚本、frame、base URI 与表单来源；验证错误只返回固定原因，不回显请求。

写请求在 JSON 解析前限制为 64 KiB 和十秒读取时间。字符串及键必须能以 UTF-8 表示，拒绝非标准 NaN/Infinity，避免数据库编码或响应异常。项目命令使用持久幂等键，修改还需精确 If-Match；丢失响应后同一 UI 操作保留原幂等键。

API/SSE/产物的统一认证前缀已有保护测试，但 SSE 和产物业务端点尚未实现；返回 401 的测试只证明认证中间层，不能证明完整事件或下载能力。

## 运行

```text
uv sync --frozen --extra dev
npm ci --prefix frontend --no-audit --no-fund
npm run build --prefix frontend
uv run python -m karajan.web serve --state-directory <专用状态目录> --project-root <允许的仓库父目录> --frontend-directory frontend/dist --port 8765
```

先构建再由同一 FastAPI 服务提供页面和 API。Vite 开发服务器不自动绕开 Origin 校验；真实验证使用编译后的同源页面。所有依赖由 Python/npm 锁文件固定。

## 实际验证（2026-09-05）

- Web：17 项测试通过，包括实际临时 Git/SQLite 的登记、预览/应用、重复命令、过期版本、已保存配置读取，以及真实子进程 CLI 启动和 HTTP 登录。Ruff、格式和严格类型检查通过。
- 前端：4 项组件交互测试通过，类型/格式检查和生产构建通过。原先重新打开配置会重置为 `{}`，新增公开界面用例先失败，读取已保存内容后通过；退出操作也先观察缺失再实现。
- 独立审查发现 bootstrap 和 preview ID 的异常 Unicode 可能导致 500，已通过公开 HTTP 红绿修复。配置中 `native_settings.env.OPENAI_API_KEY` 的模拟凭据曾可保存，领域模块已拒绝，独立 HTTP 复验确认不可应用、不可导出、SQLite 无该 canary。
- 后续审查发现含 NUL 的 base ref 会进入系统进程参数并返回 500；公开 HTTP 用例先复现异常，再由项目标识符契约拒绝控制字符，Git 边界也归类非法参数。拒绝后项目库保持空，响应不缓存。
- 真实 Codex 内置浏览器在 `http://127.0.0.1:8765/` 操作：登录、登记临时已有仓库 `sample`，保存未绑定模型的草稿，刷新后重新打开保持内容；模拟凭据预览没有保存按钮；退出并刷新后回到登录页。测试仓库在 `.cache/workbench-repositories/sample`，状态在 `.cache/workbench-control`，仅为本次演示。
- 测试使用的 Starlette 版本给出两项上游弃用提示（HTTPX TestClient 与 BlockingPortal 别名）；没有将提示隐藏，当前行为测试通过。

以上仅为离线产品行为与本机工作台证据，没有真实模型调用、订阅资格或现金支出。远端 CI 与独立审查结论以当前 PR/提交的实际记录为准。
