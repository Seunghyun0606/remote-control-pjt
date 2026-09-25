from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

from remote_control.approvals.registry import ApprovalRegistry, ApprovalStatus
from remote_control.controller.job_manager import JobManager, RESTART_RESUME_INSTRUCTION
from remote_control.hosts.models import HostStatus
from remote_control.hosts.registry import HostRegistry
from remote_control.human_gate import ApprovalOption, HumanGateRequest
from remote_control.projects.models import ProjectDefinition, RepositoryConfig
from remote_control.projects.registry import ProjectRegistry
from remote_control.recovery.models import RecoveryKind, RecoveryMode
from remote_control.recovery.quota import detect_quota_event, detect_quota_text, retry_at
from remote_control.recovery.scheduler import RecoveryScheduler
from remote_control.runners.base import AgentRunResult, AgentRunner, RunEventCallback, RunHandle
from remote_control.runner_daemon import _sanitize_event
from remote_control.runners.fake import FakeAgentRunner, FakeRunHandle
from remote_control.sessions.registry import SessionRegistry
from remote_control.storage.models import JobRecord
from remote_control.storage.repositories import (
    ApprovalRepository,
    EventRepository,
    HostRepository,
    JobRepository,
    RecoveryRepository,
    SessionRepository,
)
from remote_control.transport.protocol import Envelope, message
from remote_control.transport.runner_ws import RunnerGateway


class QuotaTwiceRunner(AgentRunner):
    def __init__(self) -> None:
        self.started = 0
        self.resumed = 0

    async def start(
        self,
        *,
        project_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        del project_id, instruction, working_directory, host_id, on_event
        self.started += 1
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=1,
                session_id="quota-session",
                final_message="You've hit your usage limit.",
                retry_kind="quota",
            )
        )

    async def resume(
        self,
        *,
        session_id: str,
        instruction: str,
        working_directory: Path,
        host_id: str | None = None,
        on_event: RunEventCallback | None = None,
    ) -> RunHandle:
        del session_id, instruction, working_directory, host_id, on_event
        self.resumed += 1
        if self.resumed == 1:
            return FakeRunHandle(
                result=AgentRunResult(
                    returncode=1,
                    session_id="quota-session",
                    final_message="Usage limit reached.",
                    retry_kind="quota",
                )
            )
        return FakeRunHandle(
            result=AgentRunResult(
                returncode=0,
                session_id="quota-session",
                final_message="completed after quota recovery",
            )
        )


def message_from_text(text: str) -> Envelope:
    return Envelope.model_validate_json(text)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[int] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


async def wait_for_state(
    manager: JobManager,
    job_id: str,
    expected: str,
    *,
    attempts: int = 300,
) -> None:
    for _ in range(attempts):
        job = await manager.require(job_id)
        if job.state == expected:
            if expected in {"COMPLETED", "FAILED", "CANCELLED"}:
                await manager.wait_until_idle(job_id)
            return
        await asyncio.sleep(0.01)
    current = await manager.require(job_id)
    raise AssertionError(f"expected {expected}, got {current.state}")


async def build_runtime(
    *,
    database,
    projects: ProjectRegistry,
    runner: AgentRunner,
    heartbeat_timeout_seconds: int = 45,
    quota_initial_seconds: int = 1,
    quota_max_seconds: int = 4,
    restart_grace_seconds: int = 0,
):
    events = EventRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )
    await hosts.register_local(
        name="Lightsail",
        os_name="linux",
        capabilities={"codex", "git", "long_running"},
    )
    sessions = SessionRegistry(
        sessions=SessionRepository(database),
        events=events,
    )
    approvals = ApprovalRegistry(
        approvals=ApprovalRepository(database),
        events=events,
    )
    recovery = RecoveryRepository(database)
    manager = JobManager(
        projects=projects,
        jobs=JobRepository(database),
        events=events,
        runner=runner,
        local_host_id="lightsail-main",
        hosts=hosts,
        sessions=sessions,
        approvals=approvals,
        recovery=recovery,
        quota_retry_initial_seconds=quota_initial_seconds,
        quota_retry_max_seconds=quota_max_seconds,
        restart_grace_seconds=restart_grace_seconds,
        progress_interval_seconds=0,
    )
    return manager, hosts, sessions, approvals, recovery


