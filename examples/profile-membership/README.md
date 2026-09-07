# Profile 资格集合的本地证据

对应 [#99](https://github.com/zhouy1017/Karajan/issues/99)。实现
`3a8cc5875b075285ab18796d1ab4bc36303192a1`，从 dev `2e587d1` 独立交付。
公开接口复用原静态路由判断，不接 Capacity、不选择或启动模型。

| 验证 | 实际结果 |
| --- | --- |
| 作者 Windows / WSL | 两平台各 156 通过（33 新增＋123 原有） |
| 最终 LF 与独立 dev 基线 | Windows 156 / 3.32s，Linux 156 / 5.40s |
| 原路由 / reserved 行为 | 7 个固定输入的 14 份完整结果全部一致 |
| 独立 Standards / Spec | 无待处理发现；Ruff 与 9 个路由源文件 mypy 通过 |

`author/` 保留原始缺接口失败、全部输入、早期和最终 XML、原 freeze 及 LF 转换记录。
`author/history-crlf/` 保存转换前的三个源文件原始字节；只有两个文件发生换行转换，AST
都相等。`independent/` 是 root 在最终 LF 源码与独立 dev 分支上的实际结果。

`publication-map.json` 记录 22 份复制件及三个实现 Git blob 的 SHA-256。原作者 freeze
仍指其当时来源，不改写成新提交的结果。正式测试输入及依赖保存在仓库中；归档中的
`.py.txt` 是历史输入，不参与默认 pytest 收集。

```text
PYTHONPATH=backend python -m pytest tests/routing -q
python -m ruff check backend/karajan/routing tests/routing/test_profile_membership.py
PYTHONPATH=backend python -m mypy backend/karajan/routing
```

这些是使用显式 snapshots 的 C 证据，没有真实资格、账户、Capacity store 或模型调用。
资格集合的规范顺序不是优先排名。Reviewer 可信绑定、真实只读角色资格、资源准入及执行
仍属于 #95；当前候选的 GitHub CI 与合并状态另行核对。
