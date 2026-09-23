import hashlib
import secrets
from dataclasses import dataclass
from enum import StrEnum


class AuthenticationError(ValueError):
    """Raised when no configured API key matches the presented value."""


class KeyRole(StrEnum):
    NORMAL = "normal"
    ADMIN = "admin"
    QUOTA_CLIENT = "quota_client"
    PEER_EXPORT = "peer_export"


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    role: KeyRole
    fingerprint: str


def authenticate_key(
    raw: str | None,
    *,
    normal_keys: tuple[str, ...],
    admin_keys: tuple[str, ...],
    quota_keys: tuple[str, ...] = (),
    peer_export_keys: tuple[str, ...] = (),
) -> ApiPrincipal:
    if not raw:
        raise AuthenticationError("missing API key")

    normal_match = False
    admin_match = False
    quota_match = False
    peer_export_match = False
    for configured in normal_keys:
        normal_match |= secrets.compare_digest(raw, configured)
    for configured in admin_keys:
        admin_match |= secrets.compare_digest(raw, configured)
    for configured in quota_keys:
        quota_match |= secrets.compare_digest(raw, configured)
    for configured in peer_export_keys:
        peer_export_match |= secrets.compare_digest(raw, configured)

    if not normal_match and not admin_match and not quota_match and not peer_export_match:
        raise AuthenticationError("invalid API key")
    role = (
        KeyRole.ADMIN
        if admin_match
        else KeyRole.NORMAL
        if normal_match
        else KeyRole.QUOTA_CLIENT
        if quota_match
        else KeyRole.PEER_EXPORT
    )
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return ApiPrincipal(role=role, fingerprint=fingerprint)
