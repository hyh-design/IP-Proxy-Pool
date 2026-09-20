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

仪表盘默认启用。短期历史每个域名最多 576 个五分钟点，长期历史最多 720 个小时点；`MAX_AGGREGATE_RECORDS` 达到上限时接口返回 `partial: true`，页面明确标注“部分样本”。关闭功能不会删除 `dashboard` 命名空间下的历史数据。
