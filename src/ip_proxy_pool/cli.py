import argparse
import asyncio
from collections.abc import Sequence

from ip_proxy_pool.config import get_settings
from ip_proxy_pool.observability.logging import configure_logging
from ip_proxy_pool.runtime import (
    ShutdownCoordinator,
    doctor,
    run_all,
    run_api,
    run_checker,
    run_collector,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ip-pool")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("api", help="run the API role")
    collect = commands.add_parser("collect", help="run the collector role")
    collect.add_argument("--region", choices=("domestic", "foreign", "all"), default="all")
    commands.add_parser("check", help="run the checker role")
    commands.add_parser("all", help="run all roles for development")
    commands.add_parser("doctor", help="run configuration and dependency checks")
    legacy = commands.add_parser("import-legacy", help="import an old proxy ZSET")
    legacy.add_argument("--redis-key", required=True)
    legacy.add_argument("--domain", required=True)
    legacy.add_argument("--dry-run", action="store_true")
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(
        settings.observability,
        role=arguments.command,
        environment=settings.environment,
    )
    if arguments.command == "doctor":
        return await doctor(settings)
    if arguments.command == "import-legacy":
        from ip_proxy_pool.migration import run_legacy_import

        return await run_legacy_import(
            settings,
            redis_key=arguments.redis_key,
            domain=arguments.domain,
            dry_run=arguments.dry_run,
        )

    coordinator = ShutdownCoordinator()
    coordinator.install_signal_handlers()
    if arguments.command == "api":
        return await run_api(settings, coordinator)
    if arguments.command == "collect":
        return await run_collector(settings, coordinator, region=arguments.region)
    if arguments.command == "check":
        return await run_checker(settings, coordinator)
    return await run_all(settings, coordinator)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return asyncio.run(_run(arguments))
