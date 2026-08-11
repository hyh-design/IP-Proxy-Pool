from datetime import datetime

from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.models import ProxyRecord, ProxyState


def apply_probe_result(
    record: ProxyRecord,
    result: ProbeResult,
    *,
    now: datetime,
) -> ProxyRecord:
    """Apply only proven proxy health outcomes to a record."""
    if result.category in {ProbeCategory.SYSTEM_ERROR, ProbeCategory.CANCELLED}:
        return record

    if result.category is ProbeCategory.SUCCESS:
        consecutive_successes = record.consecutive_successes + 1
        confirmed = record.state is ProxyState.AVAILABLE or consecutive_successes >= 2
        latency = record.latency_ewma_ms
        if result.latency_ms is not None:
            latency = (
                result.latency_ms
                if latency is None
                else (latency * 0.7) + (result.latency_ms * 0.3)
            )
        return record.model_copy(
            update={
                "score": min(
                    100,
                    max(80 if confirmed else 70, record.score) + 5,
                ),
                "state": (ProxyState.AVAILABLE if confirmed else ProxyState.CANDIDATE),
                "last_checked_at": now,
                "consecutive_successes": consecutive_successes,
                "consecutive_failures": 0,
                "success_count": record.success_count + 1,
                "latency_ewma_ms": latency,
                "last_status_code": result.status_code,
                "last_error_type": None,
                "last_error_message": None,
            }
        )

    consecutive_failures = record.consecutive_failures + 1
    penalty = 15 if consecutive_failures == 1 else 25
    if consecutive_failures >= 3:
        penalty = 35
    score = max(0, record.score - penalty)
    if consecutive_failures >= 3 or score == 0:
        state = ProxyState.QUARANTINED
    elif score < 50:
        state = ProxyState.DEGRADED
    else:
        state = record.state
    return record.model_copy(
        update={
            "score": score,
            "state": state,
            "last_checked_at": now,
            "consecutive_successes": 0,
            "consecutive_failures": consecutive_failures,
            "failure_count": record.failure_count + 1,
            "last_status_code": result.status_code,
            "last_error_type": result.error_type,
            "last_error_message": result.error_message,
        }
    )
