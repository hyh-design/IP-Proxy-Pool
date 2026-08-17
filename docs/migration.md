# 从旧版迁移

旧版 Redis ZSET 只有 endpoint 和 score，缺少域名、来源、时间和失败分类，不能直接作为新版可用记录。导入器只读扫描旧 key，接受合法全局 IP 和端口，把 score 限制到 0～70，写成 `candidate` 并立即安排复检。

先备份并演练：

```bash
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org --dry-run
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org
```

输出是包含 scanned、accepted、skipped_invalid、skipped_non_global、written 的 JSON。重复运行不会创建重复记录。导入器不会对旧 key 调用 DEL、ZREM 或 ZADD；类型、成员、分数和 TTL 保持不变。

回滚只需停止新版角色并继续使用旧系统，因为旧 key 未被修改。若要清理新版数据，只删除配置前缀下目标域名的新版 keys，必须先核对前缀和备份；不要删除旧 key 或整个 Redis 数据库。兼容 API 默认关闭，临时开启后仍需认证、限流并最多返回 200 条，不提供旧 `/test`。

## 重建可用延迟索引

现有记录迁移到精确低延迟选择时，使用幂等重建命令：

```bash
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com --dry-run
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com
```

输出 JSON 包含 `domain`、`scanned`、`indexed`、`ignored`、`dry_run` 和 `duration_seconds`，不会包含代理认证信息。`--dry-run` 只扫描统计，不修改正式索引或就绪标记。正式执行先写唯一临时 ZSET，完整扫描成功后通过 Lua 原子替换正式索引并写入版本标记；即使没有合格代理，也会保留“已完成构建”的状态。

正式切换前必须短暂停止 `api`、`checker` 和 `collector`，避免旧进程在扫描期间改变记录。失败时临时键会清理，正式索引不切换；恢复旧镜像即可回滚。大企汇服务无需停止，但代理 API 不可用期间必须跳过任务，禁止真实 IP 回退。
