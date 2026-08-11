# 运维手册

首次部署先复制 `.env.example`，生成随机普通 API Key 和至少 32 字符的 cursor secret，运行 `uv run ip-pool doctor`，再启动 Redis、API、collector、checker。生产环境每个角色独立扩缩容；API 和 checker 可水平扩展，Redis Lua 保证限流和租约一致性。collector 源有共享健康退避，避免故障源被持续请求。

探针：`/health/live` 仅表示进程存活，`/health/ready` 会 ping Redis，`/metrics` 输出 Prometheus 文本。建议告警包括 readiness 连续失败、Redis error 增长、熔断器长期 open、leased 数持续增长、可用池降至阈值以下、采集连续失败和 worker heartbeat 过期。不要把 endpoint 或错误正文作为指标标签。

Redis 开启 AOF 并使用命名 volume。备份前执行 Redis 一致性快照或云服务备份；恢复到隔离实例后先运行 doctor，再启动 API，最后启动 collector/checker。升级顺序为 Redis 兼容性检查、API、checker、collector。Lua 脚本没有独立部署状态，随应用代码执行。

故障处理：目标站故障会打开熔断器且不扣代理分；Redis 故障时 API readiness 为 503、管理员限流拒绝请求、worker 停止取得有效工作。SIGINT/SIGTERM 设置 stop event，停止新领取，等待在途任务，释放租约并关闭客户端。超过 30 秒 grace 的任务会取消，过期租约随后可回收。

## 仪表盘运行与访问

页面地址为 `http://127.0.0.1:8000/dashboard`，复用 API 服务和端口。默认回环绑定是安全边界；在另一台运维终端上使用时，优先接入组织 VPN/内网反向代理，也可以建立 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 host
```

隧道建立后在本机访问同一地址。不要把 Compose 端口改成 `0.0.0.0:8000` 作为临时替代。

五分钟历史每个 scope 最多 576 点，小时历史最多 720 点；HASH 和 ZSET 会在写入时同步清理。最新快照超过 15 分钟时页面标记“数据延迟”，超过 30 分钟标记“监测中断”。worker 最新心跳在两个心跳间隔内为 healthy，超过后为 stale，TTL 到期后为 down。关闭 `IP_POOL_DASHBOARD__ENABLED` 会停止页面、接口、心跳和新快照，但不会删除既有历史；如需清理，应先备份，再针对当前 `IP_POOL_REDIS__KEY_PREFIX` 下的 `dashboard` 命名空间执行明确维护操作。

常用命令：

```bash
docker compose ps
docker compose logs --tail=200 api checker collector
curl -fsS http://127.0.0.1:8000/health/ready
docker compose stop -t 30
docker compose down              # 默认保留 redis-data volume
docker compose down --volumes    # 会删除数据，仅在明确需要时使用
```
