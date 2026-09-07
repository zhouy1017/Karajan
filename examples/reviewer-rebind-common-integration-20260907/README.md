# PR98 共同修复下游验证

固定实现：`f7d42455752a5fe040d6670361ec6d18de0f9051`，双父为原 PR98 `f1919a97ec4a0ef609105fcdc9a30374cb9d6c34` 与共同修复 `ef440f4e7902bc1d3a262afa9112154cd9690a4a`。本轮由 qualification_integration 执行下游验证；它不是将本 reviewer 早先写的 rebind 原实现重新包装为独立原代码审查。

只读确认：相对原 PR98，4 个产品文件和 6 个测试文件发生变化，全部与共同修复逐字相同。Candidate 的 6 个产品文件及 5 个测试文件没有变化；未引入 VALID Checks/预算模块、SUBJECT 消费者或 Reviewer 资格生产。具体路径/hash 见 `scope-and-results.json`。

| 检查 | 实际结果 |
| --- | --- |
| Windows 完整 Candidate 组 | 164 passed、3 个 POSIX 权限位专用 skip，0 failed；JUnit 167 tests / 147.124s。 |
| Linux 真正 `/tmp` 完整 Candidate 组 | 167 passed、0 skipped、0 failed；17.844s。 |
| Ruff（Candidate 产品和测试） | passed，Windows host 执行。 |
| mypy win32 / linux target | 两者 passed，各 6 source files；均用已有 Windows Python 执行。 |

两平台完整组均包含 `test_review_rebind.py` 的 42 项，以及 baseline16、capture recovery19、projected capture15、ordinary validation/gate75。覆盖完整来源身份、policy-only 变更、CAS 篡改、superseded source、新 command 拒绝、lostreply 精确只读恢复、历史不读 artifact/clock、并发不分叉、未知 family 不补造，以及普通 checks/review gate。集合不相加成新能力或完整平台结论。

每轮记录 136 项 backend/test/config 来源；before/after 相同，最终当前字节也相同。没有修改产品或正式测试，没有读取真实 key、调用 provider、运行完整 native Task 三例 P 或修改 Git/远端。共同 native P 继续引用自己的 aa/ef 来源证据，不能升级为本分支新实测；本分支 G/S 本任务未执行。

## 保留的环境失败

第一次 WSL 证据脚本在测试前无法读取 Windows `.git` 的 `C:/` 管理路径；只为脚本自身的只读 Git 命令转换 `/mnt/c`，没有向 Candidate fixture 子进程传入 GIT_DIR。初始脚本和说明保留。

第一次真正 Linux pytest 使用 `/mnt/c` basetemp。首个实际失败显示 executable mode 为0777而非要求0755；其余记录为 PATH_OUTSIDE_AUTHORIZATION，不能把这些失败改写为通过。为及时取得 trace，root 授权后仅对完整 argv 精确匹配的该 pytest PID 发送一次 SIGINT。原 JUnit 记载80 tests、50 failures、0 skips，完整 stdout/XML/中断记录保留；没有为此修改产品。随后使用唯一、未复用的 Linux `/tmp/karajan-rebind-f7d-20260907-0243` 和私有 bytecode cache，原167项完整通过。

WSL venv 没有 ruff/mypy 模块，原 stderr 和退出码保留；没有安装依赖或把它们算作通过。Ruff 由可用的 Windows 工具执行；Linux 类型分支由 Windows-hosted `mypy --platform linux` 实际通过。初始 DrvFs 下的 source map 同样前后不变。

## 复跑与归档

从本 worktree 根目录，使用正确平台已有 Python、离线环境及新的独占 basetemp：

```text
python -m pytest tests/candidates -q -p no:cacheprovider -o "pythonpath=backend tests/candidates" --basetemp=<new-private-test-directory>
python -m ruff check backend/karajan/candidates tests/candidates
python -m mypy --platform win32 backend/karajan/candidates
python -m mypy --platform linux backend/karajan/candidates
```

Linux 的 basetemp 必须位于实际 Linux 文件系统，例如新的 `/tmp/...`，不能用 `/mnt/c` 证明 POSIX 模式行为。设置 PYTHONPATH 为本 worktree 的 backend；不要覆盖旧测试现场。原实际命令、运行时路径及输出位于平台子目录；脚本只用于保存证据，不是产品入口。

精选证据归档到 `examples/reviewer-rebind-common-integration-20260907`。只复制命令、来源、日志、XML、报告及 `.py.txt` 脚本，不包含 SQLite、Git fixture、state、pycache 或 mypy-cache。发布映射保存逐文件源/目标 SHA 和大小；没有 stage、commit 或 push。
