# 固定 OpenCode Go 隔离链路

本切片把固定 Linux OpenCode 原生工具、唯一推理通道和持久调用账本接在一起。
它为 [M2-05 #21](https://github.com/zhouy1017/Karajan/issues/21) 补充本机执行和 Go 实测证据。
用户已授权这个 Go 通道的实际测试；其他通道的现金调用仍暂停。

## 执行边界

`IsolatedOpenCode` 只接受固定 `opencode-linux-x64@1.18.29` ELF，其 SHA256 为
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`。
每次运行使用新的 user/mount/PID/network namespace 和 chroot。宿主只读系统依赖、固定二进制、
单个可写 `fixture.py` 及一个 pathname Unix socket 被挂载进去；没有宿主网络路由、Windows 互操作或宿主用户目录。
内侧 `/workspace` 仅含该固定文件，host 工作区的其他文件没有映射进去。这样还消除了原生 edit
工具在权限拒绝前匹配 host 文件内容的旁路；原始失败证据保留在独立审查中。
UDS 必须位于 Linux 原生文件系统，WSL 的 `/mnt/c` 不适合放这个 socket。

原生程序只获得本地临时凭据。真实 Go key 留在外侧 `GoRelay`，固定出口为
`https://opencode.ai/zen/go/v1/chat/completions`，模型固定为 `glm-5.3-flash`。
内侧 HTTP bridge 只转发固定 Chat Completions 路径，没有任意代理或 endpoint 参数。
控制通道使用独立 socketpair，描述符不传给原生子进程。

原生工具仅允许读取、编辑 `/workspace/fixture.py`。OpenCode 的权限匹配规则使用
`workspace/fixture.py`；输入路径仍为 `/workspace/fixture.py`。其余文件、shell、插件、MCP、
外部技能、LSP 和自动更新均未获准。管理端口只接受固定配置读取、受控 session 和固定模型文本请求，
不能通过请求加入 permission、任意 agent、file part 或未签发的 session ID。

## 持久调用与停止

`GoCallJournal` 是受控发送记录，不是账户余额或现金账本。controller 预先保存固定 grant，
relay 生成 call ID，并在真正发送前用 SQLite 事务保存 `send_unknown`。只有首次提交后返回的
`send_allowed=true` 可以发送；重放、重开数据库和更换 relay 均不重置次数。
每个固定诊断 grant 至多六次请求。部分 SSE、响应丢失或完成写入失败保留未知状态，不退款、不重发。

已认证的到期拒绝会持久化，系统时钟回退也不能使许可复活。`snapshot` 和历史 receipt 查询只读。
观察器还要求 grant 未使用、仍有效且绑定一致，不能换一个目录继续消费旧诊断。

`runtime_digest` 是 `go_runtime_source()` 完整描述符的规范化 SHA256，包含实际 ELF、运行脚本、
relay/journal、系统执行文件、内核和脱敏原生配置；它与单独的 `artifact_sha256` 含义不同。
观察器在启动前核对当前描述符，不能借用其他运行配置的 grant。

收尾先撤销新发送权限，再停止本地 runtime 和 relay。正常停止使用 namespace init 的 pidfd，
并核对 PID 与 birth 身份。零参数 `probe_lifecycle()` 只运行固定 setsid 子树诊断，不授予原生 shell。
未观察到 init 的启动失败保留 `local_stop=unknown`。任何本地停止结果都不证明 provider 远端停止。

## 实测入口

先按锁文件安装 runtime，在 Linux/WSL 原生目录中执行；密钥路径必须位于诊断目录之外：

```bash
PYTHONPATH=backend python examples/opencode-go-isolated/run_live.py \
  --live \
  --runtime /path/to/opencode-linux-x64/bin/opencode \
  --credential-file /path/to/opencodego.key.txt \
  --directory /tmp/karajan-go-edit-unique \
  --scenario edit
```

另一个固定场景是 `denied_read`，需要新的目录。没有 `--live` 时不读密钥、不创建目录、不启动程序。
已存在的目录拒绝重跑；`start.json` 和 journal 在运行前持久化，报告只包含脱敏事实。
文件功能检查用受限 AST 解释器核对四个 clamp 用例，不执行模型生成的任意代码。

## 验证与未完成项

作者测试覆盖真实原生 read/edit、六类越界读取拒绝、管理请求限制、FD/网络观察、setsid 子树停止、
启动回执丢失，以及实际 relay/journal 组合。独立审查保留失败原例和修复后的复验。
GitHub Linux CI 安装锁定 ELF，并必需运行这些本机离线检查；上游用 HTTP fixture，无 key。
CI 只在一次性的托管 Linux runner 中允许所需的 user namespace，不修改用户机器的系统配置。

该诊断始终返回 `dispatch_eligible=false`、`runtime_tools_status=not_run`。
`http_fixture` 和 `official_go` 观察来源分开记录。固定文件测试没有证明任意 Task 路径权限、
Reviewer/Commander、候选 Collector、账户计量窗口、现金限制或完整调度已具备。

下一切片需将固定 suite 接入 `ProfileQualificationStore` 的 start/record/revocation，
绑定已批准 Profile、受控 runtime source 和真实 credential generation，再由现有 routing guard 消费。
不能导入本报告的 `passed` 字段来启用 Profile，也不能把诊断中的临时 auth generation 当作账户凭据登记。
