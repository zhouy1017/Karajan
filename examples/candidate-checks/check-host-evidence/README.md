# #94 Check Host 本机生命周期证据

作者：capacity_facts。范围是公开 `CheckAttemptManifest` / `HostManifest` 解析以及 `RunnerHost` 的准备、一次 control 初始化、激活、真实 supervisor/直属 child、读回、原启动重放、当前 fence 和精确历史取消。`contracts/probe.py` 未改，原模型 manifest JSON 和行为保持。Check 用环境 id/revision/source 与 execution digest，不登记虚构 Profile，也不授予 Check 结果或 Review 通过。

## 已执行

- 首个公开 API 缺失：`first-red.xml`，原输入 `test-first.py.txt`；随后 `first-green.xml` 1 passed。
- 真实子进程路径的原模型解析拒绝 Check：`start-red.xml`，原输入 `test-start-red.py.txt` 与 `host-start-red.py.txt`；随后 `start-green.xml` 2 passed。
- 严格 object parser 收尾：`parser-red.xml` 21 passed / 3 failed。其中一个是实际可变 Pydantic 实例绕过再验证的问题，已改为重新验证其字段；另两个是作者测试误用不存在的 `accept_result`，已更正为既有公开 `receive_result`，不将这两项称为产品缺陷。原测试 `test-parser-red.py.txt` 保留。
- 新 Check 公共行为最终：`check-windows.xml`，24 passed，4.13 秒。
- 全 execution 兼容组：Windows `execution-windows.xml`，121 passed，26.44 秒；WSL Linux `execution-linux.xml`，121 passed，39.70 秒。Linux 有一个 pytest cache 写入权限警告，未影响测试或 JUnit 保存。
- 4 个产品源和新测试 Ruff check / format check 通过；`mypy backend/karajan/execution` 与 `mypy --platform linux backend/karajan/execution` 均 6 sources 通过（mypy 本身在 Windows 执行）。

## 可复现命令

在本工作树设置 `PYTHONPATH=backend`，使用仓库 Python 环境运行：

```text
python -m pytest tests/execution/test_check_manifest.py -q
python -m pytest tests/execution -q
python -m mypy backend/karajan/execution
python -m mypy --platform linux backend/karajan/execution
```

Linux 使用已有 WSL Python 3.12 环境，显式 `PYTHONPATH=backend`，同一 `tests/execution` 命令。测试运行真实本机 Python 进程和 SQLite，不使用 provider、模型、账户凭据、容器网络或 Check 环境镜像。

证据层级为 C/P：严格协议、持久身份和真实本机进程。未覆盖本票其他作者的完整 Candidate 检查 facade / 固定环境执行器集成，不代表 #94 整票或真实模型资格完成。最终源码与证据文件摘要见 `freeze.json`；归档红测试以 `.py.txt` 保存，不参加常规 pytest 收集。
