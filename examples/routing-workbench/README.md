# 路由模拟工作台验证

本目录记录固定快照模拟 HTTP、页面和独立审查的实际结果。它关联 [M3-01 #23](https://github.com/zhouy1017/Karajan/issues/23)，不关闭整个路由、可信快照组装或统一派发要求。

- [接口行为与验证范围](api-contract.md)、[作者来源绑定](api-verification.json)：21 项 API、新增重复键拒绝及非空状态无副作用检查，保留实际红绿报告。
- [Root API Standards](root-api-standards.json)：Windows Web/routing 137 passed、WSL2 API 21 passed、全仓 Ruff 与后端 strict mypy。
- [最终前端报告](frontend-final.junit.xml)：138 passed，包括 48 项模拟交互；此报告在数字导入与窄屏修复后执行。
- [真实浏览器观察](browser-verification.json)：8 个操作/显示观察，含前后状态快照和最终构建摘要。页面计数不充当零模型请求的证据。
- [独立 Spec](review-fixes/spec-review.json)：21 项原 HTTP、10 项独立 HTTP、修复数字边界前的 36 项 UI 检查。之后发现的数值问题及其最终复验单独保留在 `ui-standards-*`，不改写先前测试历史。
- [最终 UI Standards](review-fixes/ui-standards-review.json)：再次独立通过两个原输入、4 项安全整数边界及 48 项交互，无遗留发现。
- `review-fixes/label-prototype.*`：原合法输入造成渲染失败，修复后同一用例通过。
- `review-fixes/ui-standards-overflow.*`：原 `1e400` 在界面被改成 `null`，真实原始 HTTP 拒绝与改写后错误选中有对照；最终在导入时拒绝。
- `review-fixes/ui-standards-unsafe-integer.*`：整数舍入改变资格摘要的 HTTP 对照；该 UI 用例首次运行已遇到进行中的修复，因此不冒称旧 UI 红绿记录。

候选排序、请求大小和重复键属于静态预审发现并以回归测试闭合。Spec 首轮一次失败来自审查者写错原因码断言，原报告仍保留，不将它算作产品缺陷。

每份报告保留自己实际执行时的来源摘要；早期报告不暗示覆盖之后的源码。`freeze.report.json` 在最终提交前绑定当前产品源、测试与报告文件，工作树与 Git 暂存字节差异另行核对。

范围始终为 `explicit_simulation` / `activation_allowed=false`；真实来源资格是 `not_run`，没有现金 API 调用。HTTP 副作用证据覆盖实际存在的项目、Run、容量三库和绑定本地接收器，没有宣称独立 Host 或现金账参与该测试。
