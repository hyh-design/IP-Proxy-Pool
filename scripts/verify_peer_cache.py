"""Read-only peer-cache release checks; never print credentials or raw responses."""

import argparse
import json
import stat
import subprocess
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path


def command(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode:
        raise RuntimeError(f"check failed: {arguments[0]} {arguments[1]}")
    return result.stdout.strip()


def request(base: str, path: str, *, key: str | None = None) -> bytes:
    headers = {"X-API-Key": key} if key else {}
    with urllib.request.urlopen(
        urllib.request.Request(base + path, headers=headers), timeout=4
    ) as response:
        return response.read(256 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--domain", default="portal.daqihui.com")
    parser.add_argument("--read-key-file", type=Path)
    parser.add_argument("--require-peer-ready", action="store_true")
    arguments = parser.parse_args()
    parsed = urllib.parse.urlsplit(arguments.api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        parser.error("API URL must be loopback HTTP")
    base = arguments.api_url.rstrip("/")
    compose_dir = arguments.compose_dir.resolve()
    containers = command(
        "docker",
        "compose",
        "--profile",
        "peer-cache",
        "ps",
        "--format",
        "json",
        cwd=compose_dir,
    )
    statuses = []
    health: dict[str, str] = {}
    for line in containers.splitlines():
        item = json.loads(line)
        name = item.get("Service", "?")
        statuses.append((name, item.get("State", "?")))
        health[name] = item.get("Health", "")
    print("services=" + ",".join(f"{name}:{state}" for name, state in sorted(statuses)))
    image = command(
        "docker",
        "image",
        "inspect",
        "ip-proxy-pool:local",
        "--format",
        "{{.Id}}",
        cwd=compose_dir,
    )
    print("image_id=" + image)
    ready = json.loads(request(base, "/health/ready"))
    print("api_ready=" + str(ready.get("status") == "ready").lower())
    metrics = request(base, "/metrics").decode("utf-8")
    allowed_metrics = (
        "ip_pool_peer_metrics_available",
        "ip_pool_peer_valid_candidates",
        "ip_pool_peer_consecutive_sync_failures",
        "ip_pool_peer_sync_heartbeat_timestamp_seconds",
        "ip_pool_peer_last_success_timestamp_seconds",
        "ip_pool_peer_evictions_10m",
    )
    peer_values: dict[str, list[float]] = {}
    for line in metrics.splitlines():
        if line.startswith(allowed_metrics):
            print(line)
            name = line.split("{", 1)[0]
            with suppress(ValueError):
                peer_values.setdefault(name, []).append(float(line.rsplit(" ", 1)[-1]))
    if arguments.require_peer_ready:
        running = dict(statuses)
        if running.get("peer-tunnel") != "running" or running.get("peer-sync") != "running":
            raise RuntimeError("peer services not running")
        if health.get("peer-tunnel") != "healthy" or health.get("peer-sync") != "healthy":
            raise RuntimeError("peer services not healthy")
        if 1.0 not in peer_values.get("ip_pool_peer_metrics_available", []):
            raise RuntimeError("peer metrics unavailable")
        if max(peer_values.get("ip_pool_peer_valid_candidates", [0.0])) < 5:
            raise RuntimeError("peer inventory below release gate")
    if arguments.read_key_file is not None:
        key_file = arguments.read_key_file.resolve()
        if stat.S_IMODE(key_file.stat().st_mode) & 0o077:
            parser.error("read-key-file must not be group/world accessible")
        key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            parser.error("read-key-file is empty")
        query = urllib.parse.urlencode({"domain": arguments.domain, "limit": 20})
        sources = json.loads(request(base, f"/v1/dashboard/sources?{query}", key=key))
        print(
            "source_counts="
            + ",".join(
                f"{item['name']}:{item['available']}/{item['total']}"
                for item in sources.get("items", [])
            )
        )
    else:
        print("source_counts=not_requested")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit(f"verification failed: {type(error).__name__}") from None
