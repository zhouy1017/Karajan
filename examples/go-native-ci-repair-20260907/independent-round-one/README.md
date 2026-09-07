# Native CI repair 独立审查：当前候选未通过

审查者 capacity_facts，不是这些 CI repair 文件的作者。基线 `825248a29c4dcdb4f432157fdf0979f26ed9c9b9`；按 `.cache/spark-native-path.md` 与 `.cache/luna-native-task.md` 的原范围，分别核对 Standards/Spec。原始源码字节、首轮输入与 XML 均保留。没有修改产品/正式 tests、Git、workflow 或远端；没有真实 provider 调用或真实 key 读取。

## Spec：3 个 P2，均已实证

**NATIVE-001：未知 native 停止时删除 socket 根。** `go_task.execute_go_task` 的 finally 在 native local_stop=unknown、relay closed 后仍无条件调用 `_cleanup_relay_socket_root`。独立 public execute 测试返回 unknown/零请求/无capture，原 dev/inode/uid/mode 记录存在，却发现原 `/tmp/karajan-go-relay-*` 空根已被删除。当前 inode/owner/mode及拒绝非空内容检查正确，但不替代停止前提。建议仅 native confirmed/not_started 且 relay closed 时清理；未知必须保留租约并给稳定诊断，不能借删除清除未知状态。原例：`test_unknown_native_stop_retains_original_empty_socket_lease`。

**NATIVE-002：任意大写异常文本进入报告。** `_safe_failure_code` 的正则验证字符形状，不是稳定错误代码 allowlist。独立 public execute 的受控 start callback 抛出 `RuntimeError(task.prompt)`，合成私有文本 `PRIVATE_CUSTOMER_CASE_ACME_INTERNAL_2026` 原样出现在 GoTaskResult.report.error_reason_code。实际 secret/capability 的最后精确扫描仍存在，本报告不声称已证明真实 key 泄漏。建议仅枚举已知领域错误代码，未知错误保留安全固定分类和白名单异常类型；不要把匹配大写格式当作内容无泄露证明。原例：`test_arbitrary_uppercase_private_text_is_not_a_diagnostic_code`。

**NATIVE-003：公共 consume 仍丢失内层失败诊断。** 给实际 `consume_go_task` 的 native port 一个明确 failed GoTaskResult（capture=None，内层错误码 UNIX_RELAY_PATH_TOO_LONG）；真实 Collector 抛 TASK_STOPPED_CAPTURE_REQUIRED，原 effect claim 已提交，但 `ApprovedGoTaskExecution.get` 从原 SQLite operation 读回时完全不存在内层错误码。`_observe_failure` 仅记录 failure=runner，固定 child 只输出 TASK_RUNNER_FAILED，新增 producer report 因此无法解决原 CI 的诊断缺口。建议在 Collector 调用之前/失败兜底由可信 controller 持久受限诊断 DTO，绑定原 intent/Attempt/fence/grant/source；历史回读可见，不放宽capture门，不把公开接口变成任意报告上传。原例：`test_public_consume_preserves_native_failure_when_collector_rejects_capture`。

前两例是实际 producer + Journal + UDS，native 启动/停止为明确故障 double。第三例使用真实 Project/Run/Capacity/operation/Journal/Git/CAS/Host noop child；直属 child 当前身份及 producer 返回为明确 double，其余 consume→Collector拒绝→失败观察→公共get链真实。它们是 C 证据，不是当前候选的 native P 通过。

## Standards：当前必需静态门失败

实际 `ruff check` 四个受影响产品/正式test文件返回8项问题：go_task.py和test_go_task.py导入未排序、长行。实际 `mypy backend/karajan`（Windows默认目标）检查128源，`go_task.py:88 os.geteuid` 一项错误。原 Path注解已被作者改成_RelaySocketRoot，不把旧注解算作当前finding。compileall/diffcheck不替代这两个 required gate。最终原输出/退出码见 `ruff.txt`、`mypy.txt` 和 `review.json`。

## 结果与限制

- `first.xml`：3 skipped，独立命令漏设置固定 runtime 路径，测试未执行，不算通过。未修改原 XML。
- `semantic-before.xml`：前2项产品问题实际失败；第3项首次是审查 fixture未建立批准路径的Git基线，产品正确拒绝Workspace，不能计作产品红。该输入在 `test-first-baseline-missing.py.txt` 保存。
- `consumer-before.xml`：补齐临时真实Git基线后第3项在预期公共诊断断言实际失败，31.15s。没有更改产品或通过假授权跳过 source/current guards。
- 当前relay源码以 `os.fsencode` 校验107字节，新增作者107/108例存在；本轮独立没有重跑该两例或原3个实际native用例，不借作者152.34s报告声明最终P通过。根可在修复最终source后按影响补验。
- 本次不新增“对可信同账号恶意并发替换的完整OS保证”；当前确定问题是显式unknownstop条件缺失、错误内容与持久诊断，而非无限扩展威胁模型。

## 重现

在本 worktree 执行 WSL Python `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`，显式 `PYTHONPATH=backend`、`KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode`、`KARAJAN_REQUIRE_OPENCODE_ISOLATION=1`、`KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-task-execution/.cache/go-context-artifacts`、`KARAJAN_REQUIRE_GO_TOKENIZER=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。

```text
python -m pytest -c pyproject.toml -p no:cacheprovider -o "pythonpath=backend tests/projects tests/runs tests/web tests/isolation tests/adapters/opencode tests/candidates tests/execution" .cache/native-resume-independent/test_native_boundaries.py -q
```

测试新增结果必须写新 XML，勿覆盖本次原失败。此处只有独立修复输入；执行修复仍由 root 指定的 Luna/Spark 作者承担。
