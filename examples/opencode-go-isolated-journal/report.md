# GoCallJournal 独立边界审查

审查者：capacity_facts；作者：qualification_integration。仅审查 `backend/karajan/adapters/opencode/go_journal.py` 与其作者测试；为理解消费者只读了 Go relay 的 journal 调用点。没有修改产品、调用真实模型或读取凭据。

基线 commit：`15084c2d07f36165c1e00901d6689c3fd105b749`。

初始产品 SHA256：`394958d3f020d6016404e88f7fed0a06525785b1f7f4523bdff173a7def2a451`。

初始作者测试 SHA256：`4a3d2490190285eee2b545d0680b5457c6632f3f47c2352792c95f97983a3469`。

## 确认发现

**P2 GO-JOURNAL-001：已到期的发送许可可以因系统时间回退再次激活。**

公开调用 `create_grant(clock=100, expires_at=110)`，随后 `begin_call(clock=110)` 正确抛出 `GRANT_EXPIRED`；重新打开同一个 SQLite，以 `clock=105` 调用新的 call，却返回 `send_allowed=True` 并持久写入发送意图。`begin_call` 到期分支只比较当前 wall clock，没有持久化失效状态或可信时间高水位。一次明确的到期拒绝因此没有收敛，真实系统校时/重启可能延长已经失效的发送权限。

原输入：`test_independent.py::test_expiry_rejection_cannot_be_undone_by_wall_clock_rollback_after_reopen`。实际首次运行 `before.junit.xml`：1 失败、10 通过，0.74 秒。状态：已关闭。作者新增持久过期记录，先提交到期事实再抛出 `GRANT_EXPIRED`；之后即使时钟回退仍拒绝新 call，历史读与完成保留。

独立复验 `after.junit.xml`：原 11 项全部通过，0.76 秒。复验产品 SHA256：`2da85e23d025e5ff821dbd17869b1385ee9fde9f5ca0acaff150d94aced3852d`；作者测试 SHA256：`40ee17ec6731a66f190c354267f04c4aea7ea56305729cca696cb5c0464fbd3c`。仅规范化了独立测试 import 的空行，原输入与断言不变；没有把作者复跑当成独立结果。

## 已通过的独立控制

- 真实子进程提交发送意图后立即退出，不向父进程返回许可；重开 SQLite 后同 call 只能读到 unknown，不能再次发送。
- 四个真实进程竞争同一 SQLite：总共只颁发六个许可，同一个逻辑 call 仅一个进程获得许可，序号连续且不复用。
- 撤销和到期后可记录原发送的 `send_unknown` 结果，不能退款或重新颁发许可；重复创建同 grant 不返回新 capability。
- 历史 call 仍严格检查完整绑定；错误 auth generation 不返回旧许可或修改状态。
- 原始 header/secret 字段以固定错误拒绝；本地 synthetic secret 与一次性 capability 不进入 SQLite 字节或快照。
- 已构造实例的数据库随后移走时，只读入口拒绝且不新建数据库。
- `inf`、`nan`、布尔值、负数 clock 拒绝新意图且保持原记录不变。

作者原测试另行运行：51 通过，1.89 秒，见 `author-suite.before.junit.xml`。这不替代上述独立时间失败。

执行命令（cwd 为当前隔离 worktree，导入与子进程 PYTHONPATH 均固定此处的 backend）：

```powershell
C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe -m pytest .cache/go-journal-review/test_independent.py -q --junitxml=.cache/go-journal-review/before.junit.xml
C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe -m pytest tests/adapters/opencode/test_go_journal.py -q --junitxml=.cache/go-journal-review/author-suite.before.junit.xml
```

这些证据仅覆盖本地持久发送许可及脱敏记录，不证明真实请求发送、现金约束、操作系统隔离或远端取消。
