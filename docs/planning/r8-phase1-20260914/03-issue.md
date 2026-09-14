# R8-P1-03：Workflow 配置真实加载、部署回执与 active 槽位

## 问题与结果

基于02的实际配置包，将“保存/编译”与“部署生效”分开：持久部署意图，物化 pending 包，由可信 Workflow loader 实际读取并核对完整文件/编译摘要，条件更新 active，重启后恢复同一配置。提供可调用部署/读回/回滚 API，不能只写一条 deployed=true 记录。

## 依赖与范围

依赖本批02；依据 architecture/10 的 pending/readback/CAS active，FR25/WD-AC03–09 的配置加载子范围。新增 deployment/loader 模块及认证 HTTP 入口，复用02存储/编译和现有项目身份。定义发布、部署状态、实际业务执行必须区分。正向ready样例使用02中真实可调用、已有测试证据的本机确定性 artifact_aggregate，不能用只登记schema的虚构能力使模型Workflow通过；模型能力未知的配置仍阻塞。04的控制协议授权样例不宣称某个模型执行器已ready。

## 验收条件

- [ ] AC1：确认请求绑定项目/会话、准确 bundle/file/compiled digest、preview revision、slot expected active revision 与 deploy_only 动作。未编译/旧预览/摘要错误/跨项目/无效授权拒绝，不能让模型文本激活配置。
- [ ] AC2：持久化命令后物化 pending，可信 loader 真实读取受管文件、重新核对摘要及已登记执行种类/必要能力，再返回加载回执；只有相同摘要/目标且 CAS 成功才 active。准备新版本期间旧 active 保持有效，pending不接新运行入口。
- [ ] AC3：同幂等键/同载荷查询原结果，异载荷拒绝；并发槽位更新至多一者成功。物化/加载/发布结果未知时先核对原命令，不用生成新包或重发新部署掩盖未知。失败保留可恢复诊断。
- [ ] AC4：新服务进程启动重新加载/核对 active 的真实文件，不把旧 ready 回执当本进程已加载；文件损坏或不匹配时显示 unavailable/blocked。回滚指定历史包并 CAS 当前slot，走相同pending/加载流程。
- [ ] AC5：提供可信消费者读取“已加载且active”的不可变定义接口，返回准确部署/文件/编译身份；消费者固定该句柄后，新部署/回滚不热改旧句柄。未就绪不可消费。测试实际文件读写、加载、失败窗口、并发CAS、重启和旧句柄隔离，不能全部以返回true的loader fixture验收。
- [ ] AC6：认证/CSRF/跨项目拒绝和完整API读回通过，包含不带具体业务输入的参数化模板deploy_only示例；Ruff/mypy/快速门通过，证据标C/P。尚无资格的执行能力显式阻塞，不能通过请求注入loader绕过。

## 非目标

本票只完成配置加载/激活及可信消费接口，不启动模型/业务Run、不承诺完整WD/S验收，不实现网关服务安装。部署API对尚未实现的deploy_and_run明确拒绝/unsupported，不伪造Run或重复确认界面。Phase1-04会消费冻结部署形成持久运行任务图；物理Agent执行和最终交付另行验收。

## 交付

分支 codex/r8-p1-03-workflow-deployment，依赖02合入后的dev。Claude Code CLI默认gemini-use worker实现、测试、提交PR；root独立审查合并。只关闭本子票配置部署范围，完整WD/Designer/运行父范围保持未完成。返修沿用同一PR，PROGRESS.md仅本机检查点。

Parent: #174
Refs #1
