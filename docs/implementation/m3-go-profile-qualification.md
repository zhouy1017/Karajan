# Go 固定场景的持久 Profile 观察

本切片把隔离原生 Go 场景接入 `ProfileQualificationStore`。服务端根据当前批准的
Profile、凭据 generation 和固定 suite 产生结果，没有上传报告或填写 `passed` 的入口。
固定文件的 read/edit 观察尚不能满足任意 Task 的路径权限要求。

## 控制器入口

控制器先配置 `CredentialSourceStore` 的本地来源映射、私有存储目录，以及
`FixedGoSuite` 的固定 Linux runtime、工作目录和 `GoCallJournal`。这些路径不属于
资格请求参数。凭据来源以 `(project_id, auth_ref)` 区分，generation 由控制器生成。

```python
store = ProfileQualificationStore(projects, credentials=credentials, go_suite=go_suite)
record = store.qualify_runtime_tools(
    project_id,
    profile_ref,
    principal=owner,
    command_key="qualify-go-1",
    suite_ref={"id": "opencode-go-native-read-edit-linux", "revision": 1},
    validity_seconds=3600,
)
```

Profile 固定为 `opencode-go-isolated` / `1.18.29`、`glm-5.3-flash`、`api_key`，
权限为 `read`、`edit`，原生设置仅包含上述 `suite_ref`。账户、通道、计费路径及完整登记
从当前批准配置读取。测试不会修改 enabled、model_family 或声明的 capabilities。

## 持久顺序和恢复

1. 在项目 owner 事务内保存 start。它包含完整 Profile/账户/通道/仓库、实际 runtime
   来源、credential generation、suite 来源，以及两个场景各自的 Attempt、fence 和 grant ID。
2. 提交项目事务后，精确解析该 generation 的内存凭据。固定 suite 先配置两个预定 grant，
   然后执行 `edit` 和 `denied_read`。每场景最多六次模型请求；这是请求数限制，不是现金上限。
3. suite 对照真实 journal 检查调用与清理事实；异常也按预定 ID 撤销 grant。持久发送意图
   先于上游 HTTP，部分响应、丢失回执及尾部协议不完整不能作为成功调用。
4. 回到项目事务，重新核对批准配置、runtime 和当前 credential generation，再保存不可变结果。
   发生变化时保留失败结果，历史证据不会重写成新来源的通过结果。

同一 owner/command_key 完成后只返回历史记录，包括控制器重启或来源已经撤销的情况。
相同 key 的参数改变会冲突。已有 start 而没有完成记录时保持 unknown，不重新执行，
不生成替代 grant，也不补回已经消耗或结果未知的调用额度。

`get_start(project_id, observation_id, principal=...)` 允许在模型请求前读回 start；客户端
丢失响应、还不知道 observation ID 时，可用 `get_command_start` 和原始 command_key 找回；
`get` 读取完成记录和单独的资格撤销事实。`revoke` 针对已完成的资格记录，不是运行中
场景的取消接口。正常场景结束时的 grant 撤销与资格撤销是不同对象。

## 读取范围

最新 start 按项目、精确 Profile、scope 和 suite 选择。新的同范围 unknown 或失败观察
不能回退到旧的 passed；测试用上游和本地 Python fixture 不覆盖真实 Go 的观察。

| scope | 可读取内容 | 不能推导的结论 |
|---|---|---|
| `local_fixture` | 现有本地固定脚本的 write/check/review | 没有真实模型或工具沙箱资格 |
| `fixed_native_tools_fixture` | 原生 runtime 配合测试上游的固定工具行为 | 不证明官方认证或模型实现能力 |
| `fixed_native_tools` | 真实官方 Go 在固定文件上 read/edit 及拒绝越界读取 | 不证明任意 Task 路径、Collector、Reviewer 或 Commander |
| `runtime_tools` | 沿用现有路由 guard 的完整任务资格入口 | 固定文件观察仍返回 `TASK_PERMISSION_SCOPE_NOT_QUALIFIED` |

固定观察只输出 `fixed_go_fixture_read/edit/denied_read`，不输出通用 `controlled_tools`、
`bounded_code_edit` 或 `candidate_capture`。上下文容量保留 null，现金约束保留 unknown，
`dispatch_eligible`、`live_qualified` 均为 false。只有测试上游的源仍返回
`RUNTIME_TOOLS_NOT_QUALIFIED`，不会尝试把它当作官方 Go 来源。

## 来源失效

项目数据库保存公开 generation 和撤销事实；私有凭据存储保存材料核对所需的私有数据。
WSL 的私有目录须位于原生 Linux 文件系统，并由控制器管理该目录及父路径；不使用 DrvFS
权限位假定 Windows 文件已经私有。Windows 路径则核对实际 owner/admin/SYSTEM DACL。
凭据内容变化即使保留文件名和 mtime，也不能继续使用旧 generation。运行时仅把解析后的
内存凭据交给可信 suite/relay，原生 OpenCode 不接收 provider key。

SQLite 事务只锁住项目登记事实。它不锁住宿主文件，也无法撤回已经返回的 Python 字符串
或已经到达服务商的请求。真实解析、suite 执行前后和事实读取时分别核对来源；轮换或撤销
使旧事实不能进入后续路由，不宣称能远程取消正在进行的请求。

关联：[固定隔离链路](m2-opencode-go-isolated.md)、
[Profile 资格存储](m3-profile-qualification.md)、
[下一切片安排](../planning/go-runtime-qualification-next.md)。
