from uuid import uuid4

from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import OwnershipMember, Settings


def test_two_api_instances_share_cases_and_server_bound_members(isolated_redis: str) -> None:
    configured = Settings.model_validate(
        {
            "redis": {"url": isolated_redis, "key_prefix": "owner-api"},
            "api": {"api_keys": ["read-key"], "cursor_secret": "x" * 32},
            "ownership": {
                "enabled": True,
                "members": {
                    "one:a": {"system_id": "system-one", "api_key": "own-a"},
                    "two:b": {"system_id": "system-two", "api_key": "own-b"},
                },
            },
        }
    )
    case_id = str(uuid4())
    body = {
        "schema_version": 1,
        "case_id": case_id,
        "cycle": {
            "domain": "portal.daqihui.com",
            "lead_code": "L-42",
            "record_id": 42,
            "entered_public_pool_at": "2026-09-23 08:00:00",
        },
    }
    with (
        TestClient(create_app(configured), raise_server_exceptions=False) as first,
        TestClient(create_app(configured), raise_server_exceptions=False) as second,
    ):
        created = first.post(
            "/v1/reclaim/ownership/cases",
            json={**body, "members": ["one:a"]},
            headers={"X-API-Key": "own-a"},
        )
        assert created.status_code == 422
        created = first.post(
            "/v1/reclaim/ownership/cases", json=body, headers={"X-API-Key": "own-a"}
        )
        assert created.status_code == 200
        assert created.json()["members"] == ["one:a", "two:b"]
        assert created.json()["creator_member"] == "one:a"
        fetched = second.get(
            f"/v1/reclaim/ownership/cases/{case_id}",
            params={"schema_version": 1},
            headers={"X-API-Key": "own-b"},
        )
        assert fetched.status_code == 200, fetched.json()
        assert fetched.json()["case_id"] == case_id
        expanded_ownership = configured.ownership.model_copy(
            update={
                "members": {
                    **configured.ownership.members,
                    "one:c": OwnershipMember(system_id="system-one", api_key="own-c"),
                }
            }
        )
        third_settings = configured.model_copy(update={"ownership": expanded_ownership})
        with TestClient(create_app(third_settings), raise_server_exceptions=False) as third:
            denied = third.get(
                f"/v1/reclaim/ownership/cases/{case_id}",
                params={"schema_version": 1},
                headers={"X-API-Key": "own-c"},
            )
            assert denied.status_code == 403
        job = second.get(
            "/v1/reclaim/ownership/jobs/next",
            params={"schema_version": 1},
            headers={"X-API-Key": "own-b"},
        )
        assert job.status_code == 200
        assert job.json()["case_id"] == case_id
        assert job.json()["round_no"] == 1
        checked = second.post(
            "/v1/reclaim/ownership/checks",
            json={
                "schema_version": 1,
                "case_id": case_id,
                "round_no": 1,
                "result": "found",
                "result_id": str(uuid4()),
                "lease_token": job.json()["lease_token"],
            },
            headers={"X-API-Key": "own-b"},
        )
        assert checked.status_code == 200
        assert checked.json()["status"] == "internal"
        event_id = str(uuid4())
        published = second.post(
            "/v1/reclaim/ownership/success",
            json={"schema_version": 1, "event_id": event_id, "cycle": body["cycle"]},
            headers={"X-API-Key": "own-b"},
        )
        assert published.status_code == 200
        assert published.json()["member_id"] == "two:b"
        replay = second.post(
            "/v1/reclaim/ownership/success",
            json={"schema_version": 1, "event_id": event_id, "cycle": body["cycle"]},
            headers={"X-API-Key": "own-b"},
        )
        assert replay.json() == published.json()
        conflict = first.post(
            "/v1/reclaim/ownership/success",
            json={"schema_version": 1, "event_id": str(uuid4()), "cycle": body["cycle"]},
            headers={"X-API-Key": "own-a"},
        )
        assert conflict.status_code == 409
        heartbeat = second.post(
            "/v1/reclaim/ownership/heartbeat",
            json={"schema_version": 1, "success_backlog": 2},
            headers={"X-API-Key": "own-b"},
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["member_id"] == "two:b"
        metrics = first.get("/metrics")
        assert metrics.status_code == 200
        assert 'ip_pool_reclaim_ownership_success_backlog{member_id="two:b"} 2.0' in metrics.text
        assert "ip_pool_reclaim_ownership_revision_conflicts 1.0" in metrics.text
