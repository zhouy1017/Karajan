# #104 模型审查输出 parser：证据归档

本目录对应 [#104](https://github.com/zhouy1017/Karajan/issues/104)，属于 #95 的供应商无关纯 C 解析切片。实现代码固定为 **4ab3e64a403943e53a3e06c78f20e9c5756020e1**，基线为 **2e587d1773c514361689e13ebbd16ba62f1cd219**。[publication-map.json](publication-map.json) 绑定四份冻结源码 SHA 与实现 Git blobs；原始 freeze/review 内提交前的 pending 状态不改写。

Parser revision 为 karajan.review-output-parser.v1。parser SHA 为 8b72d037154f7c4d1a9bb8af95d5713b3851de4122327aa070f3f2562c226733，正式测试 SHA 为 66a4b72f55928a18815373ebeeb9063ac23a4e566b563bd4fc22ac48139f6d6f。归档时四份源文件与作者 freeze、独立 before/after 仍精确匹配；没有修改产品、正式测试或再次执行行为测试。

## 原始结果与归属

| 来源 | 实际结果 | 范围 |
|---|---|---|
| [作者记录](author/README.md) | 269 parser C passed | 严格 UTF-8/完整JSON、重复key、bounds、精确引用、verdict、无可信字段及安全错误 |
| 作者完整 Candidate Windows | 372 passed / 3 POSIX skip，共375项 | 包含同一269 parser，剩余为旧Candidate合同/存储/gate回归 |
| 作者首次 Candidate Linux | 374 passed / 1 failed | 旧进程探针缺继承backend PYTHONPATH，ModuleNotFoundError；保留 candidate-final-linux.xml/log，虽名final也不是全绿 |
| 作者修正环境后 Linux | 375 passed / 0 skipped | 正确PYTHONPATH后同组通过；未改变旧probe、parser或存储代码 |
| [独立审查](independent/README.md) | Standards 0 finding；Spec 0 finding | capacity_facts 同一位独立 reviewer 分别审两轴；作者运行仍归作者 |
| 独立自有公开输入 | Windows 15 passed / Linux 15 passed，均无skip | 包括实际临时Git/CAS与三种状态装配到原ReviewResult/gate；可信身份来自明确合成controller fixture |

独立没有重跑作者269/375；它的15项在两平台复验，不计作30个唯一用例。parser-only与Candidate组也不重复加总。raw README、stdout与JUnit中的计时原值各自保留，不强行修改为同一个数字。

全仓库 Ruff、backend mypy 123 sources 与两个新Python文件format check的原日志/零退出保留在author。本切片没有新增 P/S，没有真实 Reviewer 资格、消息完成/来源证明、模型调用或生产 Evidence consumer；passed 只是解析内容，不能直接取得成功gate。G 仍待当前CI、发布与owner合并；不因本包关闭父票#95/#13/#14。

## 原件与重跑边界

45份raw副本：34份作者原记录（包括其freeze全部33个artifact）、10份独立记录、1份冻结正式测试输入。另有 README/map/bytecheck/.gitattributes 四份新文件，共49文件。每个copy的原位置、目标、SHA和长度见map；[bytecheck.json](bytecheck.json) 是重新读取原件与副本的字节核对。

保留实际缺入口红例（1 collection error）与 JSON、字段、scope 的分阶段 red/green输入/XML/log；这些是开发时中间实现的历史，不提升为最终版本测试结果。初次WSL被访问权限拒绝发生在测试前，没有对应测试XML，作者README按原事实说明。Linux环境失败与产品红例分开保留。

initial-test.py.txt、各 *-red-test.py.txt、implementation/test_review_output.py.txt 与 independent/test_parser_contract.py.txt 是原输入字节，只改归档后缀，不被pytest/Ruff自动发现。最终parser源码原字节在 independent/review_output.before.py.txt；正式测试仍位于仓库 tests/candidates/test_review_output.py。重跑应恢复到独立目录或使用正式测试路径，并保留现有tests/candidates/test_validation.py所需正常Git环境；具体命令见author README与independent command/review，不从改名后的档案目录自动执行。

旧记录的.cache与绝对Windows/WSL路径表示当时工作目录。本归档按map解释新目标，不重写旧报告制造新来源；未复制build_evidence.py、数据库、临时仓库、pytest缓存、bytecode、key、provider材料或真实prompt。独立 source maps 与作者 final-sources 完整保留；final-sources是作者执行后固定的来源记录，不冒称新获得的执行前观测。

## 空白与提交

所有raw原件保留原字节和CRLF/尾空白；叶级 .gitattributes 仅含 * -text 防止Git自动换行，不添加whitespace忽略规则。新README/map/bytecheck/gitattributes为LF、无BOM/尾空白并单独检查。此归档任务没有stage或提交，staged diff --check 未运行，不能将未跟踪文件的检查范围当全发布通过。代码提交由root完成，原历史freeze字节不变。
