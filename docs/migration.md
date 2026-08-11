# 从旧版迁移

旧版 Redis ZSET 只有 endpoint 和 score，缺少域名、来源、时间和失败分类，不能直接作为新版可用记录。导入器只读扫描旧 key，接受合法全局 IP 和端口，把 score 限制到 0～70，写成 `candidate` 并立即安排复检。

先备份并演练：

```bash
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org --dry-run
uv run ip-pool import-legacy --redis-key proxies --domain httpbin.org
```

输出是包含 scanned、accepted、skipped_invalid、skipped_non_global、written 的 JSON。重复运行不会创建重复记录。导入器不会对旧 key 调用 DEL、ZREM 或 ZADD；类型、成员、分数和 TTL 保持不变。

回滚只需停止新版角色并继续使用旧系统，因为旧 key 未被修改。若要清理新版数据，只删除配置前缀下目标域名的新版 keys，必须先核对前缀和备份；不要删除旧 key 或整个 Redis 数据库。兼容 API 默认关闭，临时开启后仍需认证、限流并最多返回 200 条，不提供旧 `/test`。
