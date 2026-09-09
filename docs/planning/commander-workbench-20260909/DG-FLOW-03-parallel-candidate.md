# 交付切片｜两个无依赖 Task 的真实并行与组合候选

Parent: [#17 2–3 子任务并行与组合验证](https://github.com/zhouy1017/Karajan/issues/17)。Related: [#13 串行执行与质量门](https://github.com/zhouy1017/Karajan/issues/13)、[#153 工作台业务闭环](https://github.com/zhouy1017/Karajan/issues/153)、[#158 设计治理](https://github.com/zhouy1017/Karajan/issues/158)。

发布标签：`kind:task`、`status:queued`。Worker：`gpt-5.6-terra`；独立 Reviewer：`gpt-6-astra`。实现依赖：P2 同版批准（[#160](https://github.com/zhouy1017/Karajan/issues/160)，原生 blocked-by）、#13 的执行/候选契约、#90/#94 提供的 capture/check 原语。真实验收 gate：#17 的原 AC 仍保留；本票只承接可独立验收的并行与组合候选实现，不满足 #17 对 #14/#15 的完整交付依赖。

关联交互验收：UX-AC04、UX-AC07、UX-AC09、UX-AC10，定义见 [工作台设计](https://github.com/zhouy1017/Karajan/blob/codex/commander-workbench-design-20260909/docs/prd/commander-workbench.md)。

## 范围

按已批准同版任务图派发两个无依赖 writer，使用独立 workspace、Attempt、授权、资源和消费记录。两个任务完成后按固定顺序组合候选，并对组合内容重新运行适用 checks。

## 验收

- [ ] **C/P/S：真实并行。** 两个 Task 各有独立 Attempt、workspace、来源、授权和消费/停止记录；固定输入下两个合格且已授权的较低级别模型实际编码执行窗口重叠，记录实际来源、模型、请求与停止事实；主 Commander 不代写文件或代理 writer。脚本执行者可证明 C/P 子集，不能替代本项 S 或关闭本票。
- [ ] **C：依赖。** 有未满足依赖的 Task 不启动；两个无依赖 Task 才能并行；每项输入、允许路径、写权限和检查要求与批准版本一致。
- [ ] **P：边界和故障。** 越界、冲突、失败、取消、unknown、重启和迟到产物均有明确状态；未知发送不释放资源，旧 term/来源撤销不能产生新效果。
- [ ] **C/P：组合。** 按固定顺序集成到新的 CandidateStore 内容身份；组合候选有新的 tree/input/policy digest，旧候选 checks 不可直接继承，全部适用 checks 重新运行。
- [ ] **G：交付。** 保存两个 Attempt 和组合 Candidate 的复现输入、命令、期望/实际、证据层级、当前 commit、独立审查和必需 CI；本票完成不关闭 #17 或宣称完整交付/多来源通过。

## 非范围与证据边界

本票不完成独立 Reviewer、真实 GitHub PR、自动换源、完整 v1、多来源资格或 #17 的最终 delivery gate。模型自述“同时运行”、两个异步函数或本地 fixture 不足以证明真实并行；必须有受管工作区、进程/Attempt 和持久收据证据。

## 交接

完成后把依赖图、并行时间观察、组合候选 digest、旧候选失效和故障证据回填 #17/#13；P4 只消费当前组合候选。
