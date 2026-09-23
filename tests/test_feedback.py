from datetime import datetime, timedelta, timezone

from remote_control.feedback import Feedback, FeedbackLevel, FeedbackPolicy, FeedbackThrottler


def test_agent_message_is_progress():
    feedback = FeedbackPolicy().classify(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Unit tests are running"},
        }
    )
    assert feedback is not None
    assert feedback.level == FeedbackLevel.PROGRESS


def test_failed_command_is_important():
    feedback = FeedbackPolicy().classify(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pytest",
                "exit_code": 1,
            },
        }
    )
    assert feedback is not None
    assert feedback.level == FeedbackLevel.IMPORTANT


def test_progress_is_throttled():
    throttler = FeedbackThrottler(interval_seconds=300)
    feedback = Feedback(FeedbackLevel.PROGRESS, "working")
    now = datetime.now(timezone.utc)

    assert throttler.allow("JOB-1", feedback, now=now)
    assert not throttler.allow("JOB-1", feedback, now=now + timedelta(seconds=30))
    assert throttler.allow("JOB-1", feedback, now=now + timedelta(seconds=301))


def test_important_feedback_is_not_throttled():
    throttler = FeedbackThrottler(interval_seconds=300)
    feedback = Feedback(FeedbackLevel.IMPORTANT, "failed")
    now = datetime.now(timezone.utc)

    assert throttler.allow("JOB-1", feedback, now=now)
    assert throttler.allow("JOB-1", feedback, now=now)
