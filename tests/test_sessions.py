import pytest

from remote_control.sessions.registry import SessionRegistry, SessionStatus
from remote_control.storage.repositories import EventRepository, SessionRepository


@pytest.mark.asyncio
async def test_session_registration_and_rebind(database):
    registry = SessionRegistry(
        sessions=SessionRepository(database),
        events=EventRepository(database),
    )

    first = await registry.record(
        job_id="JOB-1",
        project_id="demo",
        host_id="lightsail-main",
        external_session_id="thread-1",
    )
    assert first.status == SessionStatus.ACTIVE.value

    rebound = await registry.record(
        job_id="JOB-1",
        project_id="demo",
        host_id="lightsail-main",
        external_session_id="thread-2",
    )
    assert rebound.id == first.id
    assert rebound.external_session_id == "thread-2"

    paused = await registry.mark("JOB-1", SessionStatus.PAUSED)
    assert paused is not None
    assert paused.status == SessionStatus.PAUSED.value
