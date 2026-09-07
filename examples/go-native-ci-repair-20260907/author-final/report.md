# Luna native TEST-ONLY 收尾

日期：2026-09-07
工作树：`C:/Users/Chooo/Playground/Karajan/.cache/ci-spark-task-native`

## 变更

- `tests/runs/test_go_task_execution.py`：首次 `consume_go_task` 仍精确断言 `TASK_STOPPED_CAPTURE_REQUIRED`；第二次调用直接读取 `facade.reconcile` 恢复结果，保留诊断、原 intent/effect claim、grant revoked/0 请求、producer 次数 1 及回放不写入语义。
- 新增 `tests/runs/test_go_native_diagnostic_contract.py`：真实 SQLite/原 intent fixture 覆盖替换 runner、首次正确写入、幂等同值、冲突拒绝，以及 `native_stop`/`relay_status` 的任意字符串、`True`、`None`、`list`、`str` 子类在写入前拒绝且 operation 完全不变。

## 改前红证据

独立第三轮已核对源码相同并保存于 `.cache/native-resume-independent/round-three/README.md` 与 `final-linux.xml`：正式旧测试首次 consume 的完整诊断/claim/grant 断言通过，第二次错误使用 `pytest.raises(TASK_STOPPED_CAPTURE_REQUIRED)`，实际 DID NOT RAISE；该红测对应 `consume_go_task` 已有 effect claim 时只读 `facade.reconcile` 的现行实现。未修改产品。

## 最终验证

Pytest 入口均使用：

```text
pytest -o "pythonpath=backend tests/projects tests/runs tests/web tests/isolation tests/adapters/opencode tests/candidates tests/execution"
```

Windows（Python `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe`）：

```text
tests/runs/test_go_task_execution.py::test_consume_preserves_native_failure_when_collector_rejects_missing_capture
tests/runs/test_go_native_diagnostic_contract.py
tests/runs/test_go_execution_intent.py
```

结果：exit 0，34 passed，1 skipped（本机未 provision pinned official tokenizer artifacts）。
证据：`.cache/luna-native-final-windows.xml`、`.cache/luna-native-final-windows.stdout.txt`。

Linux WSL（Python `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`，固定 ELF/tokenizer/offline 环境）：同一测试选择，结果 exit 0，35 passed，0 skipped；公共 consume 实际通过。证据：`.cache/luna-native-final-linux.xml`、`.cache/luna-native-final-linux.stdout.txt`。

`C:/Users/Chooo/Playground/Karajan/.venv/Scripts/ruff.exe check .`：exit 0，All checks passed。

`C:/Users/Chooo/Playground/Karajan/.venv/Scripts/mypy.exe backend/karajan`：exit 0，Success: no issues found in 128 source files。

仅本次两个测试文件执行 `ruff format --check`：exit 0，2 files already formatted；未执行全仓格式化。

## 产品冻结 hash（前后相同）

```text
088C335D1BFEEDC2B69BC2395ACC704EA6B32F71B6ADDCC74C06D87B2400170B  backend/karajan/adapters/opencode/go_relay.py
8BD911B79030DCDCD7D6C52941B592D4671235EFCF648620E0905868441D4EF8  backend/karajan/isolation/go_task.py
4E72794F6AAE846D31788D136DBE47BF15AA6FDE839731C57D14F5AA86C5A8E9  backend/karajan/orchestration/go_execution_intent.py
789E9945A698D6F519F65DB35205460AD35A0BABF35AEE2FBB5C6F82EC095B4D  backend/karajan/orchestration/go_task_execution.py
```

未运行真实 provider/key；未改 backend、runtime/协议、CI、依赖、Git 或远端。

## 最终 consume 重放边界修正

按维护回归要求，第二次调用保持走 `consume_go_task` 自身的已有 effect-claim no-new-effect 分支（内部返回 `facade.reconcile`），未跳过公共入口。仅此一行变更后，Linux WSL 单测结果为 exit 0，1 passed，0 skipped。

证据：`.cache/luna-native-final-linux-consume.xml`、`.cache/luna-native-final-linux-consume.stdout.txt`。

最终 SHA256：`64CA3965F03EA339041F7E2BA559AF3B0E6355FCEB92152CE4CCCA70250B4F85` (`tests/runs/test_go_task_execution.py`)。四个 backend hash 仍与上方冻结值一致。
