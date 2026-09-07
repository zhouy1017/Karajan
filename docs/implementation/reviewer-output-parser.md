# 模型审查输出的纯解析边界

对应 [#104](https://github.com/zhouy1017/Karajan/issues/104) 与[固定规格](../planning/v1/issues/m3-review-output-parser.md)。本实现属于 C：消费一条文本及控制器提供的引用集合，返回内容 DTO；没有连接 Reviewer 会话、资格、Evidence 写入或 gate。

`karajan.candidates.review_output.parse_review_output(content, *, allowed_files, allowed_acceptance_refs)` 接收严格 UTF-8 的 `str | bytes`，两个 scope 必须为 `frozenset[str]`。输出 `ParsedReviewOutput` 仅有 `verdict` 与现有 `Finding` 列表。wire `pass / changes_requested / inconclusive` 分别映射到存储内容值 `passed / failed / inconclusive`；`pass` 含阻断 finding 时整体拒绝。空 findings 不改变失败或无法确定的结论，severity 不代替 blocking。

固定 `PARSER_REVISION = karajan.review-output-parser.v1`。上限为 65,536 UTF-8 bytes、3 层 JSON 容器、32 条 findings、每个 behavior/trigger 2,048 码点、file 4,096 字符、acceptance_ref 256 字符、line 1 至 2,147,483,647。限制不接受 caller 覆盖，也不代表模型能力或批准预算。解析前检查字符串感知的容器深度，decoder 拒绝重复 key、非有限数字和不完整 JSON；解码后再次拒绝孤立 surrogate。

错误顺序为 scope → 输入编码/体积 → JSON 深度/语法/歧义 → 解码字符串 → 字段完整性/精确类型 → 正确类型字段上限 → 引用语法 → 既有 Finding 与文本约束 → 精确引用成员 → verdict 冲突。复用 `Contract`、`Finding`、`Identifier` 和纯 `relative_path`；额外类型预检用于保持这套错误优先级，既有存储 schema 未收紧。引用不做大小写、Unicode、URL 或空白归一；空 scope 表示没有可引用项。

所有拒绝抛出 `ReviewOutputError(ValueError)`，`.code`、`str/repr/args` 只含稳定安全代码，不输出原文、路径、验收 ID 或底层解析错误。七类代码与优先级见固定规格。没有截断、JSON 片段提取、默认补字段或部分成功返回。

公开测试位于 [test_review_output.py](../../tests/candidates/test_review_output.py)。它覆盖完整/歧义 JSON、限额边界、引用、严格类型、结论矛盾、可信字段排除、所有错误脱敏以及无文件/网络/时钟/数据库效果的纯函数调用；复用现有合成控制器 fixture 与真实临时 Git/CAS，验证内容可装配进原 `ReviewResult`，但单独不能成为 Review Evidence。作者原始红例与最终检查记录位于工作树 `.cache/review-parser-author`，由后续发布流程保存，不据本地源码或单测声称 G 完成。

本候选作者验证为 269 项 parser C 通过；完整 Candidate 回归包含同一批 parser，Windows 372 passed / 3 POSIX skip、Linux 375 passed，不重复累加计数。全仓库 Ruff、backend 123 个源文件 mypy 与新增 Python 文件格式检查通过。Linux 首轮旧进程探针因未继承 backend 的 PYTHONPATH 出现 1 个缺模块失败；保留原记录，仅修正测试启动环境后通过，未改旧探针或业务协议。独立 Standards/Spec、固定实现 commit 和当前远端 CI 由后续交付流程核对。

后续 #95 observer 必须确认实际消息角色、完成/截断状态、当前来源与独立只读执行，并从冻结 subject/批准验收材料编译 allowlist。控制器还要提供真实 Actor、Candidate、Policy、Check IDs、来源与日志身份，再调用现有 `record_review` 和 gate。解析得到 `passed` 只是内容值；本票没有 P/S、真实 Reviewer 资格或生产 consumer 验收，也不关闭父票 #95/#13/#14。
