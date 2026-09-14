# R8-P1-02：实际 Workflow 配置包与同源编译预览

## 问题与结果

在已合入的项目/会话认证 API 中保存真实声明式配置包，从这些文件生成同版本流程图、角色/步骤表和差异；无需目标 Run 即可创作/修改，不再将新职责映射回固定 Commander/Worker/Reviewer 枚举。

## 范围

依据 architecture/09、10、11，FR22–24 的控制面子集。新增 karajan.workflows 的 definition/bundle/compiler 服务、web/workflows.py 与 create_app 最小注册。首版支持明确版本 schema 的 manifest.json、workflow.yaml、roles 文件；安全解析 YAML，拒绝任意可执行表达式与未知执行种类。新增依赖须更新并核对锁文件。使用受信任注册表，注册项不能由请求载荷导入任意模块。

此票实现配置编辑/编译服务及 API，不声称模型已经从文字生成配置或页面已完成对话 Designer；后者另有验收。与新 gateway 目录按固定引用衔接，未核验来源可作为草稿但明确不可执行。为03的真实ready样例提供一个实际注册的本机确定性 `artifact_aggregate` 适配器：仅按类型化输入合并文本为报告产物，无模型、shell、任意导入或外部副作用，并以真实调用测试证明能力；仅注册schema不算适配器可用。

## 验收条件

- [ ] AC1：项目内创建配置候选并持久化实际 manifest/workflow/role 文件；重新打开/重启可取得原文件字节、文件/bundle 摘要与 immutable revision。路径穿越、绝对路径、symlink/junction 逃逸、重复规范化路径和不合法清单拒绝。
- [ ] AC2：编译已登记执行种类、自定义角色、输入输出、静态依赖、声明式条件及允许的动态扩展/调度边界；缺引用、依赖环、不兼容输出或未注册执行种类返回定位明确的诊断，不猜成 Worker。合法超过100节点的配置可完整编译/读取，不受旧 Plan 的100项上限或默认人数截断。
- [ ] AC3：文件、角色/步骤表、Mermaid/结构化图和 diff 同源，绑定 bundle digest、compiler revision 和模板 compiled digest；模板摘要不混入某次 Run 输入。可视文本安全转义，不能让角色名注入 Mermaid/HTML。
- [ ] AC4：表格/文件编辑直接写新 revision 并确定性编译，不调用模型；文字仅持久为创作输入/明确待生成状态，不能伪造已生成。旧 expected revision 冲突，迟到写入不覆盖新候选；相同幂等请求不多建版本。
- [ ] AC5：HTTP 复用项目/会话归属和 Session/CSRF；覆盖两个不同角色/拓扑的配置、非法路径/图、超过100节点、直接编辑无模型调用、重启读回及旧版本冲突。给出可运行的 API/文件示例，Ruff/mypy/快速门通过。

## 非目标

不激活配置、不启动 Run/模型、不实现任意脚本引擎、拖拽画布或完整 Designer 模型调用；不修改已批准旧 Run。report/patch/pr 必需目标政策不能因自定义名称而绕过，尚无执行适配器的种类显示 unavailable。

## 交付

分支 codex/r8-p1-02-workflow-bundles；从本批设计基线开始，按01之后合入dev。新增独立模块，create_app 只作注册；避免旧 #172/#173 业务改动。Claude Code CLI 默认 gemini-use worker实现并提交PR，由root审核合并；本批不超过4PR，返修沿用原PR。保存范围明确的实现证据，PROGRESS.md仅本机检查点。

Parent: #174
Refs #1
