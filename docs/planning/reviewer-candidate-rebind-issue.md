# Candidate 同内容 Reviewer 政策重绑定与精确恢复

Parent: #95。Related: #90、#94。范围是内部 Candidate 存储原语，不启动模型。

## 输入与行为

可信控制器提供原 Candidate 全身份（含 Freeze request 摘要）、固定 command_key 与版本化
Reviewer binding。绑定保留原 Run/operation/Reviewer Task、capture、批准、计划、
ExecutionPolicy、规则与资格来源摘要。声明不等于当前角色资格；上层须在当前 guards 内调用。

`CandidateStore.rebind_reviewers(binding, command_key=...)` 从现有完整 CAS 创建同一 series
的新 revision，仅派生 Review 允许集合及 policy 摘要。原 baseline、tree/content/input、
manifest/模式、作者、Writer 停止事实、允许路径、任务等级和全部 Checks 不变。

`lookup_review_rebind(...)` 精确恢复原提交，不读 artifact/Git/时钟，不创建目录或数据库。
不接受替代 Freeze request、检查命令/环境或调用者给出的 Candidate 内容。

## 验收标准

- [ ] **C/P：内容不变。** 临时真实 Git/CAS 的二进制、空文件、未修改文件及 Linux 可执行模式
  在新版本保留；原 Candidate 与 Evidence 不改，新版本须有自己的 Checks/Review。
- [ ] **C：精确来源。** 全部来源身份/原请求摘要必须匹配，空/重复 Reviewer 集合、额外政策字段、
  错误来源、歧义或损坏的持久身份均拒绝。未知 family 保留未知，不造资格。
- [ ] **C/P：新效果。** 新提交仅接受当前 source revision，核对完整 Candidate 与 baseline CAS
  的字节/大小/哈希/文件类型与 Git blob/tree 身份；损坏、缺失或链接资产不创建新版本。
- [ ] **C：一次提交。** 并发相同命令只产生一个 revision；同 key 不同绑定（含跨 series）冲突。
  commit 已成功但回复丢失时，新实例精确读回原 ID，不再提交。
- [ ] **C：历史恢复。** 旧 source 已被替代、CAS/Git 不可用时，精确 lookup/replay 仍只读返回原收据；
  新的不同命令继续受当前来源门限制。缺失账本不初始化替代状态。
- [ ] **G：可审阅交付。** 当前实现 commit、公开可复跑输入、两平台相应检查、独立 Standards/Spec
  及当前 PR 必需 CI 齐备。合入 dev 后按本票原范围验收，不自动合并。

## 明确保留

#95 仍负责当前 Reviewer 资格/Rulebook 交集的真实编译、依赖选路/Capacity、只读模型执行、
真实 S、结果和 Review Evidence。#94 仍负责可信 subject 切换、全部 Checks 重跑及累计预算。
本票不把手写 binding 当真实资格，不实现 HTTP 入口，不改原 capture 指针，也不宣布父票完成。

## 当前实现进度（2026-09-07）

已完成：尚无本票实现已进入 dev 的完成项。[草稿 PR #98](https://github.com/zhouy1017/Karajan/pull/98) 已发布存储原语及本地 C/P 证据；实现 `58eafc337b7a2350c9e983d550e14ce50b2dc5e9`，首发候选 `bd4982dc9c4be5b5913871994537631b711609d7`。

本地验证：42 项新增公共测试通过；Windows Candidate 回归 164 passed / 3 POSIX skips，WSL 167 passed；独立代码审查与 Linux 复跑核心 7 项通过。15 份原始复制件、6 个生成文件和 9 个仓库输入的 Git blob 摘要已核对；[证据索引](https://github.com/zhouy1017/Karajan/blob/bd4982dc9c4be5b5913871994537631b711609d7/examples/reviewer-candidate-rebind/README.md)保留原始失败和复跑输入。授权/资格是明确 fixture；S 不适用于本存储原语。

剩余工作：当前 PR 必需 CI、合并与本票原范围最终验收。#95 的当前资格编译/路由/Review/S 和 #94 的 subject 切换、全部 Checks 重跑、预算保持仍属于对应父票，不因本地原语完成而通过。原验收清单保留，当前 status:in-progress；未使用 Closes。

阻塞：基础 Task #90 / PR #92 与共享 relay PR #88 的 CI 修复尚未完成；指定 Spark 当前无额度，替换模型选择等待 owner。不会自动合并。
