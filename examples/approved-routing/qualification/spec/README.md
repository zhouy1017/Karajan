# Profile 资格切片的独立 Spec 验收

对 `source-final.json` 所列三份冻结文件完成只读审查，并从本目录的独立合成 Git 仓库、真实项目数据库和公共服务执行 **11 项检查，全部通过**。没有 Spec 发现。

`test_public_qualification.py` 未导入作者测试 helper。基础配置复用明确的 offline configuration 样例，随后通过公开 preview/apply 命令登记；它不代表真实账户配置。

- 真实执行固定 write/check/review 三个子进程。记录 wrapper 将调用原样交给实际 subprocess.run，只捕获实际返回；核对隔离参数、独立 cwd、文件内容、结构化检查/审查输出、stdout/stderr 摘要和幂等重放不再执行。
- 当前模型、原生设置、账号 provider、owner 声明改变后，旧观察均不能通过最新登记继续使用。
- 撤销后重建服务仍拒绝，原观察保持不变；非 owner 无权撤销。
- 两个独立服务连接共享真实 SQLite；资格 guard 持有时，并发撤销确实阻塞，释放后才完成且后续读取拒绝。
- runtime_tools 请求、未知 runtime 与看似 imported passed 的 owner 声明都不能升级真实资格。
- 单个故障注入在 start 持久化后模拟控制器崩溃；观察缺结果时不重跑、不回退较早通过记录。
- 时钟回退和精确到期拒绝返回事实。

复跑时从本 worktree 根执行，使用 Python dev 依赖，并为每次运行选择新目录：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
python -m pytest examples/approved-routing/qualification/spec/test_public_qualification.py --basetemp .cache/qualification-spec/replay-new -q
```

测试仅在 `--basetemp` 下创建独立合成 Git 仓库及资格运行目录；不改变项目 worktree 的 Git 元数据，不读 key，不运行模型。`final.junit.xml` 为实际本轮结果；`review.json` 记录冻结源码和证据摘要。Ruff check/format 均通过。

此结论覆盖固定本地 fixture 资格生产器及持久读取边界。它不证明语言模型上下文、模型任务能力、OS 工具沙箱、现金收口或真实 provider 资格，也不将项目库 guard 描述成跨库原子准入。

正式发布测试以向上查找 `pyproject.toml` 定位仓库根。history/ 保留开发路径的历史记录；本目录的 source-final.json、review.json 与 final.junit.xml 对应发布路径实际复跑。发布目录不包含运行 state，临时 Git/SQLite 仅存在 --basetemp 指定缓存中。
