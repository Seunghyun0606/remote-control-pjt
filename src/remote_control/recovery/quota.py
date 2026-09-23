from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class QuotaSignal:
    message: str
    reset_at: datetime | None = None


_QUOTA_PHRASES = (
    "you've hit your usage limit",
    "you’ve hit your usage limit",
    "usage limit reached",
    "rate limit reached",
    "rate limit exceeded",
    "too many requests",
)


def detect_quota_text(text: str | None) -> QuotaSignal | None:
    if not text:
        return None
    lowered = text.casefold()
    if not any(phrase in lowered for phrase in _QUOTA_PHRASES):
        return None
    return QuotaSignal(message=text.strip()[:4000])


def detect_quota_event(event: dict[str, Any]) -> QuotaSignal | None:
    event_type = str(event.get("type") or event.get("event_type") or "").casefold()
    typed_limit = "rate_limit" in event_type or "usage_limit" in event_type
    error_like = (
        typed_limit
        or event_type == "raw_output"
        or "error" in event_type
        or "failed" in event_type
        or "failure" in event_type
    )
    if not error_like:
        return None

    reset_at = _extract_reset(event)
    for text in _text_values(event):
        signal = detect_quota_text(text)
        if signal is not None:
            return QuotaSignal(message=signal.message, reset_at=reset_at)

    if typed_limit:
        return QuotaSignal(
            message=str(event.get("message") or event_type),
            reset_at=reset_at,
        )
    return None


def retry_at(
    *,
    attempt_count: int,
    initial_seconds: int,
    max_seconds: int,
    reset_at: datetime | None,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    if reset_at is not None:
        normalized = _aware(reset_at)
        if normalized > current:
            return normalized

    initial = max(int(initial_seconds), 1)
    maximum = max(int(max_seconds), initial)
    exponent = max(attempt_count - 1, 0)
    delay = min(initial * (2**exponent), maximum)
    return current + timedelta(seconds=delay)


def _extract_reset(value: Any) -> datetime | None:
    if isinstance(value, dict):
        for key in (
            "resets_at",
            "reset_at",
            "retry_at",
            "retry_after",
            "retry_after_seconds",
        ):
            if key in value:
                parsed = _parse_reset_value(key, value[key])
                if parsed is not None:
                    return parsed
        for nested in value.values():
            parsed = _extract_reset(nested)
            if parsed is not None:
                return parsed
    elif isinstance(value, list):
        for nested in value:
            parsed = _extract_reset(nested)
            if parsed is not None:
                return parsed
    return None


def _parse_reset_value(key: str, value: Any) -> datetime | None:
    now = datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        if key == "retry_after_seconds" or (key == "retry_after" and value < 10_000_000):
            return now + timedelta(seconds=max(float(value), 0))
        if value > 1_000_000_000:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return _parse_reset_value(key, int(stripped))
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _aware(parsed)
    return None


def _text_values(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"message", "text", "error", "detail", "reason"} and isinstance(nested, str):
                yield nested
            elif isinstance(nested, (dict, list)):
                yield from _text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _text_values(nested)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
