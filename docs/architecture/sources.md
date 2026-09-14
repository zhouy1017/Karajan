# 来源、推断与未决事实

记录整理日期：2026-09-14。下文外部事实分别核对于 2026-09-05、2026-09-09 或固定提交，并非今天对所有上游的重新验证；非固定 URL 也不表示其内容持续不变。本文件区分用户决定、带日期的研究事实与待验收设计，不维护实时实现状态。文档整理未读取凭据、调用模型或运行隔离/来源验收。

## 2026-09-14 r8 角色调度与并行规模

用户修正：Workflow 内具备权限的调度器、Commander 或自定义角色应按实际任务决定拆分和调度，Karajan 不应硬编码前后端分工或 coding Agent 数量上限。对应 [ADR 0008](../adr/0008-role-directed-scheduling.md) 与 [11 角色调度契约](11-role-directed-scheduling.md)。旧两 writer 初值被撤销，原两任务样例保留历史验收责任。SchedulerGrant、授权内动态图修订和资源背压是本项目的设计承接；本轮未做吞吐测试或取得新来源资格。

## 2026-09-14 r7 对话创作与真实部署

用户进一步明确：Workflow 应通过对话 Agent 接收文字要求并撰写配置，以流程图、表格协助确认修改，然后真实部署。这是本项目的产品要求，由 [ADR 0007](../adr/0007-conversational-workflow-deployment.md) 与 [配置部署契约](10-conversational-workflow-deployment.md) 承接。配置包、编译投影、精确确认、pending 加载读回再 active、模板与 Run 摘要、创作限额和恢复语义均为 Karajan 设计决定，不是 CLIProxyAPI 提供的工作流能力。

## 2026-09-14 r6 设计变更

用户提出两项修改：外置 CLIProxyAPI 作为统一 provider 网关；角色分工、职责和 Workflow 不得硬编码。对应 [ADR 0005](../adr/0005-external-model-gateway.md)、[ADR 0006](../adr/0006-configurable-roles-and-workflows.md)；角色/流程对象和接入边界是本项目设计决定，不是上游产品现成能力。

