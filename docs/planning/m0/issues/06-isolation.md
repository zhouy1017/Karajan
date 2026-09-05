# M0-06｜验证选定执行环境的工具、凭据和交付隔离

## 用户价值

Worker 能读写自己的任务代码，但不能获取平台或交付凭据、控制其他执行者，或绕过独立交付入口直接写远端。

## 范围与演示

针对 [M0-04 #5](https://github.com/zhouy1017/Karajan/issues/5)、[M0-05 #6](https://github.com/zhouy1017/Karajan/issues/6) 选定的 Windows/WSL2/必要容器路径，使用假 secret、测试工作区和本地 bare Git remote，验证实际启用的工具访问边界。只覆盖实际候选部署，不建设所有操作系统组合。

- 每 Attempt 使用独立 clone/snapshot；共享 linked worktree 不能作为安全边界。
- 探测平台凭据、Git 写凭据、其他任务目录、junction/symlink、WSL 互操作、环境变量、MCP/hooks 及网络管理端。
- API 执行路径中真实 provider key 只应由 broker 持有；工具不能直达 provider 或管理 API。订阅路径分别说明官方认证存储与原生工具保护。
- 检查产物收集不会以高权限执行工作区的 Git hooks、filters、fsmonitor 或不可信配置。
- 区分操作系统隔离与可信工具守卫；未能观测的原生文件工具路径列为待真实验证。

## 验收标准

- [ ] 已启用且可直接测试的命令/文件/网络工具逐项跑 canary，用外部读写/收包记录说明阻断效果。
- [ ] 允许的任务读写成功；对平台、其他任务、管理端和远端写入的尝试被阻断，不能用“所有工具禁用”替代可用边界。
- [ ] 子进程仍存活时不能释放写占用给同一受保护目标；与 [M0-02 #3](https://github.com/zhouy1017/Karajan/issues/3) 的取消事实一致。
- [ ] 收集候选时恶意 Git 配置/钩子不在交付权限下执行，候选可绑定确定的内容身份。
- [ ] 输出每条执行路径的 passed/failed/not_run/unsupported 与证据；夹具无法触发的真实工具守卫保留 not_run，交给 M0-07。
- [ ] 假凭据不替代真实认证路径的证明；本票使用的 canary 与真实秘密完全分开。

## 验证与边界

不接触生产凭据、不访问外部写入目标、不创建 PR。若某边界无法成立，记录限制和最小替代环境；不能通过删掉必需检查宣布合格。

关联 FR02、FR13、FR19；A13、A14 及 A10 的环境部分。此票的工程探针完成与运行时最终合格分开判断。

## 依赖

Blocked by：[M0-02 #3](https://github.com/zhouy1017/Karajan/issues/3)（生命周期入口）、[M0-04 #5](https://github.com/zhouy1017/Karajan/issues/5)（订阅工具与权限路径）、[M0-05 #6](https://github.com/zhouy1017/Karajan/issues/6)（API 工具和网络路径）。

## Parent

[PRD v1 #1](https://github.com/zhouy1017/Karajan/issues/1)

<!-- karajan:m0:06 -->
