# 来源、推断与未决事实

核对日期：2026-09-05。依据为用户已确认目标、本地调研和官方当前文档。本轮没有读取账户凭据、调用模型或运行底座/隔离验收。

## 用户与本地材料

- 已确认：个人单机、需求到 PR、先确认计划、自行合并、Web、允许复用底座、首个需求 2–3 子任务。
- 核心要求：ChatGPT/Claude 订阅、DeepSeek 官方 API、Go 订阅及第三方 API 同项目协作；高级 Commander、较低成本 Worker/Reviewer；矩阵与配额协调。
- [原报告](../../outputs/toil-like-heavy-framework-report.md)记录 Bernstein `3.19.1 / 3596346879c3ea26505273248eaa240aa7342c69`。本轮未取得固定提交材料，也未执行 qualification。
- [原产品草案](../../outputs/karajan-design-blueprint.md)与[原路由草案](../../outputs/karajan-routing-and-quota-design.md)保留访谈过程，当前规范为本目录。

## ai7-harness

只读检查本地仓库，没有 fetch 或切分支。冻结工作目录 HEAD 是 `2b71db36aa99f0f85cd1748748d25192fedeb789`；主要阅读本地 `dev` 的 `fe2c0fecd13e597fc2728a207ff38b88ef0028b3`，不声称远端最新。

当前规则按 Commander harness 选 Codex/Claude 路线，固定绑定且不自动回退。Karajan 借鉴角色契约、T0–T3、有界简报、启动/返回证据及独立审查；不继承整项目绑定 Commander 来源的做法。[固定 runbook](https://github.com/zhouy1017/ai7-harness/blob/fe2c0fecd13e597fc2728a207ff38b88ef0028b3/kick-in/27-repository-development-dispatch.md)、[ADR 0061](https://github.com/zhouy1017/ai7-harness/blob/fe2c0fecd13e597fc2728a207ff38b88ef0028b3/docs/adr/0061-route-repository-dispatch-by-commander-harness.md)

历史 Layer A/B 分离与回退保留任务等级是参考原则；Claude-first 消耗顺序、指定快模型、并发数和手工操作步骤不是 Karajan 默认值。[历史 runbook](https://github.com/zhouy1017/ai7-harness/blob/4746bb15b96cc76afee2b450746c8fb069f3229e/kick-in/27-repository-development-dispatch.md)

## Bernstein

当前普通 plugin hooks 为通知机制，异常记录后丢弃，示例质量插件本身不硬阻断。另有 lifecycle hooks，但未获得所有 Attempt/模型请求必经的完整证明。[Plugin SDK](https://bernstein.readthedocs.io/en/latest/integrations/plugin-sdk/)、[Lifecycle](https://bernstein.readthedocs.io/en/latest/contributing/hooks/)

Per-step routing 存在初选绑定与非 Claude adapter 的参数支持差异；内部 continuation 和委托型 adapter 的可见性另有边界，不能推断严格 Profile 始终不变。[Per-step routing](https://bernstein.readthedocs.io/en/latest/workflows/per-step-routing/)、[Hardening](https://bernstein.readthedocs.io/en/latest/concepts/orchestrator-hardening/)、[Delegation adapters](https://bernstein.readthedocs.io/en/latest/adapters/ADAPTER_GUIDE/)

持久化和 REST 查询存在，但调用方 Attempt 幂等启动、未知进程核对、所有交付入口受控性仍待版本验收。[Persistence](https://bernstein.readthedocs.io/en/latest/architecture/state-persistence/)、[REST](https://bernstein.readthedocs.io/en/latest/reference/openapi-reference/)、[Issue-to-PR](https://bernstein.readthedocs.io/en/latest/orchestration/issue-to-pr/)

**设计推断：**Karajan 拥有唯一业务协调器，Bernstein 满足受控执行契约后采用。未声称无法适配，也未把当前文档事实归于报告 pin。详见 [采用门](05-build-and-validation.md#3-bernstein-采用门)。

## 模型来源

| 来源 | 已核查事实与限制 | 官方依据 |
|---|---|---|
| Codex/ChatGPT | 官方登录与 API key 计费分开；app-server 提供会话、事件、额度读取。用户档位、模型、schema 与隔离能力待验收 | [认证](https://learn.chatgpt.com/docs/auth)、[app-server](https://learn.chatgpt.com/docs/app-server)、[Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) |
| Claude | `-p` 无交互；认证环境可能改变计费；订阅 SDK 政策不能沿用过期结论。原生 Windows/WSL2 的工具隔离范围不同 | [headless](https://code.claude.com/docs/en/headless)、[认证](https://code.claude.com/docs/en/authentication)、[订阅 SDK](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)、[sandbox](https://code.claude.com/docs/en/sandboxing) |
| Claude 配额 | Claude 与 Code 可共享限额，部分 statusline 有窗口数据；纯 headless 稳定额度查询仍待实测 | [Pro/Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)、[statusline](https://code.claude.com/docs/en/statusline) |
| DeepSeek | 有余额查询、账户级并发，工具在客户端执行；兼容 Responses 不代表全部参数/会话可用 | [余额](https://api-docs.deepseek.com/api/get-user-balance/)、[rate limit](https://api-docs.deepseek.com/quick_start/rate_limit/)、[tools](https://api-docs.deepseek.com/guides/tool_calls/)、[Responses](https://api-docs.deepseek.com/guides/responses_api/) |
| Go | 多模型协议、工具标识、多个窗口服务额度、可能存在额外余额路径；公开额度查询能力待确认 | [Go 官方文档](https://opencode.ai/docs/go/) |
| OpenCode runtime | 有 headless server/session/events/abort 和应用权限；管理接口隔离、broker 全覆盖、进程树停止仍需验收 | [server](https://opencode.ai/docs/server/)、[permissions](https://opencode.ai/docs/permissions/) |
| 第三方 | 厂商未指定，协议、模型、池、计费和数据去向逐项登记 | 接入时核对该厂商官方材料 |

程序接受、供应商报告、模型自报分别记录，不把名称等同于模型权重级证明。型号不预先按品牌划为高级/低级，能力组按任务验收维护。

## 技术与默认策略

技术事实来自官方材料，选为 v1 技术组合属于设计建议：[SQLite transactions](https://www.sqlite.org/transactional.html)、[WAL](https://www.sqlite.org/wal.html)、[backup](https://www.sqlite.org/backup.html)、[FastAPI](https://fastapi.tiangolo.com/async/)、[React](https://react.dev/learn/thinking-in-react)。

2026-09-05 审阅 Q1–Q8 确认了核心产品策略；Q9 确认其余工程基线，包括技术栈、独立交付、两个 writer、两轮质量修复、每根任务两次基础设施重试、PR 完成定义和历史恢复后的核对/继续决定。具体状态与适用范围见 [审阅记录](06-review-and-decisions.md)。这是设计确认，不是实际账户或能力测试结果。

所有真实资格案例当前均为 not_run。文档/JSON 校验不代表账户、Agent、现金限制或隔离测试通过。