只读核查 CLIProxyAPI [固定提交 7fa443d](https://github.com/router-for-me/CLIProxyAPI/commit/7fa443dc8bf8ca2f1ffd81c2472deb31b097b697)：[README](https://github.com/router-for-me/CLIProxyAPI/blob/7fa443dc8bf8ca2f1ffd81c2472deb31b097b697/README.md)、[配置](https://github.com/router-for-me/CLIProxyAPI/blob/7fa443dc8bf8ca2f1ffd81c2472deb31b097b697/config.example.yaml)、[HTTP 路由](https://github.com/router-for-me/CLIProxyAPI/blob/7fa443dc8bf8ca2f1ffd81c2472deb31b097b697/internal/api/server_routes.go) 证实其提供兼容模型调用和可配置多来源。严格 HTTP 账户绑定、请求变换、逐次重试/来源收据、持久消费与远端取消仍须按选定部署验证；已知差异和永久源码链接集中维护于 [网关事实与推断](08-provider-gateway.md#6-外部事实与设计推断)。本次仅修改设计文档，未安装、登录或调用该网关。

## 用户与本地材料

- 原始基线（2026-09-05/09）：个人单机、需求到 PR、先确认计划、自行合并、Web、允许复用底座、首个需求 2–3 子任务；持久 Commander 会话负责建议，协调器机械调度。r6/r7 将代码到 PR 明确为模板，增加自定义角色、报告/补丁目标和对话式配置部署；不要求每个 Run 先调用 Commander。
- 原始来源目标：ChatGPT/Claude 订阅、DeepSeek 官方 API、Go 订阅及第三方 API 同项目协作；分工、能力与配额协调继续适用。具体型号、来源集合和角色绑定属于配置，不由早期三角色示例固定。
- [原报告](../../outputs/toil-like-heavy-framework-report.md)记录 Bernstein `3.19.1 / 3596346879c3ea26505273248eaa240aa7342c69`。本轮未取得固定提交材料，也未执行 qualification。
- [原产品草案](../../outputs/karajan-design-blueprint.md)与[原路由草案](../../outputs/karajan-routing-and-quota-design.md)保留访谈过程，按[历史调研导航](../../outputs/README.md)使用；当前规范为本目录。

## ai7-harness

只读检查本地仓库，没有 fetch 或切分支。冻结工作目录 HEAD 是 `2b71db36aa99f0f85cd1748748d25192fedeb789`；主要阅读本地 `dev` 的 `fe2c0fecd13e597fc2728a207ff38b88ef0028b3`，不声称远端最新。

原核对版本的规则按 Commander harness 选 Codex/Claude 路线，固定绑定且不自动回退。Karajan 借鉴角色契约、T0–T3、有界简报、启动/返回证据及独立审查；不继承整项目绑定 Commander 来源的做法。[固定 runbook](https://github.com/zhouy1017/ai7-harness/blob/fe2c0fecd13e597fc2728a207ff38b88ef0028b3/kick-in/27-repository-development-dispatch.md)、[ADR 0061](https://github.com/zhouy1017/ai7-harness/blob/fe2c0fecd13e597fc2728a207ff38b88ef0028b3/docs/adr/0061-route-repository-dispatch-by-commander-harness.md)

历史 Layer A/B 分离与回退保留任务等级是参考原则；Claude-first 消耗顺序、指定快模型、并发数和手工操作步骤不是 Karajan 默认值。[历史 runbook](https://github.com/zhouy1017/ai7-harness/blob/4746bb15b96cc76afee2b450746c8fb069f3229e/kick-in/27-repository-development-dispatch.md)

## Bernstein

2026-09-09 实际参考了 [Screens](https://bernstein.readthedocs.io/en/latest/gui/screens/) 的 Tasks、Agents 和详情抽屉信息架构；Karajan 借用其可观察工作台表面，不继承其任务状态或调度所有权。Toil 式工作方式参考 [toil-offloading skill](https://github.com/zhouy1017/toil/blob/main/src/toil/_assets/skills/toil-offloading/SKILL.md)；本项目中的 Commander 角色与受控协调器边界仍是本规格决定。

原核对时普通 plugin hooks 为通知机制，异常记录后丢弃，示例质量插件本身不硬阻断。另有 lifecycle hooks，但未获得所有 Attempt/模型请求必经的完整证明。[Plugin SDK](https://bernstein.readthedocs.io/en/latest/integrations/plugin-sdk/)、[Lifecycle](https://bernstein.readthedocs.io/en/latest/contributing/hooks/)

Per-step routing 存在初选绑定与非 Claude adapter 的参数支持差异；内部 continuation 和委托型 adapter 的可见性另有边界，不能推断严格 Profile 始终不变。[Per-step routing](https://bernstein.readthedocs.io/en/latest/workflows/per-step-routing/)、[Hardening](https://bernstein.readthedocs.io/en/latest/concepts/orchestrator-hardening/)、[Delegation adapters](https://bernstein.readthedocs.io/en/latest/adapters/ADAPTER_GUIDE/)

持久化和 REST 查询存在，但调用方 Attempt 幂等启动、未知进程核对、所有交付入口受控性仍待版本验收。[Persistence](https://bernstein.readthedocs.io/en/latest/architecture/state-persistence/)、[REST](https://bernstein.readthedocs.io/en/latest/reference/openapi-reference/)、[Issue-to-PR](https://bernstein.readthedocs.io/en/latest/orchestration/issue-to-pr/)

**设计推断：**Bernstein 可在通过受控执行契约后复用为 UI、会话或 runtime 底座；它不构成第二个模型主控，也不拥有独立任务图、路由或交付权。未声称无法适配，也未把当前文档事实归于报告 pin。详见 [采用门](05-build-and-validation.md#3-bernstein-采用门)。

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

原始架构审阅时尚未进行真实资格验收；之后的来源实测及其通过、失败和范围见 [实现证据导航](../implementation/README.md) 与对应 Issue/候选。不得继续用早期“全部 not_run”覆盖后来的事实，也不能把某个固定场景通过升级为网关或完整角色资格。文档/JSON 校验不代表账户、Agent、现金限制或隔离测试通过。
