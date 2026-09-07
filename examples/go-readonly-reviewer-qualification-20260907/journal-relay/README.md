# Issue106 Journal / Relay 作者冻结

只修改两份产品源码及两份新增正式测试。新 schema 为 `karajan.go-reviewer-qualification-grant.v1`：原 qualification_id / common binding，加 probe_spec_digest、clean_review / defect_review / denied_read 和原 GoQualificationLimits。每 grant 仍最多六次，先持久未知发送、一次返回 send_allowed、重复不发、失落 begin 精确读回、撤销/过期不重置。

`GoReviewerQualificationContext` 是独立 frozen dataclass，顺序为 accounting、source_sha256、probe_spec_digest、scenario、approved_input_tokens、reserved_output_tokens、operating_context_tokens、fixed_margin、ratio_margin_basis_points。`limits()` 返回独立的六字段dict，`measure(payload)` 使用原实际固定tokenizer计量和当前source校验；不返回原文。

Relay 精确匹配该类型与新 grant，不与 Task / Worker / legacy 互用。完整请求计量后，工具声明和结构化调用历史仅允许 read；数据中提及 edit 不会被误判。完整 SSE 回复只允许最终 read 工具名；禁止工具不转给native。工具集由具体类型/绑定推导，无 caller 可扩参数。实际文件范围、native 权限/会话/资格封印仍由上层观察器验证，Relay拒绝不冒称OS拒绝。

## 实际检查

- Journal 首次公开输入 red：新 schema 原被拒绝（1failed），最小实现后1passed。before/ 保留这一次原产品与测试字节。
- Relay 首次 red：缺新 context（1failed）；新增后1passed。readonly response 的 edit 原返回200（1failed/3passed），改为固定拒绝后4passed；声明/历史edit原可发送（2failed/4passed），改为零Journal/零上游后6passed。各原XML保留；未声称每个中间源码都有独立完整快照。
- 新正式合同最终 Windows **61passed / 5.06s**；WSL Linux **61passed / 6.31s**。实际SQLite和本地HTTP、固定离线tokenizer；仅上游HTTP是明确MockTransport。Linux有一个pytest缓存目录权限warning，不影响结果，也未通过改权限或跳过测试掩盖。
- 兼容组 Windows **318passed / 1failed / 28.45s** 原结果保留。失败为旧 `test_go_relay_context.py::test_failed_context_observation_revokes_remaining_sends[missing]` 在HTTP502后立即读取final Journal，观察到暂态send_unknown。原case单次复验passed；同一公开HTTP/Journal延迟completion边界在冻结前Relay和当前Relay均证明：502已返回且grant已revoked时final账本可能仍unknown，close后记录response_received。该定向组3passed/2.16s，不伪称完整319新跑全绿，未改旧测试或旧完成顺序。
- Ruff check / format 4文件通过；mypy 两产品文件 Windows / Linux目标均通过。实际命令/输出见static-commands.json及对应stdout/stderr。

`legacy-ast-comparison.json` 验证旧 binding数据模型、编码、create/revoke/complete/authenticate/history和旧两种context保持AST相同；send_guard、exact call recovery/withdraw/complete、请求读取/drain、start/close也保持相同。新逻辑仅增加schema分派、必需context与Reviewer工具集，旧JSON/固定canonical意义不变。

## 复跑与边界

Windows使用ROOT `.venv/Scripts/python.exe`，设置 `PYTHONPATH=backend`、`KARAJAN_GO_TOKENIZER_DIRECTORY=ROOT/.cache/go-task-execution/.cache/go-context-artifacts`、`KARAJAN_REQUIRE_GO_TOKENIZER=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，执行两份新正式tests；compatibility-windows-command.json含完整旧/新组命令。Linux沿用固定venv `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python` 和同一artifact的/mnt/c路径；对两份正式tests执行pytest并指向本树pyproject。没有模型/真实key/远端/Git提交操作。

原读时序诊断为cache中的 `test_completion_timing.py`，只通过公开GoRelay/GoCallJournal模拟晚到的completion；它不是新增HTTP权限，也不是本片角色资格证据。当前产品/source摘要均在freeze.json；此后修改须明确重新冻结，不把旧source升级为当前通过。
