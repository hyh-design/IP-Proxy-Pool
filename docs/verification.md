# 完整改造验收记录

## 2026-09-23 双机共享额度与代理缓存预发布门禁

本节只记录本地代码与隔离环境验证，**不代表线上已启用**。系统二先前的单账号收敛为独立操作，代理缓存回滚不得恢复旧账号。正式启用还要求两机受限 SSH/权限、独立外部宕机告警、同步库存和各自至少 30 分钟灰度观察。

| 检查 | 结果 | 范围与限制 |
|---|---|---|
| 代理池格式、静态、类型 | `ruff format --check .`、`ruff check .`、`mypy src` 均通过 | 当前隔离工作树；源代码最终提交后复核制品 SHA |
| 代理池完整测试 | 366 passed、1 skipped | `uv run pytest -q --browser-channel chrome`；跳过的是依赖本地镜像的容器隧道测试，不把跳过计为通过 |
| 容器隧道单独验收 | 1 passed | `DOCKER_CONFIG` 指向无凭据助手的临时配置后，隔离 Docker 网络内真实 SSH 容器、健康转发及无宿主端口通过；本机不接触生产 SSH 目标 |
| 捡回仓库完整测试 | 333 passed | 从其独立工作树、使用已有虚拟环境运行；线上开关尚未切换 |
| 双向正式池/实际客户端 | 通过 | 两个独立 Redis 命名空间与 API，A→B→A 不回流，实际捡回端客户端选择及反馈不写对端正式池；测试依赖显式 `DAQIHUI_RECLAIM_ROOT` |
| Compose 默认/profile | 通过 | 默认仍四服务；profile 增两服务、无新增宿主端口；缺少 sync 角色 env 文件启动失败 |

线上发布阶段必须另记：两个仓库提交 SHA、镜像 ID/digest、备份位置、有效配置开关、角色权限负向结果、NTP、外部告警、额度失联 fail-closed、缓存指标和双方 30 分钟观察结果。缺任一项不得将此预发布记录标为完成。

验收日期：2026-08-10。以下检查均针对 `feat/full-redesign` 的最终实现提交重新执行，命令退出码均为 `0`；随后该提交将合并到默认分支 `main`。

| # | 验收标准 | 权威命令或制品 | 期望证据 | 实际证据 | 状态 |
|---:|---|---|---|---|:---:|
| 1 | Git 已初始化，默认分支为 `main` | `git -C .. branch --show-current`、`.git/` | 根仓库当前分支为 `main` | 根仓库输出 `main`，功能分支在隔离 worktree 中完成 | PASS |
| 2 | 原项目、压缩包、评估文档和 `.codegraph` 不被 Git 跟踪 | `git ls-files`、`git check-ignore -v ...`、`tests/unit/test_repository_boundaries.py` | 禁止路径无跟踪项且均命中忽略规则 | 跟踪项匹配数为 0；四类路径均由 `.gitignore` 命中；边界测试 2 项通过 | PASS |
| 3 | Codegraph 可用且不索引原项目 | `codegraph sync .`、`codegraph status .`、`codegraph files`、`codegraph query ProxyRecord` | 索引最新、核心符号可查询、禁止路径无结果 | 94 files、1,127 nodes、2,269 edges，状态 up to date；`ProxyRecord` 和 `CheckerWorker` 可查询；禁止路径匹配数为 0 | PASS |
| 4 | API 默认安全监听并要求认证 | `docker compose config`、Compose 实测、`tests/e2e/test_compose.py` | 仅回环发布 API，Redis 不发布，业务路由无 Key 返回 401 | API 为 `127.0.0.1:8000`，Redis 仅容器内 `6379/tcp`；匿名 `/v1/domains` 返回 401 | PASS |
| 5 | admin probe 默认关闭并通过 SSRF/限流测试 | Compose 实测、`tests/unit/api/test_admin_probe.py` | 默认 404；私网/DNS、角色、超时、重定向、限流失败关闭均被测试 | `/v1/admin/probe` 返回 404；对应 8 项安全测试全部包含在完整测试通过结果中 | PASS |
| 6 | 目标或系统故障不修改代理分数 | `test_system_error_does_not_change_failure_state`、`test_cancelled_probe_does_not_change_health` | 系统错误与取消不扣分 | 两项断言均通过 | PASS |
| 7 | 多 checker 不重复领取有效租约 | `TEST_REDIS_URL=... pytest -m docker`、`test_two_workers_complete_each_endpoint_once` | 真实 Redis 下每个 endpoint 只完成一次 | Redis 集成测试 14 项通过，双 worker 租约测试通过 | PASS |
| 8 | 预测试成功的代理立即可查询 | `test_prediction_success_is_available_without_second_probe`、Compose 业务路由实测 | 成功预测直接写入 available 集合并可查询 | 单元断言通过；验收样本的 list/random 均返回 200 和 available 记录 | PASS |
| 9 | 查询分页且存在硬上限 | `test_proxy_cursor_advances_and_is_accepted`、`test_proxy_limit_above_max_is_rejected`、`test_random_count_is_hard_bounded` | cursor 可续页，limit/count 超限被拒绝 | 三项断言均通过；Compose list/random 正常返回 200 | PASS |
| 10 | PSL、IPv4 和 IPv6 域名解析通过 | `tests/unit/test_domain.py`、`tests/unit/test_models.py` | PSL/private PSL、IP 字面量与 endpoint 规范化通过 | 域名测试 5 项、模型测试 7 项全部通过 | PASS |
| 11 | 日志和指标覆盖主要子系统 | `tests/unit/observability/`、`src/ip_proxy_pool/observability/`、`/metrics` | 采集、检测、熔断、API、Redis、worker 有低基数指标，日志脱敏 | observability 测试 9 项通过；高基数/密钥标签被拒绝；`/metrics` 返回 200 | PASS |
| 12 | legacy import 不破坏旧 Redis key | `tests/integration/test_legacy_import.py`、`tests/unit/test_migration.py` | 类型、成员、分数、TTL 保持不变，导入可重复 | 真实 Redis legacy 集成断言与 3 项迁移单元测试均通过 | PASS |
| 13 | Ruff、mypy、pytest、覆盖率、Redis 集成和 Docker build 通过 | 完整门禁命令见下节 | 所有退出码为 0，覆盖率至少 80% | Ruff 通过；mypy 47 个源文件无问题；171 个本地测试和 14 个 Redis 测试通过；覆盖率 84.59%；Docker build 成功 | PASS |
| 14 | Compose 服务健康并能优雅停止 | `docker compose up/ps/stop -t 30/down`、健康路由 | Redis/API 健康，collector/checker 运行，宽限期内停止 | 四服务均为 Up，Redis/API 为 healthy；ready 返回 200；四服务正常 Stopped/Removed | PASS |
| 15 | 文档和配置完整一致 | `tests/unit/test_documentation.py`、README 与 `docs/` | README、模板、架构、运维、安全、迁移和 Codegraph 文档存在且一致 | 文档一致性测试 3 项通过；所列文档和 `.env.example` 均存在 | PASS |