def remote_project(path: Path, *, auto_fallback: bool = False) -> ProjectRegistry:
    paths = {"desktop-main": str(path)}
    allowed = ["desktop-main"]
    if auto_fallback:
        paths["lightsail-main"] = str(path)
        allowed.append("lightsail-main")
    return ProjectRegistry(
        {
            "demo": ProjectDefinition(
                id="demo",
                name="Demo",
                adapter="generic_git",
                repository=RepositoryConfig(path=paths),
                allowed_hosts=allowed,
                default_host="desktop-main",
            )
        }
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_quota_retry_backoff_and_structured_reset():
    now = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    assert retry_at(
        attempt_count=1,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=None,
        now=now,
    ) == now + timedelta(minutes=30)
    assert retry_at(
        attempt_count=2,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=None,
        now=now,
    ) == now + timedelta(hours=1)
    assert retry_at(
        attempt_count=4,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=None,
        now=now,
    ) == now + timedelta(hours=2)

    reset = now + timedelta(minutes=47)
    assert retry_at(
        attempt_count=1,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=reset,
        reset_grace_seconds=600,
        now=now,
    ) == reset + timedelta(minutes=10)

    just_after_reset = reset + timedelta(minutes=2)
    assert retry_at(
        attempt_count=2,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=reset,
        reset_grace_seconds=600,
        now=just_after_reset,
    ) == reset + timedelta(minutes=10)

    after_grace = reset + timedelta(minutes=11)
    assert retry_at(
        attempt_count=2,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=reset,
        reset_grace_seconds=600,
        now=after_grace,
    ) == after_grace + timedelta(hours=1)

    signal = detect_quota_event(
        {
            "type": "error",
            "message": "You've hit your usage limit.",
            "reset_at": reset.isoformat(),
        }
    )
    assert signal is not None
    assert signal.reset_at == reset

    seoul = ZoneInfo("Asia/Seoul")
    local_now = datetime(2026, 9, 24, 17, 37, 8, tzinfo=seoul)

    stale_time_only = detect_quota_text(
        "You've hit your usage limit. Try again at 5:37 PM.",
        now=local_now,
    )
    assert stale_time_only is not None
    assert stale_time_only.reset_at is None
    assert retry_at(
        attempt_count=2,
        initial_seconds=1800,
        max_seconds=7200,
        reset_at=stale_time_only.reset_at,
        now=local_now.astimezone(timezone.utc),
    ) == local_now.astimezone(timezone.utc) + timedelta(hours=1)

    future_time_only = detect_quota_text(
        "You've hit your usage limit. Try again at 5:38 PM.",
        now=local_now,
    )
    assert future_time_only is not None
    assert future_time_only.reset_at == datetime(
        2026,
        9,
        24,
        8,
        38,
        tzinfo=timezone.utc,
    )

    naive_structured = detect_quota_event(
        {
            "type": "error",
            "message": "You've hit your usage limit.",
            "reset_at": "2026-09-24T17:40:00",
        },
        now=local_now,
    )
    assert naive_structured is not None
    assert naive_structured.reset_at == datetime(
        2026,
        9,
        24,
        8,
        40,
        tzinfo=timezone.utc,
    )

    sanitized = _sanitize_event(
        {
            "type": "error",
            "message": "You've hit your usage limit. Try again at 5:38 PM.",
        },
        now=local_now,
    )
    assert sanitized["reset_at"] == "2026-09-24T08:38:00+00:00"

    text_signal = detect_quota_text(
        "You've hit your usage limit. Add credits to continue, "
        "or try again at Sep 25th, 2026 2:20 PM.",
        now=now,
    )
    assert text_signal is not None
    assert text_signal.reset_at == datetime(
        2026,
        9,
        25,
        14,
        20,
        tzinfo=timezone.utc,
    )

    coded_signal = detect_quota_event(
        {
            "type": "error",
            "message": "request rejected",
            "codex_error_info": "usage_limit_exceeded",
        }
    )
    assert coded_signal is not None

    assert (
        detect_quota_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "text": "External API returned rate limit exceeded",
                },
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_quota_wait_retries_and_preserves_attempt_count(
    project_registry,
    database,
):
    runner = QuotaTwiceRunner()
    manager, hosts, _, _, recovery = await build_runtime(
        database=database,
        projects=project_registry,
        runner=runner,
    )
    scheduler = RecoveryScheduler(jobs=manager, hosts=hosts, interval_seconds=1)

    job = await manager.create(
        project_id="demo",
        instruction="work until quota",
        requested_by_channel="test",
        requested_by_user="u1",
    )
    await wait_for_state(manager, job.id, "WAITING_QUOTA")

    first = await recovery.get(job.id)
    assert first is not None
    assert first.attempt_count == 1
    assert first.kind == RecoveryKind.QUOTA.value

    await scheduler.tick(now=_aware(first.next_retry_at) + timedelta(seconds=1))
    await wait_for_state(manager, job.id, "WAITING_QUOTA")

    second = await recovery.get(job.id)
    assert second is not None
    assert second.attempt_count == 2
    assert second.next_retry_at is not None

    await scheduler.tick(now=_aware(second.next_retry_at) + timedelta(seconds=1))
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.started == 1
    assert runner.resumed == 2
    assert await recovery.get(job.id) is None


@pytest.mark.asyncio
async def test_explicit_offline_host_waits_then_redispatches(tmp_path, database):
    project_path = tmp_path / "remote-project"
    project_path.mkdir()
    projects = remote_project(project_path)
    runner = FakeAgentRunner()
    manager, hosts, _, _, recovery = await build_runtime(
        database=database,
        projects=projects,
        runner=runner,
    )

    job = await manager.create(
        project_id="demo",
        instruction="run on desktop",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="desktop-main",
    )
    assert job.state == "WAITING_HOST"
    record = await recovery.get(job.id)
    assert record is not None
    assert record.mode == RecoveryMode.START.value

    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex", "git"},
    )
    await manager.recover_due(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.started
    assert runner.started[0]["host_id"] == "desktop-main"


@pytest.mark.asyncio
async def test_auto_routing_falls_back_to_online_host(tmp_path, database):
    project_path = tmp_path / "fallback-project"
    project_path.mkdir()
    projects = remote_project(project_path, auto_fallback=True)
    runner = FakeAgentRunner()
    manager, _, _, _, _ = await build_runtime(
        database=database,
        projects=projects,
        runner=runner,
    )

    job = await manager.create(
        project_id="demo",
        instruction="auto route",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="auto",
    )
    await wait_for_state(manager, job.id, "COMPLETED")

    assert runner.started[0]["host_id"] == "lightsail-main"


@pytest.mark.asyncio
async def test_heartbeat_expiry_persists_offline(database):
    events = EventRepository(database)
    hosts = HostRegistry(
        hosts=HostRepository(database),
        events=events,
        local_host_id="lightsail-main",
        heartbeat_timeout_seconds=1,
    )
    await hosts.register_local(
        name="Lightsail",
        os_name="linux",
        capabilities={"codex"},
    )
    desktop = await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex"},
    )
    assert desktop.last_heartbeat is not None

    expired = await hosts.expire_stale(
        now=_aware(desktop.last_heartbeat) + timedelta(seconds=2)
    )
    assert expired == ["desktop-main"]
    current = await hosts.get("desktop-main")
    assert current is not None
    assert current.status == HostStatus.OFFLINE


