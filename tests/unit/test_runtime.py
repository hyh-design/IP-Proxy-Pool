import asyncio
from datetime import UTC, datetime

from ip_proxy_pool.config import Settings
from ip_proxy_pool.runtime import (
    ShutdownCoordinator,
    _start_dashboard_maintenance,
    run_dashboard_maintenance,
    target_from_settings,
    validation_targets_from_settings,
)


class Resource:
    def __init__(self) -> None:
        self.stop_called = False
        self.release_inflight_called = False
        self.close_called = False

    async def stop_new_work(self) -> None:
        self.stop_called = True

    async def release_inflight(self) -> None:
        self.release_inflight_called = True

    async def close(self) -> None:
        self.close_called = True


async def test_shutdown_releases_inflight_and_closes_resources() -> None:
    coordinator = ShutdownCoordinator(grace_seconds=0.1)
    resource = Resource()
    coordinator.register_resource(resource)

    exit_code = await coordinator.stop()

    assert exit_code == 0
    assert coordinator.stop_event.is_set()
    assert resource.stop_called
    assert resource.release_inflight_called
    assert resource.close_called


async def test_shutdown_cancels_tasks_after_grace_period() -> None:
    coordinator = ShutdownCoordinator(grace_seconds=0.01)
    started = asyncio.Event()

    async def blocked() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(blocked())
    await started.wait()
    coordinator.track_task(task)

    exit_code = await coordinator.stop()

    assert exit_code == 1
    assert task.cancelled()


def test_signal_registration_is_safe() -> None:
    coordinator = ShutdownCoordinator(grace_seconds=1)

    coordinator.install_signal_handlers()


def test_runtime_target_comes_from_settings() -> None:
    settings = Settings.model_validate(
        {
            "target": {
                "name": "daqihui",
                "url": "https://portal.daqihui.com/",
                "domain": "portal.daqihui.com",
                "expected_statuses": [200],
                "json_keys": [],
            }
        }
    )

    target = target_from_settings(settings)

    assert target.name == "daqihui"
    assert target.domain == "portal.daqihui.com"

    validators = validation_targets_from_settings(settings)
    assert validators[0].name == "ipify"
    assert validators[0].domain == "portal.daqihui.com"


class RecordingHeartbeat:
    def __init__(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self.calls: list[tuple[str, str, datetime]] = []

    async def beat(self, role: str, instance_id: str, *, now: datetime) -> int:
        self.calls.append((role, instance_id, now))
        self.stop_event.set()
        return 1


class RecordingSnapshot:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def record_if_due(self, now: datetime) -> bool:
        self.calls.append(now)
        return True


async def test_dashboard_maintenance_beats_and_records_before_stop() -> None:
    stop_event = asyncio.Event()
    heartbeat = RecordingHeartbeat(stop_event)
    recorder = RecordingSnapshot()
    now = datetime(2026, 8, 11, 8, tzinfo=UTC)

    await run_dashboard_maintenance(
        stop_event,
        role="checker",
        instance_id="checker-test",
        heartbeat=heartbeat,
        recorder=recorder,
        interval_seconds=30,
        now=lambda: now,
    )

    assert heartbeat.calls == [("checker", "checker-test", now)]
    assert recorder.calls == [now]


async def test_disabled_dashboard_does_not_start_maintenance() -> None:
    stop_event = asyncio.Event()
    heartbeat = RecordingHeartbeat(stop_event)
    settings = Settings.model_validate({"dashboard": {"enabled": False}})
    coordinator = ShutdownCoordinator()

    task = _start_dashboard_maintenance(
        settings,
        coordinator,
        role="checker",
        instance_id="checker-test",
        heartbeat=heartbeat,
        recorder=None,
    )

    assert task is None
    assert heartbeat.calls == []
