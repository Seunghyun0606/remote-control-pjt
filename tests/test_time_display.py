from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from remote_control.controller.service import _recovery_detail, _recovery_suffix
from remote_control.time_display import format_korea_datetime


def test_format_korea_datetime_converts_utc_to_kst():
    value = datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)

    assert format_korea_datetime(value) == "2026-09-27 17:30:00 KST"


def test_format_korea_datetime_treats_naive_internal_timestamp_as_utc():
    value = datetime(2026, 9, 27, 8, 30)

    assert format_korea_datetime(value) == "2026-09-27 17:30:00 KST"


def test_recovery_user_output_uses_kst():
    record = SimpleNamespace(
        kind="QUOTA",
        mode="RESUME",
        attempt_count=2,
        next_retry_at=datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc),
        last_error="usage limit",
    )

    suffix = _recovery_suffix(record)
    detail = _recovery_detail(record)

    assert "retry_at=2026-09-27 18:00:00 KST" in suffix
    assert "Next retry: 2026-09-27 18:00:00 KST" in detail
    assert "+00:00" not in suffix
    assert "+00:00" not in detail