@pytest.mark.asyncio
async def test_controller_restart_recovers_local_session(
    monkeypatch,
    project_registry,
    database,
):
    async def terminate(pid, *, working_directory, timeout_seconds):
        del pid, working_directory, timeout_seconds
        return True

    monkeypatch.setattr(
        "remote_control.controller.job_manager.terminate_persisted_codex_process",
        terminate,
    )
    runner = FakeAgentRunner()
    manager, _, sessions, _, recovery = await build_runtime(
        database=database,
        projects=project_registry,
        runner=runner,
        restart_grace_seconds=0,
    )
    job = JobRecord(
        id="JOB-RESTART-LOCAL",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="unfinished work",
        state="RUNNING",
        external_session_id="thread-old",
        pid=4242,
    )
    await manager.jobs.add(job)
    await sessions.record(
        job_id=job.id,
        project_id="demo",
        host_id="lightsail-main",
        external_session_id="thread-old",
    )

    now = datetime.now(timezone.utc)
    assert await manager.reconcile_startup(now=now) == 1
    waiting = await manager.require(job.id)
    assert waiting.state == "WAITING_HOST"

    record = await recovery.get(job.id)
    assert record is not None
    assert record.kind == RecoveryKind.RESTART.value
    assert record.mode == RecoveryMode.RESUME.value

    await manager.recover_due(now=now + timedelta(seconds=1))
    await wait_for_state(manager, job.id, "COMPLETED")
    assert runner.resumed
    assert runner.resumed[0]["session_id"] == "thread-old"


