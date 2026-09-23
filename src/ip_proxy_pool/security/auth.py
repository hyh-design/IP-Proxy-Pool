import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class AuthenticationError(ValueError):
    """Raised when no configured API key matches the presented value."""


class KeyRole(StrEnum):
    NORMAL = "normal"
    ADMIN = "admin"
    QUOTA_CLIENT = "quota_client"
    PEER_EXPORT = "peer_export"
    OWNERSHIP_CLIENT = "ownership_client"


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    role: KeyRole
    fingerprint: str
    member_id: str | None = None


def authenticate_key(
    raw: str | None,
    *,
    normal_keys: tuple[str, ...],
    admin_keys: tuple[str, ...],
    quota_keys: tuple[str, ...] = (),
    peer_export_keys: tuple[str, ...] = (),
    ownership_keys: Mapping[str, str] | None = None,
) -> ApiPrincipal:
    if not raw:
        raise AuthenticationError("missing API key")

    normal_match = False
    admin_match = False
    quota_match = False
    peer_export_match = False
    ownership_member: str | None = None
    for configured in normal_keys:
        normal_match |= secrets.compare_digest(raw, configured)
    for configured in admin_keys:
        admin_match |= secrets.compare_digest(raw, configured)
    for configured in quota_keys:
        quota_match |= secrets.compare_digest(raw, configured)
    for configured in peer_export_keys:
        peer_export_match |= secrets.compare_digest(raw, configured)
    for member_id, configured in (ownership_keys or {}).items():
        if secrets.compare_digest(raw, configured):
            ownership_member = member_id

    if (
        not normal_match
        and not admin_match
        and not quota_match
        and not peer_export_match
        and ownership_member is None
    ):
        raise AuthenticationError("invalid API key")
    role = (
        KeyRole.ADMIN
        if admin_match
        else KeyRole.NORMAL
        if normal_match
        else KeyRole.QUOTA_CLIENT
        if quota_match
        else KeyRole.PEER_EXPORT
        if peer_export_match
        else KeyRole.OWNERSHIP_CLIENT
    )
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return ApiPrincipal(
        role=role,
        fingerprint=fingerprint,
        member_id=ownership_member if role is KeyRole.OWNERSHIP_CLIENT else None,
    )
