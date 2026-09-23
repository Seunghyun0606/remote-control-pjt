from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HostStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


@dataclass(slots=True)
class HostInfo:
    id: str
    name: str
    os: str
    status: HostStatus
    capabilities: set[str]
    last_heartbeat: datetime | None
