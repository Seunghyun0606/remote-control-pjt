import pytest

from remote_control.controller.states import InvalidJobTransition, JobState, validate_transition


def test_valid_transition():
    validate_transition(JobState.QUEUED, JobState.ASSIGNED)
    validate_transition(JobState.RUNNING, JobState.COMPLETED)
    validate_transition(JobState.RUNNING, JobState.CANCELLING)
    validate_transition(JobState.CANCELLING, JobState.CANCELLED)


def test_invalid_transition():
    with pytest.raises(InvalidJobTransition):
        validate_transition(JobState.QUEUED, JobState.COMPLETED)
