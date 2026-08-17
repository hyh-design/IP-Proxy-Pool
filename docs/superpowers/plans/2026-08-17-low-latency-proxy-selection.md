# Low-Latency Proxy Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace probabilistic sampling from the full quality index with an exact, per-domain low-latency candidate index so every request returns up to 20 proxies satisfying the caller's hard constraints without falling back to slower proxies or the server's real IP.

**Architecture:** Add a Redis sorted set keyed by domain whose members are only `available` records with finite, non-negative `latency_ewma_ms`, scored by latency. Maintain it atomically on every record mutation, use a readiness marker to distinguish a valid empty index from an index that has never been built, and select from the requested latency range with a randomized circular scan plus final record validation and self-healing. Add an idempotent rebuild command, bounded-cardinality metrics, and a dashboard count for proxies that satisfy the configured selection policy.

**Tech Stack:** Python 3.11+, FastAPI, redis-py asyncio, Redis Lua, Pydantic v2, Prometheus client, fakeredis with Lua, pytest/pytest-asyncio, Ruff, mypy, Docker Compose.

## Global Constraints

- Keep `max_latency_ms=1000` as a hard production ceiling for Daqihui; never widen it to 1500ms, 2000ms, or the project default.
- Never fall back to the server's direct network path when the proxy API is unavailable or returns no candidates.
- Preserve the existing `GET /v1/proxies/random` response schema and maximum `count=20` behavior.
- Preserve the existing score, freshness, and consecutive-success filters; the new index narrows by state and latency but does not replace final validation.
- Redis updates that change a record, state, latency, or existence must update the latency index in the same transaction or Lua script.
- A valid empty index must be distinguishable from an index that has not been built. Use a separate readiness marker because Redis removes empty sorted sets.
- Do not expose proxy endpoints, credentials, API keys, or error messages as Prometheus labels.
- Keep the Daqihui service running during proxy-pool rollout; it must skip work while the proxy API is unavailable.

---

### Task 1: Define the latency-index key contract and eligibility rules

**Files:**
- Modify: `src/ip_proxy_pool/storage/keys.py`
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Test: `tests/unit/storage/test_keys.py`
- Test: `tests/unit/storage/test_repository.py`

- [ ] **Step 1: Add failing key-contract tests**

Extend `test_keys.py` so one domain produces both stable keys:

```python
keys = keys_for("ippool:test", "portal.daqihui.com")
assert keys.available_latency == (
    "ippool:test:pool:portal.daqihui.com:available-latency"
)
assert keys.available_latency_ready == (
    "ippool:test:pool:portal.daqihui.com:available-latency-ready"
)
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run: `uv run pytest -q tests/unit/storage/test_keys.py`

Expected: failure because `PoolKeys` has no latency-index fields.

- [ ] **Step 3: Extend `PoolKeys` and `keys_for`**

Add:

```python
@dataclass(frozen=True, slots=True)
class PoolKeys:
    records: str
    quality: str
    due: str
    leased: str
    lease_owners: str
    available_latency: str
    available_latency_ready: str
```

Build the two new names from the existing escaped domain base.

- [ ] **Step 4: Add eligibility unit tests**

Add parametrized tests for the repository helper covering:

```python
(
    (ProxyState.AVAILABLE, 0.0, 0.0),
    (ProxyState.AVAILABLE, 1000.0, 1000.0),
    (ProxyState.CANDIDATE, 100.0, None),
    (ProxyState.DEGRADED, 100.0, None),
    (ProxyState.QUARANTINED, 100.0, None),
    (ProxyState.AVAILABLE, None, None),
    (ProxyState.AVAILABLE, -1.0, None),
    (ProxyState.AVAILABLE, float("inf"), None),
    (ProxyState.AVAILABLE, float("nan"), None),
)
```

- [ ] **Step 5: Implement one canonical helper**

In `repository.py`, add:

```python
def latency_index_score(record: ProxyRecord) -> float | None:
    latency = record.latency_ewma_ms
    if record.state is not ProxyState.AVAILABLE or latency is None:
        return None
    value = float(latency)
    return value if math.isfinite(value) and value >= 0 else None
```

All write, rebuild, and self-heal code must call this helper rather than duplicate eligibility logic.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest -q tests/unit/storage/test_keys.py tests/unit/storage/test_repository.py`

