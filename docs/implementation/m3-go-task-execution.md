# 批准 Go Task 的固定执行入口

本切片对应 [#90](https://github.com/zhouy1017/Karajan/issues/90)，连接原批准 Run、已预约
operation、冻结 Workspace、Host 直属进程和 Candidate。前置接口分别见
[#89](https://github.com/zhouy1017/Karajan/issues/89) 与
[#87](https://github.com/zhouy1017/Karajan/issues/87)。本文件说明实现契约，GitHub Issue
保存实际验收状态；本地 fixture 的成功不等于部署 Profile 已取得真实服务资格。

## 入口与部署

`ApprovedGoTaskExecution` 的 `advance/get/cancel/reconcile` 只接受原
run_id、operation_id 和 principal。HTTP 请求、项目文件和模型不能提供 argv、provider
地址、prompt、凭据路径或 Collector 报告。内部服务由可信控制器构造。

`GoTaskSettings` 是私有部署配置。显式 `write_go_task_bootstrap` 只创建固定名称的私有
配置文件，不创建业务库或凭据。固定 child 使用 Python `-I` 和部署目录中的确定入口，
从当前控制目录读取 bootstrap，导入自身部署的 backend，拒绝项目目录的同名 Python 包。
真正执行的配置还核对固定 runtime、解释器、venv 配置及源码；主执行平台为 Linux/WSL2。
任务临时目录必须与控制状态及项目仓库分离，且足够短以满足本机 Unix socket 路径限制。

重新连接使用 `existing_only`：核心 Project、Run、Capacity、Admission 必须存在且具备
所需 schema，连接使用 SQLite `mode=rw` 或只读模式，不运行迁移或补建空账本。
子存储的父对象也必须使用既有库模式，避免一个正常初始化模式的父对象在重连时补建库。

历史读取和收尾不解析 tokenizer、凭据或真实资格 suite。Journal、Host、Candidate
可先构造 `existing_only=True, defer_validation=True` 的内部路径句柄：这不访问或创建
账本，每项实际观察和操作仍使用既有库连接。某个可选账本缺失时，它的事实保持未知；
其他已证明归属的 Host 收尾仍可进行。执行模式则在产生效果前严格验证执行材料和账本。

## 原身份、当前权限与实际效果

执行意图保存在原 Admission operation 中，固定原 Attempt/context、Workspace、授权、
预算与 activation/start/grant/cancel 标识。启动准备和一次性效果 claim 分开持久化。
`initialize_control_once` 只初始化原 prepared Host 的缺失控制，不覆盖已撤回控制或新 fence。
Host 精确登记 supervisor 实际启动的直属 child PID/birth；任意 caller 提供的 PID、
旧收据或孙进程不能获得该身份。

每次实际启动与 HTTP 发送按 operation → Run → Project → Capacity → Host 顺序取得
当前门禁，然后核对实际源码。响应正文读取不长时间持有业务数据库锁。注册握手的等待
也在业务锁外进行。效果 claim 先提交，随后才解析已批准认证来源并签发原 grant；丢失
claim 返回不会重新解析材料、替换 grant 或再运行模型。

`task_runner_source` 绑定完整 `backend/karajan/**/*.py` 的排序相对路径及 SHA-256，
包括间接依赖的路由规则、配额和数据模型。v1 不支持运行时热更新；任何后端源码变更均
要求新的来源绑定。资格机制摘要与整个 Task runner 摘要分开，历史实测报告不改写。

任务输入由原批准 Workspace 和完整 CAS 基线编译。当前范围要求已有文件的 read/edit，
工具与输入限制沿用批准 ExecutionPolicy；不读用户当前工作树来替换已批准内容。

## 停止、收集与恢复

固定 child 将实际 `execute_go_task` 返回的停止捕获交给内部 `ApprovedGoCollector`。
类型和摘要只检查一致性；可信入口、真实直属 child 和当前门禁共同确定来源，公开入口
不接受调用者提交的捕获 JSON。Collector 验证原输入、grant、执行源码和实际停止证明，
从原批准检查与作者身份编译 Freeze，保留完整基线和文件模式。

收集先把完整 Freeze request、projection、内容摘要及停止证据绑定写入原 operation，
再提交 Candidate。提交成功但回执丢失时，恢复只读查找同一完整请求的唯一历史 Candidate；
即使它已不是最新 revision，也可链接原收据。恢复不重跑 worker、不读取可变旧临时目录，
不根据不完整身份推测成功。缺少 Candidate 收据时保持待核对。

本片 Candidate 的检查和独立 Review 保持待执行；worker 结果不能给自己放行。
尚未批准合格 Reviewer 时不生成假的 reviewer 列表，也不执行 PR 交付。

取消先提交原取消意图，释放业务锁后精确撤销原 grant 并停止 Host。Journal 不可用时仍
尝试有原始身份证明的 Host 终止，远端发送状态保持 unknown；核对继续同一收尾。
已提交但丢返回的 Capacity activation 只从原 command_receipt 恢复其 expiry，不调用
activate。已发送未知占用不退款，历史观察不激活、启动或替换 grant。

## 证据与后续范围

作者和独立测试分别覆盖既有库重连、部署入口、生命周期、精确 Candidate 恢复及故障。
本机组合用例使用实际 Linux Host child、namespace、固定 OpenCode 和本地 HTTP fixture；
其中资格与上游回答明确为合成事实。每份报告绑定被测源码，原失败证据与修复后证据分开。
当前提交是否通过 Windows/Linux 与前端 CI，以对应 GitHub 运行记录为准。

最终源码的官方 Go 资格及实际 Task 调用另列 S 证据，不沿用旧源码实测。其他服务现金
调用继续暂停。验证环境运行检查、合格 Reviewer、修复及组合任务、PR 交付和订阅窗口
核销仍由关联需求继续实现；token 参考量不冒充订阅窗口实际用量。

组件、独立审查及最终组合来源的报告见[执行证据索引](../../examples/go-task-execution/README.md)。
