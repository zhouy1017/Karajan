# 批准 Candidate 的可信 Checks：本地证据

关联 [#94](https://github.com/zhouy1017/Karajan/issues/94)。实现覆盖原 Worker capture 的
subject revision 1。Review 绑定版本切换、当前 PR CI 和合入 dev 尚未完成，本目录不宣布
#94、#13、完整平台或真实服务链完成。

| 范围 | 实际结果 | 证据 |
| --- | --- | --- |
| 固定工厂 → Host child → Linux namespace → 全部 Checks → Evidence | 正确候选两项 passed；缺陷候选行为 failed、语法 passed；重开复用原证据 | `check-composition-final.xml`，2 passed / 73.85s |
| 最终来源 | 136 个 backend 源文件前后摘要一致；4 个实际 Check 收据 | `check-composition-final-source-*.json`、`check-composition-final-observations.json` |
| 隔离、停止、日志 | 作者 19 项 Linux；独立新增 5 项及原 19 项通过 | `check-runner-author/`、`check-runner-independent/` |
| Check Host 与旧角色兼容 | 两平台 execution 各 121 项；root 独立 24 项通过 | `check-host-evidence/`、`check-host-root-review.*` |
| 阶段、全部检查、恢复、取消 | 作者 26 项；独立原 26 项及两平台各 3 组故障通过 | `candidate-checks-author/`、`check-facade-independent/` |
| 执行中取消并发 | 两个 advance 与一次 cancel 竞争；仅启动一个检查，确认停止，重开不重复启动 | `check-concurrency-author/`，1 passed / 22.18s |
| 共享 Run 预算 | 两平台各 10 项独立边界；最后 Writer/Capture 61 项回归通过 | `shared-budget-independent/`、`candidate-checks-author/final-writer-regression.xml` |
| 固定工厂与 Evidence 精确恢复 | 工厂 18 项；最终查询 11 项作者 + 5 项独立通过 | `check-factory-current.xml`、`evidence-lookup-final-format.xml` |

实际执行使用临时 Git/CAS、SQLite、固定直属 child、Linux namespace 和受控 Python stdlib。
规划、资格及 Writer 输出是显式 fixture；没有官方 provider 调用，不授予任何角色 S 资格。
测试最后主动取消以清理 Host，导出 observation 明确标记这一清理事实。

共享预算保留首次 Writer 执行意图和累计进程 claim；历史和晚到 capture 不消费新权限。
控制器在新效果准入处复核 deadline。它不是对整个运行中 Writer、底层准备、Journal 写回
或远端接收时刻的端到端硬截止，也未新增质量修复/基础设施重试循环。

## 失败历史

`check-smoke-source/` 保留中间快照、原输入及两个失败用例。最初正例批准 10 秒窗口，
child 最后报错发生在 claim 后约 12.26/10.35 秒，均无 native claim。工厂重开成功，
source 扫描约 0.85 秒；固定日志只有通用失败码，不能单独证明精确错误原因。
后续正例明确批准 60 秒以覆盖 Host 启动、导入与来源复核；短 deadline 仍由故障用例验证。

最后只调整新增 Evidence 条件换行，AST 未变，见 `store-format-observation.json`。
随后复跑了 16 项查询及最终两项实际 Host 全链。旧 74.42 秒通过和首次失败原样保留。
各目录 README 区分产品失败、测试脚本错误和过严断言，不用重跑覆盖失败。独立 CLI 与
JUnit 计时各自保留。作者早期 freeze 的 pending 审查不改写；最终独立报告另列。

取消并发用例的初稿断言排除了合法的 `completed` 加非零退出码观察。独立复核后修正为
禁止把零退出的 completed 当作取消结果，并继续要求业务已取消、停止确认、Evidence 未通过；
最终单项真实 Linux 复跑通过。原测试、首次通过结果和报告保存在 `check-concurrency-author/history/`。

## 重现与核对

安装仓库锁定依赖，WSL state/image/work 使用 Linux 文件系统；配置本地已验证 tokenizer
与离线环境变量后执行：

```text
python -m pytest tests/runs/test_candidate_checks_native.py -q -p no:cacheprovider -o "pythonpath=backend tests/projects tests/runs tests/web tests/isolation tests/adapters/opencode tests/candidates tests/execution"
python -m pytest tests/runs/test_candidate_checks_concurrency_native.py -q -p no:cacheprovider -o "pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web" --basetemp=/tmp/karajan-check-race-reproduce
python -m pytest tests/isolation/test_check_runner.py tests/execution/test_check_manifest.py -q
```

`publication-map.json` 保存复制件的原位置、大小和 SHA-256。独立 Python 输入使用
`.py.txt`，复跑时复制为临时 `.py` 并设置同一 pythonpath。原 freeze 保留未发布本机
辅助脚本的历史摘要，map 列出省略项。未复制数据库、bootstrap、镜像、原生工作区或账户材料。

Reviewer 允许集合仍为空，质量门与交付资格保持 false。可信 Review 绑定、新 Candidate
版本消费及全部检查重跑继续由 #94/#95 接口推进。