- [ ] **Step 7: Commit**

```bash
git add src/ip_proxy_pool/storage/keys.py src/ip_proxy_pool/storage/repository.py tests/unit/storage/test_keys.py tests/unit/storage/test_repository.py
git commit -m "feat: define available latency index"
```

### Task 2: Maintain the new index atomically on every mutation path

**Files:**
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `src/ip_proxy_pool/storage/lua.py`
- Modify: `tests/unit/storage/test_repository.py`
- Modify: `tests/unit/storage/test_capacity.py`
- Modify: `tests/integration/test_redis_leases.py`

- [ ] **Step 1: Add failing transaction-path tests**

Test `save_record` for these transitions and inspect `ZSCORE` after each write:

```python
await repo.save_record(available.model_copy(update={"latency_ewma_ms": 125.0}))
assert await fake_redis.zscore(keys.available_latency, endpoint) == 125.0

await repo.save_record(available.model_copy(update={"state": ProxyState.QUARANTINED}))
assert await fake_redis.zscore(keys.available_latency, endpoint) is None
```

Also test a latency update from 125ms to 240ms and candidate insertion through `upsert_candidate`.

- [ ] **Step 2: Add failing Lua and capacity tests**

Extend the Redis lease integration tests so:

- successful `complete` adds or updates `available_latency`;
- successful `complete` with a non-eligible record removes it;
- a lease owned by a different worker changes neither records nor any index;
- `delete_leased` removes the member from all six structures;
- `enforce_capacity` removes evicted members from `available_latency`.

- [ ] **Step 3: Run the mutation tests and confirm failures**

Run:

```bash
uv run pytest -q tests/unit/storage/test_repository.py tests/unit/storage/test_capacity.py tests/integration/test_redis_leases.py
```

- [ ] **Step 4: Update `save_record` transactionally**

Append exactly one index operation to the existing transactional pipeline:

```python
latency = latency_index_score(verified)
if latency is None:
    pipeline.zrem(keys.available_latency, endpoint)
else:
    pipeline.zadd(keys.available_latency, {endpoint: latency})
```

Do not set the readiness marker here; only a completed full rebuild may declare the domain ready.

- [ ] **Step 5: Extend `COMPLETE_LEASE` atomically**

Pass `keys.available_latency` as `KEYS[6]` and a nullable latency string as `ARGV[6]`. After writing the record and existing indexes, execute:

```lua
if ARGV[6] == '' then
  redis.call('ZREM', KEYS[6], ARGV[1])
else
  redis.call('ZADD', KEYS[6], ARGV[6], ARGV[1])
end
```

The owner check must remain the first operation so a stale worker cannot mutate any index.

- [ ] **Step 6: Extend deletion paths**

- Pass `available_latency` as `KEYS[6]` to `DELETE_LEASE` and `ZREM` it in the same Lua execution.
- Add `pipeline.zrem(keys.available_latency, *chunk)` to each capacity-eviction transaction.
- Verify feedback remains covered because `proxy_feedback` persists through `save_record`.

- [ ] **Step 7: Run focused and full storage tests**

Run:

```bash
uv run pytest -q tests/unit/storage tests/integration/test_redis_leases.py
```

- [ ] **Step 8: Commit**

```bash
git add src/ip_proxy_pool/storage/repository.py src/ip_proxy_pool/storage/lua.py tests/unit/storage/test_repository.py tests/unit/storage/test_capacity.py tests/integration/test_redis_leases.py
git commit -m "feat: maintain latency index atomically"
```

### Task 3: Replace probabilistic sampling with exact bounded selection

**Files:**
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `tests/unit/storage/test_repository.py`
- Modify: `tests/integration/test_redis_leases.py`

- [ ] **Step 1: Add a typed diagnostic result without breaking callers**

Add:

```python
@dataclass(frozen=True, slots=True)
class ProxySelection:
    records: tuple[ProxyRecord, ...]
    indexed_candidates: int
    inspected: int
    skipped_score: int
    skipped_freshness: int
    skipped_successes: int
    skipped_inconsistent: int
```

Implement `select_random_proxies(...) -> ProxySelection`; retain `random_proxies(...) -> list[ProxyRecord]` as a compatibility wrapper returning `list(result.records)`.

- [ ] **Step 2: Add boundary and self-heal tests**

Cover:

