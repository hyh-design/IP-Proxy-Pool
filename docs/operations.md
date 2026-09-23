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

## 低延迟索引升级

随机选择依赖每个域名的 `available-latency` 索引和版本就绪标记。索引未构建时 readiness 和随机接口返回 503，不会使用旧的概率抽样，也不会通过服务器真实出口访问目标站。

上线前先保持旧服务运行并做只读演练：

```bash
docker compose run --rm api rebuild-latency-index \
  --domain portal.daqihui.com --dry-run
```

确认 Redis 快照可恢复后，只暂停代理池的读写角色；大企汇进程保持运行，并在代理 API 不可用时跳过本次任务：

```bash
docker compose stop api checker collector
docker compose run --rm api rebuild-latency-index \
  --domain portal.daqihui.com
docker compose up -d checker collector
docker compose up -d api
curl -fsS http://127.0.0.1:8000/health/ready
```

用独立 Redis DB 运行 10,000 条记录基准，命令只清理包含 `benchmark` 的专用前缀：

```bash
uv run python scripts/benchmark_selection.py \
  --redis-url redis://127.0.0.1:6379/15 \
  --key-prefix ippool:benchmark \
  --domain portal.daqihui.com \
  --iterations 500 \
  --max-latency-ms 1000 \
  --min-score 80
```

验收要求是 `returned_min=20`、`empty_count=0`、`p95_ms<=100`，并连续检查 100 次接口返回的评分和延迟。上线后观察 30 分钟，比较空返回率、查询错误率、Redis 延迟、checker 吞吐和容器健康。

回滚时恢复升级前记录的镜像版本，按 `checker`、`collector`、`api` 顺序启动。新增索引键可保留，旧版本会忽略；不要删除记录、质量、到期或租约键。

## 双机额度与代理缓存发布

这两项功能相互独立，默认关闭。系统一是唯一全局捡回额度发放方；系统二只运行 `lixi` 的 primary，secondary 保持禁用。代理缓存回滚不能关闭全局额度，也不能恢复旧账号。上线前记录两仓库源码 SHA、两台主机镜像 ID、实际 Compose 目录/业务服务名、受限配置与 Redis 可恢复备份、NTP 偏差和同负载查询基线。上线制品须标明这两个 SHA；线上目录可能不是 Git checkout，不能直接假定 `git pull`。任何未经验证的配置/镜像不用于生产。

额度先行：两端升级兼容客户端但保持全局开关关闭，系统一先部署额度 API，系统二建立独立的宿主回环 SSH 转发 `127.0.0.1:18000` 至系统一回环 API。两个 QUOTA_CLIENT Key 必须互异且仅能申请额度；普通、管理员、PEER_EXPORT Key 均须被拒绝。真实 SSH 权限负向验收包含执行命令、SFTP、其他目标和远端转发。停止两侧常驻、手动及 smoke 等全部捡回入口，记录最后一次旧本地许可，静默至少 60 秒后同时启用全局模式并恢复服务。隔离环境证明连续 60 秒第六次申请被拒绝；生产不主动触发第六次捡回。Redis、API 或 SSH 不可用时两侧 fail-closed，查询和我的 Leads 复核可继续。确需回退额度时，协调停止两侧全部捡回入口、从最后一次许可再静默至少 60 秒，然后同步恢复旧配置；绝不单侧关闭开关。

代理缓存上线前，在隔离环境完成双向只导出正式池、权限矩阵、碰撞否决、超时/乱序/迟到回执和客户端实际反馈测试；确认外部主机/容器/Redis 宕机告警可达，不能只依赖应用内 Webhook 自报。两侧 SSH 使用独立于额度隧道的 `proxy-peer` 专用账号和方向专用 Key。服务端 `authorized_keys` 需 `from` 来源约束、`restrict,port-forwarding,permitopen="127.0.0.1:8000"`；`Match User` 要求 `MaxSessions 0`、仅本地 TCP 转发、`PermitOpen 127.0.0.1:8000`、禁密码、TTY、SFTP、远端/Unix socket/代理转发。先验证远端主机指纹，再执行 `sshd -t` 和 `sshd -T -C`，保留管理员会话后 reload，切勿直接 restart 或锁死远程管理。核验合法转发与所有负向用例，不增加公网监听。

角色秘密文件见 [配置](configuration.md)。先在两端上线兼容 API 和客户端，但缓存/导出/业务代理开关均关闭；验证旧接口和回路。启用专用导出后，再次检查 PEER_EXPORT、QUOTA_CLIENT、普通和管理员 Key 的交叉权限。系统一先加载 API 缓存与 Webhook 配置，但客户端仍关闭；仅重建目标服务，不执行整栈 `up --build`：

```bash
docker compose up -d --no-deps --force-recreate api
docker compose --profile peer-cache up -d --no-deps peer-tunnel
docker compose --profile peer-cache up -d --no-deps peer-sync
scripts/verify-peer-cache.sh --compose-dir /opt/software/pythonproject/ip-proxy-pool --require-peer-ready
```

