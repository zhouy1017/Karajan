# Rulebook 发布后端 Spec 审查

结论：本轮后端持久化范围没有未闭合的确认问题。对照 M3-01、架构中的版本化与授权约束、
`m3-rulebook-publication-plan.md` 的项目发布步骤，核对两个写入口的不可变身份、当前有效目录、
owner、TTL、CAS、服务器预览绑定、旧库迁移与保存/发布语义。未扩审新前端或 Run 启动 guard。

两项原反例均已在冻结源上独立复验通过，输入与 before 未改写：

| 固定输入 | 原问题 | 修后实际观察 |
|---|---|---|
| `spec-legacy-effective-catalog.input.json` | 最新草稿引用损坏，迁移只读最新预览，丢失此前有效撤销目录 | 从已接受历史恢复最近有效目录；Profile 仍停用、批准集合仍为空 |
| `spec-invalid-budget-catalog.input.json` | 保存 `NaN` 预算草稿时，当前目录被覆盖并重新启用已撤销 Profile | 草稿仍可保存；当前目录 revision、内容和撤销状态完全不变 |

迁移输入包含受控旧版三表及真实公开命令产生的配置历史。重放在新建的本机 SQLite 中执行；
有效目录核对走公开 `ProjectRegistry` 方法。所有配置均为合成事实，没有真实凭据、模型或现金请求。

独立验证：两份固定输入 passed；`tests/projects` **107 passed，24.07 秒**；重放入口 Ruff 通过。
作者报告的 projects+routing 185 项不计作本轮独立执行。源码、输入及报告指纹见 `spec-review-index.json`。
关键冻结源码：

- `registry.py`：`aca0573465ce00350547790b3336ea18be64d7b947128d87aff7308d94827458`
- `configuration.py`：`3dd388edeea88bd74292725b0680e8f47c46e9a07023215dd8c67273df489b03`
- `publication.py`：`b57cd2ef51738b1f9871b55670a0c7b3e434bc6b25ee2c5b3eebc34b0516124a`

在仓库根目录重放，输出路径必须尚不存在；另一个 `--case` 为 `invalid-budget-catalog`：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
.venv/Scripts/python.exe examples/rulebook-publication/review-fixes/spec_replay.py --case legacy-effective-catalog --output .cache/publication-spec-review/migration-replayed.json
```

发布仍返回 `waiting_qualification`，不授予真实派发权限。Run 采用、当前限制与启动排序、完整工作台和真实来源资格
不由本轮后端审查替代；本结论不能单独关闭整个 M3-01。提交及远端 CI 由主任务另行绑定。