- latency exactly 1000ms is returned;
- latency 1000.001ms is not returned;
- score, freshness, and consecutive-success filters still apply;
- a missing record, non-available record, or changed latency in the index is skipped and removed from the ZSET;
- an absent readiness marker raises `LatencyIndexNotReadyError`;
- a present marker with an empty ZSET returns an empty result.

- [ ] **Step 3: Add the sparse-fast-proxy regression**

Seed 9962 eligible-state records above 1000ms and 38 records at or below 1000ms, mark the index ready, then make 100 calls with `count=20`:

```python
for _ in range(100):
    selected = await repo.random_proxies(
        "portal.daqihui.com",
        min_score=80,
        count=20,
        max_latency_ms=1000,
        max_checked_age_seconds=600,
        min_consecutive_successes=2,
        now=now,
    )
    assert len(selected) == 20
    assert all((item.latency_ewma_ms or 0) <= 1000 for item in selected)
```

Assert the union of endpoints across calls contains more than 20 entries. Repeat with 12 fast proxies and assert all 12 are returned, then with zero and assert `[]`.

- [ ] **Step 4: Run the new tests and confirm the old algorithm fails**

Run: `uv run pytest -q tests/unit/storage/test_repository.py`

- [ ] **Step 5: Implement randomized circular scanning**

Use `ZCOUNT available_latency 0 max_latency_ms`, select one random starting offset, and fetch in bounded batches (200 members) with `ZRANGEBYSCORE ... LIMIT offset count`. Wrap once to offset zero when reaching the end. Batch `HMGET` records, validate all constraints, and stop only after finding `requested` valid records or inspecting the complete latency-bounded range.

Required invariants:

```python
requested = min(count, 20)
candidate_count = int(
    await self._redis.zcount(keys.available_latency, 0, max_latency_ms)
)
start = random.randrange(candidate_count) if candidate_count else 0
```

- Never query `keys.quality` for random selection.
- Never inspect a member twice in one call.
- Shuffle the valid records before slicing to `requested`.
- Collect inconsistent members and remove them with one bounded `ZREM` after validation.
- Treat score, freshness, and consecutive-success failures as currently ineligible, not as corrupt index members.

- [ ] **Step 6: Add a real-Redis concurrency integration test**

While one coroutine repeatedly changes a record between `AVAILABLE` and `QUARANTINED` through repository methods, another repeatedly selects. Assert every returned decoded record is `AVAILABLE` and within the latency bound at the point it was read, and that the final ZSET state matches the final record.

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run pytest -q tests/unit/storage/test_repository.py tests/integration/test_redis_leases.py
```

- [ ] **Step 8: Commit**

```bash
git add src/ip_proxy_pool/storage/repository.py tests/unit/storage/test_repository.py tests/integration/test_redis_leases.py
git commit -m "feat: select proxies from latency index"
```

### Task 4: Add idempotent rebuild and readiness management

**Files:**
- Modify: `src/ip_proxy_pool/migration.py`
- Modify: `src/ip_proxy_pool/storage/lua.py`
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `src/ip_proxy_pool/cli.py`
- Modify: `tests/unit/test_migration.py`
- Modify: `tests/unit/test_cli.py`

- [ ] **Step 1: Add a rebuild summary model and failing tests**

Define expected output fields:

```python
class LatencyIndexRebuildSummary(BaseModel):
    domain: str
    scanned: int = 0
    indexed: int = 0
    ignored: int = 0
    dry_run: bool = False
    duration_seconds: float = 0.0
