# Hot Proxy Refresh and Consumer Backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep qualified low-latency proxies fresh without lowering selection thresholds, and let Daqihui automatically ride through a temporary empty proxy pool without direct fallback or error-notification storms.

**Architecture:** The proxy pool gains an atomically maintained `priority-due` Redis index and claims at most half of each checker batch from that index before filling the batch from the general due queue. Daqihui keeps one shared proxy client, globally throttles empty-pool refreshes to one attempt every five seconds, and records those periods in a separate statistic instead of treating them as platform query failures.

**Tech Stack:** Python 3.11, Redis sorted sets and Lua, FastAPI, requests, SQLite, pytest, Docker Compose, systemd.

## Global Constraints

- Keep the existing minimum score, maximum latency of 1000ms, maximum checked age of 600 seconds, and minimum consecutive-success thresholds unchanged.
- Never fall back to a direct Daqihui connection for login, query, or reclaim.
- Do not increase Checker `batch_size=200` or `concurrency=100`.
- Suppress DingTalk notifications only for proxy-pool availability and proxy transport failures; preserve platform query, reclaim, success, and competition-lost notifications.
- Existing Redis and SQLite data must migrate idempotently and remain rollback-compatible.

---

### Task 1: Add the priority due index and eligibility rules

**Files:**
- Modify: `src/ip_proxy_pool/storage/keys.py`
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `src/ip_proxy_pool/api/dependencies.py`
- Modify: `src/ip_proxy_pool/runtime.py`
- Test: `tests/unit/storage/test_keys.py`
- Test: `tests/unit/storage/test_repository.py`
- Test: `tests/unit/api/test_dependencies.py`

**Interfaces:**
- Produces: `PoolKeys.priority_due`, `PoolKeys.priority_due_ready`.
- Produces: `RedisRepository(redis, *, prefix: str, priority_max_latency_ms: float)`.
- Produces: `priority_due_score(record: ProxyRecord, max_latency_ms: float) -> float | None`.

- [ ] **Step 1: Write failing key and eligibility tests**

```python
def test_keys_include_priority_due_indexes() -> None:
    keys = keys_for("ippool:test", "portal.daqihui.com")
    assert keys.priority_due.endswith(":priority-due")
    assert keys.priority_due_ready.endswith(":priority-due-ready")


def test_priority_due_score_requires_selectable_latency(available_record) -> None:
    assert priority_due_score(available_record, 1000) == available_record.next_check_at.timestamp()
    assert priority_due_score(
        available_record.model_copy(update={"latency_ewma_ms": 1000.1}), 1000
    ) is None
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest -q tests/unit/storage/test_keys.py tests/unit/storage/test_repository.py`

Expected: collection or assertion failure because the priority keys, constructor argument, and helper do not exist.

- [ ] **Step 3: Implement keys, constructor wiring, and transactional membership updates**

Add the two keys to `PoolKeys`. Store `priority_max_latency_ms` on `RedisRepository`. In `upsert_candidate`, `save_record`, and all repository factories, use:

```python
priority_due_at = priority_due_score(record, self._priority_max_latency_ms)
if priority_due_at is None:
    pipeline.zrem(keys.priority_due, endpoint)
else:
    pipeline.zadd(keys.priority_due, {endpoint: priority_due_at})
```

Construct repositories with `settings.selection.max_latency_ms`; do not introduce a second threshold.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest -q tests/unit/storage/test_keys.py tests/unit/storage/test_repository.py tests/unit/api/test_dependencies.py`

Expected: PASS.

- [ ] **Step 5: Commit the proxy index foundation**

```bash
git add src/ip_proxy_pool/storage/keys.py src/ip_proxy_pool/storage/repository.py \
  src/ip_proxy_pool/api/dependencies.py src/ip_proxy_pool/runtime.py \
  tests/unit/storage/test_keys.py tests/unit/storage/test_repository.py \
  tests/unit/api/test_dependencies.py
