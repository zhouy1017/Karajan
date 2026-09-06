# Go Task 上下文与 ExecutionPolicy v2 证据

本目录保存离线计量、真实本地 HTTP/SQLite 和 WSL 原生工具组合的选定记录。没有真实账户调用，没有发布 tokenizer 大文件、SQLite 数据库或凭据；固定数据由 CI 准备脚本单独校验下载。

- `accounting`：完整官方模板计量的来源、纯数值示例及部分原始红绿测试。`source.json` 绑定该计量模块实际字节与固定依赖；它不授予 Profile 资格。
- `relay`：Task 缺配置、provider 用量异常及持久恢复的原红灯和修复后结果。
- `relay-review`：独立作者实际复现的账本丢返回与后续 usage 覆盖缺陷，以及原测试不弱化的修复验证；附加边界没有伪称旧实现红灯。
- `native`：固定 Linux OpenCode、namespace、投影文件、UDS relay 与实际 tokenizer；上游响应为本地 fixture。两项检查覆盖三次读/改/历史请求及超限停止。
- `policy-review`：v2 的独立公共登记、版本身份与 Run 检查引用验收。
- `provision`：固定公开数据准备脚本的本地原子发布与故障检查。

独立用例从仓库根运行：

```text
python -m pytest -o "pythonpath=backend tests/adapters/opencode tests/projects tests/runs tests/web" examples/go-task-context
```

需先运行 `.github/scripts/provision_go_tokenizer.py --directory .cache/go-context-artifacts`，再设置 `KARAJAN_REQUIRE_GO_TOKENIZER=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。新 Task 计量公共测试位于 `tests/adapters/opencode`，原生组合在 `tests/isolation/test_opencode_task_context.py`，脚本测试在 `tests/tools/test_go_tokenizer_provision.py`。CI 已接线；新提交的远端成功状态必须另查其实际运行。

当前证据不证明服务端精确 token 数、不扩大官方固定 scope，也不代表批准 Run 已能完成 Worker → Collector → Reviewer → PR。边界及后续执行工作见 [实现说明](../../docs/implementation/m3-go-task-context.md)。
