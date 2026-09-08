# DG02：Go 当前资格到真实业务审查

目标：完成 #107/#113/#117 各自限定范围，得到真实需求→只读 Planning→owner 精确批准→Worker Candidate→完整 Checks→独立 Reviewer Evidence/当前 receipt。此阶段尚不等于完整 v1 或完整 M1 出口。

## 顺序与边界

1. #107 独立于 #116：固定当前 source/runtime/tokenizer/Profile/generation/Journal，在原 C/P 满足后经一次原有界三场景 suite，真实 `ApprovedReviewerBindings.current_locked` 做 membership-only 正控，再撤销/失效负控。保留历史 18 请求、过期/撤销和累计消耗；同 key 只读恢复，不复活旧记录。
2. #113 等待 #112 与独立资格 producer 的 C/P。固定 Commander 来源与原有限上限后做 S；其角色资格不从 Worker/Reviewer 借用。原 intent、真实模型 Plan 和原 execution ID 绑定。
3. 向 owner 展示真实计划版本及权限影响，通过产品原批准入口决定；订阅测试授权不能代替该具体批准。批准后至少一个真实 T1 Task 产生 Candidate。
4. #117 等待 #116、#113 和当前有效 Reviewer 来源。先验证正确/缺陷业务 Candidate，再跑整链；#107 负控撤销后的历史资格不作为当前可用资格，需要新 start 时按原规则单列。

每次 S 前固定实际来源、有效期、请求/时间/I/O/C 上限与目标隔离环境。失败或 unknown 保留原记录和消费，修正代码先回 C/P 并冻结新来源，再按已有授权和原边界安排新尝试；不自动循环探测直到绿灯。

## 出口

逐项原 AC 有 S/C/P 与适用 G 证据，真实来源和业务 Candidate 关联可读回；旧 term、取消、失败 Checks、不确定 Review、来源失效均拒绝升级为通过。当前候选经独立双审和必需 CI 后按授权进入 dev。

完成名称：Go 真实规划与审查链完成。Go 的 PR 交付在 DG03；#12 顾问/人工交接、#13 两来源/T2/T3 等父范围继续保留。缺少具体 owner 批准时只阻塞该链，推进 DG03 离线接线及 DG04 订阅准备。
# 历史安排：当前业务顺序见 [当前业务顺序](../business-first.md)；本文件保留原范围与原 AC，供追溯。
