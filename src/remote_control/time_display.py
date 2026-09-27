from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


def format_korea_datetime(value: datetime | None) -> str:
    """Format an internal UTC timestamp for user-facing Korea time."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KOREA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S KST")
