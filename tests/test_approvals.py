from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from remote_control.approvals.registry import ApprovalRegistry, ApprovalStatus
from remote_control.controller.job_manager import JobManager
from remote_control.controller.service import ControllerService
from remote_control.human_gate import ApprovalOption, HumanGateRequest
from remote_control.runners.base import (
    AgentRunResult,
    AgentRunner,
    RunEventCallback,
    RunHandle,
)
from remote_control.runners.fake import FakeRunHandle
from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    JobRepository,
    SessionRepository,
)


class GateHandle(RunHandle):
    def __init__(self, on_event: RunEventCallback | None) -> None:
        self.pid = 9191
        self.session_id = "gate-session"
        self._on_event = on_event
        self._cancelled = asyncio.Event()

    async def wait(self) -> AgentRunResult:
        if self._on_event is not None:
            await self._on_event(
                {"type": "thread.started", "thread_id": "gate-session"}
            )
            await self._on_event(
                {
                    "type": "HUMAN_GATE",
                    "approval_type": "architecture_change",
                    "question": "Save Schema v3 migration을 허용할까요?",
                    "details": "Persisted schema migration is required.",
                    "options": [
                        {"key": "A", "label": "기존 Schema 유지"},
                        {"key": "B", "label": "v3 Migration 진행"},
                    ],
                }
            )
        await asyncio.wait_for(self._cancelled.wait(), timeout=1)
        return AgentRunResult(
            returncode=130,
            session_id="gate-session",
            final_message="waiting for human decision",
        )

    async def cancel(self) -> None:
        self._cancelled.set()


class GateRunner(AgentRunner):
    def __init__(self) -> None:
        self.resumed: list[dict] = []

    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        del project_id, instruction, working_directory, host_id
        return GateHandle(on_event)

    async def resume(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        self.resumed.append(
            {
                "session_id": session_id,
                "instruction": instruction,
                "working_directory": working_directory,
                "host_id": host_id,
            }
        )
        if on_event is not None:
            await on_event({"type": "thread.started", "thread_id": session_id})
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=0,
                session_id=session_id,
                final_message="completed after human decision",
            )
        )


async def wait_for_state(
    manager: JobManager,
    job_id: str,
    expected: str,
    *,
    attempts: int = 200,
) -> None:
    for _ in range(attempts):
        job = await manager.require(job_id)
        if job.state == expected:
            return
        await asyncio.sleep(0.01)
    current = await manager.require(job_id)
    raise AssertionError(f"expected {expected}, got {current.state}")


def build_manager(project_registry, database, runner: AgentRunner):
    events = EventRepository(database)
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(database),
        events=events,
    )
    sessions = SessionRegistry(
        sessions=SessionRepository(database),
        events=events,
    )
    manager = JobManager(
        projects=project_registry,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        sessions=sessions,
        approvals=approvals,
        progress_interval_seconds=0,
    )
    return manager, approvals


@pytest.mark.asyncio
async def test_approval_registry_create_and_reject(database):
    events = EventRepository(database)
    registry = ApprovalRegistry(
        approvals=ApprovalRepository(database),
        events=events,
    )
    record = await registry.create(
        job_id="JOB-1",
        project_id="demo",
        host_id="lightsail-main",
        requested_by_user="100",
        request=HumanGateRequest(
            approval_type="architecture_change",
            question="Proceed?",
            details="Needs a decision",
            options=(
                ApprovalOption("A", "Yes"),
                ApprovalOption("B", "No"),
            ),
        ),
    )

    assert record.status == ApprovalStatus.PENDING.value
    rejected = await registry.resolve(
        record.id,
        option_key=None,
        rejected=True,
    )
    assert rejected.status == ApprovalStatus.REJECTED.value


@pytest.mark.asyncio
async def test_human_gate_waits_and_text_choice_resumes_same_session(
    project_registry,
    database,
):
    runner = GateRunner()
    manager, approvals = build_manager(project_registry, database, runner)
    controller = ControllerService(projects=project_registry, jobs=manager)

    job = await manager.create(
        project_id="demo",
        instruction="implement next step",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await wait_for_state(manager, job.id, "WAITING_HUMAN")

    pending = await approvals.pending_for_user("100")
    assert len(pending) == 1
    assert pending[0].question.startswith("Save Schema")

    session = await manager.sessions.get_for_job(job.id)
    assert session is not None
    assert session.status == SessionStatus.WAITING_HUMAN.value

    response = await controller.handle_text(
        "B",
        channel="telegram",
        user_id="100",
    )
    assert "선택: B" in response
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.resumed
    assert runner.resumed[0]["session_id"] == "gate-session"
    assert "Proceed with option B" in runner.resumed[0]["instruction"]

    resolved = await approvals.get(pending[0].id)
    assert resolved.status == ApprovalStatus.RESOLVED.value
    assert resolved.selected_option == "B"


@pytest.mark.asyncio
async def test_approval_response_rejects_wrong_user(project_registry, database):
    runner = GateRunner()
    manager, approvals = build_manager(project_registry, database, runner)

    job = await manager.create(
        project_id="demo",
        instruction="implement next step",
        requested_by_channel="telegram",
        requested_by_user="100",
    )
    await wait_for_state(manager, job.id, "WAITING_HUMAN")
    pending = await approvals.pending_for_user("100")

    with pytest.raises(ValueError, match="belongs to another user"):
        await manager.respond_approval(
            pending[0].id,
            user_id="999",
            option_key="A",
        )

    await manager.cancel(job.id)