@pytest.mark.asyncio
async def test_remote_running_job_is_adopted_after_controller_restart(tmp_path, database):
    project_path = tmp_path / "adopt-project"
    project_path.mkdir()
    projects = remote_project(project_path)
    runner = FakeAgentRunner()
    manager, hosts, sessions, _, recovery = await build_runtime(
        database=database,
        projects=projects,
        runner=runner,
        restart_grace_seconds=30,
    )
    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex", "git"},
    )

    job = JobRecord(
        id="JOB-RESTART-REMOTE",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="desktop-main",
        assigned_host="desktop-main",
        instruction="unfinished remote work",
        state="RUNNING",
        external_session_id="thread-remote",
    )
    await manager.jobs.add(job)
    await sessions.record(
        job_id=job.id,
        project_id="demo",
        host_id="desktop-main",
        external_session_id="thread-remote",
    )
    await recovery.upsert(
        job.id,
        kind=RecoveryKind.RESTART.value,
        mode=RecoveryMode.ADOPT.value,
        attempt_count=0,
        next_retry_at=None,
        execution_id="exec-existing",
        resume_instruction=None,
        last_error=None,
    )

    await manager.reconcile_startup(now=datetime.now(timezone.utc))
    assert (await manager.require(job.id)).state == "WAITING_HOST"

    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)
    adopted = await manager.reconcile_runner(
        host_id="desktop-main",
        running_jobs=[
            {"execution_id": "exec-existing", "session_id": "thread-remote"}
        ],
        completed_jobs=[],
        gateway=gateway,
    )
    assert adopted == 1
    assert (await manager.require(job.id)).state == "RUNNING"

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id="exec-existing",
            returncode=0,
            session_id="thread-remote",
            final_message="remote work completed",
        ),
    )
    await wait_for_state(manager, job.id, "COMPLETED")
    assert not runner.started
    assert not runner.resumed


@pytest.mark.asyncio
async def test_pending_remote_cancel_is_readopted_and_confirmed_after_reconnect(
    tmp_path,
    database,
):
    project_path = tmp_path / "cancel-reconnect-project"
    project_path.mkdir()
    projects = remote_project(project_path)
    manager, hosts, sessions, _, recovery = await build_runtime(
        database=database,
        projects=projects,
        runner=FakeAgentRunner(),
    )
    await hosts.register(
        host_id="desktop-main",
        name="Desktop",
        os_name="windows",
        capabilities={"codex", "git"},
    )

    job = JobRecord(
        id="JOB-CANCEL-RECONNECT",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="desktop-main",
        assigned_host="desktop-main",
        instruction="cancel me safely",
        state="CANCELLING",
        external_session_id="thread-cancel",
    )
    await manager.jobs.add(job)
    await sessions.record(
        job_id=job.id,
        project_id="demo",
        host_id="desktop-main",
        external_session_id="thread-cancel",
    )
    await recovery.upsert(
        job.id,
        kind=RecoveryKind.HOST.value,
        mode=RecoveryMode.CANCEL.value,
        attempt_count=0,
        next_retry_at=None,
        execution_id="exec-cancel",
        resume_instruction=None,
        last_error="runner disconnected during cancellation",
    )

    gateway = RunnerGateway(cancel_ack_timeout_seconds=1)
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    adopted = await manager.reconcile_runner(
        host_id="desktop-main",
        running_jobs=[
            {"execution_id": "exec-cancel", "session_id": "thread-cancel"}
        ],
        completed_jobs=[],
        gateway=gateway,
    )
    assert adopted == 1

    cancel_message = None
    for _ in range(50):
        if websocket.sent:
            cancel_message = message_from_text(websocket.sent[-1])
            if cancel_message.type == "JOB_CANCEL":
                break
        await asyncio.sleep(0)
    assert cancel_message is not None
    assert cancel_message.type == "JOB_CANCEL"
    assert cancel_message.payload["execution_id"] == "exec-cancel"
    assert (await manager.require(job.id)).state == "CANCELLING"

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id="exec-cancel",
            returncode=130,
            session_id="thread-cancel",
            final_message="cancelled",
        ),
    )
    await wait_for_state(manager, job.id, "CANCELLED")
    assert await recovery.get(job.id) is None


