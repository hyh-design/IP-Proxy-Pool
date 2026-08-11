import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, cast


class InvalidCursor(ValueError):
    """Raised when an opaque cursor cannot be authenticated or validated."""


@dataclass(frozen=True, slots=True)
class CursorData:
    domain: str
    offset: int
    filters_hash: str


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class CursorCodec:
    def __init__(self, *, secret: bytes) -> None:
        if not secret:
            raise ValueError("cursor secret must be non-empty")
        self._secret = secret

    def encode(self, *, domain: str, offset: int, filters_hash: str) -> str:
        if not domain or not filters_hash:
            raise ValueError("cursor fields must be non-empty")
        if not 0 <= offset <= 10_000:
            raise ValueError("cursor offset must be within 0..10000")
        payload = json.dumps(
            {"v": 1, "d": domain, "o": offset, "f": filters_hash},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.digest(self._secret, payload, "sha256")
        return f"{_encode_base64(payload)}.{_encode_base64(signature)}"

    def decode(
        self,
        cursor: str,
        *,
        expected_domain: str | None = None,
        expected_filters_hash: str | None = None,
    ) -> CursorData:
        try:
            payload_part, signature_part = cursor.split(".", maxsplit=1)
            payload = _decode_base64(payload_part)
            signature = _decode_base64(signature_part)
            expected_signature = hmac.digest(self._secret, payload, "sha256")
            if not hmac.compare_digest(signature, expected_signature):
                raise InvalidCursor("cursor signature is invalid")
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise InvalidCursor("cursor payload is invalid")
            value = cast(dict[str, Any], raw)
            if value.get("v") != 1:
                raise InvalidCursor("cursor version is invalid")
            domain = value.get("d")
            offset = value.get("o")
            filters_hash = value.get("f")
            if (
                not isinstance(domain, str)
                or not isinstance(offset, int)
                or isinstance(offset, bool)
                or not isinstance(filters_hash, str)
                or not 0 <= offset <= 10_000
            ):
                raise InvalidCursor("cursor fields are invalid")
        except InvalidCursor:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise InvalidCursor("cursor is invalid") from error

        if expected_domain is not None and domain != expected_domain:
            raise InvalidCursor("cursor domain does not match query")
        if expected_filters_hash is not None and filters_hash != expected_filters_hash:
            raise InvalidCursor("cursor filters do not match query")
        return CursorData(domain=domain, offset=offset, filters_hash=filters_hash)


def query_filters_hash(*values: object) -> str:
    payload = json.dumps(values, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
