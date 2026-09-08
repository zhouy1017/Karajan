Parent: [#93](https://github.com/zhouy1017/Karajan/issues/93)。

负责 worker：gpt-5.6-terra；CI 与审查修复仍由同级 worker。Reviewer：gpt-6-astra high。当前为后续批次，依赖按实际发布关系核验。

从 #93 A 剩余范围实现可被 #110 直接使用的生产 PlanningAdmissionAuthority，输入只为 owner 的 Run/intent/execution IDs。
读取原 Run ceiling、当前主 Commander/term、planning_budget_ref、冻结配置/Rulebook、当前 Profile/source/generation 与显式有限估计；复用纯 select_rule/evaluate_route，不能循环要求已批准 Plan、不能切换主 Commander。
先持久固定原 command keys/请求/Attempt/fence，再依次取得真实 Capacity admit/activate 与准确规划预算权威；现金上界不具备时拒绝，Go 授权只允许固定官方订阅、有限次数/时长和 unknown 策略，不凭 budget_ref 或 token estimate 造硬预算证明。
建议新增 orchestration/planning_admission.py 与专属 tests，必要扩展 projects/demand 的规划 scope；#110 binding 由该票负责人协同冻结，勿复制 Task worker 假admission。

验收：
- [ ] 正常主 Commander 获得可当前核验的原 binding+容量+预算证据；批准 Plan 尚不存在也可准确准入。
- [ ] 缺/歧义规则、未合格/旧来源、owner/term/Profile/预算/估计/窗口不符时零新reservation/启动/发送；顾问无lead权限。
- [ ] 两Run争用、Commander保护量及全资源向量生效，不留半预留；跨库phase与补偿边界明确。
- [ ] admit/activate丢回复只读精确查原request/key；missing/unknown不新claim、不退款，不接受caller admitted或receipt。
- [ ] 生产 factory可直接给#110供证；测试fixture无法被当真实来源，源/授权变化立即使当前权威拒绝。
- [ ] C公共行为/真实SQLite与Capacity反例、相关回归、当前CI及独立审查。
本票不执行native/provider；#93 B transport和#93 C真实批准整链另负责。

已完成：无。
剩余工作：上述全部条件；本票之外的父要求继续保留。
阻塞：依赖下列发布记录中的当前候选接口/资格，解除后由原分派模型执行。未完成前保持Open，CI与独立审查通过但未合入dev时标status:awaiting-merge。