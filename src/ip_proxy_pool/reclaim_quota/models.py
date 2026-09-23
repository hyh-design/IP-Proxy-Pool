from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    granted: bool
    retry_after_ms: int
    remaining: int
    unavailable: bool = False
