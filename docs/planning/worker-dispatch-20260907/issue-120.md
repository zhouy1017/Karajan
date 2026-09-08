Related: #10，PR #118 / PR #119。执行worker：gpt-5.6-luna；集成到terra候选由terra负责。独立reviewer：gpt-6-astra high。此为当前交付被CI前置下载间歇失败阻断的有界修复，不更改产品模型/隔离/资产来源。

## 观察输入
- PR118 head e533163，Windows job101637443807在官方tokenizer准备阶段报TOKENIZER_PROVISION_FAILED；同SHA push Windows相同步骤通过。
- PR119 head92934b5，Ubuntu push job101638715797亦在同一步骤失败。
现有脚本只有单次下载，未知异常被压成一个reason。当前证据支持外部瞬态获取失败；具体HTTP/网络原因未可见，不能伪称已经定位CDN错误。

## 范围
仅 `.github/scripts/provision_go_tokenizer.py`、专属tests/tools/test_go_tokenizer_provision.py的相关测试及故障/修复说明。可以新增一个不依赖大tokenizer资产的错误/重试测试文件。不改workflow门禁、固定revision/URL/size/SHA、runtime模块、认证/代理策略或锁文件。

## 验收
- [ ] 在不输出URL/headers/response body/本地路径的前提下，区分稳定脱敏错误类别；HTTP可记录数字状态码，拒绝任意异常字符串。
- [ ] 仅连接/读取过程中明确瞬态网络错误或429/5xx进行有限重试，建议最多3次；全部尝试共享已有总时间预算，重试不把180秒乘以次数。
- [ ] 长度/摘要不符、不安全redirect、文件系统/权限等确定性错误不盲重试；失败仍非零，缺资产不skip。
- [ ] 每次失败清理自己临时文件，旧文件不覆盖；只有固定size+SHA验证后原子发布，已验证文件零网络请求。
- [ ] 离线可控反例覆盖瞬态后成功/持续失败/超总期限/不可重试/错误脱敏，原artifact边界回归通过；不借真实下载刷测试。
- [ ] 记录当前失败与同SHA对照；最新候选CI仍须实际通过，不能仅用重试功能存在作完成证据。
- [ ] GPT-6 high独立Standards/Spec及固定候选静态/CI通过，PR合入dev后方可关闭。

已完成：只读对照证据。
剩余工作：以上范围的实现、验证、当前CI；#114暂待该修复完成后续做。
阻塞：无；不新增任何模型/现金调用，公开固定资产HTTP与当前CI准备一致。