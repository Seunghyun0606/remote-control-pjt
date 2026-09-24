from __future__ import annotations

import re
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
    "you have hit your usage limit",
    "usage limit reached",
    "usage limit exceeded",
    "rate limit reached",
    "rate limit exceeded",
    "too many requests",
)
_QUOTA_CODES = (
    "usage_limit_exceeded",
    "rate_limit_exceeded",
)
_TRY_AGAIN_AT = re.compile(r"\btry again at\s+([^\n.]+)", re.IGNORECASE)
_ORDINAL_DAY = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_FULL_RESET_FORMATS = (
    "%b %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%b %d %Y %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
)


def detect_quota_text(
    text: str | None,
    *,
    now: datetime | None = None,
) -> QuotaSignal | None:
    if not text:
        return None
    lowered = text.casefold()
    is_quota = any(phrase in lowered for phrase in _QUOTA_PHRASES) or any(
        code in lowered for code in _QUOTA_CODES
    )
    if not is_quota:
        return None
    return QuotaSignal(
        message=text.strip()[:4000],
        reset_at=_extract_text_reset(text, now=now),
    )


def detect_quota_event(
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> QuotaSignal | None:
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

    reset_at = _extract_reset(event, now=now)
    for text in _text_values(event):
        signal = detect_quota_text(text, now=now)
        if signal is not None:
            return QuotaSignal(
                message=signal.message,
                reset_at=reset_at or signal.reset_at,
            )

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


def _extract_text_reset(
    text: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    match = _TRY_AGAIN_AT.search(text)
    if match is None:
        return None

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    candidate = _ORDINAL_DAY.sub("", match.group(1)).strip(" ,")
    candidate = re.sub(r"\s+", " ", candidate)

    for fmt in _FULL_RESET_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        localized = parsed.replace(tzinfo=current.tzinfo)
        if localized <= current:
            return None
        return localized.astimezone(timezone.utc)

    try:
        parsed_time = datetime.strptime(candidate, "%I:%M %p").time()
    except ValueError:
        return None
    localized = current.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )
    if localized <= current:
        # A time-only hint is ambiguous once the clock has passed it. Treat it
        # as stale instead of assuming "tomorrow", otherwise a retry a few
        # seconds after the reset boundary can incorrectly jump by ~24 hours.
        return None
    return localized.astimezone(timezone.utc)


def _extract_reset(
    value: Any,
    *,
    now: datetime | None = None,
) -> datetime | None:
    if isinstance(value, dict):
        for key in (
            "resets_at",
            "reset_at",
            "retry_at",
            "retry_after",
            "retry_after_seconds",
        ):
            if key in value:
                parsed = _parse_reset_value(key, value[key], now=now)
                if parsed is not None:
                    return parsed
        for nested in value.values():
            parsed = _extract_reset(nested, now=now)
            if parsed is not None:
                return parsed
    elif isinstance(value, list):
        for nested in value:
            parsed = _extract_reset(nested, now=now)
            if parsed is not None:
                return parsed
    return None


def _parse_reset_value(
    key: str,
    value: Any,
    *,
    now: datetime | None = None,
) -> datetime | None:
    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    current = reference.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        if key == "retry_after_seconds" or (key == "retry_after" and value < 10_000_000):
            return current + timedelta(seconds=max(float(value), 0))
        if value > 1_000_000_000:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return _parse_reset_value(key, int(stripped), now=current)
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=reference.tzinfo)
        return parsed.astimezone(timezone.utc)
    return None


def _text_values(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {
                "message",
                "text",
                "error",
                "detail",
                "reason",
                "code",
                "error_code",
                "codex_error_info",
            } and isinstance(nested, str):
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
