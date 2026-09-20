# IP Proxy Pool

一个面向 Python 3.11+ 的安全代理池：从公开免费代理源采集候选，预测试后按目标域名保存到 Redis，使用租约驱动的并发检测器维护质量，并通过受认证、限流且有界的 FastAPI 接口提供查询。

> 免费代理是不可信输入。它们可能窃听明文流量、篡改响应、记录凭据或突然失效。不要通过免费代理传输账号、Cookie、令牌、隐私数据或生产机密；本项目也不承诺代理的合法性、匿名性或可用性。

## 快速开始

安装 [uv](https://docs.astral.sh/uv/) 和 Docker 后：

```bash
cp .env.example .env
# 替换 API Key 和至少 32 字符的 cursor secret
uv sync --frozen
docker compose up -d redis
uv run ip-pool doctor
uv run ip-pool all
```

生产部署应拆分角色：

```bash
uv run ip-pool api
uv run ip-pool collect --region all
uv run ip-pool check
```

`ip-pool all` 只适合本地开发。`ip-pool doctor` 输出单个 JSON 诊断对象。旧版 ZSET 可先演练再导入：

```bash
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org --dry-run
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org
```

升级选择索引时，先演练再正式重建。该命令会同时重建低延迟选择索引和优先复测索引：

```bash
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com --dry-run
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com
```

API 就绪检查和 Checker 启动都要求两个索引已完成重建；升级部署时应先运行正式重建命令，再启动 API 与 Checker。

## Compose

```bash
docker compose build
docker compose up -d redis api collector checker
curl http://127.0.0.1:8000/health/ready
docker compose down
```

Redis 没有宿主端口，API 只映射 `127.0.0.1:8000`。Compose 不内置 API 密钥；必须通过 `.env` 或部署平台 secret 注入。可用 `--profile observability` 启动 Prometheus。

## 监测页面

API 启动后访问 [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)，输入 `.env` 中配置的普通只读 API Key。页面重点展示可用率、代理池数量、IP 质量、来源质量和 collector/checker 状态，不提供删除、探测或其他管理操作。

历史范围可选 `1h / 6h / 12h / 24h / 7d / 30d`：24 小时以内使用 5 分钟快照，7 天和 30 天使用小时快照。页面默认每 30 秒刷新实时面板，标签页隐藏时暂停；代理明细默认折叠，展开后才按状态、评分和来源分页读取。

页面自身不增加 Compose 服务或端口，也不依赖 Grafana/Prometheus。API 仍只监听回环地址；其他机器应通过组织已有的 VPN、受控反向代理或 SSH 隧道访问，不能直接把 Uvicorn 暴露到局域网或互联网。

## API

健康检查 `/health/live`、`/health/ready` 和 `/metrics` 匿名可访问。业务路由默认要求 `X-API-Key`：

```bash
curl -H 'X-API-Key: YOUR_KEY' http://127.0.0.1:8000/v1/domains
curl -H 'X-API-Key: YOUR_KEY' 'http://127.0.0.1:8000/v1/proxies?domain=httpbin.org&limit=20'
curl -H 'X-API-Key: YOUR_KEY' 'http://127.0.0.1:8000/v1/proxies/random?domain=httpbin.org&count=5&min_score=90'
curl -H 'X-API-Key: YOUR_KEY' http://127.0.0.1:8000/v1/stats
```

随机接口会强制执行服务端热池下限：默认要求评分至少 90、连续成功 2 次、最近 10 分钟验证过且 EWMA 延迟不超过 5 秒。候选代理的两次成功检测至少间隔 30–60 秒，每轮还必须同时通过主目标和国内可达的 `myip.ipip.net` 交叉验证。调用方可以提出更严格的条件，不能降低服务端下限。

大企汇调用固定使用 `max_latency_ms=1000` 硬上限；合格代理不足时返回实际数量或空列表，不会放宽延迟，也不会回退到服务器真实出口。监控页的“可用代理”表示健康状态，“当前可选”表示同时满足评分、延迟、新鲜度和连续成功次数的代理。

查询调用方应把实际结果反馈给代理池，使坏代理立即退出热池：

```bash
curl -X POST -H 'Content-Type: application/json' -H 'X-API-Key: YOUR_KEY' \
  -d '{"domain":"httpbin.org","endpoint":"1.1.1.1:80","outcome":"proxy_error","error_type":"connection_reset"}' \
  http://127.0.0.1:8000/v1/proxies/feedback
```

分页最多 200 条，随机最多 20 条。每个目标域名默认保留最多 10,000 条记录，并优先清理隔离、低分和陈旧记录。采集和检测默认排除 Cloudflare 官方代理网段；业务调用反馈失败后立即隔离 6–24 小时，检测连续失败 5 次后清理。管理员探测和旧兼容路由默认关闭。错误响应不会返回 Redis、网络或代理内部异常文本。

## 文档

- [架构与数据流](docs/architecture.md)
- [配置清单](docs/configuration.md)
- [安全边界](docs/security.md)
- [运行、扩容和故障处理](docs/operations.md)
- [旧数据迁移](docs/migration.md)
- [Codegraph 使用](docs/codegraph.md)
- [仪表盘验收记录](docs/verification.md)

项目使用 MIT License。
