# Go 固定资格已完成范围与下一切片

固定 Go 观察现已通过受控入口进入 `ProfileQualificationStore`，不接收用户上传的 passed 报告。
2026-09-06 的一次真实官方 Go 公共入口验证通过：`edit` / `denied_read` 分别产生 3 / 2 次
HTTP 200，同命令重放返回同一记录且无新增请求。默认 guard 仍返回
`TASK_PERMISSION_SCOPE_NOT_QUALIFIED`；固定 scope 的通过不能启用任意 Task。
见[实现说明](../implementation/m3-go-profile-qualification.md)与[实测记录](../../examples/go-profile-qualification/README.md)。
本安排沿用已确认的自有协调器、批准集合和资格门，没有扩大自动交接、合并或现金授权，
也不表示整个 M2-05 已完成或本次尚未提交的新 PR 已通过 CI。

## 本切片已实现的持久边界

1. credential reference 已有 controller 管理的不可变 generation。凭据材料留在受保护的本地
   resolver，数据库只保存公开 generation、来源与撤销事实。`resolve_exact` 必须发现 key 文件实际材料
   已变，拒绝旧 generation；文件名、mtime 或用户填写的版本号不够。证据不公开 key 哈希。
2. runtime source 已复用 `go_runtime_source` 的实际 artifact、系统和源码绑定。
   数据库事务只保证登记事实一致，不能声称同时锁住宿主文件；运行前仍须核对实际材料。
3. 已提供受控 `qualify_runtime_tools(project_id, profile_ref, principal, command_key,
   suite_ref, validity_seconds)` 入口。suite 从控制器固定配置解析，请求不接收 key、路径、endpoint、
   binding、任意代码、HTTP factory 或报告正文。
4. 在项目 owner/catalog 事务中保存 start、固定场景 Attempt/fence/grant 身份和完整来源绑定，
   然后释放锁执行。重放同命令只读历史，未完成 start 保留 unknown，不重建 grant 补额度。
5. 收尾从真实 journal 与受控观察器交叉核对，再次检查当前 Profile、runtime 和 auth generation，
   写入不可变 record。清理前核对 grant 的完整归属绑定，ID 冲突不撤销别人的 grant；匹配的创建
   丢失回复仍会撤销。grant 撤销是单次诊断收尾；qualification 撤销是后续事实失效，两者分别保存。
6. 共享 `_facts` 已有固定 scope 投影，保留现有 `routing_facts_guard` 与 `ApprovedRunRouting` 的消费结构。
   最新 start 按 project、精确 Profile、scope、suite 选择；最新 unknown 不能回退到更旧的 passed。

## 下一切片：批准 Task 权限与可信 Collector

完整 registration 已在当前资格绑定中。先固定批准配置，再做资格；之后改变 enabled、模型家族或
原生设置会使旧证据失配。可信 capability 只在路由读取时覆盖副本，不写回配置伪装为用户声明。

固定 `/workspace/fixture.py` 的 read/edit 不能自动推广到任意 Task path。应先登记具有明确范围的
观察，继续阻塞尚未具备的任务能力。下一步先定义由当前批准 Task 生成的路径权限材料，绑定
计划/授权版本、Attempt/fence、仓库输入与实际投影；工具执行前检查实际路径、只读输入和写入范围。
随后接入可信 Collector，从该 Attempt 的隔离目录捕获受控变更，保留 source/base/tree 与权限证据，
越界、过期身份或缺证据时不能形成可验证候选。取得这些公共验收后，再把资格准入与实际 effect 连接。
`context_tokens`、现金约束、Reviewer/Commander、candidate_capture 和远端停止保持未知或未执行，
不为了让一次路由变成 selected 而补默认值。

HTTP fixture 的 native 工具检查与真实 Go 模型检查分开。两者都能测试服务端持久控制流程，只有实际
官方 Go 调用能支持该次认证、模型接受及模型实现行为；诊断 auth generation 不能替代上述 resolver 登记。

## 后续必须保留的公共验收链

- 在第一条 provider 请求前，能从真实项目数据库读回 start 与固定 grant 身份。
- 同命令重开后读取同一记录，不再次调用模型；丢失回复、scope 错配、来源变更及撤销保持明确拒绝。
- 受控入口产生的记录通过现有 routing guard 消费；输入拼接的报告、配置 passed 和旧 Python canary
  都不能建立真实工具资格。
- 模型请求数、send_unknown、协议事实和清理结果与 journal 一致。任一必需事实未知都不输出完整 passed。
- 实际 Profile 读取仍可因任务路径、上下文、角色或候选能力不足而 blocked，保留对应原因及历史评估。

本切片将持久入口 Spec、Standards、suite 独立审阅及 CLI 独立审阅四个目录加入无 provider 凭据的
CI，并将 Python 矩阵任务上限设为 30 分钟；已有测试、前端和汇总门不削弱。具体新提交结果须等
远端实际检查，见[测试门禁](../implementation/testing-gates.md)。未完成上述后续范围前，不关闭
M2-05 或把整个来源接入记为完成。

关联：[固定隔离实现](../implementation/m2-opencode-go-isolated.md)、
[M2-05 #21](https://github.com/zhouy1017/Karajan/issues/21)、
[可信路由 #23](https://github.com/zhouy1017/Karajan/issues/23)、
[任务准入 #24](https://github.com/zhouy1017/Karajan/issues/24)。
