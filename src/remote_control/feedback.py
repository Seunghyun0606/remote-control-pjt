from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class FeedbackLevel(StrEnum):
    DEBUG = "DEBUG"
    PROGRESS = "PROGRESS"
    IMPORTANT = "IMPORTANT"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    FINAL = "FINAL"


@dataclass(slots=True)
class Feedback:
    level: FeedbackLevel
    text: str


class FeedbackPolicy:
    def classify(self, event: dict) -> Feedback | None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        lowered = event_type.casefold()

        if "error" in lowered or "failed" in lowered:
            return Feedback(
                FeedbackLevel.IMPORTANT,
                _text(event) or f"Agent event: {event_type}",
            )

        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict):
                item_type = str(item.get("type") or "")
                if item_type == "agent_message":
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        return Feedback(FeedbackLevel.PROGRESS, _truncate(text.strip(), 1200))
                if item_type == "command_execution":
                    exit_code = item.get("exit_code")
                    if isinstance(exit_code, int) and exit_code != 0:
                        command = str(item.get("command") or "command")
                        return Feedback(
                            FeedbackLevel.IMPORTANT,
                            f"Command failed (exit={exit_code}): {_truncate(command, 300)}",
                        )

        if event_type in {"fake.progress", "progress"}:
            text = _text(event)
            if text:
                return Feedback(FeedbackLevel.PROGRESS, _truncate(text, 1200))

        return None


class FeedbackThrottler:
    def __init__(self, interval_seconds: int = 300) -> None:
        self.interval = timedelta(seconds=max(interval_seconds, 0))
        self._last_progress: dict[str, datetime] = {}

    def allow(self, job_id: str, feedback: Feedback, *, now: datetime | None = None) -> bool:
        if feedback.level != FeedbackLevel.PROGRESS:
            return True
        current = now or datetime.now(timezone.utc)
        previous = self._last_progress.get(job_id)
        if previous is not None and current - previous < self.interval:
            return False
        self._last_progress[job_id] = current
        return True


def _text(event: dict) -> str | None:
    for key in ("message", "text", "error"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
