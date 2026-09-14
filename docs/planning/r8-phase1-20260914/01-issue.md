# R8-P1-01：外置网关连接与版本化模型绑定目录

## 问题与结果

当前项目配置仍以旧 provider/Profile 为入口，缺少 r8 外置 CLIProxyAPI 的明确连接和模型绑定目录。新增可认证访问、持久化、重启可读回的网关目录；用户可以登记网关连接和固定版本模型绑定，查看未验证/可见/已核验的区别。

## 规格和范围

依据 docs/architecture/08-provider-gateway.md、工作台 PRD FR21/UX-AC15。首 commit 纳入本地已确认的 r8 文档基线，不另开文档 PR。实现新 karajan.gateway 模块及 web/gateways.py 注册到现有 create_app，复用现有 Session/CSRF、ProjectRegistry 与 storage 约定；避免修改旧 #173 的 planning/proposal 业务。

本票提供目录与显式无推理目录探测，不实现推理工具循环、账户登录/管理、自动换源或产品真实来源资格。不得用模型别名或 /models 返回值直接标记 dispatch eligible。

## 验收条件

- [ ] AC1：认证 HTTP 可为已登记项目创建/读取 GatewayConnection 与不可变 ModelBinding revision；校验 gateway/项目归属与引用版本，重启后相同对象和摘要可读回。修改生成新版本，旧引用保留。
- [ ] AC2：写操作有主体绑定的 Idempotency-Key、适用的 expected revision/CAS；同键同载荷返回原结果，同键异载荷或旧版本冲突；未知项目/跨项目绑定拒绝。未认证、错误 Origin/CSRF 保持现有拒绝语义。
- [ ] AC3：连接保存 base URL、协议/版本标识和 secret_ref；凭据由可信 resolver 解析，不接受或返回明文 credential 字段，不在响应/错误/日志中回显 secret。严格绑定固定 gateway revision、模型 alias、声明的 provider/account/计费与变换策略身份；声明身份与核验证据分开。
- [ ] AC4：明确的模型目录探测通过所登记连接获取 catalog，禁止跟随重定向到另一来源，不进行推理或自动 fallback。用本机受控 HTTP 上游证明请求路径与恢复的结果；网络错误/认证失败/未知保持结构化状态，/models 可见不授予执行资格。未配置 secret_ref 的解析器返回明确缺口。
- [ ] AC5：测试覆盖上述 HTTP、持久版本、幂等/CAS、归属、凭据不泄漏和 probe 失败；Ruff/mypy/既有快速门通过。提供可复现 API 样例和限定 C/P 证据，不宣称 S 资格。

## 非目标与兼容

不改现有来源资格或已批准 Run，不批量迁移旧 Profile，不扩展真实 provider 消费。原历史默认值仍属于旧 schema，新目录不新增默认 Agent 数量。

## 交付

分支 codex/r8-p1-01-gateway，PR base dev。实现由 Claude Code CLI 默认 gemini-use worker 完成；只提交本票范围与已备好的设计基线，提交 PR 后不得自行合并。根协调器审查/返修/合并。本批总共最多4PR，返修更新同一PR。

证据按 docs/agents/issue-tracker.md 和 .github/pull_request_template.md。原任务范围全部满足才 Closes 本 Issue；完整 r8、真实模型资格等仅 Refs 父票。PROGRESS.md 作为本机检查点更新，不把临时日志/提示词/凭据提交。

Parent: #174
Refs #1
