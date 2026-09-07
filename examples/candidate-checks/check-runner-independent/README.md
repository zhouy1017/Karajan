# #94 FixedCheckRunner 独立审查

审查者 capacity_facts；执行器作者 qualification_integration。本审查不包含审查者自己实现的 Host manifest 端口，也不以接口对齐代替独立运行。基于 `docs/planning/candidate-checks-issue.md` 阅读 `check_runner.py`、`_check_environment.py`、`_check_namespace.py` 及实际调用的 capability drop helper。

## 结果与范围

最终独立 5 项全部通过（Linux，14.86 秒），没有确认的产品 finding。它们使用真实临时 Git/CAS、受控 Python 3.12 镜像、user/mount/pid/net namespace、实际进程和日志；Candidate writer/审批数据及 `start_guard` 明确为合成 fixture。没有 provider、真实凭据或 Profile 资格。

实际验证：通过 proc root/FD 仍不可读取宿主私有 canary；创建独立 session 的孤儿随 namespace init 退出而停止；在 claim 后、真实启动 guard 内变更镜像会拒绝且重放不再启动；缺失或硬链接日志不能恢复为完整通过；第二控制器在真实 Candidate Python 已 exec 后取消，退出非零、停止已确认、Candidate Evidence 为 failed，重放不复权。

`linux-final.xml` 是独立 5 项；`linux-original-regression.xml` 是审查者独立执行的原作者公开测试回归。最终数量和源码摘要见 `review.json`。原测试覆盖真实隔离、完整复制、成功/非零/超时/超限、取消、start/结果丢回执、无第二 claim，以及真正 Popen 前的截止复查。本审查没有扩展成完整 facade/Host/bootstrap/两检查业务集成验收；该范围由本票其他组合测试负责。

## 保留的失败与来源变化

首轮 `linux-first.xml` 为 4 passed / 1 failed。`test-initial.py.txt` 的取消断言要求 `outcome != completed`；实际持久结果见 `external-cancel-observation.json`：`completed / exit_code=-9 / local_stop=confirmed`。非零退出不能产生 passed，因而该失败是审查测试期待过严，不是产品 finding。最终测试改为真实执行后取消、验证非零退出和实际 Evidence failed；原失败字节未覆盖。

作者随后独立发现持久 spawn-intent 写入耗时可能越过启动截止，保留了自己的红灯并修复：在真实 Popen 紧前再核 monotonic 与原 wall 截止。首源 `486f35b6...` 的证据不升级到新源 `6dbb0337...`；本审查最终五项和原测试回归均在新来源重跑。作者的截止失败属于作者发现，不计为本审查发现。

## 命令

工作树设置 `PYTHONPATH=backend:tests/isolation`，使用 WSL Python 3.12：

```text
python -m pytest -c pyproject.toml -p no:cacheprovider .cache/check-runner-independent/test_runner_boundaries.py --basetemp=/tmp/karajan-check-independent-20260906-b -q --junitxml=.cache/check-runner-independent/linux-final.xml
python -m pytest -c pyproject.toml -p no:cacheprovider tests/isolation/test_check_runner.py --basetemp=/tmp/karajan-check-independent-original-20260906 -q --junitxml=.cache/check-runner-independent/linux-original-regression.xml
```

Windows 不具备此 namespace 实现，未把 skip 作为隔离成功。只读审查没有修改产品或作者测试，没有 Git/CI 操作。独立测试路径和 JUnit/来源记录公开可复制，临时镜像、SQLite、候选工作目录不属于发布材料。