```

Tests must prove:

- only eligible records are written;
- repeated rebuilds produce the same members and scores;
- `--dry-run` changes neither the formal index nor readiness marker;
- the formal index remains unchanged until cutover;
- an empty rebuild removes an old formal ZSET but still marks the domain ready;
- a failed scan/cutover leaves the formal index and marker unchanged.

- [ ] **Step 2: Add CLI parsing tests**

Expected interface:

```bash
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com --dry-run
uv run ip-pool rebuild-latency-index --domain portal.daqihui.com
```

`--domain` is required and `--dry-run` defaults to false.

- [ ] **Step 3: Run tests and confirm failures**

Run: `uv run pytest -q tests/unit/test_migration.py tests/unit/test_cli.py`

- [ ] **Step 4: Implement batched rebuild**

Add `LatencyIndexRebuilder` that `HSCAN`s `keys.records` in batches of 500, decodes each record, applies `latency_index_score`, and writes eligible members to a unique temporary ZSET such as:

```python
temporary_key = f"{keys.available_latency}:rebuild:{uuid4().hex}"
```

Always delete the temporary key in `finally`. Do not print endpoints or serialized records.

- [ ] **Step 5: Implement atomic cutover including the empty-index case**

Add a Lua script receiving temporary index, formal index, and readiness marker. It must:

1. delete the old formal index;
2. rename the temporary index only when it exists;
3. set the readiness marker to schema version `1`;
4. return the new indexed count.

This solves the Redis empty-ZSET problem without a fake member.

- [ ] **Step 6: Wire the CLI runner**

Add `run_latency_index_rebuild(...)` beside `run_legacy_import(...)`. Emit exactly one JSON object containing the summary and return nonzero on failure through the existing CLI exception behavior.

- [ ] **Step 7: Run tests**

Run:

```bash
uv run pytest -q tests/unit/test_migration.py tests/unit/test_cli.py tests/unit/storage
```

- [ ] **Step 8: Commit**

```bash
git add src/ip_proxy_pool/migration.py src/ip_proxy_pool/storage/lua.py src/ip_proxy_pool/storage/repository.py src/ip_proxy_pool/cli.py tests/unit/test_migration.py tests/unit/test_cli.py
git commit -m "feat: rebuild latency index safely"
```

### Task 5: Enforce readiness and expose safe selection telemetry

**Files:**
- Modify: `src/ip_proxy_pool/api/dependencies.py`
- Modify: `src/ip_proxy_pool/api/routes/health.py`
- Modify: `src/ip_proxy_pool/api/routes/proxies.py`
- Modify: `src/ip_proxy_pool/observability/metrics.py`
- Modify: `tests/unit/api/test_dependencies.py`
- Modify: `tests/unit/api/test_health_routes.py`
- Modify: `tests/unit/api/test_query_routes.py`
- Modify: `tests/unit/observability/test_metrics.py`

- [ ] **Step 1: Add failing readiness route tests**

Assert `/health/ready` returns 503 when Redis is healthy but any registered domain lacks `available_latency_ready`, and 200 when every registered domain is marked. Assert `/v1/proxies/random` returns a sanitized 503 with detail `latency index not ready` for the requested unbuilt domain.

- [ ] **Step 2: Add failing metrics tests**

Extend `Metrics`, `PrometheusMetrics`, and `NoopMetrics` with bounded-cardinality methods for:

```text
ip_pool_latency_index_members{domain}
ip_pool_selectable_proxies{domain}
ip_pool_proxy_selection_total{domain,outcome}
ip_pool_proxy_selection_returned{domain}
ip_pool_proxy_selection_skipped_total{domain,reason}
ip_pool_proxy_selection_duration_seconds{domain}
```

Allowed `outcome` values are `success`, `partial`, and `empty`; allowed `reason` values are `score`, `freshness`, `successes`, and `inconsistent`.

- [ ] **Step 3: Run tests and confirm failures**

Run:

```bash
uv run pytest -q tests/unit/api/test_dependencies.py tests/unit/api/test_health_routes.py tests/unit/api/test_query_routes.py tests/unit/observability/test_metrics.py
```

- [ ] **Step 4: Expose `get_metrics` and instrument the random route**

Add a typed dependency returning `Metrics`. Measure only repository selection time with `time.perf_counter()`. Record indexed candidate count, valid count, returned count, skip reasons, duration, and one outcome per request. Do not add endpoint labels.

- [ ] **Step 5: Enforce readiness without fallback**

Add repository methods:

```python
LATENCY_INDEX_SCHEMA_VERSION = "1"

async def latency_index_ready(self, domain: str) -> bool:
    keys = keys_for(self._prefix, domain)
    version = await self._redis.get(keys.available_latency_ready)
    return version == LATENCY_INDEX_SCHEMA_VERSION

async def all_latency_indexes_ready(self) -> bool:
    for domain in await self.list_domains():
        if not await self.latency_index_ready(domain):
            return False
    return True
```

The health route checks Redis and all registered domains. The random route catches only `LatencyIndexNotReadyError` for the explicit 503; other Redis failures retain the existing sanitized `service unavailable` response.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run pytest -q tests/unit/api tests/unit/observability/test_metrics.py
```

