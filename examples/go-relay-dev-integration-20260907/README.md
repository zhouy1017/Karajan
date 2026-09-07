# PR88 与 dev 组合：固定版本证据

测试与审查固定在 **`046045f2d9573c3cae6375c38c8de2f6b4190d70`**，双父为 `9e092f868db3cdcf6c215de08f3a1a1eacd833ec` 与 `2e587d1773c514361689e13ebbd16ba62f1cd219`。Relay SHA-256 **`b79f1e08afa5aa0931e4b1056dbce7f674014d04606ce64a9d3cd48ced723881`**，Git blob **`c5cfae045c22d8aa0534346d4e654e7ac9dcfd04`**。`implementation/go_relay.py.txt` 直接来自该提交对象；后续只提交本归档不会改变这里的历史代码身份。

[publication-map.json](publication-map.json) 映射每个原始/目标路径、SHA、字节数和来源；[bytecheck.json](bytecheck.json) 记录原件、副本、Git 对象及新文本的复核。**34 份 raw 副本**（33 原文件 + 1 Git blob）与 README/map/bytecheck/`.gitattributes` 四份新文件，共38文件。没有运行新的行为测试、修改产品或执行 Git/远端写操作。

## 实际执行与独立审查

| 记录 | 实际结果 | 边界 |
|---|---|---|
| `author-history/` | Luna 145 passed / 60 skipped；Ruff 通过，backend mypy 122 sources 通过 | tokenizer 未配置造成 skip，保留原文；不是205项全执行 |
| `root/win32/` | root 205 passed / 0 skipped / 0 failed | 固定 tokenizer 配置后实际执行原七套；229源 before=after |
| `root/linux/` | root 232 passed / 0 skipped / 0 failed | 包含 #89 既有 native producer、send_guard、UDS 与 source补验；229源 before=after |
| `root/boundaries-win32/` | root 13 passed / 0 skipped / 0 failed | 原3 publication + 原6拒绝/恢复 + 原4 framing；四份旧输入及观察原样复跑，不是13项新设计测试 |
| [独立报告](independent/README.md) | Standards 0 finding；Spec 0 finding | **同一个独立 reviewer** 分别审阅两个轴，读取root/Luna原执行记录与源码；没有第二次行为执行 |

各组有重叠，不相加为一个唯一测试总数。Linux本机 namespace/OpenCode 配本地 HTTP fixture 证明其 C/P 范围，官方 S 未重跑。当前仅旧 `9e092f8` head 的 CI 已成功；**新 `046045f` 组合 G 待运行**，不将旧 CI 当成新版本验收，也不声明代码已经进入 dev 或 #89 已关闭。

本次归档额外按 Git `046045f` 的229个 source blobs，逐字核对两平台执行前后 source maps 全部相同；不是使用可能继续变化的工作树作为代码基准。四份边界输入同时与原.cache文件、root实际执行副本、记录hash匹配。静态输出执行者为 Luna，独立报告只审核它；并列记录，不改变作者/审查归属。

## 输入和历史路径

`root/run.py.txt`、`root/boundaries.py.txt` 保留当时实际命令编排，平台子目录保留具体 argv、环境资产路径、前后source、退出结果、XML/stdout。四个 `test_*.py.txt` 保留实际执行输入；这些是档案文本，不能从改名后的归档目录直接当正式pytest/生产entry运行。复跑需恢复到独立测试目录、配置原固定资产并使用各command/result记载的公开输入。

原记录中的 `.cache/...` 和 Windows/WSL绝对路径是当时运行位置；map给出本归档目标，没有改写原JSON/README以假装旧路径本来就属于这里。`independent/SHA256SUMS` 的两份报告校验已原样核对。未复制其他CLI events、任务/prompt、key、bootstrap、数据库、临时仓库、runtime/tokenizer资产或bytecode。

## 字节和空白范围

所有 raw 副本按字节保留，历史 CRLF/XML/源码尾空白不清洗。叶级 `.gitattributes` 仅 `* -text` 防止自动换行转换；未添加 whitespace 忽略规则。新写四份文件独立检查 LF、无BOM、无尾空白。此任务不stage，**staged diff --check 未运行**，不凭此前文件未跟踪而声称全发布空白门禁通过。历史空白与产品/test CI应分别报告。
