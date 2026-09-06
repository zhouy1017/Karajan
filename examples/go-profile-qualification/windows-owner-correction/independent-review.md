# PR54 owner 修正最终独立查读

结论：此有界差异未发现阻塞问题，可交下一次 CI 验证。没有重跑测试，没有改产品或原始证据。

- 产品差异只有 owner 白名单一行和三行解释。可信 owner 通过后继续执行原有完整 DACL 读取/解析、目录 protected 及 ACE 检查，没有早退或新增豁免；POSIX 分支、路径类型/链接/祖先检查均未变。
- 新测试明确只替换 GetNamedSecurityInfoW 的 owner 输出；原实际 DACL、真实 TokenUser/SID 转换、NTFS 文件、SQLite 和公共 Store 流程保留。BA/SYSTEM 两例实际注册、重开、解析精确 generation，再在合成目录实际加入 Everyone ACE，确认仍拒绝。BUILTIN Users/Everyone owner 两例拒绝。没有改真实对象 owner 或 token。
- 原生正控制只创建测试临时目录，输出 object_owner/token_default_owner 的分类，不输出个人 SID 或目录。当前本机两者都是 current_user；BA/SYSTEM 的通过是受控 owner 观察输入，不能写成真实 BA/SYSTEM-owned 文件的实测。
- 原 red XML：5 cases、2 failures，恰为新增可信 owner 两例；final XML：27 cases、0 failures/errors/skips，XML suite time 7.130 秒（终端总耗时另可为 7.21 秒）。这是查读 root 已保存的执行证据，不冒充 reviewer 独立执行。

绑定 SHA256：

| 文件 | SHA256 |
|---|---|
| backend/karajan/projects/credential_sources.py | 75653d59353f53399f3fd41a5894d398a62c1ca92d607351f4682dde341076e1 |
| tests/projects/test_credential_windows_owners.py | 552eb3b240c6a640b120594a1918870cd4211537c7fb46bd65732d968738e212 |

该修改保持 `.cache/pr54-windows-owner-review.md` 已核对的 host admin/SYSTEM 信任边界。新 GitHub Windows CI 的成功及实际 owner 分类尚待发布后确认，不由本机绿色推断。
