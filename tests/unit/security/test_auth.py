import pytest
from fastapi import HTTPException

from ip_proxy_pool.api.dependencies import require_ownership_client
from ip_proxy_pool.security.auth import (
    AuthenticationError,
    KeyRole,
    authenticate_key,
)


def test_admin_key_has_admin_role() -> None:
    principal = authenticate_key(
        "admin-secret",
        normal_keys=("read-secret",),
        admin_keys=("admin-secret",),
    )

    assert principal.role is KeyRole.ADMIN
    assert "admin-secret" not in repr(principal)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(AuthenticationError):
        authenticate_key(
            "wrong",
            normal_keys=("read-secret",),
            admin_keys=(),
        )


def test_admin_role_wins_for_duplicate_runtime_values() -> None:
    principal = authenticate_key(
        "shared",
        normal_keys=("shared",),
        admin_keys=("shared",),
    )

    assert principal.role is KeyRole.ADMIN


@pytest.mark.parametrize("raw", ["", None])
def test_empty_or_missing_key_is_rejected(raw: str | None) -> None:
    with pytest.raises(AuthenticationError):
        authenticate_key(raw, normal_keys=("read",), admin_keys=())


def test_fingerprint_is_stable_and_does_not_contain_key() -> None:
    first = authenticate_key("read-secret", normal_keys=("read-secret",), admin_keys=())
    second = authenticate_key("read-secret", normal_keys=("read-secret",), admin_keys=())

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 16
    assert "read-secret" not in first.fingerprint


def test_ownership_key_is_bound_to_configured_member() -> None:
    principal = authenticate_key(
        "own-b",
        normal_keys=("read",),
        admin_keys=(),
        ownership_keys={"one:a": "own-a", "two:b": "own-b"},
    )

    assert (principal.role, principal.member_id) == (KeyRole.OWNERSHIP_CLIENT, "two:b")
    assert "own-b" not in repr(principal)


@pytest.mark.asyncio
async def test_non_ownership_role_cannot_access_ownership_dependency() -> None:
    principal = authenticate_key("read", normal_keys=("read",), admin_keys=())

    with pytest.raises(HTTPException) as error:
        await require_ownership_client(principal)

    assert error.value.status_code == 403