- [ ] **Step 7: Commit**

```bash
git add src/ip_proxy_pool/api/dependencies.py src/ip_proxy_pool/api/routes/health.py src/ip_proxy_pool/api/routes/proxies.py src/ip_proxy_pool/observability/metrics.py tests/unit/api tests/unit/observability/test_metrics.py
git commit -m "feat: expose latency selection readiness and metrics"
```

### Task 6: Show actually selectable inventory on the dashboard

**Files:**
- Modify: `src/ip_proxy_pool/storage/repository.py`
- Modify: `src/ip_proxy_pool/api/dashboard_models.py`
- Modify: `src/ip_proxy_pool/dashboard/service.py`
- Modify: `src/ip_proxy_pool/api/dependencies.py`
- Modify: `src/ip_proxy_pool/static/dashboard/dashboard.js`
- Modify: `tests/unit/dashboard/test_service.py`
- Modify: `tests/unit/api/test_dashboard_page.py`

- [ ] **Step 1: Add failing dashboard-service tests**

Add `selectable` and `latency_indexed` to `DashboardSummary`. Seed records so `available=4`, `latency_indexed=3`, and only one satisfies the configured score, 1000ms latency, freshness, and consecutive-success thresholds. Assert the summary reports all three values distinctly.

- [ ] **Step 2: Add an exact count method**

Implement `selection_counts(...)` in the repository. It must count the latency-index range and batch-validate records with the same predicate used by selection, but it must not shuffle or cap at 20. Reuse the same validation helper so dashboard counts cannot drift from API behavior.

- [ ] **Step 3: Pass `SelectionSettings` into `DashboardService`**

Construct the service with both dashboard settings and the server selection policy. For a global summary, sum per-domain counts; if an index is not ready, mark the summary partial instead of silently reporting zero.

- [ ] **Step 4: Render two distinct KPIs**

Change `renderKpis` labels to show:

```javascript
["可用代理", formatInteger(summary.available), "健康状态为可用", "healthy", "proxy-available"],
["当前可选", formatInteger(summary.selectable), "满足评分、延迟与新鲜度", "healthy", "proxy-selectable"],
```

Keep `high_quality` for compatibility unless removing it is separately approved.

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
uv run pytest -q tests/unit/dashboard/test_service.py tests/unit/api/test_dashboard_page.py tests/unit/api/test_dashboard_routes.py
```

- [ ] **Step 6: Commit**

```bash
git add src/ip_proxy_pool/storage/repository.py src/ip_proxy_pool/api/dashboard_models.py src/ip_proxy_pool/dashboard/service.py src/ip_proxy_pool/api/dependencies.py src/ip_proxy_pool/static/dashboard/dashboard.js tests/unit/dashboard/test_service.py tests/unit/api/test_dashboard_page.py tests/unit/api/test_dashboard_routes.py
git commit -m "feat: display selectable proxy inventory"
```

### Task 7: Add performance and migration runbooks

**Files:**
- Create: `scripts/benchmark_selection.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/migration.md`
- Modify: `tests/unit/test_documentation.py`

- [ ] **Step 1: Add failing documentation assertions**

Assert the documentation contains `rebuild-latency-index`, the hard 1000ms production rule, no-direct-fallback rule, preflight command, final maintenance rebuild, rollback, and benchmark command.

- [ ] **Step 2: Implement a bounded benchmark script**

The script must accept Redis URL, key prefix, domain, iterations, and API-equivalent selection settings. It seeds a dedicated benchmark prefix with 9962 slow and 38 fast records, marks the domain ready, performs at least 200 warm calls, and outputs one JSON object containing `iterations`, `returned_min`, `empty_count`, `p50_ms`, and `p95_ms`. It must delete only its dedicated prefix in `finally`.

Reference command:

```bash
uv run python scripts/benchmark_selection.py \
  --redis-url redis://127.0.0.1:6379/15 \
  --key-prefix ippool:benchmark \
  --domain portal.daqihui.com \
  --iterations 500 \
  --max-latency-ms 1000 \
  --min-score 80
```

- [ ] **Step 3: Document exact rollout commands**

Use the existing Compose service names:

```bash
# Preflight while the old stack is still serving; no cutover.
docker compose run --rm api rebuild-latency-index \
  --domain portal.daqihui.com --dry-run

