"""Strict, cycle-aware ownership data models."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_ENTRY_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)


class LeadCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: Literal["portal.daqihui.com"]
    lead_code: str = Field(min_length=1, max_length=256)
    record_id: int = Field(gt=0, strict=True)
    entered_public_pool_at: str | None = Field(default=None, max_length=48)

    def reliable_entry_time(self) -> str | None:
        raw = self.entered_public_pool_at
        if raw is None or _ENTRY_TIME.fullmatch(raw) is None:
            return None
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class SuccessReceipt:
    member_id: str
    event_id: str
    server_time: int
    expires_at: int
