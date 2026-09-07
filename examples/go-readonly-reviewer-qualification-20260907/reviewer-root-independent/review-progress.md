# #106 Root 5 文件独立审查（待当前 guard 最终复验）

审查者 capacity_facts；不是这5文件的作者。此前与实现者对齐过 Journal/Relay seam，且本人实现低层2模块，本报告不独立审查本人低层代码。Standards和Spec由同一独立reviewer分别评估，不虚称两个独立reviewer。

固定范围：projects/qualification.py、orchestration/go_reviewer_scope.py、reviewer_binding.py、qualification_services.py、check_services_factory.py。依据已发布#106规格与实现seams、原#95责任边界、AGENTS/Issue证据流程。所有产物仅本.cache目录，未修改任何产品、正式测试、Git或远端。

Standards：其余4模块未发现确认违反。scope通过单一公开resolver在membership前生效；原Binding DTO不混入新可信身份，profile_limits为JSON list；配置采用显式v2，旧v1形状保持；source factory复用现有同Project资格/credential/Journal，history不构造当前assets，公开输入不能选择provider或传已通过report。

Spec：固定3场景start、原seal/record、同project Worker/Reviewer共存、fixture不导出角色、原资源/候选/Review无效果、current source/generation、限定T1+read+I/O/C/margins的实现可追溯。存在root自查且本review代码确认的mid-suite authority缺口，正由Store current_guard→Suite/observer→既有Relay send_guard修复。独立首轮混合版本helper失败不作产品红；当前正常/credential revoke/Profile禁用3项C已通过3.70s，完整原文见current-three.xml/stdout。

额外实证 P2 CURRENT-EXPIRY-001：current_guard先读时点，再做source/credential I/O，yield紧前没有重新核原expiry。公开Store＋具体Suite＋observer替身调用真实guard，在source读期间将可信clock1000推进1700，原expiry1600，仍记录1次过期guarded effect。随后Suite/Store正确failed并撤销，因此没有过报成功资格；但不满足新效果期限前置。effect-expiry-before.xml/stdout为真实单例红，源520c5802逐字节存档。最小修复是最终yield紧前复核同一原始时间窗；不延长grant，不要求重做整个架构。此证据只C端口，不是实际namespace/provider超时发送。

待root最后的inflight revoke/latest guard＋expiry修复冻结后，复验正常/credential revoke/Profile禁用/qualification revoke/latest unknown及expiry合计6个C，不重跑作者大组或声称P/S/G已通过。S仍属于#107；本票不实现ReviewerTask/ReviewEvidence。
