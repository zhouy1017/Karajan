# R8-P1-01 网关目录样例

实现说明见 [R8-P1-01 网关目录](../../docs/implementation/r8-phase1-gateway.md)。本目录提供一个可复现操作入口，证明控制面行为，**不**证明真实 provider 资格、推理或完整 r8。

```text
python examples/gateway/catalog_probe.py
```

脚本在同一进程内启动一个仅绑定 `127.0.0.1` 的 HTTP 上游，通过真实认证边界（Session + Origin + CSRF + `Idempotency-Key`）登记连接与固定版本绑定，并执行一次无推理目录探测。实际输出（2026-09-14，Windows 11 + Python 3.12.14）：

```text
connection: 201 http://127.0.0.1:62984
binding:   201 True False        # 已声明身份, 未核验
probe:     201 ok ['vendor-model-a', 'vendor-model-b']
eligible:  False
upstream requests: ['/v1/models'] (one read, no inference)
```

## 边界

| 项目 | 事实 |
|---|---|
| 真实 provider 调用 | 无 |
| 推理/生成请求 | 无（脚本断言上游只收到一次 `GET /v1/models`） |
| 凭据 | 由控制器本地文件解析；不上传、不返回、不落库 |
| 探测路径 | 由 `openai_compatible` 协议族派生，非调用方指定 |
| 执行资格 | 不授予；`execution_eligible=false`、`verified=false` |
| 证据层级 | C/P（本机控制面行为）；**不**宣称 S 资格 |

脚本使用 `TestClient` 而不是真实网络端口以保持确定性；同一套路由在 `python -m karajan.web serve` 下由 `create_app` 注册，认证、来源校验和 CSRF 中间件相同。