## 最终门禁结果

本地工具版本：Python 3.11.15、uv 0.12.1、Ruff 0.16.2、mypy 1.20.2、pytest 9.1.1、Docker 29.4.1、Docker Compose 5.1.3、Codegraph 0.9.9。

```text
uv sync --frozen                                      PASS
uv run ruff format --check .                          PASS (106 files)
uv run ruff check .                                   PASS
uv run mypy src                                       PASS (47 source files)
uv run pytest -m "not docker" --cov=ip_proxy_pool ... PASS (171 passed, 14 deselected)
coverage                                               PASS (84.59%, threshold 80%)
TEST_REDIS_URL=redis://localhost:6389/15 pytest -m docker -q
                                                       PASS (14 passed, 171 deselected)
uv run pip-audit                                      PASS (no known vulnerabilities)
docker compose config --quiet                         PASS
docker compose build                                  PASS
```

pytest 仅报告一条来自 FastAPI 测试客户端兼容层的 `StarletteDeprecationWarning`，不影响运行结果。`pip-audit` 跳过当前本地项目包本身（其不在 PyPI），所有第三方锁定依赖均已审计。

## Compose 运行证据

使用一次性验收 Key 启动服务；文档中不记录 Key 值。实测结果：

```text
GET  /health/ready                         200 {"status":"ready"}
GET  /v1/domains（无认证）                  401
GET  /v1/domains（已认证）                  200
GET  /v1/proxies?domain=httpbin.org        200（1 条 available 样本）
GET  /v1/proxies/random?domain=httpbin.org 200（1 条 available 样本）
GET  /v1/stats                             200
POST /v1/admin/probe（默认配置）             404
GET  /metrics                              200
```

Compose 显示 API 仅发布 `127.0.0.1:8000`，Redis 没有宿主机端口映射。验收结束执行 `docker compose stop -t 30` 和 `docker compose down`，容器及网络均正常停止并移除，命名数据卷按运维约定保留。

## Git 与 Codegraph 边界

`.codegraph/` 与 `docs/superpowers/` 在 `git status --short --ignored` 中显示为 ignored，不在 `git ls-files` 中；Codegraph 索引刷新后仍为最新状态。日常刷新和查询方法见 [Codegraph 使用](codegraph.md)。

## 代理池监测页面追加验收

验收日期：2026-08-11。以下证据针对 `feat/dashboard`，浏览器测试和异步 Redis 测试分别在独立 pytest 进程中执行，避免同步 Playwright 与 `pytest-asyncio` 共用事件循环。

