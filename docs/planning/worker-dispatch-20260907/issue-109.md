Parent: [#93 真实 Commander 规划桥](https://github.com/zhouy1017/Karajan/issues/93)。

执行分派：gpt-5.6-luna；CI/审查修复仍由 luna；独立 reviewer：gpt-6-astra，high。基线 dev `6cf89abc1a6ca8cb99d76f82191dc7d82efeb05b`。本票是 #93 A 的内容解析原语，可独立 C 验收；不会产生规划资格或完成父票。

## 输入、行为和文件边界

新增 `backend/karajan/runs/planning_output.py`，公开纯函数 `parse_planning_output(content: str | bytes, *, version: Literal["v1", "v2"]) -> Plan | PlanV2`。版本来自受信 controller 参数；模型只给完整 JSON Plan 内容（summary / authorization / tasks），使用现有 Plan/PlanV2 严格契约，不建立第二套计划字段。可以采用等价类型返回值，但须先与 terra consumer 对齐。

只允许新增该模块、专属 tests/runs/test_planning_output.py 与 docs/implementation/planning-output-parser.md。不修改 RunPlanner、既有 models/routing_authorization、资格、资源、HTTP 或 CI。纯函数无文件、DB、进程、网络和提交效果。

## 验收

- [ ] 完整 v1/v2 合法内容稳定解析；语义相同的键顺序/空白不改变结果；保留文本、任务及金额原字符串，不猜修复缺字段。
- [ ] 拒绝重复 key（任意嵌套）、非有限数、类型强制转换、非对象/多个 JSON/代码围栏/尾部文本/截断、未知字段、坏 UTF-8/代理字符；异常仅稳定 reason code，不回显原始模型文本。
- [ ] 固定并记录字节上限（建议 262144）与 JSON 深度上限（建议 16），解码前检查；无隐藏裁剪。包含实际边界反例。
- [ ] 模型输出含 run/intent/term/principal/receipt/admitted/authority/source/digest 等可信身份或批准材料时拒绝；不能产生 Receipt、资格、Run 提交或 owner 批准。
- [ ] 复用现有领域 schema；路径/DAG/授权 ceiling 的最终业务校验仍由 RunPlanner 执行，文档明确 parser 成功不是授权或完整计划验收。
- [ ] 对应公共 parser 行为 pytest、ruff、mypy 通过；保留固定 commit 的 C 证据，当前 PR CI 与 GPT-6 high 独立 Standards/Spec 通过后标 awaiting-merge。

## 剩余归属与依赖

已完成：无。
剩余工作：本票全部验收。#93 的 terra 执行 consumer 负责直接消费此接口并绑定原模型 artifact；transport/官方来源资格/owner 批准/下游 Worker 整链留给 #93 其他子票。
阻塞：无；可与 terra 持久规划执行并行，最终 consumer 组合验收依赖本票。合入 dev 前保持 Open，不因 parser C passed 关闭 #93。