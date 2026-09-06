# Go Task 的逐次上下文计量与验证政策

本切片为已批准 Task 的执行器准备上下文与验证约束。它接在 [Workspace 与 Task grant](m3-approved-task-workspace.md) 之后：Task grant 通过 Go relay 发送时，必须附带可信协调器解析的计量配置；每一次完整请求都先计量并写入发送账本，再调用上游。实际 Run 启动、可信 Collector、独立验证和 Reviewer 的接线仍待后续实现。

## 固定计量来源

`GoRequestAccounting` 从本地加载官方 GLM-5.3-Flash tokenizer 与 chat template，固定 Hugging Face revision `690b705278a3a58e538fcb37c2ca8b5f9511213c`。构造时检查三个文件的长度、SHA-256 和 `transformers==5.16.1`、`tokenizers==0.23.2`、`Jinja2==3.1.6`。加载不使用 Hub loader、远程 Python 或模型权重。CI 准备阶段可以下载这些公开文件，测试与执行计量阶段不下载。

官方通道记录沿用 GLM-5.3-Flash 的声明容量：上下文 1,000,000，最大输出 131,072。这是来源声明；当前受限 relay 的单次输出请求仍限于 4,096，不能由声明容量推断已验证更大执行范围。固定资料包括 [Go 通道记录](https://github.com/anomalyco/models.dev/blob/98383f3755693d8b173ec1a2ff8bd0ae851ef207/providers/opencode-go/models/glm-5.3-flash.toml)、[模型记录](https://github.com/anomalyco/models.dev/blob/98383f3755693d8b173ec1a2ff8bd0ae851ef207/models/zhipuai/glm-5.3-flash.toml) 和 [官方 tokenizer](https://huggingface.co/zai-org/GLM-5.3-Flash/tree/690b705278a3a58e538fcb37c2ca8b5f9511213c)。

计量方法明确记录为 `reference_tokenizer_estimate`，可信度为 `local_estimate`。服务端隐藏包装、模板或路由可能不同，本地计数不能冒充服务端精确值。安全余量由已批准政策定义，provider 返回的用量另行记录；新的组合仍需要目标环境实际资格验证。

## 每次请求的检查

输入包括最终发送的 system/user/assistant 文本、工具定义、完整工具调用与结果历史，以及保留的 reasoning 字段。调用参数按拒绝重复键的 JSON 对象解析后应用官方模板；输入原文、工具结果和 reasoning 原文不进入计量收据。未知形态、多模态及会丢弃历史的 `clear_thinking=true` 明确拒绝。

令本地模板计数为 L，固定余量为 F，比例为 B 个基点：

- 余量 M = F + ceil(L × B / 10000)。
- 计入的输入 A = L + M，必须不超过已批准输入上限 I。
- 请求输出不超过预留输出 O。
- A + O 不超过当前执行上下文 C；即使本次请求输出更小，仍保留完整 O。

Task grant 缺计量配置、政策摘要不符、来源变化、请求不支持或越界时，不能消耗新的 durable send slot。`GoRelayContext` 只是可信内部端口：未来执行消费者必须从持久批准、Workspace 与 ExecutionPolicy 解析其参数，不能让 native 请求或 HTTP 客户端自报上限，也不能把这个对象当成 Run 启动授权。

## 账本、响应与恢复

`GoCallJournal.begin_call` 在同一发送意图记录中保存严格白名单的 `request_context`，包括请求与来源摘要、计数、余量及上限。记录提交后才允许首次发送。重放相同 call ID 只返回历史；修改或省略已有计量会冲突，不重发、不退款。旧 qualification 收据无新增默认字段。

relay 完整接收并检查 SSE 后才交给 native。Task 请求要求 provider 返回输入与输出用量；输入超过 A、输出超过请求值、缺用量或传输结果不明时，撤销本 grant 的剩余发送并关闭本地通道。多段 usage 保留各字段已报告的最高值，后续较小值不能擦除超限证据。缓存与 reasoning 细分不从总输入/输出中减掉。

发送意图提交后丢返回时，只读查找完整 binding、协调器生成的 call ID 和 request_context，确认归属后撤销该 grant。完成记录写入失败也撤销剩余权限。若数据库不可读取或撤销失败，仅能确认本地通道拒绝新请求；持久未知状态仍需协调器恢复时核对，不能声称远端停止或已退款。错误的 capability 或其他 binding 不得撤销不属于该请求的 grant。

## ExecutionPolicy v2

v2 沿用现有项目政策表、版本身份与 Run 批准摘要，增加计量来源/余量和具体 validation 定义：必需命令的 argv、超时、环境引用，以及独立审查的环境与隔离要求。相同组件 ID/revision 不可静默改变；改变内容须提高相应 revision 并重新批准。v1 的原记录形状与摘要保持兼容。

验证环境记录固定平台、运行时来源摘要、Candidate 副本、禁网、显式环境变量和日志上限。这些是配置要求，尚不代表已观察到相应环境。后续真实检查执行器必须验证实际来源；Reviewer 消费者还须从可信作者链与当前资格确定独立性。本切片没有新建一套 Reviewer 资格，也没有以元数据替代实际检查。

## 已执行验证与后续范围

Windows 公共测试覆盖计量、完整工具历史、I/O/C 拒绝、Task/qualification 兼容和实际本地 HTTP/SQLite 恢复。独立审查复现的账本丢返回与 usage 覆盖问题已修复，并保留原红灯与修复后记录。

WSL 测试使用固定原生 OpenCode、真实 namespace、精确文件投影和 UDS relay；只有上游响应与凭据为本地合成材料。读取 `src/math_ops.py`、修改加法、携带两轮工具结果的三个请求全部计量后发送；超限用量场景在修改前停止。它证明本机离线组合行为，不是官方模型接受或真实项目交付。

下一切片从原 reservation 固定 Profile，复查当前批准、资格、凭据与输入；容量复查排除自己的预留，保留其他 Run 的占用。启动必须覆盖真实 namespace 创建的授权边界，恢复先核对原启动和发送意图。固定 fixture 的旧 Go 实测不能自动升级为 Task scope；Collector、检查、Reviewer、PR 的真实贯通仍未完成。
