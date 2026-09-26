from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 3


class Envelope(BaseModel):
    protocol_version: Literal[3] = PROTOCOL_VERSION
    type: str
    id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)


def message(message_type: str, **payload: Any) -> Envelope:
    return Envelope(type=message_type, payload=payload)
