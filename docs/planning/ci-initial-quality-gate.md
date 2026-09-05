# CI｜建立 Linux/Windows 测试与主分支 quality-gate

## 目标

使 Karajan 当前提交的离线检查成为实际 GitHub 合并门：Python 3.12 在 Linux/Windows 通过契约测试、lint、类型检查与锁文件一致性验证，汇总检查 `quality-gate` 必须通过才允许更新主分支。

## 验收标准

- [ ] pull_request、main/codex/** 的 push、merge_group 触发真实 workflow，没有路径过滤或缺测试成功占位。
- [ ] 固定 Actions SHA、uv 版本并提交依赖锁文件；Linux 和 Windows 均执行 pytest、ruff、mypy。
- [ ] quality-gate 对必需 job 的 failure/cancelled/skipped/missing/unknown 均拒绝；真实失败用例证明 gate 不通过，修复后当前提交通过。
- [ ] 主分支规则启用必需 quality-gate，无绕过列表；要求通过 PR 变更并阻止强推/删除。规则由 GitHub 实际配置读回验证。
- [ ] 后续前端加入时纳入锁定安装、类型、测试与生产构建，不静默缺检。
- [ ] 文档记录运行命令、实际远端 run/commit 证据及覆盖限制。

## 边界

只使用离线夹具、本地假服务和测试进程；不向 CI 注入模型凭据。用户当前暂不进行现金 API 调用。CI 绿色不证明真实模型、工具沙箱或整个 PRD 已完成。

## Parent

[PRD v1 #1](https://github.com/zhouy1017/Karajan/issues/1)

## Blocked by

[M0-01 #2](https://github.com/zhouy1017/Karajan/issues/2) 提供第一批真正的契约行为测试；workflow 可以并行准备，验收依赖该测试路径。

<!-- karajan:ci:initial-quality-gate -->
