# #106 root 五模块独立审查结论

审查者 capacity_facts。这五模块由 root 实现；本人仅曾与作者对齐接口，未编写其产品。Standards / Spec 由同一独立 reviewer 分别评估，不声称两位独立审查者；本人编写的 Journal/Relay 不在本独立范围。

## Standards

0 项未关闭确认发现。显式 v2 bootstrap 保留 v1 shape；同一 Project/Qualification/Credential/Journal 装配 Worker 与 Reviewer 的具体来源，history 不加载当前资产。唯一 scope resolver 在 membership 前执行，未知/fixture/Worker 不形成合格 Reviewer；新 limits 用 JSON list，未扩展外部授权或模型输入。

## Spec

0 项未关闭确认发现。Root 自查的 mid-suite current authority 缺口经代码独立确认，现接入真实 Store current guard，并在场景/native/send 边界使用。另独立实证 CURRENT-EXPIRY-001：source 核验期间跨原 expiry 仍 yield；单例原红确认，现 source/credential 后紧前复查原时间窗，原例已通过。

最终独立 Windows C：6 passed / 0 failed / 0 skipped，pytest 5.96s；正常、credential revoke、Profile disabled、inflight qualification revoke、new latest unknown、source 核验跨期限。撤销/禁用公共写入与新未完成 start 必须真正成功；禁止进入第二场景或到期 guard effect，finally 原 grants 撤销，原 command 重放不再观察。七个相关输入源码测试前后逐字节相等；root Store 8deff58b、Suite 88ec0b83，完整 SHA 见 source-final-before/after.json。

此矩阵是真实公开 Store/SQLite/具体 Suite/Journal，source 与 observer 是显式替身，report 校验在本授权调度边界内隔离；未运行 namespace/provider/完整 parser 验证，不作为 P/S。原首三例 before.xml 是旧 Suite＋已更新 Store 的混合来源搭建失败，不作产品红；helper-diagnostic 明确 unexpected current_guard。原时间边界红、其源字节和最终绿全部保留。

本审查可接受这五模块及已复验 guard 修复。#106 整票的独立 native/observer/Suite C/P、影响回归、静态与当前 G 由其他已分工证据承担；这里不虚称全部已完。#107官方机制S、#95真实ReviewerTask/业务Candidate质量/ReviewEvidence仍未由此证明。

命令：`PYTHONPATH=backend KARAJAN_INDEPENDENT_SOURCE=current KARAJAN_GO_TOKENIZER_DIRECTORY=<固定官方离线资产目录> HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest .cache/reviewer-root-independent/test_current_authority.py .cache/reviewer-root-independent/test_effect_expiry.py -o "pythonpath=backend tests/projects tests/adapters/opencode .cache/reviewer-root-independent" -p no:cacheprovider -q --junitxml=.cache/reviewer-root-independent/final-six.xml`。实际解释器为 ROOT/.venv/Scripts/python.exe；资产路径见本任务上下文，无模型/密钥读取。
