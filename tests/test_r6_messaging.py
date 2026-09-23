from __future__ import annotations

import asyncio

import pytest

from remote_control.approvals.registry import ApprovalPrompt
from remote_control.controller.job_manager import JobManager
from remote_control.human_gate import ApprovalOption
from remote_control.messaging.slack import (
    build_approval_blocks,
    encode_approval_value,
    is_authorized,
    parse_approval_value,
)
from remote_control.runners.fake import FakeAgentRunner
from remote_control.settings import Settings
from remote_control.storage.models import JobRecord, ProjectWorkRecord
from remote_control.storage.repositories import (
    EventRepository,
    JobRepository,
    ProjectWorkRepository,
)


def test_slack_approval_payload_round_trip():
    encoded = encode_approval_value("APPROVAL-123", "B")
    assert encoded == "APPROVAL-123|B"
    assert parse_approval_value(encoded) == ("APPROVAL-123", "B")

    with pytest.raises(ValueError, match="invalid Slack approval value"):
        parse_approval_value("broken")


def test_slack_approval_blocks_and_authorization():
    prompt = ApprovalPrompt(
        id="APPROVAL-1",
        job_id="JOB-1",
        approval_type="architecture_change",
        question="Proceed with schema v3?",
        details="Migration changes the saved-data format.",
        options=(
            ApprovalOption("A", "Keep v2"),
            ApprovalOption("B", "Migrate to v3"),
        ),
        expires_at=None,
    )
    blocks = build_approval_blocks(prompt)

    action_ids = {
        element["action_id"]
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
    }
    assert {"approval_choose", "approval_details", "approval_reject"} <= action_ids
    assert is_authorized("U123", {"U123"})
    assert not is_authorized("U999", {"U123"})


def test_slack_allowlist_settings(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", " U123, U456 ,,")
    settings = Settings(_env_file=None)
    assert settings.slack_allowed_user_ids == {"U123", "U456"}


@pytest.mark.asyncio
async def test_job_notifications_are_isolated_by_messenger_channel(
    project_registry,
    database,
):
    events = EventRepository(database)
    work = ProjectWorkRepository(database)
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=FakeAgentRunner(),
        local_host_id="lightsail-main",
        project_work=work,
    )
    telegram_messages: list[tuple[str, str]] = []
    slack_messages: list[tuple[str, str]] = []

    async def telegram_notifier(user_id: str, message: str) -> None:
        telegram_messages.append((user_id, message))

    async def slack_notifier(user_id: str, message: str) -> None:
        slack_messages.append((user_id, message))

    manager.set_notifier(telegram_notifier, channel="telegram")
    manager.set_notifier(slack_notifier, channel="slack")

    telegram_job = JobRecord(
        id="JOB-TG",
        project_id="demo",
        requested_by_channel="telegram",
        requested_by_user="100",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="test",
        state="RUNNING",
    )
    slack_job = JobRecord(
        id="JOB-SLACK",
        project_id="demo",
        requested_by_channel="slack",
        requested_by_user="U123",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="test",
        state="RUNNING",
    )
    await manager.jobs.add(telegram_job)
    await manager.jobs.add(slack_job)
    await work.add(
        ProjectWorkRecord(
            job_id=slack_job.id,
            adapter="project_os",
            host_id="lightsail-main",
            task_id="TASK-043",
            role="developer",
            status="PREPARED",
        )
    )

    await manager._notify(telegram_job.id, "telegram result")
    await manager._notify(slack_job.id, "slack result")

    assert telegram_messages == [
        ("100", "[demo / JOB-TG]\ntelegram result")
    ]
    assert len(slack_messages) == 1
    assert slack_messages[0][0] == "U123"
    assert "[demo / TASK-043 / JOB-SLACK]" in slack_messages[0][1]
    assert "slack result" in slack_messages[0][1]

    await asyncio.sleep(0)