# After taking a Redis snapshot, stop only proxy-pool writers/readers.
docker compose stop api checker collector

# Rebuild from a stable record set and atomically mark ready.
docker compose run --rm api rebuild-latency-index \
  --domain portal.daqihui.com

# Start writers first, then API.
docker compose up -d checker collector
docker compose up -d api
```

Document that the Daqihui process is not stopped and must not bypass the proxy during this window.

- [ ] **Step 4: Document verification and rollback**

Verification must include 100 authenticated random requests with `min_score=80`, `count=20`, and `max_latency_ms=1000`; validate every returned item and report minimum count, empty count, unique endpoints, and latency P95. Rollback restores the previous image for `api`, `checker`, and `collector`; the extra Redis keys may remain because old code ignores them.

- [ ] **Step 5: Run documentation tests and benchmark against local Redis**

Run:

```bash
uv run pytest -q tests/unit/test_documentation.py
uv run python scripts/benchmark_selection.py --redis-url redis://127.0.0.1:6379/15 --key-prefix ippool:benchmark --domain portal.daqihui.com --iterations 500 --max-latency-ms 1000 --min-score 80
```

Expected benchmark: `returned_min=20`, `empty_count=0`, `p95_ms<=100`.

- [ ] **Step 6: Commit**

```bash
git add scripts/benchmark_selection.py README.md docs/operations.md docs/migration.md tests/unit/test_documentation.py
git commit -m "docs: add latency index rollout and benchmark"
```

### Task 8: Complete verification before any deployment

**Files:**
- Verify all modified files

- [ ] **Step 1: Run formatting and static analysis**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

- [ ] **Step 2: Run the complete non-Docker test suite**

Run: `uv run pytest -q -m 'not docker'`

- [ ] **Step 3: Run real-Redis and Compose tests**

Run:

```bash
uv run pytest -q tests/integration/test_redis_leases.py
uv run pytest -q -m docker tests/e2e/test_compose.py
```

- [ ] **Step 4: Run security and package checks**

```bash
uv run pip-audit
docker compose build
docker compose config --quiet
```

- [ ] **Step 5: Confirm spec coverage**

Check every goal, non-goal, failure mode, observability item, migration step, rollback step, and acceptance criterion in `docs/superpowers/specs/2026-08-17-low-latency-proxy-selection-design.md` against code or a named verification command.

- [ ] **Step 6: Scan the implementation for placeholders and unsafe fallbacks**

```bash
rg -n "TODO|FIXME|pass$|1500|2000|direct|fallback" src tests scripts docs
```

Review every match. There must be no unfinished code, threshold widening, or direct-network fallback in the selection path.

- [ ] **Step 7: Review the final diff and commit verification-only fixes**

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

If verification requires edits, rerun the affected focused tests and commit them as:

```bash
git add -A
git commit -m "test: verify low latency proxy selection"
```

### Task 9: Deploy with a short proxy-pool maintenance window

**Files:**
- Follow: `docs/operations.md`
- Follow: `docs/migration.md`

- [ ] **Step 1: Record the currently deployed image/commit and take a Redis snapshot**

Do not proceed unless both rollback identifiers are available and the snapshot completed successfully.

- [ ] **Step 2: Run the online dry-run rebuild**

Run the documented `--dry-run` command and record scanned, indexed, ignored, and duration values. Compare indexed count with an independent full-record calculation.

- [ ] **Step 3: Build the new image without changing running containers**

Run: `docker compose build api checker collector`

- [ ] **Step 4: Stop only proxy-pool API/checker/collector, rebuild, and restart**

Use the exact Task 7 commands. If rebuilding fails, restart the previous image immediately; do not start the new API without the readiness marker.

- [ ] **Step 5: Verify readiness and selection correctness**

Require:

- `/health/ready` returns 200;
- 100 consecutive requests have no sampling-caused empty result;
- every returned proxy has score at least 80 and latency at most 1000ms;
- when at least 20 fully valid proxies exist, every request returns 20;
- selection P95 is at most 100ms;
- no direct-network fallback appears in Daqihui logs.

- [ ] **Step 6: Observe for 30 minutes**

Compare proxy empty-return rate, Daqihui query-error rate, Redis latency, checker throughput, and container health with the immediately preceding 30-minute window. Roll back if correctness or availability regresses.
