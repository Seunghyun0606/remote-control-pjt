import asyncio\nfrom pathlib import Path

import pytest

from remote_control.transport.protocol import Envelope, message
from remote_control.transport.runner_ws import RunnerGateway


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000):
        self.closed.append(code)


@pytest.mark.asyncio
async def test_remote_runner_gateway_result_flow():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.start_remote(
        host_id="desktop-main",
        project_id="demo",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )
    assert websocket.sent

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_ACCEPTED",
            execution_id=handle.execution_id,
            pid=123,
            session_id="thread-1",
        ),
    )
    assert handle.pid == 123

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id=handle.execution_id,
            returncode=0,
            session_id="thread-1",
            final_message="done",
        ),
    )
    result = await handle.wait()
    assert result.returncode == 0
    assert result.session_id == "thread-1"
    assert result.final_message == "done"


@pytest.mark.asyncio
async def test_remote_steering_uses_job_steer_protocol():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.steer_remote(
        host_id="desktop-main",
        session_id="thread-1",
        instruction="backend only",
        working_directory=Path("C:/dev/demo"),
    )

    envelope = Envelope.model_validate_json(websocket.sent[-1])
    assert envelope.type == "JOB_STEER"
    assert envelope.payload["session_id"] == "thread-1"
    assert envelope.payload["instruction"] == "backend only"

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id=handle.execution_id,
            returncode=0,
            session_id="thread-1",
            final_message="done",
        ),
    )
    result = await handle.wait()
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_remote_cancel_waits_for_runner_result_ack():
    gateway = RunnerGateway(cancel_ack_timeout_seconds=1)
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.resume_remote(
        host_id="desktop-main",
        session_id="thread-1",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )

    cancel_task = asyncio.create_task(handle.cancel())
    await asyncio.sleep(0)
    cancel = Envelope.model_validate_json(websocket.sent[-1])
    assert cancel.type == "JOB_CANCEL"
    assert cancel.payload["execution_id"] == handle.execution_id
    assert cancel_task.done() is False

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id=handle.execution_id,
            returncode=130,
            session_id="thread-1",
            final_message="cancelled",
        ),
    )
    await cancel_task
    result = await handle.wait()

    assert result.returncode == 130


@pytest.mark.asyncio
async def test_remote_cancel_disconnect_is_not_treated_as_ack():
    gateway = RunnerGateway(cancel_ack_timeout_seconds=1)
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.resume_remote(
        host_id="desktop-main",
        session_id="thread-1",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )
    cancel_task = asyncio.create_task(handle.cancel())
    await asyncio.sleep(0)

    assert await gateway.detach("desktop-main", websocket) is True
    with pytest.raises(ConnectionError, match="runner disconnected"):
        await cancel_task


@pytest.mark.asyncio
async def test_stale_runner_detach_does_not_fail_new_connection_pending_jobs():
    gateway = RunnerGateway()
    old_websocket = FakeWebSocket()
    new_websocket = FakeWebSocket()
    await gateway.attach("desktop-main", old_websocket)

    handle = await gateway.start_remote(
        host_id="desktop-main",
        project_id="demo",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )

    await gateway.attach("desktop-main", new_websocket)
    assert old_websocket.closed == [1012]

    detached = await gateway.detach("desktop-main", old_websocket)
    assert detached is False
    assert handle._result_future.done() is False
    assert gateway.is_connected("desktop-main") is True

    await gateway.handle(
        "desktop-main",
        message(
            "JOB_RESULT",
            execution_id=handle.execution_id,
            returncode=0,
            session_id="thread-1",
            final_message="done",
        ),
    )
    result = await handle.wait()
    assert result.returncode == 0
