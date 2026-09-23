from pathlib import Path

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
async def test_remote_cancel_unblocks_controller_waiter():
    gateway = RunnerGateway()
    websocket = FakeWebSocket()
    await gateway.attach("desktop-main", websocket)

    handle = await gateway.resume_remote(
        host_id="desktop-main",
        session_id="thread-1",
        instruction="continue",
        working_directory=Path("C:/dev/demo"),
    )
    await handle.cancel()
    result = await handle.wait()

    assert result.returncode == 130
    cancel = Envelope.model_validate_json(websocket.sent[-1])
    assert cancel.type == "JOB_CANCEL"
