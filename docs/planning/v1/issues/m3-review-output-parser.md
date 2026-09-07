# 实现切片｜模型审查输出的严格解析与 verdict 映射

Native parent: [#95 只读 Reviewer](https://github.com/zhouy1017/Karajan/issues/95)。本票交付供应商无关的纯 C Module：将一条最终 assistant 文本解析为受限审查内容。它承担 #95 的结构化 verdict/findings 校验部分，不替代角色资格、受控执行、可信 Evidence 编译或 S 验收。

建议标签：`kind:task`、`ready-for-agent`、`status:ready`。此纯解析切片不依赖 #93 真实规划、#100 绑定或 #101 subject 切换先完成；真实 Reviewer consumer 在接收模型结论前依赖本票。新实现分支从届时最新 `dev` 创建，当前只是可发布规格，尚无实现/测试/远端验收完成项。

## 依据与已有规则

设计核对版本：`d314b8d545e7862e608ac1d782507f568b30d67d`。

- `docs/architecture/04-api-and-workbench.md:28–30` 将模型 wire verdict 定义为 `pass / changes_requested / inconclusive`，模型输出不携带可执行授权。
- `backend/karajan/candidates/models.py:11–12,139–155` 已有 `Contract(extra="forbid", strict=True)`、`Finding` 和存储 `ReviewResult`。存储 verdict 是 `passed / failed / inconclusive`；`ReviewResult` 还含控制器才能提供的身份、版本与 Evidence 字段。
- `backend/karajan/candidates/store.py:800–874` 的 `record_review` 已核对 Candidate/Policy/Check Evidence/Actor/独立性；blocking finding 会使存储结果失败，缺日志或绑定错误不能 passed。此票不复制或削弱这些规则，也不修改已存 JSON。
- `backend/karajan/orchestration/serial.py:954–987` 是旧 `synthetic=true` fixture 日志分支，接收的是存储式 verdict，且固定编译空 findings。它不是生产模型文本 parser；本票不把它当真实通路，也不改写其历史协议。
- `adapters/opencode/go_relay.py:58–72`、`adapters/deepseek/protocol.py:156–182`、`adapters/claude/replay.py:18–41` 有各自私有 JSON 校验。复用它们已验证的 duplicate-key/非有限数/UTF-8 处理思路，不能从通用审查 Module 导入供应商私有 parser 或借机改造这些协议。

当前缺少独立的生产审查文本 parser。最小实现共享既有 `Finding`/`Contract`，把文本、限制与引用校验集中在一个 Interface 后，不新增另一套存储/调度/资格状态机。

## 最小 Interface

建议文件：`backend/karajan/candidates/review_output.py`。

```python
def parse_review_output(
    content: str | bytes,
    *,
    allowed_files: frozenset[str],
    allowed_acceptance_refs: frozenset[str],
) -> ParsedReviewOutput: ...
```

`ParsedReviewOutput` 是仅含以下两字段的严格内容 DTO，复用 `Finding`，不继承 `EvidenceInput` 或完整 `ReviewResult`：

```python
verdict: Literal["passed", "failed", "inconclusive"]
findings: list[Finding]
```

DTO 的 `model_dump()` 仅输出 `verdict/findings`，不含任何 `passed_gate`、Candidate、Actor、来源或权限字段。每次解析返回新内容，保持 finding 顺序和文本，不静默去重、截断、清洗或补全缺项。结果可供未来控制器编译 `ReviewResult` 的两个内容字段，不能直接当 Evidence。

输入必须是控制器从已完成消息中选出的最终 assistant 文本；此纯函数没有消息来源或完成状态，不能证明它实际已完成。完成/角色/停止/截断判断由后续可信 observer 负责。不能用模型提交的 `completed=true` 或一个 caller 状态布尔值替代该事实；这些字段在 wire 中属于未知字段而被拒绝。本票不解析 SSE、工具事件、日志中的任意 JSON 段或多条消息。

两个 allowlist 都由控制器从冻结 Review 输入包及原批准验收材料编译后传入，不从模型文本、用户任意路径或 ReviewResult JSON 中取得。它们是纯输入，不要求读取 Run/CAS/磁盘。空集合表示没有可引用项，不能解释为不限制；零 finding 的结论仍按 verdict 解析，但不因此证明已取得审查权限。

### Wire 与存储明确映射

wire 顶层恰好两个必需字段，禁止 schema/version/role/summary 等额外字段：

```json
{
  "verdict": "changes_requested",
  "findings": [
    {
      "severity": "high",
      "file": "src/export.py",
      "line": 27,
      "behavior": "空结果没有输出批准的表头",
      "trigger": "查询没有匹配行时调用导出",
      "acceptance_ref": "acceptance:csv-v1",
      "blocking": true
    }
  ]
}
```

此例以 `allowed_files=frozenset({"src/export.py"})`、`allowed_acceptance_refs=frozenset({"acceptance:csv-v1"})` 解析，输出同一 findings，`verdict="failed"`。

| 模型 wire 值 | 解析后内容值，供存储使用 | 限制 |
|---|---|---|
| `pass` | `passed` | 任一 `blocking=true` 时拒绝整个输出，不静默丢弃 finding 或改判 |
| `changes_requested` | `failed` | 即使 findings 为空也保持失败，不能当通过；不额外虚构 finding |
| `inconclusive` | `inconclusive` | 不提升为通过；后续存储还可能因 blocking/失效等更严格规则变为其他非 passed 状态 |

不接受 wire 的 `passed`/`failed` 别名，不大小写转换。`pass` 可以带非阻断 findings；severity 不另推导 blocking，保留既有字段语义。存储 `ReviewResult` 的枚举、JSON、digest、历史 replay 及旧 fixture 语义保持原样。

## 严格解析与明确默认限制

本票采用固定、可测试的 parser revision `karajan.review-output-parser.v1`，作为实现常量，由后续控制器来源摘要覆盖；不让模型自报 revision，也不暴露任意放宽限制的参数。以下属于本地输入防护上限，不代表模型 context/output 资格、批准运行预算或新的业务权限。

| 项目 | 本票默认上限/规则 | 理由 |
|---|---|---|
| 原始文本 | UTF-8 最多 65,536 bytes；`str` 用严格 UTF-8 编码计量，`bytes` 严格解码；其他类型拒绝 | 给多条结构化反馈留余量，同时限制内存与解析工作量；实际模型输出预算仍另受原约束 |
| finding 数量 | 0–32，33 条拒绝整体，不取前 32 条 | 小功能审查足够表达多个问题，防止截断掉阻断项 |
| JSON 容器深度 | 最多 3 层，顶层 object=1、findings array=2、finding object=3 | 合法协议恰为这三层，不需要递归对象 |
| `behavior` / `trigger` | 各 1–2,048 个 Unicode code point，不能全空白；保留原文本 | 约束自由文本长度，避免复制整文件；不改变既有存储 Finding 的宽松历史上限 |
| `file` | 复用既有 Candidate 纯 `relative_path` 规则与 4,096 字符上限，再精确匹配 allowlist | 同一相对路径语义，拒绝绝对路径、`..`、反斜杠、glob、`.git` 等非法表示 |
| `acceptance_ref` | 复用 `Finding` 的 `Identifier`，最多 256 字符，再精确匹配 allowlist | 不另建验收 ID 语法 |
| `line` | 严格整数 `1..2_147_483_647`，拒绝 bool、float、数字字符串、0/负值 | 有界位置表示，防止隐式类型转换；不冒称已核对实际 CAS 行数 |
| `blocking` | 只接受 JSON `true/false` | 不把 `0/1/"false"/null` 当布尔 |

上限只约束新的 wire parser，不收紧 `Finding` 或 `ReviewResult` 的既存 schema。允许多行 behavior/trigger 中的换行、回车和制表符；拒绝 NUL 和其他 C0 控制字符。Unicode 合法文本按原码点保留，不做路径大小写、Unicode normalization、URL decode、trim 后匹配或模糊验收 ID 匹配。

实现应遵守以下顺序与处理规则：

1. 验证两个 controller allowlist 的容器与元素类型及引用语法；`allowed_files` 有大小写折叠碰撞时拒绝 scope，防止跨平台别名歧义。allowlist 不接受字符串当迭代容器，也不自动扩展目录或 glob。
2. 拒绝非 `str/bytes`、非法 UTF-8、超长输入。只接受完整 JSON object；前后 JSON 空白可以存在，BOM、Markdown code fence、解释前后缀、拼接对象或截断内容拒绝，不尝试修复或提取其中一段。
3. 在进入可能递归构建对象的解析前，做引号/转义感知的深度限制检查；它只计数容器，语法正确性仍交给 JSON decoder。字符串内的括号不算容器，不能用不理解字符串的字符计数器。任意层级重复 key 都拒绝，包括转义后相同的 key。
4. JSON decoder 拒绝 `NaN/Infinity/-Infinity` 与指数溢出得到的非有限值。JSON 解码后的所有字符串也必须可严格编码为 UTF-8，以拒绝转义形成的孤立 surrogate。类型/形状异常、深层错误、数值转换上限或 `RecursionError` 统一转为安全领域错误，不外泄解释器异常。
5. 顶层只允许必需的 `verdict/findings`；先核对字段完整性和精确类型，再对正确类型的字段检查本票上限，随后检查引用语法，最后复用 `Finding.model_validate` 的严格字段/enum/bool/int 校验与非空白要求。缺字段、未知字段、null、dict 代 list、list 代 finding、嵌套对象代字符串等拒绝，不默认补 `blocking=false`。不要让 `Identifier` 的提前校验抹去下一节规定的超限或非法引用分类。
6. 每个 `file` 与 `acceptance_ref` 必须与已验证 allowlist 精确相等。路径合法但不在集合中也拒绝；包含某父目录或相似大小写都不扩大引用范围。此处只检查引用，不打开文件、不评判 finding 内容是否真实。
7. 检查 `pass + blocking` 矛盾，再执行明确 verdict 映射。任何失败都拒绝整条输出，不能返回部分 findings 或成功 verdict。

### 稳定错误合同

公开 `ReviewOutputError(ValueError)`，只含稳定 `.code` 与固定安全消息；`str/repr/args` 不包含原文本、模型提供的 key/value、文件名、验收 ID 或 decoder/Pydantic 原错误。不可直接向日志或返回值拼接原异常/validation details。原始模型输出的受控留存另由未来 observer/日志策略负责。

| code | 情况 |
|---|---|
| `REVIEW_OUTPUT_SCOPE_INVALID` | controller allowlist 类型、非法引用或大小写碰撞 |
| `REVIEW_OUTPUT_INPUT_INVALID` | content 类型、UTF-8、孤立 surrogate 非法 |
| `REVIEW_OUTPUT_LIMIT_EXCEEDED` | 文本、容器深度、finding 数或字段上限超限 |
| `REVIEW_OUTPUT_JSON_INVALID` | JSON 语法、重复 key、非有限数、BOM/围栏/尾随内容等 |
| `REVIEW_OUTPUT_SCHEMA_INVALID` | 缺失/未知字段、错误类型/枚举、空白描述、非法控制字符、非正 line 等 |
| `REVIEW_OUTPUT_REFERENCE_DENIED` | 模型文件或验收引用非法或未被 controller allowlist 允许 |
| `REVIEW_OUTPUT_VERDICT_CONFLICT` | `pass` 同时含任一 blocking finding |

测试以单一失效条件确认具体 code；多项同时非法时按上述处理阶段确定先出现的拒绝，不要求泄露所有错误。finding 缺字段或字段类型错误归 `REVIEW_OUTPUT_SCHEMA_INVALID`；类型正确但超过长度/数值上限先归 `REVIEW_OUTPUT_LIMIT_EXCEEDED`；限额内的 file/acceptance_ref 引用语法或成员校验失败归 `REVIEW_OUTPUT_REFERENCE_DENIED`。这包括验收 ID 含空白或路径含 `..`；不能直接将 Pydantic 的所有错误统归 schema。controller allowlist 自身无论类型、长度或引用语法错误，一律归 `REVIEW_OUTPUT_SCOPE_INVALID`。重复 key 与非有限数属于 JSON 阶段，不能先用宽松加载静默覆盖再交给 schema。

## 实现范围与复用方式

- 新增 `backend/karajan/candidates/review_output.py`：一个纯解析 Interface、受限内容 DTO、错误类型及有界私有解析帮助函数。直接复用现有 `Finding`/`Contract`；既有 `relative_path` 是纯函数，可以只调用它而不构造 `CandidateStore`。
- 新增 `tests/candidates/test_review_output.py`：只从公开 parser Interface 验证行为，复用现有存储测试资料验证 wire→存储枚举兼容。
- 增加简短实施说明 `docs/implementation/reviewer-output-parser.md`，并在原模型 wire 文档补明确映射链接/说明，不改变原协议枚举。如采用独立 JSON 帮助 Module，必须是此 parser 所需的有界实现，不发起供应商协议公共重构。
- 不修改 CandidateStore 的 `record_review`/gate 判定，不改旧序列化内容；不修改旧 synthetic fixture parser、Review 持久 intent、routing、资格 Store、Host、relay、UI/HTTP、CI 或依赖。

不需要新数据库、registry、异步回调、消息框架或环境/provider factory。纯 Interface 仅消费给定文本与引用集合，并返回内容或安全错误。

## 验收标准

- [ ] **C：先红后绿与合法映射。** 公开新入口不存在/未实现时保留真实红输入；三个合法 verdict 精确映射，Unicode 与多条完整 findings 保序，零 finding 和 pass 的非阻断 finding 可解析；wire `passed/failed`、未知 verdict、缺 verdict/findings 拒绝。输出仅两内容字段，无可信身份或 gate。
- [ ] **C：UTF-8/完整 JSON。** raw 非法 UTF-8、Python/JSON 孤立 surrogate、BOM、空/截断/拼接文本、围栏与前后解释拒绝；正常 Unicode、转义、字符串中的括号及前后 JSON 空白正控通过。深度 4 与极深异常在同一公开入口安全拒绝，不能漏出 RecursionError。
- [ ] **C：歧义与非法数。** 顶层和 finding 内重复 key、转义后同 key、NaN/±Infinity/指数溢出拒绝，不能以最后一个值覆盖原 blocking/verdict；未知字段和嵌套异常不能被忽略。
- [ ] **C：严格 Finding。** 七个既有字段均必需；severity 枚举、`line` 与 `blocking` 的严格类型覆盖 bool/int、float/int、字符串/布尔、null、非正 line；behavior/trigger 全空白和非法控制字符拒绝。不另建与 `Finding` 漂移的一套字段规则。
- [ ] **C：大小限制。** UTF-8 bytes（含多字节文本）、32/33 findings、2,048/2,049 描述字符、line 上界和深度边缘有可复跑正反例；验证长度指编码 bytes、字段指 decoded code point。拒绝整体，不截断后形成通过。
- [ ] **C：引用 scope。** 精确授权文件/验收引用正控通过；目录前缀、glob、相似大小写、越界路径、绝对/反斜杠/父级/.git、缺少验收 ID、模型自带 allowlist 和 controller scope 非法拒绝。空 allowlist 不放行任何 finding；测试不需要真实文件存在，也不做磁盘查询。
- [ ] **C：结论不能覆盖问题。** `pass + blocking=true` 在任意 finding 位置均拒绝，32 条边界中的末项也保留检查；`changes_requested` 永远映射 failed，`inconclusive` 永远不映射 passed。仍运行既有 `tests/candidates/test_validation.py` 中 Review 结论/日志/绑定/独立性相关公共回归，证明没有改变存储 gate。
- [ ] **C：可信绑定排除与错误脱敏。** wire 尝试加入 actor、candidate_id、evidence_key、check_evidence_ids、qualification、provenance、author_reasoning_included、completed、任何 digest/权限字段均拒绝；在任意深度放置合成敏感 canary 后制造失败，公开异常与输出不回显它。没有部分成功结果，也无 stdout/log 泄露。
- [ ] **C：纯函数与兼容。** 相同内容/scope 得同一内容结果，不修改输入或共享可变状态；明确禁止网络、时钟、文件/数据库写入及 store 构造的测试仍可通过。现有 `ReviewResult` 存储枚举/JSON 与历史 fixture 原样；用既有受控 fixture 的可信字段组装 DTO，确认 parser 仅提供 verdict/findings，不能单独构成可提交 ReviewResult。本票的测试装配不冒充生产 consumer。
- [ ] **G：交付。** 固定实现 commit、公开输入/原失败/最终结果、影响范围回归、独立 Standards/Spec 和当前候选必需 CI 齐备；合入 dev 后只按本纯 C 子票核验。合并由 owner 决定，不因本票关闭而关闭 #95/#13/#14 或宣称真实 Reviewer 已获资格。

## 后续角色 suite 与生产 consumer 的明确责任

1. 角色 suite 必须观察实际只读 Reviewer 会话/输入/输出、当前 source/Profile/generation、新 context、隔离、预算及停止，再用本 parser 校验其最终完成文本。脚本返回合法 JSON 或 parser 单测通过只证明 C，不能赋予 `code_review`、`structured_findings` 或 `runtime_tools` 真实资格。
2. 生产 consumer 从当前批准 Reviewer Task、validation subject/CAS、全部作者、全部最终 Check Evidence 编译 allowlist；只把已完成 assistant 的文本传给 parser。部分流、工具返回、取消/超时/输出截断或完成状态 unknown 不得选择一段看似合法 JSON 变成通过。未完成观察的处理不以模型自报字段为依据。
3. parser 成功后，控制器自行提供真实 Actor/Attempt/context、Candidate/Evidence/Policy/environment/review revision/Check IDs、来源及日志身份，再复用 `record_review` 与当前 gate。来源资格、独立性、授权、版本失效、日志可用性和 evidence 丢响应恢复继续由现有/后续可信模块负责。parser 映射到 `passed` 只是内容值，绝不是 gate 成功。
4. 对没有合格 Reviewer、缺真实 planning 来源或无实际服务观测的情况，父票继续 blocked/not_run；本票不提供这些输入的 fixture-only 生产开关。实际 role suite、consumer C/P、正确/缺陷 Candidate 的 S 以及真实批准 Task S 分别保留父票原责任。

## 当前进度

- **已完成：** 规格与现有定义核对；尚无此子票实现进入 dev。
- **剩余工作：** 发布原生子 Issue 后实现上述纯 Interface、公开 C 矩阵及独立审查/当前 CI。
- **阻塞：** 无需新的用户产品决策或账户权限；运行上限是本票明确的常规实现选择。真实角色资格与 consumer 不在本票验收范围，仍由 #95 后续切片承担。
