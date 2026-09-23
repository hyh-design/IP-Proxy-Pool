# 配置

Pydantic Settings 使用 `IP_POOL_` 前缀和双下划线嵌套。列表/元组写成 JSON。`.env.example` 是唯一提交的环境模板，真实 `.env` 被 Git 和 Docker build 排除。

| 环境变量 | 说明 |
|---|---|
| `IP_POOL_ENVIRONMENT` | 环境名 |
| `IP_POOL_REDIS__URL` | Redis DSN |
| `IP_POOL_REDIS__KEY_PREFIX` | 所有新版 key 前缀 |
| `IP_POOL_REDIS__CONNECT_TIMEOUT_SECONDS` | 建连超时 |
| `IP_POOL_REDIS__READ_TIMEOUT_SECONDS` | 读取超时 |
| `IP_POOL_API__HOST` | API 监听地址；本机默认回环 |
| `IP_POOL_API__PORT` | API 端口 |
| `IP_POOL_API__AUTH_ENABLED` | 是否启用 Key 认证 |
| `IP_POOL_API__API_KEYS` | 普通 Key JSON 数组 |
| `IP_POOL_API__ADMIN_API_KEYS` | 管理员 Key JSON 数组 |
| `IP_POOL_API__CURSOR_SECRET` | 至少 32 字符的游标签名 secret |
| `IP_POOL_API__MAX_PAGE_SIZE` | 最大分页条数 |
| `IP_POOL_API__QUERY_RATE_LIMIT` | 普通窗口请求数 |
| `IP_POOL_API__ADMIN_RATE_LIMIT` | 管理窗口请求数 |
| `IP_POOL_API__RATE_WINDOW_SECONDS` | 限流窗口秒数 |
| `IP_POOL_API__ADMIN_PROBE_ENABLED` | 是否挂载管理员探测 |
| `IP_POOL_API__LEGACY_ROUTES_ENABLED` | 是否挂载只读兼容路由 |
| `IP_POOL_RECLAIM_QUOTA__ENABLED` | 双系统捡回额度发放方开关；仅系统一启用，默认 false |
| `IP_POOL_RECLAIM_QUOTA__API_KEYS` | 两台捡回系统各自独立的 QUOTA_CLIENT Key JSON 数组；启用时恰好两个 |
| `IP_POOL_OWNERSHIP__ENABLED` | 双系统捡回归属协调开关，默认 false；须待双方客户端就绪后单独批准启用 |
| `IP_POOL_OWNERSHIP__MEMBERS` | 成员 ID 到 `{system_id,api_key}` 的 JSON 映射，每个启用账号独立 Key，仅 API 服务读取 |
| `IP_POOL_OWNERSHIP__RATE_LIMIT` | 归属路由每 Key 独立请求速率上限，默认 60 |
| `IP_POOL_PEER_EXPORT__ENABLED` | 正式池只读导出开关，默认 false |
| `IP_POOL_PEER_EXPORT__NODE_ID` | 本机唯一节点 ID；启用导出时必填 |
| `IP_POOL_PEER_EXPORT__API_KEYS` | 专用 PEER_EXPORT Key JSON 数组，不得与其他角色共用 |
| `IP_POOL_PEER_EXPORT__RATE_LIMIT` | 导出独立固定窗口请求上限 |
| `IP_POOL_PEER_CACHE__ENABLED` | 对端代理短期缓存开关，默认 false |
| `IP_POOL_PEER_CACHE__PEER_NAME` | 对端缓存命名空间名称 |
| `IP_POOL_PEER_CACHE__ORIGIN_NODE` | 预期对端 node_id，不得等于本机 node_id |
| `IP_POOL_PEER_CACHE__BASE_URL` | 对端经 SSH 回环隧道映射的 API 地址 |
| `IP_POOL_PEER_CACHE__API_KEY` | 对端签发的专用 PEER_EXPORT Key |
| `IP_POOL_PEER_CACHE__MIN_SCORE` | 缓存最低评分，最终下限不低于 90 |
| `IP_POOL_PEER_CACHE__MAX_LATENCY_MS` | 缓存最大延迟，最终上限不高于 2000ms |
| `IP_POOL_PEER_CACHE__MAX_CHECKED_AGE_SECONDS` | 缓存最大校验年龄，最终不高于 600 秒 |
| `IP_POOL_PEER_CACHE__MIN_CONSECUTIVE_SUCCESSES` | 缓存最低连续成功次数，最终不少于 2 |
| `IP_POOL_PEER_CACHE__SYNC_INTERVAL_SECONDS` | 同步间隔，默认 60 秒 |
| `IP_POOL_PEER_CACHE__CACHE_TTL_SECONDS` | 缓存 TTL，默认 180 秒 |
| `IP_POOL_PEER_CACHE__PROXY_COOLDOWN_SECONDS` | 代理失败禁用期，默认 600 秒 |
| `IP_POOL_PEER_CACHE__MAX_ITEMS` | 每域每对端最多缓存 20 条 |
| `IP_POOL_PEER_ALERTS__ENABLED` | 代理缓存告警开关；启用缓存时必须为 true |
| `IP_POOL_PEER_ALERTS__WEBHOOK_URL` | API 专用 HTTPS 钉钉告警 Webhook；仅 API 角色读取 |
| `IP_POOL_COLLECTOR__CONCURRENCY` | 采集/预测并发 |
| `IP_POOL_COLLECTOR__MAX_PAGES_PER_SOURCE` | 单源最大页数 |
| `IP_POOL_COLLECTOR__MAX_RESPONSE_BYTES` | 单响应最大字节数 |
| `IP_POOL_COLLECTOR__MAX_PROXIES_PER_SOURCE_ROUND` | 单源单轮候选上限 |
| `IP_POOL_COLLECTOR__MAX_POOL_SIZE_PER_DOMAIN` | 单目标域名代理软上限；达到后只更新已有记录 |
| `IP_POOL_COLLECTOR__COLLECTION_INTERVAL_SECONDS` | 常规采集间隔秒数，默认 300 |
| `IP_POOL_COLLECTOR__INVENTORY_CHECK_INTERVAL_SECONDS` | 热池库存检查间隔秒数，默认 60 |
| `IP_POOL_COLLECTOR__LOW_INVENTORY_THRESHOLD` | 低库存补采阈值，默认 20；设为 0 可关闭 |
| `IP_POOL_COLLECTOR__LOW_INVENTORY_MIN_SCORE` | 低库存统计最低评分，默认 80，并受全局热池规则约束 |
| `IP_POOL_COLLECTOR__LOW_INVENTORY_MAX_LATENCY_MS` | 低库存统计最大延迟毫秒数，默认 2000，并受全局热池规则约束 |
| `IP_POOL_COLLECTOR__LOW_INVENTORY_MAX_NEW_CANDIDATES` | 单轮低库存补采允许临时新增的候选上限，默认 500；采集结束后恢复总池上限 |
| `IP_POOL_CHECKER__CONCURRENCY` | 检测并发 |
| `IP_POOL_CHECKER__BATCH_SIZE` | 单次领取上限 |
| `IP_POOL_CHECKER__LEASE_SECONDS` | 租约秒数 |
| `IP_POOL_CHECKER__REQUEST_TIMEOUT_SECONDS` | 检测请求超时 |
| `IP_POOL_CHECKER__BREAKER_FAILURES` | 熔断连续失败阈值 |
| `IP_POOL_CHECKER__BREAKER_COOLDOWN_SECONDS` | 熔断冷却时间 |
| `IP_POOL_CHECKER__DROP_AFTER_FAILURES` | 连续代理失败达到该次数后清理记录 |
| `IP_POOL_SELECTION__MIN_SCORE` | 热池最低评分，调用方不能降低 |
| `IP_POOL_SELECTION__MAX_LATENCY_MS` | 热池最大 EWMA 延迟毫秒数 |
| `IP_POOL_SELECTION__MAX_CHECKED_AGE_SECONDS` | 热池最近验证时间上限（秒） |
| `IP_POOL_SELECTION__MIN_CONSECUTIVE_SUCCESSES` | 进入热池所需连续成功次数 |
| `IP_POOL_SECURITY__ALLOW_NON_GLOBAL_PROXIES` | 受控开发环境才可放宽代理地址 |
| `IP_POOL_SECURITY__ALLOWED_PROBE_HOSTS` | 管理探测主机 JSON 数组 |
| `IP_POOL_SECURITY__ALLOW_REDIRECTS` | 重定向开关；安全默认 false |
| `IP_POOL_SECURITY__BLOCKED_PROXY_NETWORKS` | 不作为代理接收的 CIDR JSON 数组；默认包含 Cloudflare 官方 IPv4 网段 |
| `IP_POOL_OBSERVABILITY__LOG_LEVEL` | 日志级别 |
| `IP_POOL_OBSERVABILITY__JSON_LOGS` | JSON 日志开关 |
| `IP_POOL_OBSERVABILITY__METRICS_ENABLED` | Prometheus 指标开关 |
| `IP_POOL_DASHBOARD__ENABLED` | 是否挂载监测页面和 JSON 接口；关闭后保留已有历史数据 |
| `IP_POOL_DASHBOARD__SNAPSHOT_INTERVAL_SECONDS` | 聚合快照间隔，默认 300 秒 |
| `IP_POOL_DASHBOARD__SHORT_RETENTION_HOURS` | 5 分钟历史保留小时数，默认 48 |
| `IP_POOL_DASHBOARD__LONG_RETENTION_HOURS` | 小时历史保留小时数，默认 720 |
| `IP_POOL_DASHBOARD__HEARTBEAT_INTERVAL_SECONDS` | collector/checker 心跳间隔秒数 |
| `IP_POOL_DASHBOARD__HEARTBEAT_TTL_SECONDS` | worker 心跳存活秒数，至少为心跳间隔两倍 |
| `IP_POOL_DASHBOARD__REFRESH_SECONDS` | 页面实时数据默认刷新秒数 |
| `IP_POOL_DASHBOARD__MAX_AGGREGATE_RECORDS` | 单次质量聚合最多扫描的代理记录数 |
| `IP_POOL_TARGET__NAME` | 代理可用性检测目标名称 |
| `IP_POOL_TARGET__URL` | 代理可用性检测 URL |
| `IP_POOL_TARGET__DOMAIN` | 代理按目标域名隔离存储的键 |
| `IP_POOL_TARGET__EXPECTED_STATUSES` | 允许的 HTTP 状态码 JSON 数组 |
| `IP_POOL_TARGET__EXPECTED_TEXT` | 可选的响应正文校验文本；留空表示不校验 |
| `IP_POOL_TARGET__JSON_KEYS` | 必须存在的 JSON 字段 JSON 数组；普通网页使用空数组 |
| `IP_POOL_TARGET__TIMEOUT_SECONDS` | 单次代理检测超时 |
| `IP_POOL_TARGET__VALIDATION_TARGETS` | 交叉验证目标 JSON 数组；默认同时验证国内可达的 `myip.ipip.net` |

