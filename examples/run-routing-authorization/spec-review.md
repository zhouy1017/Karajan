# v2 Run 批准的独立 Spec 审阅

独立公共命令检查最终 **23 项通过**。发现并关闭 1 项 P2 输入回归，当前没有
未关闭的 Spec 发现。结果绑定最终冻结源码，详见
[spec-review.json](spec-review.json) 和 [实际 JUnit](spec-final.junit.xml)。

验证覆盖项目、所有者、配置及政策版本正文绑定；原授权对工具、目的地、币种、
金额、Attempt 时长、质量轮数和升级阶段的限制；必需检查和能力不能移除；
任务内容变更必须更换 revision；计划、路由、授权及配置摘要必须精确匹配；
Commander 交接后的旧 term 批准拒绝；重开后原命令回放保持一致。

测试特意让政策或配置比原 Run ceiling 更宽。例如政策允许两种工具，但 Run
仅允许读取工具；配置现金上限为 10/20，但 Run 仅批准 2/3。处于政策范围内的
扩权仍被原 ceiling 拒绝。这些金额只是离线测试数据，没有账户余额或现金调用。

旧数据库迁移使用公共命令先创建并批准 v1 Run，再移除当时为空的新政策表以
还原旧结构。重开后，旧项目表和所有 Run 表逐行不变，v1 创建、提交及批准回执
精确重放，没有补充 v2 权限或路由绑定。

发现的 P2 是新版本分流先调用 `request.get`，使 JSON 根值 `[]` 在创建、提交、
批准三个公共入口都抛出 `AttributeError`。原输入的三项真实失败保存在
[修复前 JUnit](spec-nonobject.before.junit.xml)；修复后三个入口恢复稳定的输入错误，
数据库不变。[原输入](spec-nonobject.input.json) 和可重跑的
[独立用例](spec_cases.py) 均保留。

真实鉴权 HTTP 验证还确认：旧页面发送的 v1 批准请求对 v2 Run 返回
`409 RUN_PROTOCOL_VERSION_MISMATCH`，没有写入批准。当前旧页面尚未展示新增权限，
因此后续 UI 必须明确禁用该按钮，或完整展示 v2 权限和已解析阶段集合后才发送
v2 批准。本审阅不等于该 UI 接线已经完成。

全部检查在明确的隔离 worktree 运行，并断言实际导入源码位于该 worktree 的
`backend`。简洁观测记录见 [spec-observed.json](spec-observed.json)。规划准入使用
测试专用 fixture receipt；没有 provider、凭据或现金 API 操作。本次也不验收完整
路由组装器、执行器工具约束、资源准入或实际启动。
