# M1 候选冻结与验证证据切片

本实现覆盖 [M1-03](../planning/v1/issues/m1-03.md) 中本地候选提取和独立质量门的领域逻辑，并为 [M1-04](../planning/v1/issues/m1-04.md) 提供固定内容与证据输入。它不是整张 M1-03/M1-04 的完成声明：没有 Agent 派发、RunnerHost 接线、原生权限执行、交付 activation、GitHub PR 或 CI 集成。

依据：[候选与证据约束](../architecture/03-execution-and-delivery.md#7-候选与证据)、[独立交付 ADR](../adr/0003-independent-delivery.md)。实现没有模型请求或现金 API 请求。

## 公开入口与信任边界

```python
from pathlib import Path
from karajan.candidates import CandidateStore, CandidateError

store = CandidateStore(Path("state/candidates"))
baseline = store.register_baseline(
    trusted_repository,
    repository_identity=registered_identity,
    base_sha=approved_base_sha,
)
candidate = store.freeze(worker_workspace, freeze_request)
copy = store.materialize(candidate["id"], new_check_directory)
check = store.record_check(check_request, log=complete_log_bytes)
review = store.record_review(review_request, log=review_log_bytes)
gate = store.gate(candidate["id"], current=current_validation_context)
historical_candidate = store.get(candidate["id"])
```

这些是可信本地控制器调用的 Python API，不是可由 Worker 直接调用的 HTTP/IPC 端点。`writer` 的停止观察、作者身份、Reviewer Profile/家族资格、独立上下文观察和当前验证上下文必须由后续控制器/执行入口提供并核对；模块不能把任意调用者提供的字符串变成真实运行证据。所有输入拒绝未知字段，错误不回显 Pydantic 原始输入。

`register_baseline` 只接受已登记、受信基准仓库及精确 40 位 commit SHA，不解析可移动分支作为最终基准。该入口读取受信仓库的 Git 对象与本地配置；绝不能把 Worker 的仓库当作其来源。它关闭用户/系统 Git 配置、hooks、fsmonitor、replace objects 和传输协议。它不会 fetch、执行构建、读取登录凭据文件或调用 credential helper。

`freeze` 完全不向 Git 传入 Worker 的 `.git`、index、配置、hooks、attributes 或 filters。它跳过顶层 `.git`，读取普通文件，再将文件内容导入平台新建的 bare Git 对象库。实际坏配置与 hook canary 用例证明这一分离。工作区根及内部 symlink/junction/reparse point、硬链接和非常规文件被拒绝；含控制存储的工作区被拒绝。这是文件提取守卫，不是对同一 OS 用户下恶意进程的沙箱认证。

## 固定输入与内容身份

`freeze_request` 的完整固定示例见 [freeze-input.json](../../examples/candidates/freeze-input.json)。其中 `baseline_id` 是登记结果，示例运行器会替换占位值。字段包含：

- `series_id`：一个候选序列的稳定身份。调用方绑定 Run/Task，不得用新序列绕过上游累计边界。
- `baseline_id`、`input_sha256`：已批准代码基准及完整输入包摘要；输入包应包含需求、任务/计划版本和相关依赖。
- `allowed_paths`：仓库相对路径。精确文件匹配，目录必须带末尾 `/`；不支持 glob、绝对路径、反斜杠、`..` 或空范围。
- `task_class`：T1/T2/T3；T0 不可冻结为就绪实现。
- `writer`：Attempt、fence、已停止状态、观察引用。必须匹配一个已登记作者，`stopped=false` 不接受。
- `authors`：每位作者的 Attempt/fence、Profile revision、模型家族、上下文身份与来源引用。
- `policy`：固定检查配置 ID/revision、每个必需检查的 ID/revision/argv/环境摘要，以及独立 Review revision/环境摘要/允许 Reviewer 身份与资格引用。

固定策略来自可信控制器，不从候选中的配置重新加载。检查 ID 和允许 Reviewer 的 Profile revision 身份不能重复。修改或删除检查条件不能沿用旧策略摘要。源代码、模式及新增/删除的真实差异由 Collector 计算，不能由 Worker 自报 touched_files 绕过路径授权。

Candidate 包含 `repository_identity + base_sha + tree_sha + input_sha256` 的规范 JSON SHA-256 内容身份，另有不含本机绝对路径的文件 manifest SHA-256。manifest 每项记录相对路径、模式、Git blob SHA、原始字节的 SHA-256/大小及本地 artifact 引用。复制到不同控制目录不会改变基准 ID、内容或 manifest 摘要。

同一序列重复冻结相同内容与同一固定请求，返回同一 ID/revision。内容或固定请求改变时追加新 revision，历史记录不改写。旧 revision 的 gate 变为 `CANDIDATE_SUPERSEDED`。新 revision 必须重新获得全部必需检查与 Review。

首版只处理普通文件和 SHA-1 Git 仓库。基准 symlink、gitlink/submodule 被明确拒绝；不解析 Git LFS 对象，不下载依赖或远端生成输入。POSIX 文件系统按实际用户执行位保存 `100644/100755`；新增可执行文件以及仅改变执行位的变更都进入 tree/manifest/changed_paths，并在 materialize 恢复模式。Windows 导入保留基准中已有文件的可执行模式，新文件为 `100644`，不能证明 Windows 工作区中的 chmod 变化。需要 Windows 模式编辑、LFS/submodule 或最终 commit 元数据的检查，须由后续 materializer 扩展并重新验收；本片不提供这些资格。

当前工程边界为单文件/单日志最多 8 MiB、快照累计 64 MiB、枚举最多 10,000 项。它们是 Collector 防资源失控的实现限制，不是用户模型预算或新的产品配额决定。

## 检查、Review 与动态 gate

`record_check` 输入绑定候选 ID、策略/输入/环境摘要、检查 ID/revision、执行者和观察引用、退出结果及 provenance。它不启动命令，也不把 `argv` 当作已经执行的证明。退出非零为 `failed`；超时、取消、未知/缺退出结果为 `inconclusive`；零退出但没有完整非空日志为 `unavailable`；任何固定条件不匹配为 `invalidated`。

`record_review` 除同样绑定外，还要求精确 Review revision、实际提供给 Reviewer 的必需检查 Evidence ID 集、执行者、是否带入作者论证、结构化 verdict/findings。每条 finding 有严重性、文件/行、具体行为、触发条件、验收依据和 blocking 标记。缺结构化字段拒绝输入；阻断 finding 或失败 verdict 不能被“passed”文本覆盖。无日志不能通过。

T1/T2 可使用作者相同 Profile，但必须是不同 Attempt、不同上下文，且没有带入作者论证；身份与来源引用为必填。T3 要求 Reviewer 和全部作者家族均已知，且 Reviewer 家族不同于每位作者。Reviewer 必须匹配固定允许集合的 Profile revision 与有来源的家族，不能靠自报新家族绕过。空允许集合等待，fixture 资格不启用真实 Profile。

Evidence 的 `evidence_key` 是全局接收幂等键。相同键、类别、完整输入与日志摘要返回原记录；换候选、输入或日志造成 `EVIDENCE_KEY_CONFLICT`，不覆盖历史。多个真实检查结果保留，gate 使用每个必需检查的最新结果；检查重跑产生新 Evidence ID 后，之前基于旧结果的 Review 失效，必须重新审查。

`gate(..., current=...)` 要求当前 `repository_identity/base_sha/input_sha256/policy_sha256`。调用方不能从旧 Candidate 自行构造“当前”上下文代替控制面事实；示例仅为固定夹具这样构造。当前上下文改变会把已存证据显示为 `effective_status=invalidated`，历史 `status` 保留。再次核对候选和所选日志的字节摘要，缺失或损坏均阻塞。历史失败/失效记录留存，不静默挑选较早的成功结果。

gate 返回：

```json
{
  "schema_version": "karajan.candidate-gate.v1",
  "candidate_id": "...",
  "local_gate_passed": true,
  "delivery_eligible": false,
  "live_qualification": "not_run",
  "reasons": [],
  "evidence": []
}
```

此处 `evidence` 在实际结果中包含记录和有效状态。`local_gate_passed` 仅代表这个领域门按已提供事实通过，不能作为远端写入许可。`delivery_eligible` 在此切片恒为 false：真实授权、暂停/取消、RunnerHost stop/fence、来源资格、预算及独立交付域尚未贯通。最终交付入口必须在自身 activation 事务重新核对这些条件；读取 gate 不是锁，也不是能力令牌。

## 存储与副本

文件先写入临时文件、flush/fsync、原子替换到内容寻址位置，之后 SQLite 才保存引用。已有同摘要但不完整的对象不再发布为有效引用。数据库采用短写事务、FULL synchronous，连接明确关闭；候选 revision 与 evidence 幂等冲突在写事务中决定。失败导入或崩溃留下的未引用对象可能存在，但不能满足任何 gate；自动回收和整机备份恢复不在本片范围。

`materialize` 从已冻结 artifact 重查摘要后，写入全新目标目录，拒绝覆盖已有目录或控制存储。不导出 `.git`，不执行 hooks、安装或检查。检查生成的 scratch 文件只影响导出副本，不会改变冻结内容。导出失败可留下未完成目录；调用方只能接受成功返回结果，不能将目录存在视为完成。工作区停止证明和控制目录 OS 隔离仍须上游实际提供，不能靠这些文件操作假称已达到 `tool_sandboxed`。

## 实际验证与红绿记录

环境：Windows、Python 3.12.14，本机 Git；所有仓库均为新建临时夹具。没有修改用户仓库，没有模型调用。

```powershell
.venv/Scripts/python.exe -m pytest tests/candidates -q
.venv/Scripts/python.exe -m ruff check backend/karajan/candidates tests/candidates examples/candidates
.venv/Scripts/python.exe -m ruff format --check backend/karajan/candidates tests/candidates examples/candidates
.venv/Scripts/python.exe -m mypy backend/karajan/candidates examples/candidates/probe_validation.py
.venv/Scripts/python.exe examples/candidates/probe_validation.py --directory .cache/candidate-validation-probe-20260905-r3
```

探针要求新的输出目录；重跑请使用新目录，不清理或覆盖已有结果。固定输入、实际运行输出分别保存在 [freeze-input.json](../../examples/candidates/freeze-input.json)、[validation.report.json](../../examples/candidates/validation.report.json)。报告绑定输入与实现文件 SHA-256、日期、OS、候选/base/tree/manifest、固定策略、实际检查 exit/log 和 fixture Review 来源。报告中的本地临时绝对路径是当次环境，不是跨机器可用的文件链接。

实际探针 10 项条件通过：真实 Git 树相符、坏 Worker Git 配置不影响提取、真实本地检查成功与失败、独立 fixture Review 门、产生新候选、旧候选失效、新候选失败阻塞、受信基准未改变及无真实交付资格。真实检查在冻结后导出的独立副本执行；Review 和 writer-stop/资格来源明确是 fixture。现金和模型调用均为 0。

以下为逐行为先红后绿的真实运行记录；每一行都先执行对应 `pytest -k` 观察失败，再做最小实现并重跑通过，没有把首次即通过的补充测试算作红绿证据。

| 轮次 | 公开行为 / 测试选择词 | 实际 red | 对应 green |
|---|---|---|---|
| 1 | `freeze_preserves` | 模块不存在，收集失败 | 1 passed |
| 2 | `outside` | 新增/修改/删除未拒绝，3 failed | 3 passed |
| 3 | `writer` | 未绑定停止观察，3 failed | 3 passed |
| 4 | `directory_links` | junction 被读取后才报越界，1 failed | 1 passed |
| 5 | `gate_waits` | gate 尚无入口，1 failed | 1 passed |
| 6 | `successful_process` | record_check 尚无入口，1 failed | 1 passed |
| 7 | `process_failure` | 失败/未知/缺日志误通过，5 failed | 5 passed |
| 8 | `different_frozen` | 检查输入未绑定，5 failed | 5 passed |
| 9 | `same_profile` | record_review 尚无入口，1 failed | 1 passed |
| 10 | `reviewer_independence` | 作者身份/上下文/资格未拒绝，6 failed | 6 passed |
| 11 | `t3_requires` | 同家族/未知家族误通过，2 failed | 2 passed |
| 12 | `review_conclusion` | 失败/不确定/finding/缺日志误通过，4 failed | 4 passed |
| 13 | `current_validation` | 当前基准/输入/策略/仓库变化未失效，4 failed | 4 passed |
| 14 | `changing_workspace` | 旧 gate 仍通过，1 failed | 1 passed |
| 15 | `artifact_integrity` | 候选/检查/Review 缺失或损坏未阻塞，6 failed | 6 passed |
| 16 | `new_check_execution` | 新检查仍沿用旧 Review，1 failed | 1 passed |
| 17 | `review_requires_exact` | Review 配置和检查集合未绑定，5 failed | 5 passed |
| 18 | `repeated_evidence` | 重复接收触发 SQLite 唯一冲突，2 failed | 2 passed |
| 19 | `repeated_freeze` | 重复冻结误产生新版本，1 failed | 1 passed |
| 20 | `portable` | manifest 含本机路径导致摘要变化，1 failed | 1 passed |
| 21 | `containing_control` | 控制存储仅被当成越界文件，1 failed | 1 passed |
| 22 | `authorized_paths_require` | 非规范路径被接受，7 failed | 7 passed |
| 23 | `duplicate_check_names` | 重复必需项未拒绝，1 failed | 1 passed |
| 24 | `bounded_file_size` | 超限内容被接受，1 failed | 1 passed |
| 25 | `materialize_exports` | materialize 尚无入口，1 failed | 1 passed |
| 26 | `missing_workspace` | FileNotFoundError 泄漏，1 failed | 1 passed |
| 27 | `root_link` | 工作区根 junction 被跟随，1 failed | 1 passed |
| 28 | `already_corrupt` | 对损坏对象继续发布引用，1 failed | 1 passed |
| 29 | `posix_executable_modes`（实际 WSL/ext4） | 新增/增加/移除执行位的 Git tree 均不匹配，3 failed in 0.72s | 3 passed in 0.58s；另全量 75 passed in 7.02s |

首次即通过的补充回归包括坏 Worker Git 配置/hook canary、已知不同家族 T3 成功、无合格 Reviewer 等待和真实探针公开 CLI；这些只算回归覆盖。最终测试/lint/typecheck 结果以下方交付记录为准。

## 交付记录

初次冻结：2026-09-05 Windows `pytest tests/candidates -q` 为 **72 passed in 33.81s**。交叉审查发现并修复 POSIX 模式遗漏；修订后的实际 WSL 全量为 **75 passed in 7.02s**，Windows 全量为 **72 passed, 3 skipped in 33.11s**（3 项 POSIX 位测试只在真实 Linux 文件系统执行）。Ruff check 与格式、严格 mypy（3 个库源文件加探针，共 4 个源文件）通过；Windows 探针 r3 退出码 0，10/10 条件通过，结果已重新保存，源码摘要对应修订后实现。

WSL 使用 Ubuntu Python 3.12.3，在全新私有 `/tmp/karajan-candidate-mode-qy6_mqo2/venv` 安装固定 `pydantic==2.13.5`、`pytest==9.1.1`；未写系统 site-packages。测试仓库由 pytest 在 Linux `/tmp` 创建，执行位不是模拟值。可复现命令（虚拟环境路径按新的私有环境替换）：

```text
PYTHONPATH=/mnt/c/Users/Chooo/Playground/Karajan/backend /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/candidates -q -k posix_executable_modes -o cache_dir=/tmp/karajan-candidate-mode-qy6_mqo2/pytest-cache
```

初次 red 因 Windows 挂载目录中的 pytest cache 不可写出现 2 个 cache warning；后续将 cache 指向同一私有临时目录，未改挂载目录权限。该文件模式验证不提升工具进程隔离资格。

尚未执行：真实模型 Reviewer、跨两种真实合格来源、OS 沙箱资格、RunnerHost/Run 当前 fence 与授权联动、质量修复计数、暂停/取消 activation、真实 PR 与远端 CI。这些均保持 `not_run` 或未集成，不据离线通过勾选整张 M1-03/M1-04。
