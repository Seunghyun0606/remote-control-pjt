from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_AGENT = "WAITING_AGENT"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_HOST = "WAITING_HOST"
    WAITING_QUOTA = "WAITING_QUOTA"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


TERMINAL_STATES = {JobState.FAILED, JobState.CANCELLED, JobState.COMPLETED}

_ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.ASSIGNED, JobState.CANCELLED, JobState.WAITING_HOST},
    JobState.ASSIGNED: {JobState.STARTING, JobState.CANCELLED, JobState.WAITING_HOST},
    JobState.STARTING: {
        JobState.RUNNING,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.WAITING_HOST,
        JobState.WAITING_QUOTA,
    },
    JobState.RUNNING: {
        JobState.WAITING_AGENT,
        JobState.WAITING_HUMAN,
        JobState.WAITING_HOST,
        JobState.WAITING_QUOTA,
        JobState.PAUSED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.COMPLETED,
    },
    JobState.WAITING_AGENT: {JobState.RUNNING, JobState.FAILED, JobState.CANCELLED},
    JobState.WAITING_HUMAN: {JobState.RUNNING, JobState.FAILED, JobState.CANCELLED},
    JobState.WAITING_HOST: {JobState.ASSIGNED, JobState.CANCELLED, JobState.FAILED},
    JobState.WAITING_QUOTA: {
        JobState.RUNNING,
        JobState.STARTING,
        JobState.CANCELLED,
        JobState.FAILED,
    },
    JobState.PAUSED: {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED},
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
    JobState.COMPLETED: set(),
}


class InvalidJobTransition(ValueError):
    pass


def validate_transition(current: JobState, target: JobState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransition(f"invalid job transition: {current} -> {target}")
