import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog

from ip_proxy_pool.config import ObservabilitySettings

_SECRET_KEY = re.compile(
    r"api[_-]?key|authorization|password|secret|token|redis[_-]?url",
    re.IGNORECASE,
)
_URL_CREDENTIAL = re.compile(r"(\w+://)[^/@\s]+@")


def _redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        sanitized = _URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)
        return sanitized[:256] if "error" in key.lower() else sanitized
    return value


def redact_secrets(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    del logger, method_name
    return {key: _redact(value, key=key) for key, value in event_dict.items()}


def configure_logging(
    settings: ObservabilitySettings,
    *,
    role: str = "unknown",
    environment: str = "unknown",
) -> None:
    logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            lambda logger, name, event: {
                **event,
                "role": role,
                "environment": environment,
            },
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger())
