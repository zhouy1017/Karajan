# OpenCode runtime pin

`package.json` 与 `package-lock.json` 固定官方 `opencode-ai@1.18.29`。可复现安装命令：

```text
npm ci --prefix runtimes/opencode --no-audit --no-fund
```

不要用自动更新替换此执行版本。Python 探针在隔离环境中运行 `--version`，版本不符拒绝。
该版本的入口在所有系统均为 `node_modules/opencode-ai/bin/opencode.exe`。
名称来自 npm 包的 `bin` 字段及 `postinstall.mjs` 的固定目标；Linux 文件内容为本平台二进制，不能按后缀推断格式。
安装工具自身需要读取 npm registry；安装完成后的模型探针只配置本地模拟服务。

此依赖不提供 Karajan 的工具沙箱或现金准入资格。固定版本依据官方 npm 包和
[OpenCode release v1.18.29](https://github.com/anomalyco/opencode/releases/tag/v1.18.29)；
实际 server 接口以该二进制 `/doc` 返回的 OpenAPI 为准。

运行与限制见 [M0 API runner](../../docs/implementation/m0-api-runner.md)。