git commit -m "feat: index priority proxy rechecks"
```

### Task 2: Claim priority and general leases atomically

**Files:**
- Modify: `src/ip_proxy_pool/storage/lua.py`
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Test: `tests/integration/test_redis_leases.py`
- Test: `tests/unit/storage/test_repository.py`

**Interfaces:**
- Produces: `CLAIM_PRIORITY_DUE` Lua script.
- Changes: `RedisRepository.claim_due(...) -> list[Lease]` claims up to `max(1, limit // 2)` priority members, then fills the remaining capacity from general due members.

- [ ] **Step 1: Write failing lease-order and non-starvation tests**

Create three due priority records and three due general records, call `claim_due(limit=4)`, and assert that the result contains two priority and two general leases. Add a concurrent claim assertion proving no endpoint appears twice.

```python
leases = await repository.claim_due(DOMAIN, "worker-a", 4, 60, now.timestamp())
assert {lease.endpoint for lease in leases[:2]} == {"1.1.1.1:80", "2.2.2.2:80"}
assert len({lease.endpoint for lease in leases}) == 4
```

- [ ] **Step 2: Run the integration test and verify RED**

Run: `uv run pytest -q tests/integration/test_redis_leases.py -k priority`

Expected: FAIL because `priority-due` is not consulted.

- [ ] **Step 3: Implement atomic priority claims and lifecycle synchronization**

`CLAIM_PRIORITY_DUE` must:

```lua
-- KEYS: priority_due, due, leased, lease_owners
-- ARGV: now, limit, expires_at, owner
-- Read due priority members, skip members already leased, remove selected
-- members from general due, add their lease, and store owner atomically.
```

Keep priority membership while leased so an expired lease becomes eligible again; the Lua script must skip active leases. Update `COMPLETE_LEASE` and `DELETE_LEASE` to set or remove priority membership atomically. `RELEASE_LEASE` restores the general due timestamp and updates the priority timestamp when the member is still present.

- [ ] **Step 4: Run lease tests and verify GREEN**

Run: `uv run pytest -q tests/integration/test_redis_leases.py tests/unit/storage/test_repository.py`

Expected: PASS, including concurrent ownership and expired lease recovery.

- [ ] **Step 5: Commit atomic priority leasing**

```bash
git add src/ip_proxy_pool/storage/lua.py src/ip_proxy_pool/storage/repository.py \
  tests/integration/test_redis_leases.py tests/unit/storage/test_repository.py
git commit -m "feat: prioritize qualified proxy leases"
```

### Task 3: Rebuild and gate the priority index

**Files:**
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `src/ip_proxy_pool/cli.py`
- Modify: `src/ip_proxy_pool/api/routes/health.py`
- Modify: `src/ip_proxy_pool/runtime.py`
- Modify: `tests/unit/test_cli.py`
- Modify: `tests/unit/api/test_health_routes.py`
- Modify: `tests/unit/test_runtime.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `RedisRepository.priority_due_index_ready(domain: str) -> bool`.
- Produces: existing `rebuild-latency-index` command also rebuilds and marks `priority-due` ready.
- Changes: readiness requires both latency and priority indexes.

- [ ] **Step 1: Write failing rebuild and readiness tests**

Assert a dry run reports `priority_indexed`, a real rebuild replaces stale priority members, `/health/ready` returns 503 when either ready marker is absent, and Checker startup refuses to run before both markers are ready.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest -q tests/unit/test_cli.py tests/unit/api/test_health_routes.py tests/unit/test_runtime.py`

Expected: FAIL because priority rebuild fields and readiness checks do not exist.

- [ ] **Step 3: Implement rebuild, marker, and documentation**

Use a temporary ZSET plus atomic rename, matching the existing latency-index rebuild. The command output must include:

```json
{"scanned": 10000, "indexed": 2773, "priority_indexed": 39, "ignored": 7227}
```

Add `all_selection_indexes_ready()` and use it from API readiness and Checker startup. Document that API and Checker must not start against an unbuilt priority index.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest -q tests/unit/test_cli.py tests/unit/api/test_health_routes.py tests/unit/test_runtime.py`

Expected: PASS.

- [ ] **Step 5: Commit index migration support**

```bash
git add src/ip_proxy_pool/storage/repository.py src/ip_proxy_pool/cli.py \
  src/ip_proxy_pool/api/routes/health.py src/ip_proxy_pool/runtime.py \
  tests/unit/test_cli.py tests/unit/api/test_health_routes.py \
  tests/unit/test_runtime.py README.md
git commit -m "feat: rebuild priority proxy index"
```

### Task 4: Add global empty-pool backoff in Daqihui

**Files:**
- Modify: `../daqihui-reclaim/daqihui/proxy_pool.py`
- Test: `../daqihui-reclaim/tests/test_proxy_pool.py`

**Interfaces:**
- Changes: `ProxyPoolClient.__init__(..., unavailable_cooldown_seconds: float = 5.0, sleeper: Callable[[float], None] = time.sleep)`.
- Preserves: `next_proxy() -> str` and no-direct-fallback behavior.

- [ ] **Step 1: Write failing sequential and concurrent recovery tests**

Use an injected clock/sleeper. First return an empty payload, then a valid payload. Assert the second attempt advances five seconds, exactly one refresh occurs after the cooldown, and the returned value is the recovered proxy.

```python
with pytest.raises(ProxyPoolError):
    client.next_proxy()
assert client.next_proxy() == "http://1.1.1.1:80"
assert sleeps == [5.0]
assert len(session.calls) == 2
```

Add a `ThreadPoolExecutor` case showing concurrent callers do not stampede the API after an empty response.

- [ ] **Step 2: Run the proxy client tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_proxy_pool.py`

Expected: FAIL because empty responses are retried immediately and no shared cooldown exists.

- [ ] **Step 3: Implement condition-based global backoff**

Track `_unavailable_until` under the existing lock. Sleep outside the lock, then re-check state under the lock; keep the network fetch serialized. On a successful non-empty fetch, set `_unavailable_until = 0.0`. Do not return `None` and do not create a direct session fallback.

- [ ] **Step 4: Run proxy client tests and verify GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_proxy_pool.py`

Expected: PASS.

- [ ] **Step 5: Commit Daqihui backoff**

```bash
git add daqihui/proxy_pool.py tests/test_proxy_pool.py
git commit -m "fix: back off when proxy pool is empty"
```

### Task 5: Classify and persist proxy unavailability separately

**Files:**
- Modify: `../daqihui-reclaim/daqihui/models.py`
- Modify: `../daqihui-reclaim/daqihui/browser.py`
- Modify: `../daqihui-reclaim/daqihui/stats.py`
- Test: `../daqihui-reclaim/tests/test_browser_logic.py`
- Test: `../daqihui-reclaim/tests/test_stats.py`

**Interfaces:**
- Produces: `TaskRunResult.proxy_unavailable_count: int = 0`.
- Produces: `DailyTaskStats.proxy_unavailable_count: int = 0`.
- Changes: SQLite aggregate and detail tables gain `proxy_unavailable_count INTEGER NOT NULL DEFAULT 0` via idempotent migration.

- [ ] **Step 1: Write failing notification and statistics tests**

Have `_ensure_authenticated()` raise `ProxyPoolError("代理池没有可用代理")` and assert:

```python
assert result.errors == []
assert result.query_errors == []
assert result.proxy_unavailable_count == 1
assert error_notifications == []
```

Record the result and assert `error_count == 0`, `empty_runs == 0`, and `proxy_unavailable_count == 1`. Re-open an old-schema database and assert the migration adds the new columns with zero defaults.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_browser_logic.py -k proxy tests/test_stats.py -k proxy`

Expected: FAIL because proxy availability has no separate result or schema field.

- [ ] **Step 3: Implement classification, migration, aggregation, retention, and compact report output**

Catch `ProxyPoolError` before the generic exception branch, increment `proxy_unavailable_count`, and do not call `_append_query_error`. Include the new field in `task_runs`, `task_run_minute_stats`, `daily_task_stats`, archive schemas, summaries, and backfill lists. Add `代理不可用 N` to the compact daily report only when the total is non-zero.

- [ ] **Step 4: Run focused and full Daqihui tests**

Run: `.venv/bin/python -m pytest -q tests/test_browser_logic.py tests/test_stats.py tests/test_proxy_pool.py`

Expected: PASS.

- [ ] **Step 5: Commit Daqihui classification and migration**

```bash
git add daqihui/models.py daqihui/browser.py daqihui/stats.py \
  tests/test_browser_logic.py tests/test_stats.py
git commit -m "fix: classify proxy pool outages separately"
```

### Task 6: Full verification and deployment

**Files:**
- Modify if required by validation: `README.md`, `../daqihui-reclaim/README.md`
- Runtime only: server Docker Compose, Redis, systemd, SQLite.

**Interfaces:**
- Consumes: all interfaces from Tasks 1–5.
- Produces: deployed proxy-pool image, rebuilt priority index, restarted Daqihui service, and an acceptance record.

- [ ] **Step 1: Run complete local quality gates**

Proxy pool:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
docker compose config -q
```

Daqihui:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q daqihui daqihui_reclaim.py run_scheduler.py
```

Expected: all commands exit 0.

- [ ] **Step 2: Create server backups and stop only the consumer**

Back up proxy source/image/Redis RDB and Daqihui source/SQLite. Stop `daqihui-reclaim.service` before rebuilding indexes so no request can bypass the readiness gate.

- [ ] **Step 3: Deploy proxy pool and rebuild both indexes**

Deploy the new image, run `ip-pool rebuild-latency-index --domain portal.daqihui.com`, verify both ready markers, then start API/Checker/Collector. Confirm `priority-due` contains the expected qualified records and Checker claims them before general candidates.

- [ ] **Step 4: Deploy and start Daqihui**

Run the SQLite migration through normal application startup, start `daqihui-reclaim.service`, and verify the process uses HTTP authentication and proxy endpoints only.

- [ ] **Step 5: Observe the 20-minute acceptance window**

Every minute record: selectable proxies, priority due count, task executions, query hits, successes, platform errors, and proxy-unavailable count. Acceptance requires:

- no periodic selectable count of zero caused by stale qualified proxies;
- no proxy-pool error webhook;
- no direct connection to Daqihui hosts;
- normal task execution continues for all configured tasks;
- Checker concurrency and batch size remain 100 and 200.

- [ ] **Step 6: Commit any final documentation-only corrections**

```bash
git add README.md
git commit -m "docs: document priority proxy recovery"
```