API 启动会校验 Key 唯一性、管理员探测依赖和 cursor secret。容器中的 `IP_POOL_API__HOST=0.0.0.0` 仅用于容器监听，Compose 宿主映射仍限制为 `127.0.0.1`。

`peer-cache` profile 使用独立的受限文件（默认位置 `deploy/secrets/`，该目录不进入 Git/镜像）：

- `peer-api.env` 仅注入 API，放入本机缓存身份、启用开关和 `IP_POOL_PEER_ALERTS__WEBHOOK_URL`，不要放对端导出 Key。此文件可缺省，但启用缓存时 API 启动校验会要求 HTTPS Webhook。
- `peer-sync.env` 仅注入同步进程，放入缓存身份、目标域名、`IP_POOL_PEER_CACHE__BASE_URL=http://peer-tunnel:8000` 和对端专用 `IP_POOL_PEER_CACHE__API_KEY`；缺失时 profile 启动失败。不要放普通/管理员 Key、Webhook 或账号凭据。
- `peer-tunnel.conf` 是 OpenSSH 客户端配置，仅定义 `Host peer-export` 的 `HostName`、`User proxy-peer`、`IdentityFile /run/peer-ssh/id_ed25519`、`UserKnownHostsFile /run/peer-ssh/known_hosts`；与专用 `peer-tunnel.key`、经指纹核验的 `peer-tunnel.known_hosts` 一并只读挂载。源文件应限制为 root 可读，容器内隧道进程仅为读取该文件使用 root，不能继承应用 `.env`。

