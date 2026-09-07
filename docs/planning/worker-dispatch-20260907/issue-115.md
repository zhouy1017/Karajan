Parent: [#95](https://github.com/zhouy1017/Karajan/issues/95)。

负责 worker：gpt-5.6-terra；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前为后续批次，依赖按实际发布关系核验。

承担#95剩余可信依赖与独立Reviewer准入。复用#100 ApprovedReviewerBindings和当前validation subject/全部最终Checks，扩展ApprovedRunRouting当前Worker-only拒绝点，复用select_rule/evaluate_route与CapacityStore；不复制第二套模型选择器。
从原批准Plan中的Reviewer depends_on=[worker_task_id]和真实全部作者谱系派生风险/复杂度/paths；membership-attempt/context仅预备身份，创建真正独立稳定Attempt/context，显式Profile资源估计，不借Worker reservation。
公开advance/get/reconcile仅Run/operation/principal IDs；旧Run没批准Reviewer不静默加节点。全部实际check通过、当前source/generation角色资格与资源/授权有效后才能admit。

验收：
- [ ] 未批准/错误多重依赖/缺失败旧Checks/伪作者或Candidate/低报复杂度/旧授权，均无预留或启动。
- [ ] T1/T2与所有作者Attempt/context独立；T3同或未知家族在占slot前拒绝。当前Go限定scope不扩至T2/T3。
- [ ] 独立稳定Attempt/context/Capacity request+key，完整资源向量/Commander保护量/窗口与原预算有效；没有统一伪token估计。
- [ ] 重复advance/丢失admit/activate回执只读恢复原完整request；unknown不新claim、不退款；Worker local stop不造remote settled腾位。
- [ ] 当前取消/审批/source/generation/window变化阻止下一effect，输出只供后续consumer的guard，不以准入替代逐send检查。
- [ ] C实际Capacity/SQLite回归+当前CI独立审查，保留v1和Worker通路。
后续consumer负责真实进程/输入/输出/Evidence/validation receipt；#107与真实业务/整链S另负责。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：依赖下列发布记录中的当前候选接口/资格，解除后由原分派模型执行。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。