# 独立 Spec 复核证据

审查基点为 `b8aafa6b29a5dc9005e4264f27c3aff5f5d39724`。`spec-source.before.json` 保存未提交初稿的源码指纹；本目录 `spec-*.before.json` 是原始 `.cache/resource-review` 报告的原样副本。修复后只新增 after，未覆盖原始记录。

本组复验只使用真实本地 SQLite、进程内认证 HTTP 客户端，以及公开 App 交互配合延迟 HTTP fixture；模型/现金调用均为零。它证明对应资源工作台边界，不证明真实服务配额可见性或派发资格。

| 原反例 | 修复前 | 独立复验结果 |
|---|---|---|
| 新鲜 fixture 报告 weekly limit=5，设置保护量 999 | HTTP 200，revision=2 | HTTP 422 / PROTECTION_EXCEEDS_POOL_LIMIT，仍 revision=1 |
| 已有资源池、无 quota observation，已记录 QUOTA_EXHAUSTED | 冷却结束后 blockers 为空 | 保留 EXHAUSTION_REQUIRES_NEW_OBSERVATION |
| 旧资源请求等待时退出并重新登录，再返回旧 401 | 新登录页面又被退出 | 保留新会话项目页，登录表单不出现 |

`spec-limit.input.json` 和 `spec-exhaustion.input.json` 直接提取自原 before 报告的 input；before/after 的逻辑输入相同。limit 的 HTTP 身份传输随 dot-segment 修复从 path 改为 query，before/after URL 均记录在 after.transport 中，因此不声称原始 HTTP URL 字节完全相同。原始观察时间戳没有重写：当前重跑时可能已陈旧，此用例只核对**已报告池上限**约束，不用它授予实时额度或执行资格。

会话场景将原复现测试里的同一组假会话值、401 和操作顺序保存为 `spec-session.input.json`。当前测试读取此固定输入；不使用真实访问码。`spec-session.before.json` 是原公开 App 复现结果，after 与 Vitest 报告证明同一交互顺序修复后通过。新测试不写 before 文件。

从仓库根目录，使用现有开发环境重跑；输出到新的 `.cache` 文件，避免改写发布的历史证据：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
.venv/Scripts/python.exe examples/resource-workbench/review-fixes/spec_replay.py --case limit --output .cache/resource-review/limit.replay.json
.venv/Scripts/python.exe examples/resource-workbench/review-fixes/spec_replay.py --case exhaustion --output .cache/resource-review/exhaustion.replay.json
$env:KARAJAN_RESOURCE_REVIEW_OUTPUT = '.cache/resource-review/session.replay.json'
node frontend/node_modules/vitest/vitest.mjs run --config frontend/vite.config.ts --root . examples/resource-workbench/review-fixes/spec-session.test.ts
```

Python 入口默认在系统临时目录新建独立数据库，也可用 `--directory <新的显式目录>` 指定，拒绝覆盖已有目录；不依赖原随机缓存路径。UI 入口使用仓库现有前端依赖，`--root .` 必須保留以解析固定输入路径。所有 fixture 访问码仅供内存 HTTP 客户端，测试不启动外网服务。

`spec-review-index.json` 绑定最终模块、复现入口、输入和 before/after 文件摘要；提交 SHA 由发布者在提交后另行记录。dot-segment/保护编辑字段限制由另一独立审查者提供证据，本组不冒称复验了那份原始输入，也不覆盖其文件。
