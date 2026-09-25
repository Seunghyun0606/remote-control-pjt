from __future__ import annotations

from datetime import datetime

from remote_control.recovery.quota import detect_quota_event
from remote_control.runners.codex import extract_session_id


def sanitize_agent_event(event: dict, *, now: datetime | None = None) -> dict:
    result: dict = {"type": str(event.get("type") or "agent_event")}
    event_type = event.get("event_type")
    if isinstance(event_type, str):
        result["event_type"] = _truncate(event_type, 128)

    session_id = extract_session_id(event)
    if session_id:
        result["thread_id"] = _truncate(session_id, 255)

    for key in ("message", "text", "error"):
        value = event.get(key)
        if isinstance(value, str):
            result[key] = _truncate(value, 1200)

    for key in (
        "question",
        "prompt",
        "details",
        "description",
        "header",
        "approval_type",
    ):
        value = event.get(key)
        if isinstance(value, str):
            result[key] = _truncate(value, 2000)

    for key in (
        "resets_at",
        "reset_at",
        "retry_at",
        "retry_after",
        "retry_after_seconds",
    ):
        value = event.get(key)
        if isinstance(value, (str, int, float)):
            result[key] = value

    quota = detect_quota_event(event, now=now)
    if quota is not None and quota.reset_at is not None:
        result["reset_at"] = quota.reset_at.isoformat()

    options = event.get("options")
    if isinstance(options, list):
        result["options"] = [_sanitize_option(item) for item in options[:8]]

    item = event.get("item")
    if isinstance(item, dict):
        clean_item: dict = {"type": _truncate(str(item.get("type") or ""), 128)}
        for key in (
            "text",
            "content",
            "command",
            "status",
            "question",
            "prompt",
            "details",
            "description",
            "header",
            "approval_type",
        ):
            value = item.get(key)
            if isinstance(value, str):
                limit = 400 if key == "command" else 1200
                clean_item[key] = _truncate(value, limit)
        item_options = item.get("options")
        if isinstance(item_options, list):
            clean_item["options"] = [
                _sanitize_option(option) for option in item_options[:8]
            ]
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int):
            clean_item["exit_code"] = exit_code
        result["item"] = clean_item

    return result


def _sanitize_option(value):
    if isinstance(value, dict):
        clean: dict = {}
        for key, item in list(value.items())[:8]:
            if isinstance(item, str):
                clean[str(key)[:64]] = _truncate(item, 500)
            elif isinstance(item, (int, float, bool)) or item is None:
                clean[str(key)[:64]] = item
        return clean
    if isinstance(value, str):
        return _truncate(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _truncate(str(value), 500)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
