# 首轮来源混合，不是产品红

首次 three-case `before.xml`/`before.stdout` 为3失败，控制组也失败。定向 `helper-diagnostic.stdout` 确认旧Suite不接受 `current_guard`，而缓存Store在读取期间已被作者同步到新必需kw；具体 source-before.json 逐字节绑定该混合输入。没有完整旧Store字节，故本次不重造旧Store，不把该失败当作原mid-suite授权缺口的实证，也不把fallback反复重试当效果。

原缺口来自修复前Store入口/结束seal与Suite仅静态ResolvedCredential验证的代码核对，已向root报告并与其自查一致。最终C应针对真实最新Store/具体Suite，以显式source和observer替身在第一场景完成后撤销真实credential或禁用真实Profile，验证禁止进入第二场景、所有旧grant撤销、原command重放无新效果。native/HTTP/provider、只读机制和parser验证不在此C测试范围。
