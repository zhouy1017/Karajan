# Commander 工作台交互原型

这是用于讨论布局和工作方式的一次性原型，保存在 `codex/prototype-workbench-20260909` 分支。设计依据为 PR #163 的 Commander 工作台规格；没有接入模型、真实仓库操作或 GitHub 写入，也不是 #159–#162 的产品验收。

## 打开

在 `frontend` 目录运行 `npm run demo -- --port 4178 --strictPort`，打开 [Commander Hub](http://127.0.0.1:4178/prototype/commander?variant=A&scene=hub&stage=proposal)。原型仅在开发模式下启用。

## 讨论入口

- [打开仓库](http://127.0.0.1:4178/prototype/commander?variant=A&scene=open)：仓库与主 Commander 选择是否足够直接。
- [Hub · 调整确认](http://127.0.0.1:4178/prototype/commander?variant=A&scene=hub&stage=proposal)：Commander 建议分工、用户调整及确认。
- [Hub · 运行任务](http://127.0.0.1:4178/prototype/commander?variant=A&scene=hub&stage=running)：主会话旁的任务缩略卡片和进度。
- [Hub · 最终汇报](http://127.0.0.1:4178/prototype/commander?variant=A&scene=hub&stage=complete)：Commander 汇总成果与交付建议。
- [任务分工](http://127.0.0.1:4178/prototype/commander?variant=A&scene=plan)：角色、模型、来源与依赖是否清楚。
- [并行执行](http://127.0.0.1:4178/prototype/commander?variant=A&scene=run)：多个 Agent 的工作、阻塞和结果能否一眼看懂。
- [审查交付](http://127.0.0.1:4178/prototype/commander?variant=A&scene=review)：Diff、检查、审查和 PR 的关系是否直观。

执行详情可比较三种信息布局：A 任务工作台、B 并行看板、C Agent 工作区。顶部进入 Hub 或展开详情，底部切布局。URL 的 stage 是刷新时载入的样例阶段；页面内展开/返回只切换视图，不推进任务。布局不是对三套产品功能的承诺。

## 当前结论

2026-09-09 r4：侧栏以项目分组展示 Commander 会话。打开项目、切换会话及新建均须明确项目归属，各项目保留自己的草稿、任务和反馈状态。样例项目不表示已读取本机的其他仓库。

演示含 Karajan（CSV 运行会话）、Docs Studio（待规划会话）、API Playground（空项目）。点项目名恢复最近会话，点下级会话进入 Hub；项目旁箭头控制折叠，项目内新建会话和 Hub 新任务归属当前项目。打开项目卡片与侧栏使用同一列表。

2026-09-09 用户确认 Commander Hub 为主工作面：Commander 拆分任务并初步分配，用户调整确认后分发；运行任务以缩略卡片常驻主会话，结果由 Commander 汇总，其他页面提供更多细节和操作。原三种运行布局保留为详情视图的比较素材，不再作为日常主入口。具体视觉布局继续按演示反馈调整。

演示数据和回复均为内存样例，页面内导航保留状态，刷新按 URL 重新载入样例。真实持久化、派发和证据汇总仍需产品实现。

r3 讨论内容：各模型状态的实时反馈图标、文字和最近反馈时间，以及会话标题/任务栏的快捷新建入口。演示的反馈来自本地模拟事件，不能证明真实模型仍在运行；新建内容和旧会话仅保存在当前页面内存，刷新后恢复样例。

顶部“反馈演示”提供正常、中断、恢复、失败和静默；正常/恢复每 3 秒模拟反馈，静默超过 30 秒显示过期。会话标题的“＋新对话”保留旧草稿，任务栏的“＋”和 Hub 的“＋新任务”打开录入表单，新任务进入独立待规划会话。

## 已检查的交互

2026-09-09：浏览器实操覆盖 Commander 模型切换、发送样例消息、分工模型和依赖编辑、启动、三种运行布局、暂停/继续派发及日志详情。类型检查和生产构建通过；开发入口未进入生产构建。以上仅验证原型可用于讨论，不作为真实任务执行、模型资格或产品验收证据。

r2：浏览器从旧对话链接进入 Hub，修改首项模型及第二项依赖，确认后停留 Hub；展开分工后控件只读，返回保留选择与状态；模拟完成前置任务后启动依赖项，Worker 完成后启动 Reviewer，结束后回到同一会话的结果汇报。详情导航本身不会产生完成状态。

r3：浏览器核对中断/恢复、静默超过 30 秒的过期反馈、完成项与等待项不被模拟故障改写；新对话空态、旧草稿恢复、新任务录入及空任务详情均已实操。新任务仅待规划，未接入真实 Commander 拆分。

r4：浏览器实测 Karajan 留草稿与中断反馈→Docs Studio 新建待规划任务→回 Karajan 恢复原草稿、三个任务及反馈模式；Docs 的新增会话只出现在其项目下。A/B/C 均提供项目侧栏，打开页提供同一组样例项目卡片。

补验通过：独立折叠不切换项目；空项目先新建后输入；新任务表单显示项目名；在 Karajan 当前选中时，从已展开的 Docs Studio 分组新建会话，新增对象归属 Docs Studio，Karajan 原会话不变。