脚本默认只读，不发捡回、不发失败反馈、不修改配置，也不打印 Key/URL；可传 `--read-key-file` 指向 0600 的普通 Key 文件以输出来源聚合计数。核实容器入口、隧道健康、缓存有效数≥5、心跳、`peer_metrics_available=1`、宿主仍仅回环监听 API 后，才开启系统一业务客户端代理缓存开关，并按**现场已核实的服务名**重建业务服务。连续观察至少 30 分钟。功能失败即回滚；或连续两个 5 分钟窗口查询成功率较同负载基线低超过 5 个百分点，且每窗至少 20 次请求时回滚；样本不足则延长观察。系统一达标后系统二按相同顺序上线并单独观察至少 30 分钟，确认只有 `lixi` primary 运行、secondary 没有恢复。两侧达标后方可声明部署完成。

验收脚本需要 Python 3.10 或更新版本；宿主默认 `python3` 较旧时，可用 `PYTHON=/path/to/python3.11 scripts/verify-peer-cache.sh ...` 指定已安装的兼容解释器。此变量只控制只读验收脚本，不改变容器运行环境。

缓存回滚顺序：先关闭受影响端业务客户端开关并重建业务服务；再关闭 API 缓存开关并**重建** API；最后 `docker compose --profile peer-cache stop peer-sync peer-tunnel`。保留回执和凭据至少 600 秒加最大在途时长，默认让缓存自然到期，不能 `FLUSHDB` 或按宽泛前缀清理。需要旧镜像时先排空回执、核对备份再恢复。只撤销确认不被另一方向使用的授权，复验 readiness、端口、业务暂停和额度继续有效。

## 双系统捡回归属协调（默认关闭，待单独批准上线）

归属 API 仅系统一代理池承载，Redis 键位于独立的 `:reclaim:ownership:` 命名空间；不占用全局捡回额度，也不启用代理缓存。先记录两仓源码 SHA、生产镜像 ID、受限配置位置、Redis/SQLite 可恢复备份、两端 NTP 偏差和当前启用账号清单。系统二只纳入实际运行的 `lixi`；系统一逐一列出实际启用账号，`mayi` 在无库容期间不得加入成员快照。成员变动必须先评估未终结案件，不能单侧删号。

先部署 API/数据模型且保持 `IP_POOL_OWNERSHIP__ENABLED=false`，再部署两个业务客户端且保持各自 `RECLAIM_OWNERSHIP_ENABLED=false`。为每个启用账号生成独立的 `OWNERSHIP_CLIENT` Key，通过系统一 API 的受限配置设置 `IP_POOL_OWNERSHIP__MEMBERS`（JSON 对象；每项为 `system_id`、`api_key`），启动校验要求系统一、二均有成员，且 Key 与普通、管理员、额度、代理导出角色互异。不要把 Key、Cookie 或密码写进日志、命令行、仓库。系统二复用现有已核验的回环额度 SSH 隧道到系统一 API，不开放额外公网端口；业务配置只指向本机 `127.0.0.1` 隧道端口。隧道的主机指纹、仅本地转发目标及权限负向测试仍须复验。

在隔离 Redis/API 和本地客户端完成真实契约测试，再做无真实捡回动作的上线前检查：`/health/ready` 为成功，宿主 API 仍仅回环监听；专用 Key 只能访问归属路由，普通/管理员/额度/导出 Key 访问归属路由为 403，归属 Key 访问额度与代理接口为 403；禁用态归属路由在正确授权后为 503。隔离环境用模拟记录验证两轮全员 `absent`、任一 `found`、离线/错误保持待确认、晚到成功更正、重放幂等和额度窗口不变。生产环境不要为验收主动捡回或伪造真实业务事件。

在两端所有启用账号的独立登录态、成员清单、隧道和补查能力均就绪后，才另行批准并协调启用 `IP_POOL_OWNERSHIP__ENABLED=true` 与两侧业务开关。启用后观察真实样本及 `/metrics`：`ip_pool_reclaim_ownership_member_heartbeat_timestamp_seconds`、`ip_pool_reclaim_ownership_pending_cases`、`ip_pool_reclaim_ownership_oldest_pending_age_seconds`、`ip_pool_reclaim_ownership_check_errors`、`ip_pool_reclaim_ownership_success_backlog`、`ip_pool_reclaim_ownership_revision_conflicts`；还须检查两端 SQLite 待发/冲突项与短时、日报统计。缺样本时延长观察，不宣称已完成真实业务验收。此前用户选择暂不配置独立的整机/Redis 宕机告警，此盲区仍在；应用自身指标无法替代外部告警。

回滚先协调停止新归属分类和通知，再把两端业务开关置 `false`，最后关闭协调 API 开关；保持全局额度开启、代理缓存关闭，不清空 Redis/SQLite、回执、案件或通知待发箱。旧规则恢复后可能把内部竞争算作被抢，须明确标记统计分界和遗留待确认案件；恢复新功能时逐案补处理，不能把单端下线自动当作对方未持有。
