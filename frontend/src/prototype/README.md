# Commander 工作台交互原型

这是用于讨论布局和工作方式的一次性原型，保存在 `codex/prototype-workbench-20260909` 分支。设计依据为 PR #163 的 Commander 工作台规格；没有接入模型、真实仓库操作或 GitHub 写入，也不是 #159–#162 的产品验收。

## 打开

在 `frontend` 目录运行 `npm run demo -- --port 4178 --strictPort`，打开 [演示](http://127.0.0.1:4178/prototype/commander?variant=A&scene=run)。原型仅在开发模式下启用。

## 讨论入口

- [打开仓库](http://127.0.0.1:4178/prototype/commander?variant=A&scene=open)：仓库与主 Commander 选择是否足够直接。
- [Commander 对话](http://127.0.0.1:4178/prototype/commander?variant=A&scene=commander)：主会话怎样连接到具体任务。
- [任务分工](http://127.0.0.1:4178/prototype/commander?variant=A&scene=plan)：角色、模型、来源与依赖是否清楚。
- [并行执行](http://127.0.0.1:4178/prototype/commander?variant=A&scene=run)：多个 Agent 的工作、阻塞和结果能否一眼看懂。
- [审查交付](http://127.0.0.1:4178/prototype/commander?variant=A&scene=review)：Diff、检查、审查和 PR 的关系是否直观。

执行场景可比较三种信息布局：A 任务工作台、B 并行看板、C Agent 工作区。顶部切场景，底部切布局；布局不是对三套产品功能的承诺。

## 当前结论

等待用户对演示的反馈，尚未选定最终布局。演示数据和对话回复均为内存中的样例，刷新可恢复初始场景。用户可以直接用“场景 + 布局 + 控件”描述修改，例如“分工页 A 的模型选择保留，执行页用 B 的列布局”。

## 已检查的交互

2026-09-09：浏览器实操覆盖 Commander 模型切换、发送样例消息、分工模型和依赖编辑、启动、三种运行布局、暂停/继续派发及日志详情。类型检查和生产构建通过；开发入口未进入生产构建。以上仅验证原型可用于讨论，不作为真实任务执行、模型资格或产品验收证据。