| # | 仪表盘验收标准 | 直接证据 | 实际结果 | 状态 |
|---:|---|---|---|:---:|
| 1 | `/dashboard` 位于现有 API 且不依赖公网资源 | 页面/CSP 单元测试、wheel 清单、构建后容器 GET | 页面、CSS 均返回 200；HTML 不含外部 URL；脚本、样式、字体均随 wheel 分发 | PASS |
| 2 | 未认证用户看不到数据，Key 不持久化或记录 | API 认证测试、401 浏览器清屏测试、安全文档 | 匿名 summary 返回 401；Key 只在 `sessionStorage`，401 后 Key 与旧面板同时清空 | PASS |
| 3 | 首屏指标口径正确 | `test_analytics.py`、summary 契约、真实浏览器固定数据 | 总数 2、可用 1、主可用率 50%、占总池 50%、高质量 1、due 2、隔离 0 | PASS |
| 4 | 六档范围、双分辨率、最多 720 点 | 历史策略单元/Redis 测试、浏览器范围测试、容器端点 | 1h/6h/12h/24h 返回 5m，7d/30d 返回 1h；六个容器请求均为 200，保留上限 576/720 | PASS |
| 5 | 质量、延迟和来源符合口径 | 聚合边界测试、质量/来源 API、浏览器面板 | 四档评分、状态、平均/P50/P95、多来源分别计数通过；partial 明确显示样本数 | PASS |
| 6 | 明细默认折叠且筛选/分页有界 | 仓储分页测试、游标签名测试、浏览器筛选测试 | 初始未展开；状态、最低/最高分、来源均进入签名；底层扫描 offset 不跳过匹配项 | PASS |
| 7 | 多 checker 幂等写入并正确清理 | 快照并发测试、真实 Redis 历史保留测试 | 同桶两个 recorder 仅一个成功；5m/小时索引与 HASH 同步清理 | PASS |
| 8 | API/Redis/collector/checker 状态与过期心跳可见 | 心跳状态/TTL 测试、stale/down 浏览器测试 | healthy、stale、down 阈值与 100 实例上限通过；页面显示“正常/延迟/离线” | PASS |
| 9 | 故障状态安全且不误导 | 503 脱敏测试、401/503/局部失败浏览器测试 | 503 只返回 `service unavailable`；summary 故障隐藏旧内容；单独质量失败不隐藏成功 summary | PASS |
| 10 | 键盘、高对比、文本状态、reduced-motion | Playwright 焦点与媒体模拟、两档视觉检查 | 范围按钮可聚焦；SVG 有文本摘要和可聚焦点；1440×1000、900×1000 浅色页面无横向溢出 | PASS |
| 11 | Compose 不新增服务/端口且仍为回环 | `docker compose config`、`test_compose.py` | 默认仍为 redis/api/collector/checker 四服务，Prometheus 仍是可选 profile；唯一发布端口为 `127.0.0.1:8000` | PASS |
| 12 | 静态、单元、Redis、Docker、浏览器门禁通过 | 下列完整门禁、容器端点、Codegraph | 全部门禁退出码 0；API 验收容器已用 `--rm` 停止并移除 | PASS |

### 仪表盘门禁结果

工具版本：Python 3.11.15、uv 0.12.1、Ruff 0.16.2、mypy 1.20.2、pytest 9.1.1、Redis 7.4.10、Docker 29.4.1、Docker Compose 5.1.3、Codegraph 0.9.9。

```text
uv sync --frozen                                      PASS
uv run ruff format --check .                          PASS (133 files)
uv run ruff check .                                   PASS
uv run mypy src                                       PASS (59 source files)
uv run pytest -m "not docker" --cov=ip_proxy_pool ... PASS (228 passed, 27 deselected)
coverage                                               PASS (86.08%, threshold 80%)
TEST_REDIS_URL=redis://localhost:6389/15
  uv run pytest tests/integration -m docker -q         PASS (17 passed)
TEST_REDIS_URL=redis://localhost:6389/15
  uv run pytest tests/e2e/test_dashboard_browser.py
  -q --browser-channel chrome                         PASS (10 passed)
uv run pip-audit                                      PASS (no known vulnerabilities)
uv build                                              PASS (sdist + wheel)
docker compose config --quiet                         PASS
docker compose build                                  PASS
```

Codegraph 同步后包含 121 files、1,620 nodes、3,133 edges，Python、JavaScript 与 YAML 索引均为最新；`DashboardSnapshotRecorder` 和 `DashboardService` 可直接查询。

### 构建后容器端点

使用 `ip-proxy-pool:local` 新构建镜像、只读根文件系统、一次性非密钥测试值和独立宿主端口运行 API，实测：

```text
GET /dashboard                                      200
GET /dashboard/assets/dashboard.css                 200
GET /health/ready                                   200
GET /metrics                                        200
GET /v1/dashboard/summary（无 Key）                  401
GET /v1/dashboard/summary?domain=example.com        200
GET /v1/dashboard/quality?domain=example.com        200
GET /v1/dashboard/sources?domain=example.com        200
GET /v1/dashboard/history?range=1h|6h|12h|24h|7d|30d
                                                     每个范围均为 200
```

页面响应同时包含预期 CSP、`X-Content-Type-Options: nosniff` 和 `Referrer-Policy: no-referrer`。验收 API 容器使用 `--rm`，停止后 `docker ps -a` 无残留；临时 Redis 在全部验证结束后单独停止。