三个文件路径可分别由 `IP_POOL_PEER_SYNC_ENV_FILE`、`IP_POOL_PEER_SSH_CONFIG_FILE`、`IP_POOL_PEER_SSH_KEY_FILE`、`IP_POOL_PEER_KNOWN_HOSTS_FILE` 在 Compose 命令环境中覆盖；API 文件路径为 `IP_POOL_PEER_API_ENV_FILE`。这些是 Compose 输入路径，不是应用设置；不要把文件内容写进命令行。默认 Compose 不启动 peer 服务，且无需这些文件。profile 不发布任何新宿主端口。

全局捡回额度入口为 `POST /v1/reclaim/quota/acquire`，仅接受 `portal.daqihui.com` 的 UUIDv4 尝试号。两端共用系统一 Redis 中的任意连续 60 秒 5 次许可；Redis 实例启动后的前 65 秒拒绝发放。该功能与代理缓存开关独立，不能把普通业务 Key 用作额度 Key；系统二仅通过受限 SSH 回环转发访问系统一 API。

对端只读导出为 `GET /v1/peer/proxies`，只访问正式池索引和记录，不读写 peer-cache 键；导出关闭时返回 503。仅 PEER_EXPORT Key 能访问，且与普通查询、管理员及额度权限完全隔离。导出最多 20 条，按请求、本机正式策略和缓存硬边界取最严格条件。同步源不得退回普通随机接口。

仪表盘默认启用。短期历史每个域名最多 576 个五分钟点，长期历史最多 720 个小时点；`MAX_AGGREGATE_RECORDS` 达到上限时接口返回 `partial: true`，页面明确标注“部分样本”。关闭功能不会删除 `dashboard` 命名空间下的历史数据。
