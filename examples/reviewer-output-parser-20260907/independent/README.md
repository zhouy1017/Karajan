# #104 独立 Standards / Spec 审查

**结论：可以接受当前纯 C 产品切片，0 个具体产品 finding；G 发布验收尚待当前提交/CI/合并。** Standards 与 Spec 由同一位独立 reviewer capacity_facts 完成，不冒称两名审阅者，也不把作者自测冒充本人的独立测试。

基线2e587d1773c514361689e13ebbd16ba62f1cd219。parser SHA8b72d037154f7c4d1a9bb8af95d5713b3851de4122327aa070f3f2562c226733；正式test SHA66a4b72f55928a18815373ebeeb9063ac23a4e566b563bd4fc22ac48139f6d6f。source-before/source-after逐项完全一致；原模型/存储/serial及test_validation与基线Git blob同字节。

## Standards

一个纯入口集中编码、深度、严格decoder、引用、Finding与结论映射，没有第二存储模型或provider依赖；复用Contract/Finding/Identifier/relative_path。类型预检用于已规定的错误优先级，没有修改旧Finding或gate。深度先于递归decoder、全部错误固定code并抑制底层异常展示；不提取JSON片段、不修补字段或返回部分成功。读代码与自有行为检查未发现越界IO、权限或隐式状态。

已核作者固定source上的完整Ruff/mypy/format输出与零退出记录，其freeze中全部artifact长度/hash与实际字节匹配；没有把这些作者静态运行改称独立执行。

## Spec

依据docs/planning/v1/issues/m3-review-output-parser.md逐项核对：

| 原AC | 结论及证据范围 |
|---|---|
| 映射/纯内容 | 三wire值映射准确，仅verdict/findings；独立真实Git/CAS正控保持旧gate，只有内容不能构成ReviewResult。 |
| UTF-8/完整JSON | 按65,536 bytes、quote/escape-aware深度和完整decoder拒绝；独立边界含额外对象、未知surrogate key、极长整数、文本括号正控。 |
| 歧义/非法数 | 任意object重复key用decoder hook拒绝，转义同key、不应被忽略的非有限未知字段独立实际拒绝。 |
| 严格Finding | 全7字段、精确类型、枚举、文本控制规则保留；作者完整参数矩阵已核，无新增存储收紧。 |
| 限额 | 固定bytes/count/codepoint/line/depth；独立65536/65537多字节及32末项阻断，作者其余边缘正反覆盖，拒绝整体。 |
| 引用scope | 纯相对路径/Identifier复用，精确成员与casefold碰撞；独立NFC/NFD不合并、casefold别名scope拒绝；空scope deny-all在作者矩阵。 |
| 结论不能覆盖问题 | 独立末条blocking拒pass；三verdict真实存储与gate兼容，failed/inconclusive不提升。 |
| 身份排除/错误脱敏 | 独立额外Actor、敏感文本、多阶段失败及traceback/capsys/log无回显；来源/完成/权限由未来observer提供，不从模型内容赋权。 |
| 纯函数/兼容 | 独立禁止文件/数据库/网络/时钟/Store构造时解析正常，返回内容修改不影响下次；旧存储字节和fixture兼容。 |
| G交付 | 尚待固定实现commit、当前CI、发布及owner合并；本review不能替代这些条件，不关闭#95或宣称P/S。 |

未发现本纯C范围的原AC缺项。作者原始缺入口/JSON/字段/scope红证据、最终269 parser测试已查；Candidate Windows375项=372pass+3POSIXskip，Linux首次374pass+1旧子进程PYTHONPATH失败保留，修正启动环境后375全pass；不得把重叠组相加计总覆盖。这些是作者记录，独立没有重跑269/375。

## 自有公开行为验证

test_parser_contract.py由本reviewer自行编写，未读取作者parser测试设计后复制用例；复用既有test_validation真实Git/CAS的可信合成身份fixture，独立断言和负面输入。Windows15passed/4.74s、WSL15passed/0.89s，两平台无skip，无产品红。读正式tests只用于随后AC覆盖核对。未改产品/正式tests/Git/远端，不需要runtime、密钥或provider，C不冒充S。

Windows命令见command.json；Linux：

```text
PYTHONPATH=backend /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest -p no:cacheprovider -o "pythonpath=backend tests/candidates" .cache/review-parser-independent/test_parser_contract.py -q --junitxml=.cache/review-parser-independent/final-linux.xml
```

从本worktree运行。公开输入复现需保留tests/candidates/test_validation.py及其正常Git环境；无cache外私有helper依赖。独立目录内的XML/原产品snapshot/来源/命令全部按本轮原字节冻结。
