# 安全说明

免费代理默认不可信。即使健康检查成功，也不能证明匿名性、运营者身份、合法用途或内容完整性。只传输可公开、可丢失的数据，并在应用层使用 TLS；不要发送认证头、Cookie、账号、个人信息和业务秘密。

API 默认开启 Key 认证，使用常量时间比较，只记录 SHA-256 指纹前缀。管理员 Key 与普通 Key 分离，重复或空 Key 会阻止 API 启动。轮换时先并行加入新 Key、更新客户端，再删除旧 Key并重启角色。`.env`、部署 secret 和日志都不应提交 Git。

双系统捡回额度使用两枚不同的 QUOTA_CLIENT Key，只能访问 `/v1/reclaim/quota/acquire`；普通和管理员 Key 不能调用该入口，额度 Key 不能访问代理查询、反馈或管理接口。额度键只存 UUIDv4 尝试号，不存账号、Leads 或代理地址。Redis/额度服务不可用时拒绝授予，客户端不得切回本机限流或无额度直发。

跨机代理导出使用独立 PEER_EXPORT Key，只能访问 `/v1/peer/proxies`，不能访问查询、反馈、仪表盘数据、管理员或捡回额度接口。普通、管理员和额度 Key 也不能导出。该路由仅返回正式池记录，不能把对端缓存重新导出。导出和缓存均默认关闭，需分别启用；对端连接只走 SSH 回环转发，不新增公网 API 端口。

代理缓存隧道使用独立 `proxy-peer` SSH 账号与每个方向不同的 Key；服务端须同时限制 `authorized_keys` 的来源/目标和 `Match User` 的本地转发权限，禁止 shell、SFTP、远端转发与其他目标。上线前以 `sshd -t`、`sshd -T -C` 及实际正反向连接测试核验，保留管理员会话后 reload。容器隧道只读挂载专用 Key、known_hosts 和客户端配置，禁用特权/可写根文件系统，不继承 API/Redis/业务 env。同步进程仅使用 Redis 和对端 PEER_EXPORT Key；Webhook 仅注入 API，collector/checker 不获得这两类秘密。隧道和同步服务均没有宿主 ports；`peer-tunnel` 容器内监听仅供内部网络访问。

管理员探测默认关闭。启用时必须同时设置管理员 Key 和精确主机 allowlist。系统解析 DNS 并拒绝任一非全局地址，代理端点也默认拒绝私网、环回、链路本地、保留和组播地址；重定向关闭，超时限制为 1～30 秒，文本片段最多 2048 字符。DNS 变更时每次请求都会重新校验。

业务与管理员限流使用 Redis 原子固定窗口并在所有 API 实例间共享。限流存储异常时管理员端点 fail closed。内部异常统一脱敏，日志递归遮蔽 API Key、Authorization、password、secret、token 和 Redis URL。指标禁止 endpoint、URL、query 和 error message 等高基数标签。

Compose 不发布 Redis，API 仅绑定 `127.0.0.1`。如需外部访问，应在受控反向代理后配置 TLS、网络 ACL 和额外身份认证，不要直接把 Uvicorn 暴露到互联网。

## 只读监测页面

`/dashboard` 外壳可以匿名加载，但所有池数据继续经过普通 API Key 认证与查询限流。Key 只保存在当前标签页的 `sessionStorage`，仅通过 `X-API-Key` 请求头发送；不会进入 URL、页面文本、服务器日志或持久化浏览器存储。401 会立即移除 Key 并清空已渲染数据，避免继续展示前一个身份的数据。

页面没有管理按钮，代理明细也只调用 GET 路由。严格 CSP 只允许同源脚本、样式、字体、图片和连接，禁止外部 CDN、内联脚本、frame 嵌入和表单外发。所有运行时数据使用 `textContent`/DOM 节点渲染，不把 API 字符串作为 HTML。静态资源随 wheel 和容器分发，因此页面不依赖公网前端资源。