@pytest.mark.asyncio
async def test_runner_disconnect_is_classified_as_host_retry():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)
    handle = await gateway.start_remote(
        host_id="desktop-main",
        project_id="demo",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )

    await gateway.detach("desktop-main", websocket)
    result = await handle.wait()
    assert result.returncode == 75
    assert result.retry_kind == "host"


@pytest.mark.asyncio
async def test_scheduler_expires_human_gate(project_registry, database):
    runner = FakeAgentRunner()
    manager, hosts, _, approvals, _ = await build_runtime(
        database=database,
        projects=project_registry,
        runner=runner,
    )
    job = JobRecord(
        id="JOB-APPROVAL-EXPIRE",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="waiting decision",
        state="WAITING_HUMAN",
    )
    await manager.jobs.add(job)
    now = datetime.now(timezone.utc)
    approval = await approvals.create(
        job_id=job.id,
        project_id="demo",
        host_id="lightsail-main",
        requested_by_user="u1",
        request=HumanGateRequest(
            approval_type="architecture_change",
            question="Proceed?",
            details="Needs a decision",
            options=(ApprovalOption("A", "Proceed"),),
            expires_at=now - timedelta(seconds=1),
        ),
    )

    scheduler = RecoveryScheduler(jobs=manager, hosts=hosts, interval_seconds=1)
    await scheduler.tick(now=now)

    expired = await approvals.get(approval.id)
    assert expired.status == ApprovalStatus.EXPIRED.value
    failed = await manager.require(job.id)
    assert failed.state == "FAILED"



@pytest.mark.asyncio
async def test_preexisting_waiting_host_is_rearmed_and_retried_on_scheduler_start(
    project_registry,
    database,
):
    runner = FakeAgentRunner(resume_session_id="thread-waiting")
    manager, hosts, sessions, _, recovery = await build_runtime(
        database=database,
        projects=project_registry,
        runner=runner,
        restart_grace_seconds=0,
    )
    job = JobRecord(
        id="JOB-PREEXISTING-WAITING-HOST",
        project_id="demo",
        requested_by_channel="test",
        requested_by_user="u1",
        requested_host="lightsail-main",
        assigned_host="lightsail-main",
        instruction="unfinished waiting work",
        state="WAITING_HOST",
        external_session_id="thread-waiting",
    )
    await manager.jobs.add(job)
    await sessions.record(
        job_id=job.id,
        project_id="demo",
        host_id="lightsail-main",
        external_session_id="thread-waiting",
    )

    now = datetime.now(timezone.utc)
    future_retry = now + timedelta(hours=24)
    await recovery.upsert(
        job.id,
        kind=RecoveryKind.HOST.value,
        mode=RecoveryMode.RESUME.value,
        attempt_count=1,
        next_retry_at=future_retry,
        execution_id=None,
        resume_instruction=RESTART_RESUME_INSTRUCTION,
        last_error="host unavailable before controller restart",
    )

    await manager.reconcile_startup(now=now)
    rearmed = await recovery.get(job.id)
    assert rearmed is not None
    retry_at_value = _aware(rearmed.next_retry_at)
    assert retry_at_value <= now

    scheduler = RecoveryScheduler(jobs=manager, hosts=hosts, interval_seconds=60)
    await scheduler.start()
    try:
        await wait_for_state(manager, job.id, "COMPLETED")
    finally:
        await scheduler.stop()

    assert runner.resumed
    assert runner.resumed[0]["session_id"] == "thread-waiting"
