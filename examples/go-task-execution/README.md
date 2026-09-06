# #90：已批准 Go Task 执行证据

范围见 [Issue #90](https://github.com/zhouy1017/Karajan/issues/90) 和[版本化规格](../../docs/planning/go-task-execution-issue.md)。本包按组合候选 `c2c4b145473a259582d0d37a01e423ca03f03b65` 整理。各报告仍只证明其记录的源码版本；组件的历史通过不自动成为组合候选通过。

本包为 C（产品行为）和适用的 P（本机执行）证据。SQLite、Git/CAS、资源回执和相关公开接口使用真实实现；测试中的资格、规划、配额、Host 身份或 native 结果替身均由各组原文说明。实际 Linux native 测试使用固定测试入口和 loopback HTTP fixture；它不能替代生产 bootstrap 或官方服务资格。**官方 provider 调用为 0；S（真实服务）、U（工作台）及 G（远端 CI）不由本包宣告通过。最终组合候选的 CI 尚待通过。**

## 已整理材料

| 证据组 | 结果与范围 |
| --- | --- |
| [生命周期作者证据](go-lifecycle-evidence/README.md) | 原 operation 持久 intent、一次 control、激活/启动/采集边界；Windows 与 Linux 各 58 通过。Host 后续修复另有来源绑定。 |
| [生命周期独立审查](task-intent-independent/README.md) | 原回执恢复、冻结 launch、缺库拒绝、control 竞态和晚到 Candidate；Windows 与 Linux 各 9 通过，0 finding。 |
| [Collector 作者证据](collector-author/README.md)及[修复补证](collector-ownership-correction/README.md) | 完整 Candidate 身份、旧 revision 恢复、缺可选资源的历史读取；修复后 Windows 与 Linux 各 52 通过。 |
| [Collector 独立审查](collector-independent/README.md) | 2 项 P2 已闭环；原 11 项在 Windows 与 Linux 均通过。 |
| [已有数据库独立审查](existing-stores-independent/review.md) | 1 项 P2 已闭环；Windows 与 Linux 各 27 通过。严格 existing-only，不重建缺失账本。 |
| [工厂与来源独立审查](runtime-independent/README.md) | 来源遗漏、可用 Host 取消被缺失 Journal 阻断两项已闭环；Windows 与 Linux 各 6 通过。 |
| [Consumer 独立审查](task-consumer-independent/README.md)及[作者修复](task-execution-author/consumer-001/README.md) | 完整 Host 取消绑定 P2 已闭环；原 10 项在 Windows 与 Linux 均通过。历史绑定不构成当前执行权限。 |
| [作者 d 轮历史](task-execution-author/history/d-round/README.md) | 11 项 facade 及 3 项实际 Linux native/HTTP fixture 通过；后续 storage、Host 与 Relay 已变，保留原 manifest，不标为最终组合来源。 |
| [组合候选 e 轮](task-execution-author/native-e/README.md) | 实际 Linux Host/native/HTTP fixture 3 项通过，161.88 秒；正常采集、grant 提交回复丢失、首发后取消。源码绑定 `c2c4b145`，保留完整公开 operation 和 source manifest。 |

根作者补证保存在 `root-checks/`：工厂延迟历史读取 22 通过；storage 兼容补验 15 通过；Spark Relay 集成 108 通过。原兼容组 **385 通过、1 失败**也保留。该失败是默认 SQLite 连接参数由 `Path` 改为字符串后，旧公开 commit 故障注入未命中；恢复默认分支原 `Path` 后，原失败及 14 项 existing-only 检查共 15 通过。existing-only 的 URI 打开语义保留；这里没有宣称完整 386 项已重新执行。

## 证据边界

最终组合的 e 轮和独立 intent 生命周期审查均已追加，d 轮仍仅作为历史；远端 CI 状态由发布流程继续核对。取消后无法证实的 native/provider 状态仍为 `unknown`。Candidate 已产生也不代表检查、独立 Review 或 PR 交付通过；这些后续行为不属于本片证据。

## 字节与复现

[copy-manifest.json](copy-manifest.json) 逐文件记录原路径、发布路径、长度、SHA256 和 XML 计数。所有复制材料保留原始字节、原命令和原来源摘要；文中 `.cache/...` 为当时的执行位置，可通过映射找到本包对应文件。历史测试与独立测试均以 `.py.txt` 存档，避免普通 pytest 自动收集故意失败历史；在独立临时位置恢复后可按原报告命令复验。正式测试仍见仓库 [runs](../../tests/runs)、[execution](../../tests/execution)和[candidates](../../tests/candidates)目录。

本包不含数据库、凭据、原始模型正文、CLI 日志、bytecode 或可变 native 运行目录。为控制体积，d 轮约 900 KB 的公开 operation 快照未复制；原 freeze 仍记录它们的摘要，本包保留公开摘要和三份完整 source manifest。

旧 `freeze.d-before-followups.json` 记录的是补充说明前的作者 README 摘要；当前复制的 README 已追加后续变更说明。该文档引用差异已逐值记录在映射文件中，旧 freeze 不改写。
